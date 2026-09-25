"""jira-mcp tools (PRD 15). Each returns markdown text plus structured content.

Scoping rules:
- Every key must belong to the configured project.
- `search_tickets` wraps the caller's JQL as `project = P AND (...)` and drops any result
  from another project.
- With a run token, `add_comment` accepts only the token's ticket.
"""

# No `from __future__ import annotations`: tool signatures embed the project key in their
# annotations, and the SDK must see them evaluated to build the input schemas.
import functools
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, TextContent, ToolAnnotations
from pydantic import Field

from codeit.jira_client import JiraError, JiraNotFound, Ticket
from codeit.jira_client.comments import add_comment as jira_add_comment
from codeit.jira_client.comments import list_comments
from codeit.jira_client.issues import IssueSpec
from codeit.jira_client.issues import create_issue as jira_create_issue
from codeit.jira_client.issues import get_ticket as jira_get_ticket
from codeit.jira_client.issues import link_issues as jira_link_issues
from codeit.jira_client.models import ticket_field_ids
from codeit.jira_client.search import search

if TYPE_CHECKING:
    from mcp_servers.jira.server import Caller, JiraMCP

MAX_COMMENT_CHARS = 30_000  # Jira's limit is 32,767; leave room for the footer
_READ = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)


def result(markdown: str, structured: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=markdown)], structured_content=structured
    )


def _jira_errors[F: Callable[..., Awaitable[CallToolResult]]](fn: F) -> F:
    """Turn Jira failures into tool errors the model can read."""

    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> CallToolResult:
        try:
            return await fn(*args, **kwargs)
        except JiraNotFound as e:
            raise ToolError(f"not found in Jira: {'; '.join(e.messages) or e}") from e
        except JiraError as e:
            raise ToolError(f"Jira error: {e}") from e

    return wrapper  # type: ignore[return-value]


def render_ticket(t: Ticket) -> str:
    facts = [
        f"- Status: {t.status}",
        f"- Priority: {t.priority or 'none'}",
        f"- Story points: {t.story_points if t.story_points is not None else 'none'}",
        f"- Labels: {', '.join(t.labels) or 'none'}",
        f"- Blocked by: {', '.join(t.blocked_by) or 'nothing'}",
        f"- Epic: {t.epic_key or 'none'}",
        f"- PR: {t.pr_url or 'none'}",
        f"- Review loops: {t.review_loop}, human returns: {t.human_returns}",
    ]
    description = t.description_md or "_No description._"
    return f"# {t.key}: {t.summary}\n\n" + "\n".join(facts) + f"\n\n## Description\n\n{description}"


def split_order_by(jql: str) -> tuple[str, str]:
    """Split `where ORDER BY x` into ("where", "ORDER BY x"), ignoring quoted text."""
    masked = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', lambda m: "_" * len(m[0]), jql)
    matches = list(re.finditer(r"\border\s+by\b", masked, re.IGNORECASE))
    if not matches:
        return jql.strip(), ""
    at = matches[-1].start()
    return jql[:at].strip(), jql[at:].strip()


def check_parens(jql: str) -> None:
    """Reject JQL whose parentheses could close our `project = P AND (` wrapper."""
    masked = re.sub(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'', "", jql)
    depth = 0
    for ch in masked:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            break
    if depth != 0:
        raise ToolError("JQL has unbalanced parentheses")


