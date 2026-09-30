"""The `events` table (PRD 13, 18): what the orchestrator did, for the dashboard (M8)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from codeit.db.models import Event, Run


def record_event(
    engine: Engine, type_: str, payload: dict[str, Any], run_id: str | None = None
) -> None:
    """`type_`: transition | comment | budget | error | ... (PRD 13). A `run_id` that has no
    `runs` row (a run refused before it started) is dropped from the event."""
    with Session(engine) as s, s.begin():
        if run_id is not None and s.get(Run, run_id) is None:
            payload = {**payload, "run_id": run_id}
            run_id = None
        s.add(Event(run_id=run_id, ts=datetime.now(UTC), type=type_, payload_json=payload))
