"""Mocked REST callback to the Java monolith.

After scoring, the service writes the result back to the monolith (which
populates score/priority/motivation on the record shown in the Vue dashboard).
In production this is an HTTP POST to a Java endpoint; here a substitutable mock
that records each callback in memory (only minimal, non-PII fields).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from src.models.output import ScoredLead

logger = logging.getLogger(__name__)


@runtime_checkable
class MonolithCallback(Protocol):
    def send_score(self, scored_lead: ScoredLead) -> dict[str, Any]: ...
    def send_agent_outcome(
        self, lead_id: str, outcome: str, note: str = ""
    ) -> dict[str, Any]: ...


class MockMonolithCallback:
    """In-memory mock of the monolith REST callback."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.agent_outcomes: list[dict[str, Any]] = []

    def send_score(self, scored_lead: ScoredLead) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "lead_id": scored_lead.lead_id,
            "score": scored_lead.score,
            "category": scored_lead.category,
            "priority": scored_lead.priority,
            "motivation": scored_lead.motivation,
            "recommended_action": scored_lead.recommended_action,
            # Operator-facing next-best-action + queue routing (Vue dashboard).
            "next_best_action": scored_lead.next_best_action,
            "queue": scored_lead.queue,
            "agent_status": scored_lead.agent_status,
            "low_confidence": scored_lead.low_confidence,
        }
        ack: dict[str, Any] = {
            "status": "delivered",
            "lead_id": scored_lead.lead_id,
            "payload": payload,
        }
        self.sent.append(ack)
        logger.info(
            "monolith callback: lead_id=%s category=%s score=%d priority=%d",
            scored_lead.lead_id,
            scored_lead.category,
            scored_lead.score,
            scored_lead.priority,
        )
        return ack

    def send_agent_outcome(
        self, lead_id: str, outcome: str, note: str = ""
    ) -> dict[str, Any]:
        """Write back an agent-session outcome (CRM writeback, non-PII).

        The hot-path callback (:meth:`send_score`) reports only the score; this
        carries the *resolution* of the async agent (booked / completed_info /
        handoff / ...) so the monolith/dashboard reflects the full lifecycle.
        """
        ack: dict[str, Any] = {
            "status": "delivered",
            "lead_id": lead_id,
            "payload": {"lead_id": lead_id, "agent_outcome": outcome, "note": note},
        }
        self.agent_outcomes.append(ack)
        logger.info("monolith agent outcome: lead_id=%s outcome=%s", lead_id, outcome)
        return ack
