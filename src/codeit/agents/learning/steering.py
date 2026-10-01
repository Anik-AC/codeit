"""Where each agent's lessons go, and how rules are written into those files (PRD 11.7
step 3). The model only proposes one-line rules; CodeIt adds them under one managed
heading, so it never rewrites what people wrote."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

SECTION = "## Learned from feedback"
SECTION_NOTE = (
    "_Added by CodeIt's Learning agent from review feedback, through reviewed pull requests._"
)
MAX_RULE = 300
_TEMPLATE = re.compile(r"\{[{%#]|[}%#]\}")


@dataclass(frozen=True)
class SteeringFile:
    agent: str
    repo: Literal["target", "codeit"]
    path: str
    suite: str  # the eval that shows whether a change helps


TARGETS: dict[str, SteeringFile] = {
    "coder": SteeringFile("Coder", "target", "CLAUDE.md", "golden"),
    "reviewer": SteeringFile(
        "Reviewer", "codeit", "prompts/reviewer/checklist.md", "golden-review"
    ),
    "planner": SteeringFile("Planner", "codeit", "prompts/planner/system.md", "planner"),
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rule(_Strict):
    lesson_key: str
    rule: str
    expected_effect: str


class Skip(_Strict):
    lesson_key: str
    reason: str


class ProposeAnswer(_Strict):
    rules: list[Rule]
    skipped: list[Skip] = []


def clean_rule(text: str) -> str | None:
    """One plain line, or None if it cannot go into a steering file."""
    line = " ".join(text.split()).lstrip("-* ").strip()
    if not line or _TEMPLATE.search(line) or line.startswith("#"):
        return None
    return line[:MAX_RULE]


def _norm(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()


def add_rules(text: str, rules: Sequence[str]) -> tuple[str, list[str]]:
    """Append `rules` as bullets at the end of the managed section (made at the end of
    the file if missing). Returns the new text and the rules actually added (not already
    present anywhere in the file)."""
    present = {_norm(line.lstrip("-* ")) for line in text.splitlines()}
    added = []
    for rule in rules:
        line = clean_rule(rule)
        if line and _norm(line) not in present:
            added.append(line)
            present.add(_norm(line))
    if not added:
        return text, []
    bullets = "".join(f"- {r}\n" for r in added)
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.strip() == SECTION), None)
    if start is None:
        body = text.rstrip("\n") + "\n\n" if text.strip() else ""
        return f"{body}{SECTION}\n\n{SECTION_NOTE}\n\n{bullets}", added
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("#")), len(lines))
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    head, tail = "".join(lines[:end]), "".join(lines[end:])
    head = head if head.endswith("\n") else head + "\n"
    return head + bullets + ("\n" + tail.lstrip("\n") if tail.strip() else ""), added
