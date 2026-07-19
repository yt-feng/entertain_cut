#!/usr/bin/env python3
"""Keep at most the requested number of current-run KC videos."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4"}


def main() -> int:
    args = parse_args()
    report = prepare_delivery(
        output_dir=args.output_dir,
        outputs_file=args.outputs_file,
        limit=max(1, args.limit),
    )
    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        "KC delivery set: "
        f"{report['selected_count']}/{report['limit']} current-run videos."
    )
    print(f"Summary: {args.summary_file}")
    return 0 if report["ready"] else 2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--outputs-file", type=Path, required=True)
    parser.add_argument("--summary-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5)
    return parser.parse_args()


def prepare_delivery(
    *,
    output_dir: Path,
    outputs_file: Path,
    limit: int,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    preferred = read_preferred_outputs(outputs_file, output_dir)
    current = preferred + [path for path in root_videos(output_dir) if path not in preferred]
    selected = current[:limit]
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
        "input_count": len(current),
        "selected_count": len(selected),
        "automatic_history_fallback": False,
        "selected_files": [str(path.resolve()) for path in selected if path.exists()],
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


if __name__ == "__main__":
    raise SystemExit(main())
