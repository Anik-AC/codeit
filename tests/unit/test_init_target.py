from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from codeit.cli import app
from codeit.init_target import init_target

CODEIT_YAML = """commands:
  install: npm ci
  lint: npm run lint
  typecheck: npm run typecheck
  unit: {unit}
  e2e: npx playwright test
test_globs: ["tests/**/*.test.ts"]
"""

EXPECTED = {
    "CLAUDE.md",
    ".claude/settings.json",
    ".claude/hooks/stop-run-unit-tests.sh",
    ".claude/hooks/lint-changed-file.sh",
    ".claude/skills/implement-ticket/SKILL.md",
    ".claude/skills/write-unit-test/SKILL.md",
    ".claude/skills/write-playwright-test/SKILL.md",
    ".claude/skills/open-pr/SKILL.md",
    ".claude/skills/address-feedback/SKILL.md",
}


def repo_with(tmp_path: Path, unit: str = "npm test -- --run") -> Path:
    (tmp_path / "codeit.yaml").write_text(CODEIT_YAML.format(unit=unit))
    return tmp_path


def test_creates_the_kit_from_codeit_yaml(tmp_path: Path) -> None:
    result = init_target(repo_with(tmp_path), project_name="demo")
    assert set(result.created) == EXPECTED
    assert result.skipped == ["codeit.yaml"]
    claude = (tmp_path / "CLAUDE.md").read_text()
    assert claude.startswith("# demo")
    assert "| Unit tests | `npm test -- --run` |" in claude
    assert "—" not in claude
    settings = json.loads((tmp_path / ".claude/settings.json").read_text())
    assert set(settings["hooks"]) == {"PostToolUse", "Stop"}
    hook = tmp_path / ".claude/hooks/stop-run-unit-tests.sh"
    assert os.access(hook, os.X_OK)
    assert "npm test -- --run" in hook.read_text()
    skill = (tmp_path / ".claude/skills/open-pr/SKILL.md").read_text()
    assert skill.startswith("---\nname: open-pr\n")
    assert "## Acceptance criteria" in skill and 'RESULT: {"status"' in skill


def test_never_overwrites(tmp_path: Path) -> None:
    repo = repo_with(tmp_path)
    (repo / "CLAUDE.md").write_text("mine")
    result = init_target(repo)
    assert "CLAUDE.md" in result.skipped
    assert (repo / "CLAUDE.md").read_text() == "mine"
    assert init_target(repo).created == []


def test_defaults_without_codeit_yaml(tmp_path: Path) -> None:
    from codeit.target import load_target_config

    result = init_target(tmp_path)
    assert "codeit.yaml" in result.created
    assert load_target_config(tmp_path).commands.unit == "npm test -- --run"  # valid YAML


def test_missing_repo(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        init_target(tmp_path / "nope")


def run_hook(repo: Path, stop_hook_active: bool) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repo / ".claude/hooks/stop-run-unit-tests.sh")],
        input=json.dumps({"hook_event_name": "Stop", "stop_hook_active": stop_hook_active}),
        capture_output=True,
        text=True,
        env={**os.environ, "CLAUDE_PROJECT_DIR": str(repo)},
        check=False,
    )


def test_stop_hook_blocks_on_failing_tests(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, unit="echo 'FAIL tasks.test.ts' && false")
    init_target(repo)
    blocked = run_hook(repo, stop_hook_active=False)
    assert blocked.returncode == 2
    assert "Unit tests are failing" in blocked.stderr
    assert "FAIL tasks.test.ts" in blocked.stderr
    assert run_hook(repo, stop_hook_active=True).returncode == 0  # no endless loop


def test_stop_hook_passes_on_green_tests(tmp_path: Path) -> None:
    repo = repo_with(tmp_path, unit='"true"')
    init_target(repo)
    assert run_hook(repo, stop_hook_active=False).returncode == 0


def test_lint_hook_ignores_non_code_files(tmp_path: Path) -> None:
    init_target(repo_with(tmp_path))
    hook = tmp_path / ".claude/hooks/lint-changed-file.sh"
    done = subprocess.run(
        [str(hook)],
        input=json.dumps({"tool_input": {"file_path": str(tmp_path / "README.md")}}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0


def test_cli(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["init-target", str(repo_with(tmp_path)), "--name", "x"])
    assert result.exit_code == 0, result.output
    assert "created  CLAUDE.md" in result.output
    assert "exists   codeit.yaml" in result.output
    missing = runner.invoke(app, ["init-target", str(tmp_path / "nope")])
    assert missing.exit_code != 0
