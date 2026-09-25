"""run_coder end to end with fakes: Jira and GitHub over respx, a fake container and clone."""

from __future__ import annotations

import contextlib
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
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from codeit import db
from codeit.agents import coder_run
from codeit.agents.coder import ORCHESTRATOR_MARK, CoderResult
from codeit.agents.coder_run import CoderError, run_coder, run_coder_local
from codeit.cli import app
from codeit.config import Config, Secrets, load_config
from codeit.db.models import McpToken, Run
from codeit.jira_client import JiraIds
from codeit.orchestrator.leases import LeaseStore
from codeit.sandbox.clone import Prepared
from codeit.sandbox.containers import ExecResult
from tests.conftest import REPO_ROOT
from tests.unit.jira.conftest import BASE, FIELDS, issue_json

GH = "https://api.github.com"
SLUG = "/repos/Anik-AC/codeit-sandbox-app"
KEY = "CODEIT-5"
BRANCH = "CODEIT-5-add-due-dates"
IDS = JiraIds(
    fields=FIELDS,
    statuses={},
    issue_types={"Story": "10009"},
    transitions={"In Dev": "3", "Agent Review": "4", "Human Review": "5", "Ready for Dev": "2"},
)
SECRETS = Secrets(
    _env_file=None,
    jira_base_url=BASE,
    jira_email="bot@example.com",
    jira_api_token=SecretStr("jira"),
    github_token_agent=SecretStr("gh-agent"),
    github_token_readonly=SecretStr("gh-ro"),
    claude_code_oauth_token=SecretStr("oauth"),
)


def result_line(status: str, pr: str | None = None, notes: str = "") -> str:
    body = CoderResult(status=status, pr_url=pr, notes=notes).model_dump_json()  # type: ignore[arg-type]
    return json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "num_turns": 9,
            "result": f"All done.\nRESULT: {body}",
        }
    )


class FakeSandbox:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.specs: list[Any] = []
        self.argv: list[str] = []
        self.stopped = 0

    def start(self, spec: Any) -> Any:
        self.specs.append(spec)
        c = MagicMock()
        c.id = "cid"
        return c

    def stop(self, container: Any, log_path: Path | None = None) -> None:
        self.stopped += 1

    async def exec_lines(
        self, cid: str, argv: list[str], *, result: ExecResult, **kw: Any
    ) -> AsyncIterator[str]:
        self.argv = argv
        for line in self.lines:
            yield line
        result.exit_code = 0


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


@pytest.fixture(autouse=True)
def fakes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    class FakeClones:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def prepare(self, key: str, branch: str) -> Prepared:
            return Prepared(tmp_path / "ws" / key, branch, created=True, head="h0")

    @contextlib.asynccontextmanager
    async def no_server(*a: Any) -> AsyncIterator[bool]:
        yield False

    monkeypatch.setattr(coder_run, "CloneManager", FakeClones)
    monkeypatch.setattr(coder_run, "running_http_server", no_server)


@pytest.fixture
def jira() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=f"{BASE}/rest/api/3", assert_all_called=False) as router:
        router.post(f"/issue/{KEY}/transitions").respond(204)
        router.put(f"/issue/{KEY}").respond(204)
        router.post(f"/issue/{KEY}/comment").respond(201, json={"id": "1"})
        router.post(f"/issue/{KEY}/remotelink").respond(201, json={"id": 1})
        router.get(f"/issue/{KEY}/comment").respond(json={"comments": [], "total": 0})
        yield router


@pytest.fixture
def github() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=GH, assert_all_called=False) as router:
        router.get(f"{SLUG}/branches/{BRANCH}").respond(404, json={"message": "Branch not found"})
        yield router


def ticket_statuses(jira: respx.MockRouter, *statuses: str, **fields: Any) -> None:
    jira.get(f"/issue/{KEY}").mock(
        side_effect=[
            httpx.Response(200, json=issue_json(KEY, status={"name": s}, **fields))
            for s in statuses
        ]
    )


def pr_json(number: int = 7, sha: str = "h1") -> dict[str, Any]:
    return {
        "number": number,
        "html_url": f"https://github.com{SLUG[6:]}/pull/{number}",
        "state": "open",
        "merged_at": None,
        "head": {"ref": BRANCH, "sha": sha},
        "base": {"ref": "main", "sha": "b"},
    }