def register_tools(server: "JiraMCP") -> None:
    jira = server.jira
    project = jira.project_key
    key_re = re.compile(rf"^{re.escape(project)}-\d+$")

    def check_key(key: str) -> str:
        key = key.strip().upper()
        if not key_re.match(key):
            raise ToolError(f"{key!r} is not a {project} ticket key (expected e.g. {project}-12)")
        return key

    @server.tool(
        annotations=_READ,
        description=f"""Read one Jira ticket: summary, status, priority, points, labels,
blockers, PR link and the full description as markdown.

Use this first whenever you work on a ticket, and to read tickets it depends on.
Example: get_ticket(key="{project}-12")
Constraints: only keys in project {project}.""",
    )
    @_jira_errors
    async def get_ticket(
        key: Annotated[str, Field(description=f"e.g. {project}-12")],
    ) -> CallToolResult:
        t = await jira_get_ticket(jira.client, check_key(key), jira.ids.fields)
        return result(render_ticket(t), t.model_dump(mode="json"))

    @server.tool(
        annotations=_READ,
        description=f"""Find tickets with a JQL query. Returns key, status and summary per ticket.

Use this to look for related or duplicate work before planning or creating tickets.
Example: search_tickets(jql='status = "Ready for Dev" ORDER BY priority DESC', limit=10)
Constraints: results are limited to project {project} (you do not need to add it);
limit is 1 to 50.""",
    )
    @_jira_errors
    async def search_tickets(
        jql: Annotated[str, Field(description="JQL, optionally ending in ORDER BY")],
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
    ) -> CallToolResult:
        check_parens(jql)
        where, order = split_order_by(jql)
        scoped = f"project = {project}" + (f" AND ({where})" if where else "")
        if order:
            scoped += f" {order}"
        tickets = [
            Ticket.from_issue(issue, jira.ids.fields)
            async for issue in search(jira.client, scoped, ticket_field_ids(jira.ids.fields), limit)
            if key_re.match(str(issue.get("key", "")))
        ]
        lines = [f"- {t.key} [{t.status}] {t.summary}" for t in tickets]
        text = "\n".join(lines) if lines else "No tickets match."
        rows = [
            {
                "key": t.key,
                "summary": t.summary,
                "status": t.status,
                "priority": t.priority,
                "story_points": t.story_points,
                "labels": t.labels,
            }
            for t in tickets
        ]
        return result(text, {"tickets": rows})

    @server.tool(
        annotations=_READ,
        description=f"""Read a ticket's comments, oldest first, as markdown with author and time.

Use this to pick up review feedback and human instructions on a ticket.
Example: get_comments(key="{project}-12", since="2026-09-25T00:00:00Z")
Constraints: only keys in project {project}; `since` is an ISO 8601 time with a timezone.""",
    )
    @_jira_errors
    async def get_comments(
        key: Annotated[str, Field(description=f"e.g. {project}-12")],
        since: Annotated[
            str | None, Field(description="ISO 8601, e.g. 2026-09-25T00:00:00Z")
        ] = None,
    ) -> CallToolResult:
        after = None
        if since:
            try:
                after = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except ValueError as e:
                raise ToolError(f"since {since!r} is not an ISO 8601 time") from e
            if after.tzinfo is None:
                raise ToolError("since must include a timezone, e.g. 2026-09-25T00:00:00Z")
        comments = await list_comments(jira.client, check_key(key), since=after)
        parts = [f"### {c.author}, {c.created.isoformat()}\n\n{c.body_md}" for c in comments]
        text = "\n\n".join(parts) if parts else "No comments."
        return result(text, {"comments": [c.model_dump(mode="json") for c in comments]})

    @server.tool(
        annotations=_WRITE,
        description=f"""Add a markdown comment to a ticket.

Use this to report progress or blockers, or to answer review feedback.
Example: add_comment(key="{project}-12", markdown="Tests for criterion 2 were **missing**; added.")
Constraints: when you are running on a ticket, you can comment only on that ticket;
at most {MAX_COMMENT_CHARS} characters. The comment is signed with your role and run.""",
    )
    @_jira_errors
    async def add_comment(
        key: Annotated[str, Field(description=f"e.g. {project}-12")],
        markdown: Annotated[str, Field(min_length=1)],
    ) -> CallToolResult:
        key = check_key(key)
        caller = server.caller()
        if caller.run_id is not None and caller.ticket_key != key:
            bound = caller.ticket_key or "no ticket"
            raise ToolError(f"this run may only comment on {bound}, not {key}")
        if len(markdown) > MAX_COMMENT_CHARS:
            raise ToolError(
                f"comment is {len(markdown)} characters; the limit is {MAX_COMMENT_CHARS}"
            )
        body = markdown + _signature(caller)
        comment_id = await jira_add_comment(jira.client, key, body)
        return result(
            f"Comment {comment_id} added to {key}.", {"key": key, "comment_id": comment_id}
        )

    @server.tool(
        annotations=_WRITE,
        description=f"""Create a Jira issue. It starts in the workflow's first status (Agent Draft).

Use this to turn planned work into tickets, one per independently shippable story.
Example: create_issue(type="Story", summary="Add due dates to tasks",
  description_md="As a user, I want ...", labels=["agent-draft"], parent="{project}-3")
Constraints: type is one of the project's work types (e.g. Story, Epic); summary at most
255 characters; parent must be a {project} key, usually the Epic.""",
    )
    @_jira_errors
    async def create_issue(
        type: Annotated[str, Field(description="Work type name, e.g. Story or Epic")],
        summary: Annotated[str, Field(min_length=1, max_length=255)],
        description_md: str = "",
        labels: list[str] | None = None,
        parent: Annotated[str | None, Field(description="Parent key, e.g. the Epic")] = None,
    ) -> CallToolResult:
        try:
            type_id = jira.ids.issue_type_id(type)
        except KeyError as e:
            known = ", ".join(sorted(jira.ids.issue_types))
            raise ToolError(f"unknown work type {type!r}; expected one of: {known}") from e
        all_labels = list(dict.fromkeys(labels or []))
        if server.caller().role == "planner" and "agent-draft" not in all_labels:
            all_labels.append("agent-draft")
        key = await jira_create_issue(
            jira.client,
            IssueSpec(
                project_key=project,
                issue_type_id=type_id,
                summary=summary,
                description_md=description_md,
                labels=all_labels,
                parent_key=check_key(parent) if parent else None,
            ),
        )
        return result(f"Created {key}.", {"key": key})

    @server.tool(
        annotations=_WRITE,
        description=f"""Record that one ticket blocks another. The blocked ticket is not picked up
until the blocker is Done.

Use this when a story depends on another story's code or data.
Example: link_issues(blocker="{project}-4", blocked="{project}-7")
Constraints: both keys in project {project}; they must differ.""",
    )
    @_jira_errors
    async def link_issues(blocker: str, blocked: str) -> CallToolResult:
        blocker, blocked = check_key(blocker), check_key(blocked)
        if blocker == blocked:
            raise ToolError("a ticket cannot block itself")
        await jira_link_issues(jira.client, blocker=blocker, blocked=blocked)
        return result(f"{blocker} now blocks {blocked}.", {"blocker": blocker, "blocked": blocked})


def _signature(caller: "Caller") -> str:
    if caller.run_id is None:
        return ""
    return f"\n\n_Posted by {caller.role} (run {caller.run_id})_"
