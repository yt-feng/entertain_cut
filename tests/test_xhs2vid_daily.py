from __future__ import annotations

import importlib.util
from datetime import datetime
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import httpx


ROOT = Path(__file__).resolve().parents[1]
XHS = ROOT / "xhs2vid"
sys.path.insert(0, str(XHS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


discover = load_module("xhs_discover_test", XHS / "discover_note.py")
fetch = load_module("xhs_fetch_test", XHS / "fetch_assets.py")
batch = load_module("xhs_batch_test", XHS / "run_daily_batch.py")
renderer = load_module("xhs_renderer_test", XHS / "render_video.py")
recorder = load_module("xhs_recorder_test", XHS / "record_processed.py")
delivery_state = load_module("xhs_delivery_state_test", XHS / "record_delivery_state.py")
import prepare_resume as resume  # noqa: E402
import workflow_support as workflow  # noqa: E402


class XhsDailyTests(unittest.TestCase):
    def test_decimal_wan_counts_and_millisecond_timestamps(self) -> None:
        self.assertEqual(discover.parse_count("1.2万"), 12_000)
        self.assertEqual(fetch.parse_like_count("3.45w+"), 34_500)
        self.assertEqual(
            discover.normalize_timestamp("1788134400000"),
            1_788_134_400,
        )

    def test_author_lookup_pool_filters_low_likes_before_slot_limit(self) -> None:
        fresh = [
            {"note_id": "same-day-low", "liked_count": 11},
            {"note_id": "same-day-almost", "liked_count": 199},
            {"note_id": "recent-viral-a", "liked_count": 4_034},
            {"note_id": "recent-viral-b", "liked_count": 378},
        ]
        pool = discover.author_lookup_pool(fresh)
        self.assertEqual(
            [note["note_id"] for note in pool],
            ["recent-viral-a", "recent-viral-b"],
        )

    def test_tikhub_402_opens_one_run_wide_circuit_without_retries(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            budget = discover.TikHubRequestBudget(Path(temporary) / "budget.json", limit=10)
            response = Mock()
            response.raise_for_status.side_effect = httpx.HTTPStatusError(
                "payment required",
                request=Mock(),
                response=Mock(status_code=402),
            )
            old_budget, old_attempts, old_blocked = (
                discover.BUDGET,
                discover.MAX_ATTEMPTS,
                discover.ACCESS_BLOCKED,
            )
            try:
                discover.BUDGET = budget
                discover.MAX_ATTEMPTS = 3
                discover.ACCESS_BLOCKED = None
                with patch.object(discover.client, "get", return_value=response) as request:
                    with self.assertRaises(discover.TikHubAccessBlocked):
                        discover.api_get("/api/v1/xiaohongshu/app_v2/get_user_info", {})
                    self.assertEqual(request.call_count, 1)
                self.assertEqual(budget.snapshot()["used"], 1)
                self.assertEqual(discover.ACCESS_BLOCKED.status_code, 402)
            finally:
                discover.BUDGET = old_budget
                discover.MAX_ATTEMPTS = old_attempts
                discover.ACCESS_BLOCKED = old_blocked

    def test_deferred_discovery_writes_machine_readable_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            old_output, old_blocked = discover.OUT_DIR, discover.ACCESS_BLOCKED
            try:
                discover.OUT_DIR = Path(temporary)
                discover.ACCESS_BLOCKED = None
                payload = discover.write_discovery_status(
                    "deferred",
                    "tikhub_author_lookup_unavailable",
                    message="HTTP 402",
                    candidate_count=1,
                )
                self.assertEqual(payload["status"], "deferred")
                self.assertEqual(
                    json.loads((Path(temporary) / "discovery_status.json").read_text())["reason"],
                    "tikhub_author_lookup_unavailable",
                )
                self.assertEqual(json.loads((Path(temporary) / "selected_notes.json").read_text()), [])
            finally:
                discover.OUT_DIR, discover.ACCESS_BLOCKED = old_output, old_blocked

    def test_delivery_state_replaces_same_business_date(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_path = Path(temporary) / "daily_delivery_state.json"
            delivery_state.update_state(
                state_path,
                "2026-09-22",
                "deferred",
                reason="tikhub_access_blocked",
            )
            delivery_state.update_state(
                state_path,
                "2026-09-22",
                "delivered",
                reason="verified_upload",
                target=5,
                succeeded=5,
            )
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["items"]), 1)
            self.assertEqual(payload["items"][0]["status"], "delivered")

    def test_batch_turns_expected_discovery_defer_into_successful_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            old_root, old_script_dir = batch.ROOT, batch.SCRIPT_DIR
            old_argv = sys.argv
            try:
                batch.ROOT = root
                batch.SCRIPT_DIR = root / "xhs2vid"
                batch.SCRIPT_DIR.mkdir()
                processed = root / "processed.json"
                processed.write_text(json.dumps({"items": []}), encoding="utf-8")
                sys.argv = [
                    "run_daily_batch.py",
                    "--limit", "1",
                    "--date", "2026-09-22",
                    "--work-root", str(root / "work"),
                    "--output-dir", str(root / "output"),
                    "--processed-manifest", str(processed),
                    "--no-hot-context",
                    "--avatar-provider", "local",
                    "--request-limit", "10",
                ]

                def fake_run(command: list[str], *, cwd: Path = batch.ROOT) -> None:
                    discovery_dir = Path(command[2])
                    discovery_dir.mkdir(parents=True, exist_ok=True)
                    (discovery_dir / "discovery_status.json").write_text(
                        json.dumps(
                            {
                                "status": "deferred",
                                "reason": "tikhub_access_blocked",
                                "message": "HTTP 402",
                            }
                        ),
                        encoding="utf-8",
                    )

                with patch.object(batch, "run", side_effect=fake_run):
                    batch.main()
                status = json.loads((root / "output" / "delivery_status.json").read_text())
                self.assertEqual(status["status"], "deferred")
                self.assertEqual(status["reason"], "tikhub_access_blocked")
                self.assertFalse(list((root / "output").glob("*.mp4")))
            finally:
                batch.ROOT, batch.SCRIPT_DIR = old_root, old_script_dir
                sys.argv = old_argv

    def test_resume_batch_accepts_page_two_and_output_five(self) -> None:
        old_argv = sys.argv
        try:
            sys.argv = [
                "run_daily_batch.py",
                "--limit", "1",
                "--pages", "2",
                "--start-index", "5",
                "--max-attempts", "1",
                "--request-limit", "59",
            ]
            args = batch.parse_args()
        finally:
            sys.argv = old_argv
        self.assertEqual(args.limit, 1)
        self.assertEqual(args.pages, 2)
        self.assertEqual(args.start_index, 5)
        self.assertEqual(args.max_attempts, 1)
        self.assertEqual(args.request_limit, 59)

    def test_voice_roster_is_unique_and_monkey_is_fast(self) -> None:
        comments = [
            {"sub_comments": [{"text": "a"}]},
            {"sub_comments": [{"text": "b"}]},
            {"sub_comments": []},
        ]
        cli, manifest = batch.voice_arguments(comments, 1)
        self.assertEqual(len(manifest), 6)
        self.assertEqual(len({item["speaker_id"] for item in manifest}), 6)
        monkey = next(item for item in manifest if item["name"] == "猴哥")
        self.assertEqual(monkey["tempo"], 1.18)
        self.assertEqual(cli.count("--segment-speaker"), 6)
        self.assertEqual(cli.count("--segment-tempo"), 6)

    def test_hot_context_can_add_segment_without_exhausting_voice_roster(self) -> None:
        comments = [
            {"sub_comments": [{"text": "a"}]},
            {"sub_comments": [{"text": "b"}]},
            {"sub_comments": [{"text": "c"}]},
        ]
        cli, manifest = batch.voice_arguments(comments, 1, ["某热搜"])
        self.assertEqual(len(manifest), 8)
        self.assertEqual(cli.count("--segment-speaker"), 8)
        self.assertEqual(cli.count("--segment-tempo"), 8)

    def test_hot_search_keywords_keep_broad_lowfan_queries(self) -> None:
        keywords = batch.hot_search_keywords(
            {"terms": ["当日热搜", "某综艺", "某明星", "某红毯", "额外"]},
            batch.DAILY_KEYWORDS,
        )
        self.assertEqual(len(keywords), 8)
        self.assertTrue(keywords[0].startswith("当日热搜 娱乐"))
        self.assertTrue(any(keyword == "日常 离谱" for keyword in keywords))

    def test_title_wrap_preserves_neighbor_word_and_highlight(self) -> None:
        self.assertEqual(
            renderer.split_header_title("隔壁的邻居好奇怪", "好奇怪"),
            ["隔壁的邻居", "好奇怪"],
        )
        self.assertEqual(renderer.pick_highlights("隔壁的邻居好奇怪"), ["好奇怪"])
        self.assertEqual(
            renderer.pick_highlights("不接受单休 就这样被hr说教…."),
            ["被hr说教"],
        )

    def test_split_pages_keeps_sentence_punctuation_and_no_padding_spaces(self) -> None:
        pages = renderer.split_pages("这句话要完整说完，然后再到下一句！最后还有一句。", limit=18)
        self.assertEqual(pages, ["这句话要完整说完，然后再到下一句！", "最后还有一句。"])
        self.assertTrue(all(" " not in page for page in pages))

    def test_record_processed_merges_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "state.json"
            incoming = root / "new.json"
            current.write_text(
                json.dumps({"version": 1, "items": [{"note_id": "old"}]}),
                encoding="utf-8",
            )
            incoming.write_text(
                json.dumps(
                    {
                        "date": "2026-09-05",
                        "items": [
                            {"note_id": "old", "title": "updated"},
                            {"note_id": "new", "title": "new"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            old_argv = sys.argv
            try:
                sys.argv = [
                    "record_processed.py",
                    "--new", str(incoming),
                    "--manifest", str(current),
                ]
                recorder.main()
            finally:
                sys.argv = old_argv
            payload = json.loads(current.read_text(encoding="utf-8"))
            self.assertEqual({item["note_id"] for item in payload["items"]}, {"old", "new"})
            self.assertEqual(len(payload["items"]), 2)
            new_item = next(item for item in payload["items"] if item["note_id"] == "new")
            self.assertEqual(new_item["delivery_date"], "2026-09-05")

    def test_delayed_schedule_and_compensation_share_business_date(self) -> None:
        primary_delayed = datetime.fromisoformat("2026-09-04T16:33:06+00:00")
        compensation = datetime.fromisoformat("2026-09-06T00:30:00+00:00")

        self.assertEqual(
            workflow.resolve_output_date(
                "schedule",
                schedule_expression="30 12 * * *",
                now=primary_delayed,
            ),
            "2026-09-04",
        )
        self.assertEqual(
            workflow.resolve_output_date(
                "schedule",
                schedule_expression="30 20 * * *",
                now=compensation,
            ),
            "2026-09-05",
        )

        self.assertEqual(
            workflow.resolve_output_date(
                "workflow_dispatch",
                "2026-09-05",
                now=compensation,
            ),
            "2026-09-05",
        )

    def test_resume_quality_does_not_lower_the_original_gates(self) -> None:
        good = {"author_fans": 20_000, "liked_count": 200, "comments_count": 3}
        self.assertTrue(resume.verified_quality(good))
        for change in ({"author_fans": 20_001}, {"author_fans": -1},
                       {"liked_count": 199}, {"comments_count": 2}):
            self.assertFalse(resume.verified_quality({**good, **change}))

    def test_historical_recovery_cannot_generate_current_posts(self) -> None:
        now = datetime.fromisoformat("2026-09-06T00:30:00+00:00")
        self.assertEqual(workflow.require_recent_discovery_date("2026-09-05", now=now), "2026-09-05")
        with self.assertRaisesRegex(ValueError, "existing artifacts"):
            workflow.require_recent_discovery_date("2026-09-01", now=now)

    def test_resume_merges_unique_artifacts_and_survives_another_failed_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            resume_root = root / "resume"
            output_dir = root / "output"
            artifact_date = "2026-09-05"

            def write_artifact(run_id: str, note_ids: list[str], used: int) -> None:
                dated = resume_root / run_id / "outputs" / "xhs_lowfan" / artifact_date
                dated.mkdir(parents=True)
                items = []
                for index, note_id in enumerate(note_ids, start=1):
                    output = f"{index:02d}_{note_id}.mp4"
                    (dated / output).write_bytes(f"video-{note_id}".encode())
                    items.append({"note_id": note_id, "output": output})
                (dated / "new_processed.json").write_text(
                    json.dumps({"date": artifact_date, "items": items}),
                    encoding="utf-8",
                )
                (dated / "daily_summary.json").write_text(
                    json.dumps(
                        {
                            "date": artifact_date,
                            "items": [
                                {
                                    **item,
                                    "status": "success",
                                    "author_fans": 100,
                                    "liked_count": 300,
                                    "comments_count": 10,
                                }
                                for item in items
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                (dated / "tikhub_request_budget.json").write_text(
                    json.dumps({"used": used, "limit": 90}),
                    encoding="utf-8",
                )

            write_artifact("33895770829", ["a", "b", "c", "d"], 40)
            write_artifact("33974893983", ["e", "f", "g"], 38)

            summary = resume.merge_artifacts(
                resume_root,
                artifact_date,
                output_dir,
                5,
            )

            self.assertTrue(summary["target_met"])
            self.assertEqual(summary["resumed_count"], 5)
            self.assertEqual(summary["contributing_run_ids"], ["33974893983", "33895770829"])
            self.assertEqual(summary["prior_tikhub_requests"], 78)
            manifest = json.loads((output_dir / "resume_processed.json").read_text())
            self.assertEqual([item["note_id"] for item in manifest["items"]], ["e", "f", "g", "a", "b"])
            self.assertEqual(
                [item["output"] for item in manifest["items"]],
                ["01_e.mp4", "02_f.mp4", "03_g.mp4", "04_a.mp4", "05_b.mp4"],
            )
            self.assertEqual(len(list(output_dir.glob("*.mp4"))), 5)

            # The upload-only recovery can fail before final delivery. Its
            # artifact must be independently reusable, with every ancestor's
            # budget counted just once when original artifacts also remain.
            second_run = resume_root / "34000000000" / "outputs" / "xhs_lowfan" / artifact_date
            second_run.parent.mkdir(parents=True)
            output_dir.rename(second_run)
            (second_run / "tikhub_request_budget.json").write_text(
                json.dumps({"used": 4, "limit": 21}), encoding="utf-8"
            )
            second_summary = resume.merge_artifacts(
                resume_root, artifact_date, root / "second_output", 5,
            )
            self.assertEqual(second_summary["resumed_count"], 5)
            self.assertEqual(second_summary["prior_tikhub_requests"], 82)
            self.assertEqual(second_summary["contributing_run_ids"], ["34000000000"])

            # A zero-video attempt still consumes the same daily budget.
            write_artifact("34000000001", [], 18)
            with self.assertRaisesRegex(ValueError, "100 TikHub requests"):
                resume.merge_artifacts(
                    resume_root, artifact_date, root / "over_budget", 5,
                )
            self.assertFalse(list((root / "over_budget").glob("*.mp4")))

    def test_synthetic_sfx_fallback_is_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_render = renderer.RENDER
            original_sample = renderer.SAMPLE
            try:
                renderer.RENDER = Path(temporary)
                renderer.SAMPLE = Path(temporary) / "missing-reference.mp4"
                fire, camera = renderer.extract_reference_sfx()
                for path, minimum_frames in ((fire, 30_000), (camera, 10_000)):
                    self.assertTrue(path.is_file())
                    with wave.open(str(path), "rb") as handle:
                        self.assertEqual(handle.getnchannels(), 2)
                        self.assertEqual(handle.getframerate(), 44_100)
                        self.assertGreater(handle.getnframes(), minimum_frames)
            finally:
                renderer.RENDER = original_render
                renderer.SAMPLE = original_sample


if __name__ == "__main__":
    unittest.main()
