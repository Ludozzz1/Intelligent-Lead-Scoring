"""Lead-Resolution loop controller (REFACTOR_SPEC §7.2).

``advance(session, event, tools)`` applies one event to a persisted session. It is
a thin controller around a :class:`~src.agent.planner.Planner`: per step it asks
the planner for one decision, runs it through ``enforce`` (decision-rights +
guardrails), executes the resulting tool, records an audit action, and transitions
-- looping until the wake reaches a wait/terminal/staged state.

The planner is chosen by ``llm_mode``: deterministic (default, mock) or LLM-driven
(off the SLA, with deterministic degrade on any LLM failure). "The LLM proposes,
the deterministic layer disposes": every action passes through ``enforce``.

Recovering info and negotiating an appointment are the same machine on different
trajectories. Booking is human-approval: it is *staged* (``PENDING_APPROVAL``) and
executed only on a ``HUMAN_APPROVAL`` event. The agent never disqualifies for
quality -- it closes only on non-response or hands off to a human.
"""

from __future__ import annotations

from typing import Any, Callable

from src.action.decision import route_complete
from src.agent.guardrails import enforce, limit_breached
from src.agent.planner import (
    DeterministicPlanner,
    LLMPlanner,
    Planner,
    rescore_recovery,
)
from src.agent.tools import AgentTools
from src.config import Settings, get_settings
from src.extraction.llm import LLMError
from src.models.agent import (
    TERMINAL_STATES,
    AgentAction,
    AgentEvent,
    AgentEventType,
    AgentGoal,
    AgentSession,
    AgentState,
)

# Outbound message tools that count against the per-lead message budget.
_OUTBOUND_MESSAGE_TOOLS = frozenset({"send_message", "send_asset", "capture_consent"})
# States that wait for an external event (a reply / a timeout): once an outbound
# message lands here, the wake must stop -- the next decision depends on the user's
# reply, not on the agent chaining further actions autonomously.
_WAITING_STATES = frozenset({
    AgentState.AWAITING_USER_REPLY,
    AgentState.AWAITING_CONFIRMATION,
})
# Safety net: max tool steps within a single wake (prevents intra-wake runaway).
_MAX_WAKE_STEPS = 12


def advance(
    session: AgentSession,
    event: AgentEvent,
    tools: AgentTools,
    settings: Settings | None = None,
    *,
    planner: Planner | None = None,
) -> AgentSession:
    """Apply one event to the session, returning the (mutated) session."""
    settings = settings or get_settings()
    if session.is_terminal:
        return session

    # An operator's verdict on a staged action: execute it (or abort) -- not a turn.
    if event is not None and event.type == AgentEventType.HUMAN_APPROVAL:
        return _on_human_approval(session, event, tools)

    session.turns += 1
    breach = limit_breached(session, settings)
    if breach:
        return _handoff(session, tools, breach)

    active = planner or _select_planner(settings, tools)
    wake: list[AgentAction] = []

    while True:
        if len(wake) >= _MAX_WAKE_STEPS:
            return _handoff(session, tools, "wake_step_limit")
        breach = limit_breached(session, settings)
        if breach:
            return _handoff(session, tools, breach)

        try:
            if isinstance(active, LLMPlanner):
                session.llm_calls += 1
            decision = active.next_action(session, event, wake, settings)
        except LLMError:
            active = DeterministicPlanner()  # degrade, keep the loop alive
            continue

        enforced = enforce(decision, session, settings)

        if enforced.action == "wait_user":
            if enforced.next_state:
                session.state = enforced.next_state
            return session

        if enforced.action == "complete":
            # Before completing a recover_info session, deterministically re-score the
            # recovered lead: if it is now booking-worthy, promote it to a booking
            # instead of just completing. Enforced here (not in a planner) so it holds
            # for ANY planner -- the LLM orchestrates the conversation but never judges
            # booking-worthiness. On promotion the deterministic planner drives the
            # negotiation to a slot proposal.
            if (session.goal == AgentGoal.RECOVER_INFO
                    and _promote_recovered_lead(session, event, tools, settings)):
                active = DeterministicPlanner()
                continue
            # A 'complete' MUST land on a terminal state; a malformed planner
            # decision (e.g. LLM 'complete' without next_state) hands off rather
            # than leaving the session stuck non-terminal forever.
            if enforced.next_state in TERMINAL_STATES:
                session.state = enforced.next_state
                return session
            return _handoff(session, tools, "complete_without_terminal_state")

        if enforced.action == "handoff":
            if enforced.pre_record:
                pr = enforced.pre_record
                _record(session, pr["tool"], pr["status"], pr["reason"], pr.get("args"))
            return _handoff(session, tools, enforced.reason)

        if enforced.action == "stage":
            _record(session, enforced.tool, "pending_approval",
                    enforced.rationale or "azione in attesa di approvazione umana",
                    enforced.args, {})
            session.pending_action = {
                "tool": enforced.tool,
                "args": enforced.args,
                "next_state": enforced.next_state.value if enforced.next_state else None,
            }
            session.state = AgentState.PENDING_APPROVAL
            return session

        # call_tool: only a well-formed tool call reaches execution. Any other
        # enforced action (an out-of-contract planner ``action``, or a call_tool
        # without a tool -- possible from the LLM planner) hands off safely rather
        # than invoking ``None`` and crashing the audit trail.
        if enforced.action != "call_tool" or not enforced.tool:
            return _handoff(session, tools, f"malformed_decision:{enforced.action}")
        res = _safe(session, tools, enforced.tool,
                    lambda: _invoke_tool(session, tools, enforced.tool, enforced.args))
        if res is None:
            return session  # _safe already handed off
        _record(session, enforced.tool, "executed",
                enforced.rationale or enforced.reason, enforced.args,
                _result_for_audit(enforced.tool, res))
        wake.append(session.actions[-1])
        _apply_side_effects(session, enforced.tool, enforced.args, res)
        if enforced.next_state:
            session.state = enforced.next_state
        if enforced.tool in _OUTBOUND_MESSAGE_TOOLS:
            session.messages_sent += 1
        # "Ask, then wait": a send_message that moves the session into a waiting state
        # (request_missing_info -> AWAITING_USER_REPLY, propose_slots ->
        # AWAITING_CONFIRMATION) ends the wake. The next step must be driven by the
        # user's reply (an external event), never chained in the same wake. Enforced in
        # code so it holds for ANY planner -- the LLM cannot run past a question it just
        # asked (e.g. answer itself, then complete) the way it could when only a
        # 'wait_user' decision stopped the loop. capture_consent is intentionally
        # excluded: its opt-in wait may be followed by a handoff in the same wake.
        if enforced.tool == "send_message" and session.state in _WAITING_STATES:
            return session


