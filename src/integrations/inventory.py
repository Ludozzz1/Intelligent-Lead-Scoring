"""Mocked inventory check for the agent (REFACTOR_SPEC §7.3).

Inventory is an AGENT tool, not a scoring feature (we deliberately removed the
hardcoded vehicle catalog from scoring). The mock answers {in_stock, alternatives}
deterministically from a hash of the vehicle string -- no enumerated catalog, no
car-name lists baked into the code.
"""

from __future__ import annotations

import hashlib
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Inventory(Protocol):
    def check(self, vehicle: str | None) -> dict[str, Any]: ...
    def recommend_alternatives(
        self, vehicle: str | None, budget: float | None = None
    ) -> dict[str, Any]: ...


class MockInventory:
    """Deterministic in-memory inventory mock (no catalog)."""

    def __init__(self, failures: set[str] | None = None) -> None:
        self._failures = failures or set()

    def check(self, vehicle: str | None) -> dict[str, Any]:
        if "check" in self._failures:
            raise RuntimeError("inventory service error")
        key = (vehicle or "").strip().lower()
        if not key:
            return {"in_stock": False, "alternatives": [], "vehicle": None}
        seed = int(hashlib.sha256(key.encode()).hexdigest(), 16)
        in_stock = seed % 3 != 0  # ~2/3 in stock, reproducible per vehicle string
        alternatives = (
            [] if in_stock else ["chiedere al dealer alternative equivalenti"]
        )
        return {"in_stock": in_stock, "alternatives": alternatives, "vehicle": vehicle}

    def recommend_alternatives(
        self, vehicle: str | None, budget: float | None = None
    ) -> dict[str, Any]:
        """Suggest equivalent in-catchment alternatives when a model is out of stock.

        Deliberately catalog-free (no car-name lists baked in): returns opaque,
        deterministic alternative slots with an indicative price derived from a
        hash, flagged whether they fit the stated budget.
        """
        if "recommend_alternatives" in self._failures:
            raise RuntimeError("inventory service error")
        key = (vehicle or "").strip().lower()
        if not key:
            return {"vehicle": None, "budget_eur": budget, "alternatives": []}
        seed = int(hashlib.sha256(f"alt|{key}".encode()).hexdigest(), 16)
        count = 1 + seed % 3  # 1..3 alternatives, reproducible
        alternatives = []
        for i in range(count):
            price = 18000 + ((seed >> (i * 4)) % 20) * 1000  # 18k..37k indicative
            alternatives.append(
                {
                    "ref": f"ALT-{hashlib.sha256(f'{key}|{i}'.encode()).hexdigest()[:8]}",
                    "note": "modello equivalente, stessa categoria, in catchment",
                    "indicative_price_eur": price,
                    "within_budget": budget is None or price <= budget,
                }
            )
        return {"vehicle": vehicle, "budget_eur": budget, "alternatives": alternatives}
