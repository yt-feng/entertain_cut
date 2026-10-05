"""Classify provider access failures separately from the local request budget."""
from __future__ import annotations

import re


ACCESS_REASONS = {
    401: "tikhub_auth_invalid",
    402: "tikhub_payment_required",
    403: "tikhub_permission_denied",
    429: "tikhub_rate_limited",
}
NONRETRYABLE_REASONS = frozenset(ACCESS_REASONS[code] for code in (401, 402, 403))
LEGACY_ACCESS_REASONS = frozenset({"tikhub_access_blocked", "tikhub_author_lookup_unavailable"})


class TikHubAccessBlocked(RuntimeError):
    """A provider rejection, without retaining credentials or response bodies."""

    def __init__(self, status_code: int, path: str) -> None:
        self.status_code = status_code
        self.path = path
        self.reason = ACCESS_REASONS[status_code]
        self.retryable = self.reason not in NONRETRYABLE_REASONS
        super().__init__(f"TikHub access blocked with HTTP {status_code} at {path}")


def normalize_access_reason(reason: str, *, status_code: object = None, message: str = "") -> str:
    """Read both new markers and existing persisted provider-block records."""
    if reason not in LEGACY_ACCESS_REASONS:
        return reason
    if type(status_code) is int and status_code in ACCESS_REASONS:
        return ACCESS_REASONS[status_code]
    match = re.search(r"TikHub access blocked with HTTP (401|402|403|429)(?!\d)", message)
    return ACCESS_REASONS[int(match.group(1))] if match else reason


def deferred_retryable(reason: str) -> bool:
    """Keep existing bounded defer behavior except actionable account failures."""
    return reason not in NONRETRYABLE_REASONS
