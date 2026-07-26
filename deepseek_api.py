#!/usr/bin/env python3
"""Small, dependency-free DeepSeek JSON client shared by cloud scripts."""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}


class DeepSeekAPIError(RuntimeError):
    """Raised when DeepSeek cannot return a usable JSON object."""


def deepseek_model() -> str:
    return os.environ.get("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL


def deepseek_url() -> str:
    return os.environ.get("DEEPSEEK_API_URL", DEFAULT_DEEPSEEK_URL).strip() or DEFAULT_DEEPSEEK_URL


def request_deepseek_json(
    api_key: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    max_tokens: int = 8192,
    timeout: int = 120,
    max_attempts: int = 3,
    opener: Callable[..., Any] | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Call DeepSeek in non-thinking JSON mode with bounded transient retries."""
    api_key = str(api_key or "").strip()
    if not api_key:
        raise DeepSeekAPIError("DEEPSEEK_API_KEY is empty")

    normalized_messages = [dict(message) for message in messages]
    if "json" not in "\n".join(str(message.get("content") or "") for message in normalized_messages).lower():
        normalized_messages.insert(
            0,
            {"role": "system", "content": "Return one valid JSON object only."},
        )

    payload = {
        "model": deepseek_model(),
        "messages": normalized_messages,
        "thinking": {"type": "disabled"},
        "temperature": float(temperature),
        "max_tokens": max(256, int(max_tokens)),
        "response_format": {"type": "json_object"},
        "stream": False,
    }
    open_request = opener or urlopen
    attempts = max(1, int(max_attempts))
    last_error: DeepSeekAPIError | None = None

    for attempt in range(1, attempts + 1):
        request = Request(
            deepseek_url(),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with open_request(request, timeout=timeout) as response:
                raw_response = response.read().decode("utf-8", errors="replace")
            response_data = json.loads(raw_response)
            content = response_data["choices"][0]["message"]["content"]
            result = parse_json_object(content)
            if not result:
                raise DeepSeekAPIError("DeepSeek returned an empty JSON object")
            return result
        except HTTPError as exc:
            detail = _http_error_detail(exc, api_key)
            last_error = DeepSeekAPIError(f"DeepSeek HTTP {exc.code}: {detail}")
            if exc.code not in RETRYABLE_STATUS_CODES:
                raise last_error from exc
        except (URLError, TimeoutError, OSError) as exc:
            last_error = DeepSeekAPIError(f"DeepSeek transport error: {exc}")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, DeepSeekAPIError) as exc:
            last_error = exc if isinstance(exc, DeepSeekAPIError) else DeepSeekAPIError(
                f"DeepSeek returned an invalid JSON response: {exc}"
            )

        if attempt < attempts:
            sleeper(min(2 ** (attempt - 1), 4))

    assert last_error is not None
    raise last_error


def parse_json_object(content: Any) -> dict[str, Any]:
    text = str(content or "").strip()
    if not text:
        raise DeepSeekAPIError("DeepSeek returned empty content")
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.S)
        if not match:
            raise
        value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise DeepSeekAPIError("DeepSeek JSON response is not an object")
    return value


def _http_error_detail(exc: HTTPError, api_key: str) -> str:
    try:
        body = exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - reporting must not hide the original HTTP status.
        body = str(exc.reason or "request rejected")
    body = body.replace(api_key, "***") if api_key else body
    body = re.sub(r"Bearer\s+[^\s\"']+", "Bearer ***", body, flags=re.I)
    body = re.sub(r"\s+", " ", body).strip()
    return body[:600] or str(exc.reason or "request rejected")
