"""Fixtures for agent tests: scripted chat backends and a valid plan."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from codeit.backends.base import Availability, ChatRequest, ChatResult
from tests.unit.jira.conftest import client, mock, sleeps  # noqa: F401

VALID_PLAN: dict[str, Any] = {
    "epic": {"summary": "Task tracker basics", "description_md": "Make it useful."},
    "stories": [
        {
            "ref": "S1",
            "summary": "Add API endpoint to create tasks",
            "user_story": "As a user, I want to create tasks, so that I can track work.",
            "acceptance_criteria": [
                "Given a title, When I POST /tasks, Then 201",
                "Given no title, When I POST /tasks, Then 400",
            ],
            "technical_notes_md": "Use `zod` for validation.",
            "test_plan": {"unit": ["POST /tasks validates"], "e2e": []},
            "suggested_points": 2,
            "depends_on": [],
            "risk": "low",
        },
        {
            "ref": "S2",
            "summary": "Add a create-task form",
            "user_story": "As a user, I want a form, so that I can add tasks.",
            "acceptance_criteria": ["Given the form, When I submit, Then the task shows"],
            "technical_notes_md": "",
            "test_plan": {"unit": [], "e2e": ["create a task"]},
            "suggested_points": 2,
            "depends_on": ["S1"],
            "risk": "low",
        },
        {
            "ref": "S3",
            "summary": "Task dependencies",
            "user_story": "As a user, I want blockers, so that I do work in order.",
            "acceptance_criteria": ["Given A blocks B, When I complete B, Then 409", "x"],
            "test_plan": {"unit": ["guards"], "e2e": ["blocked badge"]},
            "suggested_points": 8,
            "depends_on": ["S1", "S2"],
            "risk": "high",
        },
    ],
}


def valid_plan() -> dict[str, Any]:
    return copy.deepcopy(VALID_PLAN)


Script = list[dict[str, Any] | Exception]


class FakeBackend:
    """Answers each `complete` call with the next scripted item (data, or an exception)."""

    kind: Literal["chat"] = "chat"

    def __init__(self, name: str, script: Script) -> None:
        self.name = name
        self.script = list(script)
        self.requests: list[ChatRequest] = []

    async def available(self) -> Availability:
        return Availability("ok")

    async def complete(self, req: ChatRequest) -> ChatResult:
        self.requests.append(req)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(
            text=json.dumps(item),
            data=item,
            backend=self.name,
            model="fake-model",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.01,
        )
