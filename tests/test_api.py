"""FastAPI endpoints: /score, /callback, /health."""

from __future__ import annotations

from fastapi.testclient import TestClient

from src.main import app

client = TestClient(app)

_LEAD = {
    "lead_id": "API-0001",
    "platform": "DriveK",
    "channel": "meta",
    "message": "Vorrei un SUV ibrido, budget 35k, permuto una Golf del 2018. Disponibile per test drive sabato mattina. Pensavo anche a un finanziamento.",
    "vehicle_interest": "Toyota C-HR",
    "city": "Milano",
    "zip_code": "20148",
    "phone": "3471234599",
    "email": "valid.customer@gmail.com",
    "campaign": "SUV Hybrid Q2",
    "created_at": "2026-06-28T10:20:00",
    "consent": True,
}


def test_score_returns_schema():
    resp = client.post("/score", json=_LEAD)
    assert resp.status_code == 200
    body = resp.json()
    for key in (
        "lead_id", "score", "category", "validity", "features",
        "score_result", "motivation", "recommended_action", "next_best_action",
        "queue", "agent_status", "priority", "agent_triggered", "low_confidence",
    ):
        assert key in body
    assert body["category"] in ("hot", "warm", "cold", "invalid")
    assert isinstance(body["score"], int)


def test_double_submit_sets_duplicate():
    payload = {**_LEAD, "lead_id": "API-DUP"}
    client.post("/score", json=payload)
    second = client.post("/score", json=payload).json()
    assert second["personalization"]["is_duplicate"] is True


def test_callback_acknowledges():
    scored = client.post("/score", json={**_LEAD, "lead_id": "API-CB"}).json()
    ack = client.post("/callback", json=scored).json()
    assert ack["status"] == "delivered"
    assert ack["lead_id"] == "API-CB"
    # Operator routing signals reach the monolith/Vue dashboard.
    for key in ("next_best_action", "queue", "agent_status"):
        assert key in ack["payload"]
    # No raw PII in the monolith payload.
    assert "phone" not in ack["payload"]
    assert "email" not in ack["payload"]


def test_health():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["llm_mode"] == "mock"
    assert body["history_loaded"] is True
    assert "version" in body
