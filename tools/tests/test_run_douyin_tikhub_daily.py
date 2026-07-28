from __future__ import annotations

import importlib.util
import datetime as dt
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

    def test_broad_keywords_receive_eighty_percent_of_search_budget(self) -> None:
        args = SimpleNamespace(max_search_requests=10)
        seeds = ["娱乐", "明星", "娱乐圈", "综艺", "热播剧 演员", "明星 评论区", "明星 采访", "明星 舞台"]
        hot_context = {"terms": ["杨紫", "赵丽颖", "刘亦菲", "白鹿", "王一博", "肖战"]}

        planned = tikhub.plan_search_keywords(args, seeds, hot_context)

        self.assertEqual(planned[:2], ["杨紫 热议", "赵丽颖 热议"])
        self.assertEqual(planned[2:], seeds)

    def test_fifteen_request_budget_reserves_five_for_other_platforms(self) -> None:
        args = SimpleNamespace(max_search_requests=15, douyin_search_requests=10)
        seeds = [f"明星话题{index}" for index in range(12)]

        planned = tikhub.plan_search_keywords(args, seeds, {"terms": ["杨紫", "赵丽颖"]})

        self.assertEqual(len(planned), 10)
        self.assertEqual(planned[:2], ["杨紫 热议", "赵丽颖 热议"])
        self.assertEqual(planned[2:], seeds[:8])

    def test_multiplatform_jobs_cover_metric_sorted_kuaishou_and_bilibili(self) -> None:
        jobs = tikhub.plan_multiplatform_searches({"terms": ["杨紫"]})

        self.assertEqual(len(jobs), 5)
        self.assertEqual({platform for platform, _ in jobs}, {"kuaishou", "bilibili"})
        self.assertIn(("kuaishou", "杨紫 明星"), jobs)
        self.assertTrue(tikhub.TIKHUB_BILIBILI_SEARCH_URL.endswith("/bilibili/web/fetch_general_search"))

    def test_multiplatform_runs_when_there_are_enough_reserves_but_too_few_primary_clips(self) -> None:
        candidates = [
            {"engagement_tier_rank": 3},
            *({"engagement_tier_rank": 2} for _ in range(7)),
        ]

        self.assertTrue(
            tikhub.needs_multiplatform_fallback(candidates, publish_limit=5, reserve_target=8)
        )

    def test_multiplatform_stays_idle_for_five_primary_and_three_strong_reserves(self) -> None:
        candidates = [
            *({"engagement_tier_rank": 3} for _ in range(5)),
            *({"engagement_tier_rank": 2} for _ in range(3)),
        ]

        self.assertFalse(
            tikhub.needs_multiplatform_fallback(candidates, publish_limit=5, reserve_target=8)
        )


class TikHubCandidateSelectionTests(unittest.TestCase):
    def test_current_candidates_fill_by_engagement_tier_without_old_video(self) -> None:
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        candidates = [
            {"aweme_id": "primary", "title": "明星舞台", "like_count": 20_000, "create_time": now - 60},
            {"aweme_id": "fallback", "title": "明星采访", "like_count": 1_500, "create_time": now - 120},
            {"aweme_id": "emergency", "title": "明星红毯", "like_count": 600, "create_time": now - 180},
            {"aweme_id": "too-low", "title": "明星直播", "like_count": 399, "create_time": now - 240},
            {"aweme_id": "old", "title": "明星名场面", "like_count": 100_000, "create_time": now - 90_000},
        ]

        selected = tikhub.select_candidates(
            candidates,
            10,
            24,
            10_000,
            1_000,
            400,
            0,
            60,
            300,
            "明星",
            "",
            set(),
            {"terms": []},
        )

        self.assertEqual([item["aweme_id"] for item in selected], ["primary", "fallback", "emergency"])
        self.assertEqual([item["engagement_tier"] for item in selected], ["primary", "fallback", "emergency"])

    def test_search_keyword_alone_is_not_entertainment_evidence(self) -> None:
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        candidates = [
            {
                "aweme_id": "math",
                "title": "王虹导师讲数学题",
                "author": "学习课堂",
                "source_keyword": "娱乐 明星",
                "like_count": 30_000,
                "create_time": now - 60,
            }
        ]

        selected = tikhub.select_candidates(
            candidates,
            5,
            24,
            10_000,
            1_000,
            1_000,
            0,
            60,
            300,
            "娱乐,明星,演员,综艺",
            "",
            set(),
            {"terms": []},
        )

        self.assertEqual(selected, [])

    def test_bilibili_high_play_and_interaction_is_equivalent_to_high_likes(self) -> None:
        tier = tikhub.classify_engagement(
            {
                "platform": "bilibili",
                "like_count": 800,
                "play_count": 600_000,
                "comment_count": 1_000,
                "share_count": 300,
                "collect_count": 300,
            },
            primary_min_likes=10_000,
            fallback_min_likes=1_000,
            emergency_min_likes=1_000,
        )

        self.assertEqual(tier[0:2], ("primary", 3))


