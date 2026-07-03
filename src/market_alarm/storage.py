import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
import re


DEFAULT_SETTINGS = {
    "enabled": False,
    "provider": "kakao_memo",
    "schedule_times": ["10:17"],
    "lookback_hours": 24,
    "min_importance": 4,
    "max_items": 10,
    "categories": ["경제", "주식", "금리/환율", "네이버금융", "많이본뉴스_경제", "많이본뉴스_IT", "IT/산업"],
    "sentiments": ["긍정", "부정", "중립"],
    "include_ranked": True,
    "include_links": True,
    "headline": "오전 시황 브리핑",
    "collect_schedule_times": ["10:17"],
    "notify_after_collect": True,
    "collector_config_path": "config/categories.yaml",
}

SECRET_KEYS = {
    "supabase_url",
    "supabase_service_key",
    "kakao_access_token",
    "kakao_refresh_token",
    "kakao_rest_api_key",
    "kakao_redirect_uri",
    "kakao_client_secret",
}


class Store:
    def __init__(self, db_path: str):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self._import_missing_legacy_secrets()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists settings (
                    key text primary key,
                    value text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists notification_log (
                    digest_hash text primary key,
                    provider text not null,
                    sent_at text not null,
                    item_count integer not null,
                    status text not null,
                    error text
                )
                """
            )
            conn.execute(
                """
                create table if not exists secrets (
                    key text primary key,
                    value text not null,
                    updated_at text not null
                )
                """
            )
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "insert or ignore into settings(key, value) values(?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )

    def _import_missing_legacy_secrets(self) -> None:
        legacy_path = self.path.parent.parent.parent / "MarketAlarm" / "data" / self.path.name
        if not legacy_path.exists() or legacy_path.resolve() == self.path.resolve():
            return

        try:
            legacy = sqlite3.connect(legacy_path)
            legacy.row_factory = sqlite3.Row
            rows = legacy.execute("select key, value, updated_at from secrets").fetchall()
        except sqlite3.Error:
            return
        finally:
            try:
                legacy.close()
            except Exception:
                pass

        with self._connect() as conn:
            for row in rows:
                key = str(row["key"])
                value = str(row["value"])
                if key not in SECRET_KEYS or not is_valid_secret_value(key, value):
                    continue
                exists = conn.execute("select 1 from secrets where key = ?", (key,)).fetchone()
                if exists:
                    continue
                conn.execute(
                    "insert into secrets(key, value, updated_at) values(?, ?, ?)",
                    (key, value, row["updated_at"] or datetime.now(timezone.utc).isoformat()),
                )

    def get_settings(self) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute("select key, value from settings").fetchall()
        settings = dict(DEFAULT_SETTINGS)
        for row in rows:
            try:
                settings[row["key"]] = json.loads(row["value"])
            except json.JSONDecodeError:
                settings[row["key"]] = row["value"]
        return settings

    def save_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        merged = dict(DEFAULT_SETTINGS)
        merged.update(_normalize_settings(settings))
        with self._connect() as conn:
            for key, value in merged.items():
                conn.execute(
                    "insert or replace into settings(key, value) values(?, ?)",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return merged

    def get_secrets(self) -> dict[str, str]:
        with self._connect() as conn:
            rows = conn.execute("select key, value from secrets").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def get_secret_status(self) -> dict[str, bool]:
        secrets = self.get_secrets()
        return {key: is_valid_secret_value(key, secrets.get(key, "")) for key in SECRET_KEYS}

    def get_secret_info(self) -> dict[str, dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("select key, value, updated_at from secrets").fetchall()
        info = {key: {"saved": False, "updated_at": "", "masked": ""} for key in SECRET_KEYS}
        for row in rows:
            if row["key"] in info:
                value = str(row["value"])
                info[row["key"]] = {
                    "saved": is_valid_secret_value(row["key"], value),
                    "updated_at": row["updated_at"],
                    "masked": _mask_secret(value),
                }
        return info

    def save_secrets(self, payload: dict[str, Any]) -> dict[str, bool]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            for key in SECRET_KEYS:
                value = str(payload.get(key, "")).strip()
                if not value:
                    continue
                if not is_valid_secret_value(key, value):
                    continue
                conn.execute(
                    "insert or replace into secrets(key, value, updated_at) values(?, ?, ?)",
                    (key, value, now),
                )
        return self.get_secret_status()

    def was_sent(self, digest_hash: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "select 1 from notification_log where digest_hash = ?",
                (digest_hash,),
            ).fetchone()
        return row is not None

    def record_send(
        self,
        digest_hash: str,
        provider: str,
        item_count: int,
        status: str,
        error: str = "",
    ) -> None:
        sent_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                insert or replace into notification_log
                    (digest_hash, provider, sent_at, item_count, status, error)
                values (?, ?, ?, ?, ?, ?)
                """,
                (digest_hash, provider, sent_at, item_count, status, error),
            )

    def recent_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select digest_hash, provider, sent_at, item_count, status, error
                from notification_log
                order by sent_at desc
                limit ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]


def _normalize_settings(settings: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in settings.items():
        if key in {"enabled", "include_ranked", "include_links"}:
            normalized[key] = bool(value)
        elif key in {"lookback_hours", "min_importance", "max_items"}:
            normalized[key] = int(value)
        elif key in {"categories", "sentiments", "schedule_times", "collect_schedule_times"}:
            normalized[key] = [str(v).strip() for v in value if str(v).strip()]
        elif key in {"notify_after_collect"}:
            normalized[key] = bool(value)
        elif key in DEFAULT_SETTINGS:
            normalized[key] = str(value).strip()
    normalized["lookback_hours"] = max(1, min(normalized.get("lookback_hours", 24), 168))
    normalized["min_importance"] = max(1, min(normalized.get("min_importance", 4), 5))
    normalized["max_items"] = max(1, min(normalized.get("max_items", 8), 30))
    return normalized


def is_valid_secret_value(key: str, value: str) -> bool:
    value = (value or "").strip()
    if not value:
        return False
    if key == "supabase_url":
        return value.startswith(("https://", "http://")) and ".supabase.co" in value
    if key == "supabase_service_key":
        return value.startswith(("sb_", "eyJ"))
    if key == "kakao_access_token":
        return not (
            value.startswith(("https://", "http://", "sb_"))
            or _looks_like_kakao_app_key(value)
        )
    if key == "kakao_refresh_token":
        return not value.startswith(("https://", "http://", "sb_"))
    if key == "kakao_rest_api_key":
        return _looks_like_kakao_app_key(value)
    if key == "kakao_redirect_uri":
        return value.startswith(("https://", "http://"))
    if key == "kakao_client_secret":
        return not value.startswith(("https://", "http://", "sb_"))
    return False


def _looks_like_kakao_app_key(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-fA-F]{32}", value or ""))


def _mask_secret(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("http"):
        return value
    if len(value) <= 8:
        return "•" * len(value)
    return f"{value[:4]}{'•' * 8}{value[-4:]}"
