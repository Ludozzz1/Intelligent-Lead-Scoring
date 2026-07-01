"""Persistence for agent sessions (state is persisted; the agent is event-driven).

REFACTOR_SPEC §7.2: the agent is NOT request-response; it "wakes up" on events,
its state living in a store (DB/queue in prod). Here: an in-memory store (default)
and a JSON file store, both behind a tiny protocol.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from src.models.agent import AgentSession


@runtime_checkable
class SessionStore(Protocol):
    def save(self, session: AgentSession) -> None: ...
    def get(self, lead_id: str) -> AgentSession | None: ...
    def all(self) -> list[AgentSession]: ...


class InMemorySessionStore:
    """Default in-process session store."""

    def __init__(self) -> None:
        self._sessions: dict[str, AgentSession] = {}

    def save(self, session: AgentSession) -> None:
        self._sessions[session.lead_id] = session

    def get(self, lead_id: str) -> AgentSession | None:
        return self._sessions.get(lead_id)

    def all(self) -> list[AgentSession]:
        return list(self._sessions.values())


class FileSessionStore:
    """JSON-file session store (one file, lead_id -> serialized session)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("{}", encoding="utf-8")

    def _read(self) -> dict[str, dict]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def save(self, session: AgentSession) -> None:
        data = self._read()
        data[session.lead_id] = session.model_dump(mode="json")
        self._path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    def get(self, lead_id: str) -> AgentSession | None:
        raw = self._read().get(lead_id)
        return AgentSession.model_validate(raw) if raw else None

    def all(self) -> list[AgentSession]:
        return [AgentSession.model_validate(v) for v in self._read().values()]
