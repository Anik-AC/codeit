"""Ticket leases (PRD 12.4): at most one agent run per ticket at a time.

`acquire` is a single atomic upsert: it takes a free ticket, or one whose lease expired
(its holder crashed), and fails if another live run holds it.

Runs heartbeat every minute (`keep_alive`). A lease whose heartbeat stopped for a few
minutes belongs to a dead process, so the reaper does not have to wait for the full TTL
(ADR-0013).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from codeit.db.models import Lease

Clock = Callable[[], datetime]
HEARTBEAT_S = 60
# No heartbeat for this long: the holder is dead, whatever the TTL says.
STALE_AFTER = timedelta(minutes=3)


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class LeaseStore:
    def __init__(self, engine: Engine, clock: Clock = _now) -> None:
        self._engine = engine
        self._clock = clock

    def acquire(
        self, ticket_key: str, role: str, instance: str, run_id: str, ttl: timedelta
    ) -> bool:
        now = self._clock()
        values = {
            "ticket_key": ticket_key,
            "role": role,
            "instance": instance,
            "run_id": run_id,
            "acquired_at": now,
            "expires_at": now + ttl,
            "heartbeat_at": now,
        }
        stmt = insert(Lease).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=[Lease.ticket_key],
            set_={k: v for k, v in values.items() if k != "ticket_key"},
            where=Lease.expires_at <= now,
        )
        with Session(self._engine) as s, s.begin():
            result = s.execute(stmt)
            return int(result.rowcount) == 1  # type: ignore[attr-defined]

    def heartbeat(self, ticket_key: str, run_id: str, ttl: timedelta) -> bool:
        now = self._clock()
        with Session(self._engine) as s, s.begin():
            result = s.execute(
                update(Lease)
                .where(Lease.ticket_key == ticket_key, Lease.run_id == run_id)
                .values(heartbeat_at=now, expires_at=now + ttl)
            )
            return int(result.rowcount) == 1  # type: ignore[attr-defined]

    def release(self, ticket_key: str, run_id: str) -> None:
        with Session(self._engine) as s, s.begin():
            s.execute(delete(Lease).where(Lease.ticket_key == ticket_key, Lease.run_id == run_id))

    def holder(self, ticket_key: str) -> Lease | None:
        with Session(self._engine) as s:
            lease = s.get(Lease, ticket_key)
            if lease is None:
                return None
            return lease if _aware(lease.expires_at) > self._clock() else None

    def all(self) -> list[Lease]:
        with Session(self._engine) as s:
            return list(s.scalars(select(Lease).order_by(Lease.acquired_at)))

    def dead(self, stale_after: timedelta = STALE_AFTER) -> list[Lease]:
        """Leases that expired, or whose heartbeat stopped `stale_after` ago."""
        now = self._clock()
        with Session(self._engine) as s:
            return list(
                s.scalars(
                    select(Lease).where(
                        or_(Lease.expires_at <= now, Lease.heartbeat_at <= now - stale_after)
                    )
                )
            )

    @contextlib.asynccontextmanager
    async def keep_alive(
        self, ticket_key: str, run_id: str, ttl: timedelta, every_s: float = HEARTBEAT_S
    ) -> AsyncIterator[None]:
        """Heartbeat the lease every `every_s` seconds while the block runs."""

        async def beat() -> None:
            while True:
                await asyncio.sleep(every_s)
                self.heartbeat(ticket_key, run_id, ttl)

        task = asyncio.create_task(beat())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
