"""Action decision + agent trigger + priority (deterministic, §5.6 / §7.1).

Value-aligned routing (the trigger keys off the lead's VALUE + consent). Maps the
category + extraction signals to:
  * a recommended action: ``lead_valido`` | ``chiedere_info`` | ``nurturing`` |
    ``scartare`` (``nurturing`` is now only an OPERATOR low-priority label for a
    complete cold lead -- it no longer maps to an agent goal);
  * an optional agent trigger (goal). Automation is restricted to high-value
    leads:
      - INCOMPLETE + consent + rich extraction (``recovery_worthy``: coverage
        >= ``recovery_coverage_min``, NOT the missing-field-depressed band)
        -> ``RECOVER_INFO`` (the agent recovers info, then re-scores §7.2);
      - COMPLETE automation-worthy (hot, or warm with ``score >= warm_high``)
        + consent -> ``NEGOTIATE_APPOINTMENT`` (proactive booking, still staged
        for human approval);
      - everything else -> the operator: a cold lead (any completeness), a
        mid/low warm, a low-coverage incomplete, or any lead without consent.
        ``invalid`` is always discarded and never triggers the agent.
  * a 0-100 priority within the category's band (the call-center queue order).

Consent is evaluated *up front*: without it the agent cannot message, so we route
to the operator rather than triggering a goal that would immediately hand off.
See docs/progettazione.md and REFACTOR_SPEC §7.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Settings
from src.models.agent import AgentGoal
from src.models.features import FeatureVector
from src.models.lead import ExtractedFeatures
from src.models.scoring import Personalization, ValidityResult
from src.scoring.feature_vector import extraction_coverage
from src.scoring.weights import load_thresholds

ACTION_VALID = "lead_valido"
ACTION_ASK_INFO = "chiedere_info"
ACTION_NURTURE = "nurturing"
ACTION_DISCARD = "scartare"

# Priority bands per category (inclusive bounds).
_PRIORITY_BANDS: dict[str, tuple[int, int]] = {
    "hot": (80, 100),
    "warm": (50, 79),
    "cold": (20, 49),
    "invalid": (0, 19),
}
_SCORE_BOOST_SHARE = 0.7  # in-band lift driven by the score
_RETURNING_BOOST_SHARE = 0.3  # known returning customer nudges priority up


@dataclass(frozen=True)
class ActionDecision:
    recommended_action: str
    agent_goal: AgentGoal | None
    priority: int


def route_complete(
    category: str,
    score: int,
    has_consent: bool,
    settings: Settings | None = None,
) -> tuple[str, AgentGoal | None]:
    """Route a COMPLETE lead (no missing info) by value + consent.

    The single source of truth reused both here (the hot path) and by the agent's
    async re-scoring once a recovered lead is complete (§7.2) -- so a lead lifted
    to ``hot``/warm-high by enrichment is routed identically to one that arrived
    that way. Returns ``(recommended_action, agent_goal)``.
    """
    warm_high = load_thresholds(settings).get("warm_high", 62)
    book_worthy = category == "hot" or (category == "warm" and score >= warm_high)

    if book_worthy and has_consent:
        return ACTION_VALID, AgentGoal.NEGOTIATE_APPOINTMENT
    if category in ("hot", "warm"):
        # Value but no consent, or a mid/low warm -> the operator calls.
        return ACTION_VALID, None
    # cold (complete, weak): never the agent. The operator handles it at low
    # priority; automation is restricted to {hot, warm>=warm_high} (§7.1).
    return ACTION_NURTURE, None


def recovery_worthy(
    vector: FeatureVector, settings: Settings | None = None
) -> bool:
    """True if an INCOMPLETE lead is rich enough to be worth chasing (§7.1).

    Gates ``RECOVER_INFO`` on the extraction's coverage instead of its score
    band: the band is depressed by the very fields that are missing, so a strong
    lead missing one field would be under-selected. See ``extraction_coverage``.
    """
    threshold = load_thresholds(settings).get("recovery_coverage_min", 0.45)
    return extraction_coverage(vector, settings) >= threshold


def decide_action(
    category: str,
    validity: ValidityResult,
    features: ExtractedFeatures,
    score: int,
    personalization: Personalization,
    consent: bool | None = None,
    settings: Settings | None = None,
    vector: FeatureVector | None = None,
) -> ActionDecision:
    """Decide the recommended action, agent trigger and priority."""
    priority = _compute_priority(category, score, personalization)

    if category == "invalid":
        return ActionDecision(ACTION_DISCARD, None, priority)

    has_consent = consent is True

    if features.missing_critical_fields:
        # Incomplete: hand to the agent (recover info, then re-score §7.2) ONLY
        # with consent AND a rich-enough extraction -- the coverage gate, not the
        # missing-field-depressed band. Otherwise the operator asks.
        worth_it = (
            has_consent and vector is not None and recovery_worthy(vector, settings)
        )
        goal = AgentGoal.RECOVER_INFO if worth_it else None
        return ActionDecision(ACTION_ASK_INFO, goal, priority)

    action, goal = route_complete(category, score, has_consent, settings)
    return ActionDecision(action, goal, priority)


def _compute_priority(
    category: str, score: int, personalization: Personalization
) -> int:
    low, high = _PRIORITY_BANDS.get(category, _PRIORITY_BANDS["invalid"])
    headroom = high - low
    score_norm = max(0.0, min(1.0, score / 100.0))
    returning = 1.0 if personalization.is_returning_customer else 0.0
    boost = headroom * (
        _SCORE_BOOST_SHARE * score_norm + _RETURNING_BOOST_SHARE * returning
    )
    return max(low, min(high, int(round(low + boost))))
