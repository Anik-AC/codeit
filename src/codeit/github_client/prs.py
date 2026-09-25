"""Pull request reads the orchestrator needs (PRD 8): find, inspect, collect feedback."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from codeit.github_client.client import GitHubClient, GitHubNotFound


class PullRequest(BaseModel):
    number: int
    url: str
    state: str  # open | closed
    merged: bool
    head_ref: str
    head_sha: str
    base_ref: str
    base_sha: str
    mergeable_state: str | None = None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> PullRequest:
        return cls(
            number=raw["number"],
            url=raw["html_url"],
            state=raw["state"],
            merged=bool(raw.get("merged") or raw.get("merged_at")),
            head_ref=raw["head"]["ref"],
            head_sha=raw["head"]["sha"],
            base_ref=raw["base"]["ref"],
            base_sha=raw["base"]["sha"],
            mergeable_state=raw.get("mergeable_state"),
        )


class PRComment(BaseModel):
    kind: Literal["review", "review_comment", "comment"]
    author: str
    body: str
    created_at: datetime
    path: str | None = None
    line: int | None = None
    state: str | None = None  # reviews: APPROVED | CHANGES_REQUESTED | COMMENTED


async def find_pr(gh: GitHubClient, repo: str, branch: str) -> PullRequest | None:
    """The most recent PR (open first) whose head is `branch` in the same repo."""
    owner = repo.split("/")[0]
    for state in ("open", "all"):
        found = await gh.get_json(
            f"/repos/{repo}/pulls", {"head": f"{owner}:{branch}", "state": state, "per_page": 10}
        )
        if found:
            return await get_pr(gh, repo, int(found[0]["number"]))
    return None


async def get_pr(gh: GitHubClient, repo: str, number: int) -> PullRequest:
    return PullRequest.from_api(await gh.get_json(f"/repos/{repo}/pulls/{number}"))


async def branch_head(gh: GitHubClient, repo: str, branch: str) -> str | None:
    try:
        data = await gh.get_json(f"/repos/{repo}/branches/{branch}")
    except GitHubNotFound:
        return None
    return str(data["commit"]["sha"])


async def pr_feedback(
    gh: GitHubClient, repo: str, number: int, since: datetime | None = None
) -> list[PRComment]:
    """Reviews, inline review comments and conversation comments, oldest first."""
    out: list[PRComment] = []
    for raw in await gh.get_all(f"/repos/{repo}/pulls/{number}/reviews"):
        if raw.get("body") and raw.get("submitted_at"):
            out.append(
                PRComment(
                    kind="review",
                    author=_login(raw),
                    body=raw["body"],
                    created_at=raw["submitted_at"],
                    state=raw.get("state"),
                )
            )
    for raw in await gh.get_all(f"/repos/{repo}/pulls/{number}/comments"):
        out.append(
            PRComment(
                kind="review_comment",
                author=_login(raw),
                body=raw["body"],
                created_at=raw["created_at"],
                path=raw.get("path"),
                line=raw.get("line") or raw.get("original_line"),
            )
        )
    for raw in await gh.get_all(f"/repos/{repo}/issues/{number}/comments"):
        out.append(
            PRComment(
                kind="comment", author=_login(raw), body=raw["body"], created_at=raw["created_at"]
            )
        )
    if since is not None:
        out = [c for c in out if c.created_at > since]
    return sorted(out, key=lambda c: c.created_at)


def _login(raw: dict[str, Any]) -> str:
    return str((raw.get("user") or {}).get("login") or "unknown")
