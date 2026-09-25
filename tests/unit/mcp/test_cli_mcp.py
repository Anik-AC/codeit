from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeit import db
from codeit.cli import app
from codeit.run_tokens import RunTokenStore
from tests.conftest import REPO_ROOT

runner = CliRunner()


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    text = (REPO_ROOT / "config" / "config.yaml").read_text()
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("data_dir: data", f"data_dir: {tmp_path / 'data'}"))
    return path


def store(tmp_path: Path) -> RunTokenStore:
    return RunTokenStore(db.make_engine(tmp_path / "data" / "codeit.db"))


def test_token_then_revoke(cfg: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "mcp",
            "token",
            "--role",
            "coder",
            "--ticket",
            "codeit-5",
            "--run-id",
            "R1",
            "-c",
            str(cfg),
        ],
    )
    assert result.exit_code == 0, result.output
    token = result.stdout.strip().splitlines()[-1]
    grant = store(tmp_path).verify(token)
    assert grant is not None
    assert (grant.role, grant.ticket_key, grant.run_id) == ("coder", "CODEIT-5", "R1")

    result = runner.invoke(app, ["mcp", "revoke", "R1", "-c", str(cfg)])
    assert "Revoked 1 token(s)" in result.output
    assert store(tmp_path).verify(token) is None


def test_token_rejects_human(cfg: Path) -> None:
    result = runner.invoke(app, ["mcp", "token", "--role", "human", "-c", str(cfg)])
    assert result.exit_code == 1
    assert "cannot hold a run token" in result.output


def test_serve_stdio_rejects_unknown_role(cfg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CODEIT_ROLE", "admin")
    result = runner.invoke(app, ["mcp", "serve", "--stdio", "-c", str(cfg)])
    assert result.exit_code == 1
    assert "CODEIT_ROLE='admin'" in result.output


def test_serve_without_ids_file(cfg: Path, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["mcp", "serve", "--stdio", "-c", str(cfg), "--ids", str(tmp_path / "none.yaml")]
    )
    assert result.exit_code == 1
    assert "codeit jira discover" in result.output


def test_stdio_script_rejects_unknown_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcp_servers.jira.app import main_stdio

    monkeypatch.setenv("CODEIT_HOME", str(REPO_ROOT))
    monkeypatch.setenv("CODEIT_ROLE", "admin")
    monkeypatch.chdir(REPO_ROOT)
    with pytest.raises(SystemExit) as exit_:
        main_stdio()
    assert exit_.value.code == 1
    assert "CODEIT_ROLE='admin'" in capsys.readouterr().err


def test_stdio_script_needs_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcp_servers.jira.app import main_stdio

    monkeypatch.chdir(tmp_path)  # restored after the test; main_stdio changes cwd
    monkeypatch.setenv("CODEIT_HOME", str(tmp_path))
    monkeypatch.delenv("CODEIT_ROLE", raising=False)
    with pytest.raises(SystemExit):
        main_stdio()
    assert "config file not found" in capsys.readouterr().err
