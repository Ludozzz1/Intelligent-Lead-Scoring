"""Mocked financing simulator for the agent (REFACTOR_SPEC §7.3).

Turns a price (minus down payment and trade-in) into an indicative monthly
instalment, to qualify and "warm up" a budget-conscious lead ("budget 35k").
Deterministic French amortization with a fixed indicative APR -- no PII, no
external call. In production this is the dealer's financing partner API.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# Indicative terms (placeholder, would come from the financing partner).
_DEFAULT_TERM_MONTHS = 60
_ANNUAL_RATE = 0.0749  # 7.49% TAEG indicativo


@runtime_checkable
class FinancingSimulator(Protocol):
    def simulate(
        self,
        price: float | None,
        down_payment: float | None = None,
        trade_in_value: float | None = None,
        term_months: int | None = None,
    ) -> dict[str, Any]: ...


class MockFinancing:
    """Deterministic in-memory financing simulator (French amortization)."""

    def __init__(self, failures: set[str] | None = None) -> None:
        self._failures = failures or set()

    def simulate(
        self,
        price: float | None,
        down_payment: float | None = None,
        trade_in_value: float | None = None,
        term_months: int | None = None,
    ) -> dict[str, Any]:
        if "simulate" in self._failures:
            raise RuntimeError("financing service error")
        if not price or price <= 0:
            return {"financed_eur": None, "monthly_eur": None, "term_months": None}

        term = term_months or _DEFAULT_TERM_MONTHS
        financed = max(0.0, float(price) - (down_payment or 0.0) - (trade_in_value or 0.0))

        monthly_rate = _ANNUAL_RATE / 12
        if financed == 0:
            monthly = 0.0
        else:
            # Standard French amortization instalment.
            factor = (1 + monthly_rate) ** term
            monthly = financed * monthly_rate * factor / (factor - 1)

        return {
            "financed_eur": round(financed, 2),
            "monthly_eur": round(monthly, 2),
            "term_months": term,
            "taeg_pct": round(_ANNUAL_RATE * 100, 2),
            "total_eur": round(monthly * term, 2),
        }
