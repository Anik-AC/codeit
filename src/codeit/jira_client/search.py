"""JQL search over `POST /rest/api/3/search/jql` with `nextPageToken` paging (PRD 0.4, 7.2).

The legacy `/rest/api/3/search` endpoint is gone from Jira Cloud; do not use it.
Paging stops when Jira reports the last page, when a token repeats, or after
`MAX_PAGES` pages, so a misbehaving server cannot cause an endless loop.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

from codeit.jira_client.client import API, JiraClient
from codeit.jira_client.models import FieldMap, Ticket, ticket_field_ids
from codeit.log import get_logger

log = get_logger(__name__)

MAX_PAGES = 50
DEFAULT_PAGE_SIZE = 100


async def search(
    client: JiraClient,
    jql: str,
    fields: Sequence[str] = ("summary", "status"),
    max_results: int | None = None,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> AsyncIterator[dict[str, Any]]:
    """Yield raw issues matching `jql`, at most `max_results` in total (None = all)."""
    token: str | None = None
    seen: set[str] = set()
    yielded = 0
    for page in range(1, MAX_PAGES + 1):
        size = page_size if max_results is None else min(page_size, max_results - yielded)
        body: dict[str, Any] = {"jql": jql, "fields": list(fields), "maxResults": size}
        if token:
            body["nextPageToken"] = token
        data = await client.post_json(f"{API}/search/jql", body, idempotent=True)
        for issue in data.get("issues") or []:
            yield issue
            yielded += 1
            if max_results is not None and yielded >= max_results:
                return
        token = data.get("nextPageToken")
        if not token or data.get("isLast"):
            return
        if token in seen:
            log.warning("jira.search.repeated_token", jql=jql, page=page)
            return
        seen.add(token)
    log.warning("jira.search.page_cap", jql=jql, pages=MAX_PAGES)


async def search_tickets(
    client: JiraClient, jql: str, fields: FieldMap, max_results: int | None = None
) -> AsyncIterator[Ticket]:
    """Like `search`, but yields normalized `Ticket`s."""
    async for issue in search(client, jql, ticket_field_ids(fields), max_results):
        yield Ticket.from_issue(issue, fields)
