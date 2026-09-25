"""Glue for running an agent in a worker container: role credentials, the run directory,
the container's MCP config, and the M4 smoke test.

Credentials follow PRD 19: only what the role needs, and never a Jira credential (ADR-0004).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from ulid import ULID

from codeit.backends.base import AgenticRequest, AgenticResult
from codeit.backends.claude_code_agent import ClaudeCodeAgent
from codeit.backends.state import BackendStateStore
from codeit.config import Config, Secrets
from codeit.model_env import claude_model
from codeit.sandbox.containers import RUN_DIR, ContainerSpec, Sandbox

SMOKE_PROMPT = "Use the Bash tool to run `echo hello via bash`. Reply with its output only."


class MissingSecret(Exception):
    pass


def role_env(role: str, secrets: Secrets) -> dict[str, str]:
    """Environment for a worker container of `role` (PRD 19)."""
    env: dict[str, str] = {}
    if role in ("coder", "rebase", "smoke"):
        token = secrets.claude_code_oauth_token
        if token is None or not token.get_secret_value():
            raise MissingSecret("CLAUDE_CODE_OAUTH_TOKEN is not set; run `claude setup-token`")
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.get_secret_value()
        if secrets.github_token_agent is not None:
            env["GH_TOKEN"] = secrets.github_token_agent.get_secret_value()
    elif role == "reviewer" and secrets.github_token_readonly is not None:
        env["GH_TOKEN"] = secrets.github_token_readonly.get_secret_value()
    return env


def write_mcp_config(run_dir: Path, cfg: Config, token: str) -> str:
    """Write the container's `mcp.json` for the host jira-mcp. Returns its container path."""
    url = f"http://{cfg.mcp.container_host}:{cfg.mcp.port}/mcp"
    config = {
        "mcpServers": {
            "codeit-jira": {
                "type": "http",
                "url": url,
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "mcp.json"
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    path.chmod(0o600)
    return f"{RUN_DIR}/mcp.json"


@dataclass(frozen=True)
class SmokeOutcome:
    ok: bool
    result: AgenticResult
    container_log: Path


async def smoke_test(
    cfg: Config, secrets: Secrets, *, sandbox: Sandbox | None = None, keep: bool = False
) -> SmokeOutcome:
    """M4 acceptance: a worker container runs `claude -p` with a Bash call; the transcript
    is saved to data/transcripts/{run_id}.jsonl."""
    from codeit import db

    run_id = str(ULID())
    sandbox = sandbox or Sandbox()
    base = cfg.data_dir / "runs" / run_id
    spec = ContainerSpec.for_role(
        cfg,
        run_id=run_id,
        role="smoke",
        workspace=base / "workspace",
        run_dir=base / "io",
        env=role_env("smoke", secrets),
    )
    db.upgrade(cfg.db_path)
    agent = ClaudeCodeAgent(
        cfg.claude.usage_limit_patterns,
        sandbox,
        cfg.data_dir / "transcripts",
        state=BackendStateStore(db.make_engine(cfg.db_path)),
    )
    container = sandbox.start(spec)
    log_path = cfg.data_dir / "logs" / "containers" / f"{run_id}.log"
    try:
        result = await agent.run_agentic(
            AgenticRequest(
                prompt=SMOKE_PROMPT,
                run_id=run_id,
                container_id=str(container.id),
                max_turns=5,
                tools=["Bash"],
                timeout_s=300,
                model=claude_model(),
            )
        )
    finally:
        sandbox.stop(container, log_path)
        if not keep:
            shutil.rmtree(base, ignore_errors=True)
    ok = result.status == "completed" and "hello via bash" in result.final_message
    return SmokeOutcome(ok, result, log_path)
