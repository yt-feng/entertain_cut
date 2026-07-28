from __future__ import annotations

import datetime as dt
import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_douyin_entertain_free.py"
SPEC = importlib.util.spec_from_file_location("run_douyin_entertain_free", MODULE_PATH)
assert SPEC and SPEC.loader
free = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(free)


class FreeCandidateSelectionTests(unittest.TestCase):
    def test_filters_old_unrelated_low_like_and_overlong_candidates(self) -> None:
        now = int(dt.datetime.now(dt.timezone.utc).timestamp())
        candidates = [
            {
                "aweme_id": "good",
                "title": "杨紫综艺现场",
                "author": "娱乐现场",
                "like_count": 2_000,
                "duration_ms": 60_000,
                "create_time": now - 60,
            },
            {
                "aweme_id": "old",
                "title": "杨紫综艺现场",
                "author": "娱乐现场",
                "like_count": 20_000,
                "duration_ms": 60_000,
                "create_time": now - 90_000,
            },
            {
                "aweme_id": "unrelated",
                "title": "导师讲数学题",
                "author": "学习课堂",
                "source_keyword": "娱乐 明星",
                "like_count": 20_000,
                "duration_ms": 60_000,
                "create_time": now - 60,
            },
            {
                "aweme_id": "low",
                "title": "杨紫综艺现场",
                "author": "娱乐现场",
                "like_count": 999,
                "duration_ms": 60_000,
                "create_time": now - 60,
            },
            {
                "aweme_id": "long",
                "title": "杨紫综艺现场",
                "author": "娱乐现场",
                "like_count": 20_000,
                "duration_ms": 301_000,
                "create_time": now - 60,
            },
        ]

        selected = free.select_candidates(
            candidates,
            limit=5,
            recent_hours=24,
            primary_min_likes=10_000,
            fallback_min_likes=1_000,
            emergency_min_likes=1_000,
            max_duration_seconds=300,
            must_include_terms="娱乐,明星,综艺,杨紫",
        )

        self.assertEqual([item["aweme_id"] for item in selected], ["good"])


if __name__ == "__main__":
    unittest.main()
