"""Crash recovery (PRD 12.4, 6.2): runs whose process died.

A run is dead when its lease expired or its heartbeat stopped (`LeaseStore.dead`), or, at
startup, when its ticket is still `In Dev` with no lease and its last Coder run never
finished (the process was stopped mid-run). For each dead run the reaper:

1. marks the run `abandoned`, removes its container and revokes its jira-mcp token
2. releases the lease
3. moves the ticket per PRD 6.2:
   - Coder, ticket still `In Dev`: back to `Ready for Dev`; from the 2nd abandoned run on
     the ticket, to `Human Review` with `needs-human`. Runs stopped by a clean shutdown
     (`interrupted`) are not counted.
   - Reviewer, ticket still `Agent Review`: left for the next review; from the 2nd
     abandoned review, to `Human Review` with `needs-human`

A ticket a human moved in the meantime is left alone (PRD 12.5).
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine, func, select, update
from sqlalchemy.orm import Session

from codeit.agents.coder import NEEDS_HUMAN, orchestrator_comment
from codeit.db.models import Run
from codeit.events import record_event
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment
from codeit.jira_client.issues import add_labels, get_ticket
from codeit.jira_client.search import search_tickets
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.log import get_logger
from codeit.orchestrator.leases import LeaseStore
from codeit.run_tokens import RunTokenStore

log = get_logger(__name__)

READY, IN_DEV = "Ready for Dev", "In Dev"
AGENT_REVIEW, HUMAN_REVIEW = "Agent Review", "Human Review"
MAX_ABANDONED = 2
UNFINISHED = ("running", "interrupted")

RemoveContainers = Callable[[str], object]


@dataclass(frozen=True)
class Reaped:
    key: str
    role: str
    run_id: str
    action: str  # ready_for_dev | human_review | retry_review | none


def mark_runs(engine: Engine, run_ids: Collection[str], status: str, error: str) -> None:
    """Close unfinished runs with `status` (abandoned, interrupted)."""
    if not run_ids:
        return
    with Session(engine) as s, s.begin():
        s.execute(
            update(Run)
            .where(Run.id.in_(run_ids), Run.status.in_(UNFINISHED))
            .values(status=status, ended_at=datetime.now(UTC), error=error)
        )


def abandoned_count(engine: Engine, role: str, key: str) -> int:
    with Session(engine) as s:
        return int(
            s.scalar(
                select(func.count())
                .select_from(Run)
                .where(Run.role == role, Run.ticket_key == key, Run.status == "abandoned")
            )
            or 0
        )


def _last_run(engine: Engine, role: str, key: str) -> Run | None:
    with Session(engine) as s:
        return s.scalars(
            select(Run)
            .where(Run.role == role, Run.ticket_key == key)
            .order_by(Run.started_at.desc())
            .limit(1)
        ).first()


class Reaper:
    def __init__(
        self,
        engine: Engine,
        jira: JiraClient,
        ids: JiraIds,
        project_key: str,
        *,
        remove_containers: RemoveContainers,
    ) -> None:
        self.engine = engine
        self.jira = jira
        self.ids = ids
        self.project_key = project_key
        self.leases = LeaseStore(engine)
        self.tokens = RunTokenStore(engine)
        self._remove_containers = remove_containers
        self._cache = TransitionCache(ids.transitions)

    async def reap(self, live_runs: Collection[str] = ()) -> list[Reaped]:
        """Handle dead leases. Runs of this process (`live_runs`) are never reaped."""
        done = []
        for lease in self.leases.dead():
            if lease.run_id in live_runs:
                log.warning("reaper.live_run_stale", key=lease.ticket_key, run_id=lease.run_id)
                continue
            why = f"lease of {lease.instance} expired or its heartbeat stopped"
            done.append(await self._abandon(lease.ticket_key, lease.role, lease.run_id, why))
            self.leases.release(lease.ticket_key, lease.run_id)
        return done

    async def recover_orphans(self) -> list[Reaped]:
        """At startup: tickets `In Dev` with no lease whose last Coder run never finished."""
        done = []
        jql = f'project = {self.project_key} AND status = "{IN_DEV}"'
        async for ticket in search_tickets(self.jira, jql, self.ids.fields):
            if self.leases.holder(ticket.key) is not None:
                continue
            run = _last_run(self.engine, "coder", ticket.key)
            if run is None or run.status not in UNFINISHED:
                continue
            # A clean shutdown (`interrupted`) is not held against the ticket; a crash is.
            status = "interrupted" if run.status == "interrupted" else "abandoned"
            why = "the orchestrator stopped during the run"
            done.append(await self._abandon(ticket.key, "coder", run.id, why, status))
        return done

    async def _abandon(
        self, key: str, role: str, run_id: str, why: str, status: str = "abandoned"
    ) -> Reaped:
        log.warning("reaper.abandon", key=key, role=role, run_id=run_id, reason=why)
        mark_runs(self.engine, [run_id], status, why)
        with contextlib.suppress(Exception):  # Docker may be down; the container dies with it
            self._remove_containers(run_id)
        self.tokens.revoke_run(run_id)
        action = await self._route(key, role, run_id, why)
        record_event(
            self.engine,
            "transition",
            {"key": key, "role": role, "action": action, "reason": why, "by": "reaper"},
            run_id,
        )
        return Reaped(key, role, run_id, action)

    async def _route(self, key: str, role: str, run_id: str, why: str) -> str:
        status = (await get_ticket(self.jira, key, self.ids.fields)).status
        expected = {"coder": IN_DEV, "reviewer": AGENT_REVIEW}.get(role)
        if status != expected:
            return "none"  # finished its transition before dying, or a human moved it
        count = abandoned_count(self.engine, role, key)
        if count >= MAX_ABANDONED:
            await add_comment(
                self.jira,
                key,
                orchestrator_comment(
                    f"Run {run_id} ({role}) died: {why}. That is {count} abandoned {role} "
                    "runs on this ticket, so it needs a human. Labelled `needs-human`."
                ),
            )
            await add_labels(self.jira, key, [NEEDS_HUMAN])
            await transition_to(self.jira, key, HUMAN_REVIEW, self._cache)
            return "human_review"
        if role == "reviewer":
            return "retry_review"  # stays in Agent Review; the next free reviewer takes it
        await add_comment(
            self.jira,
            key,
            orchestrator_comment(f"Run {run_id} died: {why}. Back to Ready for Dev for a retry."),
        )
        await transition_to(self.jira, key, READY, self._cache)
        return "ready_for_dev"
