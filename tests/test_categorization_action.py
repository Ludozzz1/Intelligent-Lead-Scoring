"""Categorization bands, action decision + triggers, deterministic motivation."""

from __future__ import annotations

from src.action.decision import decide_action
from src.categorization.bands import categorize
from src.models.agent import AgentGoal
from src.models.features import ScoreResult
from src.models.lead import ExtractedFeatures
from src.models.scoring import Personalization, ValidityResult
from src.motivation.motivation import build_motivation

_VALID = ValidityResult(is_valid=True, failure_type="none")
_INVALID = ValidityResult(is_valid=False, failure_type="invalid", reasons=["phone_bogus"])
_NOPERS = Personalization()


# --- categorization ---------------------------------------------------------


def test_bands_hot_warm_cold():
    assert categorize(70, True, False) == "hot"
    assert categorize(50, True, False) == "warm"
    assert categorize(30, True, False) == "cold"


def test_invalid_from_gate():
    assert categorize(90, False, False) == "invalid"


def test_looks_invalid_overrides_category():
    assert categorize(90, True, True) == "invalid"


# --- action decision (value-aligned routing + consent up front) -------------


def test_invalid_discards_without_agent():
    d = decide_action("invalid", _INVALID, ExtractedFeatures(), 0, _NOPERS, consent=True)
    assert d.recommended_action == "scartare"
    assert d.agent_goal is None


def test_incomplete_any_band_with_consent_triggers_recover():
    feats = ExtractedFeatures(missing_critical_fields=["budget"])
    for cat, score in (("hot", 80), ("warm", 50), ("cold", 30)):
        d = decide_action(cat, _VALID, feats, score, _NOPERS, consent=True)
        assert d.recommended_action == "chiedere_info"
        assert d.agent_goal == AgentGoal.RECOVER_INFO


def test_incomplete_without_consent_goes_to_operator():
    feats = ExtractedFeatures(missing_critical_fields=["budget"])
    d = decide_action("hot", _VALID, feats, 80, _NOPERS, consent=None)
    assert d.recommended_action == "chiedere_info"
    assert d.agent_goal is None  # no consent -> the operator asks, no agent hop


def test_hot_complete_with_consent_negotiates():
    # No availability mention needed: a complete hot lead is booking-worthy.
    d = decide_action("hot", _VALID, ExtractedFeatures(), 85, _NOPERS, consent=True)
    assert d.recommended_action == "lead_valido"
    assert d.agent_goal == AgentGoal.NEGOTIATE_APPOINTMENT


def test_hot_complete_without_consent_to_operator():
    d = decide_action("hot", _VALID, ExtractedFeatures(), 85, _NOPERS, consent=None)
    assert d.recommended_action == "lead_valido"
    assert d.agent_goal is None


def test_warm_high_with_consent_negotiates():
    d = decide_action("warm", _VALID, ExtractedFeatures(), 60, _NOPERS, consent=True)
    assert d.agent_goal == AgentGoal.NEGOTIATE_APPOINTMENT


def test_warm_mid_goes_to_operator():
    d = decide_action("warm", _VALID, ExtractedFeatures(), 45, _NOPERS, consent=True)
    assert d.recommended_action == "lead_valido"
    assert d.agent_goal is None


def test_cold_complete_with_consent_nurtures():
    d = decide_action("cold", _VALID, ExtractedFeatures(), 30, _NOPERS, consent=True)
    assert d.recommended_action == "nurturing"
    assert d.agent_goal == AgentGoal.NURTURE


def test_cold_complete_without_consent_drops():
    d = decide_action("cold", _VALID, ExtractedFeatures(), 30, _NOPERS, consent=None)
    assert d.recommended_action == "nurturing"
    assert d.agent_goal is None  # cannot message without consent -> low-priority/drop


def test_priority_band_and_returning_boost():
    base = decide_action("hot", _VALID, ExtractedFeatures(), 90, _NOPERS, consent=True)
    ret = decide_action(
        "hot", _VALID, ExtractedFeatures(), 90,
        Personalization(is_returning_customer=True), consent=True,
    )
    assert 80 <= base.priority <= 100
    assert ret.priority >= base.priority


# --- motivation -------------------------------------------------------------


def test_motivation_invalid_looks_invalid():
    feats = ExtractedFeatures(looks_invalid=True)
    text = build_motivation("invalid", _VALID, feats, ScoreResult())
    assert "non valido" in text.lower()


def test_motivation_invalid_gate_reason():
    text = build_motivation("invalid", _INVALID, ExtractedFeatures(), ScoreResult())
    assert "telefono" in text.lower()


def test_motivation_valid_cites_signals_and_rationale():
    feats = ExtractedFeatures(
        intent_strength="high", budget_present=True, rationale_signals="budget chiaro, permuta"
    )
    sr = ScoreResult(score=88, contributions={"intent_strength": 18.0, "budget_present": 15.0})
    text = build_motivation("hot", _VALID, feats, sr)
    assert "hot" in text.lower()
    assert "88" in text
    assert "budget chiaro" in text


def test_motivation_flags_low_confidence():
    sr = ScoreResult(score=40, low_confidence=True)
    text = build_motivation("warm", _VALID, ExtractedFeatures(), sr)
    assert "bassa confidenza" in text.lower()
