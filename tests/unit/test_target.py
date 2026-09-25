from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from codeit.target import load_target_config

SAMPLE = """
commands:
  install: npm ci
  lint: npm run lint
  typecheck: npm run typecheck
  unit: npm test -- --run
  e2e: npx playwright test
test_globs: ["tests/**/*.test.ts", "e2e/**/*.ts"]
never_auto_rebase: ["package-lock.json"]
"""


def test_load(tmp_path: Path) -> None:
    (tmp_path / "codeit.yaml").write_text(SAMPLE)
    cfg = load_target_config(tmp_path)
    assert [name for name, _ in cfg.commands.in_order()] == [
        "install",
        "lint",
        "typecheck",
        "unit",
        "e2e",
    ]
    assert cfg.commands.unit == "npm test -- --run"
    assert cfg.test_globs == ["tests/**/*.test.ts", "e2e/**/*.ts"]


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"needs a codeit\.yaml"):
        load_target_config(tmp_path)


def test_missing_command(tmp_path: Path) -> None:
    (tmp_path / "codeit.yaml").write_text("commands:\n  install: npm ci\n")
    with pytest.raises(ValidationError, match="lint"):
        load_target_config(tmp_path)
