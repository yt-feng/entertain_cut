#!/usr/bin/env python3
"""Record one idempotent daily KC delivery outcome for scheduled guards."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any


VALID_STATUSES = {"deferred", "delivered"}


def update_state(
    path: Path,
    *,
    date: str,
    status: str,
    target: int,
    succeeded: int,
    reason: str,
    message: str,
    run_id: str,
) -> dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"Unsupported delivery status: {status}")
    try:
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "items": []}
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid delivery state: {path}") from exc
    if not isinstance(existing, dict) or not isinstance(existing.get("items", []), list):
        raise ValueError(f"Invalid delivery state shape: {path}")
    item = {
        "date": date,
        "status": status,
        "reason": reason,
        "message": message,
        "target": max(1, int(target)),
        "succeeded": max(0, int(succeeded)),
        "updated_at": dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds"),
        "run_id": run_id,
    }
    items = [entry for entry in existing["items"] if isinstance(entry, dict) and entry.get("date") != date]
    items.append(item)
    items.sort(key=lambda entry: str(entry.get("date", "")))
    payload = {"version": 1, "items": items[-30:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return item


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-file", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--status", choices=sorted(VALID_STATUSES), required=True)
    parser.add_argument("--target", type=int, required=True)
    parser.add_argument("--succeeded", type=int, required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--message", required=True)
    parser.add_argument("--run-id", default="")
    args = parser.parse_args()
    item = update_state(
        args.state_file,
        date=args.date,
        status=args.status,
        target=args.target,
        succeeded=args.succeeded,
        reason=args.reason,
        message=args.message,
        run_id=args.run_id,
    )
    print(json.dumps(item, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
