"""`codeit up` (PRD 12.1): jira-mcp, the dashboard API, the scheduler loop and the merge
watcher in one asyncio process. The rebase poller arrives in M10, the cron jobs in M11
and M12."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import uvicorn
from sqlalchemy import Engine

from codeit import db
from codeit.config import Config, Secrets
from codeit.github_client import GitHubClient, repo_slug
from codeit.jira_client import JiraClient, JiraIds
from codeit.orchestrator.api import ApiContext, create_app
from codeit.orchestrator.budget import Budget
from codeit.orchestrator.bus import EventBus
from codeit.orchestrator.merge_watcher import MergeWatcher
from codeit.orchestrator.scheduler import Orchestrator, install_signal_handlers
from codeit.run_tokens import RunTokenStore
from codeit.sandbox.clone import CloneManager
from codeit.sandbox.containers import Sandbox, SandboxError, image_exists
from codeit.sandbox.egress import EgressError, EgressSettings, ensure_proxy
from mcp_servers.jira.app import running_http_server
from mcp_servers.jira.server import JiraContext

Echo = Callable[[str], None]


class StartupError(Exception):
    pass


def preflight(cfg: Config, secrets: Secrets, sandbox: Sandbox) -> list[str]:
    """Fail fast on what every run needs; return warnings for what only some need."""
    missing = [
        name
        for name, value in (
            ("GITHUB_TOKEN_AGENT", secrets.github_token_agent),
            ("CLAUDE_CODE_OAUTH_TOKEN", secrets.claude_code_oauth_token),
        )
        if value is None or not value.get_secret_value()
    ]
    if missing:
        raise StartupError(f"missing in .env: {', '.join(missing)}")
    try:
        if not image_exists(sandbox.client, cfg.sandbox.image):
            raise StartupError(
                f"worker image {cfg.sandbox.image} missing; run `codeit sandbox build`"
            )
        egress = EgressSettings.from_config(cfg)
        if egress is not None:
            ensure_proxy(sandbox.client, egress)
    except (SandboxError, EgressError) as e:
        raise StartupError(str(e)) from e
    warnings = []
    if secrets.openrouter_key() is None:
        warnings.append("no OPENROUTER_API_KEY: the Reviewer will not start")
    if egress is None:
        warnings.append("sandbox.egress_proxy is off: workers have unrestricted network access")
    if secrets.codeit_api_token is None or not secrets.codeit_api_token.get_secret_value():
        warnings.append("no CODEIT_API_TOKEN: the dashboard and API are off")
    return warnings


async def serve(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    *,
    config_path: Path | None = None,
    any_time: bool = False,
    echo: Echo = print,
    stop: asyncio.Event | None = None,
) -> None:
    sandbox = Sandbox()
    for w in preflight(cfg, secrets, sandbox):
        echo(f"warning: {w}")
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    repo = cfg.project.target_repo
    assert secrets.github_token_agent is not None  # checked by preflight
    read_token = (secrets.github_token_readonly or secrets.github_token_agent).get_secret_value()
    stop = stop or asyncio.Event()
    install_signal_handlers(stop)
    async with JiraClient.from_secrets(secrets) as jira, GitHubClient(read_token) as gh:
        jira_ctx = JiraContext(jira, ids, cfg.project.jira_project_key)
        async with running_http_server(jira_ctx, cfg, RunTokenStore(engine)) as started:
            echo(
                f"jira-mcp on {cfg.mcp.host}:{cfg.mcp.port}"
                + ("" if started else " (already running; using it)")
            )
            budget = Budget(
                cfg,
                engine,
                has_openrouter_key=secrets.openrouter_key() is not None,
                any_time=any_time,
            )
            window = "any time" if any_time else ", ".join(cfg.claude.run_window) or "any time"
            echo(
                f"orchestrator up: poll {cfg.orchestrator.poll_seconds}s, slots {dict(cfg.slots)}, "
                f"Claude window {window}. Ctrl-C to stop."
            )
            watcher = MergeWatcher(
                engine,
                jira,
                gh,
                ids,
                cfg.project.jira_project_key,
                repo_slug(repo.url),
                CloneManager(cfg.data_dir, repo.name, repo.url, repo.default_branch),
            )
            bus = EventBus()
            orchestrator = Orchestrator(
                cfg,
                secrets,
                ids,
                engine=engine,
                jira=jira,
                budget=budget,
                merge_watcher=watcher,
                sandbox=sandbox,
                config_path=config_path,
                echo=echo,
                bus=bus,
            )
            async with api_server(cfg, secrets, engine, bus, budget, orchestrator, echo):
                await orchestrator.run_forever(stop)
    echo("orchestrator stopped")


@contextlib.asynccontextmanager
async def api_server(
    cfg: Config,
    secrets: Secrets,
    engine: Engine,
    bus: EventBus,
    budget: Budget,
    orchestrator: Orchestrator,
    echo: Echo,
) -> AsyncIterator[None]:
    """The dashboard API on 127.0.0.1 for the duration of the block (PRD 14)."""
    token = secrets.codeit_api_token.get_secret_value() if secrets.codeit_api_token else ""
    if not token:
        yield
        return
    app = create_app(ApiContext(cfg, engine, token, bus, budget, orchestrator))
    server = uvicorn.Server(
        uvicorn.Config(app, host=cfg.api.host, port=cfg.api.port, log_level="warning")
    )
    task = asyncio.create_task(server.serve())
    while not server.started and not task.done():  # noqa: ASYNC110 - uvicorn exposes a flag
        await asyncio.sleep(0.05)
    if task.done():
        echo(f"warning: dashboard API did not start on port {cfg.api.port}")
    else:
        echo(f"dashboard on http://{cfg.api.host}:{cfg.api.port}")
    try:
        yield
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await task