class MultiPlatformNormalizationTests(unittest.TestCase):
    def test_kuaishou_response_normalizes_freshness_metrics_and_direct_url(self) -> None:
        now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
        payload = {
            "code": 200,
            "data": {
                "feeds": [
                    {
                        "photoId": "3x-test",
                        "caption": "杨紫综艺名场面",
                        "timestamp": now_ms,
                        "duration": 65_000,
                        "likeCount": "1.2万",
                        "commentCount": 900,
                        "playUrl": "https://cdn.example.com/video.mp4",
                        "user": {"name": "娱乐现场"},
                    }
                ]
            },
        }

        items = tikhub.normalize_platform_response(
            "kuaishou", payload, "明星 娱乐", Path("kuaishou.json")
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["platform"], "kuaishou")
        self.assertEqual(items[0]["like_count"], 12_000)
        self.assertEqual(items[0]["duration_ms"], 65_000)
        self.assertEqual(items[0]["download_urls"], ["https://cdn.example.com/video.mp4"])
        self.assertTrue(items[0]["url"].endswith("/3x-test"))

    def test_bilibili_response_builds_page_url_and_parses_metrics(self) -> None:
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        payload = {
            "code": 200,
            "data": {
                "item": [
                    {
                        "bvid": "BV1TEST",
                        "title": "<em>赵丽颖</em>新剧片段",
                        "author": "影视现场",
                        "pubdate": now,
                        "duration": "01:20",
                        "play": "32.5万",
                    }
                ]
            },
        }

        items = tikhub.normalize_platform_response(
            "bilibili", payload, "热播剧 演员", Path("bilibili.json")
        )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "赵丽颖新剧片段")
        self.assertEqual(items[0]["play_count"], 325_000)
        self.assertEqual(items[0]["duration_ms"], 80_000)
        self.assertEqual(items[0]["url"], "https://www.bilibili.com/video/BV1TEST")

    def test_bilibili_app_aid_result_is_also_recognized(self) -> None:
        payload = {
            "code": 200,
            "data": {
                "items": [
                    {
                        "goto": "av",
                        "param": "123456",
                        "title": "杨紫综艺现场",
                        "ptime": int(dt.datetime.now(dt.timezone.utc).timestamp()),
                        "play": 200_000,
                        "danmaku": 500,
                    }
                ]
            },
        }

        items = tikhub.normalize_platform_response(
            "bilibili", payload, "明星 娱乐", Path("bilibili.json")
        )

        self.assertEqual(items[0]["content_id"], "av123456")
        self.assertEqual(items[0]["url"], "https://www.bilibili.com/video/av123456")


