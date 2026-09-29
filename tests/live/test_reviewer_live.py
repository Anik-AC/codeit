"""M6 acceptance, live (PRD 23): the Reviewer on real PRs in the sandbox repo.

- A PR with a failing test goes back to Ready for Dev with findings (Review Loop 1).
- The 3rd failure escalates to Human Review with `needs-human`.
- A tautological test is flagged critical (TESTS_DO_NOT_EXERCISE_CHANGE).
- A good PR passes to Human Review.

Opt-in on top of LIVE=1 (`CODEIT_LIVE_REVIEWER=1`). Uses the owner's git/gh login to make
the PRs, Docker for phase 1, and one OpenRouter call per review. PRs, branches and tickets
it creates are removed at the end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from ulid import ULID

from codeit.agents.reviewer.checks import TAUTOLOGICAL
from codeit.agents.reviewer.run import run_reviewer
from codeit.config import Secrets, load_config
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.issues import (
    IssueSpec,
    create_issue,
    delete_issue,
    get_ticket,
    update_fields,
)
from codeit.jira_client.transitions import TransitionCache, transition_to
from tests.conftest import REPO_ROOT

CFG = load_config(REPO_ROOT / "config" / "config.yaml")
SECRETS = Secrets(_env_file=REPO_ROOT / ".env")
REPO_URL = CFG.project.target_repo.url

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CODEIT_LIVE_REVIEWER") != "1", reason="set CODEIT_LIVE_REVIEWER=1"
    ),
    pytest.mark.skipif(shutil.which("gh") is None, reason="needs gh"),
]

TICKET = """**User story:** As a user, I want an endpoint with the number of tasks, so that other
tools can show my workload.

## Acceptance criteria

- [ ] Given there are 3 tasks, When I call GET /api/tasks/count, Then it returns 200 with
  `{"count": 3}`.
- [ ] Given there are no tasks, When I call GET /api/tasks/count, Then it returns `{"count": 0}`.
"""

ROUTE = """
  app.get("/api/tasks/count", (_req, res) => {
    res.json({ count: listTasks(db).length });
  });
"""

GOOD_TEST = """import request from "supertest";
import { describe, expect, it } from "vitest";
import { createApp } from "../../../src/server/app.ts";
import { openDb, seed } from "../../../src/server/db.ts";

describe("GET /api/tasks/count", () => {
  it("counts the seeded tasks", async () => {
    const db = openDb(":memory:");
    seed(db);
    const res = await request(createApp(db)).get("/api/tasks/count");
    expect(res.status).toBe(200);
    expect(res.body).toEqual({ count: 3 });
  });

  it("returns 0 when there are no tasks", async () => {
    const res = await request(createApp(openDb(":memory:"))).get("/api/tasks/count");
    expect(res.body).toEqual({ count: 0 });
  });
});
"""

FAILING_TEST = GOOD_TEST.replace("{ count: 3 }", "{ count: 4 }")
TAUTOLOGICAL_TEST = """import { describe, expect, it } from "vitest";

