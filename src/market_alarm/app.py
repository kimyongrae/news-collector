import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .digest import build_digest
from .notifiers import build_kakao_authorize_url, exchange_kakao_code, get_notifier, refresh_kakao_access_token, test_kakao_token
from .storage import SECRET_KEYS, Store, is_valid_secret_value
from .supabase import SupabaseNewsClient

KST = ZoneInfo("Asia/Seoul")
ENV_SECRET_KEYS = {
    "SUPABASE_URL": "supabase_url",
    "SUPABASE_SERVICE_KEY": "supabase_service_key",
    "KAKAO_ACCESS_TOKEN": "kakao_access_token",
    "KAKAO_REFRESH_TOKEN": "kakao_refresh_token",
    "KAKAO_REST_API_KEY": "kakao_rest_api_key",
    "KAKAO_CLIENT_SECRET": "kakao_client_secret",
}


class MarketAlarmApp:
    def __init__(self, store: Store, project_root: Path):
        self.store = store
        self.project_root = project_root
        self.supabase = SupabaseNewsClient()
        self.last_scheduler_minute = ""
        self.last_collect_minute = ""

    def settings(self) -> dict[str, Any]:
        settings = self.store.get_settings()
        secrets = self.store.get_secrets()
        secret_status = self.store.get_secret_status()
        settings["supabase_configured"] = self.supabase.configured(secrets)
        settings["kakao_configured"] = secret_status.get("kakao_access_token", False)
        settings["secret_status"] = secret_status
        settings["secret_info"] = self.store.get_secret_info()
        settings["secret_values"] = {
            key: secrets.get(key, "") if is_valid_secret_value(key, secrets.get(key, "")) else ""
            for key in SECRET_KEYS
        }
        settings["secret_storage_path"] = str(self.store.path)
        return settings

    def save_settings(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.store.save_settings(payload)
        return self.settings()

    def save_secrets(self, payload: dict[str, Any]) -> dict[str, Any]:
        status = self.store.save_secrets(payload)
        settings = self.settings()
        settings["secret_status"] = status
        return settings

    def test_connections(self) -> dict[str, Any]:
        secrets = self.store.get_secrets()
        return {
            "supabase": self.supabase.test_connection(secrets),
            "kakao": test_kakao_token(secrets.get("kakao_access_token", "")),
        }

    def test_secret(self, payload: dict[str, Any]) -> dict[str, Any]:
        key = str(payload.get("key", "")).strip()
        value = str(payload.get("value", "")).strip()
        secrets = self.store.get_secrets()
        for secret_key, secret_value in (payload.get("secrets") or {}).items():
            secret_key = str(secret_key).strip()
            secret_value = str(secret_value).strip()
            if secret_key in SECRET_KEYS and is_valid_secret_value(secret_key, secret_value):
                secrets[secret_key] = secret_value
        if value and is_valid_secret_value(key, value):
            secrets[key] = value

        if key in {"supabase_url", "supabase_service_key"}:
            result = self.supabase.test_connection(secrets)
            label = "Supabase URL" if key == "supabase_url" else "Supabase Service Key"
            return {"key": key, "label": label, **result}
        if key == "kakao_access_token":
            return {
                "key": key,
                "label": "Kakao Access Token",
                **test_kakao_token(secrets.get("kakao_access_token", "")),
            }
        return {"key": key, "label": key or "unknown", "ok": False, "detail": "지원하지 않는 키입니다."}

    def kakao_authorize_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        secrets = self.store.get_secrets()
        rest_api_key = str(payload.get("kakao_rest_api_key") or secrets.get("kakao_rest_api_key", "")).strip()
        redirect_uri = str(payload.get("kakao_redirect_uri") or secrets.get("kakao_redirect_uri", "")).strip()
        return build_kakao_authorize_url(rest_api_key, redirect_uri)

    def kakao_exchange_token(self, payload: dict[str, Any]) -> dict[str, Any]:
        secrets = self.store.get_secrets()
        rest_api_key = str(payload.get("kakao_rest_api_key") or secrets.get("kakao_rest_api_key", "")).strip()
        redirect_uri = str(payload.get("kakao_redirect_uri") or secrets.get("kakao_redirect_uri", "")).strip()
        client_secret = str(payload.get("kakao_client_secret") or secrets.get("kakao_client_secret", "")).strip()
        code = str(payload.get("kakao_auth_code", "")).strip()
        result = exchange_kakao_code(rest_api_key, redirect_uri, code, client_secret)
        if not result.get("ok"):
            return result

        token_payload = result.get("payload") or {}
        save_payload = {
            "kakao_rest_api_key": rest_api_key,
            "kakao_redirect_uri": redirect_uri,
            "kakao_client_secret": client_secret,
            "kakao_access_token": token_payload.get("access_token", ""),
            "kakao_refresh_token": token_payload.get("refresh_token", ""),
        }
        self.store.save_secrets(save_payload)
        settings = self.settings()
        settings["kakao_token_result"] = {
            "ok": True,
            "detail": result.get("detail", "access token 발급 성공"),
            "expires_in": token_payload.get("expires_in"),
            "refresh_token_expires_in": token_payload.get("refresh_token_expires_in"),
            "request_info": result.get("request_info") or {},
        }
        return settings

    def preview(self) -> dict[str, Any]:
        settings = self.store.get_settings()
        secrets = self.store.get_secrets()
        articles = self.supabase.fetch_articles(settings, secrets)
        digest = build_digest(articles, settings)
        return {
            "digest": digest,
            "article_count": len(articles),
            "logs": self.store.recent_logs(),
            "supabase_configured": self.supabase.configured(secrets),
        }

    def collect_now(self, notify: bool | None = None) -> dict[str, Any]:
        settings = self.store.get_settings()
        config_path = str(settings.get("collector_config_path") or "config/categories.yaml")
        config_abs = self.project_root / config_path
        if not config_abs.exists():
            return {"ok": False, "error": f"수집 설정 파일을 찾을 수 없습니다: {config_path}"}

        src_path = self.project_root / "src"
        if str(src_path) not in sys.path:
            sys.path.insert(0, str(src_path))
        from collector import run as run_collector

        run_collector(str(config_abs))
        should_notify = settings.get("notify_after_collect", True) if notify is None else notify
        result: dict[str, Any] = {"ok": True, "collected": True}
        if should_notify:
            result["send"] = self.send_now(force=True)
            result["ok"] = bool(result["send"].get("ok"))
        return result

    def send_now(self, force: bool = False, provider_override: str | None = None) -> dict[str, Any]:
        settings = self.store.get_settings()
        secrets = self._runtime_secrets()
        articles = self.supabase.fetch_articles(settings, secrets)
        digest = build_digest(articles, settings)
        provider = provider_override or settings.get("provider", "console")

        if not force and self.store.was_sent(digest["hash"]):
            return {"ok": True, "skipped": True, "reason": "duplicate_digest", "digest": digest}

        notifier = get_notifier(provider)
        if provider == "kakao_memo":
            self._refresh_kakao_token(secrets, settings)
        settings["kakao_access_token"] = secrets.get("kakao_access_token", "")
        result = notifier.send(digest["text"], settings)
        if not result["ok"] and settings.get("kakao_refresh_detail"):
            result["detail"] = f"{result['detail']} · token refresh: {settings['kakao_refresh_detail']}"
        status = "sent" if result["ok"] else "failed"
        self.store.record_send(
            digest_hash=digest["hash"],
            provider=provider,
            item_count=len(digest["items"]),
            status=status,
            error="" if result["ok"] else result["detail"],
        )
        return {"ok": bool(result["ok"]), "result": result, "digest": digest}

    def test_kakao_send(self) -> dict[str, Any]:
        return self.send_now(force=True, provider_override="kakao_memo")

    def _refresh_kakao_token(self, secrets: dict[str, str], settings: dict[str, Any]) -> None:
        refresh_token = secrets.get("kakao_refresh_token", "")
        rest_api_key = secrets.get("kakao_rest_api_key", "")
        client_secret = secrets.get("kakao_client_secret", "")
        missing = []
        if not refresh_token:
            missing.append("KAKAO_REFRESH_TOKEN")
        if not rest_api_key:
            missing.append("KAKAO_REST_API_KEY")
        if missing:
            settings["kakao_refresh_detail"] = (
                "access token 자동 갱신 불가: "
                + ", ".join(missing)
                + " secret이 필요합니다."
            )
            return

        result = refresh_kakao_access_token(rest_api_key, refresh_token, client_secret)
        if not result.get("ok"):
            settings["kakao_refresh_detail"] = result.get("detail", "")
            return

        payload = result.get("payload") or {}
        access_token = str(payload.get("access_token") or "").strip()
        new_refresh_token = str(payload.get("refresh_token") or "").strip()
        if access_token:
            secrets["kakao_access_token"] = access_token
        if new_refresh_token:
            secrets["kakao_refresh_token"] = new_refresh_token
            self.store.save_secrets({"kakao_refresh_token": new_refresh_token})
        settings["kakao_refresh_detail"] = result.get("detail", "")

    def _runtime_secrets(self) -> dict[str, str]:
        import os

        secrets = self.store.get_secrets()
        for env_key, secret_key in ENV_SECRET_KEYS.items():
            value = os.environ.get(env_key, "").strip()
            if value:
                secrets[secret_key] = value
        return secrets

    def scheduler_tick(self) -> None:
        settings = self.store.get_settings()
        now = datetime.now(KST)
        minute_key = now.strftime("%Y-%m-%d %H:%M")

        if minute_key != self.last_collect_minute and now.strftime("%H:%M") in set(settings.get("collect_schedule_times") or []):
            self.last_collect_minute = minute_key
            self.collect_now(notify=bool(settings.get("notify_after_collect", True)))
            return

    def web_file(self, relative_path: str) -> Path:
        path = (self.project_root / "web" / relative_path).resolve()
        web_root = (self.project_root / "web").resolve()
        if web_root not in path.parents and path != web_root:
            raise ValueError("invalid path")
        return path


def json_response(handler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
