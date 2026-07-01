"""Hot-path pipeline: end-to-end categories, idempotency, SLA, single LLM call."""

from __future__ import annotations

from datetime import datetime

from src.models.lead import Lead
from src.pipeline import Pipeline
from tests.conftest import NOW, make_lead


def _pipe() -> Pipeline:
    return Pipeline()


def test_hot_lead():
    r = _pipe().score_lead(make_lead(), now=NOW)
    assert r.category == "hot"
    assert r.score >= 65
    assert r.recommended_action in ("lead_valido", "chiedere_info")


def test_warm_lead():
    lead = make_lead(
        message="Sto valutando una Renault Captur, vorrei sapere i prezzi e le promozioni disponibili."
    )
    r = _pipe().score_lead(lead, now=NOW)
    assert r.category == "warm"


def test_cold_lead():
    lead = make_lead(message="Buongiorno.", zip_code="90133", city="Palermo")
    r = _pipe().score_lead(lead, now=NOW)
    assert r.category == "cold"


def test_invalid_lead_scores_zero():
    lead = make_lead(phone="0000000000", email=None)
    r = _pipe().score_lead(lead, now=NOW)
    assert r.category == "invalid"
    assert r.score == 0
    assert r.recommended_action == "scartare"
    # Auto-discarded: out of the operator call queue, with an audit suggestion.
    assert r.queue == "scartato"
    assert r.next_best_action


def test_looks_invalid_makes_lead_invalid():
    lead = make_lead(message="asdfgh qwerty")
    r = _pipe().score_lead(lead, now=NOW)
    assert r.category == "invalid"


def test_idempotency_marks_duplicate():
    p = _pipe()
    lead = make_lead(lead_id="DUP-1")
    first = p.score_lead(lead, now=NOW)
    second = p.score_lead(lead, now=NOW)
    assert first.score == second.score
    assert second.personalization.is_duplicate is True


def test_dedup_without_lead_id_uses_contact():
    p = _pipe()
    lead = make_lead(lead_id=None)
    a = p.score_lead(lead, now=NOW)
    b = p.score_lead(lead, now=NOW)
    assert a.lead_id == b.lead_id
    assert b.personalization.is_duplicate is True


def test_never_raises_on_empty_lead():
    r = _pipe().score_lead(Lead(), now=NOW)
    assert r.category in ("hot", "warm", "cold", "invalid")


def test_latency_and_timestamp_stamped():
    r = _pipe().score_lead(make_lead(), now=NOW)
    assert r.latency_ms >= 0
    assert r.processed_at is not None


def test_single_llm_call_in_hot_path():
    p = _pipe()
    calls = {"n": 0}
    original = p._llm.extract

    def counting(msg, context=None):
        calls["n"] += 1
        return original(msg, context)

    p._llm.extract = counting  # type: ignore[assignment]
    p.score_lead(make_lead(lead_id="ONE"), now=NOW)
    assert calls["n"] == 1  # extraction only; motivation is deterministic


def test_fallback_keeps_scoring_on_llm_failure():
    p = _pipe()

    def boom(msg, context=None):
        raise RuntimeError("llm down")

    p._llm.extract = boom  # type: ignore[assignment]
    r = p.score_lead(make_lead(lead_id="FB"), now=NOW)
    assert r.low_confidence is True
    assert r.category in ("hot", "warm", "cold", "invalid")


def test_agent_trigger_flag_on_hot_with_availability():
    r = _pipe().score_lead(make_lead(), now=NOW)
    assert r.agent_triggered is True
    assert r.agent_goal in ("negotiate_appointment", "recover_info")
    # Agent-owned lead: routed to the "agente" bucket, operator told not to call.
    assert r.queue == "agente"
    assert "non chiamare" in r.next_best_action


def test_active_operator_lead_gets_actionable_suggestion():
    # No consent -> the agent cannot message -> the operator handles it manually.
    r = _pipe().score_lead(make_lead(consent=False), now=NOW)
    assert r.queue == "attiva"
    assert r.agent_triggered is False
    assert r.next_best_action  # a concrete next-best-action, not an agent status
