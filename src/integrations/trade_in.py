"""Mocked trade-in estimator for the agent (REFACTOR_SPEC §7.3).

Returns an indicative euro range to qualify and "warm up" the lead. Deterministic
from a hash of the free-text vehicle description -- no catalog, no PII.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class TradeInEstimator(Protocol):
    def estimate(self, vehicle_desc: str | None) -> dict[str, Any]: ...


class MockTradeIn:
    """Deterministic in-memory trade-in estimator."""

    def __init__(self, failures: set[str] | None = None) -> None:
        self._failures = failures or set()

    def estimate(self, vehicle_desc: str | None) -> dict[str, Any]:
        if "estimate" in self._failures:
            raise RuntimeError("trade-in service error")
        key = (vehicle_desc or "").strip().lower()
        if not key:
            return {"range_eur": None, "vehicle_desc": None}
        seed = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        low = 2000 + (seed % 9) * 1000  # 2000..10000, reproducible
        high = low + 1500 + (seed % 5) * 500
        return {"range_eur": [low, high], "vehicle_desc": vehicle_desc}