describe("task count", () => {
  it("works", () => {
    expect(true).toBe(true);
  });
});
"""


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


Make = Callable[[str, str], Awaitable[str]]


@pytest.fixture
async def make_ticket_with_pr(
    jira: JiraClient, ids: JiraIds, project_key: str, tmp_path: Path
) -> AsyncIterator[Make]:
    """Create a PR (the count endpoint plus `test_body`) and a ticket in Agent Review."""
    made: list[tuple[str, str, int]] = []
    clone = tmp_path / "app"
    sh("git", "clone", "--quiet", REPO_URL, str(clone), cwd=tmp_path)

    async def make(name: str, test_body: str) -> str:
        branch = f"codeit-live-review-{name}-{str(ULID())[-6:].lower()}"
        sh("git", "checkout", "--quiet", "-B", branch, "origin/main", cwd=clone)
        app_ts = clone / "src/server/app.ts"
        text = app_ts.read_text()
        app_ts.write_text(
            text.replace(
                '  app.get("/api/tasks", ', ROUTE.lstrip("\n") + '\n  app.get("/api/tasks", ', 1
            )
        )
        (clone / "tests/unit/server/count.test.ts").write_text(test_body)
        sh("git", "add", "-A", cwd=clone)
        sh(
            "git",
            "-c",
            "user.name=codeit-live",
            "-c",
            "user.email=live@example.com",
            "commit",
            "--quiet",
            "-m",
            f"live review test: {name}",
            cwd=clone,
        )
        sh("git", "push", "--quiet", "-u", "origin", branch, cwd=clone)
        url = sh(
            "gh",
            "pr",
            "create",
            "--repo",
            "Anik-AC/codeit-sandbox-app",
            "--base",
            "main",
            "--head",
            branch,
            "--title",
            f"[live review test] {name}",
            "--body",
            "Temporary PR from CodeIt's Reviewer live test. Closed automatically.",
            cwd=clone,
        )
        key = await create_issue(
            jira,
            IssueSpec(
                project_key=project_key,
                issue_type_id=ids.issue_type_id("Story"),
                summary=f"[live review test] {name}: task count endpoint",
                description_md=TICKET,
                labels=["codeit-live-test"],
            ),
        )
        await update_fields(jira, key, ids.fields.ids(pr_url=url))
        await transition_to(jira, key, "Agent Review", TransitionCache(ids.transitions))
        made.append((key, branch, int(url.rsplit("/", 1)[-1])))
        return key

    yield make
    for key, _branch, number in made:
        subprocess.run(  # noqa: ASYNC221 (test cleanup)
            [
                "gh",
                "pr",
                "close",
                str(number),
                "--repo",
                "Anik-AC/codeit-sandbox-app",
                "--delete-branch",
            ],
            capture_output=True,
            check=False,
        )
        await delete_issue(jira, key)


async def test_failing_test_bounces_then_escalates(
    jira: JiraClient, ids: JiraIds, make_ticket_with_pr: Make
) -> None:
    key = await make_ticket_with_pr("failing", FAILING_TEST)
    first = await run_reviewer(CFG, SECRETS, ids, key)
    assert first.phase1.get("unit") is not None and first.phase1.get("unit").status == "fail"  # type: ignore[union-attr]
    assert first.verdict is not None and first.verdict.verdict == "fail_critical"
    assert first.routing.route == "ready_for_dev" and first.review_url
    ticket = await get_ticket(jira, key, ids.fields)
    assert (ticket.status, ticket.review_loop) == ("Ready for Dev", 1)

    # Pretend two more rounds happened: back in Agent Review with Review Loop 2.
    cache = TransitionCache(ids.transitions)
    await update_fields(jira, key, ids.fields.ids(review_loop=2))
    await transition_to(jira, key, "Agent Review", cache)
    third = await run_reviewer(CFG, SECRETS, ids, key)
    assert third.routing.route == "human_review" and third.routing.needs_human
    ticket = await get_ticket(jira, key, ids.fields)
    assert (ticket.status, ticket.review_loop) == ("Human Review", 3)
    assert "needs-human" in ticket.labels


async def test_tautological_test_is_critical(
    jira: JiraClient, ids: JiraIds, make_ticket_with_pr: Make
) -> None:
    key = await make_ticket_with_pr("tautological", TAUTOLOGICAL_TEST)
    result = await run_reviewer(CFG, SECRETS, ids, key)
    check = result.phase1.get("new_tests_fail_on_base")
    assert check is not None and check.status == "fail" and TAUTOLOGICAL in check.note
    assert result.phase1.get("unit").status == "pass"  # type: ignore[union-attr]
    assert result.verdict is not None and result.verdict.verdict == "fail_critical"
    assert any(
        f.severity == "critical" and "new_tests_fail_on_base" in f.issue
        for f in result.verdict.findings
    )


async def test_good_pr_goes_to_human(
    jira: JiraClient, ids: JiraIds, make_ticket_with_pr: Make
) -> None:
    key = await make_ticket_with_pr("good", GOOD_TEST)
    result = await run_reviewer(CFG, SECRETS, ids, key)
    statuses = {c.name: c.status for c in result.phase1.checks}
    assert all(
        statuses[n] == "pass"
        for n in ("install", "lint", "typecheck", "unit", "e2e", "new_tests_fail_on_base")
    ), statuses
    print(
        f"\nverdict: {result.verdict.verdict if result.verdict else None}, route: {result.routing}"
    )
    assert result.verdict is not None
    if result.verdict.verdict != "fail_critical":
        assert (await get_ticket(jira, key, ids.fields)).status == "Human Review"