# --- planner selection ------------------------------------------------------


def _select_planner(settings: Settings, tools: AgentTools) -> Planner:
    """Deterministic planner in mock mode; real LLM planner otherwise."""
    if settings.llm_mode == "mock":
        return DeterministicPlanner()
    return LLMPlanner(adapter=tools.adapter)


# --- human approval ---------------------------------------------------------


def _on_human_approval(
    session: AgentSession, event: AgentEvent, tools: AgentTools
) -> AgentSession:
    """Execute a staged action after an operator approves it (§7.5)."""
    if session.state != AgentState.PENDING_APPROVAL or not session.pending_action:
        return _handoff(session, tools, "unexpected_approval")
    if not event.approved:
        session.pending_action = None
        return _handoff(session, tools, "approval_rejected")

    pa = session.pending_action
    session.pending_action = None  # consume it now: whether it executes or fails, it's done
    tool, args = pa["tool"], pa.get("args", {})
    res = _safe(session, tools, tool, lambda: _invoke_tool(session, tools, tool, args))
    if res is None:
        return session  # _safe already handed off (pending_action already cleared)
    _record(session, tool, "executed", "azione confermata dopo approvazione umana",
            args, _result_for_audit(tool, res))
    _apply_side_effects(session, tool, args, res)
    session.state = AgentState(pa["next_state"]) if pa.get("next_state") else AgentState.BOOKED
    return session


# --- recovery re-score & promotion ------------------------------------------


def _reply_context(session: AgentSession) -> dict | None:
    """Question context for re-extracting a recovery reply: the vehicle + the fields
    just asked. Lets the extractor read a short answer (e.g. "entro due settimane") as
    the answer to that question instead of in a vacuum. None outside recovery (nothing
    was asked, so no reframing)."""
    if not session.missing_fields:
        return None
    return {"vehicle": session.vehicle_interest, "fields": list(session.missing_fields)}


def _promote_recovered_lead(
    session: AgentSession,
    event: AgentEvent,
    tools: AgentTools,
    settings: Settings,
) -> bool:
    """Re-score a recovered lead before it completes; promote it to a booking if worthy.

    Mirrors ``DeterministicPlanner._eval_recover``'s promotion so the SAME rule applies
    under the LLM planner: merge the reply's fresh extraction into the cached base,
    re-score deterministically, and -- if the lead is now complete and booking-worthy --
    switch the goal to ``negotiate_appointment`` (returning True). If the reply was never
    re-extracted (the LLM may have skipped it) we re-extract it here so the re-score has
    real signals. Returns False when the lead stays incomplete or not automation-worthy,
    letting the caller complete it as ``COMPLETED_INFO``.
    """
    reply = session.last_reply_features
    if reply is None:
        text = event.text if event and event.type == AgentEventType.USER_REPLY else None
        if not text:
            return False
        reply = tools.re_extract(text, _reply_context(session))
        session.last_reply_features = reply
        _record(session, "re_extract", "executed",
                "rianalisi della risposta e ri-valutazione dello score",
                {}, _result_for_audit("re_extract", reply))

    score, category, merged = rescore_recovery(session, reply, settings)
    session.category = category
    session.final_score = score  # persisted so the operator view can realign (§7.2)
    session.missing_fields = list(merged.missing_critical_fields)
    if session.missing_fields:
        return False  # still incomplete -> complete as COMPLETED_INFO

    _action, goal = route_complete(category, score, session.consent is True, settings)
    if goal != AgentGoal.NEGOTIATE_APPOINTMENT:
        return False  # cold / mid-warm after enrichment -> operator, not automation
    session.goal = AgentGoal.NEGOTIATE_APPOINTMENT
    session.state = AgentState.TRIGGERED
    return True


