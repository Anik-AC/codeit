from __future__ import annotations

import json
import os
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import respx
from pydantic import SecretStr
from typer.testing import CliRunner

from codeit.cli import app
from codeit.config import Secrets, load_config
from codeit.sandbox import runner
from codeit.sandbox.clone import CloneManager
from codeit.sandbox.containers import ExecResult
from codeit.sandbox.gc import collect_garbage
from codeit.sandbox.runner import MissingSecret, role_env, smoke_test, write_mcp_config
from tests.conftest import REPO_ROOT
from tests.unit.jira.conftest import BASE

CFG = load_config(REPO_ROOT / "config" / "config.yaml")
FULL = Secrets(
    _env_file=None,
    claude_code_oauth_token=SecretStr("oauth"),
    github_token_agent=SecretStr("gh-agent"),
    github_token_readonly=SecretStr("gh-ro"),
    jira_api_token=SecretStr("jira"),
    openrouter_api_key=SecretStr("or"),
)


def test_role_env_follows_prd_19() -> None:
    assert role_env("coder", FULL) == {"CLAUDE_CODE_OAUTH_TOKEN": "oauth", "GH_TOKEN": "gh-agent"}
    assert role_env("rebase", FULL) == role_env("coder", FULL)
    assert role_env("reviewer", FULL) == {"GH_TOKEN": "gh-ro"}
    for role in ("coder", "reviewer", "rebase"):
        values = role_env(role, FULL).values()
        assert "jira" not in values and "or" not in values  # never Jira or OpenRouter
    with pytest.raises(MissingSecret, match="claude setup-token"):
        role_env("coder", Secrets(_env_file=None))


def test_write_mcp_config(tmp_path: Path) -> None:
    path = write_mcp_config(tmp_path, CFG, "run-token")
    assert path == "/run/codeit/mcp.json"
    written = tmp_path / "mcp.json"
    assert oct(written.stat().st_mode & 0o777) == "0o600"
    server = json.loads(written.read_text())["mcpServers"]["codeit-jira"]
    assert server == {
        "type": "http",
        "url": "http://host.docker.internal:8765/mcp",
        "headers": {"Authorization": "Bearer run-token"},
    }


class FakeSandbox:
    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.started: list[Any] = []
        self.stopped: list[Any] = []

    def start(self, spec: Any) -> Any:
        self.started.append(spec)
        c = MagicMock()
        c.id = "cid"
        return c

    def stop(self, container: Any, log_path: Path | None = None) -> None:
        self.stopped.append(log_path)

    async def exec_lines(self, *a: Any, result: ExecResult, **kw: Any) -> AsyncIterator[str]:
        for line in self.lines:
            yield line
        result.exit_code = 0


@pytest.mark.parametrize(("reply", "ok"), [("hello via bash\n", True), ("nope", False)])
async def test_smoke_test(tmp_path: Path, reply: str, ok: bool) -> None:
    cfg = CFG.model_copy(update={"data_dir": tmp_path / "data"})
    done = {"type": "result", "subtype": "success", "is_error": False, "result": reply}
    fake = FakeSandbox([json.dumps(done)])
    outcome = await smoke_test(cfg, FULL, sandbox=fake)  # type: ignore[arg-type]
    assert outcome.ok is ok
    spec = fake.started[0]
    assert spec.role == "smoke" and spec.env["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"
    assert fake.stopped == [outcome.container_log]
    assert not (tmp_path / "data" / "runs").exists() or not any(
        (tmp_path / "data" / "runs").iterdir()
    )
    assert Path(outcome.result.transcript_path).exists()


async def test_gc(tmp_path: Path, mock: respx.MockRouter) -> None:
    clones = CloneManager(tmp_path, "app", "unused")
    for key in ("CODEIT-1", "CODEIT-2", "CODEIT-3", "CODEIT-4"):
        clones.path_for(key).mkdir(parents=True)
    old = time.time() - 30 * 86400
    os.utime(clones.path_for("CODEIT-4"), (old, old))
    mock.get("/issue/CODEIT-1").respond(json={"fields": {"status": {"name": "Done"}}})
    mock.get("/issue/CODEIT-2").respond(json={"fields": {"status": {"name": "In Dev"}}})
    mock.get("/issue/CODEIT-3").respond(404)
    mock.get("/issue/CODEIT-4").respond(json={"fields": {"status": {"name": "Human Review"}}})
    secrets = Secrets(
        _env_file=None, jira_base_url=BASE, jira_email="e", jira_api_token=SecretStr("t")
    )
    removed = dict(await collect_garbage(clones, secrets, max_age_days=14))
    assert removed == {
        "CODEIT-1": "ticket is Done",
        "CODEIT-3": "ticket not found",
        "CODEIT-4": "30 days old",
    }
    assert list(clones.list_clones()) == ["CODEIT-2"]
    assert await collect_garbage(CloneManager(tmp_path / "none", "app", "x"), secrets) == []


cli = CliRunner()
CONFIG = str(REPO_ROOT / "config" / "config.yaml")


def test_cli_build(monkeypatch: pytest.MonkeyPatch) -> None:
    from codeit.sandbox import containers

    monkeypatch.setattr(containers, "build_image", lambda ctx, tag: 0)
    result = cli.invoke(app, ["sandbox", "build", "-c", CONFIG])
    assert result.exit_code == 0 and "Built codeit-worker:0.1.0" in result.output
    monkeypatch.setattr(containers, "build_image", lambda ctx, tag: 7)
    assert cli.invoke(app, ["sandbox", "build", "-c", CONFIG]).exit_code == 7


def test_cli_smoke(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from codeit.backends.base import AgenticResult

    async def fake(cfg: Any, secrets: Any, keep: bool = False) -> Any:
        r = AgenticResult("completed", "hello via bash", "t.jsonl", turns=2, model="m")
        return runner.SmokeOutcome(True, r, tmp_path / "c.log")

    monkeypatch.setattr(runner, "smoke_test", fake)
    result = cli.invoke(app, ["sandbox", "smoke", "-c", CONFIG])
    assert result.exit_code == 0, result.output
    assert "status: completed, turns: 2, model: m" in result.output

    async def missing(*a: Any, **kw: Any) -> Any:
        raise MissingSecret("CLAUDE_CODE_OAUTH_TOKEN is not set")

    monkeypatch.setattr(runner, "smoke_test", missing)
    result = cli.invoke(app, ["sandbox", "smoke", "-c", CONFIG])
    assert result.exit_code == 1 and "CLAUDE_CODE_OAUTH_TOKEN" in result.output
