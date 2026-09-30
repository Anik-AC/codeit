from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy import Engine

from codeit.github_client import GitHubClient
from codeit.jira_client import JiraClient
from codeit.orchestrator.merge_watcher import MergeWatcher
from codeit.sandbox.clone import CloneManager
from tests.unit.orchestrator.fake_jira import IDS, FakeJira

SLUG = "Anik-AC/codeit-sandbox-app"
PR = f"https://github.com/{SLUG}/pull/"


def pr_json(number: int, *, merged: bool, state: str = "closed") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"{PR}{number}",
        "state": state,
        "merged": merged,
        "merge_commit_sha": "abc123def4567890" if merged else None,
        "head": {"ref": "b", "sha": "HEAD"},
        "base": {"ref": "main", "sha": "BASE"},
    }


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        router.get(f"/repos/{SLUG}/pulls/1").respond(json=pr_json(1, merged=True))
        router.get(f"/repos/{SLUG}/pulls/2").respond(json=pr_json(2, merged=False, state="open"))
        router.get(f"/repos/{SLUG}/pulls/3").respond(json=pr_json(3, merged=False))
        yield router


@pytest.fixture
async def gh(github: respx.MockRouter) -> AsyncIterator[GitHubClient]:
    async with GitHubClient("t") as client:
        yield client


async def test_merged_goes_done_and_unmerged_done_is_flagged(
    engine: Engine,
    jira: JiraClient,
    gh: GitHubClient,
    fake_jira: FakeJira,
    tmp_path: Path,
) -> None:
    fake_jira.add("CODEIT-1", "Human Review", pr_url=f"{PR}1")  # merged
    fake_jira.add("CODEIT-2", "Human Review", pr_url=f"{PR}2")  # still open
    fake_jira.add("CODEIT-3", "Human Review")  # no PR
    fake_jira.add("CODEIT-4", "Done", pr_url=f"{PR}3")  # closed without merging
    clones = CloneManager(tmp_path, "app", "https://x")
    for name in ("CODEIT-1", "CODEIT-1-review"):
        (clones.clones / name).mkdir(parents=True)

    watcher = MergeWatcher(engine, jira, gh, IDS, "CODEIT", SLUG, clones)
    actions = await watcher.tick()
    assert [(a.key, a.action) for a in actions] == [
        ("CODEIT-1", "done"),
        ("CODEIT-4", "state_mismatch"),
    ]
    assert fake_jira.moves == [("CODEIT-1", "Done")]
    assert "abc123def456" in fake_jira.issues["CODEIT-1"].comments[0]
    assert not (clones.clones / "CODEIT-1").exists()
    assert not (clones.clones / "CODEIT-1-review").exists()
    assert fake_jira.issues["CODEIT-4"].labels == ["state-mismatch"]
    assert fake_jira.issues["CODEIT-4"].status == "Done"  # nothing else changes

    # CODEIT-1 is Done now and merged; CODEIT-4 is labelled: a second tick does nothing.
    assert await watcher.tick() == []
