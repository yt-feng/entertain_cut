from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_douyin_free_kc_daily.py"
SPEC = importlib.util.spec_from_file_location("run_douyin_free_kc_daily", MODULE_PATH)
assert SPEC and SPEC.loader
daily = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(daily)


class SelectedDiversityGuardTests(unittest.TestCase):
    def test_wrapper_removes_third_video_for_same_celebrity(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            run_dir = Path(temp_dir)
            selected_dir = run_dir / "selected"
            reports_dir = run_dir / "reports"
            selected_dir.mkdir()
            reports_dir.mkdir()
            items = [
                {"aweme_id": "100000000000001", "title": "王一博舞台"},
                {"aweme_id": "100000000000002", "title": "王一博采访"},
                {"aweme_id": "100000000000003", "title": "王一博红毯"},
                {"aweme_id": "100000000000004", "title": "肖战舞台"},
            ]
            (reports_dir / "selected.json").write_text(
                json.dumps(items, ensure_ascii=False),
                encoding="utf-8",
            )
            for index, item in enumerate(items, start=1):
                (selected_dir / f"{index:02d}_{item['aweme_id']}.mp4").write_bytes(b"video")
            summary: dict = {}

            daily.enforce_selected_diversity(run_dir, 2, summary)

            remaining_files = sorted(path.name for path in selected_dir.glob("*.mp4"))
            remaining_items = json.loads((reports_dir / "selected.json").read_text(encoding="utf-8"))
            self.assertEqual(len(remaining_files), 3)
            self.assertNotIn("03_100000000000003.mp4", remaining_files)
            self.assertEqual([item["aweme_id"] for item in remaining_items], [
                "100000000000001",
                "100000000000002",
                "100000000000004",
            ])
            self.assertEqual(summary["celebrity_diversity"]["max_videos_per_celebrity"], 2)


class SelectedProcessedGuardTests(unittest.TestCase):
    def test_recovered_and_current_committed_sources_removed_but_pending_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selected = root / "selected"
            reports = root / "reports"
            selected.mkdir()
            reports.mkdir()
            items = [
                {"aweme_id": "123", "title": "Recovered source"},
                {"aweme_id": "456", "platform": "douyin", "title": "Current TikHub success"},
                {"aweme_id": "1234", "title": "Untouched candidate with shared ID prefix"},
                {"aweme_id": "pending", "title": "New candidate pending packaging"},
            ]
            metadata = reports / "selected.json"
            metadata.write_text(json.dumps(items))
            (reports / "run_info.json").write_text(json.dumps({"processed_manifest_pending": {"candidate_count": 4}}))
            names = ["01_123.mp4", "02_douyin_456.mp4", "03_1234.mp4", "04_pending.mp4"]
            for name in names:
                (selected / name).write_bytes(b"video")
            ledger = root / "processed.json"
            ledger.write_text(json.dumps({"items": [
                {"aweme_id": "123", "output_date": "2026-09-20"},
                {"aweme_id": "456", "output_date": "2026-09-20"},
            ]}))
            original_ledger = ledger.read_bytes()
            summary = {}

            daily.exclude_processed_selected(root, ledger, "2026-09-20", summary)

            self.assertEqual([item["aweme_id"] for item in json.loads(metadata.read_text())], ["1234", "pending"])
            self.assertEqual(sorted(path.name for path in selected.glob("*.mp4")), names[2:])
            self.assertEqual(ledger.read_bytes(), original_ledger)
            self.assertEqual(summary["processed_source_filter"]["excluded_ids"], ["123", "456"])
            self.assertEqual(summary["processed_source_filter"]["output_date"], "2026-09-20")

    def test_previous_day_sources_remain_excluded_and_all_rejected_files_are_removed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "selected").mkdir()
            (root / "reports").mkdir()
            (root / "selected/01_douyin_123.mp4").write_bytes(b"video")
            (root / "reports/selected.json").write_text(json.dumps([{"aweme_id": "123"}]))
            ledger = root / "processed.json"
            ledger.write_text(json.dumps({"items": [{"aweme_id": "123", "output_date": "2026-09-19"}]}))

            daily.exclude_processed_selected(root, ledger, "2026-09-20", {})

            self.assertEqual(list((root / "selected").glob("*.mp4")), [])
            self.assertEqual(json.loads((root / "reports/selected.json").read_text()), [])

    def test_missing_committed_ledger_leaves_new_selection_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "reports").mkdir()
            metadata = root / "reports/selected.json"
            metadata.write_text('[{"aweme_id":"pending"}]')
            original = metadata.read_bytes()
            daily.exclude_processed_selected(root, root / "not-committed.json", "2026-09-20", {})
            self.assertEqual(metadata.read_bytes(), original)

    def test_wrapper_applies_filter_before_diversity_for_both_providers(self) -> None:
        for provider in ("free", "tikhub"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_dir = root / "run"
                ledger = root / "processed.json"
                ledger.write_text(json.dumps({"items": [{"aweme_id": "recovered", "output_date": "2026-09-20"}]}))

                def discover(command, summary):
                    (run_dir / "selected").mkdir()
                    (run_dir / "reports").mkdir()
                    (run_dir / "reports/selected.json").write_text(json.dumps([
                        {"aweme_id": "recovered"}, {"aweme_id": "fresh"},
                    ]))
                    (run_dir / "selected/01_recovered.mp4").write_bytes(b"old")
                    (run_dir / "selected/02_fresh.mp4").write_bytes(b"new")

                def diversity(*args):
                    self.assertFalse((run_dir / "selected/01_recovered.mp4").exists())
                    self.assertEqual([item["aweme_id"] for item in json.loads((run_dir / "reports/selected.json").read_text())], ["fresh"])

                argv = [
                    "daily", "--provider", provider, "--search-only", "--run-dir", str(run_dir),
                    "--output-dir", str(root / "outputs"), "--kc-work-dir", str(root / "kc"),
                    "--processed-manifest", str(ledger), "--output-date", "2026-09-20",
                ]
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(daily, "resolve_python", return_value=sys.executable),
                    mock.patch.object(daily, "ensure_downloader"),
                    mock.patch.object(daily, "run", side_effect=discover),
                    mock.patch.object(daily, "enforce_selected_diversity", side_effect=diversity) as guard,
                    mock.patch.object(daily, "write_summary") as write,
                ):
                    self.assertEqual(daily.main(), 0)
                guard.assert_called_once()
                self.assertEqual(write.call_args.args[1]["selected_file_count"], 1)


