from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
