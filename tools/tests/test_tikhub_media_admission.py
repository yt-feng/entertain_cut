from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import httpx


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_douyin_tikhub_daily.py"
SPEC = importlib.util.spec_from_file_location("tikhub_media_admission", MODULE_PATH)
assert SPEC and SPEC.loader
tikhub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tikhub)


class MediaCandidateAdmissionTests(unittest.TestCase):
    def aweme(self, **updates):
        result = {
            "aweme_id": "test", "desc": "明星舞台", "statistics": {"digg_count": 20000},
            "create_time": int(dt.datetime.now(dt.timezone.utc).timestamp()) - 60,
            "video": {"play_addr": {"url_list": ["https://cdn.example/opaque-playback"]}},
        }
        result.update(updates)
        return result

    def select(self, items, limit=1):
        return tikhub.select_candidates(items, limit, 24, 10000, 1000, 400, 0, 60, 300, "明星", "", set(), {"terms": []})

    def test_note_and_music_do_not_consume_selection_budget(self):
        note = tikhub.normalize_aweme(self.aweme(
            aweme_id="note", share_url="https://www.iesdouyin.com/share/note/123/?region=CN",
            video={"play_addr": {"url_list": ["https://cdn.example/music.MP3?token=abc"]}},
        ), "明星", Path("."))
        video = tikhub.normalize_aweme(self.aweme(aweme_id="video"), "明星", Path("."))
        video["like_count"] = 1000
        self.assertEqual(note["download_urls"], [])
        self.assertEqual([item["aweme_id"] for item in self.select([note, video])], ["video"])

    def test_explicit_note_with_opaque_music_and_image_type_are_excluded(self):
        for updates in (
            {"share_url": "https://www.douyin.com/note/123"},
            {"aweme_type": 68},
            {"image_post_info": {"images": [{"url_list": ["https://cdn.example/pic.jpg"]}]}},
        ):
            with self.subTest(updates=updates):
                item = tikhub.normalize_aweme(self.aweme(**updates), "明星", Path("."))
                self.assertEqual(self.select([item]), [])

    def test_missing_video_metadata_and_cover_images_remain_eligible(self):
        for updates in (
            {"video": {}},
            {"aweme_type": 999},
            {"images": [{"url_list": ["https://cdn.example/thumbnail.jpg"]}]},
        ):
            with self.subTest(updates=updates):
                item = tikhub.normalize_aweme(self.aweme(**updates), "明星", Path("."))
                self.assertEqual(len(self.select([item])), 1)

    def test_audio_urls_never_enter_extracted_video_urls(self):
        aweme = self.aweme(video={"play_addr": {"url_list": [
            "https://cdn.example/music.mp3?token=abc", "https://cdn.example/play?mime_type=audio_mp4",
            "https://cdn.example/cover.jpg", "https://cdn.example/opaque-playback",
            "https://cdn.example/video.mp4?name=music.mp3",
        ]}})
        self.assertEqual(tikhub.extract_video_urls(aweme), [
            "https://cdn.example/opaque-playback", "https://cdn.example/video.mp4?name=music.mp3",
        ])

    def test_audio_only_legacy_candidate_is_excluded_before_ranking(self):
        item = tikhub.normalize_aweme(self.aweme(), "明星", Path("."))
        item["download_urls"] = ["https://cdn.example/music.m4a"]
        self.assertEqual(self.select([item]), [])

    def test_discovery_excludes_notes_before_candidate_and_editorial_budgets(self):
        args = SimpleNamespace(
            max_search_requests=1, douyin_search_requests=1, request_timeout_seconds=45,
            pages_per_keyword=1, recent_hours=24, tikhub_filter_duration="0", search_retry_attempts=1,
        )
        response = {"request_count": 1, "endpoint": "general_v1", "data": {"data": [
            {"aweme_info": self.aweme(aweme_id="note", share_url="https://www.douyin.com/note/123")},
            {"aweme_info": self.aweme(aweme_id="video")},
        ]}}
        info = {"errors": []}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tikhub, "request_tikhub_search", return_value=response):
            items = tikhub.fetch_candidates(args, "test-key", ["明星"], Path(directory), info)
        self.assertEqual([item["aweme_id"] for item in items], ["video"])
        self.assertEqual({item["aweme_id"] for item in info["media_candidate_rejections"]}, {"note"})


class MediaValidationBoundsTests(unittest.TestCase):
    def probe_result(self, **stream_updates):
        stream = {"index": 0, "codec_type": "video", "width": 16, "height": 16, "duration": "1.0"}
        stream.update(stream_updates)
        return subprocess.CompletedProcess([], 0, json.dumps({"streams": [stream]}), "")

    def test_attached_album_art_does_not_make_audio_a_video(self):
        with mock.patch.object(tikhub.subprocess, "run", return_value=self.probe_result(disposition={"attached_pic": 1})) as run:
            with self.assertRaisesRegex(ValueError, "no video stream"):
                tikhub.validate_downloaded_video(Path("audio-with-cover.mp4"))
        self.assertEqual(run.call_count, 1)

    def test_decode_failure_rejects_a_claimed_video_stream(self):
        with mock.patch.object(tikhub.subprocess, "run", side_effect=[
            self.probe_result(), subprocess.CompletedProcess([], 1, "", "invalid frame"),
        ]):
            with self.assertRaisesRegex(ValueError, "frame decode failed"):
                tikhub.validate_downloaded_video(Path("broken.mp4"))

    def test_successful_decoder_exit_without_frames_is_rejected(self):
        with mock.patch.object(tikhub.subprocess, "run", side_effect=[
            self.probe_result(), subprocess.CompletedProcess([], 0, "#format: frame checksums\n", ""),
        ]):
            with self.assertRaisesRegex(ValueError, "frame decode failed"):
                tikhub.validate_downloaded_video(Path("empty-video.mp4"))

    def test_probe_and_decode_are_bounded(self):
        for stage in ("probe", "decode"):
            with self.subTest(stage=stage):
                timeout = subprocess.TimeoutExpired(stage, 20)
                side_effects = [timeout] if stage == "probe" else [self.probe_result(), timeout]
                with mock.patch.object(tikhub.subprocess, "run", side_effect=side_effects) as run:
                    with self.assertRaisesRegex(ValueError, "validation failed"):
                        tikhub.validate_downloaded_video(Path("clip.mp4"))
                for call in run.call_args_list:
                    self.assertEqual(call.kwargs["timeout"], tikhub.MEDIA_VALIDATION_TIMEOUT_SECONDS)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg and ffprobe required")
class DownloadedMediaIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.video = Path(cls.directory.name) / "video.mp4"
        cls.audio = Path(cls.directory.name) / "audio.mp3"
        for command in (
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=blue:s=32x32:r=10", "-t", "0.3", "-c:v", "mpeg4", str(cls.video)],
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "0.3", str(cls.audio)],
        ):
            subprocess.run(command, check=True, capture_output=True, timeout=20)
        cls.video_bytes = cls.video.read_bytes()
        cls.audio_bytes = cls.audio.read_bytes()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_actual_video_is_decoded_and_mislabeled_audio_is_rejected(self):
        result = tikhub.validate_downloaded_video(self.video)
        self.assertEqual(result["decoded_frames"], 1)
        self.assertEqual((result["width"], result["height"]), (32, 32))
        with tempfile.TemporaryDirectory() as directory:
            disguised = Path(directory) / "pretend.mp4"
            disguised.write_bytes(self.audio_bytes)
            with self.assertRaisesRegex(ValueError, "no video stream"):
                tikhub.validate_downloaded_video(disguised)

    def args(self):
        return SimpleNamespace(limit=1, download_reserve_count=0, download_max_urls=3, download_timeout_seconds=30, yt_dlp_timeout_seconds=30)

    def test_invalid_first_cdn_falls_through_to_valid_second_cdn(self):
        requests = []
        def respond(request):
            requests.append(str(request.url))
            return httpx.Response(200, content=self.audio_bytes if request.url.path == "/first" else self.video_bytes)
        client = httpx.Client(transport=httpx.MockTransport(respond))
        item = {"aweme_id": "same", "download_urls": ["https://cdn.example/first", "https://cdn.example/second"]}
        info = {"errors": []}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tikhub.httpx, "Client", return_value=client):
            root = Path(directory)
            downloaded = tikhub.download_selected(self.args(), [item], root, root / "selected", info)
            selected_files = list((root / "selected").glob("*.mp4"))
            self.assertEqual(len(selected_files), 1)
            self.assertEqual(selected_files[0].read_bytes(), self.video_bytes)
            self.assertEqual(list(root.glob("*.part")), [])
        self.assertEqual(downloaded, {"same"})
        self.assertEqual(len(requests), 2)
        self.assertEqual([item["status"] for item in info["tikhub_download"]], ["failed", "downloaded"])

    def test_audio_url_is_skipped_without_consuming_cdn_attempt(self):
        client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=self.video_bytes)))
        item = {"aweme_id": "video", "download_urls": ["https://cdn.example/music.mp3", "https://cdn.example/opaque"]}
        args = self.args()
        args.download_max_urls = 1
        info = {"errors": []}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tikhub.httpx, "Client", return_value=client):
            root = Path(directory)
            downloaded = tikhub.download_selected(args, [item], root, root / "selected", info)
        self.assertEqual(downloaded, {"video"})
        self.assertEqual([item["status"] for item in info["tikhub_download"]], ["downloaded"])

    def test_failed_cdn_and_audio_ytdlp_fall_through_to_next_candidate(self):
        client = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=self.audio_bytes if request.url.path == "/bad" else self.video_bytes)
        ))
        selected = [
            {"aweme_id": "bad", "url": "https://www.douyin.com/video/1", "download_urls": ["https://cdn.example/bad"]},
            {"aweme_id": "good", "download_urls": ["https://cdn.example/good"]},
        ]
        original_run = subprocess.run
        def run(command, **kwargs):
            if "yt_dlp" in command:
                Path(command[command.index("--output") + 1]).write_bytes(self.audio_bytes)
                return subprocess.CompletedProcess(command, 0, "", "")
            return original_run(command, **kwargs)
        info = {"errors": []}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tikhub.httpx, "Client", return_value=client), mock.patch.object(tikhub.subprocess, "run", side_effect=run):
            root = Path(directory)
            downloaded = tikhub.download_selected(self.args(), selected, root, root / "selected", info)
            files = list((root / "selected").glob("*.mp4"))
            self.assertEqual([path.name for path in files], ["02_douyin_good.mp4"])
            self.assertFalse((root / "01_douyin_bad.mp4").exists())
        self.assertEqual(downloaded, {"good"})
        self.assertEqual([item["status"] for item in info["tikhub_download"]], ["failed", "failed", "downloaded"])

    def test_ytdlp_valid_output_passes_same_validation(self):
        original_run = subprocess.run
        def run(command, **kwargs):
            if "yt_dlp" in command:
                Path(command[command.index("--output") + 1]).write_bytes(self.video_bytes)
                return subprocess.CompletedProcess(command, 0, "", "")
            return original_run(command, **kwargs)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(tikhub.subprocess, "run", side_effect=run):
            result = tikhub.download_page_video("https://www.douyin.com/video/1", Path(directory) / "clip.mp4", timeout_seconds=30)
        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(result["media_validation"]["decoded_frames"], 1)


if __name__ == "__main__":
    unittest.main()
