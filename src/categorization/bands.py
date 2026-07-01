"""Categorization: map the 0-100 score to hot/warm/cold via calibrated bands.

``invalid`` is NOT a band (REFACTOR_SPEC §5.4 / anti-pattern §11): it comes from
the gate or from the LLM's ``looks_invalid`` (§5.2). Thresholds are read from the
``category_thresholds.json`` artifact (naive defaults shipped), never hardcoded
"a intuito".
"""

from __future__ import annotations

from src.config import Settings
from src.scoring.weights import load_thresholds


def categorize(
    score: int,
    is_valid: bool,
    looks_invalid: bool,
    settings: Settings | None = None,
) -> str:
    """Return "invalid" | "hot" | "warm" | "cold"."""
    if not is_valid or looks_invalid:
        return "invalid"
    thr = load_thresholds(settings)
    if score >= thr.get("hot", 65):
        return "hot"
    if score >= thr.get("warm", 40):
        return "warm"
    return "cold"
