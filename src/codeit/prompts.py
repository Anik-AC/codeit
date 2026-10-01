"""Loading and rendering of versioned prompts and templates (PRD 11).

Prompts live in `prompts/{role}/` and templates in `templates/`, both Jinja2 markdown.
`prompt_hash(role)` fingerprints a role's prompt files; every run records it.

`overlay(dir)` makes files in `dir` (laid out like `prompts/`) win over the checked-in ones
for the code inside it. The Learning agent's eval gate uses it to score a proposed prompt
change before anyone merges it (ADR-0019).
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_DIR = REPO_ROOT / "prompts"
TEMPLATES_DIR = REPO_ROOT / "templates"

_env = Environment(
    loader=FileSystemLoader([str(PROMPTS_DIR), str(TEMPLATES_DIR)]),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 - markdown for models and Jira, never HTML
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)


_overlay: ContextVar[Path | None] = ContextVar("prompt_overlay", default=None)
_overlay_envs: dict[Path, Environment] = {}


@contextlib.contextmanager
def overlay(directory: Path) -> Iterator[None]:
    token = _overlay.set(directory)
    try:
        yield
    finally:
        _overlay.reset(token)


def _current_env() -> Environment:
    directory = _overlay.get()
    if directory is None:
        return _env
    if directory not in _overlay_envs:
        _overlay_envs[directory] = _env.overlay(
            loader=FileSystemLoader([str(directory), str(PROMPTS_DIR), str(TEMPLATES_DIR)])
        )
    return _overlay_envs[directory]


def prompt_path(name: str) -> Path:
    """The file behind `prompts/<name>`, honouring an active overlay."""
    directory = _overlay.get()
    if directory is not None and (directory / name).is_file():
        return directory / name
    return PROMPTS_DIR / name


def render(name: str, **variables: Any) -> str:
    """Render `prompts/<name>` or `templates/<name>`, e.g. `planner/system.md`."""
    return _current_env().get_template(name).render(**variables).strip() + "\n"


def prompt_hash(role: str) -> str:
    """SHA-256 over the role's prompt files (names and contents), in sorted order."""
    digest = hashlib.sha256()
    for path in sorted((PROMPTS_DIR / role).rglob("*")):
        if path.is_file():
            name = path.relative_to(PROMPTS_DIR).as_posix()
            digest.update(name.encode())
            digest.update(b"\0")
            digest.update(prompt_path(name).read_bytes())  # an overlay's copy if active
    return digest.hexdigest()
