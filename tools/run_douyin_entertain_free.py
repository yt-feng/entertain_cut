#!/usr/bin/env python3
"""Free-first daily Douyin entertainment video discovery and download.

This script is designed for GitHub Actions. It uses the open-source
``jiji262/douyin-downloader`` project as a temporary dependency, asks it to
dump hot/search JSONL files, selects the top entertainment videos, then asks
the same downloader to fetch the selected videos.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

DEFAULT_SEED_KEYWORDS = [
    "娱乐 明星",
    "内娱",
    "明星",
    "综艺",
    "电视剧",
    "电影",
    "演唱会",
]

ENTERTAINMENT_TERMS = [
    "娱乐",
    "明星",
    "演员",
    "艺人",
    "歌手",
    "爱豆",
    "偶像",
    "内娱",
    "综艺",
    "电影",
    "电视剧",
    "剧集",
    "网剧",
    "热剧",
    "导演",
    "票房",
    "演唱会",
    "红毯",
    "女星",
    "男星",
    "影后",
    "影帝",
]


def main() -> int:
    args = parse_args()
    downloader_dir = project_path(args.downloader_dir)
    work_dir = project_path(args.work_dir)
    discovery_dir = work_dir / "discovery"
    download_dir = work_dir / "downloads"
    selected_dir = work_dir / "selected"
    reports_dir = work_dir / "reports"
    config_path = work_dir / "config.yml"
    download_config_path = work_dir / "download_config.yml"

    for path in (discovery_dir, download_dir, selected_dir, reports_dir):
        path.mkdir(parents=True, exist_ok=True)

    run_info: dict[str, Any] = {
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "downloader_dir": str(downloader_dir),
        "work_dir": str(work_dir),
        "commands": [],
        "errors": [],
    }

    write_downloader_config(config_path, discovery_dir, links=[])

    hot_items = run_hot_board(args, downloader_dir, config_path, discovery_dir, run_info)
    keywords = build_keywords(hot_items, args)
    run_info["keywords"] = keywords

    for keyword in keywords:
        code = run_downloader(
            downloader_dir,
            [
                sys.executable,
                "run.py",
                "-c",
                str(config_path),
                "--search",
                keyword,
                "--search-max",
                str(args.search_max),
                "-p",
                str(discovery_dir),
                "--show-warnings",
            ],
            run_info,
            check=False,
        )
        if code != 0:
            run_info["errors"].append(f"search failed for keyword={keyword!r} exit={code}")

    candidates = load_search_candidates(discovery_dir / "search")
    if not candidates:
        run_direct_search_fallback(args, downloader_dir, config_path, discovery_dir, keywords, run_info)
        candidates = load_search_candidates(discovery_dir / "search")
    if not candidates:
        run_browser_search_fallback(args, downloader_dir, config_path, discovery_dir, keywords, run_info)
        candidates = load_search_candidates(discovery_dir / "search")
    if not candidates:
        run_feed_fallback(args, downloader_dir, config_path, discovery_dir, run_info)
        candidates = load_search_candidates(discovery_dir / "search")

    selected = select_candidates(
        candidates,
        args.limit,
        args.recent_hours,
        args.primary_min_likes,
        args.fallback_min_likes,
    )
    write_reports(reports_dir, hot_items, keywords, candidates, selected, run_info)

    if not selected:
        print("No candidate videos selected. Reports were still written.")
        return 0

    links = [item["url"] for item in selected if item.get("url")]
    write_downloader_config(download_config_path, download_dir, links=links)
    if not args.search_only:
        run_downloader(
            downloader_dir,
            [sys.executable, "run.py", "-c", str(download_config_path), "-p", str(download_dir), "-t", str(args.threads), "--show-warnings"],
            run_info,
            check=False,
        )
        copy_selected_videos(download_dir, selected_dir, selected)
        write_reports(reports_dir, hot_items, keywords, candidates, selected, run_info)

    print(f"Selected {len(selected)} videos.")
    print(f"Reports: {reports_dir}")
    print(f"Downloads: {download_dir}")
    print(f"Selected copies: {selected_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--downloader-dir", default="/tmp/douyin-downloader")
    parser.add_argument("--work-dir", default=ROOT / "work" / "douyin_free_daily")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--hot-limit", type=int, default=50)
    parser.add_argument("--max-hot-keywords", type=int, default=10)
    parser.add_argument("--search-max", type=int, default=20)
    parser.add_argument("--recent-hours", type=int, default=24)
    parser.add_argument("--primary-min-likes", type=int, default=10_000)
    parser.add_argument("--fallback-min-likes", type=int, default=1_000)
    parser.add_argument("--feed-pages", type=int, default=5)
    parser.add_argument("--feed-count", type=int, default=20)
    parser.add_argument("--threads", type=int, default=3)
    parser.add_argument("--search-only", action="store_true")
    parser.add_argument(
        "--seed-keywords",
        default=",".join(DEFAULT_SEED_KEYWORDS),
        help="Comma-separated fallback/search keywords.",
    )
    return parser.parse_args()


def run_hot_board(
    args: argparse.Namespace,
    downloader_dir: Path,
    config_path: Path,
    discovery_dir: Path,
    run_info: dict[str, Any],
) -> list[dict[str, Any]]:
    code = run_downloader(
        downloader_dir,
        [
            sys.executable,
            "run.py",
            "-c",
            str(config_path),
            "--hot-board",
            str(args.hot_limit),
            "-p",
            str(discovery_dir),
            "--show-warnings",
        ],
        run_info,
        check=False,
    )
    if code != 0:
        run_info["errors"].append(f"hot board failed exit={code}")
    return read_jsonl_dir(discovery_dir / "hot_board")


def run_downloader(
    cwd: Path,
    cmd: list[str],
    run_info: dict[str, Any],
    *,
    check: bool,
) -> int:
    printable = " ".join(str(part) for part in cmd)
    print(f"+ {printable}")
    run_info["commands"].append(printable)
    result = subprocess.run(cmd, cwd=str(cwd), text=True)
    if check and result.returncode:
        raise subprocess.CalledProcessError(result.returncode, cmd)
    return int(result.returncode)


def run_direct_search_fallback(
    args: argparse.Namespace,
    downloader_dir: Path,
    config_path: Path,
    discovery_dir: Path,
    keywords: list[str],
    run_info: dict[str, Any],
) -> None:
    stats: list[dict[str, Any]] = []
    run_info["direct_search_fallback"] = stats
    if str(downloader_dir) not in sys.path:
        sys.path.insert(0, str(downloader_dir))

    async def _search() -> None:
        from config import ConfigLoader  # type: ignore
        from core.api_client import DouyinAPIClient  # type: ignore

        config = ConfigLoader(str(config_path))
        cookies = config.get_cookies()
        async with DouyinAPIClient(cookies) as api_client:
            for keyword in keywords:
                try:
                    page = await api_client.search_aweme(
                        keyword,
                        offset=0,
                        count=max(1, min(int(args.search_max), 50)),
                        sort_type=1,
                        publish_time=1,
                    )
                    items = [item for item in page.get("items") or [] if isinstance(item, dict)]
                    path = write_search_jsonl(discovery_dir / "search", keyword, items)
                    stats.append(
                        {
                            "keyword": keyword,
                            "count": len(items),
                            "status_code": page.get("status_code"),
                            "has_more": page.get("has_more"),
                            "max_cursor": page.get("max_cursor"),
                            "path": str(path),
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    message = f"direct search failed for keyword={keyword!r}: {exc}"
                    run_info["errors"].append(message)
                    stats.append({"keyword": keyword, "error": str(exc)})

    asyncio.run(_search())


def run_browser_search_fallback(
    args: argparse.Namespace,
    downloader_dir: Path,
    config_path: Path,
    discovery_dir: Path,
    keywords: list[str],
    run_info: dict[str, Any],
) -> None:
    stats: list[dict[str, Any]] = []
    run_info["browser_search_fallback"] = stats
    if str(downloader_dir) not in sys.path:
        sys.path.insert(0, str(downloader_dir))

    async def _search() -> None:
        try:
            from playwright.async_api import async_playwright  # type: ignore
        except Exception as exc:  # noqa: BLE001
            run_info["errors"].append(f"browser search unavailable: {exc}")
            return

        from config import ConfigLoader  # type: ignore
        from core.api_client import DouyinAPIClient  # type: ignore

        config = ConfigLoader(str(config_path))
        cookies = config.get_cookies()
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        user_agent = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        )

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = await browser.new_context(
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                user_agent=user_agent,
                viewport={"width": 1365, "height": 900},
            )
            await context.add_cookies(
                [
                    {
                        "name": name,
                        "value": value,
                        "domain": ".douyin.com",
                        "path": "/",
                    }
                    for name, value in cookies.items()
                    if name and value
                ]
            )
            page = await context.new_page()
            async with DouyinAPIClient(cookies) as api_client:
                for keyword in keywords:
                    encoded = urllib.parse.quote(keyword)
                    url = f"https://www.douyin.com/search/{encoded}?type=video&sort_type=1&publish_time=1"
                    try:
                        await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                        await page.wait_for_timeout(4_000)
                        for _ in range(3):
                            await page.mouse.wheel(0, 900)
                            await page.wait_for_timeout(1_000)
                        cards = await page.evaluate(
                            """
                            () => Array.from(document.querySelectorAll('a[href*="/video/"], a[href*="modal_id="]'))
                              .slice(0, 80)
                              .map((a) => {
                                const container = a.closest('[data-e2e], article, li, section, div') || a;
                                return { href: a.href || '', text: (container.innerText || a.textContent || '').slice(0, 1200) };
                              })
                            """
                        )
                        ids: list[tuple[str, str, str]] = []
                        for card in cards if isinstance(cards, list) else []:
                            if not isinstance(card, dict):
                                continue
                            href = str(card.get("href") or "")
                            aweme_id = extract_aweme_id(href)
                            if aweme_id and aweme_id not in seen:
                                seen.add(aweme_id)
                                ids.append((aweme_id, href, str(card.get("text") or "")))

                        keyword_items: list[dict[str, Any]] = []
                        for aweme_id, href, text in ids[: max(1, int(args.search_max))]:
                            detail = await api_client.get_video_detail(aweme_id, suppress_error=True)
                            if isinstance(detail, dict):
                                keyword_items.append(detail)
                            else:
                                keyword_items.append(
                                    {
                                        "aweme_id": aweme_id,
                                        "desc": text,
                                        "share_url": href or f"https://www.douyin.com/video/{aweme_id}",
                                        "statistics": {},
                                    }
                                )
                        collected.extend(keyword_items)
                        stats.append(
                            {
                                "keyword": keyword,
                                "link_count": len(cards) if isinstance(cards, list) else 0,
                                "unique_ids": len(ids),
                                "detail_count": sum(1 for item in keyword_items if item.get("statistics")),
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        message = f"browser search failed for keyword={keyword!r}: {exc}"
                        run_info["errors"].append(message)
                        stats.append({"keyword": keyword, "error": str(exc)})
            await browser.close()
        path = write_search_jsonl(discovery_dir / "search", "browser_search_fallback", collected)
        run_info["browser_search_fallback_path"] = str(path)

    asyncio.run(_search())


def extract_aweme_id(url: str) -> str:
    match = re.search(r"/video/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"[?&]modal_id=(\d+)", url)
    if match:
        return match.group(1)
    return ""


def run_feed_fallback(
    args: argparse.Namespace,
    downloader_dir: Path,
    config_path: Path,
    discovery_dir: Path,
    run_info: dict[str, Any],
) -> None:
    stats: list[dict[str, Any]] = []
    run_info["feed_fallback"] = stats
    if str(downloader_dir) not in sys.path:
        sys.path.insert(0, str(downloader_dir))

    async def _fetch() -> None:
        from config import ConfigLoader  # type: ignore
        from core.api_client import DouyinAPIClient  # type: ignore

        config = ConfigLoader(str(config_path))
        cookies = config.get_cookies()
        collected: list[dict[str, Any]] = []
        seen: set[str] = set()
        async with DouyinAPIClient(cookies) as api_client:
            for page_idx in range(max(1, int(args.feed_pages))):
                try:
                    params = await api_client._default_query()  # noqa: SLF001
                    params.update(
                        {
                            "count": max(1, min(int(args.feed_count), 30)),
                            "refresh_index": page_idx + 1,
                            "video_type_select": 1,
                        }
                    )
                    raw = await api_client._request_json(  # noqa: SLF001
                        "/aweme/v1/web/tab/feed/",
                        params,
                        suppress_error=True,
                    )
                    items = normalize_feed_items(raw)
                    entertainment_items: list[dict[str, Any]] = []
                    for item in items:
                        aweme_id = str(item.get("aweme_id") or "")
                        if not aweme_id or aweme_id in seen:
                            continue
                        seen.add(aweme_id)
                        if is_entertainment_aweme(item):
                            entertainment_items.append(item)
                            collected.append(item)
                    stats.append(
                        {
                            "page": page_idx + 1,
                            "raw_count": len(items),
                            "entertainment_count": len(entertainment_items),
                            "status_code": raw.get("status_code") if isinstance(raw, dict) else None,
                        }
                    )
                except Exception as exc:  # noqa: BLE001
                    message = f"feed fallback failed page={page_idx + 1}: {exc}"
                    run_info["errors"].append(message)
                    stats.append({"page": page_idx + 1, "error": str(exc)})
                    break
        path = write_search_jsonl(discovery_dir / "search", "feed_fallback", collected)
        run_info["feed_fallback_path"] = str(path)

    asyncio.run(_fetch())


def normalize_feed_items(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    for key in ("aweme_list", "items"):
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    data = raw.get("data")
    if isinstance(data, dict):
        value = data.get("aweme_list") or data.get("items")
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


def write_search_jsonl(search_dir: Path, keyword: str, items: list[dict[str, Any]]) -> Path:
    search_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_keyword = "".join(ch if ch.isalnum() else "_" for ch in keyword)[:40] or "query"
    path = search_dir / f"direct_{safe_keyword}_{ts}.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for item in items:
            handle.write(json.dumps(item, ensure_ascii=False))
            handle.write("\n")
    return path


def build_keywords(hot_items: list[dict[str, Any]], args: argparse.Namespace) -> list[str]:
    hot_words: list[str] = []
    for item in hot_items:
        word = hot_word(item)
        if word and is_entertainment_text(word):
            hot_words.append(word)
    seed_words = [part.strip() for part in str(args.seed_keywords).split(",") if part.strip()]
    return dedupe_keep_order(hot_words[: max(0, args.max_hot_keywords)] + seed_words)


def load_search_candidates(search_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for path in sorted(search_dir.glob("*.jsonl")):
        source_keyword = path.stem.rsplit("_", 2)[0]
        for raw in read_jsonl_file(path):
            normalized = normalize_aweme(raw, source_keyword, path)
            aweme_id = normalized.get("aweme_id")
            if not aweme_id or aweme_id in seen:
                continue
            seen.add(aweme_id)
            rows.append(normalized)
    return rows


def normalize_aweme(raw: dict[str, Any], source_keyword: str, source_file: Path) -> dict[str, Any]:
    aweme = raw.get("aweme_info") if isinstance(raw.get("aweme_info"), dict) else raw
    stats = first_dict(aweme.get("statistics"), aweme.get("stats"))
    author = first_dict(aweme.get("author"))
    aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or "")
    title = str(aweme.get("desc") or aweme.get("title") or aweme.get("caption") or "").strip()
    share_url = (
        aweme.get("share_url")
        or nested_get(aweme, ["share_info", "share_url"])
        or nested_get(aweme, ["share_info", "url"])
        or ""
    )
    if not share_url and aweme_id:
        share_url = f"https://www.douyin.com/video/{aweme_id}"
    create_time = as_int(aweme.get("create_time"))
    return {
        "aweme_id": aweme_id,
        "title": title,
        "url": str(share_url),
        "like_count": as_int(stats.get("digg_count") or stats.get("like_count")),
        "comment_count": as_int(stats.get("comment_count")),
        "share_count": as_int(stats.get("share_count")),
        "collect_count": as_int(stats.get("collect_count")),
        "play_count": as_int(stats.get("play_count")),
        "create_time": create_time,
        "create_time_iso": timestamp_iso(create_time),
        "author": str(author.get("nickname") or author.get("unique_id") or ""),
        "source_keyword": source_keyword,
        "source_file": str(source_file),
    }


def select_candidates(
    candidates: list[dict[str, Any]],
    limit: int,
    recent_hours: int,
    primary_min_likes: int,
    fallback_min_likes: int,
) -> list[dict[str, Any]]:
    now = dt.datetime.now(dt.UTC).timestamp()
    recent: list[dict[str, Any]] = []
    for item in candidates:
        created = as_int(item.get("create_time"))
        if created and recent_hours > 0 and 0 <= now - created <= recent_hours * 3600:
            recent.append(item)
    pool = recent if len(recent) >= max(1, limit) else candidates
    primary_pool = [item for item in pool if as_int(item.get("like_count")) >= primary_min_likes]
    fallback_pool = [item for item in pool if as_int(item.get("like_count")) >= fallback_min_likes]
    if len(primary_pool) >= max(1, limit):
        pool = primary_pool
    elif len(fallback_pool) >= max(1, limit):
        pool = fallback_pool
    ranked = sorted(
        pool,
        key=lambda item: (
            as_int(item.get("like_count")),
            as_int(item.get("comment_count")) + as_int(item.get("share_count")),
            as_int(item.get("play_count")),
        ),
        reverse=True,
    )
    return ranked[: max(0, limit)]


def write_reports(
    reports_dir: Path,
    hot_items: list[dict[str, Any]],
    keywords: list[str],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    run_info: dict[str, Any],
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    write_json(reports_dir / "hot_items.json", hot_items)
    write_json(reports_dir / "keywords.json", keywords)
    write_json(reports_dir / "candidates.json", candidates)
    write_json(reports_dir / "selected.json", selected)
    write_json(reports_dir / "run_info.json", run_info)
    with (reports_dir / "selected.csv").open("w", encoding="utf-8", newline="") as handle:
        fieldnames = [
            "rank",
            "aweme_id",
            "like_count",
            "comment_count",
            "share_count",
            "play_count",
            "create_time_iso",
            "author",
            "source_keyword",
            "title",
            "url",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for idx, item in enumerate(selected, 1):
            row = {key: item.get(key, "") for key in fieldnames}
            row["rank"] = idx
            writer.writerow(row)
    with (reports_dir / "summary.md").open("w", encoding="utf-8") as handle:
        handle.write("# Douyin Free Daily\n\n")
        handle.write(f"- Hot items: {len(hot_items)}\n")
        handle.write(f"- Search keywords: {', '.join(keywords)}\n")
        handle.write(f"- Candidates: {len(candidates)}\n")
        handle.write(f"- Selected: {len(selected)}\n\n")
        for idx, item in enumerate(selected, 1):
            handle.write(
                f"{idx}. {item.get('title') or item.get('aweme_id')} "
                f"likes={item.get('like_count')} keyword={item.get('source_keyword')}\n"
                f"   {item.get('url')}\n"
            )


def copy_selected_videos(download_dir: Path, selected_dir: Path, selected: list[dict[str, Any]]) -> None:
    selected_dir.mkdir(parents=True, exist_ok=True)
    mp4s = list(download_dir.rglob("*.mp4"))
    for idx, item in enumerate(selected, 1):
        aweme_id = str(item.get("aweme_id") or "")
        if not aweme_id:
            continue
        matches = [path for path in mp4s if aweme_id in path.name or aweme_id in str(path.parent)]
        for source in matches[:1]:
            target = selected_dir / f"{idx:02d}_{aweme_id}{source.suffix}"
            shutil.copy2(source, target)


def write_downloader_config(path: Path, output_dir: Path, *, links: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cookie = os.getenv("DOUYIN_COOKIE", "").replace("\\", "\\\\").replace('"', '\\"')
    lines = [
        "link:",
        *[f'  - "{url}"' for url in links],
        f'path: "{output_dir}"',
        "mode:",
        "  - post",
        "number:",
        "  post: 0",
        "  like: 0",
        "  mix: 0",
        "  allmix: 0",
        "  music: 0",
        "  collect: 0",
        "  collectmix: 0",
        "thread: 3",
        "retry_times: 3",
        "rate_limit: 2",
        "database: true",
        f'database_path: "{path.parent / "dy_downloader.db"}"',
        "folderstyle: true",
        "music: false",
        "cover: true",
        "avatar: false",
        "json: true",
        "comments:",
        "  enabled: false",
        "transcript:",
        "  enabled: false",
        "browser_fallback:",
        "  enabled: false",
        "progress:",
        "  quiet_logs: true",
    ]
    if cookie:
        lines.extend(["cookie: " + json.dumps(cookie, ensure_ascii=False)])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def read_jsonl_dir(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for file_path in sorted(path.glob("*.jsonl")):
        items.extend(read_jsonl_file(file_path))
    return items


def read_jsonl_file(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not path.exists():
        return items
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            items.append(item)
    return items


def hot_word(item: dict[str, Any]) -> str:
    for key in ("word", "sentence", "word_cover_title", "title", "keyword"):
        value = item.get(key)
        if value:
            return str(value).strip()
    return ""


def is_entertainment_text(text: str) -> bool:
    return any(term in text for term in ENTERTAINMENT_TERMS)


def is_entertainment_aweme(item: dict[str, Any]) -> bool:
    text_parts = [
        str(item.get("desc") or ""),
        str(item.get("title") or ""),
        str(nested_get(item, ["author", "nickname"]) or ""),
        str(nested_get(item, ["music", "title"]) or ""),
    ]
    for extra in item.get("text_extra") or []:
        if isinstance(extra, dict):
            text_parts.append(str(extra.get("hashtag_name") or extra.get("hashtag_id") or ""))
    for challenge in item.get("cha_list") or []:
        if isinstance(challenge, dict):
            text_parts.append(str(challenge.get("cha_name") or challenge.get("desc") or ""))
    return is_entertainment_text(" ".join(text_parts))


def first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def nested_get(data: dict[str, Any], keys: list[str]) -> Any:
    value: Any = data
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def timestamp_iso(value: Any) -> str:
    ts = as_int(value)
    if not ts:
        return ""
    return dt.datetime.fromtimestamp(ts, tz=dt.UTC).isoformat()


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def project_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path


if __name__ == "__main__":
    raise SystemExit(main())