def sent(route: respx.Route) -> list[Any]:
    return [json.loads(c.request.content) for c in route.calls]


def transitions(jira: respx.MockRouter) -> list[str]:
    ids = {v: k for k, v in IDS.transitions.items()}
    return [ids[b["transition"]["id"]] for b in sent(jira.routes[0])]


def runs(cfg: Config) -> list[Run]:
    with Session(db.make_engine(cfg.db_path)) as s:
        return list(s.scalars(select(Run)))


async def test_new_ticket_to_agent_review(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket_statuses(jira, "Ready for Dev", "In Dev")
    github.get(f"{SLUG}/pulls").mock(
        side_effect=[
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[]),
            httpx.Response(200, json=[{"number": 7}]),
        ]
    )
    github.get(f"{SLUG}/pulls/7").respond(json=pr_json())
    pr_url = f"https://github.com{SLUG[6:]}/pull/7"
    sandbox = FakeSandbox(
        [json.dumps({"type": "system"}), result_line("pr_opened", pr_url, "green")]
    )
    lines: list[str] = []

    run = await run_coder(cfg, SECRETS, IDS, KEY, sandbox=sandbox, echo=lines.append)  # type: ignore[arg-type]

    assert (run.outcome.status, run.outcome.pr_url, run.branch) == ("pr_opened", pr_url, BRANCH)
    assert transitions(jira) == ["In Dev", "Agent Review"]
    puts = sent(jira.routes[1])
    assert puts[0] == {"fields": {FIELDS.agent: "coder-1", FIELDS.run_id: run.run_id}}
    assert puts[1] == {"fields": {FIELDS.pr_url: pr_url}}
    comments = [c["body"]["content"][0]["content"][0]["text"] for c in sent(jira.routes[2])]
    assert comments[0] == f"Picked up by coder-1 (run {run.run_id})."
    assert comments[1].startswith("Opened https://github.com/")
    assert json.loads(jira.routes[3].calls.last.request.content)["object"]["url"] == pr_url

    spec = sandbox.specs[0]
    assert spec.role == "coder" and spec.workspace.name == KEY
    assert spec.env == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth", "GH_TOKEN": "gh-agent"}
    argv = sandbox.argv
    assert argv[argv.index("--mcp-config") + 1] == "/run/codeit/mcp.json"
    assert "mcp__codeit-jira__add_comment" in argv[argv.index("--allowedTools") + 1]
    assert "Skill" in argv[argv.index("--tools") + 1]
    assert sandbox.stopped == 1

    engine = db.make_engine(cfg.db_path)
    assert LeaseStore(engine).holder(KEY) is None
    with Session(engine) as s:
        token = s.scalars(select(McpToken)).one()
        assert (token.role, token.ticket_key, token.run_id) == ("coder", KEY, run.run_id)
        assert token.revoked_at is not None
    [row] = runs(cfg)
    assert (row.status, row.ticket_key, row.turns) == ("pr_opened", KEY, 9)
    assert row.prompt_hash and row.transcript_path


