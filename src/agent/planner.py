"""Agent planner: the policy that proposes the next action (REFACTOR_SPEC §7).

The loop in :mod:`src.agent.state_machine` is a thin controller: it asks a
``Planner`` for one :class:`PlannerDecision`, runs it through ``enforce`` (which
returns an :class:`EnforcedDecision`), executes it, and transitions. Two planners
share the same protocol:

* :class:`DeterministicPlanner` -- the default in ``llm_mode="mock"``: a faithful
  1:1 translation of the legacy trajectories (keyword matching), so existing
  behaviour and tests are preserved.
* :class:`LLMPlanner` -- real tool-calling via :meth:`LLMAdapter.complete_json`
  (off the SLA). On any failure it raises ``LLMError`` and the loop degrades to the
  deterministic planner.

The planner only *proposes*; ``enforce`` disposes (block / stage / allow). It never
scores or invalidates a lead.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field, ValidationError

from src.action.decision import route_complete
from src.agent.agent_prompts import (
    PLANNER_DECISION_SCHEMA,
    PLANNER_SYSTEM_PROMPT,
    build_planner_messages,
)
from src.categorization.bands import categorize
from src.config import Settings
from src.extraction.llm import LLMAdapter, LLMError
from src.models.agent import (
    AgentAction,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentSession,
    AgentState,
)
from src.models.features import FeatureVector
from src.scoring.feature_vector import merge_features, semantic_values
from src.scoring.scorer import compute_score

# Keyword matching: deterministic fallback interpretation of a free-text reply.
_CONFIRM_WORDS = ("ok", "conferm", "va bene", "sì", "si ", "perfetto", "d'accordo")
_COUNTER_WORDS = ("altro", "diverso", "non posso", "spost", "preferisc", "magari")
_REFUSAL_WORDS = ("non sono interessat", "non mi interessa", "lasciate perdere", "no grazie")


class PlannerDecision(BaseModel):
    """One action proposed by a planner (before enforcement)."""

    action: str  # call_tool | wait_user | complete | handoff
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    next_state: AgentState | None = None
    reason: str = ""
    rationale: str = ""


class EnforcedDecision(BaseModel):
    """A planner decision after ``enforce`` applied decision-rights + guardrails."""

    action: str  # call_tool | stage | wait_user | complete | handoff
    tool: str | None = None
    args: dict[str, Any] = Field(default_factory=dict)
    status: str = "executed"
    next_state: AgentState | None = None
    reason: str = ""
    rationale: str = ""
    # An action to record before handing off (e.g. the consent-blocked message).
    pre_record: dict[str, Any] | None = None


@runtime_checkable
class Planner(Protocol):
    def next_action(
        self,
        session: AgentSession,
        event: AgentEvent | None,
        wake: list[AgentAction],
        settings: Settings,
    ) -> PlannerDecision: ...


def _match_slot(text: str, slots: list[str]) -> str | None:
    """Return the offered slot the reply refers to (by its day word), if any."""
    for slot in slots:
        day = slot.split()[0].lower() if slot else ""
        if day and day in text:
            return slot
    return None


def _call(tool: str, *, args: dict | None = None, next_state: AgentState | None = None,
          rationale: str = "") -> PlannerDecision:
    return PlannerDecision(action="call_tool", tool=tool, args=args or {},
                           next_state=next_state, rationale=rationale)


def _rescore(
    session: AgentSession, reply_features, settings: Settings
) -> tuple[int, str, object]:
    """Re-score the lead off the SLA after a recovery reply (§7.2).

    Merge the reply's fresh extraction into the cached base extraction, overlay
    the recomputed semantic features on the cached structured ``base_vector``
    (reusing the SAME mappings -> no skew, no need to re-read the lead), and
    re-derive ``(score, category, merged_features)``.
    """
    base = session.base_features
    merged = merge_features(base, reply_features) if base is not None else reply_features
    values = dict(session.base_vector)
    values.update(semantic_values(merged))
    score = compute_score(FeatureVector(values=values), merged, settings).score
    category = categorize(score, True, merged.looks_invalid, settings)
    return score, category, merged


class DeterministicPlanner:
    """Legacy trajectories as a stateless per-step policy (default in mock mode).

    Reproduces the previous state machine exactly: it derives the next single
    action from ``(session.state, event, wake)``. ``wake`` is the list of actions
    already executed in the current wake, used to sequence multi-tool steps.
    """

    def next_action(
        self,
        session: AgentSession,
        event: AgentEvent | None,
        wake: list[AgentAction],
        settings: Settings,
    ) -> PlannerDecision:
        done = {a.tool for a in wake}
        etype = event.type if event else None

        if etype == AgentEventType.NO_RESPONSE_TIMEOUT:
            return PlannerDecision(action="complete",
                                   next_state=AgentState.DISQUALIFIED_NO_RESPONSE,
                                   reason="no_response")

        state = session.state
        if state == AgentState.TRIGGERED:
            if session.goal == AgentGoal.RECOVER_INFO:
                return self._recover_kickoff(session, done)
            return self._negotiate_kickoff(session, done)

        if state == AgentState.PROPOSING_SLOT:
            return self._negotiate_propose(session, done)

        # The ``"re_extract" in done`` guard stops a single reply being
        # re-extracted twice in one wake -- needed when recovery enrichment
        # promotes the lead and re-enters the negotiation trajectory in-wake.
        if state == AgentState.AWAITING_USER_REPLY:
            if etype != AgentEventType.USER_REPLY or "re_extract" in done:
                return PlannerDecision(action="wait_user",
                                       next_state=AgentState.AWAITING_USER_REPLY)
            return _call("re_extract", args={"text": event.text},
                         next_state=AgentState.EVALUATING_REPLY,
                         rationale="rianalisi della risposta utente")

        if state == AgentState.AWAITING_CONFIRMATION:
            if etype != AgentEventType.USER_REPLY or "re_extract" in done:
                return PlannerDecision(action="wait_user",
                                       next_state=AgentState.AWAITING_CONFIRMATION)
            return _call("re_extract", args={"text": event.text},
                         next_state=AgentState.EVALUATING_REPLY,
                         rationale="rianalisi della risposta utente")

        if state == AgentState.EVALUATING_REPLY:
            if session.goal == AgentGoal.RECOVER_INFO:
                return self._eval_recover(session, event, done, settings)
            return self._eval_confirm(session, event, done)

        return PlannerDecision(action="handoff", reason="no_transition")

    # -- trajectories --------------------------------------------------------

    def _recover_kickoff(self, session: AgentSession, done: set) -> PlannerDecision:
        if "send_message" not in done:
            text = (
                "Per completare la richiesta ci servono: "
                f"{', '.join(session.missing_fields)}"
            )
            return _call("send_message",
                         args={"template": "request_missing_info", "text": text,
                               "fields": session.missing_fields},
                         next_state=AgentState.AWAITING_USER_REPLY,
                         rationale="richiesta informazioni mancanti")
        return PlannerDecision(action="wait_user",
                               next_state=AgentState.AWAITING_USER_REPLY)

    def _negotiate_kickoff(self, session: AgentSession, done: set) -> PlannerDecision:
        if "check_inventory" not in done:
            return _call("check_inventory", args={"vehicle": session.vehicle_interest},
                         rationale="verifica disponibilità veicolo")
        if "check_availability" not in done:
            return _call("check_availability",
                         args={"preferences": {"vehicle": session.vehicle_interest}},
                         next_state=AgentState.PROPOSING_SLOT,
                         rationale="slot test drive disponibili")
        return PlannerDecision(action="wait_user",
                               next_state=AgentState.AWAITING_CONFIRMATION)

    def _negotiate_propose(self, session: AgentSession, done: set) -> PlannerDecision:
        if "send_message" not in done:
            slots = session.proposed_slots
            text = (
                f"Disponibilità per il test drive: {', '.join(slots)}. Quale preferisce?"
            )
            return _call("send_message",
                         args={"template": "propose_slots", "text": text, "slots": slots},
                         next_state=AgentState.AWAITING_CONFIRMATION,
                         rationale="proposta slot al cliente")
        return PlannerDecision(action="wait_user",
                               next_state=AgentState.AWAITING_CONFIRMATION)

    def _eval_recover(
        self,
        session: AgentSession,
        event: AgentEvent | None,
        done: set,
        settings: Settings,
    ) -> PlannerDecision:
        """Evaluate a recovery reply: re-extract, re-score, then re-route (§7.2).

        Enrichment can lift the lead to a better band: if it is now complete and
        booking-worthy the agent restarts the negotiation trajectory in-wake; if
        still cold or mid/low warm it hands the enriched lead to the operator
        (COMPLETED_INFO); if still incomplete it keeps chasing (bounded by the
        message budget) or hands off. It never marks a lead invalid -- "still
        weak" is not "fake".
        """
        text = (event.text or "").lower() if event else ""
        if any(w in text for w in _REFUSAL_WORDS):
            return PlannerDecision(action="handoff", reason="user_not_interested")
        raw = (event.text or "").strip() if event else ""
        reply = session.last_reply_features
        if len(raw) < 3 or reply is None:
            return PlannerDecision(action="handoff", reason="reply_uninformative")

        prev_missing = set(session.missing_fields)
        score, category, merged = _rescore(session, reply, settings)
        session.category = category
        session.final_score = score  # persisted so the operator view can realign (§7.2)
        session.missing_fields = list(merged.missing_critical_fields)
        new_missing = set(session.missing_fields)

        if new_missing:
            made_progress = len(new_missing) < len(prev_missing)
            has_room = session.messages_sent < settings.agent_max_messages
            if made_progress and has_room:
                text_out = (
                    "Per completare la richiesta ci servono ancora: "
                    f"{', '.join(session.missing_fields)}"
                )
                return _call("send_message",
                             args={"template": "request_missing_info",
                                   "text": text_out,
                                   "fields": session.missing_fields},
                             next_state=AgentState.AWAITING_USER_REPLY,
                             rationale="ulteriori informazioni mancanti")
            # Not converging: hand the (partially enriched) lead to a human.
            return PlannerDecision(action="complete",
                                   next_state=AgentState.COMPLETED_INFO,
                                   reason="info_partial")

        # Complete now -> route by value, the SAME rule as the hot path.
        _action, goal = route_complete(category, score, session.consent is True, settings)
        if goal == AgentGoal.NEGOTIATE_APPOINTMENT:
            # Promoted to booking-worthy: restart the negotiation in this wake.
            session.goal = AgentGoal.NEGOTIATE_APPOINTMENT
            session.state = AgentState.TRIGGERED
            return self._negotiate_kickoff(session, done)
        # Cold or mid/low warm after enrichment -> not automation-worthy: hand the
        # enriched lead to the operator (never nurture, never invalid).
        return PlannerDecision(action="complete",
                               next_state=AgentState.COMPLETED_INFO,
                               reason="info_completed")

    def _eval_confirm(
        self, session: AgentSession, event: AgentEvent | None, done: set
    ) -> PlannerDecision:
        text = (event.text or "").lower() if event else ""
        if any(w in text for w in _REFUSAL_WORDS):
            return PlannerDecision(action="handoff", reason="user_not_interested")

        chosen = _match_slot(text, session.proposed_slots)
        if chosen or any(w in text for w in _CONFIRM_WORDS):
            slot = chosen or (session.proposed_slots[0] if session.proposed_slots else "")
            # book_appointment is human-approval: enforce() will STAGE this.
            return _call("book_appointment", args={"slot": slot},
                         next_state=AgentState.BOOKED,
                         rationale="prenotazione predisposta, in attesa di conferma umana")

        if any(w in text for w in _COUNTER_WORDS):
            if "check_availability" not in done:
                return _call("check_availability", args={"preferences": {"counter": True}},
                             rationale="controproposta: nuovi slot")
            if "send_message" not in done:
                slots = session.proposed_slots
                return _call("send_message",
                             args={"template": "propose_slots",
                                   "text": f"Altre disponibilità: {', '.join(slots)}.",
                                   "slots": slots},
                             rationale="nuova proposta slot")
            return PlannerDecision(action="wait_user",
                                   next_state=AgentState.AWAITING_CONFIRMATION)

        return PlannerDecision(action="handoff", reason="ambiguous_reply")


class LLMPlanner:
    """Real tool-calling planner via structured output (off the SLA, mock-first).

    Builds a PII-safe decision prompt and asks the adapter for one structured
    :class:`PlannerDecision`. Any failure (no backend, timeout, invalid output)
    raises ``LLMError`` so the loop degrades to :class:`DeterministicPlanner`.
    """

    def __init__(self, adapter: LLMAdapter) -> None:
        self._adapter = adapter

    def next_action(
        self,
        session: AgentSession,
        event: AgentEvent | None,
        wake: list[AgentAction],
        settings: Settings,
    ) -> PlannerDecision:
        messages = build_planner_messages(session, event, wake)
        data = self._adapter.complete_json(
            PLANNER_SYSTEM_PROMPT, messages, PLANNER_DECISION_SCHEMA,
            model=settings.openai_agent_model,
        )
        try:
            return PlannerDecision(**data)
        except ValidationError as exc:  # pragma: no cover - requires live OpenAI
            raise LLMError(f"Invalid planner decision: {exc}") from exc
