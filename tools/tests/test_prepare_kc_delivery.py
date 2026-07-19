from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "prepare_kc_delivery.py"
SPEC = importlib.util.spec_from_file_location("prepare_kc_delivery", MODULE_PATH)
assert SPEC and SPEC.loader
delivery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery)


class PrepareKcDeliveryTests(unittest.TestCase):
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
            self.assertEqual(report["selected_count"], 0)
            self.assertEqual(outputs_file.read_text(encoding="utf-8"), "")

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
            self.assertEqual({path.name for path in delivery.root_videos(output_dir)}, {path.name for path in preferred})
            self.assertEqual(len(report["removed_extra_files"]), 2)


if __name__ == "__main__":
    unittest.main()
