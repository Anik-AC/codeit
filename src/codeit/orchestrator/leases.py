"""Ticket leases (PRD 12.4): at most one agent run per ticket at a time.

`acquire` is a single atomic upsert: it takes a free ticket, or one whose lease expired
(its holder crashed), and fails if another live run holds it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session

from codeit.db.models import Lease

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


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
            expires = (
                lease.expires_at
                if lease.expires_at.tzinfo
                else lease.expires_at.replace(tzinfo=UTC)
            )
            return lease if expires > self._clock() else None
