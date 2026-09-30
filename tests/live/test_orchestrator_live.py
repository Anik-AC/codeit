"""M7 acceptance, live (PRD 23): `codeit up` runs the pipeline unattended.

One scenario, about 20 to 40 minutes, with real Jira, GitHub, Docker, Claude and OpenRouter:

1. Tickets A and B go to Ready for Dev; `codeit up --any-time` starts.
2. While the Coder works on A, the process is killed (SIGKILL). The container, lease and
   `In Dev` ticket are left behind, as after a crash.
3. A second `codeit up` recovers: the dead run is abandoned, its container removed and A
   retried. A and B both reach Human Review with no human help.
4. Meanwhile ticket C (a PR with a failing test) is in Agent Review. While its review runs,
   the test moves C to Human Review by hand; the Reviewer must not move it back.
5. Ctrl-C (SIGINT) stops the orchestrator cleanly.

The merge step (a merged PR moves its ticket to Done) is left to the owner: merge A's or
B's PR and the next poll moves the ticket. C's PR, branch and ticket are removed at the
end; A's and B's are kept for review.

Opt-in on top of LIVE=1: `CODEIT_LIVE_ORCHESTRATOR=1`. Runs in the real `data/` directory.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import docker
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeit import db
from codeit.config import Secrets, load_config
from codeit.db.models import Run
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.issues import (
    IssueSpec,
    create_issue,
    delete_issue,
    get_ticket,
    update_fields,
)
from codeit.jira_client.transitions import TransitionCache, transition_to
from codeit.orchestrator.leases import LeaseStore
from tests.conftest import REPO_ROOT
from tests.live.test_reviewer_live import FAILING_TEST, ROUTE, TICKET, sh

CFG = load_config(REPO_ROOT / "config" / "config.yaml")
SECRETS = Secrets(_env_file=REPO_ROOT / ".env")
SANDBOX_REPO = "Anik-AC/codeit-sandbox-app"

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("CODEIT_LIVE_ORCHESTRATOR") != "1", reason="set CODEIT_LIVE_ORCHESTRATOR=1"
    ),
]

HEALTH = """**User story:** As an API client, I want to fetch one task by its id, so that I
can show a task's details.

## Acceptance criteria

- [ ] Given a task with id 1 exists, When I call GET /api/tasks/1, Then it returns 200 with
  that task.
- [ ] Given no task with id 999, When I call GET /api/tasks/999, Then it returns 404.
"""

VERSION = """**User story:** As an operator, I want a ping endpoint, so that a load balancer can
check the API is alive.

## Acceptance criteria

- [ ] Given the API is running, When I call GET /api/ping, Then it returns 200 with
  `{"pong": true}`.
"""


class Orchestrator:
    """`codeit up` in its own process group, so a kill takes `uv` and Python with it."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        out = self.log.open("ab")
        self.proc = subprocess.Popen(
            ["uv", "run", "codeit", "up", "--any-time"],
            cwd=REPO_ROOT,
            stdout=out,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )

    def signal(self, sig: int) -> None:
        assert self.proc is not None
        os.killpg(self.proc.pid, sig)

    def wait(self, timeout: float) -> int:
        assert self.proc is not None
        return self.proc.wait(timeout)

    def tail(self, n: int = 40) -> str:
        return "\n".join(self.log.read_text(errors="replace").splitlines()[-n:])


