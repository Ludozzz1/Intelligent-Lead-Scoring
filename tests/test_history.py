"""History service: runtime dedup + personalization only (no priors, §11)."""

from __future__ import annotations

from src.history import HistoryService
from tests.conftest import _VALID_EMAIL, _VALID_PHONE, make_lead


def test_find_duplicate_by_phone(tmp_history_settings):
    h = HistoryService(tmp_history_settings)
    dup = h.find_duplicate(_VALID_PHONE, None)
    assert dup.is_duplicate is True
    assert dup.prior_leads_count >= 1
    assert dup.last_seen_at is not None


def test_find_duplicate_by_email(tmp_history_settings):
    h = HistoryService(tmp_history_settings)
    assert h.find_duplicate(None, _VALID_EMAIL).is_duplicate is True


def test_no_duplicate_for_unknown_contact(tmp_history_settings):
    h = HistoryService(tmp_history_settings)
    dup = h.find_duplicate("3990000000", "unknown@nowhere.it")
    assert dup.is_duplicate is False
    assert dup.prior_leads_count == 0


def test_returning_customer_personalization(tmp_history_settings):
    h = HistoryService(tmp_history_settings)
    p = h.personalize(make_lead(phone=_VALID_PHONE, email=_VALID_EMAIL))
    assert p.is_returning_customer is True
    assert "ritorno" in p.history_notes.lower()


def test_unknown_contact_empty_personalization(tmp_history_settings):
    h = HistoryService(tmp_history_settings)
    p = h.personalize(make_lead(phone="3990000000", email="x@nowhere.it"))
    assert p.is_returning_customer is False
    assert p.prior_leads_count == 0


def test_missing_history_file_never_raises(tmp_path):
    from src.config import Settings

    h = HistoryService(Settings(leads_history_path=tmp_path / "nope.json"))
    assert h.record_count == 0
    assert h.find_duplicate("3471234599", None).is_duplicate is False


def test_real_history_loads(real_settings):
    h = HistoryService(real_settings)
    assert h.record_count > 0
