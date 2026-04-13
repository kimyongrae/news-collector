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

KST    = pytz.timezone("Asia/Seoul")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS    = ["카테고리","제목","요약","주요키워드","언론사","출처","감성","중요도","발행시간","수집시간"]
COL_WIDTHS = [90, 260, 340, 200, 90, 80, 55, 55, 120, 120]
HDR_LAST   = chr(ord('A') + len(HEADERS) - 1)
HDR_RANGE  = f"A1:{HDR_LAST}1"
TITLE_COL  = 2  # B열

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

def is_body_error(text: str) -> bool:
    """본문이 에러 페이지 내용인지 확인"""
    if not text:
        return False
    for pat in BODY_ERROR_PATTERNS:
        if pat in text:
            return True
    return False


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

def load_title_set(ws):
    """월탭의 제목(B열=col 2) 전체를 메모리에 로드해 중복 방지용 세트 반환"""
    vals = ws.col_values(TITLE_COL)
    return set(v.strip() for v in vals[1:] if v.strip())


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
        })
        time.sleep(0.3)

        if len(arts) >= limit:
            break

    print(f"    → {len(arts)}건 수집 완료")
    return arts


# ── RSS 수집 ─────────────────────────────────────────────────
def fetch_rss(url, label="", limit=10, crawl_body=True, keyword_filter=None):
    """
    RSS 파싱. summary 짧으면 기사 직접 크롤링.
    keyword_filter: 제목에 이 키워드 중 하나 이상 포함된 기사만 수집 (금리/환율용)
    """
    try:
        feed  = feedparser.parse(url)
        media = label or feed.feed.get("title", url)
        arts  = []

        for e in feed.entries[:limit * 3]:  # 필터링 감안해 여유있게 가져옴
            title = e.get("title", "").strip()
            if not title:
                continue

            # ✅ 키워드 필터 (금리/환율 카테고리 전용)
            if keyword_filter:
                if not any(kw in title for kw in keyword_filter):
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

            link = e.get("link", "")

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
            arts.append({"title": t, "url": l, "media": label or url, "pub": "", "raw": s})
    return arts


# ── Gemini AI ────────────────────────────────────────────────
# 배치 크기: 한 번의 API 호출에 묶을 기사 수
AI_BATCH_SIZE = 5

# API 호출 간 딜레이 (초) - rate limit 방지
AI_CALL_DELAY = 1.5

_gemini_model = None