class PackagingTargetTests(unittest.TestCase):
    def test_deferred_provider_summary_is_mirrored_to_latest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            run_dir = root / "run"
            work_root = root / "work"

            def fake_run(command: list[str], summary: dict) -> None:
                (run_dir / "reports").mkdir(parents=True)
                (run_dir / "reports/selected.json").write_text("[]", encoding="utf-8")

            argv = [
                "daily", "--provider", "tikhub", "--limit", "5",
                "--run-dir", str(run_dir), "--work-root", str(work_root),
                "--output-dir", str(root / "output"), "--kc-work-dir", str(root / "kc"),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(daily, "resolve_python", return_value=sys.executable),
                mock.patch.object(daily, "run", side_effect=fake_run),
                mock.patch.object(daily, "enforce_selected_diversity"),
            ):
                self.assertEqual(daily.main(), 0)

            latest_summary = work_root / "latest" / "kc_daily_summary.json"
            self.assertTrue(latest_summary.exists())
            summary = json.loads(latest_summary.read_text(encoding="utf-8"))
            self.assertEqual(summary["status"], "deferred")
            self.assertEqual(summary["defer_reason"], "no_selected_videos")

    def test_child_failure_preserves_fresh_partial_outputs_but_never_stale_outputs(self) -> None:
        for fresh_count in (0, 3):
            with self.subTest(fresh_count=fresh_count), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                run_dir = root / "run"
                selected_dir = run_dir / "selected"
                selected_dir.mkdir(parents=True)
                for index in range(5):
                    (selected_dir / f"{index}.mp4").write_bytes(b"source")
                kc_work = root / "kc"
                kc_work.mkdir()
                output_dir = root / "outputs"
                output_dir.mkdir()
                stale = output_dir / "old.mp4"
                stale.write_bytes(b"old")
                outputs_list = kc_work / "last_run_outputs.txt"
                outputs_list.write_text(str(stale) + "\n")

                def fake_run(command: list[str], summary: dict) -> None:
                    if Path(command[1]).name != "auto_kc_entertain.py":
                        return
                    self.assertFalse(outputs_list.exists())
                    if fresh_count:
                        fresh_outputs = []
                        for index in range(fresh_count):
                            output = output_dir / f"fresh-{index}.mp4"
                            output.write_bytes(b"new")
                            fresh_outputs.append(str(output))
                        outputs_list.write_text("\n".join(fresh_outputs) + "\n")
                    raise subprocess.CalledProcessError(2, command)

                argv = [
                    "daily", "--provider", "tikhub", "--limit", "5",
                    "--min-selected-videos", "1", "--run-dir", str(run_dir),
                    "--work-root", str(root / "work"), "--output-dir", str(output_dir),
                    "--kc-work-dir", str(kc_work),
                ]
                with (
                    mock.patch.object(sys, "argv", argv),
                    mock.patch.object(daily, "resolve_python", return_value=sys.executable),
                    mock.patch.object(daily, "run", side_effect=fake_run),
                    mock.patch.object(daily, "enforce_selected_diversity"),
                    mock.patch.object(daily, "mirror_latest"),
                    mock.patch.object(daily, "commit_processed_manifest_after_success") as commit,
                    mock.patch.object(daily, "write_summary") as write,
                ):
                    self.assertEqual(daily.main(), 2)
                summary = write.call_args.args[1]
                self.assertEqual(summary["kc_output_count"], fresh_count)
                self.assertNotIn(str(stale), summary["kc_outputs"])
                self.assertEqual(commit.call_count, bool(fresh_count))

    def test_processed_ledger_records_only_rendered_sources_not_unused_reserves(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "reports").mkdir()
            (root / "reports/selected.json").write_text(json.dumps([
                {"aweme_id": "rendered", "title": "明星现场"},
                {"aweme_id": "reserve", "title": "候补素材"},
                {"aweme_id": "failed", "title": "未完成"},
            ]))
            output = root / "rendered.mp4"
            output.write_bytes(b"video")
            missing = root / "missing.mp4"
            manifest = root / "packaging.json"
            manifest.write_text(json.dumps({
                "hash1": {"output": str(output), "source_metadata": {"aweme_id": "rendered"}},
                "hash2": {"output": str(missing), "source_metadata": {"aweme_id": "failed"}},
            }))
            ledger = root / "processed.json"
            daily.commit_processed_manifest_after_success(
                root, ledger, "2026-09-11", {},
                packaging_manifest=manifest, output_paths=[str(output), str(missing)],
            )
            self.assertEqual([item["aweme_id"] for item in json.loads(ledger.read_text())["items"]], ["rendered"])

    def test_packages_all_four_when_daily_target_is_five_and_minimum_is_one(self) -> None:
        self.assertEqual(
            daily.resolve_packaging_target(selected_count=4, limit=5, minimum_selected=1),
            4,
        )

    def test_still_blocks_when_explicit_minimum_is_not_met(self) -> None:
        self.assertEqual(
            daily.resolve_packaging_target(selected_count=4, limit=5, minimum_selected=5),
            0,
        )

    def test_caps_reserve_candidates_at_daily_target(self) -> None:
        self.assertEqual(
            daily.resolve_packaging_target(selected_count=8, limit=5, minimum_selected=1),
            5,
        )

    def test_main_packages_four_available_videos_instead_of_skipping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            run_dir = root / "run"
            output_dir = root / "output"
            kc_work_dir = root / "kc-work"
            commands: list[list[str]] = []

            def fake_run(command: list[str], summary: dict) -> None:
                commands.append(command)
                if Path(command[1]).name == "run_douyin_tikhub_daily.py":
                    selected_dir = run_dir / "selected"
                    reports_dir = run_dir / "reports"
                    selected_dir.mkdir(parents=True)
                    reports_dir.mkdir(parents=True)
                    metadata = []
                    for index in range(4):
                        aweme_id = f"10000000000000{index}"
                        (selected_dir / f"{index + 1:02d}_{aweme_id}.mp4").write_bytes(b"video")
                        metadata.append({"aweme_id": aweme_id, "title": f"明星视频{index}"})
                    (reports_dir / "selected.json").write_text(
                        json.dumps(metadata, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    return

                target_index = command.index("--target-count") + 1
                self.assertEqual(command[target_index], "4")
                output_dir.mkdir(parents=True, exist_ok=True)
                kc_work_dir.mkdir(parents=True, exist_ok=True)
                outputs = []
                for index in range(4):
                    output = output_dir / f"kc-{index}.mp4"
                    output.write_bytes(b"kc")
                    outputs.append(output)
                (kc_work_dir / "last_run_outputs.txt").write_text(
                    "".join(f"{path.resolve()}\n" for path in outputs),
                    encoding="utf-8",
                )

            argv = [
                "run_douyin_free_kc_daily.py",
                "--provider",
                "tikhub",
                "--limit",
                "5",
                "--min-selected-videos",
                "1",
                "--run-dir",
                str(run_dir),
                "--work-root",
                str(root / "work-root"),
                "--output-dir",
                str(output_dir),
                "--kc-work-dir",
                str(kc_work_dir),
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(daily, "resolve_python", return_value=sys.executable),
                mock.patch.object(daily, "run", side_effect=fake_run),
                mock.patch.object(daily, "enforce_selected_diversity"),
                mock.patch.object(daily, "mirror_latest"),
                mock.patch.object(daily, "commit_processed_manifest_after_success") as commit_manifest,
            ):
                result = daily.main()

            self.assertEqual(result, 0)
            self.assertEqual(len(commands), 2)
            commit_manifest.assert_called_once()


if __name__ == "__main__":
    unittest.main()
