"""Deterministic linear scorer: score = feature_vector · weights.

A microsecond dot product over the §5.3 feature vector and the (naive) weights,
exposing per-feature ``contributions`` so every score is auditable/explainable
(§5.3). The LLM is never in this path: a slow/failing LLM only degrades the
*semantic* feature values (via ``ExtractedFeatures``), never the arithmetic.
"""

from __future__ import annotations

from src.config import Settings, get_settings
from src.models.features import FeatureVector, ScoreResult
from src.models.lead import ExtractedFeatures
from src.scoring.weights import load_weights


def compute_score(
    vector: FeatureVector,
    features: ExtractedFeatures,
    settings: Settings | None = None,
) -> ScoreResult:
    """Score a feature vector into a 0-100 :class:`ScoreResult` with contributions."""
    s = settings or get_settings()
    weights, source = load_weights(s)

    contributions: dict[str, float] = {}
    total = 0.0
    for feature, weight in weights.items():
        value = vector.values.get(feature, 0.0)
        contrib = weight * value
        contributions[feature] = round(contrib, 2)
        total += contrib

    score = max(0, min(100, int(round(total))))

    return ScoreResult(
        score=score,
        contributions=contributions,
        weights_source=source,
        confidence=round(features.extraction_confidence, 2),
        low_confidence=features.low_confidence,
    )


def top_contributions(result: ScoreResult, k: int = 3) -> list[tuple[str, float]]:
    """Return the ``k`` features that contributed most to the score (for motivation/CLI)."""
    ranked = sorted(result.contributions.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, val) for name, val in ranked if val > 0][:k]
