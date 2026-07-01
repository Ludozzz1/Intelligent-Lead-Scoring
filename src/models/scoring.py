"""Scoring-support models: validity gate result and history personalization.

The legacy ``QualityBreakdown`` (4 hardcoded sub-scores) and ``RiskResult``
(separate conditional-frequency risk axis) were removed: the score is now a
single linear combination over §5.3 features (see :mod:`src.models.features`),
and confidence comes from the extraction, not a separate risk model.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class ValidityResult(BaseModel):
    """Validation-gate outcome: deterministic, rules-only, no LLM.

    ``failure_type``:
      * "none"       -> valid; proceeds to extraction + scoring.
      * "invalid"    -> positive structural evidence the lead is unusable
                        (bogus phone, disposable email) -> discard.
      * "incomplete" -> legitimate but missing/unusable required field
                        (no reachable contact, missing consent) -> ask for info.

    Semantic invalidity (spam/gibberish text) is NOT decided here; it is the
    LLM's ``ExtractedFeatures.looks_invalid`` (§5.2), applied after the gate.
    """

    is_valid: bool
    failure_type: str = "none"
    reasons: list[str] = Field(default_factory=list)
    missing_fields: list[str] = Field(default_factory=list)


class Personalization(BaseModel):
    """Signals from re-reading the customer history for this lead (exact match).

    History at runtime is limited to dedup / returning-customer detection
    (REFACTOR_SPEC §11 forbids using the history as a runtime scoring input).
    """

    is_duplicate: bool = False
    is_returning_customer: bool = False
    prior_leads_count: int = 0
    last_seen_at: datetime | None = None
    history_notes: str = ""
