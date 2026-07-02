"""Extraction: single LLM call, fixture mock, PII redaction, fallback (§5.2/§10)."""

from __future__ import annotations

import pytest

from src.extraction.extractor import extract_features
from src.extraction.llm import LLMAdapter, LLMError
from src.models.lead import Lead
from src.models.scoring import ValidityResult
from tests.conftest import make_lead

_VALID = ValidityResult(is_valid=True, failure_type="none")
_INVALID = ValidityResult(is_valid=False, failure_type="invalid")


def test_adapter_defaults_to_mock_without_key():
    assert LLMAdapter().mode == "mock"


def test_mock_fixture_extraction_is_rich_and_deterministic():
    adapter = LLMAdapter()
    lead = make_lead()  # message matches a fixture entry
    f1 = extract_features(lead, _VALID, adapter)
    f2 = extract_features(lead, _VALID, adapter)
    assert f1 == f2
    assert f1.extraction_source == "mock"
    assert f1.budget_present is True
    assert f1.intent_strength == "high"
    assert f1.low_confidence is False


def test_unknown_message_is_low_confidence_default():
    adapter = LLMAdapter()
    lead = make_lead(message="un messaggio mai visto prima di adesso")
    f = extract_features(lead, _VALID, adapter)
    assert f.extraction_source == "mock"
    assert f.low_confidence is True
    assert f.intent_strength == "low"


def test_extraction_skipped_on_invalid_lead():
    f = extract_features(make_lead(), _INVALID, LLMAdapter())
    assert f.extraction_source == "skipped"


def test_extraction_skipped_on_trivial_message():
    f = extract_features(make_lead(message="ok"), _VALID, LLMAdapter())
    assert f.extraction_source == "skipped"


def test_looks_invalid_extracted_for_gibberish():
    # The gibberish demo message is flagged looks_invalid by the fixture.
    lead = make_lead(message="asdfgh qwerty")
    f = extract_features(lead, _VALID, LLMAdapter())
    assert f.looks_invalid is True


def test_reply_context_reframes_extraction_prompt():
    # With reply_context the prompt is built differently: it marks the text as the
    # answer to a specific question (fields + vehicle), so a short reply is not
    # extracted in a vacuum. Without it, the message is passed through unchanged.
    from src.extraction.prompts import build_extraction_messages

    user = build_extraction_messages(
        "entro due settimane",
        reply_context={"vehicle": "Audi Q3", "fields": ["timeline_acquisto"]},
    )[-1]["content"]
    assert "REPLY" in user and "timeline_acquisto" in user and "Audi Q3" in user
    assert "entro due settimane" in user
    assert build_extraction_messages("ciao")[-1]["content"] == "ciao"


def test_redaction_strips_pii_before_adapter():
    captured: dict[str, str] = {}

    class SpyAdapter(LLMAdapter):
        def extract(self, redacted_message, context=None):
            captured["msg"] = redacted_message
            return super().extract(redacted_message, context)

    lead = make_lead(message="Chiamami al 3471234599 o scrivi a tizio@gmail.com")
    extract_features(lead, _VALID, SpyAdapter())
    assert "3471234599" not in captured["msg"]
    assert "tizio@gmail.com" not in captured["msg"]
    assert "[PHONE]" in captured["msg"] and "[EMAIL]" in captured["msg"]


def test_llm_error_falls_back_to_structured_default():
    class FailingAdapter(LLMAdapter):
        def extract(self, redacted_message, context=None):
            raise LLMError("boom")

    f = extract_features(make_lead(), _VALID, FailingAdapter())
    assert f.extraction_source == "fallback"
    assert f.low_confidence is True


def test_unexpected_error_also_falls_back():
    class CrashAdapter(LLMAdapter):
        def extract(self, redacted_message, context=None):
            raise RuntimeError("unexpected")

    f = extract_features(make_lead(), _VALID, CrashAdapter())
    assert f.extraction_source == "fallback"
