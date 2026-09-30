"""The Rebase agent with real git against a local "GitHub" (a bare repo), fake Jira and
GitHub over respx, and the container parts (agent, tests) replaced."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import respx
from pydantic import SecretStr

from codeit.agents import rebase
from codeit.agents.rebase import (
    RebaseError,
    escalation_reason,
    key_of_branch,
    rebase_candidates,
    run_rebase,
)
from codeit.agents.reviewer.checks import CheckResult
from codeit.backends.base import AgenticResult
from codeit.config import Config, Secrets, load_config
from codeit.github_client import GitHubClient
from codeit.jira_client import JiraClient
from codeit.orchestrator.budget import Decision
from codeit.sandbox.clone import CloneManager
from tests.conftest import REPO_ROOT
from tests.unit.jira.conftest import API, BASE
from tests.unit.orchestrator.fake_jira import IDS, FakeJira

SLUG = "Anik-AC/codeit-sandbox-app"
PR_URL = f"https://github.com/{SLUG}/pull/5"
BRANCH = "CODEIT-9-add-search"
SECRETS = Secrets(
    _env_file=None,
    jira_base_url=BASE,
    jira_email="e",
    jira_api_token=SecretStr("j"),
    github_token_agent=SecretStr("gh"),
)
YES = lambda: Decision(True)  # noqa: E731
CODEIT_YAML = (
    "commands:\n  install: 'true'\n  lint: 'true'\n  typecheck: 'true'\n"
    "  unit: 'true'\n  e2e: 'true'\n"
)


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def commit(repo: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text)
    sh("add", "-A", cwd=repo)
    sh("commit", "-qm", message, cwd=repo)
    return sh("rev-parse", "HEAD", cwd=repo)


@pytest.fixture
def remote(tmp_path: Path) -> tuple[Path, Path]:
    """(bare "GitHub" repo, a working copy that pushes to it), with main and the PR branch."""
    bare = tmp_path / "remote.git"
    sh("init", "-q", "--bare", "-b", "main", str(bare), cwd=tmp_path)
    work = tmp_path / "work"
    sh("clone", "-q", str(bare), str(work), cwd=tmp_path)
    sh("checkout", "-qb", "main", cwd=work)
    commit(
        work,
        {
            "app.ts": "a\nb\nc\n",
            "package-lock.json": "{}\n",
            "codeit.yaml": CODEIT_YAML,
        },
        "init",
    )
    sh("push", "-q", "origin", "main", cwd=work)
    sh("checkout", "-qb", BRANCH, cwd=work)
    commit(work, {"search.ts": "search\n"}, "CODEIT-9: search")
    sh("push", "-q", "origin", BRANCH, cwd=work)
    sh("checkout", "-q", "main", cwd=work)
    return bare, work


@pytest.fixture
def cfg(tmp_path: Path, remote: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch) -> Config:
    bare, _ = remote

    class LocalClones(CloneManager):
        def __init__(
            self, data_dir: Path, name: str, url: str, branch: str = "main", git: Any = None
        ) -> None:
            super().__init__(data_dir, name, str(bare), branch, git)

    monkeypatch.setattr(rebase, "CloneManager", LocalClones)
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


@pytest.fixture
def jira() -> Iterator[FakeJira]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        fake = FakeJira(router)
        fake.add("CODEIT-9", "Agent Review", pr_url=PR_URL)
        yield fake


@pytest.fixture
def github(remote: tuple[Path, Path]) -> Iterator[respx.MockRouter]:
    bare, _ = remote
    head = sh("rev-parse", BRANCH, cwd=bare)
    with respx.mock(base_url="https://api.github.com", assert_all_called=False) as router:
        router.get(f"/repos/{SLUG}/pulls/5").respond(
            json={
                "number": 5,
                "html_url": PR_URL,
                "state": "open",
                "merged": False,
                "head": {"ref": BRANCH, "sha": head},
                "base": {"ref": "main", "sha": "x"},
                "mergeable_state": "dirty",
            }
        )
        yield router


@pytest.fixture
def tests_pass(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    ran: list[Path] = []

    async def fake_test(cfg: Config, sandbox: Any, path: Path, run_id: str) -> list[CheckResult]:
        ran.append(path)
        return []

    monkeypatch.setattr(rebase, "_test", fake_test)
    return ran


def remote_head(bare: Path) -> str:
    return sh("rev-parse", BRANCH, cwd=bare)


async def go(cfg: Config, **kw: Any) -> rebase.RebaseRun:
    return await run_rebase(
        cfg, SECRETS, IDS, "CODEIT-9", sandbox=object(), echo=lambda _: None, **kw
    )  # type: ignore[arg-type]


async def test_clean_rebase_is_tested_and_pushed(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
) -> None:
    bare, work = remote
    main = commit(work, {"other.ts": "x\n"}, "unrelated change on main")
    sh("push", "-q", "origin", "main", cwd=work)
    before = remote_head(bare)
    result = await go(cfg, claude_check=YES)
    assert result.action == "rebased" and result.new_head == remote_head(bare) != before
    assert sh("merge-base", "--is-ancestor", main, BRANCH, cwd=bare) == ""  # on top of main
    assert len(tests_pass) == 1
    issue = jira.issues["CODEIT-9"]
    assert (
        "tests green" in issue.comments[-1] and issue.status == "Agent Review"
    )  # no status change


async def test_up_to_date_does_nothing(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
) -> None:
    before = remote_head(remote[0])
    result = await go(cfg, claude_check=YES)
    assert result.action == "up_to_date" and remote_head(remote[0]) == before
    assert tests_pass == [] and jira.issues["CODEIT-9"].comments == []


def conflicting_main(work: Path, files: dict[str, str]) -> None:
    """Make the PR branch and main both change `files`."""
    sh("checkout", "-q", BRANCH, cwd=work)
    commit(work, {k: v + "branch\n" for k, v in files.items()}, "CODEIT-9: branch side")
    sh("push", "-q", "origin", BRANCH, cwd=work)
    sh("checkout", "-q", "main", cwd=work)
    commit(work, {k: v + "main\n" for k, v in files.items()}, "main side")
    sh("push", "-q", "origin", "main", cwd=work)


async def test_lockfile_conflict_is_escalated(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
) -> None:
    bare, work = remote
    conflicting_main(work, {"package-lock.json": "{}\n"})
    before = remote_head(bare)
    result = await go(cfg, claude_check=YES)
    assert result.action == "escalated" and "package-lock.json" in result.reason
    assert remote_head(bare) == before  # nothing pushed
    issue = jira.issues["CODEIT-9"]
    assert issue.labels == ["needs-human"] and "Conflicted files" in issue.comments[-1]
    assert issue.status == "Agent Review"


async def test_too_many_conflicts_are_escalated(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
) -> None:
    conflicting_main(remote[1], {f"f{i}.ts": "x\n" for i in range(6)})
    result = await go(cfg, claude_check=YES)
    assert result.action == "escalated" and "more than the limit of 5" in result.reason


async def test_conflict_waits_for_claude(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
) -> None:
    bare, work = remote
    conflicting_main(work, {"app.ts": "a\nb\nc\n"})
    before = remote_head(bare)
    result = await go(cfg, claude_check=lambda: Decision(False, "outside the Claude run window"))
    assert result.action == "waiting" and "run window" in result.reason
    assert remote_head(bare) == before and jira.issues["CODEIT-9"].comments == []


def fake_agent(outcome: str = "resolved", finish: bool = True) -> Any:
    async def resolve(
        cfg: Config,
        secrets: Secrets,
        engine: Any,
        sandbox: Any,
        path: Path,
        run_id: str,
        *,
        prompt: str,
    ) -> AgenticResult:
        assert "app.ts" in prompt and "main side" in prompt  # conflicts and main's changes
        (path / "app.ts").write_text("a\nb\nc\nmain\nbranch\n")
        if finish:
            sh("add", "app.ts", cwd=path)
            sh("-c", "core.editor=true", "rebase", "--continue", cwd=path)
        return AgenticResult(
            "completed", f'RESULT: {{"status": "{outcome}", "notes": "kept both"}}', "t.jsonl"
        )

    return resolve


async def test_conflict_resolved_by_the_agent(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare, work = remote
    conflicting_main(work, {"app.ts": "a\nb\nc\n"})
    monkeypatch.setattr(rebase, "_resolve", fake_agent())
    result = await go(cfg, claude_check=YES)
    assert result.action == "resolved" and result.conflicts == ["app.ts"]
    assert sh("show", f"{BRANCH}:app.ts", cwd=bare).endswith("main\nbranch")
    comment = jira.issues["CODEIT-9"].comments[-1]  # ADF: code marks split the text
    assert "The agent resolved conflicts in" in comment and '"app.ts"' in comment


@pytest.mark.parametrize(
    ("outcome", "finish", "reason"),
    [("resolved", False, "did not finish the rebase"), ("blocked", True, "reported blocked")],
)
async def test_agent_failures_escalate(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    tests_pass: list[Path],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    finish: bool,
    reason: str,
) -> None:
    bare, work = remote
    conflicting_main(work, {"app.ts": "a\nb\nc\n"})
    before = remote_head(bare)
    monkeypatch.setattr(rebase, "_resolve", fake_agent(outcome, finish))
    result = await go(cfg, claude_check=YES)
    assert result.action == "escalated" and reason in result.reason
    assert remote_head(bare) == before


async def test_red_tests_are_not_pushed(
    cfg: Config,
    remote: tuple[Path, Path],
    jira: FakeJira,
    github: respx.MockRouter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bare, work = remote
    commit(work, {"other.ts": "x\n"}, "main moves")
    sh("push", "-q", "origin", "main", cwd=work)
    before = remote_head(bare)

    async def red(*a: Any) -> list[CheckResult]:
        return [CheckResult("unit", "fail", log_tail="1 failed")]

    monkeypatch.setattr(rebase, "_test", red)
    result = await go(cfg, claude_check=YES)
    assert result.action == "escalated" and "unit" in result.reason
    assert remote_head(bare) == before and "1 failed" in jira.issues["CODEIT-9"].comments[-1]


async def test_refuses_in_dev(cfg: Config, jira: FakeJira, github: respx.MockRouter) -> None:
    jira.issues["CODEIT-9"].status = "In Dev"
    with pytest.raises(RebaseError, match="In Dev"):
        await go(cfg)


def test_rules() -> None:
    assert key_of_branch("CODEIT-12-add-due-dates") == "CODEIT-12"
    assert key_of_branch("main") is None and key_of_branch("feature/x") is None
    never = ["**/migrations/**", "package-lock.json", ".github/**"]
    assert escalation_reason(["src/a.ts"], 5, never) == ""
    assert "migrations" in escalation_reason(["db/migrations/002.sql"], 5, never)
    assert ".github/workflows/ci.yml" in escalation_reason([".github/workflows/ci.yml"], 5, never)


async def test_candidates() -> None:
    pulls = [
        {"number": 1, "head": {"ref": "CODEIT-1-a"}},  # dirty, in review
        {"number": 2, "head": {"ref": "CODEIT-2-b"}},  # clean
        {"number": 3, "head": {"ref": "CODEIT-3-c"}},  # dirty but In Dev
        {"number": 4, "head": {"ref": "CODEIT-4-d"}},  # behind, needs-human
        {"number": 5, "head": {"ref": "not-a-ticket"}},
    ]
    states = {1: "dirty", 2: "clean", 3: "dirty", 4: "behind"}
    with respx.mock(assert_all_called=False) as router:
        router.get(f"https://api.github.com/repos/{SLUG}/pulls").respond(json=pulls)
        for n, s in states.items():
            router.get(f"https://api.github.com/repos/{SLUG}/pulls/{n}").respond(
                json={
                    "number": n,
                    "html_url": "u",
                    "state": "open",
                    "merged": False,
                    "head": {"ref": f"CODEIT-{n}-x", "sha": "h"},
                    "base": {"ref": "main", "sha": "b"},
                    "mergeable_state": s,
                }
            )
        with respx.mock(base_url=API, assert_all_called=False) as jira_router:
            fake = FakeJira(jira_router)
            fake.add("CODEIT-1", "Agent Review")
            fake.add("CODEIT-2", "Human Review")
            fake.add("CODEIT-3", "In Dev")
            fake.add("CODEIT-4", "Human Review", labels=["needs-human"])

            async def no_sleep(_: float) -> None:
                return None

            async with GitHubClient("t") as gh, JiraClient(BASE, "e", "t", sleep=no_sleep) as jc:
                assert await rebase_candidates(gh, SLUG, jc, IDS) == ["CODEIT-1"]
