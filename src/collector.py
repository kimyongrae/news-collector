"""
뉴스 수집기 v8
수정 사항:
  1) 저장 구조 변경: 월단위 파일 → 연단위 파일 (RSS_뉴스_{연도})
  2) 탭 구조 변경: 날짜별 탭 → 월별 탭 ({연도}-{월}) + INDEX 탭
  3) 연단위 파일 없으면 자동 생성 (1월 정기 생성 + 예외 생성 모두 처리)
  4) INDEX 탭: 월별 수집 건수·마지막 수집시간 자동 갱신
  5) 네이버금융 URL 조합 버그 수정 + 네이버 뉴스 URL로 변환
  6) 네이버금융 본문 에러 감지 → 빈 본문으로 처리
  7) 중복 제거를 카테고리 내로 제한 (금리/환율이 주식과 겹치지 않도록)
  8) 금리/환율 전용 키워드 필터링 추가
  9) 본문 에러 메시지 자동 감지 후 재크롤링 or 빈값 처리
"""

import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz, re, yaml, json, os, sys, time
from urllib.parse import urljoin, urlparse, parse_qs, urlencode, urlunparse
from email.utils import parsedate_to_datetime

# ── .env 로드 (로컬 개발 편의) ─────────────────────────────────
# GitHub Actions 는 repository secrets 를 os.environ 에 직접 주입하므로
# python-dotenv 가 없어도 동작합니다. 로컬에서는 .env 파일로 관리하세요.
try:
    from dotenv import load_dotenv
    # 프로젝트 루트의 .env 를 먼저 찾고, 없으면 cwd 의 .env
    _env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if os.path.exists(_env_path):
        load_dotenv(_env_path)
    else:
        load_dotenv()
except ImportError:
    pass  # dotenv 미설치 — Actions 환경이거나 사용자가 env 를 직접 export 함

import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as google_build
import google.generativeai as genai

# Supabase 동시 쓰기 (실패 격리 — 시트 흐름에는 영향 없음)
from supabase_client import SupabaseClient, to_supabase_row

KST    = pytz.timezone("Asia/Seoul")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ─────────────────────────────────────────────────────────────
# 컬럼 구조 v3 (2026-04-22) — 조회수·순위 2 컬럼 추가.
#   · 순위 : 랭킹 페이지(네이버 popularDay 등) 수집 시 1,2,3... / RSS 는 공백
#   · 조회수 : 랭킹 페이지에서 추출되면 숫자, 아니면 공백.
#              (공백일 때 TubeAI 대시보드가 "—" 로 표시)
# ─────────────────────────────────────────────────────────────
HEADERS    = ["수집시간", "발행시간", "카테고리", "제목", "요약", "주요키워드",
              "언론사",   "출처",    "감성",    "중요도", "순위",  "조회수"]
COL_WIDTHS = [140, 120, 90, 280, 340, 200, 90, 80, 55, 55, 55, 80]
HDR_LAST   = chr(ord('A') + len(HEADERS) - 1)
HDR_RANGE  = f"A1:{HDR_LAST}1"
TITLE_COL  = 4  # D열 (제목)

# 본문 에러 감지 문자열 (이 문자열이 포함되면 본문 무효 처리)
BODY_ERROR_PATTERNS = [
    "방문하시려는 페이지의 주소가 잘못",
    "페이지를 찾을 수 없습니다",
    "존재하지 않는 페이지",
    "404 Not Found",
    "접근이 거부되었습니다",
    "로그인이 필요",
    "이 페이지는 존재하지",
    "The page you requested",
    "Page Not Found",
]

BLOG_SOURCE_DOMAINS = (
    "blog.naver.com",
    "m.blog.naver.com",
    "post.naver.com",
    "m.post.naver.com",
    "brunch.co.kr",
    "tistory.com",
)

BLOG_SOURCE_HINTS = (
    "네이버 블로그",
    "네이버 포스트",
    "브런치",
    "티스토리",
    "blog",
)

def is_body_error(text: str) -> bool:
    """본문이 에러 페이지 내용인지 확인"""
    if not text:
        return False
    for pat in BODY_ERROR_PATTERNS:
        if pat in text:
            return True
    return False


def _entry_source_text(entry) -> str:
    src = entry.get("source") or {}
    if isinstance(src, dict):
        return " ".join(str(src.get(k) or "") for k in ("title", "href", "url"))
    return str(src or "")


def is_blog_source(url: str = "", source_text: str = "", media: str = "") -> bool:
    """블로그/UGC성 출처는 RSS 수집 대상에서 제외."""
    hay = f"{url} {source_text} {media}".lower()
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        host = ""
    if any(domain in host or domain in hay for domain in BLOG_SOURCE_DOMAINS):
        return True
    return any(hint.lower() in hay for hint in BLOG_SOURCE_HINTS)


def keyword_match(text: str, keywords) -> bool:
    if not keywords:
        return True
    hay = (text or "").lower()
    return any(str(kw).lower() in hay for kw in keywords)


# ── OAuth 인증 ───────────────────────────────────────────────
def get_clients():
    """GOOGLE_OAUTH_TOKEN (JSON 문자열) 으로 gspread / drive 클라이언트 생성.

    로컬:          .env 에 GOOGLE_OAUTH_TOKEN 설정 (python-dotenv 자동 로드)
    GitHub Actions: repository secrets 의 GOOGLE_OAUTH_TOKEN 사용
    """
    raw = os.environ.get("GOOGLE_OAUTH_TOKEN", "").strip()
    if not raw:
        raise ValueError(
            "환경변수 GOOGLE_OAUTH_TOKEN 이 없습니다.\n"
            "  · 로컬:   .env 파일에 설정 (python get_token.py 로 발급)\n"
            "  · Actions: Settings → Secrets → Actions 에 등록"
        )
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as err:
        raise ValueError(
            "GOOGLE_OAUTH_TOKEN 이 유효한 JSON 이 아닙니다. "
            "전체 JSON 을 한 줄로 따옴표 없이 붙여넣었는지 확인하세요. "
            f"(파싱 오류: {err})"
        )
    for key in ("refresh_token", "client_id", "client_secret"):
        if not d.get(key):
            raise ValueError(f"GOOGLE_OAUTH_TOKEN JSON 에 '{key}' 필드가 없습니다.")

    creds = Credentials(
        token=None,
        refresh_token=d["refresh_token"],
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        token_uri=d.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=SCOPES,
    )
    return gspread.authorize(creds), google_build("drive", "v3", credentials=creds)


# ── Drive / Sheets 헬퍼 ─────────────────────────────────────

# INDEX 시트 헤더
INDEX_HEADERS = ["월", "수집 건수", "마지막 수집 시간", "시트 링크"]

def find_file_in_folder(drive, folder_id, title):
    q   = (f"name='{title}' and '{folder_id}' in parents "
           f"and mimeType='application/vnd.google-apps.spreadsheet' and trashed=false")
    res = drive.files().list(q=q, fields="files(id,name)").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

def create_spreadsheet_in_folder(gc, drive, folder_id, title):
    """연단위 파일 신규 생성 후 INDEX 탭 초기화"""
    meta    = {"name": title,
               "mimeType": "application/vnd.google-apps.spreadsheet",
               "parents": [folder_id]}
    created = drive.files().create(body=meta, fields="id").execute()
    fid     = created["id"]
    print(f"[신규 연단위 파일 생성] {title} (id: {fid})")
    sp = gc.open_by_key(fid)
    # 기본 Sheet1을 INDEX로 재활용
    ws_index = sp.sheet1
    ws_index.update_title("INDEX")
    _init_index_sheet(sp, ws_index)
    return sp

