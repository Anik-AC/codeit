from __future__ import annotations

import json
from typing import Any

import pytest
import respx
from mcp_types import CallToolResult

from mcp_servers.jira.roles import ROLE_TOOLS
from mcp_servers.jira.tools import check_parens, split_order_by
from tests.unit.jira.conftest import issue_json
from tests.unit.mcp.conftest import Connect


def text(r: CallToolResult) -> str:
    return "".join(getattr(c, "text", "") for c in r.content)


def sent(route: respx.Route) -> Any:
    return json.loads(route.calls.last.request.content)


# role visibility --------------------------------------------------------------------------------


@pytest.mark.parametrize("role", sorted(ROLE_TOOLS))
async def test_each_role_sees_exactly_its_tools(connect: Connect, role: str) -> None:
    async with connect(role) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == ROLE_TOOLS[role]


@pytest.mark.parametrize("role", sorted(ROLE_TOOLS))
async def test_hidden_tools_are_refused_like_missing_ones(connect: Connect, role: str) -> None:
    hidden = sorted(set().union(*ROLE_TOOLS.values()) - ROLE_TOOLS[role])
    async with connect(role) as c:
        for name in hidden:
            r = await c.call_tool(name, {})
            assert r.is_error
            assert text(r) == f"Unknown tool: {name}"


async def test_token_role_decides_visibility(connect: Connect) -> None:
    async with connect("coder", ticket="CODEIT-5", run_id="R1") as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == ROLE_TOOLS["coder"]


def test_no_role_has_a_transition_tool() -> None:
    assert not any("transition" in t for tools in ROLE_TOOLS.values() for t in tools)


async def test_tool_descriptions_have_example_and_constraints(connect: Connect) -> None:
    async with connect("human") as c:
        for tool in (await c.list_tools()).tools:
            assert "Example:" in (tool.description or ""), tool.name
            assert "Constraints:" in (tool.description or ""), tool.name


# get_ticket -------------------------------------------------------------------------------------


async def test_get_ticket(connect: Connect, mock: respx.MockRouter) -> None:
    mock.get("/issue/CODEIT-5").respond(json=issue_json("CODEIT-5"))
    async with connect("coder") as c:
        r = await c.call_tool("get_ticket", {"key": "codeit-5"})
    assert not r.is_error
    assert text(r).startswith("# CODEIT-5: Add due dates")
    assert "- Status: Ready for Dev" in text(r)
    assert "## Description\n\nBody" in text(r)
    assert r.structured_content is not None
    assert r.structured_content["key"] == "CODEIT-5"


