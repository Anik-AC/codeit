"""The HTTP transport end to end: uvicorn, SDK bearer auth, run tokens in SQLite."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from datetime import timedelta

import httpx2
import pytest
import respx
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from codeit.config import load_config
from codeit.run_tokens import RunTokenStore
from mcp_servers.jira.app import build_http_server
from mcp_servers.jira.roles import ROLE_TOOLS
from mcp_servers.jira.server import JiraContext
from tests.conftest import REPO_ROOT

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "t", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream"}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def url(jira: JiraContext, store: RunTokenStore) -> AsyncIterator[str]:
    cfg = load_config(REPO_ROOT / "config" / "config.yaml")
    port = free_port()
    cfg = cfg.model_copy(update={"mcp": cfg.mcp.model_copy(update={"port": port})})
    server = build_http_server(jira, cfg, store)
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110 - uvicorn exposes only a flag
        await asyncio.sleep(0.02)
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    await task


def client_for(url: str, token: str) -> Client:
    http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"})
    return Client(streamable_http_client(url, http_client=http))


async def raw_post(url: str, headers: dict[str, str]) -> int:
    async with httpx2.AsyncClient() as http:
        resp = await http.post(url, json=INIT, headers={**HEADERS, **headers})
        return resp.status_code


async def test_missing_token_is_401(url: str) -> None:
    assert await raw_post(url, {}) == 401


async def test_unknown_token_is_401(url: str) -> None:
    assert await raw_post(url, {"Authorization": "Bearer made-up"}) == 401


async def test_revoked_token_is_401(url: str, store: RunTokenStore) -> None:
    token = store.mint("coder", "R1", "CODEIT-5", timedelta(minutes=5))
    assert await raw_post(url, {"Authorization": f"Bearer {token}"}) == 200
    store.revoke_run("R1")
    assert await raw_post(url, {"Authorization": f"Bearer {token}"}) == 401


async def test_foreign_host_header_is_rejected(url: str, store: RunTokenStore) -> None:
    token = store.mint("coder", "R1", "CODEIT-5", timedelta(minutes=5))
    status = await raw_post(url, {"Authorization": f"Bearer {token}", "Host": "evil.example"})
    assert status in (400, 403, 421)


async def test_coder_token_end_to_end(
    url: str, store: RunTokenStore, mock: respx.MockRouter
) -> None:
    token = store.mint("coder", "RUN42", "CODEIT-5", timedelta(minutes=5))
    own = mock.post("/issue/CODEIT-5/comment").respond(201, json={"id": "9"})
    other = mock.post("/issue/CODEIT-6/comment")
    async with client_for(url, token) as c:
        names = {t.name for t in (await c.list_tools()).tools}
        ok = await c.call_tool("add_comment", {"key": "CODEIT-5", "markdown": "done"})
        refused = await c.call_tool("add_comment", {"key": "CODEIT-6", "markdown": "sneaky"})
        hidden = await c.call_tool("create_issue", {"type": "Story", "summary": "x"})
    assert names == ROLE_TOOLS["coder"]
    assert not ok.is_error
    assert b"RUN42" in own.calls.last.request.content
    assert refused.is_error and not other.called
    assert hidden.is_error


async def test_planner_token_sees_planner_tools(url: str, store: RunTokenStore) -> None:
    token = store.mint("planner", "RUN7", None, timedelta(minutes=5))
    async with client_for(url, token) as c:
        names = {t.name for t in (await c.list_tools()).tools}
    assert names == ROLE_TOOLS["planner"]
