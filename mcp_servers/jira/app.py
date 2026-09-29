"""Entry points: run jira-mcp over stdio (owner, host) or HTTP (worker containers)."""

from __future__ import annotations

import asyncio
import os
import socket
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn

from codeit import db
from codeit.config import Config, Secrets
from codeit.jira_client import JiraClient
from codeit.jira_client.discover import load_ids
from codeit.run_tokens import RunTokenStore
from mcp_servers.jira.server import JiraContext, JiraMCP, RunTokenVerifier


@asynccontextmanager
async def jira_context(cfg: Config, ids_path: Path) -> AsyncIterator[JiraContext]:
    ids = load_ids(ids_path)
    async with JiraClient.from_secrets(Secrets()) as client:
        yield JiraContext(client=client, ids=ids, project_key=cfg.project.jira_project_key)


async def serve_stdio(cfg: Config, ids_path: Path, role: str) -> None:
    async with jira_context(cfg, ids_path) as jira:
        await JiraMCP(jira, stdio_role=role).run_stdio_async()


def build_http_server(jira: JiraContext, cfg: Config, store: RunTokenStore) -> uvicorn.Server:
    server = JiraMCP(jira, verifier=RunTokenVerifier(store))
    app = server.http_app(cfg.mcp.allowed_hosts)
    return uvicorn.Server(
        uvicorn.Config(app, host=cfg.mcp.host, port=cfg.mcp.port, log_level="warning")
    )


def port_in_use(host: str, port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


@asynccontextmanager
async def running_http_server(
    jira: JiraContext, cfg: Config, store: RunTokenStore
) -> AsyncIterator[bool]:
    """Run jira-mcp over HTTP for the duration of the block, unless one already listens
    on the configured port (e.g. `codeit mcp serve`). Yields whether it started one."""
    if port_in_use(cfg.mcp.host, cfg.mcp.port):
        yield False
        return
    server = build_http_server(jira, cfg, store)
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():  # noqa: ASYNC110 - uvicorn exposes a flag
        await asyncio.sleep(0.05)
    try:
        yield True
    finally:
        server.should_exit = True
        await task


async def serve_http(cfg: Config, ids_path: Path) -> None:
    db.upgrade(cfg.db_path)
    store = RunTokenStore(db.make_engine(cfg.db_path))
    async with jira_context(cfg, ids_path) as jira:
        await build_http_server(jira, cfg, store).serve()


def main_stdio() -> None:
    """Console script `codeit-jira-mcp`: the stdio server with no flags, runnable from any cwd.

    MCP clients such as Claude Code start stdio servers in the user's project directory, so
    this switches to the CodeIt checkout (`CODEIT_HOME`, default: this repo) before reading
    `config/`, `.env` and `jira_ids.yaml`. Role comes from `CODEIT_ROLE` (default human).
    """
    from codeit.config import DEFAULT_CONFIG_PATH, ConfigError, load_config
    from codeit.jira_client import JiraConfigError
    from codeit.jira_client.discover import DEFAULT_IDS_PATH
    from mcp_servers.jira.roles import ROLE_TOOLS

    os.chdir(os.environ.get("CODEIT_HOME") or Path(__file__).resolve().parents[2])
    role = os.environ.get("CODEIT_ROLE", "human")
    try:
        if role not in ROLE_TOOLS:
            raise ValueError(f"CODEIT_ROLE={role!r} is not one of {sorted(ROLE_TOOLS)}")
        asyncio.run(serve_stdio(load_config(DEFAULT_CONFIG_PATH), DEFAULT_IDS_PATH, role))
    except (ConfigError, JiraConfigError, FileNotFoundError, ValueError) as e:
        print(f"codeit-jira-mcp: {e}", file=sys.stderr)
        raise SystemExit(1) from e
