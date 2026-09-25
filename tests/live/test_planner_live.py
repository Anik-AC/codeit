"""Live Planner acceptance (PRD 11.1, M3): the sample plan becomes real tickets.

Uses Claude Code on the owner's subscription (one call) and the owner's Jira site. Every
issue the run creates is deleted afterwards.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from codeit.agents.planner_run import run_planner
from codeit.config import Secrets, load_config
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.issues import delete_issue, get_issue, get_ticket
from tests.conftest import REPO_ROOT

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed"),
]

SAMPLE = REPO_ROOT / "docs" / "samples" / "sample-plan.md"


async def test_sample_plan_creates_linked_agent_drafts(
    jira: JiraClient, ids: JiraIds, tmp_path: Path
) -> None:
    cfg = load_config(REPO_ROOT / "config" / "config.yaml")
    cfg = cfg.model_copy(update={"data_dir": tmp_path / "data"})
    outcome = await run_planner(
        cfg,
        Secrets(_env_file=REPO_ROOT / ".env"),
        ids,
        SAMPLE,
        dry_run=False,
        repo_path=tmp_path / "no-repo",
        echo=lambda _: None,
    )
    created = outcome.record.created
    assert created is not None
    try:
        drafts = {s.ref: s for s in outcome.record.draft.stories}
        assert len(drafts) >= 5
        assert sum(len(s.depends_on) for s in drafts.values()) >= 1, "no dependencies planned"
        epic = await get_issue(jira, created.epic_key, ["status", "labels", "issuetype"])
        assert epic["fields"]["issuetype"]["name"] == "Epic"
        for ref, key in created.stories.items():
            ticket = await get_ticket(jira, key, ids.fields)
            assert ticket.status == "Agent Draft", key
            assert "agent-draft" in ticket.labels
            assert ticket.epic_key == created.epic_key
            assert ticket.story_points is None and ticket.priority in (None, "Medium")
            expected = sorted(created.stories[d] for d in drafts[ref].depends_on)
            assert sorted(ticket.blocked_by) == expected, key
            assert ("split-me" in ticket.labels) == drafts[ref].needs_split
    finally:
        for key in [*created.stories.values(), created.epic_key]:
            await delete_issue(jira, key)
