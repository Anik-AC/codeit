"""Fixtures for live tests against the owner's Jira site. Credentials come from `.env`."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import pytest

from codeit.config import Secrets, load_config
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.discover import discover
from codeit.jira_client.issues import IssueSpec, create_issue, delete_issue
from tests.conftest import REPO_ROOT

LABEL = "codeit-live-test"

Factory = Callable[..., Awaitable[str]]


@pytest.fixture
def project_key() -> str:
    return load_config(REPO_ROOT / "config" / "config.yaml").project.jira_project_key


@pytest.fixture
async def jira() -> AsyncIterator[JiraClient]:
    async with JiraClient.from_secrets(Secrets(_env_file=REPO_ROOT / ".env")) as client:
        yield client


@pytest.fixture
async def ids(jira: JiraClient, project_key: str) -> JiraIds:
    return await discover(jira, project_key)


@pytest.fixture
async def new_story(jira: JiraClient, ids: JiraIds, project_key: str) -> AsyncIterator[Factory]:
    created: list[str] = []

    async def make(summary: str = "live test", description_md: str = "") -> str:
        key = await create_issue(
            jira,
            IssueSpec(
                project_key=project_key,
                issue_type_id=ids.issue_type_id("Story"),
                summary=f"[codeit live test] {summary}",
                description_md=description_md,
                labels=[LABEL],
            ),
        )
        created.append(key)
        return key

    yield make
    for key in created:
        await delete_issue(jira, key)