async def wait_for(
    what: str, check: Callable[[], Awaitable[bool]], timeout_s: float, every_s: float = 10
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(every_s)
    raise AssertionError(f"timed out after {timeout_s:.0f}s waiting for {what}")


def coder_containers() -> list[str]:
    client = docker.from_env()
    return [
        str(c.labels.get("codeit.run_id"))
        for c in client.containers.list(filters={"label": "codeit.role=coder"})
    ]


@pytest.fixture
async def made(jira: JiraClient) -> AsyncIterator[dict[str, object]]:
    """Things to clean up: C's ticket and PR."""
    state: dict[str, object] = {}
    yield state
    if "c_pr" in state:
        subprocess.run(  # noqa: ASYNC221 (test cleanup)
            ["gh", "pr", "close", str(state["c_pr"]), "--repo", SANDBOX_REPO, "--delete-branch"],
            capture_output=True,
            check=False,
        )
    if "c_key" in state:
        await delete_issue(jira, str(state["c_key"]))


async def story(jira: JiraClient, ids: JiraIds, project_key: str, summary: str, body: str) -> str:
    return await create_issue(
        jira,
        IssueSpec(
            project_key=project_key,
            issue_type_id=ids.issue_type_id("Story"),
            summary=summary,
            description_md=body,
            labels=["codeit-live-test"],
        ),
    )


async def test_unattended_pipeline(
    jira: JiraClient,
    ids: JiraIds,
    project_key: str,
    tmp_path: Path,
    made: dict[str, object],
) -> None:
    engine = db.make_engine(REPO_ROOT / CFG.db_path)
    leases = LeaseStore(engine)
    cache = TransitionCache(ids.transitions)

    async def status(key: str) -> str:
        return (await get_ticket(jira, key, ids.fields)).status

    a = await story(jira, ids, project_key, "Add an endpoint for a single task", HEALTH)
    b = await story(jira, ids, project_key, "Add a ping endpoint", VERSION)
    for key in (a, b):
        await transition_to(jira, key, "Ready for Dev", cache)
    print(f"\nA={a} B={b}")

    orch = Orchestrator(tmp_path / "up.log")
    try:
        # 1-2. Start, let the Coder get going on A, then kill the whole process group.
        orch.start()
        await wait_for(f"{a} In Dev", lambda: _eq(status(a), "In Dev"), 300)
        await wait_for("a coder container", lambda: _true(bool(coder_containers())), 180, 5)
        await asyncio.sleep(45)
        first_run = (await get_ticket(jira, a, ids.fields)).run_id
        orch.signal(signal.SIGKILL)
        orch.wait(30)
        assert first_run in coder_containers(), "the killed run's container should linger"
        assert leases.all() and await status(a) == "In Dev"
        print(f"killed mid-run {first_run}")

        # 4. Ticket C: a PR with a failing test, waiting for review.
        clone = tmp_path / "app"
        sh("git", "clone", "--quiet", CFG.project.target_repo.url, str(clone), cwd=tmp_path)
        branch = f"codeit-live-orch-{first_run[-6:].lower()}"
        sh("git", "checkout", "--quiet", "-B", branch, "origin/main", cwd=clone)
        app_ts = clone / "src/server/app.ts"
        app_ts.write_text(
            app_ts.read_text().replace(
                '  app.get("/api/tasks", ', ROUTE.lstrip("\n") + '\n  app.get("/api/tasks", ', 1
            )
        )
        (clone / "tests/unit/server/count.test.ts").write_text(FAILING_TEST)
        sh("git", "add", "-A", cwd=clone)
        sh("git", "-c", "user.name=codeit-live", "-c", "user.email=live@example.com",
           "commit", "--quiet", "-m", "live orchestrator test", cwd=clone)  # fmt: skip
        sh("git", "push", "--quiet", "-u", "origin", branch, cwd=clone)
        pr_url = sh("gh", "pr", "create", "--repo", SANDBOX_REPO, "--base", "main",
                    "--head", branch, "--title", "[live orchestrator test] manual move",
                    "--body", "Temporary PR from CodeIt's M7 live test.", cwd=clone)  # fmt: skip
        made["c_pr"] = int(pr_url.rsplit("/", 1)[-1])
        c = await story(jira, ids, project_key, "[live orchestrator test] task count", TICKET)
        made["c_key"] = c
        await update_fields(jira, c, ids.fields.ids(pr_url=pr_url))
        await transition_to(jira, c, "Agent Review", cache)

        # 3. Restart: recovery, then the rest runs unattended.
        orch.start()
        await wait_for(f"review of {c} to start", lambda: _true(_leased(leases, c)), 300, 3)
        await transition_to(jira, c, "Human Review", cache)  # the human steps in
        print(f"moved {c} to Human Review during its review")

        def abandoned() -> bool:
            with Session(engine) as s:
                run = s.get(Run, first_run)
                return run is not None and run.status == "abandoned"

        await wait_for("the dead run to be reaped", lambda: _true(abandoned()), 420)
        assert first_run not in coder_containers()
        await wait_for(f"{c}'s review to finish", lambda: _true(not _leased(leases, c)), 900)
        c_ticket = await get_ticket(jira, c, ids.fields)
        assert c_ticket.status == "Human Review" and c_ticket.review_loop == 0
        assert "needs-human" not in c_ticket.labels

        async def both_done() -> bool:
            return await status(a) == "Human Review" and await status(b) == "Human Review"

        await wait_for(f"{a} and {b} in Human Review", both_done, 50 * 60, 20)
        for key in (a, b):
            t = await get_ticket(jira, key, ids.fields)
            print(f"{key}: {t.status}, PR {t.pr_url}, loop {t.review_loop}, labels {t.labels}")
            assert t.pr_url

        with Session(engine) as s:
            runs = s.scalars(select(Run).where(Run.ticket_key.in_([a, b])).order_by(Run.started_at))
            for r in runs:
                print(f"  {r.ticket_key} {r.role:<8} {r.status:<14} {r.id}")

        # 5. Clean stop.
        orch.signal(signal.SIGINT)
        assert orch.wait(120) in (0, -signal.SIGINT)
        assert "orchestrator stopped" in orch.tail()
    except BaseException:
        print(orch.tail(80))
        raise
    finally:
        if orch.proc is not None and orch.proc.poll() is None:
            orch.signal(signal.SIGKILL)


def _leased(leases: LeaseStore, key: str) -> bool:
    return leases.holder(key) is not None


async def _true(value: bool) -> bool:
    return value


async def _eq(got: Awaitable[str], want: str) -> bool:
    return await got == want
