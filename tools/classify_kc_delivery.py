#!/usr/bin/env python3
"""Classify a KC batch as ready or safely deferred.

Shortage is an expected provider outcome for a scheduled run. Missing or
failed packaging evidence is not a shortage and must keep failing closed.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFERRED_STATUSES = {"partial_artifact", "insufficient_videos"}
READY_STATUSES = {"ready", "minimum_ready"}


def read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def classify_delivery(
    delivery: dict[str, Any],
    runner_summaries: list[dict[str, Any]],
    *,
    limit: int,
    minimum: int,
) -> dict[str, Any]:
    selected_count = int(delivery.get("selected_count", 0))
    status = str(delivery.get("status") or "")
    if selected_count < minimum or status in DEFERRED_STATUSES:
        if not runner_summaries and int(delivery.get("input_count", 0)) == 0:
            raise ValueError("No provider summary exists for an empty KC batch")
        packaging_failures = [
            str(summary["kc_packaging_exit_code"])
            for summary in runner_summaries
            if summary.get("kc_packaging_exit_code") is not None
        ]
        if packaging_failures:
            raise ValueError(
                "KC packaging failed; shortage classification is not allowed: "
                + ", ".join(packaging_failures)
            )
        message = (
            f"Only {selected_count}/{limit} KC video(s) are available; "
            f"minimum delivery is {minimum}. The scheduled delivery is deferred."
        )
        return {
            "status": "deferred",
            "reason": "insufficient_videos",
            "message": message,
            "limit": limit,
            "minimum": minimum,
            "selected_count": selected_count,
        }
    if status not in READY_STATUSES or not delivery.get("minimum_met"):
        raise ValueError(
            f"Unexpected KC delivery evidence: status={status!r}, "
            f"selected={selected_count}, minimum_met={delivery.get('minimum_met')!r}"
        )
    return {
        "status": "ready",
        "reason": "delivery_ready",
        "message": f"KC delivery has {selected_count}/{limit} video(s) and meets the minimum.",
        "limit": limit,
        "minimum": minimum,
        "selected_count": selected_count,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--delivery-summary", type=Path, required=True)
    parser.add_argument("--runner-summary", type=Path, action="append", default=[])
    parser.add_argument("--limit", type=int, required=True)
    parser.add_argument("--minimum", type=int, required=True)
    parser.add_argument("--outcome-file", type=Path, required=True)
    parser.add_argument("--github-env", type=Path)
    args = parser.parse_args()

    delivery = read_object(args.delivery_summary)
    runner_summaries = []
    for path in args.runner_summary:
        if path.exists():
            runner_summaries.append(read_object(path))
    outcome = classify_delivery(
        delivery,
        runner_summaries,
        limit=max(1, args.limit),
        minimum=min(max(1, args.limit), max(1, args.minimum)),
    )
    args.outcome_file.parent.mkdir(parents=True, exist_ok=True)
    args.outcome_file.write_text(json.dumps(outcome, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.github_env and outcome["status"] == "deferred":
        with args.github_env.open("a", encoding="utf-8") as env_file:
            env_file.write("KC_DEFERRED=true\n")
            env_file.write(f"KC_DEFER_REASON={outcome['reason']}\n")
            env_file.write(f"KC_DEFER_MESSAGE={outcome['message']}\n")
    print(json.dumps(outcome, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