class DeepSeekCandidateReviewTests(unittest.TestCase):
    def test_review_uses_shared_json_client_and_applies_editor_score(self) -> None:
        args = SimpleNamespace(
            deepseek_candidate_review=True,
            deepseek_candidate_review_count=30,
            limit=1,
            download_candidate_multiplier=1,
        )
        selected = [
            {
                "aweme_id": "123",
                "title": "杨紫新剧名场面",
                "author": "娱乐现场",
                "like_count": 20_000,
                "comment_count": 500,
                "share_count": 100,
                "duration_ms": 60_000,
                "create_time_iso": "2026-07-26T00:00:00Z",
                "quality_score": 50,
            }
        ]
        report = {
            "items": [
                {
                    "aweme_id": "123",
                    "editor_score": 80,
                    "comment_hook": "演技是否出圈",
                    "reason": "明星和剧名明确",
                    "primary_celebrities": ["杨紫"],
                    "verified_entities": ["杨紫"],
                    "discard": False,
                }
            ]
        }

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), mock.patch.object(
            tikhub, "request_deepseek_json", return_value=report
        ) as request_json:
            result = tikhub.deepseek_candidate_review(args, selected, {"terms": [], "items": []}, {})

        messages = request_json.call_args.args[1]
        self.assertIn("JSON", messages[0]["content"])
        self.assertEqual(result[0]["deepseek_editor_score"], 80)
        self.assertEqual(result[0]["primary_celebrities"], ["杨紫"])
        self.assertEqual(result[0]["verified_entities"], ["杨紫"])

    def test_review_discard_is_a_hard_filter(self) -> None:
        args = SimpleNamespace(
            deepseek_candidate_review=True,
            deepseek_candidate_review_count=30,
            limit=1,
            download_candidate_multiplier=2,
        )
        selected = [
            {"aweme_id": "keep", "title": "杨紫综艺现场", "quality_score": 80},
            {"aweme_id": "drop", "title": "导师讲数学题", "quality_score": 90},
        ]
        report = {
            "items": [
                {"aweme_id": "keep", "editor_score": 80, "discard": False},
                {"aweme_id": "drop", "editor_score": 10, "discard": True},
            ]
        }

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), mock.patch.object(
            tikhub, "request_deepseek_json", return_value=report
        ):
            result = tikhub.deepseek_candidate_review(args, selected, {"terms": [], "items": []}, {})

        self.assertEqual([item["aweme_id"] for item in result], ["keep"])

    def test_reviewed_choice_ranks_before_unreviewed_reserve(self) -> None:
        args = SimpleNamespace(
            deepseek_candidate_review=True,
            deepseek_candidate_review_count=30,
            limit=1,
            download_candidate_multiplier=2,
        )
        selected = [
            {
                "aweme_id": "unreviewed",
                "title": "群星串烧",
                "quality_score": 300,
                "engagement_tier_rank": 2,
            },
            {
                "aweme_id": "reviewed",
                "title": "杨紫综艺现场",
                "quality_score": 100,
                "engagement_tier_rank": 2,
            },
        ]
        report = {"items": [{"aweme_id": "reviewed", "editor_score": 80, "discard": False}]}

        with mock.patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test-key"}), mock.patch.object(
            tikhub, "request_deepseek_json", return_value=report
        ):
            result = tikhub.deepseek_candidate_review(args, selected, {"terms": [], "items": []}, {})

        self.assertEqual([item["aweme_id"] for item in result], ["reviewed", "unreviewed"])


