from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[2] / "auto_kc_entertain.py"
SPEC = importlib.util.spec_from_file_location("auto_kc_entertain", MODULE_PATH)
assert SPEC and SPEC.loader
auto_kc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(auto_kc)


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def source_analysis() -> dict:
    return {
        "stem": "01_123",
        "source_metadata": {
            "title": "王一博五年前的击鼓舞台再次被翻出",
            "known_entities": ["王一博"],
            "verified_entities": ["王一博"],
        },
        "transcript_text": "这是王一博在电影周闭幕式上的击鼓舞台",
        "visual_text": {"text": "中华力量 王一博"},
        "fact_check_evidence": {"items": []},
    }


class TitleAccuracyTests(unittest.TestCase):
    def test_supported_title_and_exact_evidence_pass(self) -> None:
        plan = {
            "title_anchor": "王一博",
            "title_evidence": ["王一博五年前的击鼓舞台"],
            "title_lines": ["王一博击鼓", "五年后再出圈"],
        }

        self.assertEqual(auto_kc.plan_accuracy_issues(plan, source_analysis()), [])

    def test_unsupported_celebrity_is_rejected(self) -> None:
        plan = {
            "title_anchor": "肖战",
            "title_evidence": ["王一博五年前的击鼓舞台"],
            "title_lines": ["肖战击鼓", "五年后再出圈"],
        }

        issues = auto_kc.plan_accuracy_issues(plan, source_analysis())

        self.assertTrue(any("unsupported" in issue for issue in issues))

    def test_generic_reversal_claim_needs_source_support(self) -> None:
        plan = {
            "title_anchor": "王一博",
            "title_evidence": ["王一博五年前的击鼓舞台"],
            "title_lines": ["王一博这次", "反差太大"],
        }

        issues = auto_kc.plan_accuracy_issues(plan, source_analysis())

        self.assertIn("title claim lacks source support: 反差", issues)

    def test_cloud_quality_mode_uses_polished_transcript_as_subtitles(self) -> None:
        transcript = [
            {"start": 0.0, "end": 2.0, "text": "王一博来到电影周闭幕式"},
            {"start": 2.0, "end": 4.0, "text": "再次表演中华鼓"},
        ]
        plan = {
            "title_anchor": "王一博",
            "title_evidence": ["王一博来到电影周闭幕式"],
            "title_lines": ["王一博击鼓", "舞台再被翻出"],
            "title_highlights": ["王一博", "击鼓"],
            "subtitles": [{"start": 0, "end": 4, "zh": "模型自由改写的台词"}],
        }

        normalized = auto_kc.normalize_plan(
            plan,
            "01_123",
            transcript,
            4.0,
            source_analysis()["source_metadata"],
            use_faithful_transcript=True,
            quality_report={"transcript_polished": True},
        )

        subtitle_text = " ".join(item["zh"] for item in normalized["subtitles"])
        self.assertIn("王一博来到电影周闭幕式", subtitle_text)
        self.assertNotIn("模型自由改写", subtitle_text)
        self.assertEqual(normalized["subtitle_source"], "deepseek_polished_whisper")
        self.assertIn("王一博击鼓舞台再被翻出", normalized["output_name"])

    def test_rejected_audit_without_correction_cannot_pass_silently(self) -> None:
        plan = {
            "title_anchor": "王一博",
            "title_evidence": ["王一博五年前的击鼓舞台"],
            "title_lines": ["王一博击鼓", "五年后再出圈"],
        }

        returned_plan, issues = auto_kc.apply_title_audit(
            plan,
            {"valid": False, "issues": ["第二行结论缺少直接证据"], "corrected_plan": {}},
        )

        self.assertIs(returned_plan, plan)
        self.assertEqual(issues, ["第二行结论缺少直接证据"])


class TavilyFactCheckTests(unittest.TestCase):
    def test_one_tavily_request_supplies_per_video_fact_evidence(self) -> None:
        captured = {}

        def opener(request: object, *, timeout: int) -> FakeResponse:
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse(
                {
                    "results": [
                        {
                            "title": "王一博击鼓舞台再被翻出",
                            "content": "该舞台来自电影周闭幕式。",
                            "url": "https://news.example/item",
                            "published_date": "2026-07-26",
                        }
                    ],
                    "usage": {"credits": 1},
                    "request_id": "req-test",
                    "response_time": 0.5,
                }
            )

        with mock.patch.dict(os.environ, {"TAVILY_API_KEY": "test-key"}):
            items, usage = auto_kc.search_tavily_fact_evidence(["王一博 击鼓舞台"], opener=opener)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["engine"], "tavily")
        self.assertEqual(usage["credits"], 1)
        self.assertEqual(captured["payload"]["time_range"], "month")
        self.assertEqual(captured["payload"]["search_depth"], "basic")
        self.assertEqual(captured["timeout"], 30)

    def test_tavily_results_avoid_slow_legacy_search(self) -> None:
        tavily_result = {
            "engine": "tavily",
            "query": "王一博 击鼓舞台",
            "title": "王一博击鼓舞台",
            "snippet": "电影周闭幕式",
            "url": "https://news.example/item",
        }
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            auto_kc,
            "search_tavily_fact_evidence",
            return_value=([tavily_result], {"configured": True, "request_count": 1, "credits": 1}),
        ), mock.patch.object(auto_kc, "search_fact_evidence") as legacy_search:
            evidence = auto_kc.collect_fact_check_evidence(
                Path("01_123.mp4"),
                [{"start": 0.0, "end": 2.0, "text": "王一博击鼓"}],
                {"text": "中华力量"},
                Path(temp_dir),
                source_analysis()["source_metadata"],
            )

        legacy_search.assert_not_called()
        self.assertTrue(evidence["available"])
        self.assertEqual(evidence["items"][0]["engine"], "tavily")


if __name__ == "__main__":
    unittest.main()
