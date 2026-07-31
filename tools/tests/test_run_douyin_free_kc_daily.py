from __future__ import annotations

import importlib.util
import json
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


class PackagingTargetTests(unittest.TestCase):
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
