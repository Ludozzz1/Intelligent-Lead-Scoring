"""Validation gate: structural, binary, conservative (REFACTOR_SPEC §5.1)."""

from __future__ import annotations

from src.gate.validity import evaluate_validity
from src.history import HistoryService
from tests.conftest import make_lead


def test_valid_lead_passes():
    v = evaluate_validity(make_lead())
    assert v.is_valid is True
    assert v.failure_type == "none"


def test_valid_with_email_only():
    v = evaluate_validity(make_lead(phone=None))
    assert v.is_valid is True


def test_valid_with_mobile_only():
    v = evaluate_validity(make_lead(email=None))
    assert v.is_valid is True


def test_valid_with_landline():
    v = evaluate_validity(make_lead(phone="0212345678", email=None))
    assert v.is_valid is True


def test_bogus_phone_no_email_is_invalid():
    v = evaluate_validity(make_lead(phone="0000000000", email=None))
    assert v.is_valid is False
    assert v.failure_type == "invalid"
    assert "phone_bogus" in v.reasons


def test_bogus_phone_wins_even_with_valid_email():
    v = evaluate_validity(make_lead(phone="1234567890"))
    assert v.is_valid is False
    assert "phone_bogus" in v.reasons


def test_disposable_email_is_invalid():
    v = evaluate_validity(make_lead(phone=None, email="x@mailinator.com"))
    assert v.is_valid is False
    assert "email_disposable" in v.reasons


def test_no_contact_is_invalid_with_missing_fields():
    v = evaluate_validity(make_lead(phone=None, email=None))
    assert v.is_valid is False
    assert "no_reachable_contact" in v.reasons
    assert "phone" in v.missing_fields and "email" in v.missing_fields


def test_malformed_email_not_fake_if_mobile_present():
    v = evaluate_validity(make_lead(email="not-an-email"))
    assert v.is_valid is True  # reachable via mobile; malformed email is just noise


def test_consent_is_not_a_gate_check():
    # Consent gates the agent (§7.5), not scoreability: a no-consent lead is valid.
    assert evaluate_validity(make_lead(consent=None)).is_valid is True
    assert evaluate_validity(make_lead(consent=False)).is_valid is True


def test_gibberish_message_passes_gate():
    # Semantic judgement (looks_invalid) is the LLM's job, not the structural gate.
    v = evaluate_validity(make_lead(message="asdfgh qwerty", name="asdfgh"))
    assert v.is_valid is True


def test_dedup_reported_but_stays_valid(tmp_history_settings):
    history = HistoryService(tmp_history_settings)
    v = evaluate_validity(make_lead(), history, tmp_history_settings)
    assert v.is_valid is True
    assert any(r.startswith("duplicate_prior_leads") for r in v.reasons)


def test_never_raises_on_empty_lead():
    from src.models.lead import Lead

    v = evaluate_validity(Lead())
    assert v.is_valid is False
