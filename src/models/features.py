"""Feature vector and score result models (the deterministic scoring contract).

``build_feature_vector`` (scoring/feature_vector.py) produces a
:class:`FeatureVector`; the scorer turns it into a :class:`ScoreResult` via a
linear combination with naive weights. Per-feature ``contributions`` are kept so
every score is explainable (REFACTOR_SPEC §5.3: "lo score deve esporre i
contributi per feature").
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FeatureVector(BaseModel):
    """Named, normalized feature values consumed by the scorer.

    Each value is normalized to roughly [0, 1]; the naive weights (which sum to
    100) turn ``value * weight`` into a 0-100 score. Keeping the vector named
    (a dict) rather than positional makes weights and contributions auditable.

    ``source`` flags which features came from the LLM extraction (semantic) vs
    the deterministic structured fields, used to explain ``low_confidence``.
    """

    values: dict[str, float] = Field(default_factory=dict)
    # True for features that depended on the (possibly degraded) LLM extraction.
    semantic_features: list[str] = Field(default_factory=list)


class ScoreResult(BaseModel):
    """Deterministic score with per-feature contributions (explainability)."""

    score: int = 0  # 0-100
    contributions: dict[str, float] = Field(default_factory=dict)
    # Effective weights source: "learned" (config/score_weights.json) or "naive".
    weights_source: str = "naive"
    # Confidence the score reflects true intent. Driven by extraction quality:
    # a structured-only fallback / skipped extraction lowers it.
    confidence: float = 1.0
    low_confidence: bool = False
