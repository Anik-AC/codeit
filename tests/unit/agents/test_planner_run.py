from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from codeit import db
from codeit.agents import planner_run
from codeit.agents.planner import PlannerError, PlanRecord, plan_issues
from codeit.agents.planner_run import apply_saved, run_planner
from codeit.backends.base import BackendUnavailable
from codeit.cli import app
from codeit.config import Config, Secrets, load_config
from codeit.db.models import Run
from codeit.jira_client.discover import write_ids
from tests.conftest import REPO_ROOT
from tests.unit.agents.conftest import FakeBackend, valid_plan
from tests.unit.agents.test_planner import IDS, created_keys
from tests.unit.jira.conftest import BASE

SECRETS = Secrets(
    _env_file=None,
    jira_base_url=BASE,
    jira_email="bot@example.com",
    jira_api_token=SecretStr("t"),
)


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    base = load_config(REPO_ROOT / "config" / "config.yaml")
    return base.model_copy(update={"data_dir": tmp_path / "data"})


@pytest.fixture
def plan_file(tmp_path: Path) -> Path:
    path = tmp_path / "plan.md"
    path.write_text("# Plan\n\nThree features.")
    return path


@pytest.fixture
def jira(mock: respx.MockRouter) -> dict[str, respx.Route]:
    return {
        "search": mock.post("/search/jql").respond(json={"issues": []}),
        "epic": mock.post("/issue").respond(201, json={"key": "CODEIT-10"}),
        "bulk": mock.post("/issue/bulk").mock(side_effect=created_keys(11)),
        "link": mock.post("/issueLink").respond(201),
    }


def runs(cfg: Config) -> list[Run]:
    with Session(db.make_engine(cfg.db_path)) as s:
        return list(s.scalars(select(Run)))


def sent_stories(route: respx.Route) -> list[dict[str, Any]]:
    return [u["fields"] for u in json.loads(route.calls.last.request.content)["issueUpdates"]]


async def test_dry_run_creates_nothing(
    cfg: Config, plan_file: Path, jira: dict[str, respx.Route]
) -> None:
    lines: list[str] = []
    outcome = await run_planner(
        cfg,
        SECRETS,
        IDS,
        plan_file,
        dry_run=True,
        backends=[FakeBackend("fake", [valid_plan()])],
        echo=lines.append,
    )
    assert not jira["epic"].called and not jira["bulk"].called and not jira["link"].called
    assert outcome.path.exists()
    assert outcome.record.created is None
    table = "\n".join(lines)
    assert "new Epic: Task tracker basics" in table
    assert "S3      8  high   S1,S2       agent-draft,split-me  Task dependencies" in table
    [run] = runs(cfg)
    assert (run.role, run.status, run.backend, run.model) == (
        "planner",
        "dry_run",
        "fake",
        "fake-model",
    )
    assert run.prompt_hash and len(run.prompt_hash) == 64
    assert run.result_json == {"plan_path": str(outcome.path)}


async def test_saved_dry_run_applies_exactly_what_it_showed(
    cfg: Config, plan_file: Path, jira: dict[str, respx.Route]
) -> None:
    """PRD 11.1: the dry-run output matches what gets created."""
    shown: list[str] = []
    outcome = await run_planner(
        cfg,
        SECRETS,
        IDS,
        plan_file,
        dry_run=True,
        backends=[FakeBackend("fake", [valid_plan()])],
        echo=shown.append,
    )
    expected = plan_issues(
        outcome.record.draft,
        IDS,
        "CODEIT",
        run_id=outcome.record.run_id,
        plan_file=outcome.record.plan_file,
    )
    applied: list[str] = []
    created = await apply_saved(cfg, SECRETS, IDS, outcome.path, echo=applied.append)

    assert created.stories == {"S1": "CODEIT-11", "S2": "CODEIT-12", "S3": "CODEIT-13"}
    stories = sent_stories(jira["bulk"])
    assert [f["summary"] for f in stories] == [s.spec.summary for s in expected.stories]
    assert [f["labels"] for f in stories] == [s.spec.labels for s in expected.stories]
    assert [f["description"] for f in stories] == [
        s.spec.to_fields()["description"] for s in expected.stories
    ]
    assert applied[: len(shown)] == shown  # same table, plus the created keys
    assert "Created CODEIT-10: S1=CODEIT-11, S2=CODEIT-12, S3=CODEIT-13" in applied[-1]
    assert PlanRecord.load(outcome.path).created == created

    with pytest.raises(PlannerError, match="already applied as CODEIT-10"):
        await apply_saved(cfg, SECRETS, IDS, outcome.path)


