from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "classify_kc_delivery.py"
SPEC = importlib.util.spec_from_file_location("classify_kc_delivery", MODULE_PATH)
assert SPEC and SPEC.loader
classifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(classifier)


class ClassifyKcDeliveryTests(unittest.TestCase):
    def test_shortage_is_deferred_without_hiding_packaging_failure(self) -> None:
        delivery = {
            "status": "partial_artifact",
            "selected_count": 1,
            "input_count": 1,
            "minimum_met": False,
        }
        outcome = classifier.classify_delivery(delivery, [], limit=5, minimum=3)
        self.assertEqual(outcome["status"], "deferred")
        self.assertEqual(outcome["reason"], "insufficient_videos")

        with self.assertRaisesRegex(ValueError, "packaging failed"):
            classifier.classify_delivery(
                delivery,
                [{"kc_packaging_exit_code": 2}],
                limit=5,
                minimum=3,
            )

    def test_minimum_ready_is_not_reclassified_as_shortage(self) -> None:
        outcome = classifier.classify_delivery(
            {"status": "minimum_ready", "selected_count": 3, "minimum_met": True},
            [],
            limit=5,
            minimum=3,
        )
        self.assertEqual(outcome["status"], "ready")
        self.assertEqual(outcome["selected_count"], 3)

    def test_unknown_delivery_status_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unexpected KC delivery evidence"):
            classifier.classify_delivery(
                {"status": "mystery", "selected_count": 4, "minimum_met": True},
                [],
                limit=5,
                minimum=3,
            )

    def test_empty_shortage_without_provider_evidence_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "No provider summary"):
            classifier.classify_delivery(
                {"status": "insufficient_videos", "selected_count": 0, "input_count": 0},
                [],
                limit=5,
                minimum=3,
            )


if __name__ == "__main__":
    unittest.main()
