from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "publish_kc_videos.py"
SPEC = importlib.util.spec_from_file_location("publish_kc_videos", MODULE_PATH)
assert SPEC and SPEC.loader
publish = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publish)


class PublishKcVideosTests(unittest.TestCase):
    def test_webdav_url_includes_my_jianguoyun_root_and_encodes_chinese(self) -> None:
        url = publish.build_webdav_url(
            "https://dav.jianguoyun.com/dav/",
            ["我的坚果云", "KCdesk", "Ops", "2026-07-13", "KC娱乐"],
        )
        self.assertEqual(
            url,
            "https://dav.jianguoyun.com/dav/"
            "%E6%88%91%E7%9A%84%E5%9D%9A%E6%9E%9C%E4%BA%91/"
            "KCdesk/Ops/2026-07-13/KC%E5%A8%B1%E4%B9%90/",
        )

    def test_git_limit_is_strict(self) -> None:
        self.assertTrue(publish.is_git_safe(99, 100))
        self.assertFalse(publish.is_git_safe(100, 100))

    def test_webdav_listing_extracts_only_video_names(self) -> None:
        xml = """<?xml version="1.0" encoding="utf-8"?>
        <d:multistatus xmlns:d="DAV:">
          <d:response><d:href>/dav/KC%E5%A8%B1%E4%B9%90/</d:href></d:response>
          <d:response>
            <d:href>/dav/KC%E5%A8%B1%E4%B9%90/KC%E5%A8%B1%E4%B9%90_%E6%97%A7%E7%89%88.mp4</d:href>
            <d:propstat><d:prop><d:getcontentlength>123</d:getcontentlength></d:prop></d:propstat>
          </d:response>
          <d:response><d:href>/dav/KC%E5%A8%B1%E4%B9%90/not-a-file.mp4/</d:href></d:response>
          <d:response><d:href>/dav/KC%E5%A8%B1%E4%B9%90/report.json</d:href></d:response>
        </d:multistatus>"""

        self.assertEqual(publish.parse_webdav_video_names(xml), ["KC娱乐_旧版.mp4"])
        self.assertEqual(publish.parse_webdav_video_entries(xml), {"KC娱乐_旧版.mp4": 123})

    def test_retry_uploads_only_missing_files_and_counts_remote_presence(self) -> None:
        existing = Path("existing.mp4")
        missing = Path("missing.mp4")
        remote_url = "https://example.test/dir/"

        with (
            mock.patch.object(
                publish,
                "list_webdav_videos",
                side_effect=[
                    {"success": True, "names": [existing.name], "sizes": {existing.name: 8}},
                    {
                        "success": True,
                        "names": [existing.name, missing.name],
                        "sizes": {existing.name: 8, missing.name: 7},
                    },
                ],
            ),
            mock.patch.object(
                publish,
                "upload_webdav_file",
                return_value={
                    "attempted": True,
                    "success": False,
                    "put_success": False,
                    "http_status": 403,
                    "reason": "PUT failed with HTTP 403",
                },
            ) as upload,
        ):
            results = publish.upload_all_webdav_files(
                [existing, missing],
                remote_url,
                {"ready": True},
                "user",
                "password",
                concurrency=3,
                expected_sizes={existing: {8}, missing: {7}},
            )

        upload.assert_called_once_with(missing, remote_url, "user", "password")
        self.assertTrue(results[existing]["success"])
        self.assertFalse(results[existing]["attempted"])
        self.assertTrue(results[missing]["success"])
        self.assertFalse(results[missing]["put_success"])
        self.assertEqual(results[missing]["http_status"], 403)
        self.assertTrue(results[missing]["remote_verified"])

    def test_two_attempts_preserve_four_and_only_retry_the_fifth(self) -> None:
        videos = [Path(f"video-{index}.mp4") for index in range(5)]
        expected_sizes = {video: {100 + index} for index, video in enumerate(videos)}
        remote_sizes: dict[str, int] = {}
        upload_attempts: list[Path] = []

        def fake_list(_url, _username, _password):
            return {
                "success": True,
                "names": list(remote_sizes),
                "sizes": dict(remote_sizes),
            }

        def fake_upload(video, _url, _username, _password):
            upload_attempts.append(video)
            if video == videos[-1] and upload_attempts.count(video) == 1:
                return {
                    "attempted": True,
                    "success": False,
                    "put_success": False,
                    "http_status": 403,
                    "reason": "PUT failed with HTTP 403",
                }
            remote_sizes[video.name] = next(iter(expected_sizes[video]))
            return {
                "attempted": True,
                "success": True,
                "put_success": True,
                "http_status": 201,
                "reason": "PUT accepted",
            }

        with (
            mock.patch.object(publish, "list_webdav_videos", side_effect=fake_list),
            mock.patch.object(publish, "upload_webdav_file", side_effect=fake_upload),
        ):
            first = publish.upload_all_webdav_files(
                videos,
                "https://example.test/dir/",
                {"ready": True},
                "user",
                "password",
                concurrency=3,
                expected_sizes=expected_sizes,
            )
            second = publish.upload_all_webdav_files(
                videos,
                "https://example.test/dir/",
                {"ready": True},
                "user",
                "password",
                concurrency=1,
                expected_sizes=expected_sizes,
            )

        self.assertEqual(sum(bool(item["success"]) for item in first.values()), 4)
        self.assertEqual(sum(bool(item["success"]) for item in second.values()), 5)
        self.assertEqual(upload_attempts.count(videos[-1]), 2)
        for video in videos[:-1]:
            self.assertEqual(upload_attempts.count(video), 1)

    def test_same_name_with_wrong_size_is_not_verified(self) -> None:
        video = Path("partial.mp4")
        listing = {
            "success": True,
            "names": [video.name],
            "sizes": {video.name: 3},
        }
        with (
            mock.patch.object(publish, "list_webdav_videos", side_effect=[listing, listing]),
            mock.patch.object(
                publish,
                "upload_webdav_file",
                return_value={
                    "attempted": True,
                    "success": False,
                    "put_success": False,
                    "http_status": 403,
                    "reason": "PUT failed with HTTP 403",
                },
            ) as upload,
        ):
            results = publish.upload_all_webdav_files(
                [video],
                "https://example.test/dir/",
                {"ready": True},
                "user",
                "password",
                concurrency=1,
                expected_sizes={video: {10}},
            )

        upload.assert_called_once()
        self.assertFalse(results[video]["success"])
        self.assertEqual(results[video]["remote_verified"], False)
        self.assertIn("remote size=3", results[video]["reason"])

    def test_previous_upload_size_survives_local_git_compression(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "outputs"
            output_dir.mkdir()
            source = output_dir / "video.mp4"
            source.write_bytes(b"small")
            summary_file = root / "summary.json"
            summary_file.write_text(
                json.dumps(
                    {
                        "files": [
                            {
                                "name": source.name,
                                "size_before": 14,
                                "webdav_expected_sizes": [14],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            previous_sizes = publish.load_previous_webdav_sizes(summary_file)

            with (
                mock.patch.object(
                    publish,
                    "ensure_webdav_directory",
                    return_value={"ready": True, "reason": "ready"},
                ),
                mock.patch.object(
                    publish,
                    "list_webdav_videos",
                    return_value={
                        "success": True,
                        "names": [source.name],
                        "sizes": {source.name: 14},
                    },
                ),
                mock.patch.object(publish, "upload_webdav_file") as upload,
            ):
                report = publish.process_directory(
                    output_dir=output_dir,
                    output_date="2026-07-13",
                    git_max_bytes=10,
                    compression_target_bytes=8,
                    backup_dir=root / "backup",
                    compression_work_dir=root / "tmp",
                    webdav_base_url="https://example.test/dav/",
                    webdav_root="我的坚果云/KCdesk/Ops",
                    webdav_category="KC娱乐",
                    webdav_user="user",
                    webdav_password="password",
                    previous_webdav_sizes=previous_sizes,
                )

        upload.assert_not_called()
        self.assertEqual(report["webdav_verified_count"], 1)
        self.assertEqual(report["webdav_existing_count"], 1)
        self.assertEqual(report["webdav_uploaded_count"], 0)
        self.assertEqual(report["files"][0]["webdav_expected_sizes"], [5, 14])

    def test_preflight_listing_failure_does_not_start_an_unverified_upload(self) -> None:
        video = Path("missing.mp4")
        with (
            mock.patch.object(
                publish,
                "list_webdav_videos",
                return_value={"success": False, "reason": "PROPFIND failed with HTTP 503"},
            ),
            mock.patch.object(publish, "upload_webdav_file") as upload,
        ):
            results = publish.upload_all_webdav_files(
                [video],
                "https://example.test/dir/",
                {"ready": True},
                "user",
                "password",
                concurrency=3,
                expected_sizes={video: {7}},
            )

        upload.assert_not_called()
        self.assertFalse(results[video]["success"])
        self.assertEqual(results[video]["phase"], "preflight")

    def test_postflight_failure_keeps_preflight_success(self) -> None:
        existing = Path("existing.mp4")
        missing = Path("missing.mp4")
        with (
            mock.patch.object(
                publish,
                "list_webdav_videos",
                side_effect=[
                    {"success": True, "names": [existing.name], "sizes": {existing.name: 8}},
                    {"success": False, "reason": "PROPFIND timed out"},
                ],
            ),
            mock.patch.object(
                publish,
                "upload_webdav_file",
                return_value={
                    "attempted": True,
                    "success": True,
                    "put_success": True,
                    "http_status": 201,
                    "reason": "PUT accepted",
                },
            ),
        ):
            results = publish.upload_all_webdav_files(
                [existing, missing],
                "https://example.test/dir/",
                {"ready": True},
                "user",
                "password",
                concurrency=3,
                expected_sizes={existing: {8}, missing: {7}},
            )

        self.assertTrue(results[existing]["success"])
        self.assertFalse(results[missing]["success"])
        self.assertTrue(results[missing]["put_success"])
        self.assertEqual(results[missing]["phase"], "postflight")

    def test_put_success_without_remote_presence_is_not_counted(self) -> None:
        video = Path("missing.mp4")
        with (
            mock.patch.object(
                publish,
                "list_webdav_videos",
                side_effect=[
                    {"success": True, "names": [], "sizes": {}},
                    {"success": True, "names": [], "sizes": {}},
                ],
            ),
            mock.patch.object(
                publish,
                "upload_webdav_file",
                return_value={
                    "attempted": True,
                    "success": True,
                    "put_success": True,
                    "http_status": 201,
                    "reason": "PUT accepted",
                },
            ),
        ):
            results = publish.upload_all_webdav_files(
                [video],
                "https://example.test/dir/",
                {"ready": True},
                "user",
                "password",
                concurrency=1,
                expected_sizes={video: {7}},
            )

        self.assertFalse(results[video]["success"])
        self.assertTrue(results[video]["put_success"])
        self.assertIn("remote size=None", results[video]["reason"])

    def test_upload_failure_reason_includes_http_status(self) -> None:
        with mock.patch.object(
            publish,
            "run_curl",
            return_value={"http_status": 403, "error": "", "returncode": 0},
        ):
            result = publish.upload_webdav_file(
                Path("video.mp4"),
                "https://example.test/dir/",
                "user",
                "password",
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "PUT failed with HTTP 403")

    def test_bitrate_targets_room_for_audio_and_container(self) -> None:
        bitrate = publish.calculate_video_bitrate_kbps(90 * 1024 * 1024, 180, 96)
        self.assertGreater(bitrate, 3_500)
        self.assertLess(bitrate, 4_100)

    def test_oversized_video_is_backed_up_and_replaced_with_compressed_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "outputs"
            output_dir.mkdir()
            source = output_dir / "KC娱乐_测试.mp4"
            source.write_bytes(b"original-large")

            def fake_compress(_source, target, _target_bytes, _git_max_bytes):
                target.write_bytes(b"small")
                return {"attempted": True, "success": True, "output_size": 5}

            with (
                mock.patch.object(
                    publish,
                    "ensure_webdav_directory",
                    return_value={"ready": True, "reason": "ready", "url": "https://example.test/dir/"},
                ),
                mock.patch.object(
                    publish,
                    "upload_webdav_file",
                    return_value={"attempted": True, "success": True, "http_status": 201},
                ),
                mock.patch.object(
                    publish,
                    "list_webdav_videos",
                    side_effect=[
                        {"success": True, "names": [], "sizes": {}},
                        {
                            "success": True,
                            "names": [source.name],
                            "sizes": {source.name: len(b"original-large")},
                        },
                    ],
                ),
                mock.patch.object(publish, "compress_video", side_effect=fake_compress),
            ):
                report = publish.process_directory(
                    output_dir=output_dir,
                    output_date="2026-07-13",
                    git_max_bytes=10,
                    compression_target_bytes=8,
                    backup_dir=root / "backup",
                    compression_work_dir=root / "tmp",
                    webdav_base_url="https://example.test/dav/",
                    webdav_root="我的坚果云/KCdesk/Ops",
                    webdav_category="KC娱乐",
                    webdav_user="user",
                    webdav_password="password",
                )

            self.assertEqual(source.read_bytes(), b"small")
            self.assertEqual((root / "backup" / source.name).read_bytes(), b"original-large")
            self.assertEqual(report["git_ready_count"], 1)
            self.assertEqual(report["webdav_uploaded_count"], 1)

    def test_git_safe_video_is_also_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "outputs"
            output_dir.mkdir()
            source = output_dir / "small.mp4"
            source.write_bytes(b"small")

            with (
                mock.patch.object(
                    publish,
                    "ensure_webdav_directory",
                    return_value={"ready": True, "reason": "ready", "url": "https://example.test/dir/"},
                ),
                mock.patch.object(
                    publish,
                    "upload_webdav_file",
                    return_value={"attempted": True, "success": True, "http_status": 201},
                ) as upload,
                mock.patch.object(
                    publish,
                    "list_webdav_videos",
                    side_effect=[
                        {"success": True, "names": [], "sizes": {}},
                        {
                            "success": True,
                            "names": [source.name],
                            "sizes": {source.name: len(b"small")},
                        },
                    ],
                ),
                mock.patch.object(publish, "compress_video") as compress,
            ):
                report = publish.process_directory(
                    output_dir=output_dir,
                    output_date="2026-07-13",
                    git_max_bytes=10,
                    compression_target_bytes=8,
                    backup_dir=root / "backup",
                    compression_work_dir=root / "tmp",
                    webdav_base_url="https://example.test/dav/",
                    webdav_root="我的坚果云/KCdesk/Ops",
                    webdav_category="KC娱乐",
                    webdav_user="user",
                    webdav_password="password",
                )

            upload.assert_called_once()
            compress.assert_not_called()
            self.assertEqual(report["git_ready_count"], 1)
            self.assertEqual(report["webdav_uploaded_count"], 1)
            self.assertEqual(report["webdav_verified_count"], 1)
            self.assertEqual(report["webdav_verified_after_attempt_count"], 1)

    def test_failed_compression_keeps_original_and_marks_git_skip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            output_dir = root / "outputs"
            output_dir.mkdir()
            source = output_dir / "large.mp4"
            source.write_bytes(b"original-large")

            with mock.patch.object(
                publish,
                "compress_video",
                return_value={"attempted": True, "success": False, "reason": "test failure"},
            ):
                report = publish.process_directory(
                    output_dir=output_dir,
                    output_date="2026-07-13",
                    git_max_bytes=10,
                    compression_target_bytes=8,
                    backup_dir=root / "backup",
                    compression_work_dir=root / "tmp",
                    webdav_base_url="https://example.test/dav/",
                    webdav_root="我的坚果云/KCdesk/Ops",
                    webdav_category="KC娱乐",
                    webdav_user="",
                    webdav_password="",
                )

            self.assertEqual(source.read_bytes(), b"original-large")
            self.assertEqual(report["git_skipped_count"], 1)
            self.assertEqual(report["files"][0]["status"], "git_skipped_original_preserved")


if __name__ == "__main__":
    unittest.main()
