"""Persistent backend state (PRD 9.4, 13 `backend_state`): parked until a reset time.

Parking survives restarts, so a usage-limited backend is not retried the moment the
orchestrator comes back up.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from codeit.backends.base import Availability
from codeit.db.models import BackendState


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class BackendStateStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def park(self, backend: str, until: datetime, error: str) -> None:
        with Session(self._engine) as s, s.begin():
            s.merge(
                BackendState(
                    backend=backend, state="parked", parked_until=until, last_error=error[:2000]
                )
            )

    def clear(self, backend: str) -> None:
        with Session(self._engine) as s, s.begin():
            s.merge(BackendState(backend=backend, state="ok", parked_until=None, last_error=None))

    def get(self, backend: str, now: datetime | None = None) -> Availability:
        now = now or datetime.now(UTC)
        with Session(self._engine) as s:
            row = s.get(BackendState, backend)
            if row is None or row.state == "ok":
                return Availability("ok")
            until = _aware(row.parked_until)
            if row.state == "parked" and until is not None and until <= now:
                return Availability("ok")
            return Availability(row.state, until, row.last_error or "")  # type: ignore[arg-type]
