"""`tickets_cache` (PRD 13): a copy of the project's tickets for the dashboard.

Jira stays the source of truth. Each poll refreshes tickets that are open, or that
changed in the last week, and returns the ones that differ from the cached copy.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from codeit.db.models import TicketCache
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.models import Ticket
from codeit.jira_client.search import search_tickets

MAX_TICKETS = 200
FIELDS = ("status", "summary", "priority", "points", "review_loop", "human_returns", "pr_url")


def jql(project_key: str) -> str:
    return (
        f"project = {project_key} AND (status NOT IN (Done, Rejected) OR updated >= -7d) "
        "ORDER BY updated DESC"
    )


def as_dict(row: TicketCache) -> dict[str, Any]:
    return {
        "key": row.key,
        "status": row.status,
        "summary": row.summary,
        "priority": row.priority,
        "points": row.points,
        "review_loop": row.review_loop,
        "human_returns": row.human_returns,
        "pr_url": row.pr_url,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def _values(t: Ticket) -> dict[str, Any]:
    return {
        "status": t.status,
        "summary": t.summary,
        "priority": t.priority,
        "points": t.story_points,
        "review_loop": t.review_loop,
        "human_returns": t.human_returns,
        "pr_url": t.pr_url,
    }


async def refresh(
    engine: Engine, jira: JiraClient, ids: JiraIds, project_key: str
) -> list[dict[str, Any]]:
    """Update the cache from Jira; return the tickets that changed."""
    tickets = [t async for t in search_tickets(jira, jql(project_key), ids.fields, MAX_TICKETS)]
    changed: list[dict[str, Any]] = []
    with Session(engine) as s, s.begin():
        cached = {
            r.key: r
            for r in s.scalars(
                select(TicketCache).where(TicketCache.key.in_([t.key for t in tickets]))
            )
        }
        for t in tickets:
            values = _values(t)
            row = cached.get(t.key)
            if row is not None and all(getattr(row, f) == values[f] for f in FIELDS):
                continue
            if row is None:
                row = TicketCache(key=t.key, **values, updated_at=t.updated)
                s.add(row)
            else:
                for f, v in values.items():
                    setattr(row, f, v)
            row.updated_at = t.updated if t.updated.tzinfo else t.updated.replace(tzinfo=UTC)
            s.flush()
            changed.append(as_dict(row))
    return changed


def cached(engine: Engine, status: str | None = None) -> list[dict[str, Any]]:
    with Session(engine) as s:
        q = select(TicketCache).order_by(TicketCache.updated_at.desc())
        if status:
            q = q.where(TicketCache.status == status)
        return [as_dict(r) for r in s.scalars(q)]