class CelebrityDiversityTests(unittest.TestCase):
    def test_third_video_for_same_celebrity_is_skipped(self) -> None:
        candidates = [
            {"aweme_id": "1", "title": "王一博舞台", "quality_score": 100},
            {"aweme_id": "2", "title": "王一博采访", "quality_score": 90},
            {"aweme_id": "3", "title": "王一博红毯", "quality_score": 80},
            {"aweme_id": "4", "title": "肖战舞台", "quality_score": 70},
        ]
        run_info: dict = {}

        selected = tikhub.diversify_candidates(candidates, max_per_celebrity=2, run_info=run_info)

        self.assertEqual([item["aweme_id"] for item in selected], ["1", "2", "4"])
        self.assertEqual(run_info["celebrity_diversity"]["celebrity_counts"]["王一博"], 2)
        self.assertEqual(run_info["celebrity_diversity"]["skipped_count"], 1)

    def test_deepseek_primary_celebrity_supports_names_outside_static_list(self) -> None:
        candidates = [
            {"aweme_id": str(index), "title": "综艺片段", "primary_celebrities": ["颜安"]}
            for index in range(1, 4)
        ]

        selected = tikhub.diversify_candidates(candidates, max_per_celebrity=2)

        self.assertEqual([item["aweme_id"] for item in selected], ["1", "2"])

    def test_short_show_title_is_not_mistaken_for_a_celebrity(self) -> None:
        candidates = [
            {"aweme_id": str(index), "title": "影视片段", "verified_entities": ["赴山海"]}
            for index in range(1, 4)
        ]

        selected = tikhub.diversify_candidates(candidates, max_per_celebrity=2)

        self.assertEqual([item["aweme_id"] for item in selected], ["1", "2", "3"])


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
                                "published_date": dt.datetime.now(
                                    dt.timezone(dt.timedelta(hours=8))
                                ).date().isoformat(),
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

    def test_tavily_uses_second_hot_query_when_first_query_has_no_entertainment_result(self) -> None:
        client = FakeClient(
            [
                httpx.Response(
                    200,
                    request=httpx.Request("POST", tikhub.TAVILY_SEARCH_URL),
                    json={"results": [{"title": "财经新闻", "content": "市场行情"}], "usage": {"credits": 1}},
                ),
                httpx.Response(
                    200,
                    request=httpx.Request("POST", tikhub.TAVILY_SEARCH_URL),
                    json={
                        "results": [{"title": "杨紫新剧热议", "content": "演员片段登上热搜"}],
                        "usage": {"credits": 1},
                    },
                ),
            ]
        )
        context: dict = {"errors": [], "sources": []}

        with mock.patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
            items = tikhub.fetch_tavily_context(client, 10, context)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(len(items), 1)
        self.assertEqual(context["tavily_usage"]["credits"], 2)
        self.assertEqual(context["tavily_usage"]["request_count"], 2)

    def test_world_cup_story_is_not_mistaken_for_chinese_entertainment(self) -> None:
        result = {
            "title": "2026年世界杯",
            "content": "美国歌手和明星球员出席开幕式，总统也将到场。",
        }

        self.assertFalse(tikhub.tavily_entertainment_result(result))

    def test_stale_hourly_report_and_static_topic_page_are_rejected(self) -> None:
        now = dt.datetime(2026, 7, 28, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        self.assertFalse(
            tikhub.tavily_recent_result(
                {"title": "新浪明星热点小时报丨2026年07月27日13时"},
                now=now,
            )
        )
        self.assertFalse(
            tikhub.tavily_recent_result(
                {"title": "胡歌 - 最新胡歌实时滚动快讯，聚合所有胡歌热门新闻"},
                now=now,
            )
        )

    def test_current_hourly_report_is_accepted(self) -> None:
        now = dt.datetime(2026, 7, 28, 17, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))

        self.assertTrue(
            tikhub.tavily_recent_result(
                {"title": "新浪明星热点小时报丨2026年07月28日13时"},
                now=now,
            )
        )

    def test_tavily_headline_entities_rank_before_names_buried_in_snippets(self) -> None:
        items = [
            {"title": "最新孟子义实时滚动快讯", "snippet": "杨紫旧闻回顾"},
            {"title": "何与- 最新何与实时滚动快讯", "snippet": ""},
            {"title": "聚合所有张真源相关热门新闻快讯", "snippet": ""},
            {"title": "《地球超新鲜2》新老嘉宾混搭", "snippet": ""},
        ]

        terms = tikhub.extract_hot_terms(items)

        self.assertEqual(terms[:4], ["孟子义", "何与", "张真源", "地球超新鲜2"])


if __name__ == "__main__":
    unittest.main()
