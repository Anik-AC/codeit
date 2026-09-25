from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeit import __version__
from codeit.cli import app
from codeit.log import bind_run, clear_run, configure_logging, get_logger
from tests.conftest import FIXTURES

runner = CliRunner()


def test_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("config", "db", "jira", "plan", "run", "sandbox", "eval", "up", "budget"):
        assert cmd in result.output


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_config_validate_ok(repo_config_path: Path) -> None:
    result = runner.invoke(app, ["config", "validate", "-c", str(repo_config_path)])
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_config_validate_bad_exits_1() -> None:
    result = runner.invoke(app, ["config", "validate", "-c", str(FIXTURES / "config_bad.yaml")])
    assert result.exit_code == 1
    assert "sandbox.image" in result.output


def test_db_upgrade(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    text = (FIXTURES.parent.parent / "config" / "config.yaml").read_text()
    cfg.write_text(text.replace("data_dir: data", f"data_dir: {tmp_path / 'data'}"))
    result = runner.invoke(app, ["db", "upgrade", "-c", str(cfg)])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "data" / "codeit.db").exists()


def test_stub_names_milestone() -> None:
    result = runner.invoke(app, ["run", "coder", "CODEIT-1"])
    assert result.exit_code == 2
    assert "M5" in result.output


def test_json_logs_carry_run_context(tmp_path: Path) -> None:
    log_file = configure_logging(tmp_path, console=False)
    try:
        bind_run(run_id="R1", ticket_key="CODEIT-7")
        get_logger("test").info("hello", extra_field=3)
    finally:
        clear_run()
        logging.shutdown()
    line = json.loads(log_file.read_text().strip().splitlines()[-1])
    assert line["event"] == "hello"
    assert line["run_id"] == "R1"
    assert line["ticket_key"] == "CODEIT-7"
    assert line["extra_field"] == 3


@pytest.mark.live
def test_live_marker_is_skipped_by_default() -> None:
    assert os.environ.get("LIVE") == "1", "live tests must be skipped unless LIVE=1"
