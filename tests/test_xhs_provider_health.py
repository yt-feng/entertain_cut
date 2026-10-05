"""Offline regressions for provider account blocks and persisted compensation."""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

import httpx
import test_xhs2vid_daily as shared

from tikhub_access import deferred_retryable, normalize_access_reason
from tikhub_budget import TikHubRequestBudget


ROOT = Path(__file__).resolve().parents[1]
TRACE = json.loads((ROOT / "tests/fixtures/xhs-provider-block-37244197361.json").read_text())
discover, fetch, batch = shared.discover, shared.fetch, shared.batch


class ProviderBlockTests(unittest.TestCase):
    def test_real_402_and_five_404_trace_stays_payment_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            budget = TikHubRequestBudget(output / "budget.json", limit=82)
            paths = []

            def respond(path, **kwargs):
                event = TRACE["attempts"][len(paths)]
                self.assertEqual(path, event["path"])
                paths.append(path)
                return httpx.Response(event["status"], request=httpx.Request("GET", "https://invalid.test" + path))

            with patch.multiple(discover, BUDGET=budget, RESERVED_REQUESTS=0,
                                MAX_ATTEMPTS=1, ACCESS_BLOCKED=None, ACTIVE_SEARCH_ENDPOINT=None,
                                OUT_DIR=output), patch.object(discover.client, "get", side_effect=respond):
                with self.assertRaises(discover.TikHubAccessBlocked) as caught:
                    discover.search_notes("日常 离谱", 1, "general")
                payload = discover.write_discovery_status(
                    "deferred", "tikhub_access_blocked", message=str(caught.exception),
                )
            self.assertEqual(len(paths), 6)
            self.assertEqual(budget.snapshot()["remaining"], 76)
            self.assertEqual(payload["reason"], "tikhub_payment_required")
            self.assertFalse(payload["retryable"])
            self.assertEqual(payload["tikhub_blocked_status"], 402)

    def test_account_statuses_are_typed_without_repeating_or_exposing_response_body(self):
        for module in (discover, fetch):
            for status, reason, retryable in (
                (401, "tikhub_auth_invalid", False),
                (402, "tikhub_payment_required", False),
                (403, "tikhub_permission_denied", False),
                (429, "tikhub_rate_limited", True),
            ):
                with self.subTest(module=module.__name__, status=status), tempfile.TemporaryDirectory() as temporary:
                    budget = TikHubRequestBudget(Path(temporary) / "budget.json", limit=10)
                    response = httpx.Response(status, text="private-response-must-not-leak",
                                              request=httpx.Request("GET", "https://invalid.test"))
                    with patch.multiple(module, BUDGET=budget, MAX_ATTEMPTS=3, ACCESS_BLOCKED=None), \
                            patch.object(module.client, "get", return_value=response) as request, \
                            patch.object(discover, "RESERVED_REQUESTS", 0):
                        with self.assertRaises(module.TikHubAccessBlocked) as caught:
                            module.api_get("/test", {})
                        self.assertEqual(request.call_count, 1)
                        self.assertEqual(caught.exception.reason, reason)
                        self.assertEqual(caught.exception.retryable, retryable)
                        self.assertNotIn("private-response", str(caught.exception))

    def test_unknown_provider_defect_is_not_account_defer(self):
        for module in (discover, fetch):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as temporary:
                budget = TikHubRequestBudget(Path(temporary) / "budget.json", limit=10)
                response = httpx.Response(500, request=httpx.Request("GET", "https://invalid.test"))
                with patch.multiple(module, BUDGET=budget, MAX_ATTEMPTS=2, ACCESS_BLOCKED=None), \
                        patch.object(discover, "RESERVED_REQUESTS", 0), \
                        patch.object(module.client, "get", return_value=response) as request, \
                        patch.object(module.time, "sleep"):
                    with self.assertRaises(httpx.HTTPStatusError):
                        module.api_get("/test", {})
                    self.assertEqual(request.call_count, 2)
                    self.assertIsNone(module.ACCESS_BLOCKED)

    def test_successful_fallback_clears_prior_account_block(self):
        for module in (discover, fetch):
            with self.subTest(module=module.__name__), tempfile.TemporaryDirectory() as temporary:
                budget = TikHubRequestBudget(Path(temporary) / "budget.json", limit=10)
                stale_block = module.TikHubAccessBlocked(402, "/prior-endpoint")
                response = httpx.Response(200, json={"data": {"items": []}},
                                          request=httpx.Request("GET", "https://invalid.test"))
                with patch.multiple(module, BUDGET=budget, MAX_ATTEMPTS=1, ACCESS_BLOCKED=stale_block), \
                        patch.object(discover, "RESERVED_REQUESTS", 0), \
                        patch.object(module.client, "get", return_value=response):
                    if module is discover:
                        with patch.object(discover, "ACTIVE_SEARCH_ENDPOINT", None):
                            module.search_notes("test", 1, "general")
                    else:
                        with patch.object(fetch, "ACTIVE_COMMENT_ENDPOINT", None):
                            module.fetch_with_endpoint_fallback(module.comment_requests("n1"))
                    self.assertIsNone(module.ACCESS_BLOCKED)

    def test_batch_preserves_nonretryable_discovery_and_real_output_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "processed.json").write_text('{"items": []}')
            args = ["batch", "--limit", "5", "--date", datetime.now(batch.BEIJING).date().isoformat(),
                    "--work-root", str(root / "work"), "--output-dir", str(root / "output"),
                    "--processed-manifest", str(root / "processed.json"), "--no-hot-context",
                    "--avatar-provider", "local"]

            def run(command, **kwargs):
                self.assertEqual(Path(command[1]).name, "discover_note.py")
                output = Path(command[2])
                output.mkdir(parents=True, exist_ok=True)
                (output / "discovery_status.json").write_text(json.dumps(TRACE["persisted"]))

            with patch.object(sys, "argv", args), patch.object(batch, "run", side_effect=run) as execute:
                batch.main()
            report = json.loads((root / "output/delivery_status.json").read_text())
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(report["reason"], "tikhub_payment_required")
            self.assertFalse(report["retryable"])
            self.assertEqual((report["target"], report["succeeded"]), (5, 0))

    def test_comment_block_marker_keeps_nonretryable_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "chosen_note.json").write_text('{"note_id": "n1"}')
            (root / "cover.png").write_bytes(b"existing cover, never decoded")
            args = ["fetch", str(root), "--reuse-comments-cache"]
            with patch.object(sys, "argv", args), \
                    patch.multiple(fetch, KEY="offline-test", WORK=root, NOTE={}, BUDGET=None,
                                   ACCESS_BLOCKED=None, ACTIVE_COMMENT_ENDPOINT=None, MAX_ATTEMPTS=3), \
                    patch.object(fetch, "load_cached_comments", side_effect=fetch.TikHubAccessBlocked(402, "/comments")):
                with self.assertRaises(SystemExit) as caught:
                    fetch.main()
                self.assertEqual(caught.exception.code, 75)
            report = json.loads((root / "tikhub_access_blocked.json").read_text())
            self.assertEqual(report["reason"], "tikhub_payment_required")
            self.assertFalse(report["retryable"])
            self.assertEqual(report["status_code"], 402)

    def test_batch_keeps_comment_payment_defer_nonretryable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "processed.json").write_text('{"items": []}')
            args = ["batch", "--limit", "1", "--date", datetime.now(batch.BEIJING).date().isoformat(),
                    "--work-root", str(root / "work"), "--output-dir", str(root / "output"),
                    "--processed-manifest", str(root / "processed.json"), "--no-hot-context",
                    "--avatar-provider", "local"]

            def run(command, **kwargs):
                name, output = Path(command[1]).name, Path(command[2])
                if name == "discover_note.py":
                    output.mkdir(parents=True, exist_ok=True)
                    (output / "discovery_status.json").write_text('{"status": "ready"}')
                    (output / "selected_notes.json").write_text('[{"note_id": "n1", "title": "test"}]')
                elif name == "fetch_assets.py":
                    (output / "tikhub_access_blocked.json").write_text(json.dumps({
                        "reason": "tikhub_payment_required", "status_code": 402, "retryable": False,
                    }))
                    raise subprocess.CalledProcessError(75, command)
                else:
                    self.fail("A blocked provider must not proceed to generation or rendering")

            with patch.object(sys, "argv", args), patch.object(batch, "run", side_effect=run):
                batch.main()
            report = json.loads((root / "output/delivery_status.json").read_text())
            self.assertEqual(report["reason"], "tikhub_payment_required")
            self.assertFalse(report["retryable"])
            self.assertEqual(report["succeeded"], 0)

    def test_legacy_persisted_402_is_normalized_by_actual_workflow_block(self):
        workflow = (ROOT / ".github/workflows/xhs-lowfan-kc-daily.yml").read_text()
        block = workflow.split('defer_reason="$(python3 - "$KC_OUTPUT_DATE" <<\'PY\'\n', 1)[1].split("\n          PY", 1)[0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / "xhs2vid/state/daily_delivery_state.json"
            state.parent.mkdir(parents=True)
            state.write_text(json.dumps({"items": [{"date": "2026-10-04", **TRACE["persisted"]}]}))
            result = subprocess.run([sys.executable, "-", "2026-10-04"],
                                    input=textwrap.dedent(block), text=True, capture_output=True, cwd=root,
                                    env={**os.environ, "PYTHONPATH": str(ROOT)}, check=True)
        self.assertEqual(result.stdout.strip(), "tikhub_payment_required")

    def test_local_budget_and_unproven_generic_errors_are_not_account_failures(self):
        for reason in ("tikhub_request_budget_exhausted", "tikhub_stage_budget_reserved", "no_strict_low_fan_candidate"):
            self.assertEqual(normalize_access_reason(reason, message="HTTP 402"), reason)
        self.assertEqual(normalize_access_reason("tikhub_access_blocked", message="unknown error"), "tikhub_access_blocked")
        self.assertTrue(deferred_retryable("tikhub_rate_limited"))


if __name__ == "__main__":
    unittest.main()
