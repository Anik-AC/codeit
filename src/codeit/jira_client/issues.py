"""Issue reads and writes: get, create, bulk create, fields, labels and links (PRD 7.2)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codeit.jira_client.adf import markdown_to_adf
from codeit.jira_client.client import API, JiraClient, JiraError
from codeit.jira_client.models import FieldMap, Ticket, ticket_field_ids

BULK_LIMIT = 50  # Jira's maximum issues per bulk create call


class IssueSpec(BaseModel):
    """Everything needed to create one issue. `extra_fields` is keyed by Jira field ID."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    project_key: str
    issue_type_id: str
    summary: str
    description_md: str = ""
    labels: list[str] = Field(default_factory=list)
    parent_key: str | None = None
    extra_fields: dict[str, Any] = Field(default_factory=dict)

    def to_fields(self) -> dict[str, Any]:
        fields: dict[str, Any] = {
            "project": {"key": self.project_key},
            "issuetype": {"id": self.issue_type_id},
            "summary": self.summary,
            "description": markdown_to_adf(self.description_md),
            "labels": list(self.labels),
            **self.extra_fields,
        }
        if self.parent_key:
            fields["parent"] = {"key": self.parent_key}
        return fields


class JiraBulkError(JiraError):
    """Some issues in a bulk create failed. `created` lists the keys that were created."""

    def __init__(self, message: str, *, created: list[str], failures: list[str]) -> None:
        super().__init__(message)
        self.created = created
        self.failures = failures


async def get_issue(
    client: JiraClient, key: str, fields: Sequence[str] | None = None
) -> dict[str, Any]:
    params = {"fields": ",".join(fields)} if fields else None
    result: dict[str, Any] = await client.get_json(f"{API}/issue/{key}", params=params)
    return result


async def get_ticket(client: JiraClient, key: str, fields: FieldMap) -> Ticket:
    issue = await get_issue(client, key, ticket_field_ids(fields))
    return Ticket.from_issue(issue, fields)


async def create_issue(client: JiraClient, spec: IssueSpec) -> str:
    """Create one issue and return its key."""
    data = await client.post_json(f"{API}/issue", {"fields": spec.to_fields()})
    return str(data["key"])


async def bulk_create(client: JiraClient, specs: Sequence[IssueSpec]) -> list[str]:
    """Create issues in batches of 50. Returns keys in input order.

    Raises `JiraBulkError` if any issue fails; its `created` lists keys that exist, so the
    caller can decide whether to clean them up.
    """
    created: list[str] = []
    for start in range(0, len(specs), BULK_LIMIT):
        batch = specs[start : start + BULK_LIMIT]
        body = {"issueUpdates": [{"fields": s.to_fields()} for s in batch]}
        data = await client.post_json(f"{API}/issue/bulk", body)
        created += [str(i["key"]) for i in data.get("issues") or []]
        errors = data.get("errors") or []
        if errors:
            failures = [_bulk_failure(e, start) for e in errors]
            raise JiraBulkError(
                f"bulk create failed for {len(failures)} issue(s): {'; '.join(failures)}",
                created=created,
                failures=failures,
            )
    return created


def _bulk_failure(error: Mapping[str, Any], offset: int) -> str:
    index = int(error.get("failedElementNumber", -1))
    element = error.get("elementErrors") or {}
    msgs = list(element.get("errorMessages") or [])
    msgs += [f"{k}: {v}" for k, v in (element.get("errors") or {}).items()]
    where = f"item {offset + index}" if index >= 0 else "unknown item"
    return f"{where}: {', '.join(msgs) or 'no detail'}"


async def update_fields(client: JiraClient, key: str, fields: Mapping[str, Any]) -> None:
    """Set fields by Jira field ID. Use `FieldMap.ids(...)` for CodeIt's custom fields."""
    await client.put(f"{API}/issue/{key}", {"fields": dict(fields)})


async def add_labels(client: JiraClient, key: str, labels: Iterable[str]) -> None:
    ops = [{"add": label} for label in labels]
    if ops:
        await client.put(f"{API}/issue/{key}", {"update": {"labels": ops}})


async def remove_labels(client: JiraClient, key: str, labels: Iterable[str]) -> None:
    ops = [{"remove": label} for label in labels]
    if ops:
        await client.put(f"{API}/issue/{key}", {"update": {"labels": ops}})


async def link_issues(client: JiraClient, blocker: str, blocked: str) -> None:
    """Record that `blocker` blocks `blocked` (a "Blocks" link).

    Jira's API puts the blocking issue in `inwardIssue`. The live test
    `test_link_direction` checks this against a real site.
    """
    body = {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": blocker},
        "outwardIssue": {"key": blocked},
    }
    await client.post_json(f"{API}/issueLink", body)


async def add_remote_link(client: JiraClient, key: str, url: str, title: str) -> None:
    """Attach an external link, e.g. the PR, to the issue. Idempotent per URL."""
    body = {"globalId": url, "object": {"url": url, "title": title}}
    await client.post_json(f"{API}/issue/{key}/remotelink", body)


async def delete_issue(client: JiraClient, key: str) -> None:
    await client.delete(f"{API}/issue/{key}", params={"deleteSubtasks": "true"})