# --- tool dispatch ----------------------------------------------------------


def _invoke_tool(
    session: AgentSession, tools: AgentTools, tool: str | None, args: dict
) -> Any:
    """Call the named AgentTools method, pulling session context as needed."""
    if tool == "re_extract":
        return tools.re_extract(args.get("text"), _reply_context(session))
    if tool == "check_inventory":
        return tools.check_inventory(args.get("vehicle") or session.vehicle_interest)
    if tool == "recommend_alternatives":
        return tools.recommend_alternatives(
            args.get("vehicle") or session.vehicle_interest, args.get("budget"))
    if tool == "check_availability":
        return tools.check_availability(args.get("preferences") or {})
    if tool == "estimate_trade_in":
        return tools.estimate_trade_in(args.get("vehicle_desc"))
    if tool == "simulate_financing":
        return tools.simulate_financing(
            args.get("price"), args.get("down_payment"), args.get("trade_in_value"))
    if tool == "send_message":
        return tools.send_message(
            session.channel, args.get("template", "generic"), session.to_token,
            text=args.get("text", ""))
    if tool == "send_asset":
        return tools.send_asset(
            session.channel, args.get("vehicle") or session.vehicle_interest,
            args.get("asset_type", "vehicle_sheet"), session.to_token)
    if tool == "capture_consent":
        return tools.capture_consent(session.channel, session.to_token)
    if tool == "schedule_followup":
        return tools.schedule_followup(session.lead_id, args.get("when", "+1d"))
    if tool == "book_appointment":
        return tools.book_appointment(args.get("slot", ""), session.lead_id)
    if tool == "update_crm":
        return tools.update_crm(
            session.lead_id, args.get("outcome", ""), args.get("note", ""))
    if tool == "warm_transfer_to_operator":
        return tools.warm_transfer_to_operator(session.lead_id, args.get("context", ""))
    raise ValueError(f"unknown tool: {tool}")


def _apply_side_effects(
    session: AgentSession, tool: str | None, args: dict, res: Any
) -> None:
    """Persist tool results that later decisions / the next wake depend on."""
    if tool == "check_availability" and isinstance(res, list):
        session.proposed_slots = list(res)
    elif tool == "re_extract":
        # Keep the fresh extraction so the planner can merge + re-score (§7.2).
        session.last_reply_features = res
    elif tool == "book_appointment":
        session.chosen_slot = args.get("slot")
    elif tool == "schedule_followup":
        session.followups_sent += 1
    elif tool == "capture_consent":
        session.consent_requested = True


def _result_for_audit(tool: str | None, res: Any) -> dict:
    """Coerce a tool result into a JSON-able audit payload."""
    if hasattr(res, "extraction_source"):  # ExtractedFeatures
        return {"extraction_source": res.extraction_source,
                "intent_strength": getattr(res, "intent_strength", None)}
    if isinstance(res, list):
        return {"slots": res}
    if isinstance(res, dict):
        return res
    return {"result": res}


# --- helpers ----------------------------------------------------------------


def _handoff(session: AgentSession, tools: AgentTools, reason: str) -> AgentSession:
    """Escalate to a human and move to the terminal HANDOFF state."""
    try:
        res = tools.escalate_to_human(reason, session.lead_id)
        status, result = "executed", res
    except Exception as exc:  # noqa: BLE001 - escalation must not crash the loop
        status, result = "failed", {"error": str(exc)}
    _record(session, "escalate_to_human", status, f"handoff: {reason}",
            {"reason": reason}, result)
    session.state = AgentState.HANDOFF_HUMAN
    return session


def _safe(
    session: AgentSession, tools: AgentTools, tool: str, fn: Callable[[], Any]
) -> Any:
    """Run a tool; on failure record it and hand off (returns None to signal stop)."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - tool errors -> handoff, never inconsistent
        _record(session, tool, "failed", f"tool error: {exc}")
        _handoff(session, tools, f"tool_failed:{tool}")
        return None


def _record(
    session: AgentSession,
    tool: str,
    status: str,
    reason: str,
    args: dict | None = None,
    result: dict | None = None,
) -> None:
    session.actions.append(
        AgentAction(
            tool=tool, status=status, reason=reason,
            args=args or {}, result=result or {},
        )
    )
