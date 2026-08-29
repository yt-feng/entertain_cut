from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "prepare_kc_delivery.py"
SPEC = importlib.util.spec_from_file_location("prepare_kc_delivery", MODULE_PATH)
assert SPEC and SPEC.loader
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class PrepareKcDeliveryTests(unittest.TestCase):
    def test_cli_preserves_partial_output_for_artifact_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            video = output_dir / "current.mp4"
            video.write_bytes(b"current")
            outputs_file = root / "outputs.txt"
            outputs_file.write_text(f"{video}\n", encoding="utf-8")
            summary_file = root / "summary.json"
            argv = [
                "prepare_kc_delivery.py",
                "--output-dir",
                str(output_dir),
                "--outputs-file",
                str(outputs_file),
                "--summary-file",
                str(summary_file),
                "--limit",
                "5",
            ]

            with mock.patch.object(sys, "argv", argv):
                result = delivery.main()

            report = json.loads(summary_file.read_text(encoding="utf-8"))
            self.assertEqual(result, 2)
            self.assertTrue(report["artifact_ready"])
            self.assertFalse(report["deliverable"])
            self.assertFalse(report["target_met"])
            self.assertEqual(report["status"], "partial_artifact")
            self.assertTrue(video.exists())
            self.assertEqual(outputs_file.read_text(encoding="utf-8").splitlines(), [str(video.resolve())])

    def test_missing_current_videos_are_not_filled_from_elsewhere(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            current = []
            for index in range(2):
                path = output_dir / f"current-{index}.mp4"
                path.write_bytes(bytes([index + 1]))
                current.append(path)
            outputs_file = root / "work" / "outputs.txt"
            outputs_file.parent.mkdir()
            outputs_file.write_text("".join(f"{path}\n" for path in current), encoding="utf-8")

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                outputs_file=outputs_file,
                limit=5,
            )

            self.assertFalse(report["ready"])
            self.assertTrue(report["artifact_ready"])
            self.assertFalse(report["deliverable"])
            self.assertFalse(report["target_met"])
            self.assertEqual(report["status"], "partial_artifact")
            self.assertEqual(report["selected_count"], 2)
            self.assertFalse(report["automatic_history_fallback"])
            self.assertEqual(len(delivery.root_videos(output_dir)), 2)

    def test_empty_current_run_stays_empty(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            outputs_file = root / "work" / "outputs.txt"

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                outputs_file=outputs_file,
                limit=5,
            )

            self.assertFalse(report["ready"])
            self.assertFalse(report["artifact_ready"])
            self.assertFalse(report["deliverable"])
            self.assertFalse(report["target_met"])
            self.assertEqual(report["selected_count"], 0)
            self.assertEqual(outputs_file.read_text(encoding="utf-8"), "")

    def test_unlisted_previous_run_files_do_not_fill_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            current = output_dir / "current.mp4"
            previous = output_dir / "previous.mp4"
            current.write_bytes(b"current")
            previous.write_bytes(b"previous")
            outputs_file = root / "outputs.txt"
            outputs_file.write_text(f"{current}\n", encoding="utf-8")

            report = delivery.prepare_delivery(output_dir=output_dir, outputs_file=outputs_file, limit=2)

            self.assertFalse(report["ready"])
            self.assertTrue(report["artifact_ready"])
            self.assertFalse(report["deliverable"])
            self.assertFalse(report["target_met"])
            self.assertEqual(report["selected_count"], 1)
            self.assertFalse(previous.exists())

    def test_prunes_extra_outputs_using_last_run_order(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            videos = []
            for index in range(7):
                path = output_dir / f"video-{index}.mp4"
                path.write_bytes(bytes([index]))
                videos.append(path)
            outputs_file = root / "outputs.txt"
            preferred = [videos[6], videos[5], videos[4], videos[3], videos[2]]
            outputs_file.write_text("".join(f"{path}\n" for path in preferred), encoding="utf-8")

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                outputs_file=outputs_file,
                limit=5,
            )

            self.assertTrue(report["ready"])
            self.assertTrue(report["artifact_ready"])
            self.assertTrue(report["deliverable"])
            self.assertTrue(report["target_met"])
            self.assertEqual({path.name for path in delivery.root_videos(output_dir)}, {path.name for path in preferred})
            self.assertEqual(len(report["removed_extra_files"]), 2)

    def test_primary_outputs_are_kept_before_free_fallback_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            primary = []
            for index in range(4):
                path = output_dir / f"primary-{index}.mp4"
                path.write_bytes(f"primary-{index}".encode())
                primary.append(path)
            fallback = output_dir / "fallback.mp4"
            fallback.write_bytes(b"fallback")
            primary_outputs = root / "primary.txt"
            primary_outputs.write_text("".join(f"{path}\n" for path in primary), encoding="utf-8")
            fallback_outputs = root / "fallback.txt"
            fallback_outputs.write_text(f"{fallback}\n", encoding="utf-8")

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                outputs_file=fallback_outputs,
                prepend_outputs_files=[primary_outputs],
                limit=5,
            )

            self.assertTrue(report["ready"])
            self.assertTrue(report["artifact_ready"])
            self.assertTrue(report["deliverable"])
            self.assertEqual(report["selected_count"], 5)
            self.assertEqual(
                fallback_outputs.read_text(encoding="utf-8").splitlines(),
                [str(path.resolve()) for path in [*primary, fallback]],
            )

    def test_prepend_outputs_are_deduplicated_and_missing_lists_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            output_dir.mkdir()
            first = output_dir / "first.mp4"
            second = output_dir / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            primary_outputs = root / "primary.txt"
            primary_outputs.write_text(f"{first}\n", encoding="utf-8")
            fallback_outputs = root / "fallback.txt"
            fallback_outputs.write_text(f"{first}\n{second}\n", encoding="utf-8")

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                outputs_file=fallback_outputs,
                prepend_outputs_files=[root / "missing.txt", primary_outputs],
                limit=5,
            )

            self.assertFalse(report["ready"])
            self.assertTrue(report["artifact_ready"])
            self.assertFalse(report["deliverable"])
            self.assertEqual(report["selected_count"], 2)
            self.assertEqual(
                fallback_outputs.read_text(encoding="utf-8").splitlines(),
                [str(first.resolve()), str(second.resolve())],
            )


if __name__ == "__main__":
    unittest.main()
