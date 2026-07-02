import json
import os
import re
from typing import Any
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class NotifyResult(dict):
    @classmethod
    def ok(cls, detail: str = "") -> "NotifyResult":
        return cls({"ok": True, "detail": detail})

    @classmethod
    def fail(cls, detail: str) -> "NotifyResult":
        return cls({"ok": False, "detail": detail})


class ConsoleNotifier:
    name = "console"

    def send(self, text: str, settings: dict[str, Any]) -> NotifyResult:
        print("\n===== MarketAlarm Preview =====")
        print(text)
        print("===== /MarketAlarm Preview =====\n")
        return NotifyResult.ok("console output")


class KakaoMemoNotifier:
    name = "kakao_memo"
    endpoint = "https://kapi.kakao.com/v2/api/talk/memo/default/send"

    def send(self, text: str, settings: dict[str, Any]) -> NotifyResult:
        token = (
            settings.get("kakao_access_token")
            or os.environ.get("KAKAO_ACCESS_TOKEN", "")
        ).strip()
        if not token:
            return NotifyResult.fail("KAKAO_ACCESS_TOKEN is not set")

        template = {
            "object_type": "text",
            "text": text[:950],
            "link": {
                "web_url": "https://developers.kakao.com",
                "mobile_web_url": "https://developers.kakao.com",
            },
            "button_title": "열기",
        }
        body = urlencode({"template_object": json.dumps(template, ensure_ascii=False)}).encode("utf-8")
        req = Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            },
            method="POST",
        )
        try:
            with urlopen(req, timeout=15) as resp:
                payload = resp.read().decode("utf-8")
            return NotifyResult.ok(payload)
        except HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")[:500]
            detail = f"HTTP {exc.code}: {body_text}"
            if exc.code == 403:
                detail += " · Kakao talk_message 동의 권한이 없거나 카카오톡 메시지 API 설정이 비활성일 가능성이 큽니다. 인가 URL을 다시 생성해 동의 후 토큰을 재발급하세요."
            return NotifyResult.fail(detail)
        except Exception as exc:
            return NotifyResult.fail(f"{type(exc).__name__}: {exc}")


NOTIFIERS = {
    ConsoleNotifier.name: ConsoleNotifier(),
    KakaoMemoNotifier.name: KakaoMemoNotifier(),
}


def get_notifier(name: str):
    return NOTIFIERS.get(name) or NOTIFIERS["console"]


def build_kakao_authorize_url(rest_api_key: str, redirect_uri: str, scope: str = "talk_message") -> dict[str, Any]:
    rest_api_key = (rest_api_key or "").strip()
    redirect_uri = (redirect_uri or "").strip()
    if not rest_api_key:
        return {"ok": False, "detail": "Kakao REST API 키가 비어 있습니다."}
    if not redirect_uri:
        return {"ok": False, "detail": "Kakao Redirect URI가 비어 있습니다."}
    query = {
        "client_id": rest_api_key,
        "response_type": "code",
        "redirect_uri": redirect_uri,
    }
    if scope:
        query["scope"] = scope
    return {
        "ok": True,
        "authorize_url": f"https://kauth.kakao.com/oauth/authorize?{urlencode(query)}",
    }


def exchange_kakao_code(
    rest_api_key: str,
    redirect_uri: str,
    code: str,
    client_secret: str = "",
) -> dict[str, Any]:
    rest_api_key = (rest_api_key or "").strip()
    redirect_uri = (redirect_uri or "").strip()
    code = (code or "").strip()
    client_secret = (client_secret or "").strip()
    if not rest_api_key:
        return {"ok": False, "detail": "Kakao REST API 키가 비어 있습니다."}
    if not redirect_uri:
        return {"ok": False, "detail": "Kakao Redirect URI가 비어 있습니다."}
    if not code:
        return {"ok": False, "detail": "인가 code가 비어 있습니다."}

    request_info = {
        "redirect_uri": redirect_uri,
        "client_secret_used": bool(client_secret),
        "code_length": len(code),
        "rest_api_key_length": len(rest_api_key),
        "rest_api_key_tail": rest_api_key[-4:] if len(rest_api_key) >= 4 else "",
    }
    body = {
        "grant_type": "authorization_code",
        "client_id": rest_api_key,
        "redirect_uri": redirect_uri,
        "code": code,
    }
    if client_secret:
        body["client_secret"] = client_secret
    req = Request(
        "https://kauth.kakao.com/oauth/token",
        data=urlencode(body).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "detail": f"access token 발급 성공 (expires_in={payload.get('expires_in')}초)",
            "payload": payload,
            "request_info": request_info,
        }
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:500]
        detail = body_text
        try:
            error_payload = json.loads(body_text)
        except json.JSONDecodeError:
            error_payload = {}
        if error_payload.get("error_code") == "KOE010":
            detail = (
                "KOE010 Bad client credentials · REST API 키가 틀렸거나, "
                "Kakao 앱의 Client Secret 사용 설정이 켜져 있는데 Client Secret을 입력하지 않았거나 틀렸습니다. "
                "Kakao Developers > 앱 키의 REST API 키를 사용 중인지, 보안의 Client Secret 상태를 확인하세요."
            )
        elif error_payload.get("error") == "invalid_grant":
            detail = (
                "invalid_grant · authorization code가 만료됐거나 이미 사용됐거나, "
                "인가 URL 생성 때 사용한 Redirect URI와 토큰 발급 Redirect URI가 다릅니다. 새 code를 다시 발급하세요."
            )
        return {
            "ok": False,
            "detail": f"HTTP {exc.code}: {detail}",
            "request_info": request_info,
        }
    except Exception as exc:
        return {
            "ok": False,
            "detail": f"{type(exc).__name__}: {exc}",
            "request_info": request_info,
        }


def test_kakao_token(token: str) -> dict[str, Any]:
    token = (token or "").strip()
    if not token:
        return {"ok": False, "detail": "Kakao Access Token이 비어 있습니다."}
    if re.fullmatch(r"[0-9a-fA-F]{32}", token):
        return {
            "ok": False,
            "detail": "Kakao Access Token이 아니라 REST API 키/앱 키 형식입니다. 카카오 OAuth로 발급한 access_token을 입력하세요.",
        }

    req = Request(
        "https://kapi.kakao.com/v1/user/access_token_info",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        expires = payload.get("expires_in")
        app_id = payload.get("app_id")
        return {
            "ok": True,
            "detail": f"토큰 유효함 (app_id={app_id}, expires_in={expires}초)",
        }
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:300]
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("msg") or payload.get("message") or body
        if exc.code == 401:
            message = f"{message} · access token이 만료됐거나 잘못 발급된 값입니다. 새 access_token을 발급해서 저장하세요."
        return {"ok": False, "detail": f"HTTP {exc.code}: {message}"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}
