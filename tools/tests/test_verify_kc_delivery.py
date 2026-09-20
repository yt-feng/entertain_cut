from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "verify_kc_delivery.py"


class VerifyKcDeliveryCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output_dir = self.root / "output"
        self.output_dir.mkdir()
        self.delivery_path = self.root / "delivery.json"
        self.publish_path = self.root / "publish.json"

    def make_delivery(self, count: int) -> dict:
        paths = [self.output_dir / f"video-{index}.mp4" for index in range(count)]
        for path in paths:
            path.write_bytes(b"video")
        self.delivery_path.write_text(json.dumps({
            "selected_count": count,
            "selected_files": [str(path) for path in paths],
            "deliverable": count >= 3,
            "minimum_met": count >= 3,
        }), encoding="utf-8")
        return {
            "remote_directory": "/kc/test",
            "files": [{
                "name": path.name,
                "webdav": {"success": True, "remote_verified": True},
                "git_ready": True,
            } for path in paths],
        }

    def write_publish(self, publish: dict) -> None:
        self.publish_path.write_text(json.dumps(publish), encoding="utf-8")

    def run_cli(self) -> tuple[int, dict]:
        process = subprocess.run([
            sys.executable, str(MODULE_PATH),
            "--delivery-summary", str(self.delivery_path),
            "--publish-summary", str(self.publish_path),
            "--output-dir", str(self.output_dir),
            "--limit", "5", "--min-delivery", "3", "--git-max-bytes", "100",
        ], capture_output=True, text=True, check=False)
        self.assertEqual(process.stderr, "")
        return process.returncode, json.loads(process.stdout)

    def test_shortage_is_first_when_publication_was_skipped(self) -> None:
        for count in (0, 1, 2):
            with self.subTest(count=count):
                self.make_delivery(count)
                returncode, report = self.run_cli()

                self.assertEqual(returncode, 2)
                self.assertFalse(report["complete"])
                self.assertFalse(report["minimum_met"])
                self.assertEqual(report["selected_count"], count)
                self.assertEqual(report["errors"][0],
                                 f"Delivery minimum not met: selected={count}, minimum=3, target=5")
                self.assertTrue(report["errors"][1].startswith("Publisher evidence unavailable:"))

    def test_missing_publication_fails_at_viable_count(self) -> None:
        self.make_delivery(3)
        returncode, report = self.run_cli()

        self.assertEqual(returncode, 2)
        self.assertFalse(report["complete"])
        self.assertTrue(report["minimum_met"])
        self.assertTrue(report["errors"][0].startswith("Publisher evidence unavailable:"))
        self.assertEqual(report["webdav_verified_count"], 0)
        self.assertEqual(report["git_ready_count"], 0)

    def test_corrupt_or_non_object_publisher_json_fails_with_json_diagnostics(self) -> None:
        self.make_delivery(3)
        for content in ('{"files":', '[]', 'null'):
            with self.subTest(content=content):
                self.publish_path.write_text(content, encoding="utf-8")
                returncode, report = self.run_cli()

                self.assertEqual(returncode, 2)
                self.assertFalse(report["complete"])
                self.assertTrue(report["minimum_met"])
                self.assertTrue(report["errors"][0].startswith("Publisher evidence unavailable:"))

    def test_corrupt_delivery_json_fails_with_json_diagnostics(self) -> None:
        self.delivery_path.write_text('{"selected_count":', encoding="utf-8")
        returncode, report = self.run_cli()

        self.assertEqual(returncode, 2)
        self.assertFalse(report["complete"])
        self.assertTrue(report["errors"][0].startswith("Delivery evidence unavailable:"))

    def test_complete_evidence_accepts_minimum_and_target(self) -> None:
        for count in (3, 5):
            with self.subTest(count=count):
                self.write_publish(self.make_delivery(count))
                returncode, report = self.run_cli()

                self.assertEqual(returncode, 0)
                self.assertTrue(report["complete"])
                self.assertTrue(report["minimum_met"])
                self.assertEqual(report["target_met"], count == 5)
                self.assertEqual(report["webdav_verified_count"], count)
                self.assertEqual(report["git_ready_count"], count)
                self.assertEqual(report["errors"], [])

    def test_every_selected_file_requires_remote_and_git_evidence(self) -> None:
        for index in range(3):
            for missing in ("webdav", "git_ready"):
                with self.subTest(index=index, missing=missing):
                    publish = self.make_delivery(3)
                    del publish["files"][index][missing]
                    self.write_publish(publish)
                    returncode, report = self.run_cli()

                    self.assertEqual(returncode, 2)
                    self.assertFalse(report["complete"])
                    self.assertTrue(any(f"video-{index}.mp4" in error for error in report["errors"]))
                    self.assertEqual(report["webdav_verified_count"], 2 if missing == "webdav" else 3)
                    self.assertEqual(report["git_ready_count"], 2 if missing == "git_ready" else 3)


if __name__ == "__main__":
    unittest.main()
