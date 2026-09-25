"""Fixtures for jira-mcp tests: a server over a respx-mocked Jira, connected in-process."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from codeit import db
from codeit.jira_client import JiraClient, JiraIds
from codeit.run_tokens import RunTokenStore
from mcp_servers.jira.server import JiraContext, JiraMCP
from tests.unit.jira.conftest import FIELDS, client, mock, sleeps  # noqa: F401

IDS = JiraIds(
    fields=FIELDS,
    statuses={"Agent Draft": "1"},
    issue_types={"Story": "10009", "Epic": "10006"},
)

Connect = Callable[..., AbstractAsyncContextManager[Client]]


@pytest.fixture
def jira(client: JiraClient) -> JiraContext:  # noqa: F811
    return JiraContext(client=client, ids=IDS, project_key="CODEIT")


@pytest.fixture
def connect(jira: JiraContext) -> Connect:
    """Open an in-process client as a stdio role, or as the holder of a run token."""

    @asynccontextmanager
    async def _connect(
        role: str = "human", *, ticket: str | None = None, run_id: str | None = None
    ) -> AsyncIterator[Client]:
        if run_id is None:
            async with Client(JiraMCP(jira, stdio_role=role)) as c:
                yield c
            return
        claims: dict[str, Any] = {"role": role, "ticket_key": ticket, "run_id": run_id}
        user = AuthenticatedUser(AccessToken(token="t", client_id="c", scopes=[], claims=claims))
        reset = auth_context_var.set(user)
        try:
            async with Client(JiraMCP(jira)) as c:
                yield c
        finally:
            auth_context_var.reset(reset)

    return _connect


@pytest.fixture
def store(tmp_path: Path) -> RunTokenStore:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    return RunTokenStore(db.make_engine(path))
