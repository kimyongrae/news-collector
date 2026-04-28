"""
Supabase REST 클라이언트 — supabase-py 의존성 없이 requests 만으로 동작.

용도:
  · collector.py 에서 기사 insert + 90일 retention cleanup
  · 시트 쓰기와 분리된 실패 격리 (Supabase 가 실패해도 시트 쓰기는 계속)

환경변수:
  SUPABASE_URL          https://xxx.supabase.co
  SUPABASE_SERVICE_KEY  service_role 키 (쓰기·삭제 권한)

스키마 (news_articles):
  id, collected_at, published_at, category, title, summary, keywords,
  media, url, sentiment, importance, rank, views, url_hash(생성열), created_at
  unique(url_hash, category) — 동일 URL+카테고리 조합 중복 방지
"""

import os
import sys
import json
import requests
from datetime import datetime, timezone


def _now():
    return datetime.now(timezone.utc).isoformat()


class SupabaseClient:
    """
    얇은 PostgREST 래퍼.

    실패 시 모든 메서드는 예외를 던지지 않고 False / 0 / None 등을 반환.
    호출부(collector.py)는 이 결과를 무시할 수 있어 시트 흐름을 막지 않음.
    """

    def __init__(self, url: str = "", service_key: str = "", timeout: int = 15):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = service_key or os.environ.get("SUPABASE_SERVICE_KEY", "")
        self.timeout = timeout
        self.enabled = bool(self.url and self.key)
        self.table = "news_articles"

        if not self.enabled:
            missing = []
            if not self.url: missing.append("SUPABASE_URL")
            if not self.key: missing.append("SUPABASE_SERVICE_KEY")
            print(f"[Supabase] 비활성 — env 누락: {', '.join(missing)}", file=sys.stderr)

    # ────────────────────────────────────────────────────────
    def _headers(self, prefer: str = "return=minimal") -> dict:
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": prefer,
        }

    def _endpoint(self, path: str) -> str:
        return f"{self.url}/rest/v1/{path.lstrip('/')}"

    # ────────────────────────────────────────────────────────
    def insert_articles(self, rows: list) -> int:
        """
        rows: [{"collected_at":..., "category":..., "title":..., ...}, ...]
        Returns: 실제 insert 된 개수 (중복은 0 으로 카운트).

        upsert (on_conflict=url_hash,category) 로 중복 무시.
        """
        if not self.enabled or not rows:
            return 0

        try:
            r = requests.post(
                self._endpoint(self.table) + "?on_conflict=url_hash,category",
                headers=self._headers(prefer="resolution=ignore-duplicates,return=representation"),
                data=json.dumps(rows, ensure_ascii=False).encode("utf-8"),
                timeout=self.timeout,
            )
            if not r.ok:
                print(f"[Supabase] insert 실패 HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
                return 0
            inserted = r.json() if r.text else []
            n = len(inserted) if isinstance(inserted, list) else 0
            skipped = len(rows) - n
            print(f"[Supabase] insert {n}건 (중복 스킵 {skipped}건)")
            return n
        except Exception as e:
            print(f"[Supabase] insert 예외: {type(e).__name__}: {e}", file=sys.stderr)
            return 0

    # ────────────────────────────────────────────────────────
    def cleanup_old(self, days: int = 90) -> int:
        """
        collected_at < now() - {days} days 인 행 삭제.
        Returns: 삭제 건수 (실패 시 0).
        """
        if not self.enabled:
            return 0
        try:
            from datetime import datetime, timedelta, timezone as tz
            from urllib.parse import quote
            # PostgREST 는 timestamptz 의 '+' 를 그대로 받아야 하는데
            # URL 에서 '+' 는 공백으로 해석되므로 quote() 로 %2B 인코딩 필수.
            cutoff = (datetime.now(tz.utc) - timedelta(days=days)).isoformat()
            cutoff_enc = quote(cutoff, safe="")
            r = requests.delete(
                self._endpoint(self.table) + f"?collected_at=lt.{cutoff_enc}",
                headers=self._headers(prefer="return=representation"),
                timeout=self.timeout,
            )
            if not r.ok:
                print(f"[Supabase] cleanup 실패 HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
                return 0
            deleted = r.json() if r.text else []
            n = len(deleted) if isinstance(deleted, list) else 0
            print(f"[Supabase] {days}일 이전 {n}건 삭제")
            return n
        except Exception as e:
            print(f"[Supabase] cleanup 예외: {type(e).__name__}: {e}", file=sys.stderr)
            return 0

    # ────────────────────────────────────────────────────────
    def health_check(self) -> bool:
        """간단한 연결 테스트 — 스키마 한 행 조회."""
        if not self.enabled:
            return False
        try:
            r = requests.get(
                self._endpoint(self.table) + "?select=id&limit=1",
                headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
                timeout=self.timeout,
            )
            return r.ok
        except Exception as e:
            print(f"[Supabase] health 예외: {e}", file=sys.stderr)
            return False


def to_supabase_row(art: dict, ai: dict, category: str, collected_at_iso: str) -> dict:
    """collector.py 의 art + ai 결과를 Supabase row dict 로 변환."""
    def _int_or_none(v):
        try:
            s = str(v).strip().replace(",", "")
            return int(s) if s and s.lstrip("-").isdigit() else None
        except Exception:
            return None

    imp_raw = ai.get("importance", "")
    imp = int(imp_raw) if str(imp_raw).isdigit() else None

    return {
        "collected_at": collected_at_iso,
        "published_at": art.get("pub", "") or None,
        "category":     category,
        "title":        art.get("title", "")[:500],
        "summary":      ai.get("summary", "") or None,
        "keywords":     ai.get("keywords", "") or None,
        "media":        art.get("media", "") or None,
        "url":          art.get("url", "") or None,
        "sentiment":    ai.get("sentiment", "") or None,
        "importance":   imp,
        "rank":         _int_or_none(art.get("rank", "")),
        "views":        _int_or_none(art.get("views", "")),
    }
