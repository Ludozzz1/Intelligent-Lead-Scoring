"""Mocked outbound messaging channels (WhatsApp / SMS / email).

In production these are real providers (Twilio, SES); here a substitutable mock.
Privacy-by-design: recipients are passed as opaque tokens (never raw PII). The
mock records sent messages in memory and NEVER logs raw PII.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

SUPPORTED_CHANNELS = ("whatsapp", "sms", "email")


@runtime_checkable
class Channel(Protocol):
    """Contract for sending a templated message to a tokenized recipient."""

    def send(self, channel: str, template: str, to_token: str) -> dict[str, Any]:
        ...


class MockChannel:
    """In-memory mock messaging channel (deterministic message ids)."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    @staticmethod
    def _message_id(channel: str, template: str, to_token: str) -> str:
        digest = hashlib.sha256(
            f"{channel}|{template}|{to_token}".encode("utf-8")
        ).hexdigest()
        return f"msg_{digest[:16]}"

    def send(self, channel: str, template: str, to_token: str) -> dict[str, Any]:
        normalized = (channel or "").strip().lower()
        status = "sent" if normalized in SUPPORTED_CHANNELS else "skipped"
        record: dict[str, Any] = {
            "message_id": self._message_id(normalized, template, to_token),
            "channel": normalized,
            "template": template,
            "to_token": to_token,
            "status": status,
        }
        self.sent.append(record)
        logger.info(
            "channel send: channel=%s template=%s to_token=%s status=%s msg=%s",
            normalized, template, to_token, status, record["message_id"],
        )
        return record