async def test_failed_run_goes_to_human_review(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket_statuses(jira, "Ready for Dev", "In Dev")
    labels = jira.put(f"/issue/{KEY}")
    github.get(f"{SLUG}/pulls").mock(return_value=httpx.Response(200, json=[]))
    sandbox = FakeSandbox([result_line("blocked", notes="AC 2 is ambiguous")])
    run = await run_coder(cfg, SECRETS, IDS, KEY, sandbox=sandbox, echo=lambda _: None)  # type: ignore[arg-type]
    assert run.outcome.status == "blocked"
    assert transitions(jira) == ["In Dev", "Human Review"]
    assert {"update": {"labels": [{"add": "needs-human"}]}} in sent(labels)


async def test_claimed_pr_without_new_commits_is_not_trusted(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket_statuses(
        jira, "Ready for Dev", "In Dev", **{FIELDS.pr_url: f"https://github.com{SLUG[6:]}/pull/7"}
    )
    github.get(f"{SLUG}/pulls/7").respond(json=pr_json(sha="same"))
    github.get(f"{SLUG}/pulls").respond(json=[{"number": 7}])
    for path in ("reviews", "comments"):
        github.get(f"{SLUG}/pulls/7/{path}").respond(json=[])
    github.get(f"{SLUG}/issues/7/comments").respond(json=[])
    sandbox = FakeSandbox([result_line("pr_updated", "https://x/pull/7")])
    run = await run_coder(cfg, SECRETS, IDS, KEY, sandbox=sandbox, echo=lambda _: None)  # type: ignore[arg-type]
    assert run.outcome.status == "failed" and "no open PR with new commits" in run.outcome.comment
    assert transitions(jira) == ["In Dev", "Human Review"]


async def test_rework_reuses_branch_and_passes_feedback(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    pr_url = f"https://github.com{SLUG[6:]}/pull/7"
    ticket_statuses(jira, "Ready for Dev", "In Dev", **{FIELDS.pr_url: pr_url})
    jira.get(f"/issue/{KEY}/comment").respond(
        json={
            "comments": [
                {
                    "id": "9",
                    "author": {"displayName": "Onix"},
                    "created": "2026-09-25T12:00:00.000+0000",
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [
                                    {"type": "text", "text": "Show the count above the list."}
                                ],
                            }
                        ],
                    },
                },
            ],
            "total": 1,
        }
    )
    github.get(f"{SLUG}/pulls/7").mock(
        side_effect=[
            httpx.Response(200, json=pr_json(sha="old")),
            httpx.Response(200, json=pr_json(sha="new")),
        ]
    )
    github.get(f"{SLUG}/pulls").respond(json=[{"number": 7}])
    github.get(f"{SLUG}/pulls/7/reviews").respond(json=[])
    github.get(f"{SLUG}/pulls/7/comments").respond(
        json=[
            {
                "user": {"login": "onix"},
                "body": "Rename this",
                "created_at": "2026-09-25T12:30:00Z",
                "path": "a.ts",
                "line": 2,
            },
        ]
    )
    github.get(f"{SLUG}/issues/7/comments").respond(json=[])
    sandbox = FakeSandbox([result_line("pr_updated", pr_url)])
    stub = MagicMock()

    async def capture(*a: Any, **kw: Any) -> Any:
        stub(kw["task"])
        return await real(*a, **kw)

    real = coder_run._run_agent
    coder_run._run_agent = capture  # type: ignore[assignment]
    try:
        run = await run_coder(cfg, SECRETS, IDS, KEY, sandbox=sandbox, echo=lambda _: None)  # type: ignore[arg-type]
    finally:
        coder_run._run_agent = real  # type: ignore[assignment]
    task = stub.call_args.args[0]
    assert "Rework ticket CODEIT-5" in task and f"Existing pull request: {pr_url}" in task
    assert "Jira comment by Onix: Show the count above the list." in task
    assert "PR comment by onix on a.ts:2: Rename this" in task
    assert run.outcome.status == "pr_updated" and run.branch == BRANCH


async def test_usage_limit_returns_ticket(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket_statuses(jira, "Ready for Dev", "In Dev")
    github.get(f"{SLUG}/pulls").mock(return_value=httpx.Response(200, json=[]))
    limited = json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "Claude AI usage limit reached|4102444800",
        }
    )
    run = await run_coder(
        cfg, SECRETS, IDS, KEY, sandbox=FakeSandbox([limited]), echo=lambda _: None
    )  # type: ignore[arg-type]
    assert run.outcome.status == "usage_limited"
    assert transitions(jira) == ["In Dev", "Ready for Dev"]


