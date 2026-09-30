from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import respx
from sqlalchemy import Engine

from codeit import db
from codeit.config import Config, load_config
from codeit.jira_client import JiraClient
from tests.conftest import REPO_ROOT
from tests.unit.jira.conftest import API, BASE
from tests.unit.orchestrator.fake_jira import FakeJira


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return load_config(REPO_ROOT / "config" / "config.yaml").model_copy(
        update={"data_dir": tmp_path / "data"}
    )


@pytest.fixture
def engine(cfg: Config) -> Engine:
    db.upgrade(cfg.db_path)
    return db.make_engine(cfg.db_path)


@pytest.fixture
def fake_jira() -> Iterator[FakeJira]:
    with respx.mock(base_url=API, assert_all_called=False) as router:
        yield FakeJira(router)


@pytest.fixture
async def jira(fake_jira: FakeJira) -> AsyncIterator[JiraClient]:
    async def no_sleep(_: float) -> None:
        return None

    async with JiraClient(BASE, "bot@example.com", "token", sleep=no_sleep) as c:
        yield c
