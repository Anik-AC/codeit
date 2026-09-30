"""Golden suite layout (PRD 17.1):

evals/suites/<suite>/suite.yaml           repo, base_commit, hidden-test commands
evals/suites/<suite>/tasks/<id>-<slug>/
    task.yaml          id, title, points, kind, tags, hidden_total
    ticket.md          exactly what the Coder receives
    hidden_tests/      copied into the workspace only for scoring (repo-relative paths)
    reference.patch    a known-good solution
    mutants/           seeded-bug variants of the reference (PRD 17.4): M1.patch, ...
                       and mutants.yaml with each one's kind and defect
"""

from __future__ import annotations

import fnmatch
import hashlib
from collections.abc import Iterable, Sequence
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

SUITES_DIR = Path(__file__).resolve().parents[3] / "evals" / "suites"
CONFIGS_DIR = Path(__file__).resolve().parents[3] / "evals" / "configs"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class HiddenCommands(_Model):
    install: str
    unit: str
    e2e: str


class Mutant(_Model):
    id: str
    kind: str
    defect: str
    patch: Path = Path()


class Task(_Model):
    id: str
    title: str
    points: int
    kind: str
    tags: list[str] = Field(default_factory=list)
    hidden_total: int | None = None
    dir: Path = Path()

    @property
    def ticket_md(self) -> str:
        return (self.dir / "ticket.md").read_text(encoding="utf-8")

    @property
    def reference_patch(self) -> Path:
        return self.dir / "reference.patch"

    def hidden_files(self) -> list[str]:
        root = self.dir / "hidden_tests"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())

    def mutants(self) -> list[Mutant]:
        index = self.dir / "mutants" / "mutants.yaml"
        if not index.exists():
            return []
        items = yaml.safe_load(index.read_text(encoding="utf-8")) or []
        return [Mutant(**m, patch=self.dir / "mutants" / f"{m['id']}.patch") for m in items]


class Suite(_Model):
    name: str
    repo: str
    base_commit: str
    commands: HiddenCommands
    hidden_globs: dict[str, list[str]] = Field(default_factory=dict)
    root: Path = Path()
    tasks: list[Task] = Field(default_factory=list)

    def is_e2e(self, path: str) -> bool:
        return any(fnmatch.fnmatch(path, g) for g in self.hidden_globs.get("e2e", []))

    def select(self, ids: Iterable[str] | None) -> list[Task]:
        if not ids:
            return list(self.tasks)
        wanted = {i.upper() for i in ids}
        chosen = [t for t in self.tasks if t.id.upper() in wanted]
        missing = wanted - {t.id.upper() for t in chosen}
        if missing:
            raise ValueError(f"no such task(s) in {self.name}: {', '.join(sorted(missing))}")
        return chosen


class EvalConfig(_Model):
    """A named Coder configuration to evaluate (`evals/configs/<name>.yaml`)."""

    name: str
    backend: str = "claude_code"
    model: str | None = None


def load_suite(name: str, root: Path = SUITES_DIR) -> Suite:
    base = root / name
    raw = yaml.safe_load((base / "suite.yaml").read_text(encoding="utf-8"))
    tasks = []
    for d in sorted((base / "tasks").iterdir()):
        if (d / "task.yaml").exists():
            data = yaml.safe_load((d / "task.yaml").read_text(encoding="utf-8"))
            tasks.append(Task(**data, dir=d))
    return Suite(**raw, root=base, tasks=tasks)


def load_config(name: str, root: Path = CONFIGS_DIR) -> EvalConfig:
    path = root / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"no eval config {name!r} (looked for {path})")
    return EvalConfig(**yaml.safe_load(path.read_text(encoding="utf-8")))


def steering_sha(files: Sequence[Path]) -> str:
    """A short hash of what steers the Coder: its prompts and the target's steering kit.
    Evals compare configurations by it (PRD 17.6)."""
    h = hashlib.sha256()
    for f in sorted(files):
        if f.is_file():
            h.update(f"{f.parent.name}/{f.name}".encode())
            h.update(f.read_bytes())
    return h.hexdigest()[:12]
