"""Validation gate: deterministic, structural, NO LLM, fail-fast (§5.1).

Per REFACTOR_SPEC §5.1 the gate is BINARY (passed / invalid) and purely
structural. It only does cheap, unambiguous checks plus a tiny externalized
blocklist; it never inspects message semantics -- "is this spam/gibberish/out of
area?" is the LLM's ``ExtractedFeatures.looks_invalid`` (§5.2), applied after the
gate. Consent is NOT a gate check: it gates the agent's outbound messaging
(§7.5), not scoreability.

A lead is INVALID when it has positive structural fake evidence (bogus phone or
disposable email) OR no reachable contact channel at all. Otherwise it is VALID.

CONSERVATIVE INVALIDATION: a false-invalid (discarding a real buyer) is the
costliest error; uncertain signals never invalidate. Dedup is reported but NEVER
invalidates (a returning customer is high value).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from src.models.lead import Lead
from src.models.scoring import ValidityResult
from src.scoring.weights import load_blocklists

if TYPE_CHECKING:
    from src.config import Settings
    from src.history import HistoryService

_MOBILE_RE = re.compile(r"^3\d{8,9}$")
_ASCENDING = "0123456789"
_DESCENDING = "9876543210"

_EMAIL_RE = re.compile(
    r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$"
)
_EMAIL_DOUBLE_DOT_RE = re.compile(r"\.\.")


def _normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return None
    if digits.startswith("0039"):
        digits = digits[4:]
    elif digits.startswith("39") and len(digits) > 10:
        digits = digits[2:]
    return digits or None


def _is_bogus_phone(national: str) -> bool:
    """Algorithmic (not list-based) structural bogus-phone detection."""
    if len(set(national)) == 1:
        return True
    if national in _ASCENDING or national in _ASCENDING[::-1]:
        return True
    if national in _DESCENDING or national in _DESCENDING[::-1]:
        return True
    if len(national) >= 8 and (
        national in _ASCENDING * 2 or national in _DESCENDING * 2
    ):
        return True
    return False


def _email_domain(email: str) -> str:
    _, _, domain = email.strip().lower().partition("@")
    return domain


def _is_valid_email_format(email: str) -> bool:
    candidate = email.strip()
    if _EMAIL_DOUBLE_DOT_RE.search(candidate):
        return False
    return bool(_EMAIL_RE.match(candidate))


def evaluate_validity(
    lead: Lead,
    history: HistoryService | None = None,
    settings: Settings | None = None,
) -> ValidityResult:
    """Run the structural validation gate on one lead (binary, deterministic)."""
    disposable_domains = set(
        load_blocklists(settings).get("disposable_email_domains", [])
    )

    invalid_reasons: list[str] = []
    missing_fields: list[str] = []

    # -- Phone ---------------------------------------------------------------
    national = _normalize_phone(lead.phone)
    has_valid_mobile = False
    has_valid_landline = False
    phone_bogus = False
    if national is None:
        missing_fields.append("phone")
    elif _is_bogus_phone(national):
        phone_bogus = True
        invalid_reasons.append("phone_bogus")
    elif _MOBILE_RE.match(national):
        has_valid_mobile = True
    elif national.startswith("0") and 6 <= len(national) <= 11:
        has_valid_landline = True
    # else: present but unusable number -> simply not a reachable channel.

    # -- Email ---------------------------------------------------------------
    has_valid_email = False
    email_disposable = False
    if not (lead.email and lead.email.strip()):
        missing_fields.append("email")
    else:
        email = lead.email.strip()
        if not _is_valid_email_format(email):
            pass  # malformed -> just not a reachable channel (not fake evidence)
        elif _email_domain(email) in disposable_domains:
            email_disposable = True
            invalid_reasons.append("email_disposable")
        else:
            has_valid_email = True

    reachable = has_valid_mobile or has_valid_landline or has_valid_email

    # -- Dedup (NEVER invalidates) -------------------------------------------
    dup_reason: str | None = None
    if history is not None:
        try:
            dup = history.find_duplicate(lead.phone, lead.email)
        except Exception:  # noqa: BLE001 - dedup must never break the gate
            dup = None
        if dup is not None and dup.is_duplicate:
            dup_reason = f"duplicate_prior_leads={dup.prior_leads_count}"

    # -- Decision ------------------------------------------------------------
    fake_evidence = phone_bogus or email_disposable
    if fake_evidence or not reachable:
        reasons = list(invalid_reasons)
        if not reachable and "no_reachable_contact" not in reasons:
            reasons.append("no_reachable_contact")
        if dup_reason:
            reasons.append(dup_reason)
        seen: set[str] = set()
        ordered_missing = [f for f in missing_fields if not (f in seen or seen.add(f))]
        return ValidityResult(
            is_valid=False,
            failure_type="invalid",
            reasons=reasons,
            missing_fields=ordered_missing,
        )

    reasons = ["contact_ok"]
    if has_valid_mobile:
        reasons.append("mobile_ok")
    if has_valid_landline:
        reasons.append("landline_ok")
    if has_valid_email:
        reasons.append("email_ok")
    if dup_reason:
        reasons.append(dup_reason)

    return ValidityResult(
        is_valid=True, failure_type="none", reasons=reasons, missing_fields=[]
    )
