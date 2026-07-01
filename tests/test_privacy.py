"""Privacy: PII redaction before the LLM, canonical dedup keys, whitelist."""

from __future__ import annotations

import pytest

from src.privacy import (
    assert_no_raw_pii,
    email_key,
    phone_key,
    redact_message,
    safe_fields_for_llm,
)
from tests.conftest import make_lead


def test_redacts_phone_and_email():
    out = redact_message("Chiamami al 3471234599 o scrivi a mario@gmail.com")
    assert "3471234599" not in out and "mario@gmail.com" not in out
    assert "[PHONE]" in out and "[EMAIL]" in out


def test_redacts_introduced_name():
    out = redact_message("Sono Mario e cerco un SUV")
    assert "[NAME]" in out


def test_phone_key_normalizes_country_prefix():
    assert phone_key("+39 347 1234599") == phone_key("3471234599")


def test_email_key_normalizes_case():
    assert email_key("Mario.Rossi@Gmail.com") == email_key("mario.rossi@gmail.com")


def test_keys_none_for_empty():
    assert phone_key(None) is None and email_key("") is None


def test_assert_no_raw_pii_raises_on_raw():
    with pytest.raises(ValueError):
        assert_no_raw_pii("contatto 3471234599")
    with pytest.raises(ValueError):
        assert_no_raw_pii("mail a tizio@gmail.com")


def test_assert_no_raw_pii_ok_on_redacted():
    assert_no_raw_pii(redact_message("Chiamami al 3471234599"))  # must not raise


def test_safe_fields_whitelist_excludes_pii():
    safe = safe_fields_for_llm(make_lead())
    for forbidden in ("phone", "email", "name", "surname"):
        assert forbidden not in safe
    assert safe.get("zip_prefix") == "201"  # truncated, not full ZIP
    assert "channel" in safe
