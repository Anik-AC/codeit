"""The fast lane (ADR-0016): while it is on, tickets in Agent Review skip the Reviewer
agent and move straight to Human Review, with a comment and the `fast-lane` label so the
human knows no agent reviewed them. A ticket being reviewed right now is left to finish.
"""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy import Engine

from codeit.agents.coder import orchestrator_comment
from codeit.events import record_event
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment
from codeit.jira_client.issues import add_labels, get_ticket
from codeit.jira_client.search import search_tickets
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.orchestrator.leases import LeaseStore

AGENT_REVIEW, HUMAN_REVIEW = "Agent Review", "Human Review"
LABEL = "fast-lane"


async def fast_track(
    engine: Engine,
    jira: JiraClient,
    ids: JiraIds,
    project_key: str,
    *,
    busy: Collection[str] = (),
) -> list[str]:
    """Move every Agent Review ticket that no run holds to Human Review. Returns their keys."""
    leases = LeaseStore(engine)
    cache = TransitionCache(ids.transitions)
    moved = []
    jql = f'project = {project_key} AND status = "{AGENT_REVIEW}" ORDER BY updated ASC'
    async for ticket in search_tickets(jira, jql, ids.fields):
        if ticket.key in busy or leases.holder(ticket.key) is not None:
            continue
        # Re-read: a human may have moved it since the search (PRD 12.5).
        if (await get_ticket(jira, ticket.key, ids.fields)).status != AGENT_REVIEW:
            continue
        await add_comment(
            jira,
            ticket.key,
            orchestrator_comment(
                "Fast lane is on, so the Reviewer agent was skipped. CI still has to pass "
                "before the PR can merge. Labelled `fast-lane`."
            ),
        )
        await add_labels(jira, ticket.key, [LABEL])
        await transition_to(jira, ticket.key, HUMAN_REVIEW, cache)
        record_event(
            engine, "transition", {"key": ticket.key, "to": HUMAN_REVIEW, "by": "fast lane"}
        )
        moved.append(ticket.key)
    return moved
