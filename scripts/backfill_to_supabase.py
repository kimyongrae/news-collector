"""
Google Sheets → Supabase 1회 백필 스크립트.

용도:
  Supabase 도입 시점에 시트에 이미 쌓인 최근 N일치 기사를 한 번에 옮긴다.
  중복은 url_hash 기반 unique 제약으로 자동 스킵 (upsert ignore-duplicates).

사용:
  cd news-collector
  python scripts/backfill_to_supabase.py [DAYS]
    DAYS 생략 시 90일치 백필.

env:
  GOOGLE_OAUTH_TOKEN   (필수)
  GDRIVE_FOLDER_ID     또는 GDRIVE_SPREADSHEET_ID  (필수)
  SUPABASE_URL         (필수)
  SUPABASE_SERVICE_KEY (필수)
"""

import os
import sys
from datetime import datetime, timedelta

import pytz

# src/ 를 import path 에 추가
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

try:
    from dotenv import load_dotenv
    env_path = os.path.join(os.path.dirname(HERE), ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
except ImportError:
    pass

import gspread
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build as google_build

from supabase_client import SupabaseClient

KST = pytz.timezone("Asia/Seoul")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_clients():
    import json
    raw = os.environ.get("GOOGLE_OAUTH_TOKEN", "").strip()
    if not raw:
        raise SystemExit("GOOGLE_OAUTH_TOKEN 미설정")
    d = json.loads(raw)
    creds = Credentials(
        token=None,
        refresh_token=d["refresh_token"],
        client_id=d["client_id"],
        client_secret=d["client_secret"],
        token_uri=d.get("token_uri", "https://oauth2.googleapis.com/token"),
        scopes=SCOPES,
    )
    return gspread.authorize(creds), google_build("drive", "v3", credentials=creds)


def parse_kst(ts: str):
    """'YYYY-MM-DD HH:MM' → aware datetime (KST). 실패 시 None."""
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts.strip()[:16], "%Y-%m-%d %H:%M")
        return KST.localize(dt)
    except Exception:
        try:
            dt = datetime.strptime(ts.strip()[:10], "%Y-%m-%d")
            return KST.localize(dt)
        except Exception:
            return None


def find_year_sheets(gc, drive, folder_id):
    q = (f"'{folder_id}' in parents and "
         f"mimeType='application/vnd.google-apps.spreadsheet' and "
         f"name contains 'RSS_뉴스_' and trashed=false")
    files = drive.files().list(q=q, fields="files(id,name)").execute().get("files", [])
    return files


def main(days: int = 90):
    folder_id = (os.environ.get("GDRIVE_FOLDER_ID") or "").strip()
    sheet_id  = (os.environ.get("GDRIVE_SPREADSHEET_ID") or "").strip()
    if not folder_id and not sheet_id:
        raise SystemExit("GDRIVE_FOLDER_ID 또는 GDRIVE_SPREADSHEET_ID 필요")

    sb = SupabaseClient()
    if not sb.enabled:
        raise SystemExit("SUPABASE_URL + SUPABASE_SERVICE_KEY 미설정")
    if not sb.health_check():
        raise SystemExit("Supabase health check 실패 — 스키마 적용 여부 확인")

    cutoff = datetime.now(KST) - timedelta(days=days)
    print(f"[backfill] cutoff = {cutoff.strftime('%Y-%m-%d %H:%M')} KST · {days}일치")

    gc, drive = get_clients()

    sheets = []
    if sheet_id:
        sheets = [{"id": sheet_id, "name": "(직접 지정)"}]
    else:
        sheets = find_year_sheets(gc, drive, folder_id)

    if not sheets:
        print("[backfill] 대상 시트 없음")
        return

    inserted_total = 0
    scanned_total  = 0
    for f in sheets:
        sp = gc.open_by_key(f["id"])
        print(f"\n▶ {sp.title}")
        for ws in sp.worksheets():
            t = ws.title
            if t == "INDEX": continue
            if not (len(t) == 7 and t[4] == "-"):
                continue
            # cutoff 보다 오래된 월 탭은 스킵 (전체가 cutoff 이전)
            try:
                y, m = int(t[:4]), int(t[5:])
            except ValueError:
                continue
            from calendar import monthrange
            last_day = monthrange(y, m)[1]
            month_end = KST.localize(datetime(y, m, last_day, 23, 59))
            if month_end < cutoff:
                continue

            print(f"  · 탭 {t} 로딩 중...")
            values = ws.get_all_values()
            if len(values) < 2:
                continue
            headers = values[0]
            rows = values[1:]
            scanned_total += len(rows)

            batch = []
            for row in rows:
                while len(row) < 12:
                    row.append("")
                collected_at_kst = parse_kst(row[0])
                if collected_at_kst is None or collected_at_kst < cutoff:
                    continue
                imp_raw = row[9]
                try:
                    imp = int(imp_raw) if str(imp_raw).strip().isdigit() else None
                except Exception:
                    imp = None
                def _int(v):
                    s = str(v).strip().replace(",", "")
                    return int(s) if s.lstrip("-").isdigit() else None
                batch.append({
                    "collected_at": collected_at_kst.astimezone(pytz.UTC).isoformat(),
                    "published_at": row[1] or None,
                    "category":     row[2],
                    "title":        row[3][:500],
                    "summary":      row[4] or None,
                    "keywords":     row[5] or None,
                    "media":        row[6] or None,
                    "url":          row[7] or None,
                    "sentiment":    row[8] or None,
                    "importance":   imp,
                    "rank":         _int(row[10]),
                    "views":        _int(row[11]),
                })
                # 1000건씩 끊어서 insert (URL 길이·요청 크기 제한 회피)
                if len(batch) >= 1000:
                    inserted_total += sb.insert_articles(batch)
                    batch = []
            if batch:
                inserted_total += sb.insert_articles(batch)

    print(f"\n✅ 백필 완료 · 시트 스캔 {scanned_total}행 · Supabase insert {inserted_total}건")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    main(days)