async def test_real_run_creates_and_records(
    cfg: Config, plan_file: Path, jira: dict[str, respx.Route]
) -> None:
    outcome = await run_planner(
        cfg,
        SECRETS,
        IDS,
        plan_file,
        dry_run=False,
        epic="CODEIT-7",
        backends=[FakeBackend("fake", [valid_plan()])],
        echo=lambda _: None,
    )
    assert not jira["epic"].called  # existing Epic
    assert outcome.record.created is not None
    assert outcome.record.created.epic_key == "CODEIT-7"
    assert jira["link"].call_count == 3
    [run] = runs(cfg)
    assert run.status == "completed"
    assert run.result_json is not None
    assert run.result_json["created"]["stories"]["S2"] == "CODEIT-12"


async def test_failed_run_is_recorded(
    cfg: Config, plan_file: Path, jira: dict[str, respx.Route]
) -> None:
    with pytest.raises(PlannerError):
        await run_planner(
            cfg,
            SECRETS,
            IDS,
            plan_file,
            dry_run=True,
            backends=[FakeBackend("fake", [BackendUnavailable("parked")])],
        )
    [run] = runs(cfg)
    assert run.status == "failed"
    assert run.error is not None and "parked" in run.error


def test_default_repo(cfg: Config) -> None:
    assert planner_run.default_repo(cfg) == cfg.data_dir / "repos" / "codeit-sandbox-app"


# CLI --------------------------------------------------------------------------------------------

runner = CliRunner()


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: Config) -> tuple[Path, Path]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "t")
    text = (REPO_ROOT / "config" / "config.yaml").read_text()
    config = tmp_path / "config.yaml"
    config.write_text(text.replace("data_dir: data", f"data_dir: {tmp_path / 'data'}"))
    ids = tmp_path / "ids.yaml"
    write_ids(IDS, ids)
    fake = FakeBackend("fake", [valid_plan()])
    monkeypatch.setattr(planner_run, "chat_route", lambda *a: [fake])
    return config, ids


def test_cli_dry_run_then_apply(
    cli_env: tuple[Path, Path], plan_file: Path, jira: dict[str, respx.Route]
) -> None:
    config, ids = cli_env
    base = ["-c", str(config), "--ids", str(ids)]
    result = runner.invoke(app, ["plan", str(plan_file), "--dry-run", *base])
    assert result.exit_code == 0, result.output
    assert "Nothing created. To create exactly this: codeit plan" in result.output
    saved = result.output.split("codeit plan ")[-1].strip()

    result = runner.invoke(app, ["plan", saved, "--epic", "X", *base])
    assert result.exit_code == 2  # --epic applies only to markdown plans

    result = runner.invoke(app, ["plan", saved, *base])
    assert result.exit_code == 0, result.output
    assert "Created CODEIT-10" in result.output

    result = runner.invoke(app, ["plan", saved, *base])
    assert result.exit_code == 1
    assert "already applied" in result.output


def test_cli_jira_failure(
    cli_env: tuple[Path, Path], plan_file: Path, mock: respx.MockRouter
) -> None:
    config, ids = cli_env
    mock.post("/search/jql").mock(return_value=httpx.Response(401))
    result = runner.invoke(app, ["plan", str(plan_file), "-c", str(config), "--ids", str(ids)])
    assert result.exit_code == 1
    assert "Planner failed" in result.output