async def test_key_outside_project_is_refused(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.get("/issue/OTHER-1").respond(json=issue_json("OTHER-1"))
    async with connect("coder") as c:
        r = await c.call_tool("get_ticket", {"key": "OTHER-1"})
    assert r.is_error
    assert "not a CODEIT ticket key" in text(r)
    assert not route.called


async def test_jira_not_found_becomes_tool_error(connect: Connect, mock: respx.MockRouter) -> None:
    mock.get("/issue/CODEIT-404").respond(404, json={"errorMessages": ["Issue does not exist"]})
    async with connect("coder") as c:
        r = await c.call_tool("get_ticket", {"key": "CODEIT-404"})
    assert r.is_error
    assert "Issue does not exist" in text(r)


async def test_other_jira_errors_become_tool_errors(
    connect: Connect, mock: respx.MockRouter
) -> None:
    mock.get("/issue/CODEIT-1").respond(403)
    async with connect("coder") as c:
        r = await c.call_tool("get_ticket", {"key": "CODEIT-1"})
    assert r.is_error
    assert "Jira error: GET /rest/api/3/issue/CODEIT-1 -> 403" in text(r)


# search_tickets ---------------------------------------------------------------------------------


async def test_search_is_scoped_to_project(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.post("/search/jql").respond(
        json={"issues": [issue_json("CODEIT-1"), issue_json("OTHER-9")]}
    )
    async with connect("planner") as c:
        r = await c.call_tool(
            "search_tickets", {"jql": 'status = "Ready for Dev" ORDER BY priority DESC', "limit": 5}
        )
    assert sent(route)["jql"] == (
        'project = CODEIT AND (status = "Ready for Dev") ORDER BY priority DESC'
    )
    assert sent(route)["maxResults"] == 5
    assert text(r) == "- CODEIT-1 [Ready for Dev] Add due dates"
    assert r.structured_content == {
        "tickets": [
            {
                "key": "CODEIT-1",
                "summary": "Add due dates",
                "status": "Ready for Dev",
                "priority": "High",
                "story_points": 2.0,
                "labels": ["agent-draft"],
            }
        ]
    }


async def test_search_with_only_order_by(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.post("/search/jql").respond(json={"issues": []})
    async with connect("planner") as c:
        r = await c.call_tool("search_tickets", {"jql": "ORDER BY created DESC"})
    assert sent(route)["jql"] == "project = CODEIT ORDER BY created DESC"
    assert text(r) == "No tickets match."


async def test_search_rejects_paren_escape(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.post("/search/jql")
    async with connect("planner") as c:
        r = await c.call_tool("search_tickets", {"jql": "x = 1) OR (project = OTHER"})
    assert r.is_error
    assert "unbalanced" in text(r)
    assert not route.called


async def test_search_limit_is_bounded(connect: Connect) -> None:
    async with connect("planner") as c:
        r = await c.call_tool("search_tickets", {"jql": "", "limit": 51})
    assert r.is_error


def test_split_order_by_ignores_quoted_text() -> None:
    assert split_order_by('summary ~ "order by x"') == ('summary ~ "order by x"', "")
    assert split_order_by("a = 1 order by b") == ("a = 1", "order by b")


def test_check_parens() -> None:
    check_parens('(a = 1) AND summary ~ ")"')
    for bad in ["(a", "a)", ") OR ("]:
        with pytest.raises(Exception, match="unbalanced"):
            check_parens(bad)


# get_comments -----------------------------------------------------------------------------------


def comment_page(*created: str) -> dict[str, Any]:
    return {
        "comments": [
            {
                "id": str(i),
                "author": {"displayName": "Onix"},
                "body": {
                    "type": "doc",
                    "version": 1,
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": f"c{i}"}]}
                    ],
                },
                "created": ts,
            }
            for i, ts in enumerate(created)
        ],
        "total": len(created),
    }


async def test_get_comments_since(connect: Connect, mock: respx.MockRouter) -> None:
    mock.get("/issue/CODEIT-1/comment").respond(
        json=comment_page("2026-09-24T10:00:00.000+0000", "2026-09-26T10:00:00.000+0000")
    )
    async with connect("coder") as c:
        r = await c.call_tool("get_comments", {"key": "CODEIT-1", "since": "2026-09-25T00:00:00Z"})
    assert text(r) == "### Onix, 2026-09-26T10:00:00+00:00\n\nc1"
    assert r.structured_content is not None
    assert len(r.structured_content["comments"]) == 1


async def test_get_comments_empty(connect: Connect, mock: respx.MockRouter) -> None:
    mock.get("/issue/CODEIT-1/comment").respond(json=comment_page())
    async with connect("coder") as c:
        r = await c.call_tool("get_comments", {"key": "CODEIT-1"})
    assert text(r) == "No comments."


@pytest.mark.parametrize("since", ["yesterday", "2026-09-25T00:00:00"])
async def test_get_comments_bad_since(connect: Connect, since: str) -> None:
    async with connect("coder") as c:
        r = await c.call_tool("get_comments", {"key": "CODEIT-1", "since": since})
    assert r.is_error


# add_comment ------------------------------------------------------------------------------------


async def test_add_comment_on_own_ticket_is_signed(
    connect: Connect, mock: respx.MockRouter
) -> None:
    route = mock.post("/issue/CODEIT-5/comment").respond(201, json={"id": "77"})
    async with connect("coder", ticket="CODEIT-5", run_id="RUN1") as c:
        r = await c.call_tool("add_comment", {"key": "CODEIT-5", "markdown": "All **green**"})
    assert not r.is_error
    assert r.structured_content == {"key": "CODEIT-5", "comment_id": "77"}
    paragraphs = sent(route)["body"]["content"]
    assert paragraphs[-1]["content"][0] == {
        "type": "text",
        "text": "Posted by coder (run RUN1)",
        "marks": [{"type": "em"}],
    }


async def test_add_comment_on_other_ticket_is_refused(
    connect: Connect, mock: respx.MockRouter
) -> None:
    route = mock.post("/issue/CODEIT-6/comment")
    async with connect("coder", ticket="CODEIT-5", run_id="RUN1") as c:
        r = await c.call_tool("add_comment", {"key": "CODEIT-6", "markdown": "hi"})
    assert r.is_error
    assert "only comment on CODEIT-5" in text(r)
    assert not route.called


async def test_add_comment_run_without_ticket_is_refused(connect: Connect) -> None:
    async with connect("reviewer", ticket=None, run_id="RUN1") as c:
        r = await c.call_tool("add_comment", {"key": "CODEIT-6", "markdown": "hi"})
    assert "only comment on no ticket" in text(r)


async def test_add_comment_as_human_is_unsigned(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.post("/issue/CODEIT-6/comment").respond(201, json={"id": "1"})
    async with connect("human") as c:
        await c.call_tool("add_comment", {"key": "CODEIT-6", "markdown": "hi"})
    assert len(sent(route)["body"]["content"]) == 1


async def test_add_comment_too_long(connect: Connect) -> None:
    async with connect("human") as c:
        r = await c.call_tool("add_comment", {"key": "CODEIT-6", "markdown": "x" * 30_001})
    assert "limit" in text(r)


# create_issue and link_issues -------------------------------------------------------------------


async def test_planner_create_issue_adds_agent_draft(
    connect: Connect, mock: respx.MockRouter
) -> None:
    route = mock.post("/issue").respond(201, json={"key": "CODEIT-20"})
    async with connect("planner") as c:
        r = await c.call_tool(
            "create_issue",
            {"type": "story", "summary": "Do it", "labels": ["x", "x"], "parent": "CODEIT-3"},
        )
    assert r.structured_content == {"key": "CODEIT-20"}
    fields = sent(route)["fields"]
    assert fields["issuetype"] == {"id": "10009"}
    assert fields["labels"] == ["x", "agent-draft"]
    assert fields["parent"] == {"key": "CODEIT-3"}


async def test_human_create_issue_keeps_labels(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.post("/issue").respond(201, json={"key": "CODEIT-21"})
    async with connect("human") as c:
        await c.call_tool("create_issue", {"type": "Epic", "summary": "Big"})
    assert sent(route)["fields"]["labels"] == []


async def test_create_issue_unknown_type(connect: Connect) -> None:
    async with connect("planner") as c:
        r = await c.call_tool("create_issue", {"type": "Bug", "summary": "x"})
    assert "Epic, Story" in text(r)


async def test_link_issues(connect: Connect, mock: respx.MockRouter) -> None:
    route = mock.post("/issueLink").respond(201)
    async with connect("planner") as c:
        r = await c.call_tool("link_issues", {"blocker": "CODEIT-1", "blocked": "CODEIT-2"})
    assert text(r) == "CODEIT-1 now blocks CODEIT-2."
    assert sent(route)["inwardIssue"] == {"key": "CODEIT-1"}


async def test_link_issue_to_itself(connect: Connect) -> None:
    async with connect("planner") as c:
        r = await c.call_tool("link_issues", {"blocker": "CODEIT-1", "blocked": "CODEIT-1"})
    assert "cannot block itself" in text(r)


async def test_unauthenticated_server_has_no_tools(jira: Any) -> None:
    from mcp import Client

    from mcp_servers.jira.server import JiraMCP

    async with Client(JiraMCP(jira)) as c:
        assert (await c.list_tools()).tools == []
        r = await c.call_tool("get_ticket", {"key": "CODEIT-1"})
    assert r.is_error


def test_unknown_stdio_role(jira: Any) -> None:
    from mcp_servers.jira.server import JiraMCP

    with pytest.raises(ValueError, match="unknown role"):
        JiraMCP(jira, stdio_role="admin")
