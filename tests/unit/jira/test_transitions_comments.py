from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from codeit.jira_client import JiraClient, JiraConflict
from codeit.jira_client.comments import add_comment, list_comments
from codeit.jira_client.transitions import TransitionCache, transition_to

TRANSITIONS = {
    "transitions": [
        {"id": "21", "name": "Start", "to": {"name": "In Dev", "id": "3"}},
        {"id": "31", "name": "Review", "to": {"name": "Agent Review", "id": "4"}},
    ]
}


def posted_ids(route: respx.Route) -> list[str]:
    return [json.loads(c.request.content)["transition"]["id"] for c in route.calls]


async def test_cache_hit_posts_directly(mock: respx.MockRouter, client: JiraClient) -> None:
    post = mock.post("/issue/CODEIT-1/transitions").respond(204)
    get = mock.get("/issue/CODEIT-1/transitions").respond(json=TRANSITIONS)
    await transition_to(client, "CODEIT-1", "in dev", TransitionCache({"In Dev": "21"}))
    assert posted_ids(post) == ["21"]
    assert not get.called


async def test_cache_miss_refetches_once(mock: respx.MockRouter, client: JiraClient) -> None:
    post = mock.post("/issue/CODEIT-1/transitions").respond(204)
    get = mock.get("/issue/CODEIT-1/transitions").respond(json=TRANSITIONS)
    cache = TransitionCache()
    await transition_to(client, "CODEIT-1", "Agent Review", cache)
    assert posted_ids(post) == ["31"]
    assert get.call_count == 1
    assert cache.get("In Dev") == "21"


async def test_stale_cached_id_refetches(mock: respx.MockRouter, client: JiraClient) -> None:
    post = mock.post("/issue/CODEIT-1/transitions").mock(
        side_effect=[
            httpx.Response(400, json={"errorMessages": ["Transition id '99' is not valid"]}),
            httpx.Response(204),
        ]
    )
    mock.get("/issue/CODEIT-1/transitions").respond(json=TRANSITIONS)
    await transition_to(client, "CODEIT-1", "In Dev", TransitionCache({"In Dev": "99"}))
    assert posted_ids(post) == ["99", "21"]


async def test_unreachable_status_raises_conflict(
    mock: respx.MockRouter, client: JiraClient
) -> None:
    mock.get("/issue/CODEIT-1/transitions").respond(json=TRANSITIONS)
    with pytest.raises(JiraConflict, match="Agent Review, In Dev"):
        await transition_to(client, "CODEIT-1", "Done", TransitionCache())


async def test_add_comment_sends_adf(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.post("/issue/CODEIT-1/comment").respond(201, json={"id": 555})
    assert await add_comment(client, "CODEIT-1", "Picked up by `coder-1`") == "555"
    body = json.loads(route.calls.last.request.content)["body"]
    assert body["type"] == "doc"
    assert body["content"][0]["content"][1] == {
        "type": "text",
        "text": "coder-1",
        "marks": [{"type": "code"}],
    }


def comment(cid: int, created: str) -> dict[str, Any]:
    return {
        "id": str(cid),
        "author": {"displayName": "Onix", "accountId": "abc"},
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": f"c{cid}"}]}],
        },
        "created": created,
    }


async def test_list_comments_pages_and_filters(mock: respx.MockRouter, client: JiraClient) -> None:
    route = mock.get("/issue/CODEIT-1/comment").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "comments": [
                        comment(1, "2026-09-24T10:00:00.000+0000"),
                        comment(2, "2026-09-25T10:00:00.000+0000"),
                    ],
                    "total": 3,
                },
            ),
            httpx.Response(
                200,
                json={"comments": [comment(3, "2026-09-26T10:00:00.000+0000")], "total": 3},
            ),
        ]
    )
    comments = await list_comments(client, "CODEIT-1", since=datetime(2026, 9, 25, 0, tzinfo=UTC))
    assert [c.body_md for c in comments] == ["c2", "c3"]
    assert comments[0].author == "Onix"
    assert comments[0].updated == comments[0].created
    assert route.calls[1].request.url.params["startAt"] == "2"


async def test_list_comments_empty(mock: respx.MockRouter, client: JiraClient) -> None:
    mock.get("/issue/CODEIT-1/comment").respond(json={"comments": [], "total": 0})
    assert await list_comments(client, "CODEIT-1") == []
