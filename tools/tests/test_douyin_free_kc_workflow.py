from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "douyin-free-kc-daily.yml"


class DouyinFreeKcWorkflowContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def step_block(self, start: str, end: str) -> str:
        return self.workflow.split(start, 1)[1].split(end, 1)[0]

    def test_schedule_has_off_peak_compensation_and_serialization(self) -> None:
        self.assertIn('- cron: "17 23 * * *"', self.workflow)
        self.assertIn('- cron: "47 23 * * *"', self.workflow)
        self.assertIn('- cron: "17 0 * * *"', self.workflow)
        self.assertIn("github.event_name == 'schedule' && 'scheduled' || github.run_id", self.workflow)
        self.assertIn("cancel-in-progress: false", self.workflow)

    def test_scheduled_compensation_skips_after_five_are_on_main(self) -> None:
        guard = self.step_block("  delivery_guard:\n", "  tavily-hot-context-smoke:\n")
        self.assertIn("contents/outputs/kc_entertain/${output_date}", guard)
        self.assertIn("published_count >= KC_LIMIT", guard)
        self.assertIn('echo "should_run=false"', guard)
        self.assertIn("needs: delivery_guard", self.workflow)
        self.assertIn("needs.delivery_guard.outputs.should_run == 'true'", self.workflow)

    def test_partial_outputs_are_packaged_and_merged_before_delivery(self) -> None:
        self.assertEqual(self.workflow.count("--min-selected-videos 1"), 2)
        self.assertIn("fallback_limit=$((KC_LIMIT - primary_count))", self.workflow)
        self.assertIn("--prepend-outputs-file work/tikhub_primary_outputs.txt", self.workflow)

        artifact = self.step_block(
            "      - name: Prepare KC artifact payload\n",
            "      - name: Prepare quality reports\n",
        )
        self.assertIn("if: ${{ !cancelled() }}", artifact)

    def test_webdav_prune_requires_full_target(self) -> None:
        publish = self.step_block(
            "      - name: Upload all KC videos and prepare Git-safe copies\n",
            "      - name: Commit KC videos to main\n",
        )
        self.assertIn('get("target_met")', publish)
        self.assertIn('"$selected_count" -lt "$KC_LIMIT"', publish)
        self.assertIn("if (( verified >= KC_LIMIT )); then", publish)
        self.assertIn("--webdav-prune-extra", publish)
        self.assertNotIn("verified >= expected", publish)

    def test_git_commit_requires_full_target_and_five_verified(self) -> None:
        commit = self.step_block(
            "      - name: Commit KC videos to main\n",
            "      - name: Prepare KC artifact payload\n",
        )
        self.assertIn('get("target_met")', commit)
        self.assertIn('"$selected_count" -lt "$KC_LIMIT"', commit)
        self.assertIn('"$verified" -lt "$KC_LIMIT"', commit)
        self.assertIn("exit 1", commit)
        self.assertNotIn("continue-on-error: true", commit)
        self.assertNotIn('"$verified" -lt "$expected"', commit)

    def test_partial_delivery_fails_final_verification(self) -> None:
        verify = self.workflow.split("      - name: Verify daily delivery\n", 1)[1]
        self.assertIn("if not target_met or selected_count < limit:", verify)
        self.assertIn("len(videos) < limit or verified < limit", verify)
        self.assertNotIn("Partial daily delivery accepted", verify)

    def test_new_daily_output_directory_does_not_break_git_staging(self) -> None:
        self.assertIn('if [[ -n "$(git ls-files -- "$KC_OUTPUT_DIR")" ]]; then', self.workflow)
        self.assertIn('git add -u -- "$KC_OUTPUT_DIR"', self.workflow)


if __name__ == "__main__":
    unittest.main()
