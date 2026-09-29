"""`codeit init-target`: copy the steering kit into a target repo (PRD 16).

Files come from `templates/target/`. Names ending in `.j2` are rendered with the repo's
`codeit.yaml` commands (or the PRD 16.4 defaults) and lose the suffix; others are copied.
Existing files are never overwritten. Hook scripts are made executable.
"""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path

from codeit.prompts import TEMPLATES_DIR, render
from codeit.target import FILE_NAME, TargetCommands, load_target_config

KIT_DIR = TEMPLATES_DIR / "target"

DEFAULT_COMMANDS = TargetCommands(
    install="npm ci",
    lint="npm run lint",
    typecheck="npm run typecheck",
    unit="npm test -- --run",
    e2e="npx playwright test",
)
DEFAULT_TEST_GLOBS = ["**/*.test.ts", "**/*.spec.ts", "e2e/**/*.ts"]


@dataclass
class InitResult:
    created: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def init_target(repo: Path, *, project_name: str | None = None) -> InitResult:
    if not repo.is_dir():
        raise FileNotFoundError(f"{repo} is not a directory")
    if (repo / FILE_NAME).is_file():
        target = load_target_config(repo)
        commands, globs = target.commands, target.test_globs
    else:
        commands, globs = DEFAULT_COMMANDS, DEFAULT_TEST_GLOBS
    variables = {
        "project_name": project_name or repo.resolve().name,
        "commands": commands,
        "test_globs": globs,
    }
    result = InitResult()
    for source in sorted(p for p in KIT_DIR.rglob("*") if p.is_file()):
        rel = source.relative_to(KIT_DIR)
        rendered = rel.suffix == ".j2"
        dest_rel = rel.with_suffix("") if rendered else rel
        dest = repo / dest_rel
        if dest.exists():
            result.skipped.append(dest_rel.as_posix())
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if rendered:
            dest.write_text(render(f"target/{rel.as_posix()}", **variables), encoding="utf-8")
        else:
            dest.write_bytes(source.read_bytes())
        if dest.suffix == ".sh":
            dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        result.created.append(dest_rel.as_posix())
    return result
