from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from sqlalchemy import Engine
from typer.testing import CliRunner

from codeit.cli import app
from codeit.config import Config
from codeit.jira_client import JiraClient
from codeit.orchestrator import settings
from codeit.orchestrator.fast_lane import fast_track
from codeit.orchestrator.leases import LeaseStore
from tests.conftest import REPO_ROOT
from tests.unit.orchestrator.fake_jira import IDS, FakeJira


def test_settings_roundtrip(tmp_path: Path) -> None:
    assert settings.load(tmp_path).fast_lane is False  # no file yet
    saved = settings.save(tmp_path, by="cli", fast_lane=True)
    assert saved.fast_lane and saved.changed_by == "cli" and saved.changed_at is not None
    assert settings.load(tmp_path).fast_lane is True
    (tmp_path / "settings.json").write_text("{broken")
    assert settings.load(tmp_path).fast_lane is False  # a broken file means off


async def test_fast_track_moves_free_tickets_only(
    engine: Engine, jira: JiraClient, fake_jira: FakeJira
) -> None:
    fake_jira.add("CODEIT-1", "Agent Review")
    fake_jira.add("CODEIT-2", "Agent Review")  # being reviewed in this process
    fake_jira.add("CODEIT-3", "Agent Review")  # leased by another process
    fake_jira.add("CODEIT-4", "Ready for Dev")
    LeaseStore(engine).acquire("CODEIT-3", "reviewer", "reviewer-9", "R", timedelta(minutes=5))
    moved = await fast_track(engine, jira, IDS, "CODEIT", busy={"CODEIT-2"})
    assert moved == ["CODEIT-1"]
    issue = fake_jira.issues["CODEIT-1"]
    assert (issue.status, issue.labels) == ("Human Review", ["fast-lane"])
    assert "Reviewer agent was skipped" in issue.comments[0]
    assert fake_jira.issues["CODEIT-2"].status == "Agent Review"
    assert fake_jira.issues["CODEIT-3"].status == "Agent Review"


def test_cli(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    text = (REPO_ROOT / "config" / "config.yaml").read_text()
    config.write_text(text.replace("data_dir: data", f"data_dir: {tmp_path / 'data'}"))
    runner = CliRunner()
    assert "fast lane: off" in runner.invoke(app, ["fast-lane", "-c", str(config)]).output
    on = runner.invoke(app, ["fast-lane", "on", "-c", str(config)])
    assert on.exit_code == 0 and "fast lane: on (set by cli" in on.output
    assert runner.invoke(app, ["fast-lane", "maybe", "-c", str(config)]).exit_code == 2


def test_cfg_fixture_is_isolated(cfg: Config) -> None:
    assert not settings.load(cfg.data_dir).fast_lane
