"""run_reviewer end to end with fakes: Jira and GitHub over respx, a fake clone and container."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import respx
from pydantic import SecretStr
from typer.testing import CliRunner

from codeit import db
from codeit.agents.reviewer import run as reviewer_run
from codeit.agents.reviewer.run import ReviewerError, run_reviewer
from codeit.backends.base import BackendUnavailable
from codeit.cli import app
from codeit.config import Config, Secrets, load_config
from codeit.jira_client import JiraIds
from codeit.orchestrator.leases import LeaseStore
from codeit.sandbox.containers import ExecResult
from tests.conftest import REPO_ROOT
from tests.unit.agents.conftest import FakeBackend
from tests.unit.jira.conftest import BASE, FIELDS, issue_json

GH = "https://api.github.com"
SLUG = "/repos/Anik-AC/codeit-sandbox-app"
KEY = "CODEIT-9"
PR_URL = "https://github.com/Anik-AC/codeit-sandbox-app/pull/12"
IDS = JiraIds(
    fields=FIELDS,
    statuses={},
    issue_types={"Story": "1"},
    transitions={"Ready for Dev": "2", "Agent Review": "4", "Human Review": "5"},
)
SECRETS = Secrets(
    _env_file=None,
    jira_base_url=BASE,
    jira_email="e",
    jira_api_token=SecretStr("j"),
    github_token_agent=SecretStr("gh-write"),
    github_token_readonly=SecretStr("gh-read"),
)
TICKET_MD = {
    "type": "doc",
    "version": 1,
    "content": [
        {
            "type": "heading",
            "attrs": {"level": 2},
            "content": [{"type": "text", "text": "Acceptance criteria"}],
        },
        {"type": "paragraph", "content": [{"type": "text", "text": "Given 3 tasks, count is 3"}]},
    ],
}
PASS = {
    "verdict": "pass",
    "ac_coverage": [{"criterion": "Given 3 tasks, count is 3", "status": "met"}],
    "findings": [],
    "summary_md": "Good.",
}
CODEIT_YAML = """commands:
  install: "npm ci"
  lint: "npm run lint"
  typecheck: "npm run typecheck"
  unit: "npm test -- --run"
  e2e: "npx playwright test"
