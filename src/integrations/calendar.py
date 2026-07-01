"""Mocked dealer calendar (availability + booking) for the agent.

In production this is the dealer's scheduling system. Here a deterministic mock:
slots derive from the dealer id (reproducible), and any operation can be made to
fail (``failures``) so the agent's guardrails / human-handoff path is testable.
No vehicle catalog, no PII.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable

_SLOT_POOL = (
    "sabato 10:00",
    "sabato 11:30",
    "lunedì 17:00",
    "martedì 18:30",
    "mercoledì 09:30",
)


@runtime_checkable
class Calendar(Protocol):
    def check_availability(self, dealer_id: str, preferences: dict) -> list[str]: ...
    def book(self, dealer_id: str, slot: str, lead_id: str) -> dict[str, Any]: ...


class MockCalendar:
    """Deterministic in-memory calendar mock."""

    def __init__(self, failures: set[str] | None = None) -> None:
        self._failures = failures or set()
        self.booked: list[dict[str, Any]] = []

    def check_availability(self, dealer_id: str, preferences: dict) -> list[str]:
        if "check_availability" in self._failures:
            raise RuntimeError("calendar unavailable")
        seed = int(hashlib.sha256((dealer_id or "").encode()).hexdigest(), 16)
        # A reproducible rotation of the pool -> 3 offered slots.
        start = seed % len(_SLOT_POOL)
        return [_SLOT_POOL[(start + i) % len(_SLOT_POOL)] for i in range(3)]

    def book(self, dealer_id: str, slot: str, lead_id: str) -> dict[str, Any]:
        if "book" in self._failures:
            raise RuntimeError("booking system error")
        ref = hashlib.sha256(f"{dealer_id}|{slot}|{lead_id}".encode()).hexdigest()[:12]
        record = {"confirmed": True, "appointment_id": f"appt_{ref}", "slot": slot}
        self.booked.append(record)
        return record
