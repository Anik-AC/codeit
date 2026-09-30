from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from codeit import db
from codeit.cli import app
from codeit.orchestrator import serve as serve_mod
from codeit.orchestrator.budget import record_spend
from codeit.orchestrator.leases import LeaseStore
from codeit.orchestrator.serve import StartupError, preflight
from tests.conftest import REPO_ROOT

runner = CliRunner()


@pytest.fixture
def config(tmp_path: Path) -> Path:
    text = (REPO_ROOT / "config" / "config.yaml").read_text()
    path = tmp_path / "config.yaml"
    path.write_text(text.replace("data_dir: data", f"data_dir: {tmp_path / 'data'}"))
    return path


def test_budget_shows_window_and_spend(config: Path, tmp_path: Path) -> None:
    db.upgrade(tmp_path / "data" / "codeit.db")
    engine = db.make_engine(tmp_path / "data" / "codeit.db")
    record_spend(engine, backend="openrouter_paid_review", role="reviewer", usd=0.12, requests=1)
    result = runner.invoke(app, ["budget", "-c", str(config)])
    assert result.exit_code == 0, result.output
    assert "window 23:00-08:00" in result.output
    assert "reviewer  $0.1200 of $0.50 today" in result.output


def test_agents_before_and_with_leases(config: Path, tmp_path: Path) -> None:
    result = runner.invoke(app, ["agents", "-c", str(config)])
    assert result.exit_code == 0 and "No agent instances yet" in result.output
    engine = db.make_engine(tmp_path / "data" / "codeit.db")
    LeaseStore(engine).acquire("CODEIT-1", "coder", "coder-1", "R1", timedelta(minutes=5))
    result = runner.invoke(app, ["agents", "-c", str(config)])
    assert "CODEIT-1" in result.output and "coder-1" in result.output


def test_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic import SecretStr

    from codeit.config import Secrets, load_config

    cfg = load_config(REPO_ROOT / "config" / "config.yaml")
    sandbox = MagicMock()
    with pytest.raises(StartupError, match="GITHUB_TOKEN_AGENT, CLAUDE_CODE_OAUTH_TOKEN"):
        preflight(cfg, Secrets(_env_file=None), sandbox)

    secrets = Secrets(
        _env_file=None,
        github_token_agent=SecretStr("g"),
        claude_code_oauth_token=SecretStr("c"),
    )
    monkeypatch.setattr(serve_mod, "image_exists", lambda client, tag: False)
    with pytest.raises(StartupError, match="codeit sandbox build"):
        preflight(cfg, secrets, sandbox)

    proxies: list[object] = []
    monkeypatch.setattr(serve_mod, "image_exists", lambda client, tag: True)
    monkeypatch.setattr(serve_mod, "ensure_proxy", lambda client, s: proxies.append(s))
    warnings = preflight(cfg, secrets, sandbox)
    assert proxies and warnings == ["no OPENROUTER_API_KEY: the Reviewer will not start"]
