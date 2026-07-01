"""Operator suggestions: queue routing, next-best-action vocabulary, finalize."""

from __future__ import annotations

from src.action.suggestions import (
    QUEUE_ACTIVE,
    QUEUE_AGENT,
    QUEUE_DISCARDED,
    agent_status_label,
    build_next_best_action,
    classify_queue,
    finalize_with_session,
)
from src.models.agent import AgentGoal, AgentSession, AgentState
from src.models.lead import ExtractedFeatures
from src.models.output import ScoredLead
from src.models.scoring import Personalization, ValidityResult


def _scored(**overrides) -> ScoredLead:
    base = dict(
        lead_id="L1",
        score=30,
        category="cold",
        validity=ValidityResult(is_valid=True, failure_type="none"),
        recommended_action="chiedere_info",
        queue=QUEUE_AGENT,
        personalization=Personalization(),
    )
    base.update(overrides)
    return ScoredLead(**base)


def _session(state: AgentState, **overrides) -> AgentSession:
    base = dict(lead_id="L1", goal=AgentGoal.RECOVER_INFO, state=state)
    base.update(overrides)
    return AgentSession(**base)


# -- classify_queue ----------------------------------------------------------


def test_classify_queue_invalid_is_discarded():
    assert classify_queue("invalid", False) == QUEUE_DISCARDED
    assert classify_queue("invalid", True) == QUEUE_DISCARDED  # invalid wins


def test_classify_queue_agent_vs_active():
    assert classify_queue("hot", True) == QUEUE_AGENT
    assert classify_queue("hot", False) == QUEUE_ACTIVE
    assert classify_queue("cold", False) == QUEUE_ACTIVE


# -- build_next_best_action --------------------------------------------------


def test_discarded_lead_gets_no_call_suggestion():
    nba = build_next_best_action("invalid", "scartare", ExtractedFeatures(), False)
    assert "Scartato" in nba and "nessuna chiamata" in nba


def test_agent_owned_lead_says_do_not_call():
    nba = build_next_best_action(
        "hot", "lead_valido", ExtractedFeatures(availability_mentioned=True),
        True, "negotiate_appointment",
    )
    assert "non chiamare" in nba


def test_ask_info_lists_missing_fields():
    feats = ExtractedFeatures(missing_critical_fields=["budget", "timeline_acquisto"])
    nba = build_next_best_action("warm", "chiedere_info", feats, False)
    assert "Chiedi le info mancanti" in nba
    assert "il budget" in nba and "i tempi d'acquisto" in nba


def test_active_hot_lead_proposes_test_drive():
    feats = ExtractedFeatures(
        availability_mentioned=True, budget_present=True, budget_value_eur=35000,
        vehicle_specificity="specific",
    )
    nba = build_next_best_action("hot", "lead_valido", feats, False)
    assert nba.startswith("Chiama subito")
    assert "test drive" in nba


def test_active_lead_without_budget_suggests_verifying_it():
    feats = ExtractedFeatures(budget_present=False, vehicle_specificity="specific")
    nba = build_next_best_action("warm", "lead_valido", feats, False)
    assert "verifica il budget" in nba


def test_levers_capped_at_two():
    feats = ExtractedFeatures(
        availability_mentioned=True, trade_in_present=True, budget_present=False,
        vehicle_specificity="none",
    )
    nba = build_next_best_action("hot", "lead_valido", feats, False)
    # availability + trade-in win; budget/model levers are dropped (max 2).
    assert "test drive" in nba and "permuta" in nba
    assert "verifica il budget" not in nba


# -- agent_status_label ------------------------------------------------------


def test_agent_status_label_known_state():
    assert agent_status_label(AgentState.BOOKED) == "Appuntamento prenotato"


# -- finalize_with_session ---------------------------------------------------


def test_finalize_completed_info_requalifies_to_operator():
    scored = _scored(category="cold", score=30, recommended_action="chiedere_info")
    out = finalize_with_session(
        scored, _session(AgentState.COMPLETED_INFO, category="warm", final_score=52)
    )
    assert out.queue == QUEUE_ACTIVE
    assert out.recommended_action == "lead_valido"
    assert out.category == "warm" and out.score == 52
    assert "qualificato" in out.next_best_action
    assert out.agent_status == "Info recuperate dall'agente"


def test_finalize_booked_closes_without_call():
    out = finalize_with_session(
        _scored(category="hot", score=80, recommended_action="lead_valido"),
        _session(AgentState.BOOKED, goal=AgentGoal.NEGOTIATE_APPOINTMENT),
    )
    assert out.queue == QUEUE_AGENT
    assert out.recommended_action == "lead_valido"
    assert "nessuna chiamata" in out.next_best_action


def test_finalize_handoff_reemerges_to_operator():
    out = finalize_with_session(
        _scored(category="warm", score=50, recommended_action="lead_valido"),
        _session(AgentState.HANDOFF_HUMAN),
    )
    assert out.queue == QUEUE_ACTIVE
    assert "riprendi" in out.next_best_action.lower()


def test_finalize_nurtured_stays_with_agent():
    out = finalize_with_session(
        _scored(category="cold", score=25),
        _session(AgentState.NURTURED, goal=AgentGoal.NURTURE),
    )
    assert out.queue == QUEUE_AGENT
    assert out.recommended_action == "nurturing"


def test_finalize_in_flight_keeps_agent_ownership():
    scored = _scored(category="warm", score=45, recommended_action="chiedere_info")
    out = finalize_with_session(scored, _session(AgentState.AWAITING_USER_REPLY))
    assert out.queue == QUEUE_AGENT
    assert out.recommended_action == "chiedere_info"  # unchanged while in flight
    assert "non chiamare" in out.next_best_action
