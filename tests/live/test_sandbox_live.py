"""Live sandbox checks (M4 acceptance). Need Docker, the worker image and
CLAUDE_CODE_OAUTH_TOKEN; each makes one short Claude call on the subscription."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import timedelta
from pathlib import Path

import pytest
from ulid import ULID

from codeit import db
from codeit.backends.base import AgenticRequest
from codeit.backends.claude_code_agent import ClaudeCodeAgent
from codeit.config import Secrets, load_config
from codeit.run_tokens import RunTokenStore
from codeit.sandbox.containers import ContainerSpec, Sandbox, image_exists
from codeit.sandbox.runner import role_env, smoke_test, write_mcp_config
from mcp_servers.jira.app import build_http_server, jira_context
from tests.conftest import REPO_ROOT
from tests.live.conftest import Factory

CFG = load_config(REPO_ROOT / "config" / "config.yaml")
SECRETS = Secrets(_env_file=REPO_ROOT / ".env")


def _ready() -> bool:
    try:
        return image_exists(Sandbox().client, CFG.sandbox.image)
    except Exception:
        return False


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(not _ready(), reason="Docker or the worker image is not available"),
]


async def test_smoke_transcript_saved(tmp_path: Path) -> None:
    cfg = CFG.model_copy(update={"data_dir": tmp_path / "data"})
    outcome = await smoke_test(cfg, SECRETS)
    assert outcome.ok, outcome.result
    transcript = Path(outcome.result.transcript_path)
    types = [json.loads(line)["type"] for line in transcript.read_text().splitlines()]
    assert types[0] == "codeit_request" and types[-1] == "result"
    assert outcome.result.rate_limit is not None
    assert outcome.container_log.exists()


async def test_container_reaches_host_jira_mcp(new_story: Factory, tmp_path: Path) -> None:
    """ADR-0004/0009: a worker calls the host jira-mcp over HTTP with a run token."""
    key = await new_story("sandbox mcp", description_md="The password is **tangerine**.")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = int(s.getsockname()[1])
    cfg = CFG.model_copy(
        update={"data_dir": tmp_path / "data", "mcp": CFG.mcp.model_copy(update={"port": port})}
    )
    db.upgrade(cfg.db_path)
    store = RunTokenStore(db.make_engine(cfg.db_path))
    run_id = str(ULID())
    token = store.mint("coder", run_id, key, timedelta(minutes=10))
    run_dir = tmp_path / "io"
    run_dir.mkdir()
    mcp_path = write_mcp_config(run_dir, cfg, token)

    sandbox = Sandbox()
    container = sandbox.start(
        ContainerSpec.for_role(
            cfg,
            run_id=run_id,
            role="coder",
            workspace=tmp_path / "ws",
            run_dir=run_dir,
            env=role_env("coder", SECRETS),
        )
    )
    try:
        async with jira_context(cfg, REPO_ROOT / "config" / "jira_ids.yaml") as jira:
            server = build_http_server(jira, cfg, store)
            task = asyncio.create_task(server.serve())
            while not server.started:  # noqa: ASYNC110 - uvicorn exposes only a flag
                await asyncio.sleep(0.05)
            try:
                agent = ClaudeCodeAgent(CFG.claude.usage_limit_patterns, sandbox, tmp_path / "t")
                result = await agent.run_agentic(
                    AgenticRequest(
                        prompt=f"Call get_ticket for {key}. Reply with only the password in it.",
                        run_id=run_id,
                        container_id=str(container.id),
                        max_turns=5,
                        tools=[],
                        allowed_tools=["mcp__codeit-jira__get_ticket"],
                        mcp_config_path=mcp_path,
                        timeout_s=300,
                    )
                )
            finally:
                server.should_exit = True
                await task
    finally:
        sandbox.stop(container)
    assert result.status == "completed", result
    assert "tangerine" in result.final_message.lower()