def _init_index_sheet(sp, ws_index):
    """INDEX 시트 헤더·서식 초기화"""
    ws_index.clear()
    ws_index.append_row(INDEX_HEADERS, value_input_option="RAW")
    ws_index.format("A1:D1", {
        "backgroundColor": {"red": 0.15, "green": 0.15, "blue": 0.45},
        "textFormat": {"bold": True,
                       "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                       "fontSize": 10},
        "horizontalAlignment": "CENTER",
    })
    ws_index.freeze(rows=1)
    # 열 너비 조정
    reqs = []
    for i, w in enumerate([90, 90, 160, 300]):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": ws_index.id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize",
        }})
    sp.batch_update({"requests": reqs})
    print("[INDEX 탭 초기화 완료]")

def get_or_create_annual_spreadsheet(gc, drive, now, folder_id, spreadsheet_id):
    """
    연단위 파일 (RSS_뉴스_{연도}) 가져오기 또는 생성.

    우선순위:
      1) categories.yaml 의 spreadsheet_id 가 있으면 그것을 사용
      2) Drive 폴더에서 해당 연도 파일 검색
      3) 없으면 자동 생성 (1월 정기 생성 + 연중 예외 생성 모두 처리)
    """
    year  = now.year
    title = f"RSS_뉴스_{year}"

    if spreadsheet_id:
        try:
            sp = gc.open_by_key(spreadsheet_id)
            print(f"[기존 파일 (spreadsheet_id)] {sp.title}")
            return sp
        except Exception as e:
            print(f"[spreadsheet_id 실패] {e} → 폴더에서 검색합니다.")

    if not folder_id:
        raise ValueError("categories.yaml 에 folder_id 를 입력해주세요.")

    eid = find_file_in_folder(drive, folder_id, title)
    if eid:
        sp = gc.open_by_key(eid)
        print(f"[기존 연단위 파일 재사용] {title}")
        # INDEX 탭이 없으면 생성 (마이그레이션 대비)
        try:
            sp.worksheet("INDEX")
        except gspread.WorksheetNotFound:
            print("[INDEX 탭 없음 → 새로 생성]")
            ws_index = sp.add_worksheet(title="INDEX", rows=20, cols=4)
            _init_index_sheet(sp, ws_index)
        return sp

    # ── 파일이 없는 경우: 자동 생성 ─────────────────────────
    is_january = (now.month == 1)
    if is_january:
        print(f"[1월 정기 생성] {title}")
    else:
        print(f"[예외 생성] {title} 파일이 없어 자동 생성합니다. (현재 {now.month}월)")
    return create_spreadsheet_in_folder(gc, drive, folder_id, title)


def get_or_create_monthly_worksheet(sp, month_label: str):
    """
    월별 탭 ({연도}-{월}) 가져오기 또는 생성.
    탭 순서: INDEX가 항상 첫 번째, 월별 탭은 월 순서로 정렬.
    """
    try:
        ws = sp.worksheet(month_label)
        print(f"[기존 월탭] {month_label}")
        return ws
    except gspread.WorksheetNotFound:
        pass

    ws       = sp.add_worksheet(title=month_label, rows=3000, cols=len(HEADERS) + 1)
    sheet_id = ws.id

    ws.append_row(HEADERS, value_input_option="RAW")
    ws.format(HDR_RANGE, {
        "backgroundColor": {"red": 0.1, "green": 0.15, "blue": 0.5},
        "textFormat": {"bold": True,
                       "foregroundColor": {"red": 1, "green": 1, "blue": 1},
                       "fontSize": 10},
        "horizontalAlignment": "CENTER",
    })
    ws.freeze(rows=1)

    reqs = []
    for i, w in enumerate(COL_WIDTHS):
        reqs.append({"updateDimensionProperties": {
            "range": {"sheetId": sheet_id, "dimension": "COLUMNS",
                      "startIndex": i, "endIndex": i + 1},
            "properties": {"pixelSize": w}, "fields": "pixelSize",
        }})
    reqs.append({"setBasicFilter": {"filter": {"range": {
        "sheetId": sheet_id,
        "startRowIndex": 0, "endRowIndex": 1,
        "startColumnIndex": 0, "endColumnIndex": len(HEADERS),
    }}}})
    sp.batch_update({"requests": reqs})

    # INDEX 탭 다음 위치로 탭 순서 정렬
    _reorder_sheet_after_index(sp, sheet_id, month_label)

    print(f"[새 월탭 생성] {month_label}")
    return ws


def _reorder_sheet_after_index(sp, new_sheet_id: int, month_label: str):
    """새로 만든 월탭을 INDEX 바로 뒤, 월 순서에 맞게 정렬"""
    try:
        worksheets = sp.worksheets()
        # INDEX는 항상 index=0 유지
        # 월탭들을 이름 기준 정렬하여 적절한 위치 계산
        month_sheets = [ws for ws in worksheets
                        if ws.title != "INDEX" and ws.id != new_sheet_id]
        month_sheets.sort(key=lambda ws: ws.title)

        # 새 탭의 목표 위치: INDEX(0) + 정렬된 월탭 중 자신의 순서
        all_months = sorted([ws.title for ws in month_sheets] + [month_label])
        target_index = all_months.index(month_label) + 1  # +1 = INDEX 이후

        sp.batch_update({"requests": [{
            "updateSheetProperties": {
                "properties": {"sheetId": new_sheet_id, "index": target_index},
                "fields": "index",
            }
        }]})
    except Exception as e:
        print(f"[탭 순서 정렬 실패] {e}")


def _month_label_to_tuple(label: str):
    """'YYYY-MM' → (year, month) 튜플. 파싱 실패 시 None."""
    try:
        parts = label.split("-")
        if len(parts) != 2: return None
        return (int(parts[0]), int(parts[1]))
    except (ValueError, AttributeError):
        return None


