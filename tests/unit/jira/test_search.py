from __future__ import annotations

import json
from typing import Any

import httpx
import respx

from codeit.jira_client import JiraClient
from codeit.jira_client.search import MAX_PAGES, search, search_tickets
from tests.unit.jira.conftest import FIELDS, issue_json


def page(keys: list[str], token: str | None = None, is_last: bool | None = None) -> httpx.Response:
    body: dict[str, Any] = {"issues": [{"key": k} for k in keys]}
    if token is not None:
        body["nextPageToken"] = token
    if is_last is not None:
        body["isLast"] = is_last
    return httpx.Response(200, json=body)


def bodies(route: respx.Route) -> list[dict[str, Any]]:
    return [json.loads(c.request.content) for c in route.calls]


async def collect(client: JiraClient, **kw: Any) -> list[str]:
    return [i["key"] async for i in search(client, "project = CODEIT", **kw)]


async def test_follows_next_page_token(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/search/jql").mock(
        side_effect=[page(["A-1", "A-2"], "t1"), page(["A-3"], "t2"), page(["A-4"])]
    )
    assert await collect(client, fields=["summary"]) == ["A-1", "A-2", "A-3", "A-4"]
    sent = bodies(route)
    assert "nextPageToken" not in sent[0]
    assert [b.get("nextPageToken") for b in sent[1:]] == ["t1", "t2"]
    assert sent[0]["jql"] == "project = CODEIT"
    assert sent[0]["fields"] == ["summary"]


async def test_stops_on_is_last(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/search/jql").mock(side_effect=[page(["A-1"], "t1", is_last=True)])
    assert await collect(client) == ["A-1"]
    assert route.call_count == 1


async def test_repeated_token_stops(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/search/jql").mock(
        side_effect=[page(["A-1"], "same"), page(["A-2"], "same"), page(["never"])]
    )
    assert await collect(client) == ["A-1", "A-2"]
    assert route.call_count == 2


async def test_page_cap(mock: respx.MockRouter, client: JiraClient) -> None:
    counter = iter(range(10_000))

    def endless(request: httpx.Request) -> httpx.Response:
        n = next(counter)
        return page([f"A-{n}"], f"token-{n}")

    route = mock.post("/search/jql").mock(side_effect=endless)
    keys = await collect(client)
    assert len(keys) == MAX_PAGES
    assert route.call_count == MAX_PAGES


async def test_max_results_limits_total_and_page_size(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    route = mock.post("/search/jql").mock(
        side_effect=[page(["A-1", "A-2"], "t1"), page(["A-3", "A-4"], "t2")]
    )
    assert await collect(client, max_results=3, page_size=2) == ["A-1", "A-2", "A-3"]
    assert [b["maxResults"] for b in bodies(route)] == [2, 1]


async def test_empty_result(mock: respx.MockRouter, client: JiraClient) -> None:
    mock.post("/search/jql").respond(json={"issues": []})
    assert await collect(client) == []


async def test_search_tickets_normalizes(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/search/jql").respond(json={"issues": [issue_json("CODEIT-9")]})
    tickets = [t async for t in search_tickets(client, "project = CODEIT", FIELDS)]
    assert [t.key for t in tickets] == ["CODEIT-9"]
    assert FIELDS.review_loop in bodies(route)[0]["fields"]
