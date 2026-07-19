from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

import httpx


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_douyin_tikhub_daily.py"
SPEC = importlib.util.spec_from_file_location("run_douyin_tikhub_daily", MODULE_PATH)
assert SPEC and SPEC.loader
tikhub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tikhub)


class FakeClient:
    def __init__(self, responses: list[httpx.Response | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, *, json: dict) -> httpx.Response:
        self.calls.append((url, json))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def payload() -> dict:
    return {
        "keyword": "entertainment",
        "cursor": 0,
        "sort_type": "1",
        "publish_time": "1",
        "filter_duration": "0",
        "content_type": "1",
        "search_id": "",
        "backtrace": "",
    }


class TikHubSearchCompatibilityTests(unittest.TestCase):
    def test_http_400_falls_back_from_v2_to_v1(self) -> None:
        client = FakeClient(
            [
                httpx.Response(400, json={"detail": "upstream rejected"}),
                httpx.Response(200, json={"code": 200, "data": {"status_code": 0, "data": []}}),
            ]
        )
        run_info: dict = {}

        result = tikhub.request_tikhub_search(
            client,
            payload(),
            keyword="entertainment",
            page=1,
            preferred_endpoint="video_v2",
            request_count=0,
            max_search_requests=5,
            retry_attempts=2,
            run_info=run_info,
        )

        self.assertEqual(result["endpoint"], "video_v1")
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(client.calls[0][0].endswith("fetch_video_search_v2"))
        self.assertTrue(client.calls[1][0].endswith("fetch_video_search_v1"))
        self.assertIn("upstream rejected", run_info["tikhub_attempts"][0]["response"])

    def test_retryable_status_retries_same_endpoint(self) -> None:
        client = FakeClient(
            [
                httpx.Response(429, json={"message": "slow down"}),
                httpx.Response(200, json={"code": 200, "data": {"status_code": 0}}),
            ]
        )

        with mock.patch.object(tikhub, "sleep_before_retry") as sleep:
            result = tikhub.request_tikhub_search(
                client,
                payload(),
                keyword="entertainment",
                page=1,
                preferred_endpoint="video_v2",
                request_count=0,
                max_search_requests=5,
                retry_attempts=2,
                run_info={},
            )

        self.assertEqual(result["endpoint"], "video_v2")
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(client.calls[0][0], client.calls[1][0])
        sleep.assert_called_once_with(0)

    def test_auth_failure_stops_without_spending_more_requests(self) -> None:
        client = FakeClient([httpx.Response(401, json={"message": "invalid token"})])

        result = tikhub.request_tikhub_search(
            client,
            payload(),
            keyword="entertainment",
            page=1,
            preferred_endpoint="video_v2",
            request_count=0,
            max_search_requests=5,
            retry_attempts=2,
            run_info={},
        )

        self.assertTrue(result["fatal"])
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(len(client.calls), 1)

    def test_general_search_uses_unrestricted_content_type(self) -> None:
        compatible = tikhub.compatible_search_payload(payload(), "general_v1")
        self.assertEqual(compatible["content_type"], "0")
        self.assertEqual(payload()["content_type"], "1")

    def test_empty_success_envelope_is_not_treated_as_search_data(self) -> None:
        self.assertIn("empty data", tikhub.tikhub_envelope_error({"code": 200, "data": None}))


if __name__ == "__main__":
    unittest.main()