test_globs: ["tests/**/*.test.ts"]
"""


class FakeGit:
    def __init__(self, changed: str) -> None:
        self.changed = changed

    async def run(self, *args: str, **kw: Any) -> str:
        if args[0] == "merge-base":
            return "BASE"
        if args[:2] == ("diff", "--name-only"):
            return self.changed
        if args[0] == "diff":
            return "diff --git a/src/app.ts b/src/app.ts\n+count"
        return ""


class FakeSandbox:
    def __init__(self, failing: tuple[str, ...] = ()) -> None:
        self.failing = failing
        self.commands: list[str] = []
        self.specs: list[Any] = []

    def start(self, spec: Any) -> Any:
        self.specs.append(spec)
        c = MagicMock()
        c.id = "cid"
        return c

    def stop(self, container: Any, log_path: Path | None = None) -> None:
        pass

    async def exec_lines(
        self, cid: str, argv: list[str], *, result: ExecResult, **kw: Any
    ) -> AsyncIterator[str]:
        command = argv[-1]
        self.commands.append(command)
        yield f"$ {command}"
        result.exit_code = 1 if any(command.startswith(f) for f in self.failing) else 0


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


@pytest.fixture(autouse=True)
def fake_clone(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeGit:
    workspace = tmp_path / "review"
    workspace.mkdir()
    (workspace / "codeit.yaml").write_text(CODEIT_YAML)
    git = FakeGit("src/app.ts\ntests/unit/count.test.ts")

    class FakeClones:
        def __init__(self, *a: Any, **kw: Any) -> None:
            self.git = git

        async def prepare_review(self, key: str, sha: str) -> Path:
            return workspace

    monkeypatch.setattr(reviewer_run, "CloneManager", FakeClones)
    return git


@pytest.fixture
def jira() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=f"{BASE}/rest/api/3", assert_all_called=False) as router:
        router.post(f"/issue/{KEY}/transitions").respond(204)
        router.put(f"/issue/{KEY}").respond(204)
        router.post(f"/issue/{KEY}/comment").respond(201, json={"id": "1"})
        router.get(f"/issue/{KEY}/comment").respond(json={"comments": [], "total": 0})
        yield router


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=GH, assert_all_called=False) as router:
        router.get(f"{SLUG}/pulls/12").respond(
            json={
                "number": 12,
                "html_url": PR_URL,
                "state": "open",
                "merged_at": None,
                "head": {"ref": "CODEIT-9-count", "sha": "HEADSHA"},
                "base": {"ref": "main", "sha": "b"},
            }
        )
        router.get(f"{SLUG}/commits/HEADSHA/check-runs").respond(
            json={
                "check_runs": [
                    {"name": "checks", "status": "completed", "conclusion": "success"},
                ]
            }
        )
        router.post(f"{SLUG}/pulls/12/reviews").respond(
            200, json={"html_url": f"{PR_URL}#review-1"}
        )
        yield router


def ticket(jira: respx.MockRouter, *statuses: str, loop: int = 0, pr: str | None = PR_URL) -> None:
    jira.get(f"/issue/{KEY}").mock(
        side_effect=[
            httpx.Response(
                200,
                json=issue_json(
                    KEY,
                    status={"name": s},
                    description=TICKET_MD,
                    **{FIELDS.review_loop: loop, FIELDS.pr_url: pr},
                ),
            )
            for s in statuses
        ]
    )


def sent(route: respx.Route) -> list[Any]:
    return [json.loads(c.request.content) for c in route.calls]


def moves(jira: respx.MockRouter) -> list[str]:
    names = {v: k for k, v in IDS.transitions.items()}
    return [names[b["transition"]["id"]] for b in sent(jira.routes[0])]


async def test_clean_pr_passes_to_human(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", "Agent Review")
    sandbox = FakeSandbox(failing=("npm test -- --run tests/",))  # the new test fails on the base
    backend = FakeBackend("rev", [PASS])
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=sandbox,
        backends=[backend],  # type: ignore[arg-type]
        ci_wait_s=0,
        echo=lambda _: None,
    )
    assert result.verdict is not None and result.verdict.verdict == "pass"
    assert (result.routing.route, result.review_url) == ("human_review", f"{PR_URL}#review-1")
    assert moves(jira) == ["Human Review"]
    assert [c.name for c in result.phase1.checks] == [
        "install",
        "lint",
        "typecheck",
        "unit",
        "e2e",
        "new_tests_fail_on_base",
        "ci_status",
    ]
    assert result.phase1.test_files == ["tests/unit/count.test.ts"]
    env = sandbox.specs[0].env
    assert sandbox.specs[0].role == "reviewer" and (env["GH_TOKEN"], env["CI"]) == ("gh-read", "1")
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env and env["HTTPS_PROXY"].endswith(":8888")
    review = github.routes[2].calls.last.request
    assert review.headers["Authorization"] == "Bearer gh-write"
    assert json.loads(review.content)["event"] == "COMMENT"
    jira_bodies = [b["body"]["content"][0]["content"][0]["text"] for b in sent(jira.routes[2])]
    assert jira_bodies[0].startswith("Picked up by reviewer-1")
    assert "Review: pass" in jira_bodies[1]
    assert LeaseStore(db.make_engine(cfg.db_path)).holder(KEY) is None


async def test_failing_unit_goes_back_with_loop(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", "Agent Review", loop=1)
    sandbox = FakeSandbox(failing=("npm test",))
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=sandbox,
        backends=[FakeBackend("rev", [PASS])],  # type: ignore[arg-type]
        ci_wait_s=0,
        echo=lambda _: None,
    )
    assert result.verdict is not None and result.verdict.verdict == "fail_critical"
    assert (result.routing.route, result.routing.review_loop) == ("ready_for_dev", 2)
    assert moves(jira) == ["Ready for Dev"]
    assert {"fields": {FIELDS.review_loop: 2}} in sent(jira.routes[1])


async def test_loop_cap_escalates(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", "Agent Review", loop=2)
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=FakeSandbox(failing=("npm run typecheck",)),  # type: ignore[arg-type]
        backends=[FakeBackend("rev", [PASS])],
        ci_wait_s=0,
        echo=lambda _: None,
    )
    assert result.routing.needs_human and result.routing.review_loop == 3
    assert moves(jira) == ["Human Review"]
    assert {"update": {"labels": [{"add": "needs-human"}]}} in sent(jira.routes[1])


async def test_no_model_and_clean_checks_goes_to_human(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", "Agent Review")
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=FakeSandbox(failing=("npm test -- --run tests/",)),  # type: ignore[arg-type]
        backends=[FakeBackend("rev", [BackendUnavailable("down")])],
        ci_wait_s=0,
        echo=lambda _: None,
    )
    assert result.verdict is None and result.routing.needs_human
    assert moves(jira) == ["Human Review"]


async def test_closed_pr_escalates(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", pr=None)
    github.get(f"{SLUG}/pulls").respond(json=[])  # nothing on the branch either
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=FakeSandbox(),
        backends=[],
        ci_wait_s=0,  # type: ignore[arg-type]
        echo=lambda _: None,
    )
    assert result.routing.reason == "no pull request" and moves(jira) == ["Human Review"]


async def test_human_moved_ticket(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", "Human Review")
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=FakeSandbox(failing=("npm test -- --run tests/",)),  # type: ignore[arg-type]
        backends=[FakeBackend("rev", [PASS])],
        ci_wait_s=0,
        echo=lambda _: None,
    )
    assert not result.applied and moves(jira) == []


async def test_wrong_status_and_lease(cfg: Config, jira: respx.MockRouter) -> None:
    ticket(jira, "In Dev")
    with pytest.raises(ReviewerError, match="not 'Agent Review'"):
        await run_reviewer(cfg, SECRETS, IDS, KEY, sandbox=FakeSandbox(), echo=lambda _: None)  # type: ignore[arg-type]
    LeaseStore(db.make_engine(cfg.db_path)).acquire(
        KEY, "coder", "coder-1", "X", timedelta(minutes=5)
    )
    with pytest.raises(ReviewerError, match="leased by coder-1"):
        await run_reviewer(cfg, SECRETS, IDS, KEY, echo=lambda _: None)
    with pytest.raises(ReviewerError, match="GITHUB_TOKEN_AGENT"):
        await run_reviewer(cfg, Secrets(_env_file=None), IDS, KEY)


def test_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from codeit.agents.reviewer.checks import Phase1
    from codeit.agents.reviewer.run import ReviewRun
    from codeit.agents.reviewer.verdict import Routing
    from codeit.jira_client.discover import write_ids

    async def fake(*a: Any, **kw: Any) -> ReviewRun:
        return ReviewRun("R", KEY, None, Phase1(), Routing("human_review", 0, True, "x"), "u", True)

    monkeypatch.setattr(reviewer_run, "run_reviewer", fake)
    ids = tmp_path / "ids.yaml"
    write_ids(IDS, ids)
    result = CliRunner().invoke(app, ["run", "reviewer", "codeit-9", "--ids", str(ids)])
    assert result.exit_code == 0, result.output
    assert "verdict: incomplete" in result.output and "review: u" in result.output


async def test_merged_pr_skips_the_review(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    github.get(f"{SLUG}/pulls/12").respond(
        json={
            "number": 12,
            "html_url": PR_URL,
            "state": "closed",
            "merged": True,
            "head": {"ref": "CODEIT-9-count", "sha": "HEADSHA"},
            "base": {"ref": "main", "sha": "b"},
        }
    )
    ticket(jira, "Agent Review")
    sandbox = FakeSandbox()
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=sandbox,
        backends=[],
        echo=lambda _: None,  # type: ignore[arg-type]
    )
    assert (result.routing.reason, result.routing.needs_human) == ("already merged", False)
    assert moves(jira) == ["Human Review"] and sandbox.specs == []


async def test_pr_found_by_branch_when_the_field_is_empty(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket(jira, "Agent Review", "Agent Review", pr=None)
    github.get(f"{SLUG}/pulls").respond(json=[{"number": 12}])
    result = await run_reviewer(
        cfg,
        SECRETS,
        IDS,
        KEY,
        sandbox=FakeSandbox(failing=("npm test -- --run tests/",)),
        backends=[FakeBackend("rev", [PASS])],  # type: ignore[list-item]
        ci_wait_s=0,
        echo=lambda _: None,
    )
    assert result.routing.route == "human_review" and result.review_url
    assert {"fields": {FIELDS.pr_url: PR_URL}} in sent(jira.routes[1])
