#!/usr/bin/env python3
"""Merge verified partial XHS Action artifacts into one resumable batch."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import subprocess


@dataclass(frozen=True)
class ArtifactBatch:
    run_id: str
    source_dir: Path
    items: tuple[dict, ...]
    budget_sources: tuple[dict, ...]


FANS_MAX = 20_000
LIKES_MIN = 200


def read_json(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object: {path}")
    return payload


def processed_ids(path: Path | None) -> set[str]:
    if path is None or not path.is_file():
        return set()
    payload = read_json(path)
    entries = payload.get("items") or payload.get("processed") or []
    return {
        str(item if isinstance(item, str) else item.get("note_id") or "")
        for item in entries
        if (item if isinstance(item, str) else item.get("note_id"))
    }


def artifact_budget_sources(source_dir: Path, run_id: str) -> tuple[dict, ...]:
    cumulative = source_dir / "cumulative_tikhub_request_budget.json"
    raw = source_dir / "tikhub_request_budget.json"
    budget_path = cumulative if cumulative.is_file() else raw
    if not budget_path.is_file():
        raise ValueError(f"artifact {run_id} is missing its TikHub request budget")
    payload = read_json(budget_path)
    nested = payload.get("sources")
    if isinstance(nested, list) and nested:
        sources = []
        for source in nested:
            if not isinstance(source, dict):
                raise ValueError(f"artifact {run_id} has an invalid budget source")
            source_id = str(source.get("run_id") or "").strip()
            used = int(source.get("used", -1))
            if not source_id or used < 0:
                raise ValueError(f"artifact {run_id} has an invalid budget source")
            sources.append({"run_id": source_id, "used": used})
        # A failed resumed attempt may not have reached the final cumulative
        # checkpoint yet. Its raw counter is still part of the next recovery.
        if raw.is_file() and run_id not in {source["run_id"] for source in sources}:
            current_used = int(read_json(raw).get("used", -1))
            if current_used < 0:
                raise ValueError(f"artifact {run_id} has an invalid current budget")
            sources.append({"run_id": run_id, "used": current_used})
        return tuple(sources)
    used = int(payload.get("used", -1))
    if used < 0:
        raise ValueError(f"artifact {run_id} has an invalid TikHub request budget")
    if budget_path == cumulative and raw.is_file() and used == 0:
        used = int(read_json(raw).get("used", -1))
    return ({"run_id": run_id, "used": used},)


def verified_quality(item: dict) -> bool:
    fans = int(item.get("author_fans", -1))
    likes = int(item.get("liked_count", -1))
    comments = int(item.get("comments_count", -1))
    return 0 <= fans <= FANS_MAX and likes >= LIKES_MIN and comments >= 3


def validate_media(path: Path) -> None:
    """Require the same codec, geometry and full-decode contract as a fresh render."""

    try:
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(probe.stdout)
        streams = payload.get("streams") or []
        video = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            None,
        )
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        duration = float((payload.get("format") or {}).get("duration") or 0)
        if not video or not audio:
            raise ValueError("video and audio streams are both required")
        if video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
            raise ValueError("expected H.264 video and AAC audio")
        if (int(video.get("width") or 0), int(video.get("height") or 0)) != (1080, 1920):
            raise ValueError("expected 1080x1920 frame size")
        if not 3 <= duration <= 300:
            raise ValueError(f"unexpected duration {duration:.3f}s")
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (json.JSONDecodeError, OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError(f"media validation failed for {path.name}: {exc}") from exc


def load_artifact(run_dir: Path, artifact_date: str, excluded: set[str]) -> ArtifactBatch:
    run_id = run_dir.name
    if not run_id.isdigit():
        raise ValueError(f"resume directory must be named with a run ID: {run_dir}")
    source_dir = run_dir / "outputs" / "xhs_lowfan" / artifact_date
    manifest_paths = [source_dir / "new_processed.json", source_dir / "resume_processed.json"]
    if not any(path.is_file() for path in manifest_paths):
        raise ValueError(
            f"artifact {run_id} does not contain outputs/xhs_lowfan/{artifact_date}/new_processed.json"
        )
    manifest_items = []
    for manifest_path in manifest_paths:
        if manifest_path.is_file():
            manifest_items.extend(read_json(manifest_path).get("items") or [])
    summary_items = []
    for summary_name in ("daily_summary.json", "resume_summary.json"):
        summary_path = source_dir / summary_name
        if summary_path.is_file():
            summary_items.extend(read_json(summary_path).get("items") or [])
    successful = {
        str(item.get("note_id") or ""): item
        for item in summary_items
        if isinstance(item, dict) and item.get("status") == "success" and item.get("note_id")
    }
    items: list[dict] = []
    seen: set[str] = set()
    for raw_item in manifest_items:
        if not isinstance(raw_item, dict):
            raise ValueError(f"artifact {run_id} contains an invalid processed item")
        note_id = str(raw_item.get("note_id") or "").strip()
        output_name = str(raw_item.get("output") or "").strip()
        if not note_id or not output_name or Path(output_name).name != output_name:
            raise ValueError(f"artifact {run_id} contains an invalid note/output record")
        video = source_dir / output_name
        if not video.is_file() or video.suffix.lower() != ".mp4" or video.stat().st_size < 1:
            raise ValueError(f"artifact {run_id} is missing MP4 {output_name}")
        if note_id in excluded or note_id in seen:
            continue
        evidence = successful.get(note_id)
        if evidence is None or Path(str(evidence.get("output") or "")).name != output_name:
            raise ValueError(f"artifact {run_id} has no matching successful summary for {note_id}")
        quality_item = {**raw_item, **evidence}
        if not verified_quality(quality_item):
            raise ValueError(f"artifact {run_id} item {note_id} fails low-fan viral evidence")
        seen.add(note_id)
        items.append(
            {
                **raw_item,
                "note_id": note_id,
                "output": output_name,
                "author_fans": int(quality_item["author_fans"]),
                "liked_count": int(quality_item["liked_count"]),
                "comments_count": int(quality_item.get("comments_count", 0)),
            }
        )
    return ArtifactBatch(
        run_id=run_id,
        source_dir=source_dir,
        items=tuple(items),
        budget_sources=artifact_budget_sources(source_dir, run_id),
    )


def merge_artifacts(
    resume_root: Path,
    artifact_date: str,
    output_dir: Path,
    target: int,
    *,
    processed_manifest: Path | None = None,
    validate_media_files: bool = False,
) -> dict:
    if not 1 <= target <= 5:
        raise ValueError("target must be between 1 and 5")
    excluded = processed_ids(processed_manifest)
    batches = [
        load_artifact(run_dir, artifact_date, excluded)
        for run_dir in resume_root.iterdir()
        if run_dir.is_dir()
    ] if resume_root.is_dir() else []
    # Later attempts contain the fresher view of the target business day. Use
    # them first, then fill any remaining slots from older partial attempts.
    batches.sort(key=lambda batch: int(batch.run_id), reverse=True)

    output_dir.mkdir(parents=True, exist_ok=True)
    selected: list[dict] = []
    selected_ids: set[str] = set()
    output_names: set[str] = set()
    contributing_runs: list[str] = []
    budget_by_run: dict[str, int] = {}

    # Count every attempted run, including attempts that generated no new
    # video, and deduplicate ancestors referenced by later recovery artifacts.
    for batch in batches:
        for source in batch.budget_sources:
            source_id = str(source["run_id"])
            budget_by_run[source_id] = max(budget_by_run.get(source_id, 0), int(source["used"]))
    prior_used = sum(budget_by_run.values())
    if prior_used >= 100:
        raise ValueError(f"resumed artifacts already used {prior_used} TikHub requests; limit is 99")

    for batch in batches:
        contributed = False
        for item in batch.items:
            if len(selected) >= target:
                break
            note_id = item["note_id"]
            source_output_name = item["output"]
            if note_id in selected_ids:
                continue
            base_name = re.sub(r"^\d{2}_", "", source_output_name)
            output_name = f"{len(selected) + 1:02d}_{base_name}"
            if output_name in output_names:
                raise ValueError(f"different resume items use the same output name: {output_name}")
            destination = output_dir / output_name
            if destination.exists():
                raise ValueError(f"resume destination already exists: {destination}")
            source_video = batch.source_dir / source_output_name
            if validate_media_files:
                validate_media(source_video)
            shutil.copy2(source_video, destination)
            selected.append(
                {
                    **item,
                    "output": output_name,
                    "source_run_id": batch.run_id,
                    "source_output": source_output_name,
                }
            )
            selected_ids.add(note_id)
            output_names.add(output_name)
            contributed = True
        if contributed:
            contributing_runs.append(batch.run_id)
        if len(selected) >= target:
            break

    budget_sources = [
        {"run_id": run_id, "used": used}
        for run_id, used in sorted(budget_by_run.items())
    ]

    resume_manifest = output_dir / "resume_processed.json"
    resume_manifest.write_text(
        json.dumps({"date": artifact_date, "items": selected}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "resume_summary.json").write_text(
        json.dumps(
            {"date": artifact_date, "items": [{**item, "status": "success"} for item in selected]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cumulative_budget = output_dir / "cumulative_tikhub_request_budget.json"
    cumulative_budget.write_text(
        json.dumps(
            {"used": prior_used, "limit": 99, "sources": budget_sources},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "artifact_date": artifact_date,
        "target": target,
        "resumed_count": len(selected),
        "target_met": len(selected) == target,
        "contributing_run_ids": contributing_runs,
        "prior_tikhub_requests": prior_used,
        "resume_manifest": str(resume_manifest),
        "cumulative_budget": str(cumulative_budget),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resume-root", type=Path, required=True)
    parser.add_argument("--artifact-date", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--processed-manifest", type=Path)
    parser.add_argument("--validate-media", action="store_true")
    parser.add_argument("--summary", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        summary = merge_artifacts(
            args.resume_root,
            args.artifact_date,
            args.output_dir,
            args.target,
            processed_manifest=args.processed_manifest,
            validate_media_files=args.validate_media,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"resume preparation failed: {exc}") from exc
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
