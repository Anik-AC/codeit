"""Fixtures for jira_client unit tests. All HTTP is mocked with respx."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
import respx

from codeit.jira_client import FieldMap, JiraClient

BASE = "https://example.atlassian.net"
API = f"{BASE}/rest/api/3"

FIELDS = FieldMap(
    story_points="customfield_10016",
    agent="customfield_10042",
    review_loop="customfield_10043",
    human_returns="customfield_10044",
    pr_url="customfield_10045",
    run_id="customfield_10046",
)


class SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleeps() -> SleepRecorder:
    return SleepRecorder()


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield router


@pytest.fixture
async def client(sleeps: SleepRecorder) -> AsyncIterator[JiraClient]:
    async with JiraClient(BASE, "bot@example.com", "token", sleep=sleeps) as c:
        yield c


def issue_json(key: str = "CODEIT-1", **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": "Add due dates",
        "description": {
            "type": "doc",
            "version": 1,
            "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Body"}]}],
        },
        "status": {"name": "Ready for Dev"},
        "priority": {"name": "High"},
        "labels": ["agent-draft"],
        "issuelinks": [],
        "parent": None,
        "created": "2026-09-25T10:00:00.000+0000",
        "updated": "2026-09-25T11:00:00.000+0000",
        FIELDS.story_points: 2.0,
        FIELDS.agent: None,
        FIELDS.review_loop: None,
        FIELDS.human_returns: 1,
        FIELDS.pr_url: None,
        FIELDS.run_id: None,
    }
    fields.update(overrides)
    return {"id": "10001", "key": key, "fields": fields}
