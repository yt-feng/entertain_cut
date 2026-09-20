from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "restore_kc_artifacts.py"
SPEC = importlib.util.spec_from_file_location("restore_kc_artifacts", MODULE_PATH)
assert SPEC and SPEC.loader
recovery = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(recovery)


class RestoreKcArtifactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = argparse.Namespace(
            repo="owner/repo", workflow="douyin-free-kc-daily.yml", branch="main",
            current_run_id="9", output_date="2026-09-20", limit=5, max_runs=3,
            output_dir=self.root / "outputs/kc_entertain/2026-09-20",
            outputs_file=self.root / "work/kc_existing_outputs.txt",
            processed_manifest=self.root / "outputs/kc_entertain/processed_aweme_ids.json",
            work_dir=self.root / "work/recovery", github_env=self.root / "env.txt",
        )
        self.calls = []
        self.runs = []
        self.artifacts = {}

    def run_info(self, run_id: int, created_at: str = "2026-09-20T01:00:00Z", **values) -> dict:
        return {"databaseId": run_id, "headBranch": "main", "status": "completed",
                "createdAt": created_at, **values}

    def write_artifacts(self, reports: Path, videos: Path, entries: list[tuple[str, str]]) -> None:
        reports.mkdir(parents=True, exist_ok=True)
        videos.mkdir(parents=True, exist_ok=True)
        dated_videos = videos / "2026-09-20"
        dated_videos.mkdir(exist_ok=True)
        selected = []
        manifest = {}
        for index, (name, content_id) in enumerate(entries):
            output = f"/home/runner/work/repo/repo/outputs/kc_entertain/2026-09-20/{name}"
            selected.append(output)
            manifest[str(index)] = {"output": output, "source_metadata": {
                "content_id": content_id, "aweme_id": content_id, "platform": "douyin", "title": name,
            }}
            (dated_videos / name).write_bytes(b"validated video fixture")
        (reports / "kc_delivery_summary.json").write_text(
            json.dumps({"selected_count": len(selected), "selected_files": selected}), encoding="utf-8")
        (reports / "auto_kc_douyin_free").mkdir(exist_ok=True)
        (reports / "auto_kc_douyin_free/processed_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def fake_command(self, command: list[str], timeout: int) -> str:
        self.calls.append(command)
        if command[1:3] == ["run", "list"]:
            return json.dumps(self.runs)
        if command[1] == "api":
            run_id = command[2].split("/runs/")[1].split("/")[0]
            names = [recovery.REPORT_ARTIFACT, recovery.VIDEO_ARTIFACT] if run_id in self.artifacts else []
            return json.dumps({"artifacts": [{"name": name, "expired": False} for name in names]})
        if command[1:3] == ["run", "download"]:
            run_id = command[3]
            directory = Path(command[command.index("--dir") + 1])
            self.write_artifacts(directory.parent / "reports", directory.parent / "videos", self.artifacts[run_id])
            return ""
        self.fail(f"Unexpected command: {command}")

    def test_run_selection_is_same_beijing_date_branch_completed_and_bounded(self) -> None:
        self.args.max_runs = 50
        runs = [
            self.run_info(9), self.run_info(8, headBranch="other"),
            self.run_info(7, status="in_progress"), self.run_info(6, "2026-09-19T15:59:59Z"),
            self.run_info(5, "2026-09-20T16:00:00Z"), self.run_info(4, "2026-09-19T16:00:00Z"),
            self.run_info(3, "2026-09-20T03:00:00Z"), self.run_info(2, "2026-09-20T02:00:00Z"),
            self.run_info(1, "2026-09-20T01:00:00Z"),
        ]
        self.assertEqual(recovery.eligible_runs(runs, self.args), ["3", "2", "1"])
        self.assertEqual(recovery.eligible_runs([self.run_info(4, "2026-09-19T16:00:00Z")], self.args), ["4"])

    def test_only_exact_day_selected_files_with_source_metadata_are_used(self) -> None:
        reports, videos = self.root / "reports", self.root / "videos"
        self.write_artifacts(reports, videos, [("valid.mp4", "100"), ("no-metadata.mp4", "101"), ("old.mp4", "102")])
        manifest_path = reports / "auto_kc_douyin_free/processed_manifest.json"
        manifest = json.loads(manifest_path.read_text())
        del manifest["1"]["source_metadata"]
        manifest["2"]["output"] = manifest["2"]["output"].replace("2026-09-20", "2026-09-19")
        manifest_path.write_text(json.dumps(manifest))
        (videos / "unlisted.mp4").write_bytes(b"not selected")
        selected = recovery.selected_artifacts(reports, videos, self.args.output_date)
        self.assertEqual([path.name for path, _ in selected], ["valid.mp4"])
        for value in ("outputs/kc_entertain/2026-09-19/old.mp4", "outputs/kc_entertain/2026-09-20/sub/nested.mp4",
                      "outputs/kc_entertain/2026-09-20/../2026-09-20/escape.mp4"):
            self.assertIsNone(recovery.same_day_output(value, self.args.output_date))

    def test_compressed_primary_is_selected_without_oversized_original_or_other_date(self) -> None:
        reports, videos = self.root / "reports", self.root / "videos"
        self.write_artifacts(reports, videos, [("selected.mp4", "100")])
        primary = videos / "2026-09-20/selected.mp4"
        originals = videos / "2026-09-20/oversized-originals"
        originals.mkdir()
        (originals / primary.name).write_bytes(b"oversized original")
        previous_date = videos / "2026-09-19"
        previous_date.mkdir()
        (previous_date / primary.name).write_bytes(b"previous day")

        selected = recovery.selected_artifacts(reports, videos, self.args.output_date)
        self.assertEqual([path for path, _ in selected], [primary])
        primary.unlink()
        self.assertEqual(recovery.selected_artifacts(reports, videos, self.args.output_date), [])

    def test_recovery_preserves_existing_deduplicates_sources_and_updates_gap_and_ledger(self) -> None:
        self.args.limit = 3
        self.args.output_dir.mkdir(parents=True)
        existing = self.args.output_dir / "published.mp4"
        existing.write_bytes(b"existing")
        self.args.outputs_file.parent.mkdir(parents=True)
        self.args.outputs_file.write_text(f"{existing}\n")
        self.args.processed_manifest.write_text(json.dumps({"items": [
            {"aweme_id": "99", "content_id": "99", "platform": "douyin", "title": "original"},
        ]}))
        self.runs = [self.run_info(3, "2026-09-20T03:00:00Z"), self.run_info(2, "2026-09-20T02:00:00Z")]
        self.artifacts = {"3": [("first.mp4", "100")], "2": [("renamed.mp4", "100"), ("second.mp4", "101")]}

        with mock.patch.object(recovery, "run_command", side_effect=self.fake_command), \
                mock.patch.object(recovery, "validate_video") as validate:
            report = recovery.restore(self.args)

        self.assertEqual(report["errors"], [])
        self.assertEqual(report["attempted_runs"], ["3", "2"])
        self.assertEqual(report["selected_count"], 3)
        self.assertEqual(report["generation_limit"], 0)
        self.assertEqual([Path(path).name for path in self.args.outputs_file.read_text().splitlines()],
                         ["published.mp4", "first.mp4", "second.mp4"])
        self.assertFalse((self.args.output_dir / "renamed.mp4").exists())
        self.assertEqual(validate.call_count, 3)
        ledger = json.loads(self.args.processed_manifest.read_text())
        self.assertEqual(ledger["count"], 3)
        self.assertEqual({item["content_id"] for item in ledger["items"]}, {"99", "100", "101"})
        self.assertEqual(ledger["items"][0]["title"], "original")
        self.assertEqual(self.args.github_env.read_text().splitlines()[-1], "KC_GENERATION_LIMIT=0")
        self.assertEqual(len(json.loads((self.args.work_dir / "processed_manifest.json").read_text())), 2)

    def test_invalid_media_does_not_stage_or_update_processed_ledger(self) -> None:
        self.runs = [self.run_info(3)]
        self.artifacts = {"3": [("broken.mp4", "100")]}
        with mock.patch.object(recovery, "run_command", side_effect=self.fake_command), \
                mock.patch.object(recovery, "validate_video", side_effect=ValueError("decode failed")):
            report = recovery.restore(self.args)
        self.assertEqual(report["selected_count"], 0)
        self.assertEqual(report["generation_limit"], 5)
        self.assertFalse((self.args.output_dir / "broken.mp4").exists())
        self.assertFalse(self.args.processed_manifest.exists())
        self.assertIn("decode failed", report["skipped"][0]["reason"])

    def test_next_run_can_recover_metadata_packaged_by_previous_recovery(self) -> None:
        self.runs = [self.run_info(3)]
        self.artifacts = {"3": [("recovered.mp4", "100")]}
        with mock.patch.object(recovery, "run_command", side_effect=self.fake_command), \
                mock.patch.object(recovery, "validate_video"):
            recovery.restore(self.args)

        reports, videos = self.root / "next-reports", self.root / "next-videos"
        self.write_artifacts(reports, videos, [("recovered.mp4", "100"), ("new.mp4", "101")])
        fresh_manifest_path = reports / "auto_kc_douyin_free/processed_manifest.json"
        fresh_manifest = json.loads(fresh_manifest_path.read_text())
        del fresh_manifest["0"]  # Current packaging records only the newly rendered source.
        fresh_manifest_path.write_text(json.dumps(fresh_manifest))
        (reports / "kc_recovery").mkdir()
        (reports / "kc_recovery/processed_manifest.json").write_text(
            (self.args.work_dir / "processed_manifest.json").read_text())

        selected = recovery.selected_artifacts(reports, videos, self.args.output_date)
        self.assertEqual([(path.name, metadata["content_id"]) for path, metadata in selected],
                         [("recovered.mp4", "100"), ("new.mp4", "101")])

    def test_missing_artifacts_are_skipped_and_attempts_never_exceed_three(self) -> None:
        self.runs = [self.run_info(index, f"2026-09-20T0{index}:00:00Z") for index in range(1, 6)]
        self.args.max_runs = 10
        with mock.patch.object(recovery, "run_command", side_effect=self.fake_command):
            report = recovery.restore(self.args)
        self.assertEqual(report["attempted_runs"], ["5", "4", "3"])
        self.assertFalse(any(command[1:3] == ["run", "download"] for command in self.calls))

    def test_github_failure_stops_recovery_and_preserves_generation_gap(self) -> None:
        with mock.patch.object(recovery, "run_command", side_effect=RuntimeError("gh unavailable")) as command:
            report = recovery.restore(self.args)
        self.assertEqual(command.call_count, 1)
        self.assertEqual(report["errors"], ["gh unavailable"])
        self.assertEqual(report["generation_limit"], 5)
        self.assertEqual(json.loads((self.args.work_dir / "summary.json").read_text())["errors"], report["errors"])

    def test_full_existing_delivery_is_validated_without_github_calls(self) -> None:
        self.args.limit = 1
        self.args.output_dir.mkdir(parents=True)
        (self.args.output_dir / "already-published.mp4").write_bytes(b"video")
        with mock.patch.object(recovery, "run_command") as command, mock.patch.object(recovery, "validate_video") as validate:
            report = recovery.restore(self.args)
        validate.assert_called_once()
        command.assert_not_called()
        self.assertEqual(report["generation_limit"], 0)

    def test_media_requires_h264_aac_portrait_duration_and_full_bounded_decode(self) -> None:
        video = self.root / "video.mp4"
        video.write_bytes(b"video")
        probe = {"streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920},
            {"codec_type": "audio", "codec_name": "aac"},
        ], "format": {"duration": "299"}}
        with mock.patch.object(recovery, "run_command", side_effect=[json.dumps(probe), ""]) as command:
            recovery.validate_video(video)
        decode, timeout = command.call_args.args
        self.assertEqual(decode[0], "ffmpeg")
        self.assertIn("-xerror", decode)
        self.assertNotIn("-t", decode)
        self.assertEqual(timeout, 180)
        for location, key, value in (("video", "codec_name", "hevc"), ("audio", "codec_name", "mp3"),
                                     ("video", "width", 720), ("format", "duration", "301"),
                                     ("format", "duration", "nan")):
            with self.subTest(location=location, key=key, value=value):
                invalid = json.loads(json.dumps(probe))
                target = invalid["format"] if location == "format" else invalid["streams"][0 if location == "video" else 1]
                target[key] = value
                with mock.patch.object(recovery, "run_command", return_value=json.dumps(invalid)) as command:
                    with self.assertRaises(ValueError):
                        recovery.validate_video(video)
                self.assertEqual(command.call_count, 1)
        with mock.patch.object(recovery, "run_command", side_effect=[json.dumps(probe), subprocess.TimeoutExpired("ffmpeg", 180)]):
            with self.assertRaises(subprocess.TimeoutExpired):
                recovery.validate_video(video)


if __name__ == "__main__":
    unittest.main()
