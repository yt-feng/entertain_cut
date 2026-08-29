from __future__ import annotations

import datetime as dt
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "run_gatex_intelligence_trend_intake.py"
SPEC = importlib.util.spec_from_file_location("gatex_trend", SCRIPT)
assert SPEC and SPEC.loader
trend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(trend)


class GateXTrendIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = dt.datetime(2026, 8, 29, 12, tzinfo=dt.timezone.utc)

    def source_rows(self):
        first = trend.signal(
            source_id="gdelt-doc", source_kind="gdelt", external_id="article-1",
            title="AI Data Centre Investment Opportunities and Trading Signals",
            summary="Power grid demand rises as new compute clusters enter service.",
            url="https://example.com/ai-grid?utm_source=test", publisher="Example News",
            published_at=self.now - dt.timedelta(hours=5), observed_at=self.now - dt.timedelta(hours=4),
            metrics={"views": 1_000, "likes": 30},
        )
        second = trend.signal(
            source_id="official-grid", source_kind="official", external_id="notice-1",
            title="AI data centre power grid demand rises as compute clusters enter service",
            summary="An operating notice records new data-centre load.",
            url="https://grid.example.org/notices/1", publisher="Grid operator",
            published_at=self.now - dt.timedelta(hours=2), observed_at=self.now - dt.timedelta(hours=1),
        )
        return [first, second]

    def test_url_copy_and_manual_gate(self):
        self.assertEqual(
            trend.canonical_url("https://www.Example.com/a/?utm_source=x&b=2&a=1#top"),
            "https://example.com/a?a=1&b=2",
        )
        state = {"schema": "gatex-intelligence-trend-state/v1", "observations": {}}
        rows = self.source_rows()
        trend.enrich_with_history(rows, state, self.now, trend.DEFAULT_MARKET_TERMS, 18)
        proposals = trend.build_proposals(rows, self.now, minimum_score=0, minimum_sources=2)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual(proposal["schema"], "gatex-intelligence-intake/v1")
        self.assertEqual(proposal["topic"]["provenanceType"], "trend_proposal")
        self.assertRegex(proposal["sources"][0]["contentHash"], r"^[0-9a-f]{64}$")
        self.assertEqual(proposal["trend"]["reviewState"], "proposed")
        self.assertFalse(proposal["triggerDraft"])
        self.assertNotRegex(proposal["topic"]["title"], r"(?i)investment|trading signal")
        self.assertRegex(" ".join(source["title"] for source in proposal["sources"]), r"Investment Opportunities")
        self.assertEqual(proposal["trend"]["independentSourceCount"], 2)

    def test_short_english_terms_require_word_boundaries(self):
        self.assertTrue(trend.term_matches("AI infrastructure update", "ai"))
        self.assertFalse(trend.term_matches("Retail daily update", "ai"))

    def test_history_creates_velocity_and_state_is_bounded(self):
        state = {"schema": "gatex-intelligence-trend-state/v1", "observations": {}}
        rows = self.source_rows()
        trend.enrich_with_history(rows, state, self.now - dt.timedelta(hours=3), trend.DEFAULT_MARKET_TERMS, 18)
        later = self.source_rows()[0]
        later["observedAt"] = trend.iso(self.now)
        later["metrics"] = {"views": 80_000, "likes": 4_000, "comments": 800, "shares": 200}
        trend.enrich_with_history([later], state, self.now, trend.DEFAULT_MARKET_TERMS, 18)
        self.assertGreater(later["velocity"], 0.5)
        self.assertLessEqual(later["decay"], 1)
        self.assertEqual(len(state["observations"]), 2)

    def test_golden_cluster_id_survives_order_and_secondary_membership_changes(self):
        baseline_rows = self.source_rows()
        baseline_state = {"schema": "gatex-intelligence-trend-state/v1", "observations": {}}
        trend.enrich_with_history(
            baseline_rows, baseline_state, self.now, trend.DEFAULT_MARKET_TERMS, 18
        )
        for item in baseline_rows:
            item["qualityScore"] = min(item["qualityScore"], 0.82)
            item["relevance"] = min(item["relevance"], 0.7)
        aliases = {}
        baseline = trend.build_proposals(
            baseline_rows, self.now, minimum_score=0, minimum_sources=1,
            cluster_aliases=aliases,
        )[0]
        reordered = trend.build_proposals(
            list(reversed(baseline_rows)), self.now, minimum_score=0, minimum_sources=1,
            cluster_aliases=aliases,
        )[0]

        expanded_rows = self.source_rows()
        secondary = trend.signal(
            source_id="stronger-official", source_kind="official", external_id="secondary-1",
            title="AI data centre power grid demand surges as hyperscale compute clusters enter service",
            summary="An operating notice records new data-centre load and accelerating demand.",
            url="https://secondary.example.net/data-centre-grid", publisher="Stronger Official Source",
            published_at=self.now - dt.timedelta(minutes=20),
            observed_at=self.now - dt.timedelta(minutes=10),
            metrics={"views": 500_000, "likes": 25_000, "comments": 3_000, "shares": 2_000},
        )
        expanded_rows.append(secondary)
        expanded_state = {"schema": "gatex-intelligence-trend-state/v1", "observations": {}}
        trend.enrich_with_history(
            expanded_rows, expanded_state, self.now, trend.DEFAULT_MARKET_TERMS, 18
        )
        for item in expanded_rows[:-1]:
            item["qualityScore"] = min(item["qualityScore"], 0.82)
            item["relevance"] = min(item["relevance"], 0.7)
        secondary["qualityScore"] = 1.0
        secondary["relevance"] = 1.0
        secondary["decay"] = 1.0
        secondary["engagement"] = 1.0
        expanded = trend.build_proposals(
            list(reversed(expanded_rows)), self.now, minimum_score=0, minimum_sources=1,
            cluster_aliases=aliases,
        )[0]
        self.assertEqual(reordered["externalId"], baseline["externalId"])
        self.assertEqual(expanded["externalId"], baseline["externalId"])
        self.assertEqual(expanded["trend"]["cluster"], baseline["trend"]["cluster"])
        self.assertEqual(len(expanded["sources"]), 3)
        self.assertEqual(expanded["sources"][0]["publisher"], "Stronger Official Source")
        self.assertTrue(all(value == baseline["externalId"] for value in aliases.values()))

    def test_shared_python_typescript_cluster_signature_golden(self):
        row = trend.signal(
            source_id="golden-source", source_kind="official", external_id="golden-1",
            title="AI data centre power demand accelerates", summary="",
            url="https://golden.example.org/topic", publisher="Golden Source",
            published_at=self.now - dt.timedelta(hours=2),
            observed_at=self.now - dt.timedelta(minutes=90),
        )
        rows = [row]
        state = {"schema": "gatex-intelligence-trend-state/v1", "observations": {}}
        trend.enrich_with_history(
            rows, state, self.now, ["AI data centre", "power demand"], 18
        )
        proposal = trend.build_proposals(
            rows, self.now, minimum_score=0, minimum_sources=1, cluster_aliases={}
        )[0]
        self.assertEqual(proposal["externalId"], "trend-3896f10c88a6caab")

    def test_cli_dry_run_writes_artifact_without_posting(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture = base / "signals.json"
            output = base / "proposals.json"
            state = base / "state.json"
            fixture.write_text(json.dumps(self.source_rows()), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--skip-network", "--fixture", str(fixture),
                    "--work-dir", str(base / "work"), "--state", str(state), "--output", str(output),
                    "--now", trend.iso(self.now), "--minimum-score", "0",
                ],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema"], "gatex-intelligence-trend-batch/v1")
            self.assertEqual(payload["delivery"]["attempted"], 0)
            self.assertTrue(all(not item["triggerDraft"] for item in payload["proposals"]))

    def test_scheduled_no_collector_success_fails_without_saving_history(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            output = base / "proposals.json"
            state = base / "state.json"
            original_state = {
                "schema": "gatex-intelligence-trend-state/v1",
                "updatedAt": "2026-08-29T01:00:00Z",
                "observations": {
                    "sentinel:item": {
                        "firstSeenAt": "2026-08-29T00:00:00Z",
                        "lastSeenAt": "2026-08-29T01:00:00Z",
                    }
                },
            }
            state.write_text(json.dumps(original_state), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--skip-network",
                    "--require-successful-collector",
                    "--work-dir", str(base / "work"), "--state", str(state),
                    "--output", str(output), "--now", trend.iso(self.now),
                ],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["proposalCount"], 0)
            self.assertTrue(payload["collectorFailure"])
            self.assertFalse(payload["historySaved"])
            self.assertEqual(json.loads(state.read_text(encoding="utf-8")), original_state)

    def test_successful_collector_with_zero_market_matches_is_a_normal_empty_run(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            fixture = base / "signals.json"
            output = base / "proposals.json"
            state = base / "state.json"
            off_topic = trend.signal(
                source_id="fixture", source_kind="news", external_id="off-topic",
                title="Celebrity dance challenge reaches the weekend chart",
                summary="A dance-only fixture about weekend chart rankings.",
                url="https://example.net/entertainment", publisher="Fixture",
                published_at=self.now, observed_at=self.now,
            )
            fixture.write_text(json.dumps([off_topic]), encoding="utf-8")
            completed = subprocess.run(
                [
                    sys.executable, str(SCRIPT), "--skip-network", "--fixture", str(fixture),
                    "--require-successful-collector",
                    "--work-dir", str(base / "work"), "--state", str(state),
                    "--output", str(output), "--now", trend.iso(self.now),
                ],
                cwd=ROOT, text=True, capture_output=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["proposalCount"], 0)
            self.assertEqual(payload["collectorSuccessCount"], 1)
            self.assertFalse(payload["collectorFailure"])
            self.assertTrue(payload["historySaved"])
            self.assertTrue(state.exists())

    def test_intake_url_is_exact_and_cannot_receive_the_secret_via_redirect_configuration(self):
        self.assertEqual(
            trend.validate_gatex_intake_url(
                "https://gatex.fund/api/integrations/intelligence/intake"
            ),
            trend.GATEX_INTAKE_URL,
        )
        for value in (
            "https://example.com/api/integrations/intelligence/intake",
            "https://gatex.fund:444/api/integrations/intelligence/intake",
            "https://gatex.fund/api/integrations/intelligence/intake?next=https://example.com",
            "https://user@gatex.fund/api/integrations/intelligence/intake",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                trend.validate_gatex_intake_url(value)

    def test_workflow_requires_delivery_configuration_and_commits_state_only_on_success(self):
        workflow = (ROOT / ".github/workflows/gatex-intelligence-trend-intake.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn('-z "$GATEX_INTELLIGENCE_INTAKE_URL"', workflow)
        self.assertIn('-z "$GATEX_INTELLIGENCE_INTAKE_SECRET"', workflow)
        self.assertIn("args+=(--require-successful-collector)", workflow)
        self.assertIn(
            "if: ${{ success() && (github.event_name == 'schedule' || inputs.post_to_gatex == true) }}",
            workflow,
        )

    def test_post_uses_only_the_dedicated_intelligence_intake_secret(self):
        original_post_gatex_intake = trend.post_gatex_intake
        original_values = {
            name: os.environ.get(name)
            for name in (
                "GATEX_INTELLIGENCE_INTAKE_URL",
                "GATEX_INTELLIGENCE_INTAKE_SECRET",
                "GATEX_GENERATION_CALLBACK_SECRET",
            )
        }
        calls = []

        def fake_post_gatex_intake(url, payload, token, timeout=30):
            calls.append({"url": url, "payload": payload, "token": token, "timeout": timeout})

        try:
            trend.post_gatex_intake = fake_post_gatex_intake
            os.environ["GATEX_INTELLIGENCE_INTAKE_URL"] = "https://gatex.fund/api/integrations/intelligence/intake"
            os.environ["GATEX_INTELLIGENCE_INTAKE_SECRET"] = "dedicated-intake-secret"
            os.environ["GATEX_GENERATION_CALLBACK_SECRET"] = "legacy-callback-secret"
            delivery = trend.post_proposals([{"externalId": "trend-test"}], [])
            self.assertEqual(delivery, {"configured": 1, "attempted": 1, "accepted": 1})
            self.assertEqual(calls[0]["token"], "dedicated-intake-secret")

            calls.clear()
            del os.environ["GATEX_INTELLIGENCE_INTAKE_SECRET"]
            delivery = trend.post_proposals([{"externalId": "trend-test"}], [])
            self.assertEqual(delivery, {"configured": 0, "attempted": 0, "accepted": 0})
            self.assertEqual(calls, [], "the legacy generation callback secret must not authorize intake")
        finally:
            trend.post_gatex_intake = original_post_gatex_intake
            for name, value in original_values.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
