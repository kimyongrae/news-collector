"""
뉴스 수집기 - 카테고리별 RSS/웹 크롤링 후 Google Sheets에 저장
"""

import feedparser
import requests
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import re
import yaml
import os
import sys
import time

import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

KST = pytz.timezone("Asia/Seoul")

# ── 설정 로드 ──────────────────────────────────────────────
def load_config(path: str = "config/categories.yaml") -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)

# ── Google Sheets 클라이언트 ────────────────────────────────
def get_sheets_client():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
    return gspread.authorize(creds)

# ── 월별 스프레드시트 가져오기 / 생성 ──────────────────────
def get_or_create_spreadsheet(client, now: datetime, spreadsheet_id: str | None) -> gspread.Spreadsheet:
    title = now.strftime("%Y년 %m월 뉴스 리포트")
    if spreadsheet_id:
        try:
            return client.open_by_key(spreadsheet_id)
        except Exception:
            pass
    # 이름으로 검색
    try:
        return client.open(title)
    except gspread.SpreadsheetNotFound:
        sp = client.create(title)
        sp.share(None, perm_type="anyone", role="reader")  # 읽기 공개(선택)
        print(f"[신규 시트 생성] {title}")
        return sp

# ── 일별 워크시트 가져오기 / 생성 ──────────────────────────
def get_or_create_worksheet(spreadsheet: gspread.Spreadsheet, date_str: str) -> gspread.Worksheet:
    """date_str 예: '04월 11일'"""
    try:
        ws = spreadsheet.worksheet(date_str)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=date_str, rows=500, cols=10)
        # 헤더 작성
        headers = ["카테고리", "제목", "출처", "수집시간", "주요 키워드", "요약", "원문 URL"]
        ws.append_row(headers, value_input_option="RAW")
        # 헤더 스타일 (bold + 배경색)
        ws.format("A1:G1", {
            "backgroundColor": {"red": 0.2, "green": 0.2, "blue": 0.6},
            "textFormat": {"bold": True, "foregroundColor": {"red": 1, "green": 1, "blue": 1}},
        })
        ws.freeze(rows=1)
        print(f"[새 시트] {date_str}")
    return ws

# ── RSS 기사 수집 ───────────────────────────────────────────
def fetch_rss_articles(feed_url: str, limit: int = 10) -> list[dict]:
    feed = feedparser.parse(feed_url)
    articles = []
    for entry in feed.entries[:limit]:
        articles.append({
            "title": entry.get("title", "").strip(),
            "url": entry.get("link", ""),
            "source": feed.feed.get("title", feed_url),
            "summary_raw": BeautifulSoup(
                entry.get("summary", entry.get("description", "")), "html.parser"
            ).get_text()[:800],
        })
    return articles

# ── 웹페이지 직접 수집 (URL 리스트 방식) ───────────────────
def fetch_web_articles(url: str, selectors: dict) -> list[dict]:
    """
    selectors 예:
      items: "article.news-item"
      title: "h2.title"
      link:  "a"          (href 속성)
      summary: "p.desc"
    """
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

# ── Gemini 키워드 + 요약 생성 ───────────────────────────────
def ai_process(title: str, raw_summary: str, category: str) -> tuple[str, str]:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        keywords = extract_keywords_simple(title + " " + raw_summary)
        summary  = raw_summary[:150] + ("…" if len(raw_summary) > 150 else "")
        return keywords, summary

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
        import json
        data = json.loads(text)
        return data.get("keywords", ""), data.get("summary", raw_summary[:150])
    except Exception as e:
        print(f"  [AI 처리 실패] {e}")
        return extract_keywords_simple(title), raw_summary[:150]

def extract_keywords_simple(text: str) -> str:
    # 간단한 규칙 기반 키워드 (AI 없을 때 fallback)
    stopwords = {"의","을","를","이","가","은","는","에","와","과","도","로","으로","에서","한","하는","있는"}
    words = re.findall(r"[가-힣]{2,}", text)
    freq: dict[str, int] = {}
    for w in words:
        if w not in stopwords:
            freq[w] = freq.get(w, 0) + 1
    top = sorted(freq, key=lambda x: -freq[x])[:5]
    return ", ".join(top)

# ── 중복 체크 ───────────────────────────────────────────────
def is_duplicate(ws: gspread.Worksheet, title: str) -> bool:
    existing = ws.col_values(2)  # 제목 열
    return title in existing

# ── 메인 수집 루프 ──────────────────────────────────────────
def run(config_path: str = "config/categories.yaml"):
    cfg = load_config(config_path)
    now = datetime.now(KST)
    date_label = now.strftime("%m월 %d일")
    collected_at = now.strftime("%Y-%m-%d %H:%M")

    client      = get_sheets_client()
    spreadsheet = get_or_create_spreadsheet(
        client, now, cfg.get("spreadsheet_id")
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
                if not art["title"]:
                    continue
                if is_duplicate(ws, art["title"]):
                    continue

                keywords, summary = ai_process(art["title"], art["summary_raw"], cat_name)
                row = [
                    cat_name,
                    art["title"],
                    art.get("source", src["url"]),
                    collected_at,
                    keywords,
                    summary,
                    art["url"],
                ]
                ws.append_row(row, value_input_option="USER_ENTERED")
                total += 1
                print(f"  ✓ {art['title'][:50]}")
                time.sleep(0.5)  # API rate limit 방지

    print(f"\n✅ 수집 완료: {total}건 → '{spreadsheet.title}' / {date_label} 시트")
    # spreadsheet ID를 출력해두면 GitHub Actions 로그에서 확인 가능
    print(f"   Spreadsheet ID: {spreadsheet.id}")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config/categories.yaml"
    run(config_path)
