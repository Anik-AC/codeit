"""Classify signals into lessons, and pick the lessons that recur (PRD 11.7 steps 1-2)."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from codeit.backends.base import ChatBackend, ChatMessage
from codeit.backends.structured import ModelCall, structured
from codeit.db.models import Signal
from codeit.prompts import render

Theme = Literal[
    "testing",
    "ui-conventions",
    "api-design",
    "ticket-quality",
    "scope-creep",
    "naming",
    "error-handling",
    "other",
]
Target = Literal["coder", "reviewer", "planner"]
BATCH = 40
MIN_OCCURRENCES = 2


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Classified(_Strict):
    id: int
    actionable: bool
    target: Target
    theme: Theme
    lesson_key: str
    lesson: str


class ClassifyAnswer(_Strict):
    signals: list[Classified]


@dataclass
class Lesson:
    key: str
    target: str
    theme: str
    lesson: str
    signals: list[Signal] = field(default_factory=list)

    @property
    def learn(self) -> bool:
        return any(s.learn for s in self.signals)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def existing_lessons(engine: Engine) -> list[Lesson]:
    """Lessons already known (classified, not yet addressed), one per key."""
    return list(open_lessons(engine).values())


def open_lessons(engine: Engine, scope: Sequence[int] | None = None) -> dict[str, Lesson]:
    """Classified signals not yet addressed, grouped by (target, lesson key)."""
    with Session(engine) as s:
        q = select(Signal).where(Signal.status == "classified").order_by(Signal.id)
        if scope is not None:
            q = q.where(Signal.id.in_(scope))
        rows = s.scalars(q).all()
    out: dict[str, Lesson] = {}
    for sig in rows:
        key = f"{sig.target}:{sig.lesson_key}"
        lesson = out.setdefault(
            key,
            Lesson(sig.lesson_key or "", sig.target or "", sig.theme or "", sig.lesson or ""),
        )
        lesson.signals.append(sig)
        lesson.lesson = sig.lesson or lesson.lesson  # the latest wording
    return out


def qualifying(lessons: dict[str, Lesson]) -> list[Lesson]:
    """Lessons seen at least twice, or once when a human wrote `#learn`."""
    return [
        lesson
        for lesson in lessons.values()
        if len(lesson.signals) >= MIN_OCCURRENCES or lesson.learn
    ]


async def classify(
    engine: Engine,
    backends: Sequence[ChatBackend],
    scope: Sequence[int] | None = None,
) -> tuple[int, list[ModelCall]]:
    """Classify every `new` signal (or those in `scope`); returns (count, model calls)."""
    with Session(engine) as s:
        q = select(Signal).where(Signal.status == "new").order_by(Signal.id)
        if scope is not None:
            q = q.where(Signal.id.in_(scope))
        pending = list(s.scalars(q).all())
    calls: list[ModelCall] = []
    for start in range(0, len(pending), BATCH):
        batch = pending[start : start + BATCH]
        known = existing_lessons(engine)
        messages = [
            ChatMessage("system", render("learning/classify.md")),
            ChatMessage("user", render("learning/classify_task.md", signals=batch, existing=known)),
        ]
        answer, call = await structured(backends, messages, ClassifyAnswer, "learning_classify")
        calls.append(call)
        _save(engine, batch, answer)
    return len(pending), calls


def _save(engine: Engine, batch: Sequence[Signal], answer: ClassifyAnswer) -> None:
    by_id = {c.id: c for c in answer.signals}
    with Session(engine) as s, s.begin():
        for sig in batch:
            row = s.get(Signal, sig.id)
            assert row is not None
            c = by_id.get(sig.id)
            if c is None:
                continue  # left out: stays `new` for the next run
            row.theme, row.target, row.lesson = c.theme, c.target, c.lesson.strip()
            row.lesson_key = _slug(c.lesson_key) or _slug(c.lesson)
            row.status = "classified" if c.actionable else "ignored"


def group(lessons: Sequence[Lesson]) -> dict[str, list[Lesson]]:
    by_target: dict[str, list[Lesson]] = defaultdict(list)
    for lesson in lessons:
        by_target[lesson.target].append(lesson)
    return dict(by_target)
