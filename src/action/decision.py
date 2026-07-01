"""Action decision + agent trigger + priority (deterministic, §5.6 / §7.1).

Value-aligned routing (the trigger keys off the lead's VALUE + consent, not just
"is there an open interaction"). Maps the category + extraction signals to:
  * a recommended action: ``lead_valido`` | ``chiedere_info`` | ``nurturing`` |
    ``scartare``;
  * an optional agent trigger (goal): an INCOMPLETE lead (any band, with consent)
    -> ``RECOVER_INFO`` (the agent recovers info, then re-scores §7.2); a COMPLETE
    *automation-worthy* lead (hot, or warm above ``warm_high``, with consent) ->
    ``NEGOTIATE_APPOINTMENT`` (the agent attempts the booking proactively); a
    COMPLETE *cold* lead (with consent) -> ``NURTURE`` (one automatic asset, no
    operator call). Everything else (no consent, mid/low warm) -> the operator.
    ``invalid`` is always discarded and never triggers the agent.
  * a 0-100 priority within the category's band (the call-center queue order).

Consent is evaluated *up front*: without it the agent cannot message, so we route
to the operator rather than triggering a goal that would immediately hand off.
This deliberately supersedes the older §7.1 ("invalid/cold never trigger") -- see
docs/progettazione.md §2.x and REFACTOR_SPEC §7.1.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.config import Settings
from src.models.agent import AgentGoal
from src.models.lead import ExtractedFeatures
from src.models.scoring import Personalization, ValidityResult
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
    warm_high = load_thresholds(settings).get("warm_high", 55)
    book_worthy = category == "hot" or (category == "warm" and score >= warm_high)

    if book_worthy and has_consent:
        return ACTION_VALID, AgentGoal.NEGOTIATE_APPOINTMENT
    if category in ("hot", "warm"):
        # Value but no consent, or a mid/low warm -> the operator calls.
        return ACTION_VALID, None
    # cold (complete, weak): nurture automatically if we may message, else drop.
    if has_consent:
        return ACTION_NURTURE, AgentGoal.NURTURE
    return ACTION_NURTURE, None


def decide_action(
    category: str,
    validity: ValidityResult,
    features: ExtractedFeatures,
    score: int,
    personalization: Personalization,
    consent: bool | None = None,
    settings: Settings | None = None,
) -> ActionDecision:
    """Decide the recommended action, agent trigger and priority."""
    priority = _compute_priority(category, score, personalization)

    if category == "invalid":
        return ActionDecision(ACTION_DISCARD, None, priority)

    has_consent = consent is True

    if features.missing_critical_fields:
        # Incomplete at ANY band: recover info, then the agent re-scores (§7.2).
        # Without consent the agent cannot message -> the operator asks instead.
        goal = AgentGoal.RECOVER_INFO if has_consent else None
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
