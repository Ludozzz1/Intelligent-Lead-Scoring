"""Shared pytest fixtures for the lead-scoring test suite.

All tests run deterministically with NO API key: the LLM adapter stays in mock
mode (fixture map), so scores/categories are reproducible. ``NOW`` pins the
reference time so the ``recency`` feature is stable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from src.config import Settings
from src.models.lead import Lead
from src.privacy import email_key, phone_key

# Fixed reference time for deterministic recency.
NOW = datetime(2026, 6, 29, 12, 0, 0)

# A reachable, consented, fully-valid lead. Its message matches a fixture entry
# (LEAD-0001) so the mock extraction yields rich, hot signals.
_VALID_PHONE = "3471234599"
_VALID_EMAIL = "valid.customer@gmail.com"
_RICH_MESSAGE = (
    "Vorrei un SUV ibrido, budget 35k, permuto una Golf del 2018. "
    "Disponibile per test drive sabato mattina. Pensavo anche a un finanziamento."
)


def make_lead(**overrides: Any) -> Lead:
    """Build a baseline valid Lead, overriding any field by keyword."""
    base: dict[str, Any] = {
        "lead_id": "TEST-0001",
        "platform": "DriveK",
        "channel": "meta",
        "message": _RICH_MESSAGE,
        "vehicle_interest": "Toyota C-HR",
        "city": "Milano",
        "zip_code": "20148",
        "phone": _VALID_PHONE,
        "name": "Mario",
        "surname": "Rossi",
        "email": _VALID_EMAIL,
        "campaign": "SUV Hybrid Q2",
        "created_at": datetime(2026, 6, 28, 10, 20, 0),
        "consent": True,
    }
    base.update(overrides)
    return Lead(**base)


def _history_record(
    lead_id: str,
    *,
    phone: str | None = None,
    email: str | None = None,
    created_at: str = "2026-06-01T10:00:00",
    converted: bool = False,
    qualified: bool = False,
) -> dict[str, Any]:
    return {
        "lead_id": lead_id,
        "channel": "meta",
        "platform": "DriveK",
        "campaign": "SUV Hybrid Q2",
        "created_at": created_at,
        "phone_key": phone_key(phone),
        "email_key": email_key(email),
        "outcome": {"validity": "valid", "qualified": qualified, "converted": converted},
    }


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def baseline_lead() -> Lead:
    return make_lead()


@pytest.fixture
def real_settings() -> Settings:
    """Settings pointing at the committed data + config artifacts."""
    return Settings()


@pytest.fixture
def tmp_history_path(tmp_path: Path) -> Path:
    """Controlled history dataset: a returning customer + filler records."""
    records: list[dict[str, Any]] = [
        _history_record(
            "HIST-RET-0001",
            phone=_VALID_PHONE,
            email=_VALID_EMAIL,
            created_at="2026-05-20T10:00:00",
            converted=True,
            qualified=True,
        )
    ]
    for i in range(5):
        records.append(_history_record(f"HIST-{i:04d}", phone=f"34800000{i:03d}"))
    path = tmp_path / "history.json"
    path.write_text(json.dumps(records), encoding="utf-8")
    return path


@pytest.fixture
def tmp_history_settings(tmp_history_path: Path, tmp_path: Path) -> Settings:
    """Settings whose history points at the controlled dataset."""
    return Settings(
        leads_history_path=tmp_history_path,
        leads_mock_path=tmp_path / "leads_mock.json",
    )