def _months_back(year: int, month: int, n: int):
    """(year, month) 에서 n 개월 뒤로 간 (year, month) 반환. 음수 보정."""
    total = year * 12 + (month - 1) - n
    return (total // 12, total % 12 + 1)


def _is_month_older_than_cutoff(label: str, cutoff_year: int, cutoff_month: int) -> bool:
    """label 이 cutoff (포함) 보다 이전이면 True — 즉 삭제 대상."""
    t = _month_label_to_tuple(label)
    if t is None: return False
    return t < (cutoff_year, cutoff_month)


def prune_old_month_tabs(gc, drive, now, folder_id: str, retention_months: int = 6) -> int:
    """매월 1일 호출 — 6개월 retention 을 넘긴 월탭/연단위 시트를 삭제.

    예시 (retention=6):
      now=2026-07 → 보관 [2026-07,...,2026-02] · 삭제 ≤ 2026-01
      now=2026-08 → 보관 [2026-08,...,2026-03] · 삭제 ≤ 2026-02
    cross-year 도 동일 — now=2026-03 은 2025-10 까지 보관, 2025-09 이전 삭제.

    동작:
      1) 폴더의 모든 RSS_뉴스_* 스프레드시트 순회
      2) 각 시트에서 cutoff 보다 오래된 월탭 삭제 + INDEX 행 제거
      3) 시트에 INDEX 외 월탭이 하나도 안 남으면 시트 자체를 휴지통으로 이동

    반환: 삭제한 월탭 개수.
    """
    cutoff_year, cutoff_month = _months_back(now.year, now.month, retention_months - 1)
    print(f"[정리] retention={retention_months}개월 · 보관 시작 (포함) = {cutoff_year:04d}-{cutoff_month:02d} · "
          f"이 이전 월탭은 모두 삭제")

    if not folder_id:
        print("[정리] folder_id 없음 — 단일 시트만 정리합니다 (스프레드시트 자체 삭제는 skip).")
        return 0

    deleted_total = 0
    deleted_files = 0
    try:
        # RSS_뉴스_YYYY 형식 스프레드시트 모두 조회
        q = (f"'{folder_id}' in parents and "
             f"mimeType='application/vnd.google-apps.spreadsheet' and "
             f"name contains 'RSS_뉴스_' and trashed=false")
        files = drive.files().list(q=q, fields="files(id, name)").execute().get("files", [])
    except Exception as e:
        print(f"[정리] 스프레드시트 목록 조회 실패: {e}")
        return 0

    for f in files:
        sp_id   = f.get("id")
        sp_name = f.get("name", "")
        try:
            sp = gc.open_by_key(sp_id)
        except Exception as e:
            print(f"[정리] 시트 열기 실패 ({sp_name}): {e}")
            continue

        worksheets = sp.worksheets()
        month_tabs = [ws for ws in worksheets
                      if ws.title != "INDEX" and _month_label_to_tuple(ws.title)]
        old_tabs   = [ws for ws in month_tabs
                      if _is_month_older_than_cutoff(ws.title, cutoff_year, cutoff_month)]

        if not old_tabs:
            print(f"[정리] {sp_name}: 삭제 대상 없음 ({len(month_tabs)}개 월탭 모두 보관 범위)")
            continue

        # 월탭 + INDEX 행 삭제
        ws_index = None
        try: ws_index = sp.worksheet("INDEX")
        except gspread.WorksheetNotFound: pass

        for ws in old_tabs:
            label = ws.title
            try:
                sp.del_worksheet(ws)
                deleted_total += 1
                print(f"[정리] {sp_name}: 월탭 삭제 → {label}")
            except Exception as e:
                print(f"[정리] {sp_name}: 월탭 삭제 실패 {label}: {e}")
                continue

            # INDEX 에서 해당 월 행 제거
            if ws_index is not None:
                try:
                    rows = ws_index.get_all_values()
                    for i, row in enumerate(rows):
                        if i == 0: continue  # 헤더
                        if row and row[0] == label:
                            ws_index.delete_rows(i + 1)
                            print(f"[정리] {sp_name}: INDEX 행 제거 → {label}")
                            break
                except Exception as e:
                    print(f"[정리] {sp_name}: INDEX 행 제거 실패 {label}: {e}")

        # 시트에 월탭이 하나도 안 남으면 휴지통으로 (INDEX 만 있으면 빈 시트라 의미 없음)
        try:
            remaining = [ws for ws in sp.worksheets()
                         if ws.title != "INDEX" and _month_label_to_tuple(ws.title)]
            if not remaining:
                drive.files().update(fileId=sp_id, body={"trashed": True}).execute()
                deleted_files += 1
                print(f"[정리] {sp_name}: 월탭 0개 → 시트 자체를 휴지통으로 이동")
        except Exception as e:
            print(f"[정리] {sp_name}: 빈 시트 정리 실패: {e}")

    print(f"[정리] 완료 · 삭제 월탭 {deleted_total}개 · 휴지통 시트 {deleted_files}개")
    return deleted_total


def update_index_sheet(sp, month_label: str, count: int, collected_at: str):
    """
    INDEX 탭의 해당 월 행을 갱신 (없으면 추가).
    컬럼: 월 | 수집 건수 | 마지막 수집 시간 | 시트 링크
    """
    try:
        ws_index = sp.worksheet("INDEX")
    except gspread.WorksheetNotFound:
        print("[INDEX 탭 없음 → 건너뜀]")
        return

    sheet_link = f'=HYPERLINK("#gid={sp.worksheet(month_label).id}","{month_label}")'

    all_vals = ws_index.get_all_values()
    # 헤더 제외하고 월 컬럼(A열) 검색
    for row_idx, row in enumerate(all_vals[1:], start=2):
        if row and row[0] == month_label:
            # 기존 행 업데이트 (수집 건수 누적)
            existing_count = int(row[1]) if (len(row) > 1 and str(row[1]).isdigit()) else 0
            ws_index.update(
                f"A{row_idx}:D{row_idx}",
                [[month_label, existing_count + count, collected_at, sheet_link]],
                value_input_option="USER_ENTERED",
            )
            print(f"[INDEX 갱신] {month_label} → 누적 {existing_count + count}건")
            return

    # 없으면 새 행 추가
    ws_index.append_row(
        [month_label, count, collected_at, sheet_link],
        value_input_option="USER_ENTERED",
    )
    print(f"[INDEX 추가] {month_label} → {count}건")

def load_title_sets_by_category(ws):
    """월탭의 기존 제목을 카테고리별로 로드해 중복 방지용 세트 반환"""
    rows = ws.get_all_values()
    title_sets = {}
    for row in rows[1:]:
        category = row[2].strip() if len(row) > 2 else ""
        title = row[TITLE_COL - 1].strip() if len(row) >= TITLE_COL else ""
        if category and title:
            title_sets.setdefault(category, set()).add(title)
    return title_sets


# ── 기사 본문 크롤링 ─────────────────────────────────────────
BODY_SELECTORS = [
    "div#newsct_article",        # 네이버 뉴스 (신버전)
    "div#articleBodyContents",   # 네이버 뉴스 (구버전)
    "div#articeBody",
    "article.article-body",
    "div.article-body",
    "div.article_body",
    "div.news_end",
    "section.article_section",
    "div.article-view-content-div",
    "div.view_text",
    "div.article_txt",
    "div#article-view-content-div",
    "div.reporter_article",
    "div#content-body",
]

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

def clean_body(text: str) -> str:
    """
    수집된 본문에서 노이즈 제거:
    - 사진=게티이미지뱅크, 【사진=연합뉴스】 등 캡션
    - [앵커] [앵커멘트] 방송 마커
    - 기자명, ※ 면책문구, ◆◇ 특수기호 메타라인
    - 저작권/무단전재/구독유도 문구
    - 의미 없는 짧은 잔여 문장 제거 (문장 단위 필터)
    """
    if not text:
        return ""

    # ── 1단계: 패턴 제거 ──────────────────────────────────
    # 사진/영상/그래픽 캡션 (= 기호)
    text = re.sub(
        r'(사진|영상|그래픽|자료|AP|EPA|AFP|로이터|Reuters|게티이미지|Getty)\s*=\s*[^\s,。.]{1,40}',
        '', text
    )
    # 【...】 대괄호 이미지/출처 태그
    text = re.sub(
        r'[【\[]\s*[^\]】]{0,50}(사진|영상|그래픽|자료|=|AP|EPA|AFP)[^\]】]{0,50}[\]】]',
        '', text
    )
    text = re.sub(r'【[^】]{0,80}】', '', text)

    # [앵커] [앵커멘트] 방송 마커
    text = re.sub(r'\[(앵커멘트?|리포트?|기자|출처|자료|영상|사진|속보)\]', '', text)

    # 기자명 패턴
    text = re.sub(r'[가-힣]{2,5}\s*(기자|특파원|논설위원|칼럼니스트|편집장)\s*', '', text)

    # ※ 주의/면책 문구 (줄 끝까지)
    text = re.sub(r'※[^\n。.]*', '', text)

    # 특수기호 메타라인 (◆◇△▲▶ 등으로 시작하는 줄)
    text = re.sub(r'[◆◇△▲▶●■□◀▼★☆]+[^\n]{0,150}', '', text)

    # 저작권/무단전재
    text = re.sub(
        r'(무단\s*전재|저작권자|Copyright|All\s*rights?\s*reserved)[^\n。.]{0,100}',
        '', text, flags=re.IGNORECASE
    )

    # 구독·바로가기·제보 유도 문구 (네이버 뉴스 포함)
    noise_patterns = [
        r'바로가기단?기?',
        r'주요기[로]?선정',
        r'언론사를?\s*바로선',
        r'언론사\s*홈[으]?로',
        r'기사\s*제보',
        r'뉴스\s*제보',
        r'앱으로\s*보기',
        r'만나보세요',
        r'더\s*많은\s*기사',
        r'전체\s*기사\s*보기',
        r'메인\s*뉴스에서\s*\S+\s*주요뉴스를\s*볼\s*수\s*있습니다',
        r'이\s*기사는\s*[\w\s]{0,20}(으로|통해)\s*제공',
        r'구독\s*하기',
        r'뉴스레터\s*구독',
    ]
    for pat in noise_patterns:
        text = re.sub(pat, '', text, flags=re.IGNORECASE)

    # HTML 잔여 태그
    text = re.sub(r'<[^>]+>', ' ', text)

    # ── 2단계: 문장 단위 필터 ─────────────────────────────
    # 의미있는 문장(한글 5자 이상 & 전체 10자 이상)만 남김
    parts = re.split(r'(?<=[。.!?])\s+|\n', text)
    good = []
    for s in parts:
        s = re.sub(r'\s{2,}', ' ', s).strip()
        if not s:
            continue
        korean_count = len(re.findall(r'[가-힣]', s))
        if korean_count >= 5 and len(s) >= 10:
            good.append(s)

    result = ' '.join(good) if good else text
    result = re.sub(r'\s{2,}', ' ', result).strip()
    return result


def fetch_body(url: str, referer: str = "https://www.google.com/", timeout: int = 10) -> str:
    """기사 URL → 본문 텍스트 추출 후 clean_body 적용."""
    if not url or not url.startswith("http"):
        return ""
    try:
        resp = SESSION.get(url, headers={"Referer": referer}, timeout=timeout)
        resp.raise_for_status()
    except Exception as e:
        print(f"    [크롤링 실패] {url[:70]} → {e}")
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")

    raw = ""
    for sel in BODY_SELECTORS:
        tag = soup.select_one(sel)
        if tag:
            raw = tag.get_text(separator=" ", strip=True)
            if len(raw) > 80:
                break

    # fallback: <p> 묶기
    if not raw or len(raw) < 80:
        paragraphs = soup.find_all("p")
        raw = " ".join(
            p.get_text(strip=True)
            for p in paragraphs
            if len(p.get_text(strip=True)) > 30
        )

    if is_body_error(raw):
        return ""

    # ✅ 핵심: 본문 정제
    cleaned = clean_body(raw)
    return cleaned[:1800] if cleaned else ""


# ── ✅ FIX: 네이버 금융 뉴스 URL 정규화 ────────────────────
def normalize_naver_news_url(href: str) -> str:
    """
    네이버 금융 링크를 네이버 뉴스 URL로 변환.
    /news/read.nhn?mode=...&oid=001&aid=0014... 
      → https://n.news.naver.com/article/001/0014...
    /news/article.nhn?oid=...&aid=...
      → https://n.news.naver.com/article/oid/aid
    """
    if not href:
        return ""

    # 이미 완전한 URL이면 그대로
    if href.startswith("http"):
        full = href
    elif href.startswith("//"):
        full = "https:" + href
    elif href.startswith("/"):
        full = "https://finance.naver.com" + href
    else:
        full = "https://finance.naver.com/" + href

    parsed = urlparse(full)
    qs = parse_qs(parsed.query)

    oid = qs.get("oid", qs.get("office_id", [None]))[0]
    aid = qs.get("aid", qs.get("article_id", [None]))[0]

    if oid and aid:
        # 네이버 뉴스 표준 URL
        return f"https://n.news.naver.com/article/{oid}/{aid}"

    return full


# ── ✅ FIX: 네이버 금융 뉴스 파서 ──────────────────────────
def fetch_naver_finance(section_url: str, label: str, limit: int = 8) -> list:
    """
    네이버 금융 뉴스 페이지 HTML 파싱.
    링크를 n.news.naver.com 표준 URL로 변환 후 본문 크롤링.
    """
    try:
        resp = SESSION.get(
            section_url,
            headers={"Referer": "https://finance.naver.com/"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [네이버금융 실패] {label} → {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── 링크 후보 추출 (여러 선택자 순서대로 시도) ──────────
    candidates = []

    # 방법 1: 뉴스 목록 형태
    for sel in [
        "ul.newsList li a",
        "dl.articleSubject dd a",
        ".articleSubject a",
        ".news_list li a",
        "div.list_news a.news_tit",
        "ul.type06_headline li a",
        "ul.type06 li a",
    ]:
        found = soup.select(sel)
        if found:
            candidates = found
            print(f"    [선택자 히트] {sel} ({len(found)}개)")
            break

    # 방법 2: fallback - href에 oid/aid 가 있는 a 태그 전부
    if not candidates:
        candidates = [
            a for a in soup.find_all("a", href=True)
            if ("oid=" in a.get("href","") or "article" in a.get("href",""))
            and len(a.get_text(strip=True)) > 8
        ]
        print(f"    [fallback 링크] {len(candidates)}개")

    arts = []
    seen_titles = set()

    for a in candidates:
        title = a.get_text(strip=True)
        href  = a.get("href", "")

        if not title or len(title) < 6:
            continue
        if title in seen_titles:
            continue
        seen_titles.add(title)

        # ✅ URL 정규화
        url = normalize_naver_news_url(href)
        if not url:
            continue

        print(f"    [크롤링] {url[:70]}")
        body = fetch_body(url, referer="https://finance.naver.com/")

        # 에러 페이지 감지
        if is_body_error(body):
            print(f"      → 에러 페이지 감지, 스킵")
            body = ""

        arts.append({
            "title": title,
            "url":   url,
            "media": label,
            "pub":   "",
            "raw":   body,
            "rank":  "",
            "views": "",
        })
        time.sleep(0.3)

        if len(arts) >= limit:
            break

    print(f"    → {len(arts)}건 수집 완료")
    return arts


# ── ✅ 네이버 랭킹 뉴스 (조회순/댓글순) ─────────────────────────
# 랭킹 페이지 예:
#   · https://news.naver.com/main/ranking/popularDay.naver  (많이 본 뉴스 — 전 영역)
#   · https://news.naver.com/main/ranking/popularMemo.naver (댓글 많은 뉴스)
#   · 섹션별: ...?sectionId=101 (경제), 105 (IT), 100 (정치) 등
# 순위·댓글수·조회수를 추출해 rank/views 필드로 보존.
def fetch_naver_ranking(section_url: str, label: str, limit: int = 12,
                        ranking_type: str = "view") -> list:
    """
    네이버 뉴스 랭킹 페이지를 파싱해 순위·조회수(혹은 댓글수) 포함 기사 목록 반환.

    ranking_type:
      · "view"    → 조회순 랭킹 (page views)
      · "comment" → 댓글 많은 순 랭킹
    """
    try:
        resp = SESSION.get(
            section_url,
            headers={"Referer": "https://news.naver.com/"},
            timeout=15,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"  [네이버 랭킹 실패] {label} → {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    # 랭킹 페이지 구조: <ul class="rankingnews_list"> 안에 <li class="as_thumb">...
    # 각 <li> 에 순위 숫자 + 제목 링크 + 조회수/댓글수 span
    arts = []
    rank_items = soup.select(
        "ul.rankingnews_list li, div.rankingnews_box li, li.as_thumb, div.box_ranking li"
    )
    if not rank_items:
        # fallback — 일반 a 태그 중 랭킹 숫자 근처
        rank_items = soup.select("ol.ranking_list li, ul.lst_type01 li")

    rank_counter = 0
    seen = set()
    for li in rank_items:
        rank_counter += 1
        a = li.select_one("a.list_title, a.list_headline, a[href*='n.news.naver'], a[href*='news.naver']")
        if not a:
            # 마지막 fallback
            a = li.find("a")
        if not a or not a.get("href"):
            continue
        title = a.get_text(strip=True)
        if not title or len(title) < 6:
            continue
        if title in seen:
            continue
        seen.add(title)

        url = a.get("href", "")
        if url.startswith("/"):
            url = "https://news.naver.com" + url

        # 조회수/댓글수 추출 — span.list_view, span.list_commend, span.count 등
        views = ""
        for sel in [
            "span.list_view", "span.count_view", "em.view", "span.view",
            "span.list_commend", "span.count_comment", "em.comment", "span.comment_count",
        ]:
            el = li.select_one(sel)
            if el:
                txt = re.sub(r"[^\d]", "", el.get_text())
                if txt:
                    views = txt
                    break

        # 언론사 — span.list_press, span.wrt_tit 등
        media_press = label
        for sel in ["span.list_press", "span.wrt_tit", "em.info_press"]:
            el = li.select_one(sel)
            if el:
                media_press = el.get_text(strip=True)
                break

        print(f"    #{rank_counter} [{views or '조회수N/A'}] {title[:50]}")
        body = fetch_body(url, referer="https://news.naver.com/")
        if is_body_error(body):
            body = ""

        arts.append({
            "title": title,
            "url":   url,
            "media": media_press,
            "pub":   "",
            "raw":   body,
            "rank":  rank_counter,      # 1,2,3... (랭킹 페이지 순서 보존)
            "views": views,             # "12345" 문자열 (빈값 가능)
            "rank_type": ranking_type,  # "view" | "comment"
        })
        time.sleep(0.25)

        if len(arts) >= limit:
            break

    print(f"    → {len(arts)}건 (랭킹 {ranking_type}) 수집 완료")
    return arts


# ── RSS 수집 ─────────────────────────────────────────────────
def fetch_rss(
    url,
    label="",
    limit=10,
    crawl_body=True,
    keyword_filter=None,
    exclude_keyword_filter=None,
    scan_multiplier=3,
    keyword_match_fields="title",
):
    """
    RSS 파싱. summary 짧으면 기사 직접 크롤링.
    keyword_filter: 제목에 이 키워드 중 하나 이상 포함된 기사만 수집
    exclude_keyword_filter: 제목에 이 키워드가 있으면 제외 (광범위 카테고리 중복 방지)
    """
    try:
        feed  = feedparser.parse(url)
        media = label or feed.feed.get("title", url)
        arts  = []

        scan_count = max(limit, limit * max(int(scan_multiplier or 3), 1))
        for e in feed.entries[:scan_count]:  # 필터링 감안해 여유있게 가져옴
            title = e.get("title", "").strip()
            if not title:
                continue

            link = e.get("link", "")
            source_text = _entry_source_text(e)
            if is_blog_source(link, source_text, media):
                continue

            pub = ""
            for f in ("published", "updated", "created"):
                raw = e.get(f, "")
                if raw:
                    try:
                        pub = parsedate_to_datetime(raw).astimezone(KST).strftime("%Y-%m-%d %H:%M")
                    except:
                        pub = raw[:16]
                    break

            rss_text = BeautifulSoup(
                e.get("summary", e.get("description", "")), "html.parser"
            ).get_text()[:1800]
            # ✅ RSS summary도 정제
            rss_text = clean_body(rss_text)

            fields = set(str(keyword_match_fields or "title").replace(",", " ").split())
            filter_parts = [title]
            if "summary" in fields:
                filter_parts.append(rss_text)
            if "source" in fields:
                filter_parts.append(source_text)
            filter_text = " ".join(filter_parts)
            if not keyword_match(filter_text, keyword_filter):
                continue
            if exclude_keyword_filter and keyword_match(title, exclude_keyword_filter):
                continue

            # 본문 크롤링 (RSS summary 짧거나 없을 때)
            if crawl_body and len(rss_text.strip()) < 100 and link:
                body = fetch_body(link)
                raw_text = body if (body and not is_body_error(body)) else rss_text
            else:
                raw_text = rss_text if not is_body_error(rss_text) else ""

            arts.append({
                "title": title,
                "url":   link,
                "media": media,
                "pub":   pub,
                "raw":   raw_text,
                "rank":  "",     # RSS 는 순위 없음
                "views": "",     # RSS 는 조회수 없음
            })

            if len(arts) >= limit:
                break

        return arts
    except Exception as ex:
        print(f"  [RSS 실패] {url} → {ex}")
        return []


# ── 웹 수집 ──────────────────────────────────────────────────
def fetch_web(url, selectors, label="", limit=10):
    try:
        resp = SESSION.get(url, timeout=12)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [웹 실패] {url} → {e}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    arts = []
    for item in soup.select(selectors.get("items", "article"))[:limit]:
        te = item.select_one(selectors.get("title", "h2"))
        le = item.select_one(selectors.get("link",  "a"))
        de = item.select_one(selectors.get("summary", "p"))
        t  = te.get_text(strip=True) if te else ""
        l  = le["href"] if le and le.has_attr("href") else url
        if l.startswith("/"):
            b = urlparse(url); l = f"{b.scheme}://{b.netloc}{l}"
        s = de.get_text(strip=True)[:600] if de else ""
        if not s and l:
            s = fetch_body(l)
        if t:
            arts.append({
                "title": t, "url": l, "media": label or url, "pub": "", "raw": s,
                "rank": "", "views": "",
            })
    return arts


# ── Gemini AI ────────────────────────────────────────────────
# 배치 크기: 한 번의 API 호출에 묶을 기사 수.
#   무료 쿼터가 일 20회(가입 직후) ~ 분당 5회 로 매우 타이트해서, 배치를 크게 묶어
#   호출 횟수 자체를 줄여야 fallback 없이 최대한 많은 기사를 AI 처리할 수 있다.
#   (20개씩 묶으면 1회 호출에 20 기사 처리 → 일 400 기사까지 AI 로 커버)
AI_BATCH_SIZE = int(os.environ.get("AI_BATCH_SIZE", "20"))

# API 호출 간 딜레이 (초) - rate limit 방지
AI_CALL_DELAY = float(os.environ.get("AI_CALL_DELAY", "1.5"))

# ── 쿼터 소진 / Gemini 비활성 플래그 ──────────────────────────
# Gemini 는 **선택 사항** — 규칙 기반 fallback(extract_kw / _guess_sentiment /
#   _guess_importance / 본문 앞 300자 요약) 이 항상 동작하므로 API 키 없어도 OK.
#
# · SKIP_GEMINI=1 env 설정 시 처음부터 전체 fallback 으로만 처리 (쿼터 걱정 불필요).
# · 세션 중 429(쿼터 초과) 에러가 발생하면 _gemini_circuit_open=True 로 전환되어
#   남은 모든 기사는 추가 API 호출 없이 fallback 으로 즉시 처리 (무의미한 재시도 방지).
_SKIP_GEMINI_ENV = os.environ.get("SKIP_GEMINI", "").strip().lower() in ("1", "true", "yes")
_gemini_circuit_open = False

_gemini_model = None

def _is_quota_err(exc) -> bool:
    s = str(exc).lower()
    return ("429" in s) or ("quota" in s) or ("rate limit" in s) or ("resource_exhausted" in s)

def _trip_gemini_circuit(reason: str = ""):
    """쿼터 소진 감지 시 호출 — 이후 _get_model() 은 None 반환 → 전체 fallback."""
    global _gemini_circuit_open
    if not _gemini_circuit_open:
        _gemini_circuit_open = True
        print(f"  ⚠ Gemini 쿼터 소진 감지 → 남은 기사는 규칙 기반 fallback 으로 처리합니다. ({reason})", file=sys.stderr)

def _get_model():
    global _gemini_model
    # SKIP_GEMINI 또는 circuit open 상태이면 API 호출 자체를 하지 않음
    if _SKIP_GEMINI_ENV or _gemini_circuit_open:
        return None
    if _gemini_model is None:
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            print("[경고] GEMINI_API_KEY 미설정 — AI 분석이 단순 추출로 fallback 됩니다", file=sys.stderr)
            return None
        genai.configure(api_key=api_key)
        _gemini_model = genai.GenerativeModel("gemini-2.5-flash")
    return _gemini_model


def _fallback_single(title, raw):
    """API 없거나 실패 시 단순 추출"""
    return {
        "keywords":   extract_kw(title + " " + raw),
        "summary":    (raw[:300] if raw.strip() else title),
        "sentiment":  _guess_sentiment(title),
        "importance": _guess_importance(title),
    }


def _guess_sentiment(title: str) -> str:
    """제목 키워드로 감성 추측 (fallback용)"""
    pos = ["상승","급등","호재","흑자","성장","회복","돌파","역대","최고","수혜","긍정","개선","완화"]
    neg = ["하락","급락","폭락","악재","적자","위기","경고","우려","규제","제재","손실","붕괴","파산","긴축","불안"]
    pos_cnt = sum(1 for w in pos if w in title)
    neg_cnt = sum(1 for w in neg if w in title)
    if pos_cnt > neg_cnt:   return "긍정"
    if neg_cnt > pos_cnt:   return "부정"
    return "중립"


def _guess_importance(title: str) -> str:
    """제목 키워드로 중요도 추측 (fallback용)"""
    high5 = ["기준금리","FOMC","Fed","연준","긴급","인수합병","파산","디폴트","전쟁","제재"]
    high4 = ["GDP","CPI","실적","영업이익","환율","관세","정책","예산","금리","IPO","공매도","상장폐지"]
    low1  = ["인터뷰","프로필","취미","연예","스포츠","날씨","광고"]
    if any(w in title for w in high5): return "5"
    if any(w in title for w in high4): return "4"
    if any(w in title for w in low1):  return "1"
    return "3"


def _parse_batch_response(text: str, count: int) -> list:
    """
    배치 응답 파싱. 모델이 JSON 배열로 반환한다고 가정.
    실패 시 None 반환 → 호출자가 개별 재시도.
    """
    text = re.sub(r"```json\s*|```\s*", "", text).strip()
    # 배열로 감싸져 있지 않으면 감싸기 시도
    if not text.startswith("["):
        # { ... }{ ... } 형태 → [{...},{...}] 로 변환
        text = "[" + re.sub(r"}\s*{", "},{", text) + "]"
    try:
        arr = json.loads(text)
        if isinstance(arr, list) and len(arr) == count:
            return arr
    except Exception:
        pass
    return None


def ai_process_batch(articles: list, category: str) -> list:
    """
    articles: [{"title":..., "raw":...}, ...]
    반환:     [{"keywords":..., "summary":..., "sentiment":..., "importance":...}, ...]
    배치로 Gemini 1회 호출. 실패 시 개별 재시도.
    circuit open(쿼터 소진) / API 키 없음 / SKIP_GEMINI=1 인 경우 전체 fallback.
    """
    model = _get_model()

    # API 키 없음 / SKIP_GEMINI / circuit open — 전체 fallback (추가 호출 없음)
    if model is None:
        return [_fallback_single(a["title"], a.get("raw","")) for a in articles]

    # 기사 목록 직렬화
    items_txt = ""
    for i, a in enumerate(articles, 1):
        raw = a.get("raw","").strip() or f"[본문없음] {a['title']}"
        items_txt += f"""
--- 기사 {i} ---
제목: {a['title']}
본문: {raw[:800]}
"""

    prompt = f"""아래 {len(articles)}개의 {category} 뉴스를 분석해, 
반드시 JSON 배열 [{{"id":1,...}}, {{"id":2,...}}, ...]  형태로만 답하세요.
마크다운·코드블록 없이 순수 JSON 배열만 출력.

분석 규칙:
- keywords: 기사별 고유 핵심어 5개 이하, 쉼표 구분
  ① 고유명사·수치·정책명·기업명·인물명 우선 (예: 삼성전자, 기준금리 3.5%, FOMC, 트럼프 관세)  
  ② 맥락 없는 일반어 금지: 관련·규모·이후·증가·하락·상승·현재 등
- summary: 3~4문장, 수치/인물/기관명 포함, 투자자 시각
- sentiment: 반드시 "긍정" / "부정" / "중립" 중 하나만. 
  긍정=주가·매출·실적 호재·정책 완화 등 / 부정=손실·규제·리스크·하락 등 / 중립=사실보도·동향
  ※ 대부분을 "중립"으로 처리하지 말 것. 명확히 긍·부정 신호가 있으면 반드시 표시.
- importance: 1~5 숫자만
  5=시장 즉각 영향(기준금리결정·대형M&A·국가부도·전쟁)
  4=주요 경제지표·기업실적·환율급변·규제 발표
  3=일반 경제기사·산업 동향
  2=단순 동향·전망 기사
  1=광고성·인물소개·잡보
  ※ 모두 3으로 처리하지 말 것. 내용을 보고 실제 시장 영향력을 평가.

{items_txt}

출력 형식 (id는 1부터 {len(articles)}까지):
[
  {{"id": 1, "keywords": "...", "summary": "...", "sentiment": "긍정|부정|중립", "importance": "1~5"}},
  ...
]"""

    try:
        resp = model.generate_content(prompt)
        parsed = _parse_batch_response(resp.text, len(articles))
        if parsed:
            results = []
            for i, (a, d) in enumerate(zip(articles, parsed)):
                sent = d.get("sentiment", "")
                imp  = str(d.get("importance", ""))
                # 유효성 검증
                if sent not in ("긍정", "부정", "중립"):
                    sent = _guess_sentiment(a["title"])
                if not imp.isdigit() or not (1 <= int(imp) <= 5):
                    imp = _guess_importance(a["title"])
                results.append({
                    "keywords":   d.get("keywords", extract_kw(a["title"])),
                    "summary":    d.get("summary",  a.get("raw","")[:300] or a["title"]),
                    "sentiment":  sent,
                    "importance": imp,
                })
            print(f"  [AI 배치] {len(articles)}건 처리 완료")
            return results
        else:
            print(f"  [AI 배치 파싱 실패] 개별 재시도...")
    except Exception as ex:
        # 쿼터 초과 — circuit 열고 즉시 fallback. 재시도 시간 낭비 방지.
        if _is_quota_err(ex):
            _trip_gemini_circuit(f"배치 호출 중 {str(ex)[:80]}")
            return [_fallback_single(a["title"], a.get("raw","")) for a in articles]
        print(f"  [AI 배치 실패] {ex} → 개별 재시도...")

    # 배치 실패 (non-quota) → 개별 재시도
    time.sleep(AI_CALL_DELAY)
    return [_ai_process_single(a["title"], a.get("raw",""), category) for a in articles]


def _ai_process_single(title: str, raw: str, category: str) -> dict:
    """단건 처리 (배치 실패 fallback)"""
    model = _get_model()
    if model is None:
        return _fallback_single(title, raw)
    if not raw.strip():
        raw = f"[본문없음] {title}"

    prompt = f"""다음 {category} 뉴스를 분석해 JSON으로만 답하세요. 마크다운 없이 순수 JSON.
제목: {title}
본문: {raw[:800]}

{{
  "keywords":   "핵심어5개, 쉼표구분, 고유명사·수치·기업명 우선",
  "summary":    "3~4문장, 수치·인물·기관명 포함, 투자자 시각",
  "sentiment":  "긍정 또는 부정 또는 중립 (명확한 호재/악재는 반드시 긍정/부정)",
  "importance": "1~5 숫자만 (5=금리결정·대형M&A, 4=실적·지표, 3=일반기사, 2=동향, 1=잡보)"
}}"""
    try:
        resp  = model.generate_content(prompt)
        text  = re.sub(r"```json\s*|```\s*", "", resp.text).strip()
        d     = json.loads(text)
        sent  = d.get("sentiment", "")
        imp   = str(d.get("importance", ""))
        if sent not in ("긍정", "부정", "중립"):
            sent = _guess_sentiment(title)
        if not imp.isdigit() or not (1 <= int(imp) <= 5):
            imp = _guess_importance(title)
        return {
            "keywords":   d.get("keywords",  extract_kw(title)),
            "summary":    d.get("summary",   raw[:300] or title),
            "sentiment":  sent,
            "importance": imp,
        }
    except Exception as ex:
        if _is_quota_err(ex):
            _trip_gemini_circuit(f"단건 호출 중 {str(ex)[:80]}")
        else:
            print(f"  [AI 단건 실패] {ex}")
        return _fallback_single(title, raw)


def extract_kw(text):
    """
    Gemini API 없을 때 fallback 키워드 추출.
    경제 뉴스 전용 불용어 확장 + 2글자 일반어 필터링.
    """
    # 기본 조사/접속사
    basic_sw = {
        "의","을","를","이","가","은","는","에","와","과","도","로","으로","에서",
        "한","하는","있는","없는","위한","대한","관한","통해","따라","위해","대해",
        "때문","경우","이번","지난","최근","현재","앞으로","이후","이전","당시",
        "모든","같은","다른","이런","저런","그런","어떤",
    }
    # ✅ 경제 뉴스 전용 일반 불용어 (단독으로 키워드가 되면 안 되는 단어)
    economy_sw = {
        "관련","규모","이후","증가","하락","상승","발표","전망","우려","예상",
        "지구","블록","만기","현황","상황","내용","방식","수준","방향","기반",
        "영향","결과","원인","부분","문제","사업","진행","운영","관리","추진",
        "계획","목표","성장","확대","감소","변화","개선","강화","유지","완화",
        "가능","필요","중요","주요","대표","일부","전체","자체","이상","미만",
        "이하","정도","주간","월간","연간","분기","올해","내년","작년",
    }
    stopwords = basic_sw | economy_sw

    words = re.findall(r"[가-힣]{2,}", text)
    freq  = {}
    for w in words:
        if w not in stopwords and len(w) >= 2:
            # 2글자 단어는 빈도가 3 이상일 때만 키워드 후보
            freq[w] = freq.get(w, 0) + 1

    # 2글자는 빈도 3 이상, 3글자 이상은 빈도 1 이상
    filtered = {w: c for w, c in freq.items()
                if (len(w) >= 3) or (len(w) == 2 and c >= 3)}

    top = sorted(filtered, key=lambda x: -filtered[x])[:5]
    return ", ".join(top) if top else ", ".join(
        sorted(freq, key=lambda x: -freq[x])[:5]
    )

def imp_color(imp):
    s = int(imp) if str(imp).isdigit() else 3
    if s == 5: return {"red": 1.0,  "green": 0.95, "blue": 0.8}
    if s == 4: return {"red": 0.95, "green": 0.98, "blue": 1.0}
    return None


# ── 메인 ────────────────────────────────────────────────────
def run(config_path="config/categories.yaml"):
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    now            = datetime.now(KST)
    month_label    = now.strftime("%Y-%m")          # 탭 이름: 2026-04
    collected_at   = now.strftime("%Y-%m-%d %H:%M")

    # ── 환경변수 > yaml 순으로 우선 적용 ─────────────────────
    # GDRIVE_FOLDER_ID / GDRIVE_SPREADSHEET_ID 가 설정돼 있으면 그 값을 사용,
    # 없으면 categories.yaml 의 값을 fallback 으로 사용.
    folder_id = (
        os.environ.get("GDRIVE_FOLDER_ID", "").strip()
        or cfg.get("folder_id", "").strip()
    )
    spreadsheet_id = (
        os.environ.get("GDRIVE_SPREADSHEET_ID", "").strip()
        or cfg.get("spreadsheet_id", "").strip()
    )

    if not folder_id and not spreadsheet_id:
        raise ValueError(
            "Drive 저장 위치가 지정되지 않았습니다.\n"
            "  · 환경변수 GDRIVE_FOLDER_ID 를 설정하거나\n"
            "  · categories.yaml 의 folder_id 를 입력하세요."
        )

    print(f"[설정] folder_id={folder_id[:8]}... spreadsheet_id={spreadsheet_id[:8] or '(자동)'}")
    print(f"[설정] 수집 월: {month_label} · KST {collected_at}")

    gc, drive = get_clients()

    # ── Supabase 클라이언트 (선택) ─────────────────────────────
    # SUPABASE_URL + SUPABASE_SERVICE_KEY 가 있으면 자동으로 동시 쓰기.
    # 없으면 enabled=False 가 되어 시트만 기록 (이전 동작과 동일).
    sb = SupabaseClient()
    sb_retention_days = int(os.environ.get("SUPABASE_RETENTION_DAYS", "90"))
    if sb.enabled:
        sb.cleanup_old(days=sb_retention_days)
    sb_inserted_total = 0
    # collected_at 을 ISO8601(UTC) 로 변환 — Supabase timestamptz 용
    collected_at_iso = now.astimezone(pytz.UTC).isoformat()

    # ── 매월 1일 retention cleanup (6개월 보관 정책) ─────────
    # · NEWS_RETENTION_MONTHS 환경변수로 보관 개월 수 조정 가능 (기본 6)
    # · NEWS_RETENTION_FORCE=1 이면 1일이 아니어도 강제 실행 (수동 정리용)
    try:
        retention_months = int(os.environ.get("NEWS_RETENTION_MONTHS", "6"))
    except ValueError:
        retention_months = 6
    force_prune = os.environ.get("NEWS_RETENTION_FORCE", "").strip() in {"1", "true", "yes"}
    if (now.day == 1 or force_prune) and folder_id:
        why = "1일 정기 정리" if now.day == 1 else "수동 강제 정리"
        print(f"[정리] {why} 실행 (retention={retention_months}개월)")
        try:
            prune_old_month_tabs(gc, drive, now, folder_id, retention_months=retention_months)
        except Exception as e:
            print(f"[정리] 실행 실패 (수집은 계속): {type(e).__name__}: {e}")

    # ── 연단위 파일 가져오기 (없으면 자동 생성) ──────────────
    sp = get_or_create_annual_spreadsheet(gc, drive, now, folder_id, spreadsheet_id)

    # ── 월별 탭 가져오기 (없으면 자동 생성) ──────────────────
    ws = get_or_create_monthly_worksheet(sp, month_label)

    # ── 월탭의 기존 제목 세트 로드 (카테고리별 중복 방지) ─────
    title_sets_by_category = load_title_sets_by_category(ws)

    total    = 0
    fmt_reqs = []

    def _accumulate_sb_inserted(n: int):
        nonlocal sb_inserted_total
        sb_inserted_total += n

    def flush_batch(batch: list, cat_name: str):
        """기사 배치를 AI로 분석하고 시트에 일괄 기록"""
        nonlocal total
        if not batch:
            return

        ai_results = ai_process_batch(
            [{"title": a["title"], "raw": a.get("raw", "")} for a in batch],
            cat_name
        )
        time.sleep(AI_CALL_DELAY)

        rows = []
        for art, ai in zip(batch, ai_results):
            # HEADERS 순서 (v3) — 수집시간 첫 열, 뒤에 순위/조회수 추가
            rank_val  = art.get("rank", "")
            views_val = art.get("views", "")
            # 조회수가 숫자문자열이면 int 로 전달해 Sheets 에서 숫자 정렬 가능하게
            try:
                views_cell = int(views_val) if (isinstance(views_val, str) and views_val.isdigit()) else views_val
            except Exception:
                views_cell = views_val
            rows.append([
                collected_at,           # A 수집시간
                art["pub"],             # B 발행시간
                cat_name,               # C 카테고리
                art["title"],           # D 제목
                ai["summary"],          # E 요약
                ai["keywords"],         # F 주요키워드
                art["media"],           # G 언론사
                art.get("url", ""),     # H 출처
                ai["sentiment"],        # I 감성
                ai["importance"],       # J 중요도
                rank_val,               # K 순위 (랭킹 페이지만 숫자)
                views_cell,             # L 조회수 (랭킹 페이지에서 추출 성공 시)
            ])
            title_sets_by_category.setdefault(cat_name, set()).add(art["title"])
            total += 1
            rank_tag = f"#{rank_val}" if rank_val else ""
            view_tag = f"조회 {views_val}" if views_val else ""
            extra    = " ".join(x for x in [rank_tag, view_tag] if x)
            print(f"  ✓ [{ai['importance']}★/{ai['sentiment']}] {extra} {art['title'][:50]}")

        ws.append_rows(rows, value_input_option="USER_ENTERED")

        # ── Supabase 동시 쓰기 (실패 격리) ──────────────────────
        if sb.enabled:
            try:
                sb_rows = [
                    to_supabase_row(art, ai, cat_name, collected_at_iso)
                    for art, ai in zip(batch, ai_results)
                ]
                nonlocal_inserted = sb.insert_articles(sb_rows)
                # nonlocal 누적 (closure 변수)
                _accumulate_sb_inserted(nonlocal_inserted)
            except Exception as e:
                print(f"  [Supabase 쓰기 실패 — 시트만 기록] {e}", file=sys.stderr)

        for i, (_, ai) in enumerate(zip(batch, ai_results)):
            c = imp_color(ai["importance"])
            if c:
                row_idx = total - len(batch) + i + 2
                fmt_reqs.append({"repeatCell": {
                    "range": {"sheetId": ws.id,
                              "startRowIndex": row_idx - 1,
                              "endRowIndex":   row_idx,
                              "startColumnIndex": 0,
                              "endColumnIndex": len(HEADERS)},
                    "cell": {"userEnteredFormat": {"backgroundColor": c}},
                    "fields": "userEnteredFormat.backgroundColor",
                }})

    # ── 카테고리별 수집 ────────────────────────────────────────
    for cat in cfg["categories"]:
        cat_name       = cat["name"]
        sources        = cat.get("sources", [])
        keyword_filter = cat.get("keyword_filter", None)
        exclude_keyword_filter = cat.get("exclude_keyword_filter", None)
        limit          = cfg.get("limit_per_source", 8)

        print(f"\n▶ [{cat_name}] {len(sources)}개 소스")

        cat_title_set = set(title_sets_by_category.get(cat_name, set()))
        pending_batch = []      # AI 배치 대기열

        for src in sources:
            src_type  = src.get("type", "rss")
            src_url   = src.get("url", "")
            src_label = src.get("label", "")
            src_limit = src.get("limit", limit)

            try:
                if src_type == "naver_finance":
                    arts = fetch_naver_finance(src_url, src_label, src_limit)
                elif src_type == "naver_ranking":
                    # 랭킹 페이지 — limit 을 좀 여유있게 잡아 조회수 상위 기사를 많이 가져옴
                    arts = fetch_naver_ranking(
                        src_url, src_label, src_limit,
                        ranking_type=src.get("ranking_type", "view"),
                    )
                elif src_type == "rss":
                    src_keyword_filter = src.get("keyword_filter", keyword_filter)
                    src_exclude_keyword_filter = src.get("exclude_keyword_filter", exclude_keyword_filter)
                    arts = fetch_rss(
                        src_url, src_label, src_limit,
                        crawl_body=src.get("crawl_body", True),
                        keyword_filter=src_keyword_filter,
                        exclude_keyword_filter=src_exclude_keyword_filter,
                        scan_multiplier=src.get("scan_multiplier", 3),
                        keyword_match_fields=src.get(
                            "keyword_match_fields",
                            cat.get("keyword_match_fields", "title"),
                        ),
                    )
                elif src_type == "web":
                    arts = fetch_web(src_url, src.get("selectors", {}), src_label, src_limit)
                else:
                    print(f"  [알 수 없는 type] {src_type}")
                    arts = []
            except Exception as e:
                print(f"  [소스 오류] {src_url} → {e}")
                arts = []

            # 중복 필터링 후 배치에 추가
            for art in arts:
                t = art["title"].strip()
                if not t or t in cat_title_set:
                    continue
                cat_title_set.add(t)
                pending_batch.append(art)

                # 배치 크기 도달 시 즉시 처리
                if len(pending_batch) >= AI_BATCH_SIZE:
                    flush_batch(pending_batch, cat_name)
                    pending_batch = []

        # 카테고리 끝 - 남은 기사 처리
        if pending_batch:
            flush_batch(pending_batch, cat_name)

    # ── 서식 일괄 적용 ────────────────────────────────────────
    if fmt_reqs:
        try:
            sp.batch_update({"requests": fmt_reqs})
            print(f"\n[서식] {len(fmt_reqs)}개 행 색상 적용")
        except Exception as e:
            print(f"[서식 실패] {e}")

    # ── INDEX 탭 갱신 ─────────────────────────────────────────
    if total > 0:
        update_index_sheet(sp, month_label, total, collected_at)

    # v2 변경: TubeAI 가 Google Sheets 직독 방식으로 전환되어
    #   더 이상 docs/data/ 에 JSON 을 생성하지 않습니다.
    #   필요 시 이전 구조로 돌아가려면 git history 참조.

    print(f"\n✅ 완료: {total}건 → '{sp.title}' / {month_label} 탭")
    print(f"   URL: https://docs.google.com/spreadsheets/d/{sp.id}")
    if sb.enabled:
        print(f"   Supabase: {sb_inserted_total}건 insert (retention={sb_retention_days}일)")
    else:
        print(f"   Supabase: 비활성 (env 미설정)")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "config/categories.yaml")
