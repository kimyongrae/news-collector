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
    max_text_length = 950
    articles_per_message = 4
    max_articles = 8

    def send(self, text: str, settings: dict[str, Any]) -> NotifyResult:
        token = (
            settings.get("kakao_access_token")
            or os.environ.get("KAKAO_ACCESS_TOKEN", "")
        ).strip()
        if not token:
            return NotifyResult.fail("KAKAO_ACCESS_TOKEN is not set")

        chunks = _split_kakao_digest(
            text,
            articles_per_message=self.articles_per_message,
            max_articles=self.max_articles,
            limit=self.max_text_length - 8,
        )
        sent_count = 0
        for idx, chunk in enumerate(chunks, 1):
            label = f" ({idx}/{len(chunks)})" if len(chunks) > 1 else ""
            result = self._send_chunk(token, _fit_kakao_text(chunk, label, self.max_text_length))
            if not result["ok"]:
                if sent_count:
                    result["detail"] = f"{sent_count}개 메시지 발송 후 실패: {result['detail']}"
                return result
            sent_count += 1
        return NotifyResult.ok(f"{sent_count}개 메시지 발송 완료")

    def _send_chunk(self, token: str, text: str) -> NotifyResult:
        first_url = _first_url(text) or "https://developers.kakao.com"
        template = {
            "object_type": "text",
            "text": text,
            "link": {
                "web_url": first_url,
                "mobile_web_url": first_url,
            },
            "button_title": "전체보기",
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


def _split_kakao_text(text: str, limit: int) -> list[str]:
    text = (text or "").strip()
    if not text:
        return [""]

    chunks: list[str] = []
    for block in re.split(r"\n{2,}", text):
        block = block.strip()
        if not block:
            continue
        if len(block) > limit:
            chunks.extend(_split_long_block(block, limit))
            continue
        chunks.append(block)
    return chunks or [text[:limit]]


def _split_kakao_digest(
    text: str,
    articles_per_message: int,
    max_articles: int,
    limit: int,
) -> list[str]:
    text = (text or "").strip()
    if not text:
        return [""]

    header_blocks, article_blocks = _parse_digest_blocks(text)

    if not article_blocks:
        return _split_kakao_text(text, limit)

    selected = article_blocks[:max_articles]
    header_lines = _header_lines(header_blocks)
    headline = header_lines[0] if header_lines else "[시황 브리핑]"
    meta = _kakao_digest_meta(header_lines[1:], selected)
    chunks: list[str] = []

    for start in range(0, len(selected), articles_per_message):
        group = selected[start:start + articles_per_message]
        end_num = start + len(group)
        parts = []
        if start == 0:
            header = "\n".join([headline, *(_compact(line, 85) for line in meta)])
            parts.append(header)
        parts.append(f"[중요 뉴스 {start + 1}-{end_num}/{len(selected)}]")
        chunks.append(_render_kakao_chunk(parts, group, limit))

    return chunks


def _parse_digest_blocks(text: str) -> tuple[list[str], list[str]]:
    header_lines: list[str] = []
    article_blocks: list[str] = []
    current_article: list[str] = []

    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "[중요 뉴스]":
            continue
        if re.match(r"^\d+\.\s", line):
            if current_article:
                article_blocks.append("\n".join(current_article))
            current_article = [line]
            continue
        if current_article:
            current_article.append(line)
        elif not article_blocks:
            header_lines.append(line)

    if current_article:
        article_blocks.append("\n".join(current_article))
    return header_lines, article_blocks


def _header_lines(header_blocks: list[str]) -> list[str]:
    lines: list[str] = []
    for block in header_blocks:
        for line in block.splitlines():
            line = line.strip()
            if line:
                lines.append(line)
    return lines


def _kakao_digest_meta(meta: list[str], selected: list[str]) -> list[str]:
    category_counts: dict[str, int] = {}
    for block in selected:
        match = re.match(r"^\d+\.\s+\[([^/\]]+)", block)
        if match:
            category = match.group(1).strip()
            category_counts[category] = category_counts.get(category, 0) + 1

    result = list(meta)
    if category_counts:
        category_text = ", ".join(f"{name} {count}건" for name, count in category_counts.items())
        result = [line for line in result if not line.startswith("주요 카테고리:")]
        result.insert(0, f"주요 카테고리: {category_text}")
    return result[:2]


def _render_kakao_chunk(prefix_parts: list[str], article_blocks: list[str], limit: int) -> str:
    attempts = [
        (66, 42),
        (58, 30),
        (50, 0),
        (42, 0),
    ]
    fallback = ""
    for title_limit, summary_limit in attempts:
        parts = list(prefix_parts)
        parts.extend(
            _compact_kakao_article(block, title_limit, summary_limit)
            for block in article_blocks
        )
        chunk = "\n\n".join(part for part in parts if part != "")
        fallback = chunk
        if len(chunk) <= limit:
            return chunk
    return _trim_preserve_urls(fallback, limit)


def _compact_kakao_article(block: str, title_limit: int = 66, summary_limit: int = 42) -> str:
    title = ""
    summary = ""
    url = ""
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("http://") or line.startswith("https://"):
            url = line
        elif line.startswith("- ") and not summary:
            summary = line
        elif not title:
            title = line

    parts = []
    if title:
        parts.append(_compact(title, title_limit))
    if summary and summary_limit > 0:
        parts.append(_compact(summary, summary_limit))
    if url:
        parts.append(url)
    return "\n".join(parts)


def _split_long_block(block: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        while len(line) > limit:
            head = line[:limit].rstrip()
            chunks.append(head)
            line = line[limit:].lstrip()
        candidate = f"{current}\n{line}".strip() if current else line
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def _first_url(text: str) -> str:
    match = re.search(r"https?://\S+", text or "")
    return match.group(0).rstrip(").,]") if match else ""


def _fit_kakao_text(text: str, suffix: str, limit: int) -> str:
    suffix = suffix or ""
    budget = max(1, limit - len(suffix))
    text = (text or "").strip()
    if len(text) > budget:
        text = text[: budget - 1].rstrip() + "…"
    return f"{text}{suffix}"


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _trim_preserve_lines(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _trim_preserve_urls(text: str, limit: int) -> str:
    lines = [line for line in (text or "").strip().splitlines() if line.strip()]
    kept: list[str] = []
    current_len = 0
    for line in lines:
        line_len = len(line) + (2 if kept else 0)
        if current_len + line_len <= limit:
            kept.append(line)
            current_len += line_len
            continue
        if line.startswith(("http://", "https://")):
            kept.append(line)
            current_len += line_len
    result = "\n\n".join(kept).strip()
    if len(result) <= limit:
        return result
    return _trim_preserve_lines(result, limit)


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


def refresh_kakao_access_token(
    rest_api_key: str,
    refresh_token: str,
    client_secret: str = "",
) -> dict[str, Any]:
    rest_api_key = (rest_api_key or "").strip()
    refresh_token = (refresh_token or "").strip()
    client_secret = (client_secret or "").strip()
    if not rest_api_key:
        return {"ok": False, "detail": "Kakao REST API 키가 비어 있어 access token을 갱신할 수 없습니다."}
    if not refresh_token:
        return {"ok": False, "detail": "Kakao refresh token이 비어 있어 access token을 갱신할 수 없습니다."}

    body = {
        "grant_type": "refresh_token",
        "client_id": rest_api_key,
        "refresh_token": refresh_token,
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
            "detail": f"access token 갱신 성공 (expires_in={payload.get('expires_in')}초)",
            "payload": payload,
        }
    except HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")[:500]
        return {
            "ok": False,
            "detail": f"HTTP {exc.code}: {body_text} · refresh token이 만료/폐기됐으면 카카오 OAuth 인가를 다시 진행해 새 refresh token을 저장해야 합니다.",
        }
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


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
