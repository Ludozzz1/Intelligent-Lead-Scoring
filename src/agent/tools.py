"""Mocked agent tools (REFACTOR_SPEC §7.3).

Thin wrappers over the mocked integrations + the extraction adapter. Tools only
*execute*; the decision-rights gating (consent, human-approval) and guardrails
live in :mod:`src.agent.guardrails` / the state machine. Recipients are always
opaque tokens (never raw PII). ``mark_invalid`` is intentionally absent: the
agent never disqualifies a lead for quality (that stays the deterministic gate).
"""

from __future__ import annotations

import hashlib
from typing import Any

from src.extraction.llm import LLMAdapter
from src.integrations.calendar import Calendar, MockCalendar
from src.integrations.channels import Channel, MockChannel
from src.integrations.financing import FinancingSimulator, MockFinancing
from src.integrations.inventory import Inventory, MockInventory
from src.integrations.monolith_callback import MockMonolithCallback, MonolithCallback
from src.integrations.scheduler import MockScheduler, Scheduler
from src.integrations.trade_in import MockTradeIn, TradeInEstimator
from src.models.lead import ExtractedFeatures
from src.privacy import redact_message

DEFAULT_DEALER_ID = "DEALER-MI-01"


def _ticket_id(*parts: str) -> str:
    """Deterministic ticket reference (SHA-256, like the other mocks).

    Builtin ``hash()`` is salted by ``PYTHONHASHSEED`` and changes across process
    runs, which would make escalation/handoff references non-reproducible in the
    audit trail. SHA-256 keeps them stable.
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"TICKET-{digest[:10]}"


class AgentTools:
    """The agent's tool belt, bound to (mock) external clients."""

    def __init__(
        self,
        channels: Channel | None = None,
        calendar: Calendar | None = None,
        inventory: Inventory | None = None,
        trade_in: TradeInEstimator | None = None,
        adapter: LLMAdapter | None = None,
        financing: FinancingSimulator | None = None,
        scheduler: Scheduler | None = None,
        monolith: MonolithCallback | None = None,
        dealer_id: str = DEFAULT_DEALER_ID,
    ) -> None:
        self.channels = channels or MockChannel()
        self.calendar = calendar or MockCalendar()
        self.inventory = inventory or MockInventory()
        self.trade_in = trade_in or MockTradeIn()
        self.adapter = adapter or LLMAdapter()
        self.financing = financing or MockFinancing()
        self.scheduler = scheduler or MockScheduler()
        self.monolith = monolith or MockMonolithCallback()
        self.dealer_id = dealer_id

    # -- §7.3 tools ----------------------------------------------------------

    def re_extract(
        self, message: str | None, reply_context: dict | None = None
    ) -> ExtractedFeatures:
        """Re-analyze the user's reply (PII-redacted) into ExtractedFeatures.

        ``reply_context`` (vehicle + fields asked) lets the extractor read a short
        reply as the answer to that question instead of in a vacuum (§7.2).
        """
        redacted = redact_message(message)
        return self.adapter.extract(redacted, reply_context=reply_context)

    def check_availability(self, preferences: dict | None = None) -> list[str]:
        return self.calendar.check_availability(self.dealer_id, preferences or {})

    def estimate_trade_in(self, vehicle_desc: str | None) -> dict[str, Any]:
        return self.trade_in.estimate(vehicle_desc)

    def check_inventory(self, vehicle: str | None) -> dict[str, Any]:
        return self.inventory.check(vehicle)

    def recommend_alternatives(
        self, vehicle: str | None, budget: float | None = None
    ) -> dict[str, Any]:
        """Equivalent in-catchment alternatives when a model is out of stock."""
        return self.inventory.recommend_alternatives(vehicle, budget)

    def simulate_financing(
        self,
        price: float | None,
        down_payment: float | None = None,
        trade_in_value: float | None = None,
    ) -> dict[str, Any]:
        """Indicative monthly instalment to qualify a budget-conscious lead."""
        return self.financing.simulate(price, down_payment, trade_in_value)

    def send_message(
        self, channel: str | None, template: str, to_token: str | None, text: str = ""
    ) -> dict[str, Any]:
        if not to_token:
            return {"sent": False, "reason": "no_recipient", "template": template}
        result = self.channels.send(channel or "sms", template, to_token)
        return {
            "sent": result.get("status") == "sent",
            "message_id": result.get("message_id"),
            "channel": result.get("channel"),
            "template": template,
        }

    def send_asset(
        self,
        channel: str | None,
        vehicle: str | None,
        asset_type: str,
        to_token: str | None,
    ) -> dict[str, Any]:
        """Send a vehicle sheet / price list / configurator link (consent-gated)."""
        out = self.send_message(channel, f"asset_{asset_type}", to_token)
        out["vehicle"] = vehicle
        out["asset_type"] = asset_type
        return out

    def capture_consent(
        self, channel: str | None, to_token: str | None
    ) -> dict[str, Any]:
        """Send a GDPR double opt-in request to obtain messaging consent."""
        return self.send_message(channel, "consent_double_optin", to_token)

    def schedule_followup(self, lead_id: str, when: str) -> dict[str, Any]:
        """Schedule a deferred follow-up (non-responder ladder)."""
        return self.scheduler.schedule(lead_id, when)

    def book_appointment(self, slot: str, lead_id: str) -> dict[str, Any]:
        return self.calendar.book(self.dealer_id, slot, lead_id)

    def update_crm(self, lead_id: str, outcome: str, note: str = "") -> dict[str, Any]:
        """Write the agent-session outcome back to the CRM/monolith (non-PII)."""
        return self.monolith.send_agent_outcome(lead_id, outcome, note)

    def warm_transfer_to_operator(
        self, lead_id: str, context: str = ""
    ) -> dict[str, Any]:
        """Hand a lead to a human operator with full context (rich handoff)."""
        ticket = _ticket_id("transfer", lead_id, context)
        return {"ticket_id": ticket, "lead_id": lead_id, "context": context,
                "transferred": True}

    def escalate_to_human(self, reason: str, lead_id: str) -> dict[str, Any]:
        ticket = _ticket_id("escalate", reason, lead_id)
        return {"ticket_id": ticket, "reason": reason, "lead_id": lead_id}
