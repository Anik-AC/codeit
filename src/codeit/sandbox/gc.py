"""`codeit sandbox gc` (PRD 10.2): drop clones of finished tickets and stale clones."""

from __future__ import annotations

from codeit.config import Secrets
from codeit.jira_client import JiraClient, JiraNotFound
from codeit.jira_client.issues import get_issue
from codeit.sandbox.clone import CloneManager

TERMINAL = frozenset({"Done", "Rejected"})


async def collect_garbage(
    clones: CloneManager, secrets: Secrets, *, max_age_days: float = 14
) -> list[tuple[str, str]]:
    """Remove clones whose ticket is terminal or gone, or that are older than the limit.
    Returns (key, reason) for each removal."""
    found = clones.list_clones()
    if not found:
        return []
    removed: list[tuple[str, str]] = []
    async with JiraClient.from_secrets(secrets) as client:
        for key, age in found.items():
            reason = None
            try:
                issue = await get_issue(client, key, ["status"])
                status = str(issue["fields"]["status"]["name"])
                if status in TERMINAL:
                    reason = f"ticket is {status}"
            except JiraNotFound:
                reason = "ticket not found"
            if reason is None and age > max_age_days:
                reason = f"{age:.0f} days old"
            if reason is not None and clones.remove(key):
                removed.append((key, reason))
    return removed
