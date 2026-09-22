#!/usr/bin/env python3
"""发现今天的小红书低粉爆款笔记(TikHub app_v2/web_v3/web_v2 接口)。

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
HOT_TERMS: list[str] = []
ACCESS_BLOCKING_STATUSES = frozenset({401, 402, 403, 429})
ACCESS_BLOCKED: "TikHubAccessBlocked | None" = None
ACTIVE_SEARCH_ENDPOINT: str | None = None

SEARCH_ENDPOINTS = (
    "/api/v1/xiaohongshu/app_v2/search_notes",
    "/api/v1/xiaohongshu/web_v3/fetch_search_notes",
    "/api/v1/xiaohongshu/web_v2/fetch_search_notes",
)
USER_ENDPOINTS = (
    "/api/v1/xiaohongshu/app_v2/get_user_info",
    "/api/v1/xiaohongshu/web_v3/fetch_user_info",
    "/api/v1/xiaohongshu/web_v2/fetch_user_info",
)


class TikHubAccessBlocked(RuntimeError):
    """A run-wide TikHub condition that should not be retried per candidate."""

    def __init__(self, status_code: int, path: str) -> None:
        self.status_code = status_code
        self.path = path
        super().__init__(f"TikHub access blocked with HTTP {status_code} at {path}")


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


def api_get(path: str, params: dict, *, ignore_access_block: bool = False) -> dict:
    global ACCESS_BLOCKED
    if ACCESS_BLOCKED is not None and not ignore_access_block:
        raise ACCESS_BLOCKED
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
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            status_code = exc.response.status_code
            if status_code in ACCESS_BLOCKING_STATUSES:
                blocked = TikHubAccessBlocked(status_code, path)
                if not ignore_access_block:
                    ACCESS_BLOCKED = blocked
                raise blocked from exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(1.5 * (attempt + 1))
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < MAX_ATTEMPTS:
                time.sleep(1.5 * (attempt + 1))
    raise last_exc  # type: ignore[misc]


def search_params(
    path: str,
    keyword: str,
    page: int,
    sort_type: str,
    *,
    search_id: str = "",
    session_id: str = "",
) -> dict:
    """Build the parameter names used by each documented TikHub surface."""
    if "/web_v3/" in path:
        params = {
            "keyword": keyword,
            "page": page,
            "sort": "popularity_descending" if sort_type != "general" else "general",
            "note_type": "normal",
        }
        return params
    if "/web_v2/" in path:
        return {
            "keywords": keyword,
            "page": page,
            "sort_type": "popularity_descending" if sort_type != "general" else "general",
            "note_type": "normal",
        }
    params = {
        "keyword": keyword,
        "page": page,
        "sort_type": sort_type,
        "note_type": "普通笔记",
        "time_filter": "一天内",
    }
    if search_id:
        params["search_id"] = search_id
    if session_id:
        params["search_session_id"] = session_id
    return params


def search_notes(
    keyword: str,
    page: int,
    sort_type: str,
    *,
    search_id: str = "",
    session_id: str = "",
) -> tuple[dict, str]:
    """Use the first working XHS surface and remember it for this run.

    A 402 on app_v2 is not retried three times and is not allowed to abort the
    run before the documented web surfaces get one probe each. If every
    surface is blocked, the caller receives one structured access error.
    """
    global ACCESS_BLOCKED, ACTIVE_SEARCH_ENDPOINT
    endpoints = list(SEARCH_ENDPOINTS)
    if ACTIVE_SEARCH_ENDPOINT in endpoints:
        endpoints.remove(ACTIVE_SEARCH_ENDPOINT)
        endpoints.insert(0, ACTIVE_SEARCH_ENDPOINT)
    blocked: list[TikHubAccessBlocked] = []
    last_error: Exception | None = None
    for path in endpoints:
        try:
            data = api_get(
                path,
                search_params(
                    path,
                    keyword,
                    page,
                    sort_type,
                    search_id=search_id,
                    session_id=session_id,
                ),
                ignore_access_block=True,
            )
            ACTIVE_SEARCH_ENDPOINT = path
            ACCESS_BLOCKED = None
            return data, path
        except RequestBudgetExceeded:
            raise
        except TikHubAccessBlocked as exc:
            blocked.append(exc)
            print(f"[warn] search endpoint blocked: {exc}")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            print(f"[warn] search endpoint unavailable {path}: {exc}")
    if blocked:
        ACCESS_BLOCKED = blocked[-1]
        raise ACCESS_BLOCKED
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"no TikHub search endpoint available for {keyword!r}")


def response_items(data: dict) -> list[dict]:
    """Find the result list across app_v2/web_v2/web_v3 response wrappers."""
    def looks_like_item(value: object) -> bool:
        if not isinstance(value, dict):
            return False
        return any(
            key in value
            for key in ("note", "note_card", "note_info", "note_id", "id")
        )

    def walk(value: object, depth: int = 0) -> list[dict]:
        if depth > 6:
            return []
        if isinstance(value, list):
            items = [item for item in value if looks_like_item(item)]
            if items:
                return items
            for item in value:
                found = walk(item, depth + 1)
                if found:
                    return found
            return []
        if isinstance(value, dict):
            for key in ("items", "notes", "note_list", "data", "result"):
                if key in value:
                    found = walk(value[key], depth + 1)
                    if found:
                        return found
            for nested in value.values():
                found = walk(nested, depth + 1)
                if found:
                    return found
        return []

    return walk(data)


def first_mapping(*values: object) -> dict:
    return next((value for value in values if isinstance(value, dict)), {})


def image_url(image: object) -> str:
    if isinstance(image, str):
        return image
    if not isinstance(image, dict):
        return ""
    for key in ("url_size_large", "url_default", "url_large", "url", "url_pre"):
        value = image.get(key)
        if value:
            return str(value)
    for key in ("info_list", "url_list"):
        nested = image.get(key)
        if isinstance(nested, list):
            for item in nested:
                value = image_url(item)
                if value:
                    return value
    return ""


def normalize_search_item(item: dict, keyword: str) -> dict | None:
    note = first_mapping(item.get("note"), item.get("note_card"), item.get("note_info"))
    if not note:
        note = item
    note_type = note.get("type") or note.get("note_type") or "normal"
    if str(note_type).lower() not in {"normal", "image", "图文", ""}:
        return None
    nid = str(note.get("id") or note.get("note_id") or note.get("nid") or "")
    if not nid:
        return None
    stats = first_mapping(note.get("interact_info"), note.get("interact"), note.get("stats"))
    user = first_mapping(note.get("user"), note.get("user_info"), note.get("author"))
    images = (
        note.get("images_list")
        or note.get("image_list")
        or note.get("images")
        or note.get("image_list_v2")
        or []
    )
    if not isinstance(images, list):
        images = []
    timestamp = (
        note.get("timestamp")
        or note.get("time")
        or note.get("create_time")
        or note.get("last_update_time")
    )
    return {
        "note_id": nid,
        "title": note.get("title") or note.get("display_title") or "",
        "desc": note.get("desc") or note.get("description") or "",
        "liked_count": parse_count(
            note.get("liked_count", stats.get("liked_count", stats.get("like_count", 0)))
        ),
        "comments_count": parse_count(
            note.get("comments_count", stats.get("comments_count", stats.get("comment_count", 0)))
        ),
        "collected_count": parse_count(
            note.get("collected_count", stats.get("collected_count", stats.get("collect_count", 0)))
        ),
        "shared_count": parse_count(
            note.get("shared_count", stats.get("shared_count", stats.get("share_count", 0)))
        ),
        "timestamp": normalize_timestamp(timestamp),
        "cover_url": image_url(images[0]) if images else image_url(note.get("cover")),
        "images_count": len(images),
        "author_id": str(
            user.get("userid") or user.get("user_id") or user.get("uid") or user.get("id") or ""
        ),
        "author_name": user.get("nickname") or user.get("name") or "",
        "author_fans": embedded_author_fans(user),
        "keyword": keyword,
    }


def search_all() -> dict[str, dict]:
    notes: dict[str, dict] = {}
    for kw in KEYWORDS:
        for sort_type in SORTS:
            search_id = ""
            session_id = ""
            for page in range(1, PAGES + 1):
                try:
                    data, endpoint = search_notes(
                        kw,
                        page,
                        sort_type,
                        search_id=search_id,
                        session_id=session_id,
                    )
                except RequestBudgetExceeded:
                    raise
                except TikHubAccessBlocked:
                    raise
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] search {kw}/{sort_type} p{page}: {exc}")
                    continue
                payload = data.get("data") or {}
                search_id = payload.get("search_id") or search_id
                session_id = payload.get("search_session_id") or session_id
                items = response_items(data)
                got = 0
                for item in items:
                    rec = normalize_search_item(item, kw)
                    if rec is None:
                        continue
                    prev = notes.get(rec["note_id"])
                    if not prev or rec["liked_count"] > prev["liked_count"]:
                        notes[rec["note_id"]] = rec
                    got += 1
                print(f"[info] {kw}/{sort_type} p{page} via {endpoint}: {got} notes")
                time.sleep(0.4)
    return notes


def author_fans(user_id: str) -> int:
    if not user_id:
        return -1
    global ACCESS_BLOCKED
    endpoint_order = list(USER_ENDPOINTS)
    if ACTIVE_SEARCH_ENDPOINT and "/web_v3/" in ACTIVE_SEARCH_ENDPOINT:
        endpoint_order = [USER_ENDPOINTS[1], USER_ENDPOINTS[2], USER_ENDPOINTS[0]]
    elif ACTIVE_SEARCH_ENDPOINT and "/web_v2/" in ACTIVE_SEARCH_ENDPOINT:
        endpoint_order = [USER_ENDPOINTS[2], USER_ENDPOINTS[1], USER_ENDPOINTS[0]]
    blocked: list[TikHubAccessBlocked] = []
    for path in endpoint_order:
        try:
            data = api_get(path, {"user_id": user_id}, ignore_access_block=True)
            ACCESS_BLOCKED = None
            fans = extract_fans(data)
            if fans >= 0:
                return fans
        except RequestBudgetExceeded:
            raise
        except TikHubAccessBlocked as exc:
            blocked.append(exc)
            print(f"[warn] author endpoint blocked: {exc}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] user {user_id} via {path}: {exc}")
    if blocked:
        ACCESS_BLOCKED = blocked[-1]
        print(f"[warn] author lookup circuit opened: {ACCESS_BLOCKED}")
    return -1


def extract_fans(data: dict) -> int:
    """Extract follower counts without treating unrelated numeric fields as fans."""
    keys = ("fans", "fans_count", "followers", "follower_count", "follower_num")

    def walk(value: object, depth: int = 0) -> int:
        if depth > 8:
            return -1
        if isinstance(value, dict):
            for key in keys:
                if key in value and value[key] not in (None, ""):
                    return parse_count(value[key])
            for nested in value.values():
                found = walk(nested, depth + 1)
                if found >= 0:
                    return found
        elif isinstance(value, list):
            for nested in value:
                found = walk(nested, depth + 1)
                if found >= 0:
                    return found
        return -1

    return walk(data)


def embedded_author_fans(user: dict) -> int | None:
    """Use a follower count already present in search results when available."""
    mappings = [user]
    for key in ("interact_info", "stats", "fans_info"):
        if isinstance(user.get(key), dict):
            mappings.append(user[key])
    for mapping in mappings:
        for key in ("fans", "followers", "follower_count", "fans_count", "follower_num"):
            value = mapping.get(key)
            if value not in (None, ""):
                return parse_count(value)
    return None


def write_discovery_status(
    status: str,
    reason: str,
    *,
    message: str = "",
    total_notes: int = 0,
    fresh_notes: int = 0,
    eligible_notes: int = 0,
    candidate_count: int = 0,
    selected_count: int = 0,
) -> dict:
    payload = {
        "status": status,
        "reason": reason,
        "retryable": status == "deferred",
        "message": message,
        "total_notes": total_notes,
        "fresh_notes": fresh_notes,
        "eligible_notes": eligible_notes,
        "candidate_count": candidate_count,
        "selected_count": selected_count,
        "tikhub_access_blocked": ACCESS_BLOCKED is not None,
        "tikhub_blocked_status": ACCESS_BLOCKED.status_code if ACCESS_BLOCKED else None,
    }
    (OUT_DIR / "discovery_status.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if status == "deferred":
        for name in ("candidates.json", "selected_notes.json"):
            (OUT_DIR / name).write_text("[]\n", encoding="utf-8")
    return payload


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
        "--hot-term",
        action="append",
        default=[],
        help="当天娱乐热点词；命中内容优先选入，但不改变低粉/点赞硬门槛。",
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
    global ACCESS_BLOCKED, ACTIVE_SEARCH_ENDPOINT, BUDGET, HOT_TERMS, KEYWORDS, MAX_ATTEMPTS, OUT_DIR, PAGES, TOP_AUTHOR_CHECK
    args = parse_args()
    OUT_DIR = args.out_dir.expanduser().resolve()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PAGES = args.pages
    TOP_AUTHOR_CHECK = args.top_author_check
    MAX_ATTEMPTS = args.max_attempts
    HOT_TERMS = list(dict.fromkeys(
        re.sub(r"\s+", " ", str(term)).strip()
        for term in args.hot_term
        if re.sub(r"\s+", " ", str(term)).strip()
    ))[:8]
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
    ACCESS_BLOCKED = None
    ACTIVE_SEARCH_ENDPOINT = None
    try:
        notes = search_all()
    except RequestBudgetExceeded as exc:
        write_discovery_status(
            "deferred",
            "tikhub_request_budget_exhausted",
            message=str(exc),
        )
        print(f"[deferred] {exc}")
        return
    except TikHubAccessBlocked as exc:
        write_discovery_status(
            "deferred",
            "tikhub_access_blocked",
            message=str(exc),
        )
        print(f"[deferred] {exc}")
        return
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
    if not eligible_for_lookup:
        write_discovery_status(
            "deferred",
            "no_recent_viral_candidates",
            message="No recent notes met the minimum likes/comments/search evidence.",
            total_notes=len(notes),
            fresh_notes=len(fresh),
        )
        print("[deferred] no recent viral-like candidates")
        return

    candidates = []
    fans_by_author: dict[str, int] = {}
    for rec in eligible_for_lookup[:TOP_AUTHOR_CHECK]:
        author_id = rec["author_id"]
        if rec.get("author_fans") is not None:
            fans = int(rec["author_fans"])
        elif author_id not in fans_by_author:
            try:
                fans_by_author[author_id] = author_fans(author_id)
            except RequestBudgetExceeded as exc:
                write_discovery_status(
                    "deferred",
                    "tikhub_request_budget_exhausted",
                    message=str(exc),
                    total_notes=len(notes),
                    fresh_notes=len(fresh),
                    eligible_notes=len(eligible_for_lookup),
                    candidate_count=len(candidates),
                )
                print(f"[deferred] {exc}")
                return
            fans = fans_by_author[author_id]
        else:
            fans = fans_by_author[author_id]
        rec["author_fans"] = fans
        candidates.append(rec)
        print(
            f"  {rec['liked_count']:>6} 赞 | {rec['comments_count']:>5} 评 | "
            f"粉丝 {fans:>7} | {rec['title'][:30]} | {rec['note_id']}"
        )
        time.sleep(0.4)
        if ACCESS_BLOCKED is not None:
            break

    low_fan = [
        candidate
        for candidate in candidates
        if 0 <= candidate["author_fans"] <= FANS_MAX
        and candidate["liked_count"] >= LIKES_MIN
    ]
    if args.strict_low_fan and not low_fan:
        reason = (
            "tikhub_author_lookup_unavailable"
            if ACCESS_BLOCKED is not None
            else "no_strict_low_fan_candidate"
        )
        message = (
            str(ACCESS_BLOCKED)
            if ACCESS_BLOCKED is not None
            else f"No candidate met fans <= {FANS_MAX}, likes >= {LIKES_MIN}."
        )
        write_discovery_status(
            "deferred",
            reason,
            message=message,
            total_notes=len(notes),
            fresh_notes=len(fresh),
            eligible_notes=len(eligible_for_lookup),
            candidate_count=len(candidates),
        )
        print(f"[deferred] {message}")
        return
    pool = low_fan or [c for c in candidates if c["liked_count"] >= LIKES_MIN] or candidates
    def viral_score(candidate: dict) -> float:
        likes = candidate["liked_count"]
        comments = candidate["comments_count"]
        fans = max(candidate.get("author_fans", 0), 50)
        age_hours = max(0.0, (now - candidate["timestamp"]) / 3600)
        hot_matches = [
            term for term in HOT_TERMS
            if term and term in f"{candidate.get('title', '')} {candidate.get('desc', '')}"
        ]
        candidate["hot_context_matches"] = hot_matches[:8]
        hot_bonus = min(24.0, 12.0 * len(hot_matches))
        return hot_bonus + (
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
        write_discovery_status(
            "deferred",
            "no_candidate_after_quality_filter",
            message="No candidate remained after quality filtering.",
            total_notes=len(notes),
            fresh_notes=len(fresh),
            eligible_notes=len(eligible_for_lookup),
            candidate_count=len(candidates),
        )
        print("[deferred] no candidate after quality filtering")
        return
    # Reserve up to two slots for genuine same-day entertainment-hot matches,
    # then fill with the strongest generic low-fan posts. This keeps the daily
    # batch varied instead of turning all five outputs into one hot topic.
    selected: list[dict] = []
    selected_ids: set[str] = set()
    selected_authors: set[str] = set()
    hot_pool = [candidate for candidate in pool if candidate.get("hot_context_matches")]
    generic_pool = [candidate for candidate in pool if not candidate.get("hot_context_matches")]
    hot_target = min(2, args.limit, len(hot_pool))
    for candidate in [*hot_pool[:hot_target], *generic_pool, *hot_pool[hot_target:]]:
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
    write_discovery_status(
        "ready",
        "quality_gate_passed",
        total_notes=len(notes),
        fresh_notes=len(fresh),
        eligible_notes=len(eligible_for_lookup),
        candidate_count=len(candidates),
        selected_count=len(selected),
    )
    print(
        f"\n[selected] {len(selected)}/{args.limit}: "
        + ", ".join(note["note_id"] for note in selected)
    )
    print("[chosen]", json.dumps(chosen, ensure_ascii=False, indent=2))
    print("[budget]", json.dumps(BUDGET.snapshot(), ensure_ascii=False))


if __name__ == "__main__":
    main()
