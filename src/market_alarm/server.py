import json
import mimetypes
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .app import MarketAlarmApp, json_response
from .env import load_dotenv
from .storage import Store


class Handler(BaseHTTPRequestHandler):
    app: MarketAlarmApp

    def log_message(self, fmt, *args):
        sys.stderr.write("[NewsCollector] " + fmt % args + "\n")

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/kakao/callback":
            return self._serve_kakao_callback(parsed)
        if parsed.path == "/api/settings":
            return json_response(self, self.app.settings())
        if parsed.path == "/api/preview":
            try:
                return json_response(self, self.app.preview())
            except Exception as exc:
                return json_response(self, {"error": str(exc)}, 500)
        if parsed.path == "/api/status":
            return json_response(self, {"logs": self.app.store.recent_logs()})
        if parsed.path == "/" or parsed.path == "/index.html":
            return self._serve_file("index.html")
        if parsed.path.startswith("/static/"):
            return self._serve_file(parsed.path.removeprefix("/static/"))
        return self._not_found()

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            payload = self._read_json()
            saved = self.app.save_settings(payload)
            return json_response(self, saved)
        if parsed.path == "/api/secrets":
            payload = self._read_json()
            saved = self.app.save_secrets(payload)
            return json_response(self, saved)
        if parsed.path == "/api/test-connections":
            return json_response(self, self.app.test_connections())
        if parsed.path == "/api/test-secret":
            payload = self._read_json()
            return json_response(self, self.app.test_secret(payload))
        if parsed.path == "/api/collect":
            payload = self._read_json()
            try:
                return json_response(self, self.app.collect_now(payload.get("notify")))
            except Exception as exc:
                return json_response(self, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, 500)
        if parsed.path == "/api/kakao/authorize-url":
            payload = self._read_json()
            return json_response(self, self.app.kakao_authorize_url(payload))
        if parsed.path == "/api/kakao/token":
            payload = self._read_json()
            return json_response(self, self.app.kakao_exchange_token(payload))
        if parsed.path == "/api/send":
            params = parse_qs(parsed.query)
            force = params.get("force", ["0"])[0] in {"1", "true", "yes"}
            try:
                return json_response(self, self.app.send_now(force=force))
            except Exception as exc:
                return json_response(self, {"ok": False, "error": str(exc)}, 500)
        return self._not_found()

    def _read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _serve_file(self, relative_path: str):
        try:
            path = self.app.web_file(relative_path)
        except ValueError:
            return self._not_found()
        if not path.exists() or not path.is_file():
            return self._not_found()
        body = path.read_bytes()
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_kakao_callback(self, parsed):
        params = parse_qs(parsed.query)
        code = params.get("code", [""])[0]
        error = params.get("error", [""])[0]
        body = f"""
<!doctype html>
<html lang="ko">
  <head><meta charset="utf-8" /><title>Kakao OAuth</title></head>
  <body>
    <script>
      localStorage.setItem("marketAlarm.kakaoAuthCode", {json.dumps(code)});
      localStorage.setItem("marketAlarm.kakaoAuthError", {json.dumps(error)});
      location.replace("/");
    </script>
  </body>
</html>
""".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _not_found(self):
        return json_response(self, {"error": "not_found"}, 404)


def _scheduler(app: MarketAlarmApp):
    while True:
        try:
            app.scheduler_tick()
        except Exception as exc:
            print(f"[scheduler] {type(exc).__name__}: {exc}", file=sys.stderr)
        time.sleep(20)


def main():
    project_root = Path(__file__).resolve().parents[2]
    load_dotenv(str(project_root / ".env"))

    db_path = os.environ.get("MARKET_ALARM_DB", "data/market_alarm.sqlite3")
    host = os.environ.get("NEWS_COLLECTOR_HOST") or os.environ.get("MARKET_ALARM_HOST", "127.0.0.1")
    port = int(os.environ.get("NEWS_COLLECTOR_PORT") or os.environ.get("MARKET_ALARM_PORT", "8768"))

    store = Store(str(project_root / db_path if not os.path.isabs(db_path) else db_path))
    app = MarketAlarmApp(store=store, project_root=project_root)
    Handler.app = app

    thread = threading.Thread(target=_scheduler, args=(app,), daemon=True)
    thread.start()

    server = ThreadingHTTPServer((host, port), Handler)
    print(f"NewsCollector running at http://{host}:{port}")
    server.serve_forever()
