from __future__ import annotations

import importlib.util
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
