#!/usr/bin/env python3
"""Publish KC videos to WebDAV and prepare Git-safe copies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse


DEFAULT_GIT_MAX_BYTES = 99 * 1024 * 1024
DEFAULT_COMPRESSION_TARGET_BYTES = 90 * 1024 * 1024
DEFAULT_WEBDAV_BASE_URL = "https://dav.jianguoyun.com/dav/"
DEFAULT_WEBDAV_ROOT = "我的坚果云/KC Desk Notes/Ops"
DEFAULT_WEBDAV_CATEGORY = "Portal 娱乐"
DEFAULT_WEBDAV_UPLOAD_CONCURRENCY = 3


def main() -> int:
    args = parse_args()
    previous_webdav_sizes = load_previous_webdav_sizes(args.summary_file)
    report = process_directory(
        output_dir=args.output_dir,
        output_date=args.output_date,
        git_max_bytes=args.git_max_bytes,
        compression_target_bytes=args.compression_target_bytes,
        backup_dir=args.backup_dir,
        compression_work_dir=args.compression_work_dir,
        webdav_base_url=args.webdav_base_url,
        webdav_root=args.webdav_root,
        webdav_category=args.webdav_category,
        webdav_upload_concurrency=args.webdav_upload_concurrency,
        webdav_user=os.environ.get("JIANGUOYUN_WEBDAV_USER", "").strip(),
        webdav_password=os.environ.get("JIANGUOYUN_WEBDAV_PASSWORD", "").strip(),
        webdav_prune_extra=args.webdav_prune_extra,
        webdav_prune_prefixes=tuple(args.webdav_prune_prefix),
        previous_webdav_sizes=previous_webdav_sizes,
    )
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "KC publish summary: "
        f"{report['git_ready_count']} Git-ready, "
        f"{report['webdav_verified_count']} WebDAV verified "
        f"({report['webdav_existing_count']} already present, "
        f"{report['webdav_verified_after_attempt_count']} verified after PUT), "
        f"{report['webdav_uploaded_count']} PUT(s) accepted this attempt, "
        f"{report['git_skipped_count']} Git skip(s)."
    )
    print(f"Summary: {args.summary_file}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--output-date", required=True)
    parser.add_argument("--git-max-bytes", type=int, default=DEFAULT_GIT_MAX_BYTES)
    parser.add_argument(
        "--compression-target-bytes",
        type=int,
        default=DEFAULT_COMPRESSION_TARGET_BYTES,
    )
    parser.add_argument("--backup-dir", type=Path, default=Path("work/kc_oversized_originals"))
    parser.add_argument("--compression-work-dir", type=Path, default=Path("work/kc_publish_tmp"))
    parser.add_argument("--summary-file", type=Path, default=Path("work/kc_publish_summary.json"))
    parser.add_argument("--webdav-base-url", default=DEFAULT_WEBDAV_BASE_URL)
    parser.add_argument("--webdav-root", default=os.environ.get("JIANGUOYUN_REMOTE_ROOT") or DEFAULT_WEBDAV_ROOT)
    parser.add_argument("--webdav-category", default=DEFAULT_WEBDAV_CATEGORY)
    parser.add_argument("--webdav-prune-extra", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument(
        "--webdav-prune-prefix", action="append", default=[],
        help="Only prune superseded MP4s with this filename prefix; repeat to add prefixes.",
    )
    parser.add_argument(
        "--webdav-upload-concurrency",
        type=int,
        default=DEFAULT_WEBDAV_UPLOAD_CONCURRENCY,
    )
    return parser.parse_args()


def load_previous_webdav_sizes(summary_file: Path) -> dict[str, set[int]]:
    if not summary_file.is_file():
        return {}
    try:
        report = json.loads(summary_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    sizes_by_name: dict[str, set[int]] = {}
    for item in report.get("files", []):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", ""))
        if not name:
            continue
        raw_sizes = item.get("webdav_expected_sizes", [item.get("size_before")])
        if not isinstance(raw_sizes, list):
            raw_sizes = [raw_sizes]
        sizes: set[int] = set()
        for raw_size in raw_sizes:
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                continue
            if size >= 0:
                sizes.add(size)
        if sizes:
            sizes_by_name[name] = sizes
    return sizes_by_name


def process_directory(
    *,
    output_dir: Path,
    output_date: str,
    git_max_bytes: int,
    compression_target_bytes: int,
    backup_dir: Path,
    compression_work_dir: Path,
    webdav_base_url: str,
    webdav_root: str,
    webdav_category: str,
    webdav_user: str,
    webdav_password: str,
    webdav_upload_concurrency: int = DEFAULT_WEBDAV_UPLOAD_CONCURRENCY,
    webdav_prune_extra: bool = False,
    webdav_prune_prefixes: tuple[str, ...] = (),
    previous_webdav_sizes: dict[str, set[int]] | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.resolve()
    videos = sorted(path for path in output_dir.glob("*.mp4") if path.is_file())
    remote_segments = split_remote_path(webdav_root) + [output_date, webdav_category]
    remote_directory_url = build_webdav_url(webdav_base_url, remote_segments)
    directory_result: dict[str, Any] = {
        "ready": False,
        "reason": "No videos",
        "url": remote_directory_url,
    }

    if videos:
        if webdav_user and webdav_password:
            directory_result = ensure_webdav_directory(
                webdav_base_url,
                remote_segments,
                webdav_user,
                webdav_password,
            )
        else:
            directory_result = {
                "ready": False,
                "reason": "WebDAV credentials are not configured",
                "url": remote_directory_url,
            }
            print("::warning::Jianguoyun WebDAV credentials are not configured; videos stay in Artifact.")

    previous_webdav_sizes = previous_webdav_sizes or {}
    expected_webdav_sizes: dict[Path, set[int]] = {}
    for video in videos:
        expected_webdav_sizes[video] = {
            video.stat().st_size,
            *previous_webdav_sizes.get(video.name, set()),
        }

    upload_results = upload_all_webdav_files(
        videos,
        remote_directory_url,
        directory_result,
        webdav_user,
        webdav_password,
        concurrency=webdav_upload_concurrency,
        expected_sizes=expected_webdav_sizes,
    )
    all_uploaded = bool(videos) and all(bool(upload_results.get(video, {}).get("success")) for video in videos)
    if webdav_prune_extra and all_uploaded:
        prune_result = prune_extra_webdav_videos(
            remote_directory_url,
            {video.name for video in videos},
            webdav_user,
            webdav_password,
            managed_prefixes=webdav_prune_prefixes,
        )
    else:
        prune_result = {
            "attempted": False,
            "success": not webdav_prune_extra,
            "reason": "disabled" if not webdav_prune_extra else "not all current videos uploaded",
            "deleted": [],
        }

    report: dict[str, Any] = {
        "output_date": output_date,
        "output_dir": str(output_dir),
        "git_max_bytes": git_max_bytes,
        "compression_target_bytes": compression_target_bytes,
        "remote_directory": remote_directory_url,
        "webdav_directory": directory_result,
        "webdav_upload_concurrency": max(1, webdav_upload_concurrency),
        "webdav_prune": prune_result,
        "files": [],
    }

    backup_dir.mkdir(parents=True, exist_ok=True)
    compression_work_dir.mkdir(parents=True, exist_ok=True)
    for video in videos:
        before_size = video.stat().st_size
        item: dict[str, Any] = {
            "name": video.name,
            "path": str(video),
            "size_before": before_size,
            "webdav_expected_sizes": sorted(expected_webdav_sizes.get(video, {before_size})),
            "oversized_original": not is_git_safe(before_size, git_max_bytes),
            "webdav": upload_results.get(
                video,
                {"attempted": False, "success": False, "reason": "Upload result unavailable"},
            ),
            "compression": {"attempted": False, "success": False},
        }

        if is_git_safe(before_size, git_max_bytes):
            item["size_after"] = before_size
            item["git_ready"] = True
            item["status"] = "git_ready_original"
            report["files"].append(item)
            print(f"Git-ready: {video.name} ({format_bytes(before_size)})")
            continue

        print(f"Oversized: {video.name} ({format_bytes(before_size)})")
        compressed_path = compression_work_dir / f"{video.stem}.git-safe{video.suffix}"
        compressed_path.unlink(missing_ok=True)
        item["compression"] = compress_video(
            video,
            compressed_path,
            compression_target_bytes,
            git_max_bytes,
        )
        if item["compression"].get("success"):
            backup_path = unique_backup_path(backup_dir, video.name)
            try:
                shutil.copy2(video, backup_path)
                os.replace(compressed_path, video)
                item["original_backup"] = str(backup_path.resolve())
                item["size_after"] = video.stat().st_size
                item["git_ready"] = is_git_safe(item["size_after"], git_max_bytes)
                item["status"] = "compressed_for_git" if item["git_ready"] else "git_skipped_after_compression"
            except OSError as exc:
                compressed_path.unlink(missing_ok=True)
                item["compression"] = {
                    **item["compression"],
                    "success": False,
                    "error": f"Could not install compressed copy: {exc}",
                }
                item["size_after"] = video.stat().st_size
                item["git_ready"] = False
                item["status"] = "git_skipped_original_preserved"
        else:
            compressed_path.unlink(missing_ok=True)
            item["size_after"] = video.stat().st_size
            item["git_ready"] = False
            item["status"] = "git_skipped_original_preserved"

        if item["git_ready"]:
            print(f"Compressed for Git: {video.name} ({format_bytes(item['size_after'])})")
        else:
            print(f"::warning::Skipping oversized Git file: {video.name} ({format_bytes(item['size_after'])})")
        report["files"].append(item)

    report["git_ready_count"] = sum(bool(item.get("git_ready")) for item in report["files"])
    report["git_skipped_count"] = sum(not bool(item.get("git_ready")) for item in report["files"])
    report["webdav_verified_count"] = sum(
        bool(item.get("webdav", {}).get("success")) for item in report["files"]
    )
    report["webdav_existing_count"] = sum(
        bool(item.get("webdav", {}).get("success"))
        and not bool(item.get("webdav", {}).get("attempted"))
        for item in report["files"]
    )
    report["webdav_verified_after_attempt_count"] = sum(
        bool(item.get("webdav", {}).get("success"))
        and bool(item.get("webdav", {}).get("attempted"))
        for item in report["files"]
    )
    report["webdav_uploaded_count"] = sum(
        bool(item.get("webdav", {}).get("put_success")) for item in report["files"]
    )
    report["compression_success_count"] = sum(
        bool(item.get("compression", {}).get("success")) for item in report["files"]
    )
    return report


def upload_all_webdav_files(
    videos: list[Path],
    remote_directory_url: str,
    directory_result: dict[str, Any],
    username: str,
    password: str,
    *,
    concurrency: int,
    expected_sizes: dict[Path, set[int]],
) -> dict[Path, dict[str, Any]]:
    if not videos:
        return {}
    if not directory_result.get("ready"):
        reason = directory_result.get("reason", "WebDAV directory unavailable")
        return {
            video: {
                "attempted": False,
                "success": False,
                "remote_verified": False,
                "phase": "directory",
                "reason": reason,
            }
            for video in videos
        }

    preflight = list_webdav_videos(remote_directory_url, username, password)
    if not preflight.get("success"):
        reason = preflight.get("reason", "WebDAV preflight listing failed")
        print(f"::warning::Could not list Jianguoyun before upload: {reason}")
        return {
            video: {
                "attempted": False,
                "success": False,
                "remote_verified": False,
                "phase": "preflight",
                "reason": reason,
            }
            for video in videos
        }

    results: dict[Path, dict[str, Any]] = {}
    preflight_names = set(preflight.get("names", []))
    missing_videos: list[Path] = []
    for video in videos:
        url = remote_directory_url.rstrip("/") + "/" + quote(video.name, safe="")
        remote_size = matching_webdav_size(video, preflight, expected_sizes)
        if remote_size is not None:
            results[video] = {
                "attempted": False,
                "success": True,
                "remote_verified": True,
                "remote_size": remote_size,
                "phase": "preflight",
                "reason": "matching name and size already present on WebDAV",
                "url": url,
            }
            print(f"Already present on Jianguoyun: {video.name} ({remote_size} bytes)")
        else:
            if video.name in preflight_names:
                print(
                    f"::warning::Jianguoyun has a non-matching copy of {video.name}: "
                    f"remote={preflight.get('sizes', {}).get(video.name)!r}, "
                    f"expected={sorted(expected_sizes.get(video, set()))}"
                )
            missing_videos.append(video)

    if not missing_videos:
        return results

    workers = min(len(missing_videos), max(1, int(concurrency)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(upload_webdav_file, video, remote_directory_url, username, password): video
            for video in missing_videos
        }
        for future in as_completed(futures):
            video = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one upload must not block the daily run.
                result = {
                    "attempted": True,
                    "success": False,
                    "put_success": False,
                    "error": str(exc),
                    "reason": f"PUT raised an exception: {exc}",
                }
            result["put_success"] = bool(result.get("put_success", result.get("success")))
            results[video] = result
            if result.get("put_success"):
                print(f"Jianguoyun accepted PUT: {video.name}")
            else:
                reason = result.get("reason") or result.get("error") or "upload failed"
                print(f"::warning::Jianguoyun upload did not complete for {video.name}: {reason}")

    postflight = list_webdav_videos(remote_directory_url, username, password)
    if not postflight.get("success"):
        verification_reason = postflight.get("reason", "WebDAV post-upload listing failed")
        print(f"::warning::Could not verify Jianguoyun after upload: {verification_reason}")
        for video in missing_videos:
            put_result = results[video]
            put_reason = put_result.get("reason") or put_result.get("error") or "PUT result unavailable"
            results[video] = {
                **put_result,
                "success": False,
                "remote_verified": False,
                "phase": "postflight",
                "reason": f"{put_reason}; remote verification failed: {verification_reason}",
            }
        return results

    for video in videos:
        prior_result = results[video]
        remote_size = matching_webdav_size(video, postflight, expected_sizes)
        if remote_size is not None:
            attempted = bool(prior_result.get("attempted"))
            results[video] = {
                **prior_result,
                "success": True,
                "remote_verified": True,
                "remote_size": remote_size,
                "phase": "postflight",
                "reason": (
                    "matching name and size present after upload attempt"
                    if attempted
                    else "matching name and size present before and after upload"
                ),
            }
            print(f"Verified on Jianguoyun: {video.name} ({remote_size} bytes)")
        else:
            if prior_result.get("attempted"):
                prior_reason = (
                    prior_result.get("reason")
                    or prior_result.get("error")
                    or "PUT result unavailable"
                )
            else:
                prior_reason = "preflight copy disappeared or changed size"
            results[video] = {
                **prior_result,
                "success": False,
                "remote_verified": False,
                "phase": "postflight",
                "reason": (
                    f"{prior_reason}; remote size={postflight.get('sizes', {}).get(video.name)!r}, "
                    f"expected={sorted(expected_sizes.get(video, set()))}"
                ),
            }
            print(
                f"::warning::Jianguoyun delivery is not verified for {video.name}: "
                f"{results[video]['reason']}"
            )
    return results


def matching_webdav_size(
    video: Path,
    listing: dict[str, Any],
    expected_sizes: dict[Path, set[int]],
) -> int | None:
    raw_size = listing.get("sizes", {}).get(video.name)
    try:
        remote_size = int(raw_size)
    except (TypeError, ValueError):
        return None
    return remote_size if remote_size in expected_sizes.get(video, set()) else None


def is_git_safe(size_bytes: int, git_max_bytes: int) -> bool:
    return 0 <= size_bytes < git_max_bytes


def split_remote_path(value: str) -> list[str]:
    segments = [segment for segment in value.strip("/").split("/") if segment]
    if any(segment in {".", ".."} for segment in segments):
        raise ValueError("WebDAV path cannot contain '.' or '..'")
    return segments


def build_webdav_url(base_url: str, segments: list[str]) -> str:
    base = base_url.rstrip("/") + "/"
    return base + "/".join(quote(segment, safe="") for segment in segments) + "/"


def ensure_webdav_directory(
    base_url: str,
    segments: list[str],
    username: str,
    password: str,
) -> dict[str, Any]:
    current: list[str] = []
    for segment in segments:
        current.append(segment)
        url = build_webdav_url(base_url, current)
        result = run_curl(
            ["--request", "MKCOL", url],
            username,
            password,
            timeout_seconds=120,
        )
        if result["http_status"] not in {200, 201, 204, 405}:
            return {
                "ready": False,
                "reason": f"MKCOL failed with HTTP {result['http_status'] or 'transport error'}",
                "url": url,
                "curl_error": result.get("error", ""),
            }
    return {"ready": True, "reason": "ready", "url": build_webdav_url(base_url, segments)}


def upload_webdav_file(
    path: Path,
    remote_directory_url: str,
    username: str,
    password: str,
) -> dict[str, Any]:
    url = remote_directory_url.rstrip("/") + "/" + quote(path.name, safe="")
    result = run_curl(
        ["--upload-file", str(path), url],
        username,
        password,
        timeout_seconds=3600,
    )
    try:
        http_status = int(result.get("http_status", 0))
    except (TypeError, ValueError):
        http_status = 0
    curl_error = str(result.get("error", "")).strip()
    returncode = result.get("returncode")
    put_success = http_status in {200, 201, 204} and returncode in {None, 0}
    if put_success:
        reason = "PUT accepted"
    elif http_status:
        reason = f"PUT failed with HTTP {http_status}"
    else:
        reason = "PUT failed with a transport error"
    if not put_success and curl_error:
        reason = f"{reason}: {curl_error}"
    return {
        "attempted": True,
        "success": put_success,
        "put_success": put_success,
        "http_status": http_status,
        "returncode": returncode,
        "url": url,
        "error": "" if put_success else curl_error,
        "reason": reason,
    }


def prune_extra_webdav_videos(
    remote_directory_url: str,
    expected_names: set[str],
    username: str,
    password: str,
    *,
    managed_prefixes: tuple[str, ...] = (),
) -> dict[str, Any]:
    prefixes = tuple(prefix for prefix in managed_prefixes if prefix)
    if not prefixes:
        return {
            "attempted": False, "success": False, "deleted": [],
            "reason": "no managed filename prefixes configured for this shared folder",
        }
    listing = list_webdav_videos(remote_directory_url, username, password)
    if not listing.get("success"):
        return {"attempted": True, "success": False, "deleted": [], "listing": listing}
    extras = sorted(
        name for name in set(listing.get("names", [])) - expected_names
        if name.startswith(prefixes)
    )
    deleted: list[str] = []
    errors: list[dict[str, Any]] = []
    for name in extras:
        url = remote_directory_url.rstrip("/") + "/" + quote(name, safe="")
        result = run_curl(["--request", "DELETE", url], username, password, timeout_seconds=120)
        if result.get("http_status") in {200, 204, 404}:
            deleted.append(name)
            print(f"Removed superseded Jianguoyun file: {name}")
        else:
            errors.append({"name": name, **result})
    return {
        "attempted": True,
        "success": not errors,
        "deleted": deleted,
        "errors": errors,
        "listed_count": len(listing.get("names", [])),
        "managed_prefixes": list(prefixes),
    }


def list_webdav_videos(remote_directory_url: str, username: str, password: str) -> dict[str, Any]:
    if not shutil.which("curl"):
        return {"success": False, "reason": "curl is unavailable", "names": [], "sizes": {}}
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--retry",
        "2",
        "--retry-all-errors",
        "--connect-timeout",
        "20",
        "--max-time",
        "120",
        "--user",
        f"{username}:{password}",
        "--request",
        "PROPFIND",
        "--header",
        "Depth: 1",
        "--output",
        "-",
        "--write-out",
        "\n%{http_code}",
        remote_directory_url,
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=150)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"success": False, "reason": str(exc), "names": [], "sizes": {}}
    body, separator, raw_status = completed.stdout.rpartition("\n")
    http_status = int(raw_status) if separator and raw_status.isdigit() else 0
    if http_status not in {200, 207}:
        reason = (
            f"PROPFIND failed with HTTP {http_status}"
            if http_status
            else "PROPFIND failed with a transport error"
        )
        return {
            "success": False,
            "reason": reason,
            "error": completed.stderr.strip()[-1000:],
            "names": [],
            "sizes": {},
        }
    try:
        sizes = parse_webdav_video_entries(body)
    except ET.ParseError as exc:
        return {
            "success": False,
            "reason": f"invalid PROPFIND XML: {exc}",
            "names": [],
            "sizes": {},
        }
    return {
        "success": True,
        "http_status": http_status,
        "names": list(sizes),
        "sizes": sizes,
    }


def parse_webdav_video_names(xml_text: str) -> list[str]:
    return list(parse_webdav_video_entries(xml_text))


def parse_webdav_video_entries(xml_text: str) -> dict[str, int | None]:
    root = ET.fromstring(xml_text)
    entries: dict[str, int | None] = {}
    for response in root.findall(".//{DAV:}response"):
        href_node = response.find("{DAV:}href")
        if href_node is None:
            continue
        href = str(href_node.text or "")
        path = unquote(urlparse(href).path)
        if path.endswith("/"):
            continue
        name = path.rsplit("/", 1)[-1] if path else ""
        if not name.lower().endswith(".mp4"):
            continue
        size: int | None = None
        size_node = response.find(".//{DAV:}getcontentlength")
        if size_node is not None:
            try:
                parsed_size = int(str(size_node.text or ""))
            except ValueError:
                pass
            else:
                if parsed_size >= 0:
                    size = parsed_size
        if name not in entries or entries[name] is None:
            entries[name] = size
    return entries


def run_curl(
    request_args: list[str],
    username: str,
    password: str,
    *,
    timeout_seconds: int,
) -> dict[str, Any]:
    if not shutil.which("curl"):
        return {"http_status": 0, "error": "curl is unavailable"}
    command = [
        "curl",
        "--silent",
        "--show-error",
        "--retry",
        "3",
        "--retry-all-errors",
        "--connect-timeout",
        "20",
        "--max-time",
        str(timeout_seconds),
        "--user",
        f"{username}:{password}",
        "--output",
        "/dev/null",
        "--write-out",
        "%{http_code}",
        *request_args,
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout_seconds + 30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"http_status": 0, "error": str(exc)}
    raw_status = completed.stdout.strip()[-3:]
    http_status = int(raw_status) if raw_status.isdigit() else 0
    return {
        "http_status": http_status,
        "error": completed.stderr.strip()[-1000:],
        "returncode": completed.returncode,
    }


def compress_video(
    source: Path,
    target: Path,
    target_bytes: int,
    git_max_bytes: int,
) -> dict[str, Any]:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        return {"attempted": False, "success": False, "reason": "ffmpeg or ffprobe is unavailable"}
    duration = probe_duration(source)
    if duration <= 0:
        return {"attempted": True, "success": False, "reason": "Could not determine video duration"}

    audio_kbps = 96
    video_kbps = calculate_video_bitrate_kbps(target_bytes, duration, audio_kbps)
    target.parent.mkdir(parents=True, exist_ok=True)
    passlog = target.parent / f"{target.stem}.ffmpeg2pass"
    common = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-b:v",
        f"{video_kbps}k",
        "-vf",
        "scale=w='min(1080,iw)':h=-2",
        "-pix_fmt",
        "yuv420p",
        "-passlogfile",
        str(passlog),
    ]
    first_pass = [*common, "-pass", "1", "-an", "-f", "null", os.devnull]
    second_pass = [
        *common,
        "-pass",
        "2",
        "-map",
        "0:a:0?",
        "-c:a",
        "aac",
        "-b:a",
        f"{audio_kbps}k",
        "-movflags",
        "+faststart",
        str(target),
    ]
    try:
        first = subprocess.run(first_pass, check=False, capture_output=True, text=True)
        if first.returncode != 0:
            return {
                "attempted": True,
                "success": False,
                "reason": "ffmpeg first pass failed",
                "error": first.stderr.strip()[-1000:],
            }
        second = subprocess.run(second_pass, check=False, capture_output=True, text=True)
        if second.returncode != 0 or not target.is_file():
            return {
                "attempted": True,
                "success": False,
                "reason": "ffmpeg second pass failed",
                "error": second.stderr.strip()[-1000:],
            }
        output_size = target.stat().st_size
        return {
            "attempted": True,
            "success": is_git_safe(output_size, git_max_bytes),
            "duration_seconds": round(duration, 3),
            "video_bitrate_kbps": video_kbps,
            "output_size": output_size,
            "reason": "compressed below Git limit" if is_git_safe(output_size, git_max_bytes) else "compressed file is still oversized",
        }
    except OSError as exc:
        return {"attempted": True, "success": False, "reason": "ffmpeg could not start", "error": str(exc)}
    finally:
        for path in passlog.parent.glob(passlog.name + "*"):
            path.unlink(missing_ok=True)


def probe_duration(path: Path) -> float:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
        return float(completed.stdout.strip()) if completed.returncode == 0 else 0.0
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return 0.0


def calculate_video_bitrate_kbps(target_bytes: int, duration_seconds: float, audio_kbps: int) -> int:
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    total_kbps = (target_bytes * 8) / duration_seconds / 1000
    return max(250, int(total_kbps * 0.95) - audio_kbps)


def unique_backup_path(backup_dir: Path, filename: str) -> Path:
    candidate = backup_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    index = 2
    while True:
        candidate = backup_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def format_bytes(value: int) -> str:
    return f"{value / (1024 * 1024):.2f} MiB"


if __name__ == "__main__":
    raise SystemExit(main())

