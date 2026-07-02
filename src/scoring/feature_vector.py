"""The single, shared feature-builder (REFACTOR_SPEC §5.3 — anti-skew).

``build_feature_vector`` is THE function that turns an ``ExtractedFeatures`` +
the lead's structured fields into a normalized, named feature vector. It is the
one place feature semantics live, so the (future, offline) training pipeline can
reuse it verbatim on historical leads to fit weights over the SAME vector the
runtime scores -- eliminating training/serving skew (§5.3, §6).

Features (and only these, per §5.3):
  semantic (from the LLM extraction):
    intent_strength, budget_present, vehicle_specificity, trade_in_present,
    availability, sentiment
  deterministic structured:
    reachability, recency, geo_match

Each value is normalized to [0, 1]. No history at runtime, no vehicle catalog.
"""

from __future__ import annotations

from datetime import datetime, timezone

from src.models.features import FeatureVector
from src.models.lead import ExtractedFeatures, Lead
from src.scoring.weights import load_catchment

# Recency horizon: a lead this many days old scores 0 on recency (older = less
# operationally valuable, §5.3). Linear decay from 1.0 at age 0.
_RECENCY_HORIZON_DAYS = 30

_SPECIFICITY = {"specific": 1.0, "generic": 0.5, "none": 0.0}
_INTENT = {"high": 1.0, "medium": 0.6, "low": 0.2}
_SENTIMENT = {"positive": 1.0, "neutral": 0.5, "negative": 0.0}

# Ordinal ranks (used by merge_features to keep the *stronger* of two readings).
_SPECIFICITY_RANK = {"none": 0, "generic": 1, "specific": 2}
_INTENT_RANK = {"low": 0, "medium": 1, "high": 2}
_SENTIMENT_RANK = {"negative": 0, "neutral": 1, "positive": 2}

# Features whose value depends on the (possibly degraded) LLM extraction.
_SEMANTIC_FEATURES = (
    "intent_strength",
    "budget_present",
    "vehicle_specificity",
    "trade_in_present",
    "availability",
    "sentiment",
)


def semantic_values(features: ExtractedFeatures) -> dict[str, float]:
    """Normalize the LLM-extracted signals into the semantic feature values.

    Split out of :func:`build_feature_vector` so the agent's async re-scoring
    (after recovering missing info) recomputes ONLY the semantic features over
    the SAME mappings, then overlays them on the cached structured values -- no
    training/serving skew, no need to re-read the structured lead (§5.3).
    """
    return {
        "intent_strength": _INTENT.get(features.intent_strength, 0.2),
        "budget_present": 1.0 if features.budget_present else 0.0,
        "vehicle_specificity": _SPECIFICITY.get(features.vehicle_specificity, 0.0),
        "trade_in_present": 1.0 if features.trade_in_present else 0.0,
        "availability": 1.0 if features.availability_mentioned else 0.0,
        "sentiment": _SENTIMENT.get(features.sentiment, 0.5),
    }


def build_feature_vector(
    features: ExtractedFeatures,
    lead: Lead,
    now: datetime | None = None,
) -> FeatureVector:
    """Build the normalized §5.3 feature vector for a lead (deterministic)."""
    values: dict[str, float] = {
        # -- semantic (LLM) --
        **semantic_values(features),
        # -- deterministic structured --
        "reachability": _reachability(lead),
        "recency": _recency(lead.created_at, now),
        "geo_match": _geo_match(lead),
    }
    return FeatureVector(values=values, semantic_features=list(_SEMANTIC_FEATURES))


def _stronger(reply_val: str, base_val: str, rank: dict[str, int]) -> str:
    """Return whichever ordinal reading is stronger (reply wins on a tie)."""
    return reply_val if rank.get(reply_val, 0) >= rank.get(base_val, 0) else base_val


