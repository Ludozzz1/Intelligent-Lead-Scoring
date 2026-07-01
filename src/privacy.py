"""Deterministic PII handling, applied BEFORE any LLM call.

Privacy-by-design enforcement and the *canonical* implementation of the dedup
keys used across the codebase:

  * ``redact_message`` tokenizes phones / emails / obvious person names in the
    free-text ``message`` so the redacted text still reads naturally for the
    LLM extraction, while no raw PII ever leaves the process toward the LLM.
  * ``safe_fields_for_llm`` exposes ONLY a whitelist of non-PII fields.
  * ``phone_key`` / ``email_key`` produce stable SHA-256 dedup keys (matching
    those stored in ``data/leads_history.json``).
  * ``assert_no_raw_pii`` raises if a raw phone/email survives redaction.

Pure standard library (``re`` + ``hashlib``); fully deterministic.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

PHONE_TOKEN = "[PHONE]"
EMAIL_TOKEN = "[EMAIL]"
NAME_TOKEN = "[NAME]"

# --- Detection patterns -----------------------------------------------------

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")

_PHONE_RE = re.compile(
    r"""
    (?<![\w.])
    (?:\+39|0039|\+\d{1,3})?
    [\s.\-]?
    (?:\d[\s.\-]?){8,13}\d
    (?![\w])
    """,
    re.VERBOSE,
)

_NAME_INTRO_RE = re.compile(
    r"""
    \b(
        (?i:mi\s+chiamo|sono(?:\s+il|\s+la)?|nome|chiamatemi)
    )
    \s+
    (
        [A-ZÀ-Þ][a-zà-ÿ']{1,20}
        (?:\s+[A-ZÀ-Þ][a-zà-ÿ']{1,20})?
    )
    """,
    re.VERBOSE,
)

_RAW_EMAIL_GUARD = _EMAIL_RE
_RAW_PHONE_GUARD = re.compile(r"(?:\+39|0039)?[\s.\-]?(?:\d[\s.\-]?){8,13}\d")

# Whitelist of lead attributes exposed to the LLM. Strictly non-PII.
_SAFE_FIELD_NAMES = ("channel", "platform", "campaign", "vehicle_interest", "city")


def redact_message(text: str | None) -> str:
    """Replace phones, emails and obvious person names with placeholder tokens.

    Order matters: emails first (they embed dot/digit sequences a phone pattern
    could grab), then phones, then introduced names. Deterministic and pure.
    """
    if not text:
        return ""

    redacted = _EMAIL_RE.sub(EMAIL_TOKEN, text)
    redacted = _PHONE_RE.sub(PHONE_TOKEN, redacted)
    redacted = _NAME_INTRO_RE.sub(lambda m: f"{m.group(1)} {NAME_TOKEN}", redacted)
    return redacted


def safe_fields_for_llm(lead: Any) -> dict[str, Any]:
    """Return ONLY a whitelist of non-PII fields for LLM consumption.

    Never includes phone, email, name or surname. The ZIP is reduced to its
    first 3 digits (catchment area, not a precise locator).
    """
    safe: dict[str, Any] = {}
    for field in _SAFE_FIELD_NAMES:
        value = getattr(lead, field, None)
        if value not in (None, ""):
            safe[field] = value

    zip_code = getattr(lead, "zip_code", None)
    if zip_code:
        digits = re.sub(r"\D", "", str(zip_code))
        if digits:
            safe["zip_prefix"] = digits[:3]

    return safe


def phone_key(phone: str | None) -> str | None:
    """Canonical dedup key for a phone (SHA-256 of the national number)."""
    if not phone:
        return None

    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None

    if digits.startswith("0039"):
        digits = digits[4:]
    elif digits.startswith("39") and len(digits) > 10:
        digits = digits[2:]

    if not digits:
        return None

    return hashlib.sha256(digits.encode("utf-8")).hexdigest()


def email_key(email: str | None) -> str | None:
    """Canonical dedup key for an email (SHA-256 of the normalized address)."""
    if not email:
        return None

    normalized = email.strip().lower()
    if not normalized:
        return None

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def assert_no_raw_pii(text: str) -> None:
    """Raise ``ValueError`` if a raw phone or email remains in ``text``.

    Defensive last line of defense before handing text to the LLM adapter.
    """
    if not text:
        return

    if _RAW_EMAIL_GUARD.search(text):
        raise ValueError("Raw email PII detected in text destined for the LLM.")
    if _RAW_PHONE_GUARD.search(text):
        raise ValueError("Raw phone PII detected in text destined for the LLM.")
