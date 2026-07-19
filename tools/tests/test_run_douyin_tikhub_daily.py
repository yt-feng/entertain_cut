from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import SimpleNamespace
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

    def post(self, url: str, *, json: dict, headers: dict | None = None) -> httpx.Response:
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


class TikHubSearchBudgetTests(unittest.TestCase):
    def test_daily_ten_cent_budget_caps_search_at_ten_calls(self) -> None:
        self.assertEqual(tikhub.resolve_search_request_limit(50, 0.10), 10)

    def test_lower_configured_limit_wins(self) -> None:
        self.assertEqual(tikhub.resolve_search_request_limit(7, 0.10), 7)

    def test_zero_budget_disables_only_the_dollar_cap(self) -> None:
        self.assertEqual(tikhub.resolve_search_request_limit(10, 0), 10)

    def test_sub_cent_positive_budget_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            tikhub.resolve_search_request_limit(10, 0.005)

    def test_broad_seed_keywords_fill_the_ten_request_plan(self) -> None:
        args = SimpleNamespace(max_search_requests=10)
        seeds = ["娱乐", "明星", "娱乐圈", "综艺", "热播剧 演员", "明星 评论区", "明星 采访", "明星 舞台", "明星 红毯", "演唱会 明星"]

        planned = tikhub.plan_search_keywords(args, seeds, {"terms": []})

        self.assertEqual(planned, seeds)

    def test_hot_terms_keep_half_the_plan_for_broad_keywords(self) -> None:
        args = SimpleNamespace(max_search_requests=10)
        seeds = ["娱乐", "明星", "娱乐圈", "综艺", "热播剧 演员", "明星 评论区"]
        hot_context = {"terms": ["杨紫", "赵丽颖", "刘亦菲", "白鹿", "王一博", "肖战"]}

        planned = tikhub.plan_search_keywords(args, seeds, hot_context)

        self.assertEqual(planned[:5], ["杨紫 热议", "赵丽颖 热议", "刘亦菲 热议", "白鹿 热议", "王一博 热议"])
        self.assertEqual(planned[5:], seeds[:5])


class TavilyHotContextTests(unittest.TestCase):
    def test_tavily_uses_one_china_focused_general_search_for_the_last_day(self) -> None:
        client = FakeClient(
            [
                httpx.Response(
                    200,
                    request=httpx.Request("POST", tikhub.TAVILY_SEARCH_URL),
                    json={
                        "results": [
                            {
                                "title": "World Cup final",
                                "content": "Argentina and Spain prepare for the match.",
                                "url": "https://example.com/sports",
                            },
                            {
                                "title": "杨紫新剧引发热议",
                                "content": "相关片段登上热搜。",
                                "url": "https://example.com/news",
                                "published_date": "2026-07-19",
                            }
                        ],
                        "usage": {"credits": 1},
                        "response_time": 1.2,
                        "request_id": "test-request",
                    },
                )
            ]
        )
        context: dict = {"errors": [], "sources": []}

        with mock.patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            items = tikhub.fetch_tavily_context(client, 10, context)

        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0][1]["topic"], "general")
        self.assertEqual(client.calls[0][1]["country"], "china")
        self.assertEqual(client.calls[0][1]["time_range"], "day")
        self.assertEqual(client.calls[0][1]["search_depth"], "basic")
        self.assertEqual(client.calls[0][1]["include_domains"], tikhub.TAVILY_ENTERTAINMENT_DOMAINS)
        self.assertEqual(context["tavily_usage"]["credits"], 1)
        self.assertEqual(context["sources"], ["tavily"])
        self.assertEqual(context["tavily_discarded_result_count"], 1)
        self.assertEqual(len(items), 1)

    def test_douyin_candidates_supply_hot_terms_when_external_search_is_empty(self) -> None:
        candidates = [
            {
                "title": "#杨紫 新剧《国色芳华》名场面 #我要上热门",
                "like_count": 50_000,
                "comment_count": 500,
            },
            {"title": "#赵丽颖 红毯采访", "like_count": 20_000, "comment_count": 200},
        ]
        context = {"terms": [], "items": [], "sources": []}

        tikhub.enrich_hot_context_from_candidates(context, candidates)

        self.assertIn("杨紫", context["douyin_terms"])
        self.assertIn("国色芳华", context["douyin_terms"])
        self.assertIn("赵丽颖", context["douyin_terms"])
        self.assertNotIn("我要上热门", context["douyin_terms"])
        self.assertTrue(context["available"])
        self.assertEqual(context["sources"], ["douyin_search_metadata"])


if __name__ == "__main__":
    unittest.main()
