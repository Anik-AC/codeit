"""Live jira-mcp checks (M2 acceptance). Run with `LIVE=1`.

- stdio, as the owner uses it from interactive Claude Code
- HTTP with a run token, driven by a real `claude -p` (PRD 15)
"""

from __future__ import annotations

import asyncio
import json
import shutil
import socket
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client, StdioServerParameters

from codeit import db
from codeit.config import load_config
from codeit.run_tokens import RunTokenStore
from mcp_servers.jira.app import build_http_server, jira_context
from tests.conftest import REPO_ROOT
from tests.live.conftest import Factory

pytestmark = pytest.mark.live

CONFIG = REPO_ROOT / "config" / "config.yaml"
IDS = REPO_ROOT / "config" / "jira_ids.yaml"


async def test_stdio_get_ticket(new_story: Factory) -> None:
    key = await new_story("mcp stdio", description_md="Hello from **stdio**")
    params = StdioServerParameters(
        command="uv", args=["run", "--quiet", "codeit", "mcp", "serve", "--stdio"], cwd=REPO_ROOT
    )
    async with Client(params) as c:
        tools = {t.name for t in (await c.list_tools()).tools}
        r = await c.call_tool("get_ticket", {"key": key})
    assert "get_ticket" in tools
    assert not r.is_error
    assert "Hello from **stdio**" in r.content[0].text  # type: ignore[union-attr]


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
async def test_claude_fetches_ticket_over_http(new_story: Factory, tmp_path: Path) -> None:
    summary_word = "zanzibar"
    key = await new_story(f"mcp http {summary_word}")
    cfg = load_config(CONFIG)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    cfg = cfg.model_copy(update={"mcp": cfg.mcp.model_copy(update={"port": port})})
    db.upgrade(tmp_path / "codeit.db")
    store = RunTokenStore(db.make_engine(tmp_path / "codeit.db"))
    token = store.mint("coder", "LIVETEST", key, timedelta(minutes=10))
    mcp_json = tmp_path / "mcp.json"
    server_entry = {
        "type": "http",
        "url": f"http://127.0.0.1:{port}/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }
    mcp_json.write_text(json.dumps({"mcpServers": {"codeit-jira": server_entry}}))

    async with jira_context(cfg, IDS) as jira:
        server = build_http_server(jira, cfg, store)
        task = asyncio.create_task(server.serve())
        while not server.started:  # noqa: ASYNC110 - uvicorn exposes only a flag
            await asyncio.sleep(0.05)
        try:
            proc = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                f"Call the get_ticket tool for {key} and reply with only the ticket's summary.",
                "--mcp-config",
                str(mcp_json),
                "--strict-mcp-config",
                "--allowedTools",
                "mcp__codeit-jira__get_ticket",
                "--max-turns",
                "3",
                "--output-format",
                "json",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            out, err = await asyncio.wait_for(proc.communicate(), 240)
        finally:
            server.should_exit = True
            await task
    assert proc.returncode == 0, err.decode()[-2000:]
    reply = json.loads(out)
    assert not reply.get("is_error"), reply
    assert summary_word in str(reply.get("result", "")).lower()
