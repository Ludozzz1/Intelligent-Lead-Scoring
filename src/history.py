"""Historical-dataset service: runtime dedup + personalization ONLY.

REFACTOR_SPEC §11 forbids using the history as a runtime scoring input
("storico usato nel runtime"). So at runtime this service does exactly two
allowed things, both keyed by exact contact match:

  * duplicate / idempotency detection (``find_duplicate``);
  * returning-customer personalization (``personalize``).

The per-stratum conversion/invalid priors that the legacy version queried live
have been removed. The outcome labels in ``data/leads_history.json`` remain, but
they are consumed only OFFLINE by the (documented, not implemented) calibration
pipeline (docs/calibration.md) -- never here.

Pure, deterministic, rules-only. Never raises on lookup.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from src.config import Settings, get_settings
from src.models.lead import Lead
from src.models.scoring import Personalization
from src.privacy import email_key, phone_key


@dataclass(frozen=True)
class DuplicateResult:
    """Result of a duplicate lookup over the history."""

    is_duplicate: bool = False
    prior_leads_count: int = 0
    last_seen_at: datetime | None = None
    matched_lead_ids: list[str] = field(default_factory=list)


def _parse_dt(value: Any) -> datetime | None:
    """Parse an ISO-8601 datetime defensively; return None on any failure."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (ValueError, TypeError):
        return None


class HistoryService:
    """In-memory dedup/personalization view over the historical leads.

    Construct once and reuse: the dedup indices are built eagerly in
    ``__init__`` from the configured ``leads_history_path``.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()

        self._records: list[dict[str, Any]] = []
        # Dedup indices: canonical key -> list of records (load order).
        self._by_phone: dict[str, list[dict[str, Any]]] = {}
        self._by_email: dict[str, list[dict[str, Any]]] = {}

        self._load()

    # -- loading & indexing --------------------------------------------------

    def _load(self) -> None:
        path: Path | None = self._settings.leads_history_path
        if path is None or not Path(path).exists():
            return
        try:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, list):
            return

        for rec in raw:
            if isinstance(rec, dict):
                self._index_record(rec)

    def _index_record(self, rec: dict[str, Any]) -> None:
        self._records.append(rec)
        pk = rec.get("phone_key")
        if pk:
            self._by_phone.setdefault(pk, []).append(rec)
        ek = rec.get("email_key")
        if ek:
            self._by_email.setdefault(ek, []).append(rec)

    @property
    def record_count(self) -> int:
        """Number of historical records loaded."""
        return len(self._records)

    # -- public: dedup / personalization ------------------------------------

    def find_duplicate(
        self,
        phone: str | None,
        email: str | None,
        within_days: int | None = None,
    ) -> DuplicateResult:
        """Look up prior leads matching this contact's phone or email.

        Matching uses the canonical ``phone_key`` / ``email_key`` so it lines up
        with the stored keys. ``within_days`` (defaults to the configured dedup
        window) bounds the recency window for ``is_duplicate``;
        ``prior_leads_count`` always counts all matches.
        """
        window = (
            self._settings.dedup_window_days if within_days is None else within_days
        )
        matched = self._matching_records(phone, email)
        if not matched:
            return DuplicateResult()

        matched_ids = [r.get("lead_id", "") for r in matched]
        last_seen = _max_created_at(matched)
        recent = _within_window(matched, window) if window and window > 0 else matched

        return DuplicateResult(
            is_duplicate=len(recent) > 0,
            prior_leads_count=len(matched),
            last_seen_at=last_seen,
            matched_lead_ids=matched_ids,
        )

    def personalize(self, lead: Lead) -> Personalization:
        """Re-read the history for this lead and produce its Personalization.

        ``is_returning_customer`` is True when any matched prior record had a
        converted outcome. Notes are Italian (operator-facing).
        """
        matched = self._matching_records(lead.phone, lead.email)
        if not matched:
            return Personalization(history_notes="Nessun contatto storico trovato.")

        dup = self.find_duplicate(lead.phone, lead.email)
        is_returning = any((r.get("outcome") or {}).get("converted") for r in matched)
        notes = self._build_notes(
            prior=len(matched),
            is_duplicate=dup.is_duplicate,
            is_returning=is_returning,
            last_seen=dup.last_seen_at,
        )
        return Personalization(
            is_duplicate=dup.is_duplicate,
            is_returning_customer=is_returning,
            prior_leads_count=len(matched),
            last_seen_at=dup.last_seen_at,
            history_notes=notes,
        )

    # -- internals -----------------------------------------------------------

    def _matching_records(
        self, phone: str | None, email: str | None
    ) -> list[dict[str, Any]]:
        """Union of records matching by canonical phone key or email key."""
        pk = phone_key(phone)
        ek = email_key(email)

        seen: set[int] = set()
        out: list[dict[str, Any]] = []
        for rec in (self._by_phone.get(pk, []) if pk else []) + (
            self._by_email.get(ek, []) if ek else []
        ):
            if id(rec) in seen:
                continue
            seen.add(id(rec))
            out.append(rec)
        return out

    @staticmethod
    def _build_notes(
        prior: int,
        is_duplicate: bool,
        is_returning: bool,
        last_seen: datetime | None,
    ) -> str:
        parts: list[str] = []
        if is_returning:
            parts.append("Cliente di ritorno (conversione passata).")
        elif is_duplicate:
            parts.append("Lead duplicato recente.")
        seen_txt = (
            f", ultimo contatto {last_seen.date().isoformat()}" if last_seen else ""
        )
        parts.append(f"{prior} contatto/i storico/i{seen_txt}.")
        return " ".join(parts)


def _max_created_at(records: list[dict[str, Any]]) -> datetime | None:
    """Most recent ``created_at`` among records (None if none parse)."""
    dts = [dt for dt in (_parse_dt(r.get("created_at")) for r in records) if dt]
    return max(dts) if dts else None


def _within_window(
    records: list[dict[str, Any]], within_days: int
) -> list[dict[str, Any]]:
    """Subset of records within ``within_days`` of the most recent matched one."""
    last = _max_created_at(records)
    if last is None:
        return []
    cutoff = last - timedelta(days=within_days)
    return [
        r
        for r in records
        if (dt := _parse_dt(r.get("created_at"))) is not None and dt >= cutoff
    ]
