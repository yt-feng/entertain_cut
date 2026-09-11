from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "douyin-free-kc-daily.yml"
TOOLS_DIR = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("verify_kc_delivery", TOOLS_DIR / "verify_kc_delivery.py")
assert SPEC and SPEC.loader
verifier = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verifier)


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
        self.assertIn("group: ${{ github.workflow }}-${{ github.ref_name }}", self.workflow)
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

    def test_delivery_minimum_is_scoped_to_this_workflow(self) -> None:
        self.assertIn('KC_MIN_DELIVERY=$((KC_LIMIT < 3 ? KC_LIMIT : 3))', self.workflow)
        prepare = self.step_block(
            "      - name: Prepare current-run KC videos for delivery\n",
            "      - name: Upload all KC videos and prepare Git-safe copies\n",
        )
        self.assertIn('--min-delivery "$KC_MIN_DELIVERY"', prepare)

    def test_upload_accepts_minimum_but_webdav_prune_requires_full_target(self) -> None:
        publish = self.step_block(
            "      - name: Upload all KC videos and prepare Git-safe copies\n",
            "      - name: Commit KC videos to main\n",
        )
        self.assertIn('get("target_met")', publish)
        self.assertIn('"$selected_count" -lt "$KC_MIN_DELIVERY"', publish)
        self.assertIn('if [[ "$target_met" == "true" ]]; then', publish)
        self.assertIn('prune_args+=(--webdav-prune-extra', publish)
        self.assertIn('"${prune_args[@]}"', publish)
        self.assertIn("if python tools/verify_kc_delivery.py", publish)

    def test_git_commit_requires_every_selected_video_verified_and_git_ready(self) -> None:
        commit = self.step_block(
            "      - name: Commit KC videos to main\n",
            "      - name: Prepare KC artifact payload\n",
        )
        self.assertIn("if ! python tools/verify_kc_delivery.py", commit)
        self.assertIn('--git-max-bytes "$KC_GIT_MAX_BYTES"', commit)
        self.assertIn('git restore --worktree -- "$previous_video"', commit)
        self.assertIn("exit 1", commit)
        self.assertNotIn("continue-on-error: true", commit)

    def test_final_delivery_requires_verification_without_error_masking(self) -> None:
        verify = self.workflow.split("      - name: Verify daily delivery\n", 1)[1]
        self.assertIn("python tools/verify_kc_delivery.py", verify)
        self.assertIn('--min-delivery "$KC_MIN_DELIVERY"', verify)
        self.assertIn('--git-max-bytes "$KC_GIT_MAX_BYTES"', verify)
        self.assertNotIn("continue-on-error", verify)

    def test_fallback_does_not_replace_primary_selected_artifacts(self) -> None:
        fallback = self.step_block(
            "      - name: Try free Cookie source to fill missing KC videos\n",
            "      - name: Download selected videos from prior free run\n",
        )
        self.assertIn("--work-root work/douyin_free_fallback", fallback)
        self.assertNotIn("--work-root work/douyin_free_daily", fallback)
        self.assertIn("work/douyin_reports_artifact/free_fallback", self.workflow)

    def test_compensation_reuses_only_same_day_and_requests_remaining_gap(self) -> None:
        stage = self.step_block(
            "      - name: Reuse same-day published videos for scheduled compensation\n",
            "      - name: Install system packages\n",
        )
        script = textwrap.dedent(stage.split("        run: |\n", 1)[1])
        for event, existing_count, expected_gap in (("schedule", 4, 1), ("schedule", 5, 0), ("workflow_dispatch", 4, 5)):
            with self.subTest(event=event, existing_count=existing_count), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                output_dir = root / "outputs" / "2026-09-11"
                output_dir.mkdir(parents=True)
                for index in range(existing_count):
                    (output_dir / f"today-{index}.mp4").write_bytes(b"today")
                old_dir = output_dir.parent / "2026-09-10"
                old_dir.mkdir()
                (old_dir / "yesterday.mp4").write_bytes(b"old")
                env_file = root / "env.txt"
                result = subprocess.run(
                    ["bash", "-e", "-c", script.replace("python -", f'"{sys.executable}" -')],
                    cwd=root, env={**os.environ, "KC_LIMIT": "5", "GITHUB_EVENT_NAME": event,
                                   "KC_OUTPUT_DIR": str(output_dir), "GITHUB_ENV": str(env_file)},
                    capture_output=True, text=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f"KC_GENERATION_LIMIT={expected_gap}", env_file.read_text())
                manifest = (root / "work/kc_existing_outputs.txt").read_text()
                self.assertNotIn("yesterday", manifest)
                self.assertEqual(len(manifest.splitlines()), 5 - expected_gap)
        self.assertIn('--limit "$KC_GENERATION_LIMIT"', self.workflow)
        self.assertIn('--prepend-outputs-file work/kc_existing_outputs.txt', self.workflow)

    def test_new_daily_output_directory_does_not_break_git_staging(self) -> None:
        self.assertIn('if [[ -n "$(git ls-files -- "$KC_OUTPUT_DIR")" ]]; then', self.workflow)
        self.assertIn('git add -u -- "$KC_OUTPUT_DIR"', self.workflow)


class KcDeliveryVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output_dir = Path(self.temp.name)

    def evidence(self, count: int, verified: int, limit: int = 5) -> tuple[dict, dict]:
        paths = [self.output_dir / f"current-{index}.mp4" for index in range(count)]
        for path in paths:
            path.write_bytes(b"video")
        delivery = {"selected_count": count, "selected_files": [str(path) for path in paths],
                    "minimum_met": count >= min(3, limit), "deliverable": count >= min(3, limit)}
        publish = {"webdav_verified_count": verified, "files": [
            {"name": path.name, "git_ready": True,
             "webdav": {"success": index < verified, "remote_verified": index < verified}}
            for index, path in enumerate(paths)
        ]}
        return delivery, publish

    def verify(self, delivery: dict, publish: dict, limit: int = 5) -> dict:
        return verifier.verify_delivery(delivery, publish, output_dir=self.output_dir,
                                        limit=limit, min_delivery=3, git_max_bytes=100)

    def test_minimum_and_every_selected_file_are_required(self) -> None:
        for count, verified, limit, expected in ((2, 2, 5, False), (3, 3, 5, True), (4, 4, 5, True),
                                                  (5, 5, 5, True), (5, 4, 5, False), (3, 2, 5, False),
                                                  (1, 1, 1, True)):
            with self.subTest(count=count, verified=verified, limit=limit):
                result = self.verify(*self.evidence(count, verified, limit), limit=limit)
                self.assertEqual(result["complete"], expected, result)
                self.assertEqual(result["minimum_met"], count >= min(3, limit))
                self.assertEqual(result["target_met"], count == limit)

    def test_aggregate_counts_or_put_success_do_not_prove_selected_files_verified(self) -> None:
        delivery, publish = self.evidence(3, 3)
        publish["files"][0]["webdav"] = {"put_success": True, "success": True, "remote_verified": False}
        self.assertFalse(self.verify(delivery, publish)["complete"])
        publish["files"][0] = {"name": "another-video.mp4", "git_ready": True,
                              "webdav": {"success": True, "remote_verified": True}}
        self.assertFalse(self.verify(delivery, publish)["complete"])

    def test_missing_local_file_and_failed_git_compression_fail_delivery(self) -> None:
        delivery, publish = self.evidence(3, 3)
        publish["files"][0]["git_ready"] = False
        self.assertFalse(self.verify(delivery, publish)["complete"])
        publish["files"][0]["git_ready"] = True
        Path(delivery["selected_files"][0]).unlink()
        self.assertFalse(self.verify(delivery, publish)["complete"])

    def test_missing_or_duplicate_selection_cannot_pass(self) -> None:
        delivery, publish = self.evidence(3, 3)
        delivery["selected_files"][0] = delivery["selected_files"][1]
        self.assertFalse(self.verify(delivery, publish)["complete"])
        delivery["selected_files"] = []
        self.assertFalse(self.verify(delivery, publish)["complete"])


class KcDeliveryWorkflowExecutionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def script(self, step: str, next_step: str | None = None) -> str:
        block = self.workflow.split(f"      - name: {step}\n", 1)[1]
        if next_step:
            block = block.split(f"      - name: {next_step}\n", 1)[0]
        return textwrap.dedent(block.split("        run: |\n", 1)[1])

    def make_run(self, root: Path, count: int, verified: int, limit: int = 5) -> dict[str, str]:
        output_dir = root / "outputs"
        output_dir.mkdir()
        work_dir = root / "work"
        work_dir.mkdir()
        files = [output_dir / f"current-{index}.mp4" for index in range(count)]
        for path in files:
            path.write_bytes(b"video")
        report = {"selected_count": count, "selected_files": [str(path) for path in files],
                  "target_met": count >= limit, "minimum_met": count >= min(3, limit),
                  "deliverable": count >= min(3, limit)}
        (work_dir / "kc_delivery_summary.json").write_text(json.dumps(report))
        tool_dir = root / "tools"
        tool_dir.mkdir()
        shutil.copy2(TOOLS_DIR / "verify_kc_delivery.py", tool_dir / "verify_kc_delivery.py")
        (tool_dir / "publish_kc_videos.py").write_text(textwrap.dedent("""\
            import json
            import os
            import sys
            from pathlib import Path
            args = sys.argv[1:]
            output_dir = Path(args[args.index('--output-dir') + 1])
            summary_path = Path(args[args.index('--summary-file') + 1])
            with Path('work/publish_attempts.jsonl').open('a') as log:
                log.write(json.dumps(args) + '\\n')
            verified = int(os.environ['MOCK_VERIFIED'])
            files = [{'name': path.name, 'git_ready': True,
                      'webdav': {'success': index < verified, 'remote_verified': index < verified}}
                     for index, path in enumerate(sorted(output_dir.glob('*.mp4')))]
            summary_path.write_text(json.dumps({'files': files, 'webdav_verified_count': verified}))
            """), encoding="utf-8")
        return {**os.environ, "KC_OUTPUT_DIR": str(output_dir), "KC_OUTPUT_DATE": "2026-09-11",
                "KC_LIMIT": str(limit), "KC_MIN_DELIVERY": str(min(3, limit)), "KC_GIT_MAX_BYTES": "100",
                "JIANGUOYUN_REMOTE_ROOT": "test", "MOCK_VERIFIED": str(verified)}

    def run_script(self, script: str, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
        # Exercise the workflow shell with local publisher evidence and no delays or network.
        prefix = f'python() {{ "{sys.executable}" "$@"; }}\nsleep() {{ :; }}\n'
        return subprocess.run(["bash", "-e", "-c", prefix + script], cwd=root, env=env,
                              capture_output=True, text=True)

    def test_upload_and_final_verification_follow_selected_set(self) -> None:
        upload_script = self.script("Upload all KC videos and prepare Git-safe copies", "Commit KC videos to main")
        verify_script = self.script("Verify daily delivery")
        for count, verified, limit, expected in ((2, 2, 5, False), (3, 3, 5, True), (4, 4, 5, True),
                                                  (5, 5, 5, True), (5, 4, 5, False), (3, 2, 5, False),
                                                  (1, 1, 1, True)):
            with self.subTest(count=count, verified=verified, limit=limit), tempfile.TemporaryDirectory() as temp_dir:
                root = Path(temp_dir)
                env = self.make_run(root, count, verified, limit)
                upload = self.run_script(upload_script, root, env)
                self.assertEqual(upload.returncode, 0, upload.stderr)
                attempts_file = root / "work/publish_attempts.jsonl"
                attempts = [json.loads(line) for line in attempts_file.read_text().splitlines()] if attempts_file.exists() else []
                if count < min(3, limit):
                    self.assertEqual(attempts, [])
                else:
                    self.assertEqual(len(attempts), 1 if verified == count else 3)
                    self.assertEqual("--webdav-prune-extra" in attempts[0], count == limit)
                    publish = json.loads((root / "work/kc_publish_summary.json").read_text())
                    self.assertEqual(len(publish["files"]), count)
                final = self.run_script(verify_script, root, env)
                self.assertEqual(final.returncode == 0, expected, final.stdout + final.stderr)

    def test_incomplete_upload_never_reaches_git(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_run(root, 5, 4)
            upload = self.script("Upload all KC videos and prepare Git-safe copies", "Commit KC videos to main")
            self.assertEqual(self.run_script(upload, root, env).returncode, 0)
            commit = self.script("Commit KC videos to main", "Prepare KC artifact payload")
            result = self.run_script('git() { echo called > git-called.txt; return 1; }\n' + commit, root, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "git-called.txt").exists())

    def test_below_target_run_restores_prior_tracked_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env = self.make_run(root, 3, 3)
            for command in (["git", "init", "-q"], ["git", "config", "user.name", "test"],
                            ["git", "config", "user.email", "test@example.invalid"]):
                subprocess.run(command, cwd=root, check=True, capture_output=True)
            previous = [root / "outputs" / f"previous-{index}.mp4" for index in range(5)]
            for path in previous:
                path.write_bytes(b"previous")
            subprocess.run(["git", "add", "--", *map(str, previous)], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-qm", "Existing delivery"], cwd=root, check=True, capture_output=True)
            for path in previous:
                path.unlink()
            upload = self.script("Upload all KC videos and prepare Git-safe copies", "Commit KC videos to main")
            self.assertEqual(self.run_script(upload, root, env).returncode, 0)
            commit = self.script("Commit KC videos to main", "Prepare KC artifact payload").split("shopt -s nullglob", 1)[0]
            result = self.run_script(commit, root, env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(all(path.read_bytes() == b"previous" for path in previous))


if __name__ == "__main__":
    unittest.main()
