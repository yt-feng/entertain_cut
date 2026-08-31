#!/usr/bin/env python3
"""抓取选中笔记的封面图、最热评论和对应子评论(TikHub app_v2 接口)。"""
from __future__ import annotations

import argparse
from io import BytesIO
import json
import os
import re
import time
from pathlib import Path

import httpx
from PIL import Image

from tikhub_budget import RequestBudgetExceeded, TikHubRequestBudget

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "api_key" / "tikhub.txt"
KEY = os.environ.get("TIKHUB_API_KEY", "").strip()
if not KEY and KEY_FILE.is_file():
    KEY = KEY_FILE.read_text(encoding="utf-8").strip()
WORK = ROOT / "xhs2vid" / "work"
NOTE: dict = {}
MAX_ATTEMPTS = 3
BUDGET: TikHubRequestBudget | None = None

client = httpx.Client(
    headers={
        **({"Authorization": f"Bearer {KEY}"} if KEY else {}),
        "User-Agent": "kc-entertain-xhs2vid/1.0",
    },
    timeout=60,
)
cover_client = httpx.Client(
    headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.xiaohongshu.com/",
    },
    timeout=60,
    follow_redirects=True,
)


def api_get(path: str, params: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            if BUDGET is None:
                raise RuntimeError("TikHub request budget is not initialized")
            number = BUDGET.consume(f"GET {path}")
            print(f"[budget] TikHub attempt {number}/{BUDGET.limit}: {path}")
            response = client.get(f"https://api.tikhub.io{path}", params=params)
            response.raise_for_status()
            return response.json()
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def extract_comment_items(data: dict) -> list[dict]:
    payload = data.get("data") or {}
    inner = payload.get("data") or payload
    comments = None
    for key in ("comments", "comment_list", "items"):
        if isinstance(inner, dict) and isinstance(inner.get(key), list):
            comments = inner[key]
            break
    if comments is None and isinstance(payload, dict):
        for key in ("comments", "comment_list"):
            if isinstance(payload.get(key), list):
                comments = payload[key]
                break
    return [item for item in (comments or []) if isinstance(item, dict)]


def parse_like_count(value: object) -> int:
    text = str(value or 0).strip().lower().replace(",", "").replace("+", "")
    multiplier = 1
    if text.endswith("万") or text.endswith("w"):
        multiplier = 10_000
        text = text[:-1]
    try:
        return max(0, int(float(text) * multiplier))
    except ValueError:
        match = re.search(r"\d+(?:\.\d+)?", text)
        return max(0, int(float(match.group(0)) * multiplier)) if match else 0


def normalize_timestamp(value: object) -> int:
    timestamp = parse_like_count(value)
    while timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp


def normalize_subcomment(item: dict) -> dict:
    user = item.get("user") or item.get("user_info") or {}
    return {
        "comment_id": str(item.get("id") or item.get("comment_id") or ""),
        "text": (item.get("content") or item.get("text") or "").strip(),
        "like_count": parse_like_count(
            item.get("like_count", item.get("liked_count", 0))
        ),
        "time": normalize_timestamp(item.get("time") or item.get("create_time")),
        "ip_location": item.get("ip_location") or "",
        "nickname": user.get("nickname") or user.get("name") or "",
    }


def normalize_comment(item: dict) -> dict:
    user = item.get("user") or item.get("user_info") or {}
    sub_comments = [
        normalize_subcomment(sub)
        for sub in (item.get("sub_comments") or item.get("sub_comment_list") or [])
        if isinstance(sub, dict)
    ]
    return {
        "comment_id": str(item.get("id") or item.get("comment_id") or ""),
        "text": (item.get("content") or item.get("text") or "").strip(),
        "like_count": parse_like_count(
            item.get("like_count", item.get("liked_count", 0))
        ),
        "time": normalize_timestamp(item.get("time") or item.get("create_time")),
        "ip_location": item.get("ip_location") or "",
        "sub_comment_count": int(item.get("sub_comment_count") or 0),
        "nickname": user.get("nickname") or user.get("name") or "",
        "sub_comments": [sub for sub in sub_comments if sub["text"]],
    }


def fetch_comments(note_id: str) -> list[dict]:
    data = api_get(
        "/api/v1/xiaohongshu/app_v2/get_note_comments",
        {
            "note_id": note_id,
            "cursor": "",
            "index": 0,
            "pageArea": "UNFOLDED",
            "sort_strategy": "like_count",
        },
    )
    (WORK / "comments_raw.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    comments = extract_comment_items(data)
    if not comments:
        raise SystemExit("no comments found in TikHub response")
    out = [normalize_comment(item) for item in comments]
    out = [comment for comment in out if comment["text"]]
    out.sort(key=lambda comment: comment["like_count"], reverse=True)
    return out


def load_cached_comments() -> list[dict]:
    data = json.loads((WORK / "comments_raw.json").read_text(encoding="utf-8"))
    out = [normalize_comment(item) for item in extract_comment_items(data)]
    out = [comment for comment in out if comment["text"]]
    out.sort(key=lambda comment: comment["like_count"], reverse=True)
    return out


def fetch_missing_subcomments(
    note_id: str, comments: list[dict], *, max_calls: int
) -> None:
    calls = 0
    for comment in comments:
        if comment["sub_comments"] or comment["sub_comment_count"] <= 0:
            continue
        if calls >= max_calls:
            break
        comment_id = comment.get("comment_id")
        if not comment_id:
            continue
        try:
            data = api_get(
                "/api/v1/xiaohongshu/app_v2/get_note_sub_comments",
                {
                    "note_id": note_id,
                    "comment_id": comment_id,
                    "cursor": "",
                    "index": 1,
                },
            )
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] subcomments {comment_id}: {exc}")
            continue
        calls += 1
        (WORK / f"subcomments_{comment_id}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        comment["sub_comments"] = [
            normalize_subcomment(item) for item in extract_comment_items(data)
        ]
        comment["sub_comments"] = [
            item for item in comment["sub_comments"] if item["text"]
        ]


def select_comment_threads(comments: list[dict], *, limit: int) -> list[dict]:
    """优先选有真实回复且长度适合竖屏卡片的高赞一级评论。"""
    all_with_replies = [
        comment
        for comment in comments
        if comment["sub_comments"] or comment["sub_comment_count"] > 0
    ]
    readable = [
        comment
        for comment in all_with_replies
        if 8 <= len(comment["text"]) <= 120
        and any(2 <= len(reply["text"]) <= 90 for reply in comment["sub_comments"])
    ]
    readable.sort(key=lambda comment: comment["like_count"], reverse=True)
    remaining_with_replies = [
        comment for comment in all_with_replies if comment not in readable
    ]
    without_replies = [
        comment for comment in comments if comment not in all_with_replies
    ]
    return (readable + remaining_with_replies + without_replies)[:limit]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "work_dir", nargs="?", type=Path, default=ROOT / "xhs2vid" / "work"
    )
    parser.add_argument("--max-attempts", type=int, default=2)
    parser.add_argument("--request-limit", type=int, default=90)
    parser.add_argument(
        "--budget-file",
        type=Path,
        help="共享 TikHub 请求计数文件；默认写在 work_dir。",
    )
    parser.add_argument("--max-subcomment-calls", type=int, default=3)
    parser.add_argument(
        "--reuse-comments-cache",
        action="store_true",
        help="只从 comments_raw.json 重新筛选，不再调用评论或子评论 API。",
    )
    args = parser.parse_args()
    if not 1 <= args.max_attempts <= 3:
        parser.error("--max-attempts must be between 1 and 3")
    if not 1 <= args.request_limit < 100:
        parser.error("--request-limit must be between 1 and 99")
    if not 0 <= args.max_subcomment_calls <= 3:
        parser.error("--max-subcomment-calls must be between 0 and 3")
    return args


