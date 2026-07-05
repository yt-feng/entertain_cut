#!/usr/bin/env python3
"""Discover and download recent high-like Douyin entertainment videos via TikHub."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx


ROOT = Path(__file__).resolve().parents[1]
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".webm"}
TIKHUB_VIDEO_SEARCH_URL = "https://api.tikhub.io/api/v1/douyin/search/fetch_video_search_v2"


def main() -> int:
    args = parse_args()
    api_key = os.environ.get("TIKHUB_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("TIKHUB_API_KEY is required for TikHub source discovery.")

    work_dir = project_path(args.work_dir)
    discovery_dir = work_dir / "discovery"
    selected_dir = work_dir / "selected"
    reports_dir = work_dir / "reports"
    downloads_dir = work_dir / "downloads" / "tikhub"
    for path in (discovery_dir, selected_dir, reports_dir, downloads_dir):
        path.mkdir(parents=True, exist_ok=True)

    keywords = split_terms(args.seed_keywords)
    run_info: dict[str, Any] = {
        "provider": "tikhub",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "work_dir": str(work_dir),
        "keywords": keywords,
        "commands": [],
        "errors": [],
        "requested_limit": args.limit,
        "minimum_selected_videos": args.min_selected_videos,
        "max_search_requests": args.max_search_requests,
    }

    candidates = fetch_candidates(args, api_key, keywords, discovery_dir, run_info)
    selected = select_candidates(
        candidates,
        max(args.limit, args.limit * max(1, args.download_candidate_multiplier)),
        args.recent_hours,
        args.primary_min_likes,
        args.fallback_min_likes,
        args.max_duration_seconds,
        args.must_include_terms,
        args.exclude_terms,
    )
    write_reports(reports_dir, keywords, candidates, selected, run_info)

    if not selected:
        print("TikHub returned no selected candidates. Reports were still written.")
        return 0

    downloaded_ids = download_selected(args, selected, downloads_dir, selected_dir, run_info)
    successful_ids = selected_aweme_ids(selected_dir)
    if successful_ids:
        selected = [item for item in selected if str(item.get("aweme_id") or "") in successful_ids][: args.limit]
        rewrite_selected_dir(selected_dir, selected)
    record_selected_files(selected_dir, run_info)
    run_info["downloaded_ids"] = sorted(downloaded_ids)
    write_reports(reports_dir, keywords, candidates, selected, run_info)

    selected_file_count = count_selected_files(selected_dir)
    print(f"TikHub selected files: {selected_file_count}/{args.limit} (minimum {args.min_selected_videos})", flush=True)
    if selected_file_count < min(max(1, args.limit), max(1, args.min_selected_videos)):
        run_info["minimum_not_met"] = (
            f"Only {selected_file_count}/{args.limit} selected videos downloaded; "
            f"minimum for publishing is {args.min_selected_videos}."
        )
        write_reports(reports_dir, keywords, candidates, selected, run_info)
    print(f"Reports: {reports_dir}")
    print(f"Selected copies: {selected_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", default=ROOT / "work" / "douyin_tikhub_daily")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--min-selected-videos", type=int, default=7)
    parser.add_argument("--recent-hours", type=int, default=24)
    parser.add_argument("--primary-min-likes", type=int, default=10_000)
    parser.add_argument("--fallback-min-likes", type=int, default=1_000)
    parser.add_argument("--max-duration-seconds", type=int, default=300)
    parser.add_argument("--download-candidate-multiplier", type=int, default=4)
    parser.add_argument("--max-search-requests", type=int, default=5)
    parser.add_argument("--pages-per-keyword", type=int, default=1)
    parser.add_argument("--request-timeout-seconds", type=int, default=45)
    parser.add_argument("--download-timeout-seconds", type=int, default=120)
    parser.add_argument("--download-max-urls", type=int, default=3)
    parser.add_argument("--seed-keywords", default="")
    parser.add_argument("--must-include-terms", default="")
    parser.add_argument("--exclude-terms", default="")
    return parser.parse_args()


def fetch_candidates(
    args: argparse.Namespace,
    api_key: str,
    keywords: list[str],
    discovery_dir: Path,
    run_info: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    request_count = 0
    max_search_requests = max(0, int(args.max_search_requests))
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "kc-entertain-daily/1.0",
    }
    timeout = httpx.Timeout(connect=15, read=args.request_timeout_seconds, write=20, pool=15)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for keyword in keywords:
            if max_search_requests and request_count >= max_search_requests:
                break
            cursor = 0
            search_id = ""
            backtrace = ""
            for page in range(max(1, args.pages_per_keyword)):
                if max_search_requests and request_count >= max_search_requests:
                    break
                payload = {
                    "keyword": keyword,
                    "cursor": cursor,
                    "sort_type": "1",
                    "publish_time": "1",
                    "filter_duration": "0",
                    "content_type": "1",
                    "search_id": search_id,
                    "backtrace": backtrace,
                }
                try:
                    request_count += 1
                    response = client.post(TIKHUB_VIDEO_SEARCH_URL, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    write_json(discovery_dir / f"tikhub_{safe_slug(keyword)}_{page + 1}.json", data)
                    page_items = 0
                    for aweme in iter_aweme_infos(data):
                        item = normalize_aweme(aweme, keyword, discovery_dir)
                        aweme_id = str(item.get("aweme_id") or "")
                        key = aweme_id or str(item.get("url") or "")
                        if not key or key in seen:
                            continue
                        seen.add(key)
                        candidates.append(item)
                        page_items += 1
                    run_info.setdefault("tikhub_search", []).append(
                        {"keyword": keyword, "page": page + 1, "count": page_items, "cursor": cursor}
                    )
                    next_cursor = find_first_value(data, {"cursor"})
                    next_search_id = find_first_value(data, {"search_id"})
                    next_backtrace = find_first_value(data, {"backtrace"})
                    if next_cursor is None or int_or_zero(next_cursor) == cursor:
                        break
                    cursor = int_or_zero(next_cursor)
                    search_id = str(next_search_id or search_id or "")
                    backtrace = str(next_backtrace or backtrace or "")
                except Exception as exc:  # noqa: BLE001
                    message = f"TikHub search failed keyword={keyword!r} page={page + 1}: {exc}"
                    run_info["errors"].append(message)
                    print(message, flush=True)
                    break
    return candidates


def iter_aweme_infos(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            aweme = value.get("aweme_info")
            if isinstance(aweme, dict):
                found.append(aweme)
            elif looks_like_aweme(value):
                found.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(data)
    return found


def looks_like_aweme(value: dict[str, Any]) -> bool:
    return bool(value.get("aweme_id") and (value.get("video") or value.get("statistics")))


def normalize_aweme(aweme: dict[str, Any], source_keyword: str, source_dir: Path) -> dict[str, Any]:
    stats = first_dict(aweme.get("statistics"), aweme.get("stats"))
    author = first_dict(aweme.get("author"))
    video = first_dict(aweme.get("video"))
    aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or aweme.get("item_id") or "")
    title = str(aweme.get("desc") or aweme.get("title") or aweme.get("caption") or "").strip()
    share_url = (
        aweme.get("share_url")
        or nested_get(aweme, ["share_info", "share_url"])
        or nested_get(aweme, ["share_info", "url"])
        or ""
    )
    if not share_url and aweme_id:
        share_url = f"https://www.douyin.com/video/{aweme_id}"
    return {
        "aweme_id": aweme_id,
        "title": title,
        "url": str(share_url),
        "download_urls": extract_video_urls(aweme),
        "duration_ms": int_or_zero(video.get("duration") or aweme.get("duration")),
        "like_count": int_or_zero(stats.get("digg_count") or stats.get("like_count")),
        "comment_count": int_or_zero(stats.get("comment_count")),
        "share_count": int_or_zero(stats.get("share_count")),
        "collect_count": int_or_zero(stats.get("collect_count")),
        "play_count": int_or_zero(stats.get("play_count")),
        "create_time": int_or_zero(aweme.get("create_time")),
        "create_time_iso": timestamp_iso(int_or_zero(aweme.get("create_time"))),
        "author": str(author.get("nickname") or author.get("unique_id") or ""),
        "source_keyword": source_keyword,
        "source_file": str(source_dir),
        "provider": "tikhub",
    }


def extract_video_urls(aweme: dict[str, Any]) -> list[str]:
    video = first_dict(aweme.get("video"))
    urls: list[str] = []

    def add_addr(addr: Any) -> None:
        if not isinstance(addr, dict):
            return
        value = addr.get("url_list") or addr.get("url_list_264") or addr.get("url_list_265")
        if isinstance(value, list):
            urls.extend(str(url) for url in value if url)
        elif isinstance(value, str):
            urls.append(value)

    for key in ("play_addr_h264", "play_addr", "download_addr"):
        add_addr(video.get(key))
    for bit_rate in video.get("bit_rate") or []:
        if isinstance(bit_rate, dict):
            add_addr(bit_rate.get("play_addr"))
            add_addr(bit_rate.get("play_addr_265"))
    return dedupe_keep_order([url for url in urls if url.startswith(("http://", "https://"))])


def select_candidates(
    candidates: list[dict[str, Any]],
    limit: int,
    recent_hours: int,
    primary_min_likes: int,
    fallback_min_likes: int,
    max_duration_seconds: int,
    must_include_terms: str,
    exclude_terms: str,
) -> list[dict[str, Any]]:
    scoped = candidates
    if max_duration_seconds > 0:
        max_duration_ms = max_duration_seconds * 1000
        short = [item for item in scoped if not item.get("duration_ms") or int_or_zero(item.get("duration_ms")) <= max_duration_ms]
        scoped = short if short else scoped
    include_terms = split_terms(must_include_terms)
    exclude_terms_list = split_terms(exclude_terms)
    if exclude_terms_list:
        scoped = [item for item in scoped if not matches_any_term(item, exclude_terms_list)]
    if include_terms:
        included = [item for item in scoped if matches_any_term(item, include_terms)]
        scoped = included if included else scoped

    recent = []
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    for item in scoped:
        created = int_or_zero(item.get("create_time"))
        if not created or recent_hours <= 0 or 0 <= now - created <= recent_hours * 3600:
            recent.append(item)
    scoped = recent if recent else scoped

    primary = [item for item in scoped if int_or_zero(item.get("like_count")) >= primary_min_likes]
    fallback = [item for item in scoped if int_or_zero(item.get("like_count")) >= fallback_min_likes]
    ranked_pool = primary if len(primary) >= max(1, limit) else fallback if len(fallback) >= max(1, limit) else scoped
    return sorted(
        ranked_pool,
        key=lambda item: (
            int_or_zero(item.get("like_count")),
            int_or_zero(item.get("comment_count")) + int_or_zero(item.get("share_count")),
            int_or_zero(item.get("play_count")),
        ),
        reverse=True,
    )[: max(0, limit)]


def download_selected(
    args: argparse.Namespace,
    selected: list[dict[str, Any]],
    downloads_dir: Path,
    selected_dir: Path,
    run_info: dict[str, Any],
) -> set[str]:
    downloaded: set[str] = set()
    stats: list[dict[str, Any]] = []
    run_info["tikhub_download"] = stats
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.douyin.com/",
        "Accept": "*/*",
    }
    timeout = httpx.Timeout(connect=20, read=60, write=60, pool=20)
    with httpx.Client(headers=headers, timeout=timeout, follow_redirects=True) as client:
        for idx, item in enumerate(selected, 1):
            if count_selected_files(selected_dir) >= args.limit:
                break
            aweme_id = str(item.get("aweme_id") or f"rank{idx}")
            urls = [url for url in item.get("download_urls") or [] if isinstance(url, str)]
            urls = urls[: max(1, args.download_max_urls)]
            if not urls:
                stats.append({"aweme_id": aweme_id, "status": "no_download_url"})
                continue
            for url_idx, url in enumerate(urls, 1):
                target = downloads_dir / f"{idx:02d}_{aweme_id}.mp4"
                temp_target = target.with_suffix(".mp4.part")
                started = time.monotonic()
                bytes_written = 0
                try:
                    with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with temp_target.open("wb") as handle:
                            for chunk in response.iter_bytes():
                                if not chunk:
                                    continue
                                if time.monotonic() - started > args.download_timeout_seconds:
                                    raise TimeoutError("TikHub CDN download timed out")
                                handle.write(chunk)
                                bytes_written += len(chunk)
                    if bytes_written <= 0:
                        raise ValueError("downloaded empty file")
                    temp_target.replace(target)
                    selected_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, selected_dir / target.name)
                    downloaded.add(aweme_id)
                    stats.append(
                        {
                            "aweme_id": aweme_id,
                            "status": "downloaded",
                            "url_index": url_idx,
                            "bytes": bytes_written,
                            "path": str(target),
                        }
                    )
                    break
                except Exception as exc:  # noqa: BLE001
                    if temp_target.exists():
                        temp_target.unlink()
                    stats.append({"aweme_id": aweme_id, "status": "failed", "url_index": url_idx, "error": str(exc)})
            if aweme_id not in downloaded:
                run_info["errors"].append(f"TikHub download failed aweme_id={aweme_id}")
    return downloaded


def write_reports(
    reports_dir: Path,
    keywords: list[str],
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    run_info: dict[str, Any],
) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
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
            "duration_ms",
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
        handle.write("# TikHub Douyin Daily\n\n")
        handle.write(f"- Keywords: {', '.join(keywords)}\n")
        handle.write(f"- Candidates: {len(candidates)}\n")
        handle.write(f"- Selected: {len(selected)}\n")
        selected_files = run_info.get("selected_files")
        if isinstance(selected_files, list):
            handle.write(f"- Selected files: {len(selected_files)}\n")
        handle.write("\n")
        for idx, item in enumerate(selected, 1):
            handle.write(
                f"{idx}. {item.get('title') or item.get('aweme_id')} "
                f"likes={item.get('like_count')} duration={duration_seconds(item)}s keyword={item.get('source_keyword')}\n"
                f"   {item.get('url')}\n"
            )


def record_selected_files(selected_dir: Path, run_info: dict[str, Any]) -> None:
    files = []
    for path in sorted(selected_dir.glob("*")):
        if path.is_file():
            files.append({"name": path.name, "bytes": path.stat().st_size})
    run_info["selected_files"] = files


def count_selected_files(selected_dir: Path) -> int:
    return sum(1 for path in selected_dir.glob("*") if path.is_file())


def selected_aweme_ids(selected_dir: Path) -> set[str]:
    ids: set[str] = set()
    for path in selected_dir.glob("*"):
        if not path.is_file():
            continue
        match = re.search(r"(\d{15,})", path.name)
        if match:
            ids.add(match.group(1))
    return ids


def rewrite_selected_dir(selected_dir: Path, selected: list[dict[str, Any]]) -> None:
    temp_dir = selected_dir / "_final"
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True, exist_ok=True)
    for idx, item in enumerate(selected, 1):
        aweme_id = str(item.get("aweme_id") or "")
        matches = [path for path in selected_dir.glob("*") if path.is_file() and (not aweme_id or aweme_id in path.name)]
        if not matches:
            continue
        source = matches[0]
        shutil.copy2(source, temp_dir / f"{idx:02d}_{aweme_id or source.stem}{source.suffix}")
    for path in selected_dir.glob("*"):
        if path.is_file():
            path.unlink()
    for path in temp_dir.glob("*"):
        shutil.move(str(path), selected_dir / path.name)
    temp_dir.rmdir()


def first_dict(*values: Any) -> dict[str, Any]:
    for value in values:
        if isinstance(value, dict):
            return value
    return {}


def nested_get(value: dict[str, Any], keys: list[str]) -> Any:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def find_first_value(data: Any, keys: set[str]) -> Any:
    if isinstance(data, dict):
        for key in keys:
            if key in data and data[key] not in (None, ""):
                return data[key]
        for value in data.values():
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_first_value(value, keys)
            if found not in (None, ""):
                return found
    return None


def matches_any_term(item: dict[str, Any], terms: list[str]) -> bool:
    text = " ".join(str(item.get(key) or "") for key in ("title", "author", "source_keyword", "aweme_id"))
    return any(term in text for term in terms)


def split_terms(raw: str) -> list[str]:
    return [part.strip() for part in str(raw or "").split(",") if part.strip()]


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^0-9A-Za-z_.\-\u4e00-\u9fff]+", "_", value).strip("_")
    return slug[:40] or "keyword"


def duration_seconds(item: dict[str, Any]) -> int:
    return int_or_zero(item.get("duration_ms")) // 1000


def timestamp_iso(value: int) -> str:
    if not value:
        return ""
    try:
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return ""


def int_or_zero(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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