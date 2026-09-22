from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "record_daily_delivery_state.py"
SPEC = importlib.util.spec_from_file_location("record_daily_delivery_state", MODULE_PATH)
assert SPEC and SPEC.loader
state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(state)


class RecordDailyDeliveryStateTests(unittest.TestCase):
    def test_same_date_is_replaced_and_history_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            for index in range(35):
                state.update_state(
                    path,
                    date=f"2026-09-{index + 1:02d}",
                    status="deferred",
                    target=5,
                    succeeded=0,
                    reason="insufficient_videos",
                    message="deferred",
                    run_id=str(index),
                )
            state.update_state(
                path,
                date="2026-09-35",
                status="deferred",
                target=5,
                succeeded=1,
                reason="insufficient_videos",
                message="updated",
                run_id="replacement",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 1)
            self.assertEqual(len(payload["items"]), 30)
            matching = [item for item in payload["items"] if item["date"] == "2026-09-35"]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["succeeded"], 1)
            self.assertEqual(matching[0]["run_id"], "replacement")


if __name__ == "__main__":
    unittest.main()
