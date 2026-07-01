"""Agent guardrails + decision-rights (REFACTOR_SPEC §7.4 / §7.5).

Stop conditions and the authority matrix that gate what the agent may do
autonomously. ``enforce`` is the single chokepoint: the planner *proposes* a
:class:`PlannerDecision`, ``enforce`` applies decision-rights + an allow-list +
consent + budget and returns an :class:`EnforcedDecision` the loop acts on
("the LLM proposes, the deterministic layer disposes"). Kept declarative so it
mirrors ``docs/decision_rights.md``.
"""

from __future__ import annotations

from src.agent.planner import EnforcedDecision, PlannerDecision
from src.config import Settings, get_settings
from src.models.agent import AgentSession

# Decision-rights matrix (v1). Authority: "auto" | "auto_if_consent" |
# "human_approval" | "never". Keyed by tool, or by message template where the
# authority depends on the message purpose. Mirrors docs/decision_rights.md.
DECISION_RIGHTS: dict[str, str] = {
    # Internal / read-only tools.
    "re_extract": "auto",
    "check_inventory": "auto",
    "recommend_alternatives": "auto",
    "check_availability": "auto",
    "estimate_trade_in": "auto",
    "simulate_financing": "auto",
    "schedule_followup": "auto",
    "update_crm": "auto",
    "warm_transfer_to_operator": "auto",
    "escalate_to_human": "auto",
    # Outbound messaging (consent-gated). Keyed by template for send_message.
    "request_missing_info": "auto_if_consent",
    "propose_slots": "auto_if_consent",  # negotiation is autonomous (v1, §7.5)
    "send_message": "auto_if_consent",
    "send_asset": "auto_if_consent",
    "capture_consent": "auto",  # the message that *acquires* consent (double opt-in)
    # Costly / irreversible: human in the loop.
    "book_appointment": "human_approval",  # confirming a booking needs a human in v1
    # Forbidden to the agent.
    "disqualify_for_quality": "never",  # only the deterministic gate invalidates
}


def consent_ok(session: AgentSession) -> bool:
    """True when the lead has verified consent for outbound messaging (§7.5)."""
    return session.consent is True


def right_key(tool: str | None, args: dict) -> str | None:
    """Resolve the decision-rights key: the template for send_message, else tool."""
    if tool == "send_message":
        return args.get("template") or "send_message"
    return tool


def limit_breached(session: AgentSession, settings: Settings | None = None) -> str | None:
    """Return a handoff reason if a step/message/LLM budget is exceeded, else None."""
    s = settings or get_settings()
    if session.turns >= s.agent_max_turns:
        return "max_turns_exceeded"
    if session.messages_sent >= s.agent_max_messages:
        return "max_messages_exceeded"
    # NB: `>` (not `>=`) is intentional. The follow-up ladder is *self-limited* by
    # the planner (schedule up to agent_max_followups, then disqualify gracefully);
    # this is a defensive backstop that only fires if a planner over-schedules
    # *beyond* the ladder. The other budgets are hard ceilings, hence `>=`.
    if session.followups_sent > s.agent_max_followups:
        return "max_followups_exceeded"
    if session.llm_calls >= s.agent_max_llm_calls:
        return "max_llm_calls_exceeded"
    return None


def enforce(
    decision: PlannerDecision, session: AgentSession, settings: Settings | None = None
) -> EnforcedDecision:
    """Apply decision-rights + allow-list + consent to a proposed decision."""
    if decision.action != "call_tool":
        # wait_user / complete / handoff carry no tool authority.
        return EnforcedDecision(
            action=decision.action, next_state=decision.next_state,
            reason=decision.reason, rationale=decision.rationale,
        )

    tool = decision.tool
    right = DECISION_RIGHTS.get(right_key(tool, decision.args))

    if right is None:
        return EnforcedDecision(action="handoff", reason=f"tool_not_allowed:{tool}")
    if right == "never":
        return EnforcedDecision(action="handoff", reason=f"forbidden:{tool}")

    if right == "auto_if_consent" and not consent_ok(session):
        return EnforcedDecision(
            action="handoff", reason="no_consent_for_messaging",
            pre_record={
                "tool": tool, "status": "pending_approval",
                "reason": "messaggio outbound: consenso assente, richiede approvazione umana",
                "args": decision.args,
            },
        )

    if right == "human_approval":
        return EnforcedDecision(
            action="stage", tool=tool, args=decision.args, status="pending_approval",
            next_state=decision.next_state, reason=decision.reason,
            rationale=decision.rationale,
        )

    return EnforcedDecision(
        action="call_tool", tool=tool, args=decision.args, status="executed",
        next_state=decision.next_state, reason=decision.reason,
        rationale=decision.rationale,
    )
