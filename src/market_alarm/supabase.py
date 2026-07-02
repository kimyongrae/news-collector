import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class SupabaseNewsClient:
    def __init__(self, url: str = "", key: str = "", timeout: int = 15):
        self.url = (url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.environ.get("SUPABASE_SERVICE_KEY", "")
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.key)

    def configured(self, secrets: dict[str, str] | None = None) -> bool:
        if secrets is None:
            return self.enabled
        return bool(
            (secrets.get("supabase_url") or self.url).strip()
            and (secrets.get("supabase_service_key") or self.key).strip()
        )

    def fetch_articles(self, settings: dict[str, Any], secrets: dict[str, str] | None = None) -> list[dict[str, Any]]:
        url = ((secrets or {}).get("supabase_url") or self.url).rstrip("/")
        key = (secrets or {}).get("supabase_service_key") or self.key
        if not url or not key:
            return []
        if not url.startswith(("https://", "http://")):
            return []
        if not key.startswith(("sb_", "eyJ")):
            return []

        since = datetime.now(timezone.utc) - timedelta(hours=int(settings["lookback_hours"]))
        query = {
            "select": "id,collected_at,published_at,category,title,summary,keywords,media,url,sentiment,importance,rank,views",
            "collected_at": f"gte.{since.isoformat()}",
            "importance": f"gte.{int(settings['min_importance'])}",
            "order": "importance.desc,views.desc,collected_at.desc",
            "limit": str(max(50, int(settings["max_items"]) * 5)),
        }

        categories = settings.get("categories") or []
        if categories:
            query["category"] = "in.(" + ",".join(_pg_escape(v) for v in categories) + ")"

        sentiments = settings.get("sentiments") or []
        if sentiments:
            query["sentiment"] = "in.(" + ",".join(_pg_escape(v) for v in sentiments) + ")"

        endpoint = f"{url}/rest/v1/news_articles?{urlencode(query, safe='(),.*\"')}"
        req = Request(
            endpoint,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        with urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def test_connection(self, secrets: dict[str, str] | None = None) -> dict[str, Any]:
        url = ((secrets or {}).get("supabase_url") or self.url).rstrip("/")
        key = (secrets or {}).get("supabase_service_key") or self.key
        if not url or not key:
            return {"ok": False, "detail": "Supabase URL 또는 Service Key가 비어 있습니다."}
        if not url.startswith(("https://", "http://")):
            return {"ok": False, "detail": "Supabase URL 형식이 올바르지 않습니다. https://...supabase.co 값을 입력하세요."}
        if not key.startswith(("sb_", "eyJ")):
            return {"ok": False, "detail": "Supabase Service Key 형식이 올바르지 않습니다."}

        endpoint = f"{url}/rest/v1/news_articles?select=id&limit=1"
        req = Request(
            endpoint,
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                rows = json.loads(resp.read().decode("utf-8"))
            return {
                "ok": True,
                "detail": f"news_articles 조회 성공 ({len(rows)}건 샘플)",
            }
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")[:300]
            return {"ok": False, "detail": f"HTTP {exc.code}: {body}"}
        except Exception as exc:
            return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _pg_escape(value: str) -> str:
    return '"' + str(value).replace('"', "") + '"'
