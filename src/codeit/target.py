"""A target repo's `codeit.yaml` (PRD 16.4): the commands agents and the Reviewer run."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

FILE_NAME = "codeit.yaml"


class TargetCommands(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    install: str
    lint: str
    typecheck: str
    unit: str
    e2e: str

    def in_order(self) -> list[tuple[str, str]]:
        """(name, command) in the order checks run (PRD 11.3 phase 1)."""
        return [
            (name, getattr(self, name)) for name in ("install", "lint", "typecheck", "unit", "e2e")
        ]


class TargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commands: TargetCommands
    test_globs: list[str] = Field(default_factory=list)
    never_auto_rebase: list[str] = Field(default_factory=list)


def load_target_config(repo: Path) -> TargetConfig:
    path = repo / FILE_NAME
    if not path.is_file():
        raise FileNotFoundError(f"{path} not found; the target repo needs a {FILE_NAME}")
    return TargetConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
