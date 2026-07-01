"""Input lead model and the LLM-extracted semantic features.

Two models live here:

* :class:`Lead` -- the raw incoming lead (structured fields + free text).
* :class:`ExtractedFeatures` -- the structured purchase-intent / quality signals
  the LLM produces from the free-text ``message`` (REFACTOR_SPEC §5.2). This is
  the system's ONLY semantic-understanding step: the deterministic scorer never
  reads the raw text, only these fields.

The LLM never assigns a score: ``ExtractedFeatures`` feeds the deterministic
``build_feature_vector`` (scoring §5.3), which is combined with naive weights.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Lead(BaseModel):
    """Raw incoming lead.

    All input fields are optional: real-world feeds are partial, and missing
    fields drive the validation gate (incomplete vs invalid) rather than
    crashing.
    """

    model_config = ConfigDict(extra="ignore")

    lead_id: str | None = None

    platform: str | None = None
    channel: str | None = None
    message: str | None = None
    vehicle_interest: str | None = None
    city: str | None = None
    zip_code: str | None = None
    phone: str | None = None
    name: str | None = None
    surname: str | None = None
    email: str | None = None
    campaign: str | None = None
    created_at: datetime | None = None

    # Consent for outbound contact. None = unknown/missing (treated as
    # incomplete, never as fake; blocks costly auto-actions in the agent).
    consent: bool | None = None


# Provenance of an extraction result.
#   "llm"      - real LLM structured-output call
#   "mock"     - deterministic fixture mock (offline default)
#   "fallback" - structured-only default after an LLM failure/timeout
#   "skipped"  - gated out (invalid lead or trivial message)
#   "none"     - extraction stage itself errored (degraded default)
ExtractionSource = Literal["llm", "mock", "fallback", "skipped", "none"]


class ExtractedFeatures(BaseModel):
    """Structured signals extracted from the free-text message by the LLM.

    Mirrors REFACTOR_SPEC §5.2. Semantic understanding (which model, how urgent,
    sentiment, whether the text looks invalid) is the LLM's job; everything
    downstream is deterministic. ``rationale_signals`` feeds the deterministic
    motivation (§5.5) so no second LLM call is needed.
    """

    # Budget
    budget_value_eur: float | None = None
    budget_present: bool = False

    # Vehicle
    vehicle_model_mentioned: str | None = None
    vehicle_specificity: Literal["specific", "generic", "none"] = "none"

    # Trade-in
    trade_in_present: bool = False
    trade_in_vehicle: str | None = None

    # Urgency / availability / intent
    urgency_signals: list[str] = Field(default_factory=list)
    intent_strength: Literal["high", "medium", "low"] = "low"
    availability_mentioned: bool = False

    # Tone
    sentiment: Literal["positive", "neutral", "negative"] = "neutral"

    # Completeness / validity / confidence
    missing_critical_fields: list[str] = Field(default_factory=list)
    looks_invalid: bool = False
    extraction_confidence: float = 0.0

    # Free-text rationale that the deterministic motivation reuses (no 2nd call).
    rationale_signals: str = ""

    extraction_source: ExtractionSource = "none"

    @property
    def low_confidence(self) -> bool:
        """True when the extraction is too weak to fully trust (gated actions).

        Set on the structured-only fallback, on skipped extraction, or whenever
        the LLM self-reports low ``extraction_confidence``.
        """
        if self.extraction_source in ("fallback", "skipped", "none"):
            return True
        return self.extraction_confidence < 0.5
