"""Loading and rendering of versioned prompts and templates (PRD 11).

Prompts live in `prompts/{role}/` and templates in `templates/`, both Jinja2 markdown.
`prompt_hash(role)` fingerprints a role's prompt files; every run records it.
"""

from __future__ import annotations

import hashlib
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


def render(name: str, **variables: Any) -> str:
    """Render `prompts/<name>` or `templates/<name>`, e.g. `planner/system.md`."""
    return _env.get_template(name).render(**variables).strip() + "\n"


def prompt_hash(role: str) -> str:
    """SHA-256 over the role's prompt files (names and contents), in sorted order."""
    digest = hashlib.sha256()
    for path in sorted((PROMPTS_DIR / role).rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(PROMPTS_DIR).as_posix().encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return digest.hexdigest()
