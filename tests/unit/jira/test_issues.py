from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
import respx

from codeit.jira_client import JiraClient
from codeit.jira_client.issues import (
    IssueSpec,
    JiraBulkError,
    add_labels,
    add_remote_link,
    bulk_create,
    create_issue,
    delete_issue,
    get_ticket,
    link_issues,
    remove_labels,
    update_fields,
)
from codeit.jira_client.models import Ticket
from tests.unit.jira.conftest import FIELDS, issue_json


def sent(route: respx.Route, i: int = -1) -> Any:
    return json.loads(route.calls[i].request.content)


def spec(summary: str = "Do it", **kw: Any) -> IssueSpec:
    return IssueSpec(project_key="CODEIT", issue_type_id="10001", summary=summary, **kw)


async def test_get_ticket_normalizes(mock: respx.MockRouter, client: JiraClient) -> None:
    raw = issue_json(
        "CODEIT-3",
        issuelinks=[
            {"type": {"name": "Blocks"}, "inwardIssue": {"key": "CODEIT-1"}},
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": "CODEIT-5"}},
            {"type": {"name": "Relates"}, "inwardIssue": {"key": "CODEIT-2"}},
        ],
        parent={"key": "CODEIT-100", "fields": {"issuetype": {"name": "Epic"}}},
        **{FIELDS.agent: "coder-1", FIELDS.review_loop: 2.0, FIELDS.pr_url: "https://gh/pr/1"},
    )
    route = mock.get("/issue/CODEIT-3").respond(json=raw)
    t = await get_ticket(client, "CODEIT-3", FIELDS)
    assert t == Ticket(
        key="CODEIT-3",
        summary="Add due dates",
        description_md="Body",
        status="Ready for Dev",
        priority="High",
        story_points=2.0,
        labels=["agent-draft"],
        agent="coder-1",
        review_loop=2,
        human_returns=1,
        pr_url="https://gh/pr/1",
        run_id=None,
        blocked_by=["CODEIT-1"],
        epic_key="CODEIT-100",
        created=datetime(2026, 9, 25, 10, tzinfo=UTC),
        updated=datetime(2026, 9, 25, 11, tzinfo=UTC),
    )
    assert FIELDS.run_id in route.calls.last.request.url.params["fields"]


def test_ticket_with_sparse_fields() -> None:
    raw = issue_json(
        description=None,
        priority=None,
        labels=None,
        issuelinks=None,
        parent={"key": "CODEIT-2", "fields": {"issuetype": {"name": "Story"}}},
        **{FIELDS.story_points: None, FIELDS.human_returns: None},
    )
    t = Ticket.from_issue(raw, FIELDS)
    assert t.description_md == ""
    assert t.priority is None
    assert t.story_points is None
    assert t.labels == []
    assert t.human_returns == 0
    assert t.epic_key is None


async def test_create_issue_sends_adf_and_parent(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    route = mock.post("/issue").respond(201, json={"id": "1", "key": "CODEIT-7"})
    key = await create_issue(
        client,
        spec(
            description_md="**Goal**",
            labels=["agent-draft"],
            parent_key="CODEIT-1",
            extra_fields={FIELDS.agent: "planner"},
        ),
    )
    assert key == "CODEIT-7"
    fields = sent(route)["fields"]
    assert fields["project"] == {"key": "CODEIT"}
    assert fields["issuetype"] == {"id": "10001"}
    assert fields["description"]["type"] == "doc"
    assert fields["description"]["content"][0]["content"][0]["marks"] == [{"type": "strong"}]
    assert fields["parent"] == {"key": "CODEIT-1"}
    assert fields["labels"] == ["agent-draft"]
    assert fields[FIELDS.agent] == "planner"


async def test_bulk_create_batches_of_50(mock: respx.MockRouter, client: JiraClient) -> None:
    counter = iter(range(1000))

    def created(request: Any) -> Any:
        n = len(json.loads(request.content)["issueUpdates"])
        import httpx

        return httpx.Response(
            201, json={"issues": [{"key": f"CODEIT-{next(counter)}"} for _ in range(n)]}
        )

    route = mock.post("/issue/bulk").mock(side_effect=created)
    keys = await bulk_create(client, [spec(f"s{i}") for i in range(51)])
    assert len(keys) == 51
    assert [len(sent(route, i)["issueUpdates"]) for i in range(2)] == [50, 1]


async def test_bulk_create_reports_failures(mock: respx.MockRouter, client: JiraClient) -> None:
    mock.post("/issue/bulk").respond(
        201,
        json={
            "issues": [{"key": "CODEIT-1"}],
            "errors": [
                {
                    "failedElementNumber": 1,
                    "elementErrors": {"errorMessages": [], "errors": {"summary": "too long"}},
                }
            ],
        },
    )
    with pytest.raises(JiraBulkError) as err:
        await bulk_create(client, [spec("ok"), spec("x" * 300)])
    assert err.value.created == ["CODEIT-1"]
    assert err.value.failures == ["item 1: summary: too long"]


async def test_update_fields_and_labels(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.put("/issue/CODEIT-1").respond(204)
    await update_fields(client, "CODEIT-1", FIELDS.ids(agent="coder-1", run_id="R1"))
    await add_labels(client, "CODEIT-1", ["needs-human"])
    await remove_labels(client, "CODEIT-1", ["agent-draft"])
    await add_labels(client, "CODEIT-1", [])  # no call
    assert route.call_count == 3
    assert sent(route, 0) == {"fields": {FIELDS.agent: "coder-1", FIELDS.run_id: "R1"}}
    assert sent(route, 1) == {"update": {"labels": [{"add": "needs-human"}]}}
    assert sent(route, 2) == {"update": {"labels": [{"remove": "agent-draft"}]}}


def test_field_map_rejects_unknown_names() -> None:
    with pytest.raises(KeyError):
        FIELDS.ids(nope=1)


async def test_link_issues_direction(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/issueLink").respond(201)
    await link_issues(client, blocker="CODEIT-1", blocked="CODEIT-2")
    assert sent(route) == {
        "type": {"name": "Blocks"},
        "inwardIssue": {"key": "CODEIT-1"},
        "outwardIssue": {"key": "CODEIT-2"},
    }


async def test_remote_link_and_delete(mock: respx.MockRouter, client: JiraClient) -> None:
    link = mock.post("/issue/CODEIT-1/remotelink").respond(201, json={"id": 1})
    delete = mock.delete("/issue/CODEIT-1").respond(204)
    await add_remote_link(client, "CODEIT-1", "https://github.com/a/b/pull/1", "PR #1")
    await delete_issue(client, "CODEIT-1")
    assert sent(link) == {
        "globalId": "https://github.com/a/b/pull/1",
        "object": {"url": "https://github.com/a/b/pull/1", "title": "PR #1"},
    }
    assert delete.calls.last.request.url.params["deleteSubtasks"] == "true"
