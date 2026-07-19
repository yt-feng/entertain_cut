#!/usr/bin/env python3
"""Prepare exactly the requested KC videos, using a prior Artifact when needed."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4"}


def main() -> int:
    args = parse_args()
    report = prepare_delivery(
        output_dir=args.output_dir,
        fallback_dir=args.fallback_dir,
        outputs_file=args.outputs_file,
        limit=max(1, args.limit),
        fallback_run_id=args.fallback_run_id,
    )
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "KC delivery set: "
        f"{report['selected_count']}/{report['limit']} videos "
        f"({report['fresh_count']} fresh, {report['reused_count']} reused)."
    )
    print(f"Summary: {args.summary_file}")
    return 0 if report["ready"] else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--fallback-dir", type=Path, default=None)
    parser.add_argument("--outputs-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--fallback-run-id", default="")
    return parser.parse_args()


def prepare_delivery(
    *,
    output_dir: Path,
    fallback_dir: Path | None,
    outputs_file: Path,
    limit: int,
    fallback_run_id: str = "",
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    preferred = read_preferred_outputs(outputs_file, output_dir)
    current = preferred + [path for path in root_videos(output_dir) if path not in preferred]
    selected = current[:limit]
    fresh_count = len(selected)
    reused: list[Path] = []

    if len(selected) < limit and fallback_dir and fallback_dir.exists():
        signatures = {video_signature(path) for path in selected}
        for source in fallback_videos(fallback_dir):
            signature = video_signature(source)
            if signature in signatures:
                continue
            target = unique_target(output_dir, source.name)
            shutil.copy2(source, target)
            selected.append(target)
            reused.append(target)
            signatures.add(signature)
            if len(selected) >= limit:
                break

    selected = selected[:limit]
    selected_set = {path.resolve() for path in selected}
    removed: list[str] = []
    for path in root_videos(output_dir):
        if path.resolve() not in selected_set:
            removed.append(path.name)
            path.unlink()

    outputs_file.parent.mkdir(parents=True, exist_ok=True)
    outputs_file.write_text(
        "".join(f"{path.resolve()}\n" for path in selected if path.exists()),
        encoding="utf-8",
    )
    ready = len(selected) >= limit and all(path.exists() for path in selected)
    return {
        "ready": ready,
        "status": "ready" if ready else "insufficient_videos",
        "limit": limit,
        "fresh_count": fresh_count,
        "reused_count": len(reused),
        "selected_count": len(selected),
        "fallback_run_id": fallback_run_id,
        "fallback_dir": str(fallback_dir) if fallback_dir else "",
        "selected_files": [str(path.resolve()) for path in selected if path.exists()],
        "reused_files": [path.name for path in reused],
        "removed_extra_files": removed,
    }


def read_preferred_outputs(outputs_file: Path, output_dir: Path) -> list[Path]:
    if not outputs_file.exists():
        return []
    output_by_name = {path.name: path for path in root_videos(output_dir)}
    preferred: list[Path] = []
    for raw_line in outputs_file.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        path = Path(raw_line)
        candidate = path if path.exists() and path.parent.resolve() == output_dir.resolve() else output_by_name.get(path.name)
        if candidate and candidate not in preferred:
            preferred.append(candidate)
    return preferred


def root_videos(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda path: path.name,
    )


def fallback_videos(directory: Path) -> list[Path]:
    return sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file()
            and path.suffix.lower() in VIDEO_EXTENSIONS
            and "oversized-originals" not in path.parts
        ),
        key=lambda path: str(path),
    )


def video_signature(path: Path) -> tuple[str, int]:
    return path.name, path.stat().st_size


def unique_target(directory: Path, filename: str) -> Path:
    candidate = directory / filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    for index in range(1, 1000):
        candidate = directory / f"{stem}_fallback{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not allocate a fallback filename for {filename}")


if __name__ == "__main__":
    raise SystemExit(main())
