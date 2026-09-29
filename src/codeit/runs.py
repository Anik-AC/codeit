"""The `runs` table (PRD 13): one row per agent run, written at start and at the end."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from codeit.backends.base import AgenticResult
from codeit.db.models import Run


def record_run(engine: Engine, run_id: str, role: str, **fields: Any) -> None:
    """Create the run row (status `running`) if missing, then set `fields`."""
    with Session(engine) as s, s.begin():
        run = s.get(Run, run_id)
        if run is None:
            run = Run(
                id=run_id, role=role, instance="", started_at=datetime.now(UTC), status="running"
            )
            s.add(run)
        for name, value in fields.items():
            setattr(run, name, value)


def previous_run_start(engine: Engine, role: str, key: str, run_id: str) -> datetime | None:
    """When the latest earlier run of `role` on ticket `key` started."""
    with Session(engine) as s:
        started = s.scalars(
            select(Run.started_at)
            .where(Run.role == role, Run.ticket_key == key, Run.id != run_id)
            .order_by(Run.started_at.desc())
            .limit(1)
        ).first()
    if started is None:
        return None
    return started if started.tzinfo else started.replace(tzinfo=UTC)


def finish_run(
    engine: Engine,
    run_id: str,
    role: str,
    fields: dict[str, Any],
    agent: AgenticResult | None = None,
) -> None:
    """Close the run: end time, outcome fields, and the agent's usage if it ran."""
    values: dict[str, Any] = {"ended_at": datetime.now(UTC), **fields}
    if agent is not None:
        values.update(
            backend="claude_code",
            model=agent.model,
            turns=agent.turns,
            input_tokens=agent.input_tokens,
            output_tokens=agent.output_tokens,
            cost_usd=agent.cost_usd,
            transcript_path=agent.transcript_path,
        )
    record_run(engine, run_id, role, **values)
