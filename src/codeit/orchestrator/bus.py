"""In-process event bus for the dashboard's live stream (PRD 14 `/api/stream`).

Publishers never block: each subscriber has a bounded queue, and a subscriber that falls
behind loses its oldest events rather than slowing the orchestrator down. Event types:
`agent_state`, `run_event`, `ticket_update`, `budget_update`.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

QUEUE_SIZE = 500


@dataclass(frozen=True)
class BusEvent:
    type: str
    data: Any


class EventBus:
    def __init__(self, queue_size: int = QUEUE_SIZE) -> None:
        self._subscribers: set[asyncio.Queue[BusEvent]] = set()
        self._queue_size = queue_size

    def publish(self, type_: str, data: Any) -> None:
        event = BusEvent(type_, data)
        for q in self._subscribers:
            if q.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    q.get_nowait()
            q.put_nowait(event)

    @contextlib.asynccontextmanager
    async def subscribe(self) -> AsyncIterator[asyncio.Queue[BusEvent]]:
        q: asyncio.Queue[BusEvent] = asyncio.Queue(self._queue_size)
        self._subscribers.add(q)
        try:
            yield q
        finally:
            self._subscribers.discard(q)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)
