#!/usr/bin/env python3
"""Publish KC videos to WebDAV and prepare Git-safe copies."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import quote


DEFAULT_GIT_MAX_BYTES = 99 * 1024 * 1024
DEFAULT_COMPRESSION_TARGET_BYTES = 90 * 1024 * 1024
DEFAULT_WEBDAV_BASE_URL = "https://dav.jianguoyun.com/dav/"
DEFAULT_WEBDAV_ROOT = "我的坚果云/KCdesk/Ops"
DEFAULT_WEBDAV_CATEGORY = "KC娱乐"
DEFAULT_WEBDAV_UPLOAD_CONCURRENCY = 3


def main() -> int:
    args = parse_args()
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
    )
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        "KC publish summary: "
        f"{report['git_ready_count']} Git-ready, "
        f"{report['webdav_uploaded_count']} WebDAV upload(s), "
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
    parser.add_argument("--webdav-root", default=DEFAULT_WEBDAV_ROOT)
    parser.add_argument("--webdav-category", default=DEFAULT_WEBDAV_CATEGORY)
    parser.add_argument(
        "--webdav-upload-concurrency",
        type=int,
        default=DEFAULT_WEBDAV_UPLOAD_CONCURRENCY,
    )
    return parser.parse_args()


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

    upload_results = upload_all_webdav_files(
        videos,
        remote_directory_url,
        directory_result,
        webdav_user,
        webdav_password,
        concurrency=webdav_upload_concurrency,
    )

    report: dict[str, Any] = {
        "output_date": output_date,
        "output_dir": str(output_dir),
        "git_max_bytes": git_max_bytes,
        "compression_target_bytes": compression_target_bytes,
        "remote_directory": remote_directory_url,
        "webdav_directory": directory_result,
        "webdav_upload_concurrency": max(1, webdav_upload_concurrency),
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
    report["webdav_uploaded_count"] = sum(bool(item.get("webdav", {}).get("success")) for item in report["files"])
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
) -> dict[Path, dict[str, Any]]:
    if not videos:
        return {}
    if not directory_result.get("ready"):
        reason = directory_result.get("reason", "WebDAV directory unavailable")
        return {
            video: {"attempted": False, "success": False, "reason": reason}
            for video in videos
        }

    results: dict[Path, dict[str, Any]] = {}
    workers = min(len(videos), max(1, int(concurrency)))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(upload_webdav_file, video, remote_directory_url, username, password): video
            for video in videos
        }
        for future in as_completed(futures):
            video = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - one upload must not block the daily run.
                result = {"attempted": True, "success": False, "error": str(exc)}
            results[video] = result
            if result.get("success"):
                print(f"Uploaded to Jianguoyun: {video.name}")
            else:
                reason = result.get("reason") or result.get("error") or "upload failed"
                print(f"::warning::Jianguoyun upload did not complete for {video.name}: {reason}")
    return results


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
    success = result["http_status"] in {200, 201, 204}
    return {
        "attempted": True,
        "success": success,
        "http_status": result["http_status"],
        "url": url,
        "error": "" if success else result.get("error", "HTTP upload failed"),
    }


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
