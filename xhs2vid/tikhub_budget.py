#!/usr/bin/env python3
"""Persistent request-attempt budget shared by the xhs2vid TikHub steps."""

from __future__ import annotations

import json
import time
from pathlib import Path


class RequestBudgetExceeded(RuntimeError):
    """Raised before an HTTP attempt would exceed the configured budget."""


class TikHubRequestBudget:
    def __init__(self, path: Path, *, limit: int) -> None:
        if not 1 <= limit < 100:
            raise ValueError("TikHub request limit must be between 1 and 99")
        self.path = path.resolve()
        self.limit = limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({"limit": limit, "used": 0, "attempts": []})
        else:
            state = self._read()
            stored_limit = int(state.get("limit", limit))
            if stored_limit != limit:
                raise ValueError(
                    f"budget file limit is {stored_limit}, requested limit is {limit}: {self.path}"
                )

    def _read(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write(self, state: dict) -> None:
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self.path)

    def consume(self, label: str) -> int:
        state = self._read()
        used = int(state.get("used", 0))
        if used >= self.limit:
            raise RequestBudgetExceeded(
                f"TikHub request budget exhausted ({used}/{self.limit})"
            )
        used += 1
        attempts = list(state.get("attempts") or [])
        attempts.append({
            "number": used,
            "label": label,
            "timestamp": int(time.time()),
        })
        self._write({"limit": self.limit, "used": used, "attempts": attempts})
        return used

    def snapshot(self) -> dict:
        state = self._read()
        used = int(state.get("used", 0))
        return {"limit": self.limit, "used": used, "remaining": self.limit - used}
