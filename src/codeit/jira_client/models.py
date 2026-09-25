"""Pydantic models for Jira data normalized for the rest of CodeIt (PRD 7.2, 7.3)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from codeit.jira_client.adf import adf_to_markdown

# The custom fields CodeIt needs, by FieldMap attribute, with the Jira field names that
# satisfy each one (PRD 7.1 step 5). Team-managed boards call story points
# "Story point estimate"; company-managed ones call them "Story Points".
REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "story_points": ("Story point estimate", "Story Points"),
    "agent": ("Agent",),
    "review_loop": ("Review Loop",),
    "human_returns": ("Human Returns",),
    "pr_url": ("PR URL",),
    "run_id": ("Run ID",),
}

# PRD 6.1, in lifecycle order.
REQUIRED_STATUSES: tuple[str, ...] = (
    "Agent Draft",
    "Rejected",
    "Ready for Dev",
    "In Dev",
    "Agent Review",
    "Human Review",
    "Done",
)

REQUIRED_ISSUE_TYPES: tuple[str, ...] = ("Epic", "Story")


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FieldMap(_Model):
    """Jira field IDs (e.g. `customfield_10042`) for CodeIt's fields."""

    story_points: str
    agent: str
    review_loop: str
    human_returns: str
    pr_url: str
    run_id: str

    def ids(self, **values: Any) -> dict[str, Any]:
        """Translate `agent="coder-1"` style values into `{field_id: value}`."""
        out: dict[str, Any] = {}
        for name, value in values.items():
            if name not in type(self).model_fields:
                raise KeyError(f"unknown CodeIt field {name!r}")
            out[getattr(self, name)] = value
        return out


class JiraIds(_Model):
    """Site-specific IDs, written by `codeit jira discover` to `config/jira_ids.yaml`."""

    fields: FieldMap
    statuses: dict[str, str]
    issue_types: dict[str, str]
    # target status name -> transition ID, sampled from one issue. May be incomplete; the
    # transitions module refetches on a miss.
    transitions: dict[str, str] = Field(default_factory=dict)

    def issue_type_id(self, name: str) -> str:
        for key, value in self.issue_types.items():
            if key.casefold() == name.casefold():
                return value
        raise KeyError(f"issue type {name!r} not in jira_ids.yaml; rerun `codeit jira discover`")


def ticket_field_ids(fields: FieldMap) -> list[str]:
    """Jira fields to request so `Ticket.from_issue` has everything it needs."""
    return [
        "summary",
        "description",
        "status",
        "priority",
        "labels",
        "issuelinks",
        "parent",
        "created",
        "updated",
        fields.story_points,
        fields.agent,
        fields.review_loop,
        fields.human_returns,
        fields.pr_url,
        fields.run_id,
    ]


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        name = value.get("name")
        return str(name) if name is not None else None
    return None


def _int(value: Any) -> int:
    return int(value) if isinstance(value, int | float) else 0


def _str(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


class Ticket(_Model):
    key: str
    summary: str
    description_md: str
    status: str
    priority: str | None
    story_points: float | None
    labels: list[str]
    agent: str | None
    review_loop: int
    human_returns: int
    pr_url: str | None
    run_id: str | None
    blocked_by: list[str]
    epic_key: str | None
    created: datetime
    updated: datetime

    @classmethod
    def from_issue(cls, issue: dict[str, Any], fields: FieldMap) -> Ticket:
        f: dict[str, Any] = issue.get("fields") or {}
        points = f.get(fields.story_points)
        return cls(
            key=issue["key"],
            summary=f.get("summary") or "",
            description_md=adf_to_markdown(f.get("description")),
            status=_name(f.get("status")) or "",
            priority=_name(f.get("priority")),
            story_points=float(points) if isinstance(points, int | float) else None,
            labels=list(f.get("labels") or []),
            agent=_str(f.get(fields.agent)),
            review_loop=_int(f.get(fields.review_loop)),
            human_returns=_int(f.get(fields.human_returns)),
            pr_url=_str(f.get(fields.pr_url)),
            run_id=_str(f.get(fields.run_id)),
            blocked_by=_blocked_by(f.get("issuelinks") or []),
            epic_key=_epic_key(f.get("parent")),
            created=f["created"],
            updated=f["updated"],
        )


def _blocked_by(links: list[dict[str, Any]]) -> list[str]:
    """Keys of issues that block this one.

    On an issue's own `issuelinks`, an entry with `inwardIssue` reads "this issue
    <inward description> inwardIssue", i.e. "is blocked by" for the Blocks type.
    """
    return [
        link["inwardIssue"]["key"]
        for link in links
        if (link.get("type") or {}).get("name") == "Blocks" and "inwardIssue" in link
    ]


def _epic_key(parent: Any) -> str | None:
    if not isinstance(parent, dict):
        return None
    issuetype = (parent.get("fields") or {}).get("issuetype") or {}
    if issuetype.get("name") == "Epic" or issuetype.get("hierarchyLevel") == 1:
        return str(parent["key"])
    return None


class Comment(_Model):
    id: str
    author: str
    author_account_id: str | None
    body_md: str
    created: datetime
    updated: datetime

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Comment:
        author = raw.get("author") or {}
        return cls(
            id=str(raw["id"]),
            author=str(author.get("displayName") or "unknown"),
            author_account_id=_str(author.get("accountId")),
            body_md=adf_to_markdown(raw.get("body")),
            created=raw["created"],
            updated=raw.get("updated") or raw["created"],
        )


class Transition(_Model):
    id: str
    name: str
    to_status: str
    to_status_id: str

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> Transition:
        to = raw.get("to") or {}
        return cls(
            id=str(raw["id"]),
            name=str(raw.get("name") or ""),
            to_status=str(to.get("name") or ""),
            to_status_id=str(to.get("id") or ""),
        )
