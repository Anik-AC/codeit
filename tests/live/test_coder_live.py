"""M5 acceptance, live (PRD 23): a small sandbox ticket goes to a PR with green CI; the ticket
ends in Agent Review with PR URL set; a rework run updates the same PR.

Opt-in on top of LIVE=1 (`CODEIT_LIVE_CODER=1`): two full Coder runs on the subscription,
several minutes each. The ticket and PR are left in place for the owner to review.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from codeit.agents.coder_run import run_coder
from codeit.config import Secrets, load_config
from codeit.github_client import GitHubClient, repo_slug
from codeit.github_client.prs import CheckRun, check_runs, get_pr
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment
from codeit.jira_client.issues import IssueSpec, create_issue, get_ticket
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.sandbox.containers import Sandbox, image_exists
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")
SECRETS = Secrets(_env_file=REPO_ROOT / ".env")

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CODEIT_LIVE_CODER") != "1", reason="set CODEIT_LIVE_CODER=1"
    ),
    pytest.mark.skipif(shutil.which("docker") is None, reason="needs Docker"),
]

TICKET = """**User story:** As a user, I want to see how many tasks I have, so that I know my
workload at a glance.

## Acceptance criteria

- [ ] Given there are 3 tasks, When the task list loads, Then the text "3 tasks" is shown
  directly under the "Tasks" heading.
- [ ] Given there is exactly 1 task, When the list loads, Then the text reads "1 task".
- [ ] Given there are no tasks, When the list loads, Then no count is shown and the empty
  state "No tasks yet." is unchanged.

## Test plan

**Unit**

- The count component renders "3 tasks", "1 task", and nothing for 0.

**End to end (Playwright)**

- With the demo data, the page shows "3 tasks" under the heading.

Suggested points: 1. Risk: low.
"""

REWORK = (
    'Please change the wording to "You have N tasks" (and "You have 1 task" for one), '
    "and keep hiding it when there are no tasks."
)


async def wait_for_ci(
    gh: GitHubClient, slug: str, sha: str, timeout_s: int = 900
) -> list[CheckRun]:
    """Poll until every check run on `sha` completes (at least two: checks and e2e)."""
    runs: list[CheckRun] = []
    for _ in range(timeout_s // 15):
        runs = await check_runs(gh, slug, sha)
        if len(runs) >= 2 and all(r.status == "completed" for r in runs):
            return runs
        await asyncio.sleep(15)
    raise AssertionError(f"CI did not finish on {sha}: {runs}")


async def test_ticket_to_green_pr_then_rework(
    jira: JiraClient, ids: JiraIds, project_key: str
) -> None:
    assert image_exists(Sandbox().client, CFG.sandbox.image), "run `codeit sandbox build` first"
    slug = repo_slug(CFG.project.target_repo.url)
    key = await create_issue(
        jira,
        IssueSpec(
            project_key=project_key,
            issue_type_id=ids.issue_type_id("Story"),
            summary="Show how many tasks there are",
            description_md=TICKET,
            labels=["codeit-live-test"],
        ),
    )
    cache = TransitionCache(ids.transitions)
    await transition_to(jira, key, "Ready for Dev", cache)

    first = await run_coder(CFG, SECRETS, ids, key, echo=print)
    assert first.outcome.status == "pr_opened", first.outcome.comment
    ticket = await get_ticket(jira, key, ids.fields)
    assert ticket.status == "Agent Review"
    assert ticket.pr_url == first.outcome.pr_url

    token = SECRETS.github_token_readonly
    async with GitHubClient(token.get_secret_value() if token else None) as gh:
        number = int(str(first.outcome.pr_url).rsplit("/", 1)[-1])
        pr = await get_pr(gh, slug, number)
        runs = await wait_for_ci(gh, slug, pr.head_sha)
        assert all(r.conclusion == "success" for r in runs), runs

        # The owner sends it back with a comment (PRD 6.2: Agent Review -> Ready for Dev).
        await add_comment(jira, key, REWORK)
        await transition_to(jira, key, "Ready for Dev", cache)
        second = await run_coder(CFG, SECRETS, ids, key, echo=print)
        assert second.outcome.status == "pr_updated", second.outcome.comment
        assert second.outcome.pr_url == first.outcome.pr_url  # the same PR
        updated = await get_pr(gh, slug, number)
        assert updated.head_sha != pr.head_sha and updated.state == "open"
        runs = await wait_for_ci(gh, slug, updated.head_sha)
        assert all(r.conclusion == "success" for r in runs), runs

    assert (await get_ticket(jira, key, ids.fields)).status == "Agent Review"
    print(f"\nLeft for review: {key} and {first.outcome.pr_url}")
