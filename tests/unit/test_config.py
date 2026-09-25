from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from codeit.config import Config, ConfigError, load_config
from tests.conftest import FIXTURES


def _minimal() -> dict[str, object]:
    return {
        "project": {
            "jira_project_key": "CODEIT",
            "target_repo": {"name": "app", "url": "https://github.com/x/app.git"},
        },
        "routing": {"coder": {"primary": "claude_code", "fallback": "opencode_paid"}},
        "models": {"opencode_paid": "openrouter/x"},
        "sandbox": {"image": "codeit-worker:0.1.0"},
    }


def _write(tmp_path: Path, data: object) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_repo_config_is_valid(repo_config_path: Path) -> None:
    cfg = load_config(repo_config_path)
    assert cfg.project.jira_project_key == "CODEIT"
    assert cfg.slots["reviewer"] == 2
    assert cfg.db_path == Path("data/codeit.db")


def test_minimal_config_gets_defaults(tmp_path: Path) -> None:
    cfg = load_config(_write(tmp_path, _minimal()))
    assert cfg.orchestrator.poll_seconds == 45
    assert cfg.rebase.max_files == 5
    assert cfg.docs.run_at == "07:00"


def test_bad_fixture_reports_every_error() -> None:
    with pytest.raises(ConfigError) as exc:
        load_config(FIXTURES / "config_bad.yaml")
    msg = str(exc.value)
    for fragment in (
        "project.jira_project_key",
        "claude.run_window",
        "sandbox.image",
        "unknown_section",
    ):
        assert fragment in msg


def test_unknown_route_backend_rejected(tmp_path: Path) -> None:
    data = _minimal()
    data["routing"] = {"coder": {"primary": "nope"}}
    with pytest.raises(ConfigError, match="unknown backend 'nope'"):
        load_config(_write(tmp_path, data))


def test_unknown_role_rejected(tmp_path: Path) -> None:
    data = _minimal()
    data["slots"] = {"wizard": 1}
    with pytest.raises(ConfigError, match=r"slots\.wizard"):
        load_config(_write(tmp_path, data))


@pytest.mark.parametrize(
    ("content", "match"),
    [
        ("project: [unclosed", "not valid YAML"),
        ("- a\n- b\n", "mapping at the top level"),
    ],
)
def test_unparsable_config(tmp_path: Path, content: str, match: str) -> None:
    p = tmp_path / "config.yaml"
    p.write_text(content)
    with pytest.raises(ConfigError, match=match):
        load_config(p)


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "absent.yaml")


def test_config_is_immutable(tmp_path: Path) -> None:
    cfg: Config = load_config(_write(tmp_path, _minimal()))
    with pytest.raises(Exception, match="frozen"):
        cfg.project.jira_project_key = "OTHER"  # type: ignore[misc]
