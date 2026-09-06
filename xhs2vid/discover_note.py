#!/usr/bin/env python3
"""发现今天的小红书低粉爆款笔记(TikHub app_v2 接口)。

选择逻辑:
- 多个情感类关键词, time_filter=一天内, 普通笔记, general/最多点赞 双通道
- 聚合去重后按 liked_count 排序
- 对头部候选查作者粉丝数, 优先 粉丝<=FANS_MAX 且 点赞>=LIKES_MIN 的"低粉爆款"
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from tikhub_budget import RequestBudgetExceeded, TikHubRequestBudget

ROOT = Path(__file__).resolve().parent.parent
KEY_FILE = ROOT / "api_key" / "tikhub.txt"
KEY = os.environ.get("TIKHUB_API_KEY", "").strip()
if not KEY and KEY_FILE.is_file():
    KEY = KEY_FILE.read_text(encoding="utf-8").strip()
BASE = "https://api.tikhub.io"
OUT_DIR = ROOT / "xhs2vid" / "work"

KEYWORDS = [
    "情感 扎心",
    "男人 女人 真相",
    "恋爱脑",
    "婚姻 现实",
    "情感语录",
    "两性关系 人间清醒",
    "分手 前任",
    "相亲 奇葩",
]
SORTS = ["general", "最多点赞"]
PAGES = 2
LIKES_MIN = 200
FANS_MAX = 20000
TOP_AUTHOR_CHECK = 12
MAX_ATTEMPTS = 3
BUDGET: TikHubRequestBudget | None = None

client = httpx.Client(
    base_url=BASE,
    headers={
        **({"Authorization": f"Bearer {KEY}"} if KEY else {}),
        "User-Agent": "kc-entertain-xhs2vid/1.0",
    },
    timeout=60,
)


def parse_count(value: object) -> int:
    """Parse TikHub counters such as 1234, ``1.2万`` and ``3w``."""
    text = str(value or "0").strip().lower().replace(",", "")
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
    timestamp = parse_count(value)
    while timestamp > 10_000_000_000:
        timestamp //= 1000
    return timestamp


def author_lookup_pool(fresh: list[dict]) -> list[dict]:
    """Return only notes that can still satisfy the viral-like threshold.

    Fan lookups are paid TikHub calls. Filtering here must happen before the
    ``TOP_AUTHOR_CHECK`` slice; otherwise same-day posts below ``LIKES_MIN``
    can consume every lookup slot and hide valid recent fallback candidates.
    """
    return [note for note in fresh if note["liked_count"] >= LIKES_MIN]


def api_get(path: str, params: dict) -> dict:
    last_exc: Exception | None = None
    for attempt in range(MAX_ATTEMPTS):
        try:
            if BUDGET is None:
                raise RuntimeError("TikHub request budget is not initialized")
            number = BUDGET.consume(f"GET {path}")
            print(f"[budget] TikHub attempt {number}/{BUDGET.limit}: {path}")
            resp = client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except RequestBudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def search_all() -> dict[str, dict]:
    notes: dict[str, dict] = {}
    for kw in KEYWORDS:
        for sort_type in SORTS:
            search_id = ""
            session_id = ""
            for page in range(1, PAGES + 1):
                params = {
                    "keyword": kw,
                    "page": page,
                    "sort_type": sort_type,
                    "note_type": "普通笔记",
                    "time_filter": "一天内",
                }
                if search_id:
                    params["search_id"] = search_id
                    params["search_session_id"] = session_id
                try:
                    data = api_get("/api/v1/xiaohongshu/app_v2/search_notes", params)
                except RequestBudgetExceeded:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] search {kw}/{sort_type} p{page}: {exc}")
                    continue
                payload = data.get("data") or {}
                search_id = payload.get("search_id") or search_id
                session_id = payload.get("search_session_id") or session_id
                items = (payload.get("data") or {}).get("items") or []
                got = 0
                for item in items:
                    note = item.get("note") or {}
                    nid = note.get("id")
                    if not nid or note.get("type") != "normal":
                        continue
                    images = note.get("images_list") or []
                    cover = ""
                    if images:
                        cover = (
                            images[0].get("url_size_large")
                            or images[0].get("url")
                            or ""
                        )
                    user = note.get("user") or {}
                    rec = {
                        "note_id": nid,
                        "title": note.get("title") or "",
                        "desc": note.get("desc") or "",
                        "liked_count": parse_count(note.get("liked_count")),
                        "comments_count": parse_count(note.get("comments_count")),
                        "collected_count": parse_count(note.get("collected_count")),
                        "shared_count": parse_count(note.get("shared_count")),
                        "timestamp": normalize_timestamp(note.get("timestamp")),
                        "cover_url": cover,
                        "images_count": len(images),
                        "author_id": user.get("userid") or "",
                        "author_name": user.get("nickname") or "",
                        "keyword": kw,
                    }
                    prev = notes.get(nid)
                    if not prev or rec["liked_count"] > prev["liked_count"]:
                        notes[nid] = rec
                    got += 1
                print(f"[info] {kw}/{sort_type} p{page}: {got} notes")
                time.sleep(0.4)
    return notes


def author_fans(user_id: str) -> int:
    if not user_id:
        return -1
    try:
        data = api_get(
            "/api/v1/xiaohongshu/app_v2/get_user_info", {"user_id": user_id}
        )
    except RequestBudgetExceeded:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] user {user_id}: {exc}")
        return -1
    import re

    blob = json.dumps(data.get("data") or {}, ensure_ascii=False)
    m = re.search(r'"fans"\s*:\s*"?(\d+)', blob)
    return int(m.group(1)) if m else -1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "out_dir", nargs="?", type=Path, default=ROOT / "xhs2vid" / "work"
    )
    parser.add_argument("--pages", type=int, default=PAGES)
    parser.add_argument("--top-author-check", type=int, default=TOP_AUTHOR_CHECK)
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    parser.add_argument("--request-limit", type=int, default=90)
    parser.add_argument(
        "--budget-file",
        type=Path,
        help="共享 TikHub 请求计数文件；默认写在 out_dir。",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1,
        help="输出多少个排好序的低粉爆款候选；chosen_note.json 仍写第一条。",
    )
    parser.add_argument(
        "--same-day",
        action="store_true",
        help="只保留北京时间当天发布的帖子。",
    )
    parser.add_argument(
        "--prefer-same-day",
        action="store_true",
        help="目标业务日优先；不足时由最近 26 小时候选补足。",
    )
    parser.add_argument(
        "--target-date",
        help="业务日期 YYYY-MM-DD；默认使用当前北京时间日期。",
    )
    parser.add_argument("--strict-low-fan", action="store_true")
    parser.add_argument(
        "--exclude-note-id",
        action="append",
        default=[],
        help="排除已经做过的笔记；可重复传入。",
    )
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords",
        help="覆盖默认搜索词；可重复传入，最多 8 个。",
    )
    args = parser.parse_args()
    if not 1 <= args.pages <= 3:
        parser.error("--pages must be between 1 and 3")
    if not 1 <= args.top_author_check <= 20:
        parser.error("--top-author-check must be between 1 and 20")
    if not 1 <= args.max_attempts <= 3:
        parser.error("--max-attempts must be between 1 and 3")
    if not 1 <= args.request_limit < 100:
        parser.error("--request-limit must be between 1 and 99")
    if not 1 <= args.limit <= 20:
        parser.error("--limit must be between 1 and 20")
    if args.keywords is not None:
        args.keywords = [keyword.strip() for keyword in args.keywords if keyword.strip()]
        if not 1 <= len(args.keywords) <= 8:
            parser.error("--keyword must provide between 1 and 8 non-empty values")
    if args.same_day and args.prefer_same_day:
        parser.error("--same-day and --prefer-same-day cannot be used together")
    if args.target_date:
        try:
            parsed_target = datetime.fromisoformat(args.target_date).date().isoformat()
        except ValueError:
            parser.error("--target-date must use YYYY-MM-DD")
        if parsed_target != args.target_date:
            parser.error("--target-date must use YYYY-MM-DD")
    return args


def main() -> None:
    global BUDGET, KEYWORDS, MAX_ATTEMPTS, OUT_DIR, PAGES, TOP_AUTHOR_CHECK
    args = parse_args()
    OUT_DIR = args.out_dir.expanduser().resolve()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PAGES = args.pages
    TOP_AUTHOR_CHECK = args.top_author_check
    MAX_ATTEMPTS = args.max_attempts
    if args.keywords is not None:
        KEYWORDS = args.keywords
    if not KEY:
        raise SystemExit(
            "TikHub API key missing: set TIKHUB_API_KEY or create api_key/tikhub.txt"
        )
    BUDGET = TikHubRequestBudget(
        (args.budget_file or OUT_DIR / "tikhub_request_budget.json")
        .expanduser()
        .resolve(),
        limit=args.request_limit,
    )

    now = time.time()
    beijing = ZoneInfo("Asia/Shanghai")
    today = (
        datetime.fromisoformat(args.target_date).date()
        if args.target_date
        else datetime.fromtimestamp(now, beijing).date()
    )
    notes = search_all()
    fresh = [
        n
        for n in notes.values()
        if n["timestamp"] > now - 26 * 3600
        and (
            not args.same_day
            or datetime.fromtimestamp(n["timestamp"], beijing).date() == today
        )
        and n["comments_count"] >= 3
        and n["title"]
        and n["cover_url"]
        and n["author_id"]
        and n["note_id"] not in set(args.exclude_note_id)
    ]
    fresh.sort(
        key=lambda n: (
            int(
                args.prefer_same_day
                and datetime.fromtimestamp(n["timestamp"], beijing).date() == today
            ),
            n["liked_count"],
        ),
        reverse=True,
    )
    eligible_for_lookup = author_lookup_pool(fresh)
    print(
        f"[info] total {len(notes)} notes, fresh with comments: {len(fresh)}, "
        f"viral-like eligible: {len(eligible_for_lookup)}"
    )

    candidates = []
    fans_by_author: dict[str, int] = {}
    for rec in eligible_for_lookup[:TOP_AUTHOR_CHECK]:
        author_id = rec["author_id"]
        if author_id not in fans_by_author:
            fans_by_author[author_id] = author_fans(author_id)
        fans = fans_by_author[author_id]
        rec["author_fans"] = fans
        candidates.append(rec)
        print(
            f"  {rec['liked_count']:>6} 赞 | {rec['comments_count']:>5} 评 | "
            f"粉丝 {fans:>7} | {rec['title'][:30]} | {rec['note_id']}"
        )
        time.sleep(0.4)

    low_fan = [
        candidate
        for candidate in candidates
        if 0 <= candidate["author_fans"] <= FANS_MAX
        and candidate["liked_count"] >= LIKES_MIN
    ]
    if args.strict_low_fan and not low_fan:
        raise SystemExit(
            "no strict low-fan viral candidate found "
            f"(fans <= {FANS_MAX}, likes >= {LIKES_MIN})"
        )
    pool = low_fan or [c for c in candidates if c["liked_count"] >= LIKES_MIN] or candidates
    def viral_score(candidate: dict) -> float:
        likes = candidate["liked_count"]
        comments = candidate["comments_count"]
        fans = max(candidate.get("author_fans", 0), 50)
        age_hours = max(0.0, (now - candidate["timestamp"]) / 3600)
        return (
            math.log1p(likes)
            + 2.0 * math.log1p(likes / fans)
            + 0.75 * math.log1p(comments)
            - 0.03 * age_hours
        )

    for candidate in pool:
        candidate["beijing_same_day"] = (
            datetime.fromtimestamp(candidate["timestamp"], beijing).date() == today
        )
        candidate["viral_score"] = round(viral_score(candidate), 6)
    pool.sort(
        key=lambda c: (
            int(args.prefer_same_day and c["beijing_same_day"]),
            c["viral_score"],
            c["liked_count"],
            c["comments_count"],
            c["timestamp"],
            c["note_id"],
        ),
        reverse=True,
    )
    if not pool:
        raise SystemExit("no candidate found")
    # Prefer different creators across the daily batch, then fill any remaining
    # slots from the ranked pool. This keeps five outputs visually/content-wise varied.
    selected: list[dict] = []
    selected_ids: set[str] = set()
    selected_authors: set[str] = set()
    for candidate in pool:
        if candidate["author_id"] in selected_authors:
            continue
        selected.append(candidate)
        selected_ids.add(candidate["note_id"])
        selected_authors.add(candidate["author_id"])
        if len(selected) >= args.limit:
            break
    for candidate in pool:
        if len(selected) >= args.limit:
            break
        if candidate["note_id"] not in selected_ids:
            selected.append(candidate)
            selected_ids.add(candidate["note_id"])
    chosen = selected[0]
    (OUT_DIR / "candidates.json").write_text(
        json.dumps(candidates, ensure_ascii=False, indent=2)
    )
    (OUT_DIR / "chosen_note.json").write_text(
        json.dumps(chosen, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (OUT_DIR / "selected_notes.json").write_text(
        json.dumps(selected, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"\n[selected] {len(selected)}/{args.limit}: "
        + ", ".join(note["note_id"] for note in selected)
    )
    print("[chosen]", json.dumps(chosen, ensure_ascii=False, indent=2))
    print("[budget]", json.dumps(BUDGET.snapshot(), ensure_ascii=False))


if __name__ == "__main__":
    main()
