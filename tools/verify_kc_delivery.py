#!/usr/bin/env python3
"""Verify every selected KC delivery file against the publisher's remote evidence."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def verify_delivery(
    delivery: dict[str, Any],
    publish: dict[str, Any],
    *,
    output_dir: Path,
    limit: int,
    min_delivery: int,
    git_max_bytes: int | None = None,
) -> dict[str, Any]:
    limit = max(1, limit)
    minimum = min(limit, max(1, min_delivery))
    output_dir = output_dir.resolve()
    selected = [Path(path) for path in delivery.get("selected_files", [])]
    selected_count = int(delivery.get("selected_count", 0))
    selected_names = {path.name for path in selected}
    errors = []
    if len(selected_names) != len(selected) or len(selected) != selected_count:
        errors.append("Selected file list is missing, duplicated, or inconsistent with selected_count")
    minimum_met = minimum <= selected_count <= limit
    if not minimum_met or not delivery.get("deliverable") or not delivery.get("minimum_met"):
        errors.append(f"Delivery minimum not met: selected={selected_count}, minimum={minimum}, target={limit}")

    items: dict[str, dict[str, Any]] = {}
    for item in publish.get("files", []):
        name = item.get("name", "")
        if name in items:
            errors.append(f"Duplicate publisher evidence: {name}")
        items[name] = item

    verified_count = 0
    git_ready_count = 0
    for selected_path in selected:
        path = selected_path.resolve()
        if path.parent != output_dir or not path.is_file() or path.stat().st_size == 0:
            errors.append(f"Selected local video is missing or outside the output directory: {selected_path}")
            continue
        item = items.get(path.name, {})
        webdav = item.get("webdav", {})
        if webdav.get("success") is True and webdav.get("remote_verified") is True:
            verified_count += 1
        else:
            errors.append(f"Selected video was not verified on WebDAV: {path.name}")
        if item.get("git_ready") is True and (git_max_bytes is None or path.stat().st_size < git_max_bytes):
            git_ready_count += 1
        elif git_max_bytes is not None:
            errors.append(f"Selected video is not ready for Git: {path.name}")

    return {
        "complete": not errors,
        "target_met": selected_count == limit,
        "minimum_met": minimum_met,
        "limit": limit,
        "min_delivery": minimum,
        "selected_count": selected_count,
        "webdav_verified_count": verified_count,
        "git_ready_count": git_ready_count,
        "remote_directory": publish.get("remote_directory", ""),
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-summary", type=Path, required=True)
    parser.add_argument("--publish-summary", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--min-delivery", type=int, required=True)
    parser.add_argument("--git-max-bytes", type=int)
    args = parser.parse_args()
    try:
        result = verify_delivery(
            json.loads(args.delivery_summary.read_text(encoding="utf-8")),
            json.loads(args.publish_summary.read_text(encoding="utf-8")),
            output_dir=args.output_dir,
            limit=args.limit,
            min_delivery=args.min_delivery,
            git_max_bytes=args.git_max_bytes,
        )
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"Delivery evidence unavailable: {exc}")
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
