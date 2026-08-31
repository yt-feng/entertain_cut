from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
import wave


ROOT = Path(__file__).resolve().parents[1]
XHS = ROOT / "xhs2vid"
sys.path.insert(0, str(XHS))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


discover = load_module("xhs_discover_test", XHS / "discover_note.py")
fetch = load_module("xhs_fetch_test", XHS / "fetch_assets.py")
batch = load_module("xhs_batch_test", XHS / "run_daily_batch.py")
renderer = load_module("xhs_renderer_test", XHS / "render_video.py")
recorder = load_module("xhs_recorder_test", XHS / "record_processed.py")


class XhsDailyTests(unittest.TestCase):
    def test_decimal_wan_counts_and_millisecond_timestamps(self) -> None:
        self.assertEqual(discover.parse_count("1.2万"), 12_000)
        self.assertEqual(fetch.parse_like_count("3.45w+"), 34_500)
        self.assertEqual(
            discover.normalize_timestamp("1788134400000"),
            1_788_134_400,
        )

    def test_voice_roster_is_unique_and_monkey_is_fast(self) -> None:
        comments = [
            {"sub_comments": [{"text": "a"}]},
            {"sub_comments": [{"text": "b"}]},
            {"sub_comments": []},
        ]
        cli, manifest = batch.voice_arguments(comments, 1)
        self.assertEqual(len(manifest), 6)
        self.assertEqual(len({item["speaker_id"] for item in manifest}), 6)
        monkey = next(item for item in manifest if item["name"] == "猴哥")
        self.assertEqual(monkey["tempo"], 1.48)
        self.assertEqual(cli.count("--segment-speaker"), 6)
        self.assertEqual(cli.count("--segment-tempo"), 6)

    def test_title_wrap_preserves_neighbor_word_and_highlight(self) -> None:
        self.assertEqual(
            renderer.split_header_title("隔壁的邻居好奇怪", "好奇怪"),
            ["隔壁的邻居", "好奇怪"],
        )
        self.assertEqual(renderer.pick_highlights("隔壁的邻居好奇怪"), ["好奇怪"])
        self.assertEqual(
            renderer.pick_highlights("不接受单休 就这样被hr说教…."),
            ["被hr说教"],
        )

    def test_record_processed_merges_without_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current = root / "state.json"
            incoming = root / "new.json"
            current.write_text(
                json.dumps({"version": 1, "items": [{"note_id": "old"}]}),
                encoding="utf-8",
            )
            incoming.write_text(
                json.dumps(
                    {
                        "items": [
                            {"note_id": "old", "title": "updated"},
                            {"note_id": "new", "title": "new"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            old_argv = sys.argv
            try:
                sys.argv = [
                    "record_processed.py",
                    "--new", str(incoming),
                    "--manifest", str(current),
                ]
                recorder.main()
            finally:
                sys.argv = old_argv
            payload = json.loads(current.read_text(encoding="utf-8"))
            self.assertEqual({item["note_id"] for item in payload["items"]}, {"old", "new"})
            self.assertEqual(len(payload["items"]), 2)

    def test_synthetic_sfx_fallback_is_portable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            original_render = renderer.RENDER
            original_sample = renderer.SAMPLE
            try:
                renderer.RENDER = Path(temporary)
                renderer.SAMPLE = Path(temporary) / "missing-reference.mp4"
                fire, camera = renderer.extract_reference_sfx()
                for path, minimum_frames in ((fire, 30_000), (camera, 10_000)):
                    self.assertTrue(path.is_file())
                    with wave.open(str(path), "rb") as handle:
                        self.assertEqual(handle.getnchannels(), 2)
                        self.assertEqual(handle.getframerate(), 44_100)
                        self.assertGreater(handle.getnframes(), minimum_frames)
            finally:
                renderer.RENDER = original_render
                renderer.SAMPLE = original_sample


if __name__ == "__main__":
    unittest.main()
