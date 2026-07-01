#!/usr/bin/env python3
"""Fetch top recent Douyin entertainment videos and optionally package them as KC clips."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MEDIA_ROOT = ROOT / "work" / "douyin_entertain"
SEARCH_PATHS = {
    "general_v2": "/api/v1/douyin/search/fetch_general_search_v2",
    "v1": "/api/v1/douyin/search/fetch_video_search_v1",
    "v2": "/api/v1/douyin/search/fetch_video_search_v2",
}
DEFAULT_BASE_URL = "https://api.tikhub.io"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".mkv"}
DESKTOP_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@dataclass
class ApiClient:
    base_url: str
    api_key: str
    timeout: int = 60
    retries: int = 3

    def post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = self._url(path)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "media_hub/douyin_entertain_kc",
        }
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                req = urllib.request.Request(url, data=body, headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                code = data.get("code")
                status_code = data.get("status_code")
                if code not in (None, 0, 200) and status_code not in (None, 0, 200):
                    raise RuntimeError(f"API returned code={code} status_code={status_code}")
                return data
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:700]
                except Exception:
                    detail = exc.reason
                last_error = RuntimeError(f"HTTP {exc.code}: {detail}")
                if 400 <= exc.code < 500:
                    break
            except (urllib.error.URLError, json.JSONDecodeError, RuntimeError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(0.8 * attempt)
        raise RuntimeError(f"POST {path} failed after {self.retries} attempts: {last_error}") from last_error

    def _url(self, path: str) -> str:
        path = path if path.startswith("/") else f"/{path}"
        return f"{self.base_url.rstrip('/')}{path}"


def main() -> int:
    args = parse_args()
    load_dotenv(ROOT / ".env")
    load_dotenv(MEDIA_ROOT / ".env")
    run_dir = args.run_dir or MEDIA_ROOT / "runs" / f"douyin_entertain_{dt.datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = project_path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)

    api_key = resolve_tikhub_key(args.api_key_file)
    base_url = args.base_url or os.getenv("TIKHUB_BASE_URL") or DEFAULT_BASE_URL
    client = ApiClient(base_url=base_url, api_key=api_key)

    print(f"Searching Douyin keyword={args.keyword!r} publish_time={args.publish_time} sort_type={args.sort_type}")
    search_path = SEARCH_PATHS[args.search_api_version]
    candidates = collect_candidates(client, args, run_dir, search_path)
    selected, threshold_used = select_candidates(
        candidates,
        limit=args.limit,
        primary_min_likes=args.primary_min_likes,
        fallback_min_likes=args.fallback_min_likes,
    )
    write_search_outputs(run_dir, candidates, selected, threshold_used, args, base_url, search_path)
    print_selected(selected, threshold_used)

    downloads: list[dict[str, Any]] = []
    kc_outputs: list[str] = []
    if args.kc:
        args.download = True
    if args.download:
        download_dir = project_path(args.download_dir)
        download_dir.mkdir(parents=True, exist_ok=True)
        downloads = download_selected(selected, download_dir)
        write_json(run_dir / "downloads.json", downloads)
    if args.kc:
        downloaded_paths = [Path(item["local_path"]) for item in downloads if item.get("status") == "ok" and item.get("local_path")]
        if not downloaded_paths:
            raise SystemExit("No downloaded videos available for KC packaging.")
        kc_outputs = run_kc_packaging(args)
        write_json(run_dir / "kc_outputs.json", kc_outputs)

    update_summary(run_dir, downloads, kc_outputs)
    print(f"Run directory: {run_dir}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keyword", default="娱乐")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--max-search-pages", type=int, default=1, help="Each page is one billable TikHub request.")
    parser.add_argument("--primary-min-likes", type=int, default=10_000)
    parser.add_argument("--fallback-min-likes", type=int, default=1_000)
    parser.add_argument("--local-recent-hours", type=int, default=24)
    parser.add_argument("--sort-type", default="1", help="TikHub Douyin sort_type: 1=most likes.")
    parser.add_argument("--search-api-version", choices=sorted(SEARCH_PATHS), default="v2")
    parser.add_argument("--publish-time", default="1", help="TikHub Douyin publish_time: 1=last day.")
    parser.add_argument("--filter-duration", default="0")
    parser.add_argument("--content-type", default="0", help="TikHub content_type; video search docs use 0.")
    parser.add_argument("--base-url", default="")
    parser.add_argument("--api-key-file", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--download", action="store_true", help="Download selected videos using free methods first.")
    parser.add_argument("--download-dir", type=Path, default=ROOT / "new_video_pending")
    parser.add_argument("--kc", action="store_true", help="Run auto_kc_entertain.py on downloaded videos.")
    parser.add_argument("--kc-output-dir", type=Path, default=ROOT / "outputs" / "kc_entertain")
    parser.add_argument("--kc-work-dir", type=Path, default=ROOT / "work" / "auto_kc")
    parser.add_argument("--encoder", choices=["auto", "videotoolbox", "libx264"], default="libx264")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-fallback", action="store_true", help="Pass through to auto_kc_entertain.py.")
    return parser.parse_args()


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def resolve_tikhub_key(api_key_file: Path | None) -> str:
    env_key = os.environ.get("TIKHUB_API_KEY", "").strip()
    if env_key:
        return env_key
    candidates = [
        api_key_file,
        ROOT / "api_key" / "tikhub.txt",
        ROOT / "api_key" / "TikHub_api.txt",
        ROOT / "api_key" / "TIKHUB_API_KEY.txt",
    ]
    for path in candidates:
        if path and path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    raise SystemExit("TIKHUB_API_KEY is required. Set the env var or provide api_key/tikhub.txt locally.")


def collect_candidates(client: ApiClient, args: argparse.Namespace, run_dir: Path, search_path: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    cursor = 0
    search_id = ""
    backtrace = ""

    for page in range(1, max(1, args.max_search_pages) + 1):
        payload = {
            "keyword": args.keyword,
            "cursor": cursor,
            "sort_type": str(args.sort_type),
            "publish_time": str(args.publish_time),
            "filter_duration": str(args.filter_duration),
            "content_type": str(args.content_type),
            "search_id": search_id,
            "backtrace": backtrace,
        }
        data = client.post_json(search_path, payload)
        write_json(run_dir / f"raw_page_{page:02d}.json", data)
        for aweme in iter_aweme_infos(data):
            row = normalize_aweme(aweme, args.keyword)
            aweme_id = row.get("aweme_id", "")
            if not aweme_id or aweme_id in seen_ids:
                continue
            seen_ids.add(aweme_id)
            if args.local_recent_hours > 0 and not is_recent(row.get("create_time", 0), args.local_recent_hours):
                continue
            rows.append(row)
        next_page = extract_pagination(data)
        cursor = as_int(next_page.get("cursor"))
        search_id = str(next_page.get("search_id") or search_id or "")
        backtrace = str(next_page.get("backtrace") or backtrace or "")
        if not next_page.get("has_more") or not cursor:
            break

    rows.sort(key=lambda item: (as_int(item.get("like_count")), as_int(item.get("play_count"))), reverse=True)
    return rows


def iter_aweme_infos(data: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            aweme_info = value.get("aweme_info")
            if isinstance(aweme_info, dict):
                found.append(aweme_info)
                return
            if value.get("aweme_id") and ("statistics" in value or "video" in value):
                found.append(value)
                return
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(data)
    return found


def normalize_aweme(aweme: dict[str, Any], keyword: str) -> dict[str, Any]:
    author = aweme.get("author") or {}
    stats = aweme.get("statistics") or aweme.get("stats") or {}
    video = aweme.get("video") or {}
    aweme_id = str(aweme.get("aweme_id") or aweme.get("id") or "")
    create_time = as_int(aweme.get("create_time"))
    share_url = first_present(aweme, "share_url", "shareUrl") or nested_get(aweme, ["share_info", "share_url"]) or ""
    if not share_url and aweme_id:
        share_url = f"https://www.douyin.com/video/{aweme_id}"
    title = clean_title(str(aweme.get("desc") or aweme.get("caption") or aweme.get("title") or ""))
    download_urls = collect_video_urls(video)
    return {
        "source_keyword": keyword,
        "aweme_id": aweme_id,
        "title": title,
        "create_time": create_time,
        "create_time_iso": timestamp_iso(create_time),
        "author_uid": str(author.get("uid") or ""),
        "author_sec_uid": str(author.get("sec_uid") or ""),
        "author_short_id": str(author.get("short_id") or ""),
        "author_unique_id": str(author.get("unique_id") or author.get("short_id") or ""),
        "author_nickname": str(author.get("nickname") or ""),
        "author_followers": as_int(author.get("follower_count")),
        "like_count": as_int(first_present(stats, "digg_count", "like_count", "diggCount")),
        "play_count": as_int(first_present(stats, "play_count", "view_count", "playCount")),
        "comment_count": as_int(first_present(stats, "comment_count", "commentCount")),
        "share_count": as_int(first_present(stats, "share_count", "shareCount")),
        "collect_count": as_int(first_present(stats, "collect_count", "collectCount")),
        "duration_ms": as_int(video.get("duration")),
        "share_url": strip_tracking(str(share_url)),
        "direct_video_urls": download_urls,
        "direct_video_url_count": len(download_urls),
    }


def collect_video_urls(video: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    preferred_keys = [
        "play_addr_h264",
        "play_addr_bytevc1",
        "play_addr_265",
        "play_addr",
        "download_addr",
    ]

    for key in preferred_keys:
        value = video.get(key)
        if isinstance(value, dict):
            urls.extend(urls_from_addr(value))
    for item in video.get("bit_rate") or []:
        if not isinstance(item, dict):
            continue
        for key in preferred_keys:
            value = item.get(key)
            if isinstance(value, dict):
                urls.extend(urls_from_addr(value))

    result: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if not url or url in seen:
            continue
        lowered = url.lower()
        if "playwm" in lowered or "watermark" in lowered:
            continue
        seen.add(url)
        result.append(url)
    return result


def urls_from_addr(addr: dict[str, Any]) -> list[str]:
    raw_urls = addr.get("url_list") or []
    if isinstance(raw_urls, str):
        raw_urls = [raw_urls]
    return [str(url) for url in raw_urls if isinstance(url, str) and url.startswith(("http://", "https://"))]


def extract_pagination(data: dict[str, Any]) -> dict[str, Any]:
    containers = [data, data.get("data") if isinstance(data.get("data"), dict) else {}]
    for container in containers:
        if not isinstance(container, dict):
            continue
        result = {
            "cursor": first_present(container, "cursor", "next_cursor"),
            "has_more": first_present(container, "has_more", "hasMore"),
            "search_id": first_present(container, "search_id", "searchId"),
            "backtrace": container.get("backtrace"),
        }
        if any(value not in (None, "", 0) for value in result.values()):
            result["has_more"] = bool(as_int(result.get("has_more")))
            return result
    return {"cursor": 0, "has_more": False, "search_id": "", "backtrace": ""}


def select_candidates(
    rows: list[dict[str, Any]],
    *,
    limit: int,
    primary_min_likes: int,
    fallback_min_likes: int,
) -> tuple[list[dict[str, Any]], int]:
    rows = sorted(rows, key=lambda item: (as_int(item.get("like_count")), as_int(item.get("play_count"))), reverse=True)
    primary = [row for row in rows if as_int(row.get("like_count")) >= primary_min_likes]
    if primary:
        return primary[:limit], primary_min_likes
    fallback = [row for row in rows if as_int(row.get("like_count")) >= fallback_min_likes]
    if fallback:
        return fallback[:limit], fallback_min_likes
    return rows[:limit], 0


def write_search_outputs(
    run_dir: Path,
    candidates: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    threshold_used: int,
    args: argparse.Namespace,
    base_url: str,
    search_path: str,
) -> None:
    summary = {
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "base_url": base_url,
        "search_path": search_path,
        "keyword": args.keyword,
        "max_search_pages": args.max_search_pages,
        "limit": args.limit,
        "primary_min_likes": args.primary_min_likes,
        "fallback_min_likes": args.fallback_min_likes,
        "local_recent_hours": args.local_recent_hours,
        "threshold_used": threshold_used,
        "candidate_count": len(candidates),
        "selected_count": len(selected),
        "selected": selected,
    }
    write_json(run_dir / "summary.json", summary)
    write_candidates_csv(run_dir / "douyin_candidates.csv", candidates)
    (run_dir / "selected_links.txt").write_text(
        "\n".join(row.get("share_url", "") for row in selected if row.get("share_url")) + ("\n" if selected else ""),
        encoding="utf-8",
    )
    write_report(run_dir / "report.md", selected, threshold_used, candidates)


def update_summary(run_dir: Path, downloads: list[dict[str, Any]], kc_outputs: list[str]) -> None:
    summary_path = run_dir / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["downloads"] = downloads
    summary["kc_outputs"] = kc_outputs
    write_json(summary_path, summary)


def write_candidates_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "aweme_id",
        "title",
        "create_time_iso",
        "author_nickname",
        "author_unique_id",
        "author_followers",
        "like_count",
        "play_count",
        "comment_count",
        "share_count",
        "collect_count",
        "duration_ms",
        "share_url",
        "direct_video_url_count",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key, "") for key in fieldnames})


def write_report(path: Path, selected: list[dict[str, Any]], threshold_used: int, candidates: list[dict[str, Any]]) -> None:
    label = f">= {threshold_used}" if threshold_used else "top available"
    lines = [
        "# Douyin Entertainment KC Run",
        "",
        f"- Candidates fetched: {len(candidates)}",
        f"- Selection likes threshold: {label}",
        "",
        "## Selected Links",
    ]
    if not selected:
        lines.append("- No selected videos.")
    for idx, row in enumerate(selected, start=1):
        title = row.get("title") or "(no title)"
        lines.append(
            f"{idx}. likes={row.get('like_count')} plays={row.get('play_count')} "
            f"author={row.get('author_nickname')} created={row.get('create_time_iso')}  \n"
            f"   {title}  \n"
            f"   {row.get('share_url')}"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def print_selected(selected: list[dict[str, Any]], threshold_used: int) -> None:
    print(f"Selected {len(selected)} videos; threshold={threshold_used or 'top available'}")
    for idx, row in enumerate(selected, start=1):
        title = row.get("title") or "(no title)"
        print(f"{idx}. likes={row.get('like_count')} plays={row.get('play_count')} {title[:80]}")
        print(f"   {row.get('share_url')}")


def download_selected(selected: list[dict[str, Any]], download_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for idx, row in enumerate(selected, start=1):
        target = download_dir / f"{idx:02d}_{row.get('like_count', 0)}_{safe_filename(row.get('title') or row.get('aweme_id') or 'douyin')}.mp4"
        result = {"aweme_id": row.get("aweme_id"), "share_url": row.get("share_url"), "target": str(target), "status": "failed", "method": ""}
        try:
            downloaded = download_one(row, target)
            result.update({"status": "ok", "method": downloaded["method"], "local_path": str(downloaded["path"])})
        except Exception as exc:  # noqa: BLE001 - keep later videos moving.
            result["error"] = str(exc)
            print(f"Download failed for {row.get('aweme_id')}: {exc}", file=sys.stderr)
        results.append(result)
    return results


def download_one(row: dict[str, Any], target: Path) -> dict[str, Any]:
    share_url = str(row.get("share_url") or "")
    direct_urls = list(row.get("direct_video_urls") or [])
    for direct_url in direct_urls:
        try:
            output = download_direct(direct_url, target, referer=share_url or "https://www.douyin.com/")
            return {"method": "direct_url_from_search", "path": output}
        except Exception as exc:  # noqa: BLE001 - try the next CDN URL.
            print(f"direct download failed: {exc}", file=sys.stderr)
    if share_url and shutil.which("yt-dlp"):
        try:
            output = download_with_ytdlp(share_url, target)
            return {"method": "yt-dlp share_url", "path": output}
        except Exception as exc:  # noqa: BLE001 - direct URLs from the same TikHub search may still work.
            print(f"yt-dlp failed for {share_url}: {exc}", file=sys.stderr)
    if share_url:
        output = download_with_ffmpeg(share_url, target)
        return {"method": "ffmpeg share_url", "path": output}
    raise RuntimeError("No usable share URL or direct video URL found.")


def download_with_ytdlp(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    before = set(target.parent.glob(f"{target.stem}.*"))
    template = str(target.with_suffix(".%(ext)s"))
    command = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--socket-timeout",
        "20",
        "--retries",
        "1",
        "--extractor-retries",
        "1",
        "--merge-output-format",
        "mp4",
        "-f",
        "bv*+ba/best",
        "-o",
        template,
    ]
    proxy = download_proxy()
    if proxy:
        command.extend(["--proxy", proxy])
    command.append(url)
    subprocess.run(command, check=True)
    after = set(target.parent.glob(f"{target.stem}.*"))
    candidates = sorted(after - before or after, key=lambda path: path.stat().st_size if path.exists() else 0, reverse=True)
    for candidate in candidates:
        if candidate.suffix.lower() in VIDEO_EXTENSIONS and is_video_file(candidate):
            if candidate != target and candidate.suffix.lower() != ".mp4":
                converted = target
                convert_to_mp4(candidate, converted)
                return converted
            if candidate != target:
                candidate.replace(target)
            return target
    raise RuntimeError("yt-dlp did not produce a valid video file")


def download_direct(url: str, target: Path, *, referer: str) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": DESKTOP_UA,
            "Referer": referer,
            "Accept": "*/*",
        },
        method="GET",
    )
    tmp = target.with_suffix(".part")
    opener = urllib.request.build_opener()
    proxy = download_proxy()
    if proxy.startswith(("http://", "https://")):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    with opener.open(req, timeout=120) as resp, tmp.open("wb") as handle:
        shutil.copyfileobj(resp, handle)
    if tmp.stat().st_size < 200_000:
        body = tmp.read_bytes()[:200].decode("utf-8", errors="replace")
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded response too small: {body}")
    tmp.replace(target)
    if not is_video_file(target):
        target.unlink(missing_ok=True)
        raise RuntimeError("downloaded file is not a playable video")
    return target


def download_with_ffmpeg(url: str, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-loglevel",
        "error",
        "-user_agent",
        DESKTOP_UA,
    ]
    proxy = download_proxy()
    if proxy.startswith(("http://", "https://")):
        command.extend(["-http_proxy", proxy])
    command.extend(["-i", url, "-c", "copy", str(target)])
    subprocess.run(command, check=True)
    if not is_video_file(target):
        raise RuntimeError("ffmpeg output is not a playable video")
    return target


def is_video_file(path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(path),
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    return "video" in result.stdout


def convert_to_mp4(source: Path, target: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(source),
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(target),
        ],
        check=True,
    )


def download_proxy() -> str:
    return (
        os.getenv("DOUYIN_PROXY_URL", "").strip()
        or os.getenv("DOUYIN_HTTPS_PROXY", "").strip()
        or os.getenv("DOUYIN_HTTP_PROXY", "").strip()
    )


def run_kc_packaging(args: argparse.Namespace) -> list[str]:
    output_dir = project_path(args.kc_output_dir)
    work_dir = project_path(args.kc_work_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "python3",
        str(ROOT / "auto_kc_entertain.py"),
        "--input-dir",
        str(project_path(args.download_dir)),
        "--output-dir",
        str(output_dir),
        "--work-dir",
        str(work_dir),
        "--encoder",
        args.encoder,
        "--threads",
        str(max(1, args.threads)),
        "--all",
    ]
    if args.force:
        command.append("--force")
    if args.force_fallback:
        command.append("--force-fallback")
    subprocess.run(command, check=True, cwd=ROOT)
    outputs_file = work_dir / "last_run_outputs.txt"
    if not outputs_file.exists():
        return []
    return [line.strip() for line in outputs_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def project_path(path: Path) -> Path:
    return path if path.is_absolute() else ROOT / path


def first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def nested_get(mapping: dict[str, Any], keys: list[str]) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def timestamp_iso(timestamp: int) -> str:
    if not timestamp:
        return ""
    return dt.datetime.fromtimestamp(timestamp, tz=dt.timezone.utc).isoformat()


def is_recent(timestamp: Any, recent_hours: int) -> bool:
    ts = as_int(timestamp)
    if not ts:
        return False
    created = dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc)
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=recent_hours)
    return created >= cutoff


def clean_title(text: str) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:240]


def strip_tracking(url: str) -> str:
    if not url:
        return ""
    parsed = urllib.parse.urlsplit(url)
    keep = []
    for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        if key.lower() in {"utm_source", "utm_medium", "utm_campaign", "share_id", "share_token"}:
            continue
        keep.append((key, value))
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(keep), ""))


def safe_filename(text: str, *, max_len: int = 60) -> str:
    text = re.sub(r"[\\/:*?\"<>|#%&{}$!`'@+=\n\r\t]", "", str(text))
    text = re.sub(r"\s+", "_", text).strip("._ ")
    return (text or "douyin_video")[:max_len]


if __name__ == "__main__":
    raise SystemExit(main())
