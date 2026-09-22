#!/usr/bin/env python3
"""Persist one delivery outcome so scheduled compensation runs are idempotent."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
STATUSES = ("deferred", "delivered")


def update_state(
    path: Path,
    date: str,
    status: str,
    *,
    reason: str = "",
    message: str = "",
    target: int = 5,
    succeeded: int = 0,
    run_id: str = "",
) -> dict:
    if status not in STATUSES:
        raise ValueError(f"unsupported delivery status: {status}")
    if path.is_file():
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        payload = {"version": 1, "items": []}
    if not isinstance(payload, dict):
        raise ValueError("delivery state must be a JSON object")
    items = [item for item in payload.get("items", []) if item.get("date") != date]
    record = {
        "date": date,
        "status": status,
        "reason": reason,
        "message": message,
        "target": target,
        "succeeded": succeeded,
        "updated_at": datetime.now(BEIJING).isoformat(),
    }
    if run_id:
        record["run_id"] = run_id
    items.append(record)
    payload = {"version": 1, "items": items[-30:]}
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--date", required=True)
    parser.add_argument("--status", choices=STATUSES, required=True)
    parser.add_argument("--reason", default="")
    parser.add_argument("--message", default="")
    parser.add_argument("--target", type=int, default=5)
    parser.add_argument("--succeeded", type=int, default=0)
    parser.add_argument("--run-id", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    print(
        json.dumps(
            update_state(
                args.state,
                args.date,
                args.status,
                reason=args.reason,
                message=args.message,
                target=args.target,
                succeeded=args.succeeded,
                run_id=args.run_id,
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
