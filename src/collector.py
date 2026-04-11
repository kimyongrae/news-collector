"""
뉴스 수집기 - OAuth 방식 (내 구글 계정으로 드라이브에 직접 저장)
"""

import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import re
import yaml
import json
import os
import sys
import time

import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as google_build
import google.generativeai as genai

KST = pytz.timezone("Asia/Seoul")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ── OAuth 인증 클라이언트 생성 ──────────────────────────────
def get_clients():
    """
    환경변수 GOOGLE_OAUTH_TOKEN (JSON 문자열) 로 인증
    {
      "client_id": "87249978372-aufoddb78fahqnubtv0k46uk0asnkoha.apps.googleusercontent.com",
      "client_secret": "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",
      "refresh_token": "1//0ez1rPqFlWhUaCgYIARAAGA4SNwF-L9IrWhggBeh4nrBBM4PsyavPc8UiwqQkQ-LFQu3bYsQaSLJyklRtwCa-HZIm5xEw2-xQ7iM",
      "token_uri": "https://oauth2.googleapis.com/token"
    }
    """
    token_json = os.environ.get("GOOGLE_OAUTH_TOKEN", "")
    if not token_json:
        raise ValueError(
            "환경변수 GOOGLE_OAUTH_TOKEN 이 없습니다.\n"
            "get_token.py 를 실행해서 GitHub Secret에 등록해주세요."
        )

    token_data = json.loads(token_json)
    creds = Credentials(
        token=None,
        refresh_token=token_data["refresh_token"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        token_uri=token_data.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=SCOPES,
    )

    sheets_client = gspread.authorize(creds)
    drive_client  = google_build("drive", "v3", credentials=creds)
    return sheets_client, drive_client

# ── 폴더 안에서 파일명으로 검색 ────────────────────────────
def find_file_in_folder(drive, folder_id, title):
    query = (
        f"name='{title}' "
        f"and '{folder_id}' in parents "
        f"and mimeType='application/vnd.google-apps.spreadsheet' "
        f"and trashed=false"
    )
    result = drive.files().list(q=query, fields="files(id, name)").execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None

# ── 폴더 안에 스프레드시트 생성 ────────────────────────────
def create_file_in_folder(drive, folder_id, title):
    file_meta = {
        "name": title,
        "mimeType": "application/vnd.google-apps.spreadsheet",
        "parents": [folder_id],
    }
    created = drive.files().create(body=file_meta, fields="id").execute()
    file_id = created["id"]
    print(f"[신규 파일 생성] {title}  (id: {file_id})")
    return file_id

# ── 월별 스프레드시트 가져오기 / 생성 ──────────────────────
def get_or_create_spreadsheet(sheets_client, drive_client, now, folder_id, spreadsheet_id):
    title = now.strftime("%Y%m월_trand")

    if spreadsheet_id:
        try:
            sp = sheets_client.open_by_key(spreadsheet_id)
            print(f"[기존 파일 사용] {sp.title}")
            return sp
        except Exception as e:
            print(f"[spreadsheet_id 열기 실패] {e}")

    if not folder_id:
        raise ValueError("categories.yaml 에 folder_id 를 입력해주세요.")

    existing_id = find_file_in_folder(drive_client, folder_id, title)
    if existing_id:
        print(f"[기존 파일 재사용] {title}")
        return sheets_client.open_by_key(existing_id)

    new_id = create_file_in_folder(drive_client, folder_id, title)
    return sheets_client.open_by_key(new_id)

# ── 일별 워크시트 가져오기 / 생성 ──────────────────────────
def get_or_create_worksheet(spreadsheet, date_str):
    try:
        ws = spreadsheet.worksheet(date_str)
        print(f"[기존 탭 사용] {date_str}")
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=date_str, rows=500, cols=10)
        headers = ["카테고리", "제목", "출처", "수집시간", "주요 키워드", "요약", "원문 URL"]
        ws.append_row(headers, value_input_option="RAW")
        ws.format("A1:G1", {
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.6},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
        ws.freeze(rows=1)
        print(f"[새 탭 생성] {date_str}")
    return ws

# ── RSS 기사 수집 ───────────────────────────────────────────
def fetch_rss_articles(feed_url, limit=10):
    feed = feedparser.parse(feed_url)
    articles = []
    for entry in feed.entries[:limit]:
        articles.append({
            "title": entry.get("title", "").strip(),
            "url":   entry.get("link", ""),
            "source": feed.feed.get("title", feed_url),
            "summary_raw": BeautifulSoup(
                entry.get("summary", entry.get("description", "")), "html.parser"
            ).get_text()[:800],
        })
    return articles

# ── 웹페이지 직접 수집 ──────────────────────────────────────
def fetch_web_articles(url, selectors):
    headers = {"User-Agent": "Mozilla/5.0 (compatible; NewsBot/1.0)"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"  [웹 수집 실패] {url} → {e}")
        return []
    soup = BeautifulSoup(resp.text, "html.parser")
    articles = []
    for item in soup.select(selectors.get("items", "article"))[:10]:
        title_el = item.select_one(selectors.get("title", "h2"))
        link_el  = item.select_one(selectors.get("link",  "a"))
        desc_el  = item.select_one(selectors.get("summary", "p"))
        title   = title_el.get_text(strip=True) if title_el else ""
        link    = link_el["href"] if link_el and link_el.has_attr("href") else url
        if link.startswith("/"):
            from urllib.parse import urlparse
            base = urlparse(url)
            link = f"{base.scheme}://{base.netloc}{link}"
        summary = desc_el.get_text(strip=True)[:400] if desc_el else ""
        if title:
            articles.append({"title": title, "url": link, "source": url, "summary_raw": summary})
    return articles

# ── Gemini AI 키워드 + 요약 ─────────────────────────────────
def ai_process(title, raw_summary, category):
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return extract_keywords_simple(title + " " + raw_summary), raw_summary[:150]
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = f"""다음 {category} 뉴스 기사를 분석해 JSON으로만 답하세요.
제목: {title}
본문 요약: {raw_summary}

{{
  "keywords": "쉼표로 구분된 핵심 키워드 5개",
  "summary": "2~3문장 핵심 요약 (한국어)"
}}"""
    try:
        resp = model.generate_content(prompt)
        text = resp.text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        data = json.loads(text)
        return data.get("keywords", ""), data.get("summary", raw_summary[:150])
    except Exception as e:
        print(f"  [AI 처리 실패] {e}")
        return extract_keywords_simple(title), raw_summary[:150]

def extract_keywords_simple(text):
    stopwords = {"의","을","를","이","가","은","는","에","와","과","도","로","으로","에서","한","하는","있는"}
    words = re.findall(r"[가-힣]{2,}", text)
    freq = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    return ", ".join(sorted(freq, key=lambda x: -freq[x])[:5])

def is_duplicate(ws, title):
    return title in ws.col_values(2)

# ── 메인 ────────────────────────────────────────────────────
def run(config_path="config/categories.yaml"):
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    now            = datetime.now(KST)
    date_label     = now.strftime("%m월 %d일")
    collected_at   = now.strftime("%Y-%m-%d %H:%M")
    folder_id      = cfg.get("folder_id", "").strip()
    spreadsheet_id = cfg.get("spreadsheet_id", "").strip()

    sheets_client, drive_client = get_clients()
    spreadsheet = get_or_create_spreadsheet(
        sheets_client, drive_client, now, folder_id, spreadsheet_id
    )
    ws = get_or_create_worksheet(spreadsheet, date_label)

    total = 0
    for cat in cfg["categories"]:
        cat_name = cat["name"]
        sources  = cat.get("sources", [])
        print(f"\n▶ [{cat_name}] 수집 시작 ({len(sources)}개 소스)")

        for src in sources:
            src_type = src.get("type", "rss")
            try:
                if src_type == "rss":
                    articles = fetch_rss_articles(src["url"], limit=cfg.get("limit_per_source", 8))
                elif src_type == "web":
                    articles = fetch_web_articles(src["url"], src.get("selectors", {}))
                else:
                    articles = []
            except Exception as e:
                print(f"  [소스 오류] {src['url']} → {e}")
                continue

            for art in articles:
                if not art["title"] or is_duplicate(ws, art["title"]):
                    continue
                keywords, summary = ai_process(art["title"], art["summary_raw"], cat_name)
                ws.append_row([
                    cat_name, art["title"],
                    art.get("source", src["url"]),
                    collected_at, keywords, summary, art["url"],
                ], value_input_option="USER_ENTERED")
                total += 1
                print(f"  ✓ {art['title'][:50]}")
                time.sleep(0.5)

    print(f"\n✅ 수집 완료: {total}건 → '{spreadsheet.title}' / {date_label} 탭")
    print(f"   Spreadsheet ID: {spreadsheet.id}")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/categories.yaml"
    run(config_path)