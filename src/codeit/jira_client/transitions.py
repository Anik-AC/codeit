"""Status transitions by target status name (PRD 7.2).

Transition IDs come from a cache seeded by `jira_ids.yaml`. On a miss, or when Jira
rejects a cached ID as stale, the issue's transitions are fetched once and the cache
is updated. Only the orchestrator calls this; agents never transition (PRD 5.2).
"""

from __future__ import annotations

from collections.abc import Mapping

from codeit.jira_client.client import API, JiraBadRequest, JiraClient, JiraConflict
from codeit.jira_client.models import Transition
from codeit.log import get_logger

log = get_logger(__name__)


class TransitionCache:
    """Target status name (case-insensitive) -> transition ID."""

    def __init__(self, initial: Mapping[str, str] | None = None) -> None:
        self._ids: dict[str, str] = {k.casefold(): v for k, v in (initial or {}).items()}

    def get(self, status: str) -> str | None:
        return self._ids.get(status.casefold())

    def update(self, transitions: list[Transition]) -> None:
        for t in transitions:
            self._ids[t.to_status.casefold()] = t.id


async def list_transitions(client: JiraClient, key: str) -> list[Transition]:
    data = await client.get_json(f"{API}/issue/{key}/transitions")
    return [Transition.from_api(t) for t in data.get("transitions") or []]


async def _post(client: JiraClient, key: str, transition_id: str) -> None:
    await client.post_json(f"{API}/issue/{key}/transitions", {"transition": {"id": transition_id}})


async def transition_to(client: JiraClient, key: str, status: str, cache: TransitionCache) -> None:
    """Move `key` to the status named `status`.

    Raises `JiraConflict` if no transition from the issue's current status leads there.
    """
    cached = cache.get(status)
    if cached is not None:
        try:
            await _post(client, key, cached)
            return
        except JiraBadRequest:
            log.info("jira.transition.stale", key=key, status=status, transition_id=cached)

    available = await list_transitions(client, key)
    cache.update(available)
    match = next((t for t in available if t.to_status.casefold() == status.casefold()), None)
    if match is None:
        reachable = ", ".join(sorted({t.to_status for t in available})) or "none"
        raise JiraConflict(f"{key} cannot transition to {status!r}; reachable: {reachable}")
    await _post(client, key, match.id)
