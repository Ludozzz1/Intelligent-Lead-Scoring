"""Mocked follow-up scheduler for the agent (REFACTOR_SPEC §7.3).

Schedules a deferred follow-up to a lead (the follow-up ladder before
disqualifying a non-responder). Deterministic id from a hash of (lead_id, when);
records every scheduled follow-up in memory. No PII (lead_id is an opaque id).
In production this is a delayed-message queue / scheduler.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Scheduler(Protocol):
    def schedule(self, lead_id: str, when: str) -> dict[str, Any]: ...


class MockScheduler:
    """Deterministic in-memory follow-up scheduler."""

    def __init__(self, failures: set[str] | None = None) -> None:
        self._failures = failures or set()
        self.scheduled: list[dict[str, Any]] = []

    def schedule(self, lead_id: str, when: str) -> dict[str, Any]:
        if "schedule" in self._failures:
            raise RuntimeError("scheduler service error")
        ref = hashlib.sha256(f"{lead_id}|{when}".encode()).hexdigest()[:12]
        record = {"scheduled": True, "followup_id": f"fu_{ref}", "when": when}
        self.scheduled.append(record)
        return record
