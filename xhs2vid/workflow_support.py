#!/usr/bin/env python3
"""Small, testable helpers used by the XHS GitHub Actions workflow."""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")


def validated_iso_date(value: str) -> str:
    """Return a strict YYYY-MM-DD value or raise ``ValueError``."""

    normalized = value.strip()
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"invalid output date {value!r}; expected YYYY-MM-DD") from exc
    if parsed.isoformat() != normalized:
        raise ValueError(f"invalid output date {value!r}; expected YYYY-MM-DD")
    return normalized


def require_recent_discovery_date(value: str, *, now: datetime | None = None) -> str:
    normalized = validated_iso_date(value)
    current = (now or datetime.now(UTC)).astimezone(BEIJING).date()
    age_days = (current - date.fromisoformat(normalized)).days
    if not 0 <= age_days <= 1:
        raise ValueError(
            "historical/future delivery dates cannot fetch current posts; "
            "recover that date from existing artifacts instead"
        )
    return normalized


def resolve_output_date(
    event_name: str,
    requested_date: str = "",
    *,
    schedule_expression: str = "",
    now: datetime | None = None,
) -> str:
    """Resolve the delivery date for manual and delayed scheduled runs.

    Scheduled attempts use the UTC calendar date of the latest occurrence of
    their triggering daily cron expression. This remains stable when GitHub
    starts a run after Beijing midnight. Manual runs use an explicit date when
    supplied, otherwise the current Beijing date.
    """

    if requested_date.strip():
        return validated_iso_date(requested_date)

    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    if event_name == "schedule":
        fields = schedule_expression.split()
        if len(fields) != 5 or fields[2:] != ["*", "*", "*"]:
            raise ValueError(f"unsupported scheduled cron expression: {schedule_expression!r}")
        try:
            minute = int(fields[0])
            hour = int(fields[1])
        except ValueError as exc:
            raise ValueError(f"unsupported scheduled cron expression: {schedule_expression!r}") from exc
        if not 0 <= minute <= 59 or not 0 <= hour <= 23:
            raise ValueError(f"unsupported scheduled cron expression: {schedule_expression!r}")
        created_utc = current.astimezone(UTC)
        scheduled_utc = created_utc.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if scheduled_utc > created_utc:
            scheduled_utc -= timedelta(days=1)
        delay = created_utc - scheduled_utc
        if delay >= timedelta(days=1):
            raise ValueError(f"scheduled run delay is ambiguous: {delay}")
        return scheduled_utc.date().isoformat()
    return current.astimezone(BEIJING).date().isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", required=True)
    parser.add_argument("--requested-date", default="")
    parser.add_argument("--schedule-expression", default="")
    parser.add_argument("--run-created-at", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        created_at = (
            datetime.fromisoformat(args.run_created_at.replace("Z", "+00:00"))
            if args.run_created_at
            else None
        )
        print(
            resolve_output_date(
                args.event,
                args.requested_date,
                schedule_expression=args.schedule_expression,
                now=created_at,
            )
        )
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