async def test_human_moved_ticket_during_run(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket_statuses(jira, "Ready for Dev", "Human Review")
    github.get(f"{SLUG}/pulls").mock(return_value=httpx.Response(200, json=[]))
    run = await run_coder(
        cfg, SECRETS, IDS, KEY, sandbox=FakeSandbox([result_line("failed")]), echo=lambda _: None
    )  # type: ignore[arg-type]
    assert run.outcome.status == "failed"
    assert transitions(jira) == ["In Dev"]  # nothing applied after the human's move


async def test_rebase_conflict_blocks(
    cfg: Config,
    jira: respx.MockRouter,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class Conflicted:
        def __init__(self, *a: Any, **kw: Any) -> None:
            pass

        async def prepare(self, key: str, branch: str) -> Prepared:
            return Prepared(tmp_path, branch, created=False, conflict=True)

    monkeypatch.setattr(coder_run, "CloneManager", Conflicted)
    ticket_statuses(jira, "Ready for Dev", "In Dev")
    github.get(f"{SLUG}/pulls").mock(return_value=httpx.Response(200, json=[]))
    sandbox = FakeSandbox([])
    run = await run_coder(cfg, SECRETS, IDS, KEY, sandbox=sandbox, echo=lambda _: None)  # type: ignore[arg-type]
    assert run.outcome.status == "blocked" and "conflicts" in run.outcome.comment
    assert sandbox.specs == [] and transitions(jira) == ["In Dev", "Human Review"]


async def test_refuses_wrong_status_and_releases_lease(cfg: Config, jira: respx.MockRouter) -> None:
    ticket_statuses(jira, "Agent Draft")
    with pytest.raises(CoderError, match="not 'Ready for Dev'"):
        await run_coder(cfg, SECRETS, IDS, KEY, sandbox=FakeSandbox([]), echo=lambda _: None)  # type: ignore[arg-type]
    assert LeaseStore(db.make_engine(cfg.db_path)).holder(KEY) is None
    assert not jira.routes[0].called
    assert runs(cfg) == []  # a refused run is not recorded as a failure


async def test_refuses_leased_ticket(cfg: Config) -> None:
    db.upgrade(cfg.db_path)
    LeaseStore(db.make_engine(cfg.db_path)).acquire(
        KEY, "coder", "coder-2", "OTHER", timedelta(minutes=5)
    )
    with pytest.raises(CoderError, match="leased by coder-2"):
        await run_coder(cfg, SECRETS, IDS, KEY, sandbox=FakeSandbox([]), echo=lambda _: None)  # type: ignore[arg-type]


async def test_needs_agent_token(cfg: Config) -> None:
    with pytest.raises(CoderError, match="GITHUB_TOKEN_AGENT"):
        await run_coder(cfg, Secrets(_env_file=None), IDS, KEY)


async def test_crash_after_claim_goes_to_human_review(
    cfg: Config, jira: respx.MockRouter, github: respx.MockRouter
) -> None:
    ticket_statuses(jira, "Ready for Dev", "In Dev")
    github.get(f"{SLUG}/pulls").mock(side_effect=httpx.ConnectError("github down"))
    with pytest.raises(Exception, match="github down"):
        await run_coder(cfg, SECRETS, IDS, KEY, sandbox=FakeSandbox([]), echo=lambda _: None)  # type: ignore[arg-type]
    assert transitions(jira) == ["In Dev", "Human Review"]
    [row] = runs(cfg)
    assert row.status == "failed"


async def test_local_mode(cfg: Config, tmp_path: Path) -> None:
    ticket = tmp_path / "ticket.md"
    ticket.write_text("# Add a footer\n\nShow the version in a footer.")
    sandbox = FakeSandbox([result_line("committed", notes="1 commit")])
    run = await run_coder_local(
        cfg, SECRETS, ticket, "LOCAL-1", sandbox=sandbox, echo=lambda _: None
    )  # type: ignore[arg-type]
    assert (run.outcome.status, run.branch) == ("committed", "LOCAL-1-add-a-footer")
    assert "--mcp-config" not in sandbox.argv


def test_cli_run_coder(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from codeit.agents.coder import Outcome
    from codeit.agents.coder_run import CoderRun
    from codeit.jira_client.discover import write_ids

    async def fake(*a: Any, **kw: Any) -> CoderRun:
        return CoderRun(
            "R",
            KEY,
            BRANCH,
            tmp_path,
            None,
            Outcome("agent_review", "pr_opened", "", "https://x/pull/1"),
        )

    monkeypatch.setattr(coder_run, "run_coder", fake)
    ids = tmp_path / "ids.yaml"
    write_ids(IDS, ids)
    result = CliRunner().invoke(app, ["run", "coder", "codeit-5", "--ids", str(ids)])
    assert result.exit_code == 0, result.output
    assert "status: pr_opened" in result.output and "pr: https://x/pull/1" in result.output
    assert CliRunner().invoke(app, ["run", "coder"]).exit_code == 2


def test_orchestrator_mark_is_stable() -> None:
    assert ORCHESTRATOR_MARK == "_(CodeIt orchestrator)_"
