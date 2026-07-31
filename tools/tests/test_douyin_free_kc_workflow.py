from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "douyin-free-kc-daily.yml"


class DouyinFreeKcWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_partial_outputs_are_packaged_and_merged_before_delivery(self) -> None:
        self.assertEqual(self.workflow.count("--min-selected-videos 1"), 2)
        self.assertIn("fallback_limit=$((KC_LIMIT - primary_count))", self.workflow)
        self.assertIn("--prepend-outputs-file work/tikhub_primary_outputs.txt", self.workflow)

    def test_delivery_threshold_uses_actual_selected_count(self) -> None:
        self.assertGreaterEqual(self.workflow.count('get("selected_count", 0)'), 3)
        self.assertIn("if (( verified >= expected )); then", self.workflow)
        self.assertIn('"$verified" -lt "$expected"', self.workflow)
        self.assertIn("verified={verified}, required={expected}", self.workflow)
        self.assertNotIn("verified >= KC_LIMIT", self.workflow)
        self.assertNotIn('"$verified" -lt "$KC_LIMIT"', self.workflow)


if __name__ == "__main__":
    unittest.main()
