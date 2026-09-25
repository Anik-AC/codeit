"""Issue comments, written as markdown and stored as ADF (PRD 0.5, 7.2)."""

from __future__ import annotations

from datetime import datetime

from codeit.jira_client.adf import markdown_to_adf
from codeit.jira_client.client import API, JiraClient
from codeit.jira_client.models import Comment

PAGE_SIZE = 100
MAX_PAGES = 50


async def add_comment(client: JiraClient, key: str, markdown: str) -> str:
    """Add a comment and return its ID."""
    data = await client.post_json(f"{API}/issue/{key}/comment", {"body": markdown_to_adf(markdown)})
    return str(data["id"])


async def list_comments(
    client: JiraClient, key: str, since: datetime | None = None
) -> list[Comment]:
    """All comments on `key`, oldest first; only those created after `since` if given."""
    out: list[Comment] = []
    start = 0
    for _ in range(MAX_PAGES):
        data = await client.get_json(
            f"{API}/issue/{key}/comment",
            params={"startAt": start, "maxResults": PAGE_SIZE, "orderBy": "created"},
        )
        page = data.get("comments") or []
        out += [Comment.from_api(c) for c in page]
        start += len(page)
        if not page or start >= int(data.get("total", 0)):
            break
    if since is not None:
        out = [c for c in out if c.created > since]
    return out
