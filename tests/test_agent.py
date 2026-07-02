"""Lead-Resolution Agent: state machine trajectories, guardrails, decision rights."""

from __future__ import annotations

from src.action.suggestions import finalize_with_session
from src.agent.guardrails import DECISION_RIGHTS
from src.agent.planner import PlannerDecision
from src.agent.runner import AgentRunner, human_approval, no_response, user_reply
from src.agent.state_machine import advance
from src.agent.tools import AgentTools
from src.config import Settings
from src.integrations.calendar import MockCalendar
from src.models.agent import AgentGoal, AgentState
from src.models.lead import ExtractedFeatures
from src.pipeline import Pipeline
from src.scoring.feature_vector import merge_features
from tests.conftest import NOW, make_lead

# An INCOMPLETE but high-value lead (score >= warm_high): strong intent + specific
# model + availability, with BOTH budget and timeline missing. Under the single
# trigger rule the agent recovers the missing fields, then books; a partial reply
# resolves one field while it keeps chasing the other. (Maps to a curated fixture
# base in scripts/build_mock_extractions.py -> data/mock_extractions.json.)
_RECOVER_MSG = (
    "Sono molto interessato alla Renault Captur, vorrei vederla e provarla il prima possibile."
)


def _scored(**overrides):
    lead = make_lead(**overrides)
    return Pipeline().score_lead(lead, now=NOW), lead


def _tools(actions):
    return [a.tool for a in actions]


def _status(actions, tool):
    return [a.status for a in actions if a.tool == tool]


def test_negotiate_confirm_books_pending_approval():
    scored, lead = _scored(lead_id="A1")
    # Confirming a slot STAGES the booking (PENDING_APPROVAL); the operator's
    # approval then executes it -> BOOKED (§7.5 human-approval gate).
    s = AgentRunner().run_scripted(
        scored, lead, [user_reply("Va bene sabato, confermo"), human_approval()]
    )
    assert s.state == AgentState.BOOKED
    assert _status(s.actions, "book_appointment") == ["pending_approval", "executed"]


def test_negotiate_confirm_pauses_for_approval():
    # Without the operator's approval the booking is staged, not executed.
    scored, lead = _scored(lead_id="A1b")
    s = AgentRunner().run_scripted(scored, lead, [user_reply("Va bene sabato, confermo")])
    assert s.state == AgentState.PENDING_APPROVAL
    assert _status(s.actions, "book_appointment") == ["pending_approval"]


def test_no_consent_routes_to_operator_not_agent():
    # Consent is evaluated UP FRONT: without it the agent cannot message, so the
    # lead goes to the operator instead of triggering a goal that would just hand
    # off. No wasted agent hop.
    scored, lead = _scored(lead_id="A2", consent=None)
    assert scored.agent_triggered is False
    assert scored.recommended_action == "lead_valido"
    assert AgentRunner().start_session(scored, lead) is None


def test_negotiate_no_response_disqualifies():
    scored, lead = _scored(lead_id="A3")
    s = AgentRunner().run_scripted(scored, lead, [no_response()])
    assert s.state == AgentState.DISQUALIFIED_NO_RESPONSE


def test_recover_uninformative_reply_hands_to_operator():
    # A reply the (mock) extractor cannot understand does NOT falsely complete the
    # lead: with no enrichment progress the agent hands it to a human.
    scored, lead = _scored(lead_id="A4", message=_RECOVER_MSG)
    assert scored.agent_goal == "recover_info"
    s = AgentRunner().run_scripted(
        scored, lead, [user_reply("Budget 25000 euro, vorrei comprare entro un mese")]
    )
    assert s.state == AgentState.COMPLETED_INFO


