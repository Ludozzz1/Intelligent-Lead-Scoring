"""Mocked ingestion queue boundary (maps to Amazon SQS + DLQ in production).

Substitutable in-process implementations behind a ``Queue`` Protocol so the
pipeline can be driven from a queue without a real broker. Idempotency/dedup is
enforced in the pipeline, not here; the queue only delivers items and isolates
poison messages into a dead-letter queue.
"""

from __future__ import annotations

import inspect
import json
import logging
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

Item = dict[str, Any]


@runtime_checkable
class Queue(Protocol):
    def publish(self, item: Item) -> None: ...
    def consume(self) -> Item | None: ...
    def empty(self) -> bool: ...


class InMemoryQueue:
    """Simple in-process FIFO queue. Default for tests and the demo."""

    def __init__(self, items: list[Item] | None = None) -> None:
        self._items: deque[Item] = deque(items or [])

    def publish(self, item: Item) -> None:
        self._items.append(item)

    def consume(self) -> Item | None:
        return self._items.popleft() if self._items else None

    def empty(self) -> bool:
        return len(self._items) == 0

    def __len__(self) -> int:
        return len(self._items)


class FileQueue:
    """File-backed FIFO queue (JSON lines). Durable boundary demo, not a broker."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text("", encoding="utf-8")

    def _read_all(self) -> list[Item]:
        items: list[Item] = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                items.append(json.loads(line))
        return items

    def _write_all(self, items: list[Item]) -> None:
        lines = [json.dumps(i, ensure_ascii=False, default=str) for i in items]
        self._path.write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8"
        )

    def publish(self, item: Item) -> None:
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, default=str) + "\n")

    def consume(self) -> Item | None:
        items = self._read_all()
        if not items:
            return None
        head = items.pop(0)
        self._write_all(items)
        return head

    def empty(self) -> bool:
        return not self._read_all()


class DeadLetterQueue:
    """Holds poison items that failed processing, with the failure reason."""

    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(self, item: Item, reason: str) -> None:
        logger.warning("dead-lettering item: %s", reason)
        self.items.append({"item": item, "reason": reason})

    def empty(self) -> bool:
        return len(self.items) == 0

    def __len__(self) -> int:
        return len(self.items)


Handler = Callable[[Item], Any] | Callable[[Item], Awaitable[Any]]


async def consume_all(
    queue: Queue, handler: Handler, dlq: DeadLetterQueue | None = None
) -> int:
    """Drain ``queue`` applying ``handler``; failed items go to the DLQ."""
    processed = 0
    while not queue.empty():
        item = queue.consume()
        if item is None:
            break
        try:
            result = handler(item)
            if inspect.isawaitable(result):
                await result
            processed += 1
        except Exception as exc:  # noqa: BLE001 - boundary: never crash the drain
            logger.exception("handler failed for item")
            if dlq is not None:
                dlq.add(item, reason=str(exc))
    return processed
