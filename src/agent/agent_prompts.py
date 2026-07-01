"""Prompt + strict-ish JSON schema for the agent's LLM planner (REFACTOR_SPEC §7).

The planner orchestrates the conversation (which tool to call next), it never
scores. Mirrors the extraction pattern ([prompts.py](../extraction/prompts.py)):
a system prompt + a JSON schema for the structured decision. The planner only
*proposes*; the deterministic ``enforce()`` disposes (it may block, stage for
human approval, or allow). All recipients are opaque tokens; free text is
PII-redacted before it reaches this prompt.
"""

from __future__ import annotations

from typing import Any

from src.models.agent import AgentEvent, AgentSession
from src.privacy import redact_message

PLANNER_SYSTEM_PROMPT = """\
You are the planner of an Italian automotive Lead-Resolution agent. Your job is to
drive a promising (hot/warm) lead toward a TERMINAL state by choosing ONE next
action per step. You DO NOT score or invalidate leads (that is a deterministic
gate elsewhere). You only orchestrate the conversation.

Goals (from the session):
- recover_info: obtain the missing critical fields, then COMPLETE.
- negotiate_appointment: propose/agree a test-drive slot, then BOOK.

Available tools (the deterministic layer enforces who may run them):
- re_extract(text): re-analyze the user's last reply into structured signals.
- check_inventory(vehicle): {in_stock, alternatives}.
- recommend_alternatives(vehicle, budget): equivalent in-catchment alternatives.
- check_availability(preferences): list of test-drive slots.
- estimate_trade_in(vehicle_desc): indicative trade-in range (warms the lead).
- simulate_financing(price, down_payment, trade_in_value): indicative monthly instalment.
- send_message(template, text): outbound message to the user (consent-gated).
- send_asset(vehicle, asset_type): send a vehicle sheet / price list (consent-gated).
- capture_consent(): send a double opt-in request when consent is missing.
- schedule_followup(when): schedule a deferred follow-up (non-responder ladder).
- book_appointment(slot): book a slot. ALWAYS requires human approval; proposing it
  stages the booking and pauses the session until an operator approves.
- update_crm(outcome, note): write the resolution back to the CRM.
- warm_transfer_to_operator(context): hand off to a human with full context.

Hard rules:
- Personal data is already redacted to opaque tokens ([PHONE], [EMAIL], [NAME]);
  never infer intent from them.
- Propose, do not assume execution: the deterministic layer may block or stage your
  action. Booking is never executed by you autonomously.
- When the user does not reply, prefer a bounded follow-up ladder before giving up.
- When unsure or out of scope, hand off to a human.

Return ONLY a JSON object with: action (call_tool|wait_user|complete|handoff),
tool (or null), args (object), next_state (or null), reason, rationale.\
"""

PLANNER_DECISION_SCHEMA: dict = {
    "name": "planner_decision",
    "strict": False,
    "schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["call_tool", "wait_user", "complete", "handoff"],
            },
            "tool": {"type": ["string", "null"]},
            "args": {"type": "object"},
            "next_state": {"type": ["string", "null"]},
            "reason": {"type": "string"},
            "rationale": {"type": "string"},
        },
        "required": ["action"],
    },
}


def build_planner_messages(
    session: AgentSession, event: AgentEvent | None, wake: list[Any]
) -> list[dict]:
    """Build the (PII-safe) user message describing the current decision point."""
    recent = "; ".join(f"{a.tool}[{a.status}]" for a in wake) or "(nessuna)"
    last_reply = redact_message(event.text) if event and event.text else "(nessuna)"
    state = {
        "goal": session.goal.value,
        "state": session.state.value,
        "vehicle_interest": session.vehicle_interest,
        "missing_fields": session.missing_fields,
        "proposed_slots": session.proposed_slots,
        "consent": session.consent,
        "followups_sent": session.followups_sent,
        "last_user_reply": last_reply,
        "actions_this_wake": recent,
    }
    lines = "\n".join(f"- {k}: {v}" for k, v in state.items())
    return [{"role": "user", "content": f"Decision point:\n{lines}"}]