def _get_model():
    global _gemini_model
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
    """
    model = _get_model()

    # API 키 없으면 전체 fallback
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
        print(f"  [AI 배치 실패] {ex} → 개별 재시도...")

    # 배치 실패 → 개별 재시도
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

    # ── 연단위 파일 가져오기 (없으면 자동 생성) ──────────────
    sp = get_or_create_annual_spreadsheet(gc, drive, now, folder_id, spreadsheet_id)

    # ── 월별 탭 가져오기 (없으면 자동 생성) ──────────────────
    ws = get_or_create_monthly_worksheet(sp, month_label)

    # ── 월탭의 기존 제목 세트 로드 (중복 방지) ───────────────
    global_title_set = load_title_set(ws)

    total    = 0
    fmt_reqs = []

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
            rows.append([
                cat_name,
                art["title"],
                ai["summary"],
                ai["keywords"],
                art["media"],
                art.get("url", ""),
                ai["sentiment"],
                ai["importance"],
                art["pub"],
                collected_at,
            ])
            global_title_set.add(art["title"])
            total += 1
            print(f"  ✓ [{ai['importance']}★/{ai['sentiment']}] {art['title'][:50]}")

        ws.append_rows(rows, value_input_option="USER_ENTERED")

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
        limit          = cfg.get("limit_per_source", 8)

        print(f"\n▶ [{cat_name}] {len(sources)}개 소스")

        cat_title_set = set()   # 카테고리 내 중복 방지
        pending_batch = []      # AI 배치 대기열

        for src in sources:
            src_type  = src.get("type", "rss")
            src_url   = src.get("url", "")
            src_label = src.get("label", "")
            src_limit = src.get("limit", limit)

            try:
                if src_type == "naver_finance":
                    arts = fetch_naver_finance(src_url, src_label, src_limit)
                elif src_type == "rss":
                    arts = fetch_rss(
                        src_url, src_label, src_limit,
                        crawl_body=src.get("crawl_body", True),
                        keyword_filter=keyword_filter,
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
                if not t or t in global_title_set or t in cat_title_set:
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

    # ── JSON 파일 저장 (GitHub Pages용) ──────────────────────
    save_json(ws, sp, month_label, collected_at, now)

    print(f"\n✅ 완료: {total}건 → '{sp.title}' / {month_label} 탭")
    print(f"   URL: https://docs.google.com/spreadsheets/d/{sp.id}")


# ── JSON 저장 ────────────────────────────────────────────────

# TubeAI 프런트엔드가 사용하는 정규화 코드 매핑
CATEGORY_CODE = {
    "경제":      "economy",
    "부동산":    "real-estate",
    "주식":      "stock",
    "금리/환율": "rate",
    "금리·환율": "rate",
    "네이버금융": "naver-fin",
}
SENTIMENT_CODE = {
    "긍정": "positive",
    "부정": "negative",
    "중립": "neutral",
}


def _normalize_article(row_art: dict) -> dict:
    """시트 원본 행 → TubeAI 프런트엔드용 정규화 스키마."""
    # keywords: "a, b, c" → ["a", "b", "c"]
    raw_kw = row_art.get("keywords", "")
    if isinstance(raw_kw, list):
        keywords = [str(k).strip() for k in raw_kw if str(k).strip()]
    else:
        keywords = [k.strip() for k in str(raw_kw).split(",") if k.strip()]

    # importance: "3" → 3 (1~5 범위)
    try:
        importance = int(row_art.get("importance") or 0)
    except (ValueError, TypeError):
        importance = 0
    if importance < 1 or importance > 5:
        importance = 3

    # 카테고리·감성 코드화
    cat_label = row_art.get("category", "") or ""
    cat_code  = CATEGORY_CODE.get(cat_label, "economy")
    sent_label = row_art.get("sentiment", "중립") or "중립"
    sent_code  = SENTIMENT_CODE.get(sent_label, "neutral")

    # 언론사: "한국경제_경제" → "한국경제"
    media_raw = row_art.get("media", "") or ""
    source = media_raw.split("_", 1)[0] if "_" in media_raw else media_raw

    return {
        "category":       cat_code,
        "category_label": cat_label,
        "title":          row_art.get("title", ""),
        "summary":        row_art.get("summary", ""),
        "keywords":       keywords,
        "source":         source or "출처 미상",
        "source_detail":  media_raw,
        "url":            row_art.get("url", ""),
        "sentiment":      sent_code,
        "sentiment_label": sent_label,
        "importance":     importance,
        "published_at":   row_art.get("published", ""),
        "collected_at":   row_art.get("collected", ""),
    }


def _article_day(art: dict, month_label: str) -> str:
    """기사 1건의 소속 일자를 결정. published 우선, 없으면 collected."""
    for key in ("published_at", "collected_at"):
        v = art.get(key, "") or ""
        if v and len(v) >= 10 and v[:10].startswith(month_label):
            return v[:10]
    # 그래도 없으면 월 1일로 배치
    return f"{month_label}-01"


def save_json(ws, sp, month_label: str, collected_at: str, now: datetime):
    """
    수집된 데이터를 아래 두 구조로 동시 저장한다.

      ① (구) docs/data/{YYYY-MM}.json           ← 월별 원본 (backwards compat)
      ② (신) docs/data/{YYYY}/{MM}/
             ├── index.json                     ← 월 요약 (일별 수, 카테고리별 수)
             └── {YYYY-MM-DD}.json              ← 일별 상세 (TubeAI 가 실제로 읽는 파일)

      ③ docs/data/index.json                    ← 전체 메타 (월 목록)
         구/신 키를 모두 담아 하나로 운영 — 구 소비자와 신 소비자 모두 호환
    """
    import pathlib

    data_dir = pathlib.Path("docs/data")
    data_dir.mkdir(parents=True, exist_ok=True)

    # 시트 읽기
    all_rows = ws.get_all_values()
    if len(all_rows) < 2:
        print("[JSON] 데이터 없음 - 저장 생략")
        return

    # 원본 스키마 (구 월별 파일용)
    raw_articles = []
    for row in all_rows[1:]:
        if not any(row):
            continue
        while len(row) < 10:
            row.append("")
        raw_articles.append({
            "category":  row[0],
            "title":     row[1],
            "summary":   row[2],
            "keywords":  row[3],
            "media":     row[4],
            "url":       row[5],
            "sentiment": row[6],
            "importance": row[7],
            "published": row[8],
            "collected": row[9],
        })

    # ── ① 구 월별 JSON ─────────────────────────────────────────
    month_file = data_dir / f"{month_label}.json"
    month_data = {
        "month":        month_label,
        "spreadsheet":  sp.id,
        "last_updated": collected_at,
        "total":        len(raw_articles),
        "articles":     raw_articles,
    }
    with open(month_file, "w", encoding="utf-8") as f:
        json.dump(month_data, f, ensure_ascii=False, indent=2)
    print(f"[JSON] {month_file} 저장 완료 ({len(raw_articles)}건) [구버전 호환]")

    # ── ② 신 일별 JSON ─────────────────────────────────────────
    year, mm = month_label.split("-")
    month_dir = data_dir / year / mm
    month_dir.mkdir(parents=True, exist_ok=True)

    # 정규화 + 일자별 그룹
    normalized = [_normalize_article(a) for a in raw_articles]
    day_buckets: "dict[str, list[dict]]" = {}
    for art in normalized:
        day = _article_day(art, month_label)
        day_buckets.setdefault(day, []).append(art)

    # 월에 해당하지 않는 기존 일별 파일은 남겨두고, 이번 수집에 포함된 날짜만 갱신
    day_summaries = []
    for day, items in day_buckets.items():
        items_sorted = sorted(
            items,
            key=lambda x: (-x["importance"], x.get("published_at", "")),
        )
        day_doc = {
            "date":         day,
            "count":        len(items_sorted),
            "collected_at": collected_at,
            "articles":     items_sorted,
        }
        day_file = month_dir / f"{day}.json"
        with open(day_file, "w", encoding="utf-8") as f:
            json.dump(day_doc, f, ensure_ascii=False, indent=2)
        print(f"[JSON] {day_file} ({len(items_sorted)}건)")

        # 월 요약용 day 엔트리
        max_imp = max((it["importance"] for it in items_sorted), default=0)
        cats_in_day: "dict[str, int]" = {}
        for it in items_sorted:
            cats_in_day[it["category_label"]] = cats_in_day.get(it["category_label"], 0) + 1
        day_summaries.append({
            "date":           day,
            "count":          len(items_sorted),
            "max_importance": max_imp,
            "categories":     cats_in_day,
        })

    # 월 인덱스: 기존 파일이 있으면 다른 날짜 엔트리는 보존
    month_index_file = month_dir / "index.json"
    existing_days = []
    if month_index_file.exists():
        try:
            with open(month_index_file, encoding="utf-8") as f:
                existing = json.load(f)
                existing_days = existing.get("days", []) or []
        except Exception as err:
            print(f"[JSON] 기존 월 인덱스 로드 실패 ({err}) — 새로 생성")

    touched_dates = {d["date"] for d in day_summaries}
    merged_days = [d for d in existing_days if d.get("date") not in touched_dates] + day_summaries
    merged_days.sort(key=lambda x: x.get("date", ""), reverse=True)

    cat_counts_total: "dict[str, int]" = {}
    for art in normalized:
        cat_counts_total[art["category_label"]] = cat_counts_total.get(art["category_label"], 0) + 1

    month_index = {
        "month":       month_label,
        "updated_at":  collected_at,
        "total":       sum(d["count"] for d in merged_days),
        "days":        merged_days,
        "by_category": cat_counts_total,
    }
    with open(month_index_file, "w", encoding="utf-8") as f:
        json.dump(month_index, f, ensure_ascii=False, indent=2)
    print(f"[JSON] {month_index_file} ({len(merged_days)}일치)")

    # ── ③ 루트 index.json (구/신 키 병기) ──────────────────────
    index_file = data_dir / "index.json"
    if index_file.exists():
        with open(index_file, encoding="utf-8") as f:
            index_data = json.load(f)
    else:
        index_data = {"months": []}

    month_entry = {
        # 신 키 (TubeAI 프런트엔드)
        "month":        month_label,
        "count":        month_index["total"],
        "updated_at":   collected_at,
        # 구 키 (backwards compat)
        "total":        month_index["total"],
        "last_updated": collected_at,
        "categories":   cat_counts_total,
    }

    months = index_data.get("months", [])
    for i, m in enumerate(months):
        if m.get("month") == month_label:
            months[i] = month_entry
            break
    else:
        months.append(month_entry)

    months.sort(key=lambda x: x.get("month", ""))
    index_data["months"]         = months
    index_data["spreadsheet"]    = sp.id
    index_data["year"]           = now.year
    index_data["updated_at"]     = collected_at
    index_data["total_articles"] = sum(m.get("count", m.get("total", 0)) for m in months)

    with open(index_file, "w", encoding="utf-8") as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)
    print(f"[JSON] {index_file} 갱신 완료 ({len(months)}개 월)")


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "config/categories.yaml")