def merge_features(
    base: ExtractedFeatures, reply: ExtractedFeatures
) -> ExtractedFeatures:
    """Combine the original extraction with a fresh re_extract of the user's reply.

    The reply *augments* the base: booleans OR, the stronger ordinal wins, a
    non-null value fills a null one. ``missing_critical_fields`` is taken from the
    reply only when the reply is informative (a confident, non-fallback
    extraction) -- a low-confidence re_extract must not falsely declare the lead
    complete. Used by the agent's async re-scoring after a recovery reply (§7.2).
    """
    informative = (
        reply.extraction_source in ("llm", "mock")
        and reply.extraction_confidence >= 0.5
    )
    return base.model_copy(
        update={
            "budget_present": base.budget_present or reply.budget_present,
            "budget_value_eur": (
                reply.budget_value_eur
                if reply.budget_value_eur is not None
                else base.budget_value_eur
            ),
            "vehicle_specificity": _stronger(
                reply.vehicle_specificity, base.vehicle_specificity, _SPECIFICITY_RANK
            ),
            "vehicle_model_mentioned": (
                reply.vehicle_model_mentioned or base.vehicle_model_mentioned
            ),
            "trade_in_present": base.trade_in_present or reply.trade_in_present,
            "trade_in_vehicle": reply.trade_in_vehicle or base.trade_in_vehicle,
            "availability_mentioned": (
                base.availability_mentioned or reply.availability_mentioned
            ),
            "intent_strength": _stronger(
                reply.intent_strength, base.intent_strength, _INTENT_RANK
            ),
            "sentiment": _stronger(reply.sentiment, base.sentiment, _SENTIMENT_RANK),
            "missing_critical_fields": (
                # A field stays missing only if the reply did NOT supply it: intersect
                # the base's needs with the reply's own missing set, so answering one
                # field removes it without re-introducing fields the base already had
                # (extracting the short reply alone would flag those missing again).
                [f for f in base.missing_critical_fields
                 if f in reply.missing_critical_fields]
                if informative
                else list(base.missing_critical_fields)
            ),
            "extraction_confidence": max(
                base.extraction_confidence, reply.extraction_confidence
            ),
            "rationale_signals": reply.rationale_signals or base.rationale_signals,
        }
    )


# --- structured feature helpers ---------------------------------------------


def _national_digits(phone: str | None) -> str | None:
    """National-number digits (Italian country prefix stripped), or None."""
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return None
    if digits.startswith("0039"):
        digits = digits[4:]
    elif digits.startswith("39") and len(digits) > 10:
        digits = digits[2:]
    return digits or None


def _is_valid_email(email: str | None) -> bool:
    if not email:
        return False
    candidate = email.strip()
    if candidate.count("@") != 1:
        return False
    local, _, domain = candidate.partition("@")
    if not local or "." not in domain:
        return False
    return " " not in candidate and bool(domain.rsplit(".", 1)[-1])


def _reachability(lead: Lead) -> float:
    """1.0 mobile, 0.6 landline/valid-email, 0.0 unreachable.

    "Un ottimo lead non contattabile non è hot" (§5.3): reachability is a
    first-class scoring feature, not a gate-only concern.
    """
    national = _national_digits(lead.phone)
    if national and national.startswith("3") and 9 <= len(national) <= 10:
        return 1.0
    has_landline = bool(national) and 6 <= len(national) <= 11
    if has_landline or _is_valid_email(lead.email):
        return 0.6
    return 0.0


def _recency(created_at: datetime | None, now: datetime | None) -> float:
    """Linear decay over ``_RECENCY_HORIZON_DAYS``; neutral 0.5 if unknown."""
    if created_at is None:
        return 0.5
    reference = now or datetime.now(timezone.utc)
    # Tolerate naive/aware mismatches by dropping tzinfo for the diff.
    c = created_at.replace(tzinfo=None)
    r = reference.replace(tzinfo=None)
    age_days = (r - c).days
    if age_days <= 0:
        return 1.0
    if age_days >= _RECENCY_HORIZON_DAYS:
        return 0.0
    return 1.0 - age_days / _RECENCY_HORIZON_DAYS


def _geo_match(lead: Lead) -> float:
    """1.0 in-catchment, 0.5 adjacent, 0.1 far, 0.3 unknown (no ZIP/city)."""
    catchment = load_catchment()
    digits = "".join(ch for ch in (lead.zip_code or "") if ch.isdigit())
    if len(digits) >= 2:
        prefix = digits[:2]
        if prefix in catchment.get("home_zip_prefixes", []):
            return 1.0
        if prefix in catchment.get("adjacent_zip_prefixes", []):
            return 0.5
        return 0.1

    city = (lead.city or "").strip().lower()
    if not city:
        return 0.3
    if city in catchment.get("home_cities", []):
        return 1.0
    if city in catchment.get("adjacent_cities", []):
        return 0.5
    return 0.1
