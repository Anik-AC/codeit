"""Live checks against the owner's Jira site (M1 acceptance). Run with `LIVE=1`.

Needs JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN in `.env`, and the project from
`config/config.yaml` set up per PRD 7.1. Every issue created here is labeled
`codeit-live-test` and deleted afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from tests.conftest import REPO_ROOT

from codeit.config import Secrets, load_config
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import add_comment, list_comments
from codeit.jira_client.discover import discover
from codeit.jira_client.doctor import run_doctor
from codeit.jira_client.issues import (
    IssueSpec,
    create_issue,
    delete_issue,
    get_ticket,
    link_issues,
    update_fields,
)
from codeit.jira_client.models import REQUIRED_STATUSES
from codeit.jira_client.search import search
from codeit.jira_client.transitions import TransitionCache, transition_to

pytestmark = pytest.mark.live

LABEL = "codeit-live-test"

# Exercises every node the ADF converter supports.
RICH_MD = """# Heading

A paragraph with **bold**, *italic*, `code` and a [link](https://example.com/x).

- bullet one
- bullet two
  - nested

3. third
4. fourth

> quoted text

```python
print("hi")
```

line\\
break

---"""

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


async def test_doctor_passes(jira: JiraClient, project_key: str) -> None:
    checks = await run_doctor(jira, project_key, write_test=True)
    failed = [c for c in checks if not c.ok]
    assert not failed, "\n".join(f"{c.name}: {c.detail} ({c.hint})" for c in failed)


async def test_transition_to_each_status(
    jira: JiraClient, ids: JiraIds, new_story: Factory
) -> None:
    key = await new_story("transitions")
    cache = TransitionCache(ids.transitions)
    start = (await get_ticket(jira, key, ids.fields)).status
    for status in [*(s for s in REQUIRED_STATUSES if s != start), start]:
        await transition_to(jira, key, status, cache)
        assert (await get_ticket(jira, key, ids.fields)).status == status


async def test_link_direction(jira: JiraClient, ids: JiraIds, new_story: Factory) -> None:
    blocker = await new_story("blocker")
    blocked = await new_story("blocked")
    await link_issues(jira, blocker=blocker, blocked=blocked)
    assert (await get_ticket(jira, blocked, ids.fields)).blocked_by == [blocker]
    assert (await get_ticket(jira, blocker, ids.fields)).blocked_by == []


async def test_adf_round_trip_through_jira(
    jira: JiraClient, ids: JiraIds, new_story: Factory
) -> None:
    key = await new_story("adf", description_md=RICH_MD)
    assert (await get_ticket(jira, key, ids.fields)).description_md == RICH_MD
    await add_comment(jira, key, RICH_MD)
    comments = await list_comments(jira, key)
    assert [c.body_md for c in comments] == [RICH_MD]


async def test_custom_fields_round_trip(jira: JiraClient, ids: JiraIds, new_story: Factory) -> None:
    key = await new_story("fields")
    await update_fields(
        jira,
        key,
        ids.fields.ids(
            agent="coder-1",
            review_loop=2,
            human_returns=1,
            pr_url="https://github.com/Anik-AC/codeit-sandbox-app/pull/1",
            run_id="01LIVETEST",
        ),
    )
    t = await get_ticket(jira, key, ids.fields)
    assert (t.agent, t.review_loop, t.human_returns, t.run_id) == ("coder-1", 2, 1, "01LIVETEST")
    assert t.pr_url == "https://github.com/Anik-AC/codeit-sandbox-app/pull/1"


async def test_search_pages_with_tokens(jira: JiraClient, new_story: Factory) -> None:
    keys = [await new_story(f"search {i}") for i in range(3)]
    jql = f"key in ({', '.join(keys)}) ORDER BY key ASC"
    found: list[str] = []
    for _ in range(10):  # the search index lags a few seconds behind writes
        found = [i["key"] async for i in search(jira, jql, ["summary"], page_size=1)]
        if len(found) == len(keys):
            break
        await asyncio.sleep(2)
    assert sorted(found) == sorted(keys)
