from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import respx

from codeit.github_client import GitHubClient, GitHubError, GitHubNotFound, repo_slug
from codeit.github_client.prs import branch_head, find_pr, get_pr, pr_feedback

API = "https://api.github.com"
REPO = "Anik-AC/codeit-sandbox-app"


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API) as router:
        yield router


@pytest.fixture
async def gh() -> AsyncIterator[GitHubClient]:
    async def no_sleep(_: float) -> None:
        return None

    async with GitHubClient("ghp_x", sleep=no_sleep) as client:
        yield client


def pr_json(number: int = 7, state: str = "open", sha: str = "abc") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com/{REPO}/pull/{number}",
        "state": state,
        "merged_at": None,
        "head": {"ref": "CODEIT-5-thing", "sha": sha},
        "base": {"ref": "main", "sha": "base1"},
        "mergeable_state": "clean",
    }


def test_repo_slug() -> None:
    assert repo_slug("https://github.com/Anik-AC/codeit-sandbox-app.git") == REPO
    assert repo_slug("git@github.com:Anik-AC/codeit-sandbox-app.git") == REPO
    assert repo_slug("https://github.com/a/b/") == "a/b"
    with pytest.raises(ValueError):
        repo_slug("https://gitlab.com/a/b")


async def test_find_pr_prefers_open(mock: respx.MockRouter, gh: GitHubClient) -> None:
    listing = mock.get(f"/repos/{REPO}/pulls").mock(
        return_value=httpx.Response(200, json=[{"number": 7}])
    )
    mock.get(f"/repos/{REPO}/pulls/7").respond(json=pr_json())
    pr = await find_pr(gh, REPO, "CODEIT-5-thing")
    assert pr is not None and (pr.number, pr.head_sha, pr.merged) == (7, "abc", False)
    params = listing.calls.last.request.url.params
    assert (params["head"], params["state"]) == ("Anik-AC:CODEIT-5-thing", "open")
    assert listing.calls.last.request.headers["Authorization"] == "Bearer ghp_x"


async def test_find_pr_falls_back_to_closed_then_none(
    mock: respx.MockRouter, gh: GitHubClient
) -> None:
    mock.get(f"/repos/{REPO}/pulls").mock(
        side_effect=[httpx.Response(200, json=[]), httpx.Response(200, json=[])]
    )
    assert await find_pr(gh, REPO, "x") is None


async def test_get_pr_merged(mock: respx.MockRouter, gh: GitHubClient) -> None:
    raw = pr_json(state="closed")
    raw["merged_at"] = "2026-09-25T10:00:00Z"
    mock.get(f"/repos/{REPO}/pulls/7").respond(json=raw)
    pr = await get_pr(gh, REPO, 7)
    assert pr.merged and pr.state == "closed"


async def test_branch_head(mock: respx.MockRouter, gh: GitHubClient) -> None:
    mock.get(f"/repos/{REPO}/branches/b1").respond(json={"commit": {"sha": "s1"}})
    mock.get(f"/repos/{REPO}/branches/none").respond(404, json={"message": "Branch not found"})
    assert await branch_head(gh, REPO, "b1") == "s1"
    assert await branch_head(gh, REPO, "none") is None


async def test_pr_feedback_merges_sorts_and_filters(
    mock: respx.MockRouter, gh: GitHubClient
) -> None:
    user = {"login": "onix"}
    mock.get(f"/repos/{REPO}/pulls/7/reviews").respond(
        json=[
            {
                "user": user,
                "body": "Please rename",
                "state": "CHANGES_REQUESTED",
                "submitted_at": "2026-09-25T10:00:00Z",
            },
            {"user": user, "body": "", "state": "APPROVED", "submitted_at": "2026-09-25T11:00:00Z"},
        ]
    )
    mock.get(f"/repos/{REPO}/pulls/7/comments").respond(
        json=[
            {
                "user": user,
                "body": "typo",
                "created_at": "2026-09-25T09:00:00Z",
                "path": "a.ts",
                "line": 3,
            }
        ]
    )
    mock.get(f"/repos/{REPO}/issues/7/comments").respond(
        json=[{"user": user, "body": "old", "created_at": "2026-09-20T09:00:00Z"}]
    )
    items = await pr_feedback(gh, REPO, 7, since=datetime(2026, 9, 21, tzinfo=UTC))
    assert [(c.kind, c.body) for c in items] == [
        ("review_comment", "typo"),
        ("review", "Please rename"),
    ]
    assert (items[0].path, items[0].line) == ("a.ts", 3)
    assert items[1].state == "CHANGES_REQUESTED"


async def test_paging_follows_link(mock: respx.MockRouter, gh: GitHubClient) -> None:
    mock.get(f"/repos/{REPO}/issues/7/comments").mock(
        side_effect=[
            httpx.Response(200, json=[{"n": 1}], headers={"Link": f'<{API}/page2>; rel="next"'}),
        ]
    )
    mock.get("/page2").respond(json=[{"n": 2}])
    assert await gh.get_all(f"/repos/{REPO}/issues/7/comments") == [{"n": 1}, {"n": 2}]


async def test_retries_then_errors(mock: respx.MockRouter, gh: GitHubClient) -> None:
    route = mock.get("/x").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json={"ok": 1})]
    )
    assert await gh.get_json("/x") == {"ok": 1}
    assert route.call_count == 2
    mock.get("/forbidden").respond(403, json={"message": "Resource not accessible"})
    with pytest.raises(GitHubError, match="Resource not accessible") as err:
        await gh.get_json("/forbidden")
    assert err.value.status == 403 and not isinstance(err.value, GitHubNotFound)
    mock.get("/down").mock(side_effect=httpx.ConnectError("x"))
    with pytest.raises(GitHubError, match="GET /down"):
        await gh.get_json("/down")


async def test_check_runs(mock: respx.MockRouter, gh: GitHubClient) -> None:
    from codeit.github_client.prs import check_runs

    mock.get(f"/repos/{REPO}/commits/abc/check-runs").respond(
        json={
            "check_runs": [
                {"name": "checks", "status": "completed", "conclusion": "success"},
                {"name": "e2e", "status": "in_progress", "conclusion": None},
            ]
        }
    )
    runs = await check_runs(gh, REPO, "abc")
    assert [(r.name, r.status, r.conclusion) for r in runs] == [
        ("checks", "completed", "success"),
        ("e2e", "in_progress", None),
    ]
