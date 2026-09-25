from __future__ import annotations

from pathlib import Path

import pytest
import respx
from typer.testing import CliRunner

from codeit.cli import app
from codeit.jira_client.discover import load_ids
from tests.conftest import REPO_ROOT
from tests.unit.jira.conftest import BASE
from tests.unit.jira.fake_site import FakeSite

runner = CliRunner()
CONFIG = str(REPO_ROOT / "config" / "config.yaml")


@pytest.fixture
def jira_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Credentials from the environment only; the cwd has no .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JIRA_BASE_URL", BASE)
    monkeypatch.setenv("JIRA_EMAIL", "bot@example.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "token")
    return tmp_path


def test_doctor_without_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    for var in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    result = runner.invoke(app, ["jira", "doctor", "-c", CONFIG])
    assert result.exit_code == 1
    assert "missing in .env: JIRA_BASE_URL, JIRA_EMAIL, JIRA_API_TOKEN" in result.output


def test_doctor_passes(jira_env: Path, mock: respx.MockRouter) -> None:
    FakeSite(mock).install()
    result = runner.invoke(app, ["jira", "doctor", "-c", CONFIG])
    assert result.exit_code == 0, result.output
    assert "PASS  auth" in result.output
    assert "FAIL" not in result.output


def test_doctor_fails_with_hint(jira_env: Path, mock: respx.MockRouter) -> None:
    site = FakeSite(mock).install()
    site.statuses = site.statuses[:-1]
    result = runner.invoke(app, ["jira", "doctor", "-c", CONFIG])
    assert result.exit_code == 1
    assert "FAIL  statuses" in result.output
    assert "hint:" in result.output


def test_discover_writes_ids(jira_env: Path, mock: respx.MockRouter) -> None:
    FakeSite(mock).install()
    out = jira_env / "ids.yaml"
    result = runner.invoke(app, ["jira", "discover", "-c", CONFIG, "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert load_ids(out).statuses["Done"] == "7"


def test_discover_reports_problems(jira_env: Path, mock: respx.MockRouter) -> None:
    site = FakeSite(mock).install()
    site.fields = [f for f in site.fields if f["name"] != "Run ID"]
    result = runner.invoke(app, ["jira", "discover", "-c", CONFIG, "--out", "ids.yaml"])
    assert result.exit_code == 1
    assert "field 'Run ID' not found" in result.output
    assert not (jira_env / "ids.yaml").exists()


def test_discover_jira_error(jira_env: Path, mock: respx.MockRouter) -> None:
    mock.get("/project/CODEIT").respond(401)
    result = runner.invoke(app, ["jira", "discover", "-c", CONFIG])
    assert result.exit_code == 1
    assert "Jira error" in result.output
