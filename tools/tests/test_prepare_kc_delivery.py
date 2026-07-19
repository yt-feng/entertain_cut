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
    def test_restores_five_videos_and_ignores_oversized_backups(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            fallback_dir = root / "artifact" / "2026-07-18"
            backup_dir = fallback_dir / "oversized-originals"
            fallback_dir.mkdir(parents=True)
            backup_dir.mkdir()
            for index in range(5):
                (fallback_dir / f"video-{index}.mp4").write_bytes(bytes([index + 1]) * (index + 1))
            (backup_dir / "should-not-copy.mp4").write_bytes(b"backup")

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                fallback_dir=root / "artifact",
                outputs_file=root / "work" / "outputs.txt",
                limit=5,
                fallback_run_id="123",
            )

            self.assertTrue(report["ready"])
            self.assertEqual(report["selected_count"], 5)
            self.assertEqual(report["reused_count"], 5)
            self.assertNotIn("should-not-copy.mp4", {path.name for path in output_dir.iterdir()})

    def test_keeps_fresh_videos_and_only_fills_missing_slots(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "output"
            fallback_dir = root / "artifact"
            output_dir.mkdir()
            fallback_dir.mkdir()
            fresh = []
            for index in range(2):
                path = output_dir / f"fresh-{index}.mp4"
                path.write_bytes(b"fresh" + bytes([index]))
                fresh.append(path)
            for index in range(5):
                (fallback_dir / f"fallback-{index}.mp4").write_bytes(b"old" + bytes([index]))
            outputs_file = root / "work" / "outputs.txt"
            outputs_file.parent.mkdir()
            outputs_file.write_text("".join(f"{path}\n" for path in fresh), encoding="utf-8")

            report = delivery.prepare_delivery(
                output_dir=output_dir,
                fallback_dir=fallback_dir,
                outputs_file=outputs_file,
                limit=5,
            )

            self.assertEqual(report["fresh_count"], 2)
            self.assertEqual(report["reused_count"], 3)
            self.assertEqual(len(delivery.root_videos(output_dir)), 5)

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
                fallback_dir=None,
                outputs_file=outputs_file,
                limit=5,
            )

            self.assertTrue(report["ready"])
            self.assertEqual({path.name for path in delivery.root_videos(output_dir)}, {path.name for path in preferred})
            self.assertEqual(len(report["removed_extra_files"]), 2)


if __name__ == "__main__":
    unittest.main()