def main() -> None:
    global BUDGET, MAX_ATTEMPTS, NOTE, WORK
    args = parse_args()
    WORK = args.work_dir.expanduser().resolve()
    NOTE = json.loads((WORK / "chosen_note.json").read_text(encoding="utf-8"))
    MAX_ATTEMPTS = args.max_attempts
    if not KEY:
        raise SystemExit(
            "TikHub API key missing: set TIKHUB_API_KEY or create api_key/tikhub.txt"
        )
    BUDGET = TikHubRequestBudget(
        (args.budget_file or WORK / "tikhub_request_budget.json")
        .expanduser()
        .resolve(),
        limit=args.request_limit,
    )

    if args.reuse_comments_cache and (WORK / "cover.png").is_file():
        print(f"[info] cover reused: {WORK / 'cover.png'}")
    else:
        cover_url = NOTE["cover_url"]
        image_response = None
        last_cover_error: Exception | None = None
        for attempt in range(1, 3):
            try:
                image_response = cover_client.get(cover_url)
                image_response.raise_for_status()
                break
            except Exception as exc:  # noqa: BLE001
                last_cover_error = exc
                image_response = None
                if attempt < 2:
                    time.sleep(1.0)
        if image_response is None:
            raise RuntimeError(f"cover download failed: {last_cover_error}")
        cover_path = WORK / "cover.webp"
        cover_path.write_bytes(image_response.content)
        with Image.open(BytesIO(image_response.content)) as cover:
            cover.convert("RGB").save(WORK / "cover.png")
        print(f"[info] cover saved: {cover_path} ({len(image_response.content)} bytes)")

    comments = load_cached_comments() if args.reuse_comments_cache else fetch_comments(NOTE["note_id"])
    top3 = select_comment_threads(comments, limit=3)
    fetch_missing_subcomments(
        NOTE["note_id"],
        top3,
        max_calls=0 if args.reuse_comments_cache else args.max_subcomment_calls,
    )
    # A newly fetched reply can promote a previously weak thread. Re-rank after
    # enrichment so the renderer gets the best readable parent/reply pairs.
    top3 = select_comment_threads(comments, limit=3)
    (WORK / "top_comments.json").write_text(
        json.dumps(top3, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for index, comment in enumerate(top3, 1):
        print(
            f"  No.{index} [{comment['like_count']}赞/"
            f"{len(comment['sub_comments'])}条已取子评] "
            f"{comment['ip_location']} | {comment['text'][:60]}"
        )
    print("[budget]", json.dumps(BUDGET.snapshot(), ensure_ascii=False))


if __name__ == "__main__":
    main()