def test_recover_enriches_and_proposes_booking():
    # The agent recovers info, RE-SCORES off the reply, and -- now booking-worthy
    # -- proactively proposes test-drive slots in the same wake (§7.2).
    scored, lead = _scored(lead_id="A10", message=_RECOVER_MSG)
    assert scored.agent_goal == "recover_info"
    s = AgentRunner().run_scripted(
        scored, lead,
        [user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese.")],
    )
    assert s.goal.value == "negotiate_appointment"
    assert s.state == AgentState.AWAITING_CONFIRMATION
    assert s.proposed_slots
    assert "check_inventory" in _tools(s.actions)


def test_recover_enriches_then_books_on_confirmation():
    scored, lead = _scored(lead_id="A11", message=_RECOVER_MSG)
    s = AgentRunner().run_scripted(
        scored, lead,
        [user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese."),
         user_reply("Va bene sabato, confermo"),
         human_approval()],
    )
    assert s.state == AgentState.BOOKED


class _CompletePlanner:
    """Stub planner that immediately completes (mimics the observed LLM behaviour)."""

    def next_action(self, *args, **kwargs) -> PlannerDecision:
        return PlannerDecision(action="complete", next_state=AgentState.COMPLETED_INFO)


def test_planner_complete_on_recover_still_promotes_to_booking():
    # Parity fix: when ANY planner (e.g. the LLM) completes a recover_info session, the
    # state machine re-scores the recovered lead deterministically and -- if it is now
    # booking-worthy -- promotes it to a booking instead of stopping at COMPLETED_INFO.
    scored, lead = _scored(lead_id="A14", message=_RECOVER_MSG)
    assert scored.agent_goal == "recover_info"
    runner = AgentRunner()
    session = runner.start_session(scored, lead)  # kickoff asks for info, then waits
    assert session.state == AgentState.AWAITING_USER_REPLY

    advance(
        session,
        user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese."),
        runner.tools,
        Settings(),
        planner=_CompletePlanner(),
    )
    assert session.goal == AgentGoal.NEGOTIATE_APPOINTMENT  # promoted, not completed
    assert session.state == AgentState.AWAITING_CONFIRMATION
    assert session.proposed_slots
    assert "re_extract" in _tools(session.actions)  # re-scored off the reply


def test_merge_removes_answered_field_without_reintroducing_known():
    # When the user answers one field at a time, the merge must remove the answered
    # field WITHOUT re-flagging as missing a field the base already knew (else recovery
    # never converges -- the reply extracted alone reports the others missing).
    base = ExtractedFeatures(extraction_source="mock", extraction_confidence=0.9,
                             missing_critical_fields=["timeline_acquisto"])  # budget known
    reply = ExtractedFeatures(extraction_source="mock", extraction_confidence=0.9,
                              missing_critical_fields=["budget"])  # reply gives timeline, not budget
    assert merge_features(base, reply).missing_critical_fields == []

    # Partial answer -> keep chasing exactly the field still missing.
    base2 = ExtractedFeatures(extraction_source="mock", extraction_confidence=0.9,
                              missing_critical_fields=["budget", "timeline_acquisto"])
    reply2 = ExtractedFeatures(extraction_source="mock", extraction_confidence=0.9,
                               missing_critical_fields=["timeline_acquisto"])
    assert merge_features(base2, reply2).missing_critical_fields == ["timeline_acquisto"]

    # Low-confidence reply -> keep the base's needs unchanged (no false completion).
    weak = ExtractedFeatures(extraction_source="mock", extraction_confidence=0.2,
                             missing_critical_fields=[])
    assert merge_features(base2, weak).missing_critical_fields == ["budget", "timeline_acquisto"]


def test_recover_partial_keeps_chasing():
    # A reply that supplies only one missing field -> the agent asks for the rest
    # (bounded by the message budget), staying in the recovery trajectory.
    scored, lead = _scored(lead_id="A12", message=_RECOVER_MSG)
    s = AgentRunner().run_scripted(
        scored, lead, [user_reply("il budget è circa 25000 euro.")]
    )
    assert s.state == AgentState.AWAITING_USER_REPLY
    assert _status(s.actions, "send_message").count("executed") == 2
    assert s.missing_fields == ["timeline_acquisto"]


def test_recover_persists_final_score_and_realigns_view():
    # After enrichment + booking, the operator view is realigned to the outcome:
    # the action flips chiedere_info -> lead_valido and the lead closes in "agente".
    scored, lead = _scored(lead_id="A13", message=_RECOVER_MSG)
    assert scored.recommended_action == "chiedere_info"
    s = AgentRunner().run_scripted(
        scored, lead,
        [user_reply("Il mio budget è 25000 euro, vorrei comprare entro un mese."),
         user_reply("Va bene sabato, confermo"), human_approval()],
    )
    assert s.state == AgentState.BOOKED
    assert s.final_score is not None  # re-scored off the recovery reply (§7.2)
    final = finalize_with_session(scored, s)
    assert final.recommended_action == "lead_valido"
    assert final.queue == "agente"
    assert final.agent_status == "Appuntamento prenotato"


def test_tool_failure_hands_off():
    scored, lead = _scored(lead_id="A5")
    tools = AgentTools(calendar=MockCalendar(failures={"check_availability"}))
    s = AgentRunner(tools=tools).run_scripted(scored, lead, [])
    assert s.state == AgentState.HANDOFF_HUMAN
    assert "failed" in _status(s.actions, "check_availability")


def test_max_turns_guardrail_hands_off():
    scored, lead = _scored(lead_id="A6")
    counters = [user_reply("preferisco un altro orario") for _ in range(8)]
    s = AgentRunner().run_scripted(scored, lead, counters)
    assert s.state == AgentState.HANDOFF_HUMAN


def test_agent_never_disqualifies_for_quality():
    scored, lead = _scored(lead_id="A7")
    s = AgentRunner().run_scripted(scored, lead, [user_reply("confermo sabato")])
    assert "mark_invalid" not in _tools(s.actions)
    assert "disqualify" not in " ".join(_tools(s.actions))


def test_decision_rights_matrix():
    assert DECISION_RIGHTS["book_appointment"] == "human_approval"
    assert DECISION_RIGHTS["send_message"] == "auto_if_consent"
    assert DECISION_RIGHTS["disqualify_for_quality"] == "never"


def test_resume_on_reply_persists_session():
    scored, lead = _scored(lead_id="A8")
    runner = AgentRunner()
    runner.start_session(scored, lead)  # kickoff -> AWAITING_CONFIRMATION
    runner.resume_on_reply("A8", user_reply("ok confermo"))  # -> PENDING_APPROVAL
    s = runner.resume_on_reply("A8", human_approval())  # operator approves -> BOOKED
    assert s is not None and s.state == AgentState.BOOKED
    # Stored session reflects the terminal state.
    assert runner.store.get("A8").state == AgentState.BOOKED


def test_no_trigger_means_no_session():
    # An incomplete lead WITHOUT consent cannot be auto-messaged -> no agent (the
    # operator handles it). Consent is the up-front gate.
    scored, lead = _scored(lead_id="A9", message="Buongiorno.", zip_code="90133",
                           city="Palermo", consent=None)
    assert scored.agent_triggered is False
    assert AgentRunner().start_session(scored, lead) is None
