from __future__ import annotations

import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import deepseek_api


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class FakeOpener:
    def __init__(self, responses: list[FakeResponse | Exception]) -> None:
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request: object, *, timeout: int) -> FakeResponse:
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def success_response(content: str = '{"status":"ok"}') -> FakeResponse:
    return FakeResponse({"choices": [{"message": {"content": content}}]})


class DeepSeekAPIClientTests(unittest.TestCase):
    def test_uses_v4_flash_non_thinking_json_mode(self) -> None:
        opener = FakeOpener([success_response()])

        result = deepseek_api.request_deepseek_json(
            "test-key",
            [{"role": "user", "content": "Return JSON with status ok"}],
            temperature=0.2,
            opener=opener,
            sleeper=lambda _: None,
        )

        request, timeout = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(result, {"status": "ok"})
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 8192)
        self.assertFalse(payload["stream"])
        self.assertEqual(timeout, 120)

    def test_adds_json_instruction_when_caller_omits_it(self) -> None:
        opener = FakeOpener([success_response()])

        deepseek_api.request_deepseek_json(
            "test-key",
            [{"role": "user", "content": "Return the status"}],
            temperature=0,
            opener=opener,
            sleeper=lambda _: None,
        )

        request, _ = opener.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertIn("JSON", payload["messages"][0]["content"])

    def test_retries_transient_http_failure(self) -> None:
        error = HTTPError(
            deepseek_api.DEFAULT_DEEPSEEK_URL,
            503,
            "busy",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"server busy"}}'),
        )
        opener = FakeOpener([error, success_response()])
        sleeps: list[float] = []

        result = deepseek_api.request_deepseek_json(
            "test-key",
            [{"role": "user", "content": "Return JSON"}],
            temperature=0,
            opener=opener,
            sleeper=sleeps.append,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(opener.requests), 2)
        self.assertEqual(sleeps, [1])

    def test_non_retryable_http_error_includes_server_detail(self) -> None:
        error = HTTPError(
            deepseek_api.DEFAULT_DEEPSEEK_URL,
            400,
            "bad request",
            hdrs=None,
            fp=io.BytesIO(b'{"error":{"message":"model has been discontinued"}}'),
        )
        opener = FakeOpener([error])

        with self.assertRaisesRegex(deepseek_api.DeepSeekAPIError, "model has been discontinued"):
            deepseek_api.request_deepseek_json(
                "test-key",
                [{"role": "user", "content": "Return JSON"}],
                temperature=0,
                opener=opener,
                sleeper=lambda _: None,
            )

        self.assertEqual(len(opener.requests), 1)

    def test_model_can_be_overridden_without_code_change(self) -> None:
        with mock.patch.dict("os.environ", {"DEEPSEEK_MODEL": "deepseek-v4-pro"}):
            self.assertEqual(deepseek_api.deepseek_model(), "deepseek-v4-pro")


if __name__ == "__main__":
    unittest.main()
