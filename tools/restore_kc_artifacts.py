#!/usr/bin/env python3
"""Recover validated, source-deduplicated KC videos from today's completed runs."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
from typing import Any


BEIJING = dt.timezone(dt.timedelta(hours=8))
REPORT_ARTIFACT = "douyin-free-daily-reports"
VIDEO_ARTIFACT = "kc-entertain-videos"
LEDGER_FIELDS = (
    "url", "title", "author", "like_count", "comment_count", "share_count",
    "duration_ms", "create_time_iso", "known_entities", "verified_entities",
    "primary_celebrities", "quality_score",
)


def run_command(command: list[str], timeout: int) -> str:
    result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"{command[0]} failed ({result.returncode}): {result.stderr.strip()[:800]}")
    return result.stdout


def read_object(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return data


def same_day_output(value: Any, output_date: str) -> PurePosixPath | None:
    if not isinstance(value, str):
        return None
    path = PurePosixPath(value)
    if ".." in path.parts or path.parent.parts[-3:] != ("outputs", "kc_entertain", output_date):
        return None
    return path if path.suffix.lower() == ".mp4" else None


def source_identity(metadata: Any) -> tuple[str, str] | None:
    if not isinstance(metadata, dict):
        return None
    content_id = str(metadata.get("content_id") or metadata.get("aweme_id") or "").strip()
    platform = str(metadata.get("platform") or "").strip().lower()
    return (platform, content_id) if platform and content_id else None


def eligible_runs(runs: list[dict[str, Any]], args: argparse.Namespace) -> list[str]:
    eligible = []
    for run in runs:
        if (str(run.get("databaseId")) == args.current_run_id
                or run.get("status") != "completed" or run.get("headBranch") != args.branch):
            continue
        try:
            created = dt.datetime.fromisoformat(str(run["createdAt"]).replace("Z", "+00:00"))
            if created.tzinfo is None or created.astimezone(BEIJING).date().isoformat() != args.output_date:
                continue
            run_id = str(run["databaseId"])
            if not run_id.isdigit():
                continue
        except (KeyError, TypeError, ValueError):
            continue
        eligible.append((created, run_id))
    return [run_id for _, run_id in sorted(eligible, reverse=True)[:min(3, max(0, args.max_runs))]]


def validate_video(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or not path.stat().st_size:
        raise ValueError("video is empty, missing, or a symlink")
    probe = json.loads(run_command([
        "ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path),
    ], 30))
    streams = probe.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), {})
    audio = next((item for item in streams if item.get("codec_type") == "audio"), {})
    duration = float(probe.get("format", {}).get("duration", 0))
    if (video.get("codec_name") != "h264" or audio.get("codec_name") != "aac"
            or video.get("width") != 1080 or video.get("height") != 1920
            or not math.isfinite(duration) or not 0 < duration <= 300):
        raise ValueError("video must be H264/AAC, 1080x1920, and no longer than 300 seconds")
    run_command([
        "ffmpeg", "-hide_banner", "-v", "error", "-xerror", "-nostdin", "-threads", "2",
        "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
    ], 180)


def metadata_by_name(reports: Path, output_date: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    ambiguous = set()
    for manifest in reports.rglob("processed_manifest.json"):
        if manifest.is_symlink() or not manifest.resolve().is_relative_to(reports.resolve()):
            continue
        for entry in read_object(manifest).values():
            if not isinstance(entry, dict):
                continue
            output = same_day_output(entry.get("output"), output_date)
            metadata = entry.get("source_metadata")
            identity = source_identity(metadata)
            if output is None or identity is None:
                continue
            if output.name in result and source_identity(result[output.name]) != identity:
                ambiguous.add(output.name)
            result[output.name] = metadata
    return {name: metadata for name, metadata in result.items() if name not in ambiguous}


def selected_artifacts(reports: Path, videos: Path, output_date: str) -> list[tuple[Path, dict[str, Any]]]:
    summaries = list(reports.rglob("kc_delivery_summary.json"))
    if len(summaries) != 1:
        raise ValueError("Expected exactly one delivery summary")
    summary = read_object(summaries[0])
    selected = summary.get("selected_files")
    if not isinstance(selected, list) or summary.get("selected_count") != len(selected):
        raise ValueError("Delivery summary selected file count is inconsistent")
    metadata = metadata_by_name(reports, output_date)
    result = []
    seen_names = set()
    for value in selected:
        output = same_day_output(value, output_date)
        if output is None or output.name in seen_names or output.name not in metadata:
            continue
        seen_names.add(output.name)
        matches = [
            path for path in videos.rglob("*.mp4")
            if path.name == output.name and path.parent.name == output_date
            and "oversized-originals" not in path.relative_to(videos).parts
        ]
        if len(matches) != 1 or not matches[0].resolve().is_relative_to(videos.resolve()):
            continue
        result.append((matches[0], metadata[output.name]))
    return result


def save_state(args: argparse.Namespace, paths: list[Path], report: dict[str, Any]) -> None:
    args.outputs_file.parent.mkdir(parents=True, exist_ok=True)
    args.outputs_file.write_text("".join(f"{path}\n" for path in paths), encoding="utf-8")
    report["selected_count"] = len(paths)
    report["generation_limit"] = max(0, args.limit - len(paths))
    args.work_dir.mkdir(parents=True, exist_ok=True)
    (args.work_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.github_env:
        with args.github_env.open("a", encoding="utf-8") as stream:
            stream.write(f"KC_GENERATION_LIMIT={report['generation_limit']}\n")


def merge_ledger(args: argparse.Namespace, ledger: dict[str, Any], metadata: dict[str, Any], run_id: str) -> None:
    platform, content_id = source_identity(metadata)  # validated before staging
    selected_at = dt.datetime.now(dt.timezone.utc).isoformat()
    item = {key: metadata[key] for key in LEDGER_FIELDS if key in metadata}
    item.update({
        "aweme_id": str(metadata.get("aweme_id") or content_id),
        "content_id": content_id, "platform": platform, "output_date": args.output_date,
        "selected_at": selected_at, "recovered_from_run_id": run_id,
    })
    items = ledger.setdefault("items", [])
    items.append(item)
    ledger.update({"updated_at": selected_at, "count": len(items)})
    args.processed_manifest.parent.mkdir(parents=True, exist_ok=True)
    args.processed_manifest.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def restore(args: argparse.Namespace) -> dict[str, Any]:
    dt.date.fromisoformat(args.output_date)
    args.output_dir = args.output_dir.resolve()
    if args.output_dir.parts[-3:] != ("outputs", "kc_entertain", args.output_date):
        raise ValueError("Output directory must be outputs/kc_entertain/<output-date>")
    args.limit = max(1, args.limit)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    ledger = read_object(args.processed_manifest) if args.processed_manifest.exists() else {"items": []}
    if not isinstance(ledger.get("items", []), list):
        raise ValueError("Processed source ledger items must be a list")
    seen_ids = {identity[1] for item in ledger.get("items", []) if (identity := source_identity(item))}
    seen_ids.update(str(value) for value in ledger.get("aweme_ids", []))
    paths: list[Path] = []
    report: dict[str, Any] = {
        "output_date": args.output_date, "attempted_runs": [], "existing_files": [],
        "restored_files": [], "skipped": [], "errors": [],
    }
    recovered_manifest: dict[str, Any] = {}
    candidates = []
    if args.outputs_file.exists():
        candidates.extend(Path(value) for value in args.outputs_file.read_text(encoding="utf-8").splitlines() if value)
    candidates.extend(sorted(args.output_dir.glob("*.mp4")))
    for candidate in candidates:
        path = candidate.resolve()
        if path in paths or path.parent != args.output_dir or len(paths) >= args.limit:
            continue
        try:
            validate_video(candidate)
        except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
            report["skipped"].append({"file": candidate.name, "reason": str(exc)})
            continue
        paths.append(path)
        report["existing_files"].append(str(path))
    save_state(args, paths, report)
    try:
        if len(paths) >= args.limit:
            return report
        runs = json.loads(run_command([
            "gh", "run", "list", "--repo", args.repo, "--workflow", args.workflow,
            "--branch", args.branch, "--status", "completed", "--limit", "100",
            "--json", "databaseId,headBranch,createdAt,status",
        ], 60))
        if not isinstance(runs, list):
            raise ValueError("GitHub run list is not an array")
        for run_id in eligible_runs(runs, args):
            if len(paths) >= args.limit:
                break
            report["attempted_runs"].append(run_id)
            artifacts = json.loads(run_command([
                "gh", "api", f"repos/{args.repo}/actions/runs/{run_id}/artifacts?per_page=100",
            ], 60))
            names = {item.get("name") for item in artifacts.get("artifacts", []) if not item.get("expired")}
            if not {REPORT_ARTIFACT, VIDEO_ARTIFACT}.issubset(names):
                report["skipped"].append({"run_id": run_id, "reason": "reports or videos artifact unavailable"})
                continue
            with tempfile.TemporaryDirectory(prefix=f"run-{run_id}-", dir=args.work_dir) as temp:
                reports = Path(temp) / "reports"
                videos = Path(temp) / "videos"
                for name, directory in ((REPORT_ARTIFACT, reports), (VIDEO_ARTIFACT, videos)):
                    run_command(["gh", "run", "download", run_id, "--repo", args.repo,
                                 "--name", name, "--dir", str(directory)], 180)
                try:
                    selected = selected_artifacts(reports, videos, args.output_date)
                except (OSError, ValueError, TypeError, AttributeError) as exc:
                    report["skipped"].append({"run_id": run_id, "reason": str(exc)})
                    continue
                for source, metadata in selected:
                    identity = source_identity(metadata)
                    if len(paths) >= args.limit:
                        break
                    if identity[1] in seen_ids:
                        report["skipped"].append({"run_id": run_id, "file": source.name, "reason": "source already processed"})
                        continue
                    destination = args.output_dir / source.name
                    if destination.exists():
                        report["skipped"].append({"run_id": run_id, "file": source.name, "reason": "output name already exists"})
                        continue
                    try:
                        validate_video(source)
                    except (OSError, ValueError, TypeError, RuntimeError, subprocess.TimeoutExpired) as exc:
                        report["skipped"].append({"run_id": run_id, "file": source.name, "reason": str(exc)})
                        continue
                    shutil.copy2(source, destination)
                    merge_ledger(args, ledger, metadata, run_id)
                    seen_ids.add(identity[1])
                    paths.append(destination)
                    recovered_manifest[":".join(identity)] = {"output": str(destination), "source_metadata": metadata}
                    (args.work_dir / "processed_manifest.json").write_text(
                        json.dumps(recovered_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
                    report["restored_files"].append({"run_id": run_id, "file": str(destination),
                                                     "platform": identity[0], "content_id": identity[1]})
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, subprocess.TimeoutExpired) as exc:
        report["errors"].append(str(exc))
    finally:
        save_state(args, paths, report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("repo", "workflow", "branch", "current-run-id", "output-date"):
        parser.add_argument(f"--{name}", required=True)
    for name in ("output-dir", "outputs-file", "processed-manifest", "work-dir"):
        parser.add_argument(f"--{name}", required=True, type=Path)
    parser.add_argument("--github-env", type=Path)
    parser.add_argument("--limit", required=True, type=int)
    parser.add_argument("--max-runs", type=int, default=3)
    args = parser.parse_args()
    try:
        report = restore(args)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        report = {"errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
