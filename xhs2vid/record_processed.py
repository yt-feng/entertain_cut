#!/usr/bin/env python3
"""Merge a successfully uploaded daily batch into the cross-day dedupe ledger."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--new", type=Path, required=True, dest="new_path")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--keep", type=int, default=500)
    args = parser.parse_args()
    if args.keep < 25:
        parser.error("--keep must be at least 25")
    return args


def main() -> None:
    args = parse_args()
    incoming = json.loads(args.new_path.read_text(encoding="utf-8"))
    incoming_items = incoming.get("items") or []
    if not incoming_items:
        raise SystemExit("new_processed.json contains no uploaded items")

    manifest_path = args.manifest.expanduser().resolve()
    if manifest_path.is_file():
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        current = {"version": 1, "items": []}
    by_id = {
        str(item.get("note_id")): item
        for item in (current.get("items") or [])
        if item.get("note_id")
    }
    now = datetime.now(BEIJING).isoformat()
    for item in incoming_items:
        note_id = str(item.get("note_id") or "")
        if not note_id:
            continue
        by_id[note_id] = {**item, "uploaded_at": now}
    merged = sorted(
        by_id.values(),
        key=lambda item: str(item.get("uploaded_at") or item.get("rendered_at") or ""),
        reverse=True,
    )[: args.keep]
    payload = {"version": 1, "updated_at": now, "items": merged}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = manifest_path.with_suffix(manifest_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(manifest_path)
    print(f"[state] recorded {len(incoming_items)} new note(s); retained {len(merged)}")


if __name__ == "__main__":
    main()
