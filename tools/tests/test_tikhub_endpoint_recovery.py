from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

import httpx


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_douyin_tikhub_daily.py"
SPEC = importlib.util.spec_from_file_location("tikhub_endpoint_recovery", MODULE_PATH)
assert SPEC and SPEC.loader
tikhub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tikhub)


class FakeClient:
    def __init__(self, statuses: list[int]) -> None:
        self.statuses = iter(statuses)
        self.calls: list[str] = []

    def post(self, url: str, *, json: dict) -> httpx.Response:
        self.calls.append(url)
        status = next(self.statuses)
        body = {"code": 200, "data": {"status_code": 0, "data": []}} if status == 200 else {"detail": "rejected"}
        return httpx.Response(status, json=body)


class TikHubEndpointRecoveryTests(unittest.TestCase):
    def search(self, client, run_info, *, preferred="video_v2", count=0, limit=15, retries=1):
        return tikhub.request_tikhub_search(
            client,
            {"keyword": "明星 采访", "content_type": "1"},
            keyword="明星 采访",
            page=1,
            preferred_endpoint=preferred,
            request_count=count,
            max_search_requests=limit,
            retry_attempts=retries,
            run_info=run_info,
        )

    def test_incompatible_video_endpoints_are_not_reprobed_for_next_keyword(self) -> None:
        for status in (400, 404, 405, 422):
            with self.subTest(status=status):
                client = FakeClient([status, status, 200, 400])
                run_info: dict = {}
                first = self.search(client, run_info)
                second = self.search(client, run_info, preferred=first["endpoint"], count=first["request_count"])
                self.assertEqual(first["endpoint"], "general_v1")
                self.assertEqual(second["request_count"], 4)
                self.assertIsNone(second["data"])
                self.assertEqual(client.calls, [url for _, url in tikhub.TIKHUB_VIDEO_SEARCH_ENDPOINTS] + [tikhub.TIKHUB_VIDEO_SEARCH_ENDPOINTS[2][1]])

    def test_previously_successful_video_endpoint_remains_available_after_keyword_failure(self) -> None:
        client = FakeClient([200, 400, 400, 200, 400, 200])
        run_info: dict = {}
        first = self.search(client, run_info)
        second = self.search(client, run_info, count=first["request_count"])
        third = self.search(client, run_info, preferred=second["endpoint"], count=second["request_count"])
        self.assertEqual(second["endpoint"], "general_v1")
        self.assertEqual(third["endpoint"], "video_v2")
        self.assertEqual(third["request_count"], 6)
        self.assertEqual(client.calls[-2:], [tikhub.TIKHUB_VIDEO_SEARCH_ENDPOINTS[2][1], tikhub.TIKHUB_VIDEO_SEARCH_ENDPOINTS[0][1]])

    def test_successful_preferred_endpoint_is_used_first(self) -> None:
        client = FakeClient([400, 200, 200])
        run_info: dict = {}
        first = self.search(client, run_info)
        second = self.search(client, run_info, preferred=first["endpoint"], count=first["request_count"])
        self.assertEqual(second["endpoint"], "video_v1")
        self.assertEqual(second["request_count"], 3)
        self.assertEqual(client.calls[-1], tikhub.TIKHUB_VIDEO_SEARCH_ENDPOINTS[1][1])

    def test_fatal_response_still_stops_without_fallback(self) -> None:
        for status in (401, 402, 403):
            with self.subTest(status=status):
                client = FakeClient([status])
                result = self.search(client, {})
                self.assertTrue(result["fatal"])
                self.assertEqual(result["request_count"], 1)
                self.assertEqual(len(client.calls), 1)

    def test_exhausted_budget_makes_no_request(self) -> None:
        client = FakeClient([])
        run_info = {"tikhub_attempts": [{"endpoint": "video_v2", "outcome": "http_error", "http_status": 400}]}
        result = self.search(client, run_info, count=15)
        self.assertEqual(result["request_count"], 15)
        self.assertIn("budget exhausted", result["error"])
        self.assertEqual(client.calls, [])

    def test_last_budget_slot_cannot_trigger_fallback(self) -> None:
        client = FakeClient([400])
        result = self.search(client, {}, count=14)
        self.assertEqual(result["request_count"], 15)
        self.assertIn("budget exhausted", result["error"])
        self.assertEqual(len(client.calls), 1)

    def test_retryable_response_keeps_existing_retry_behavior(self) -> None:
        client = FakeClient([503, 200])
        with mock.patch.object(tikhub, "sleep_before_retry") as sleep:
            result = self.search(client, {}, retries=2)
        self.assertEqual(result["endpoint"], "video_v2")
        self.assertEqual(result["request_count"], 2)
        self.assertEqual(client.calls[0], client.calls[1])
        sleep.assert_called_once_with(0)

    def test_incompatibility_history_is_local_to_the_run(self) -> None:
        history = [{"endpoint": "video_v2", "outcome": "http_error", "http_status": 400}]
        self.assertNotIn("video_v2", [name for name, _ in tikhub.ordered_search_endpoints("video_v2", history)])
        self.assertEqual(tikhub.ordered_search_endpoints("video_v2")[0][0], "video_v2")


if __name__ == "__main__":
    unittest.main()
