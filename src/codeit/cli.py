"""Typer entrypoint for the `codeit` command (PRD 12.6).

Commands owned by later milestones are registered now as stubs so the CLI shape is stable;
each stub names the milestone that implements it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from codeit import __version__, db
from codeit.config import DEFAULT_CONFIG_PATH, Config, ConfigError, Secrets, load_config
from codeit.jira_client import JiraClient, JiraConfigError, JiraError
from codeit.jira_client.discover import DEFAULT_IDS_PATH, DiscoveryError, discover, write_ids
from codeit.jira_client.doctor import Check, run_doctor

app = typer.Typer(help="CodeIt: local multi-agent software delivery.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
jira_app = typer.Typer(help="Jira setup checks and ID discovery.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Worker image and clone management.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluation harness.", no_args_is_help=True)
mcp_app = typer.Typer(help="Host-side jira-mcp server and run tokens.", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(jira_app, name="jira")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(eval_app, name="eval")
app.add_typer(mcp_app, name="mcp")

ConfigPath = Annotated[
    Path, typer.Option("--config", "-c", help="Path to config.yaml.", dir_okay=False)
]


def _not_implemented(milestone: str) -> NoReturn:
    typer.echo(f"Not implemented yet (planned for {milestone}).", err=True)
    raise typer.Exit(code=2)


def _load(path: Path) -> Config:
    try:
        return load_config(path)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"codeit {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """CodeIt: local multi-agent software delivery."""


# config ---------------------------------------------------------------------------------------


@config_app.command("validate")
def config_validate(config: ConfigPath = DEFAULT_CONFIG_PATH) -> None:
    """Validate config.yaml and report every problem found."""
    from codeit.model_env import effective_models, env_var_for

    cfg = _load(config)
    try:
        models = effective_models(cfg)
    except ConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(
        f"OK: {config} (project {cfg.project.jira_project_key}, "
        f"target repo {cfg.project.target_repo.name})"
    )
    for name, chosen in models.items():
        source = "from .env" if chosen != cfg.models[name] else "default"
        shown = ", ".join(chosen) if isinstance(chosen, list) else chosen
        typer.echo(f"  {name} ({env_var_for(name)}, {source}): {shown}")
    if not Secrets().openrouter_key():
        typer.echo("  note: no OpenRouter key in .env; OpenRouter backends are disabled")


# db -------------------------------------------------------------------------------------------


@db_app.command("upgrade")
def db_upgrade(config: ConfigPath = DEFAULT_CONFIG_PATH) -> None:
    """Create or migrate the SQLite database to the latest schema."""
    cfg = _load(config)
    db.upgrade(cfg.db_path)
    typer.echo(f"Database at {cfg.db_path} is up to date.")


# jira -----------------------------------------------------------------------------------------


def _jira_client() -> JiraClient:
    try:
        return JiraClient.from_secrets(Secrets())
    except JiraConfigError as e:
        typer.echo(f"{e}. See .env.example.", err=True)
        raise typer.Exit(code=1) from e


def _print_checks(checks: list[Check]) -> None:
    width = max(len(c.name) for c in checks)
    for c in checks:
        typer.echo(f"{'PASS' if c.ok else 'FAIL'}  {c.name:<{width}}  {c.detail}")
        if c.hint:
            typer.echo(f"      {'':<{width}}  hint: {c.hint}")


@jira_app.command("doctor")
def jira_doctor(
    write_test: Annotated[
        bool, typer.Option("--write-test", help="Also create and delete a test issue.")
    ] = False,
    config: ConfigPath = DEFAULT_CONFIG_PATH,
) -> None:
    """Check auth, project, statuses, custom fields and transitions."""
    cfg = _load(config)

    async def go() -> list[Check]:
        async with _jira_client() as client:
            return await run_doctor(client, cfg.project.jira_project_key, write_test=write_test)

    try:
        checks = asyncio.run(go())
    except JiraError as e:
        typer.echo(f"Jira error: {e}", err=True)
        raise typer.Exit(code=1) from e
    _print_checks(checks)
    if not all(c.ok for c in checks):
        raise typer.Exit(code=1)


@jira_app.command("discover")
def jira_discover(
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    out: Annotated[
        Path, typer.Option("--out", help="Where to write the IDs.", dir_okay=False)
    ] = DEFAULT_IDS_PATH,
) -> None:
    """Discover Jira field, status, issue type and transition IDs into config/jira_ids.yaml."""
    cfg = _load(config)

    async def go() -> None:
        async with _jira_client() as client:
            ids = await discover(client, cfg.project.jira_project_key)
        write_ids(ids, out)
        typer.echo(
            f"Wrote {out}: {len(ids.statuses)} statuses, {len(ids.issue_types)} work types, "
            f"{len(ids.transitions)} transitions."
        )
        if not ids.transitions:
            typer.echo("No issues to sample transitions from; they will be fetched on use.")

    try:
        asyncio.run(go())
    except DiscoveryError as e:
        typer.echo("Discovery failed:", err=True)
        for problem in e.problems:
            typer.echo(f"  - {problem}", err=True)
        typer.echo("Run `codeit jira doctor` for remediation hints.", err=True)
        raise typer.Exit(code=1) from e
    except JiraError as e:
        typer.echo(f"Jira error: {e}", err=True)
        raise typer.Exit(code=1) from e


# mcp ------------------------------------------------------------------------------------------

IdsPath = Annotated[Path, typer.Option("--ids", help="Path to jira_ids.yaml.", dir_okay=False)]


@mcp_app.command("serve")
def mcp_serve(
    stdio: Annotated[
        bool, typer.Option("--stdio", help="Serve over stdio for interactive use on the host.")
    ] = False,
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    ids: IdsPath = DEFAULT_IDS_PATH,
) -> None:
    """Run jira-mcp: HTTP with run tokens (default), or stdio as CODEIT_ROLE (default human)."""
    from mcp_servers.jira.app import serve_http, serve_stdio
    from mcp_servers.jira.roles import ROLE_TOOLS

    cfg = _load(config)
    try:
        if stdio:
            # stdout carries the MCP protocol: all messages here go to stderr.
            role = os.environ.get("CODEIT_ROLE", "human")
            if role not in ROLE_TOOLS:
                typer.echo(f"CODEIT_ROLE={role!r} is not one of {sorted(ROLE_TOOLS)}", err=True)
                raise typer.Exit(code=1)
            asyncio.run(serve_stdio(cfg, ids, role))
        else:
            typer.echo(
                f"jira-mcp on http://{cfg.mcp.host}:{cfg.mcp.port}/mcp (run tokens required)",
                err=True,
            )
            asyncio.run(serve_http(cfg, ids))
    except (JiraConfigError, FileNotFoundError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e


@mcp_app.command("token")
def mcp_token(
    role: Annotated[str, typer.Option("--role", help="Role the token grants.")],
    ticket: Annotated[
        str | None, typer.Option("--ticket", help="Ticket the run may comment on.")
    ] = None,
    run_id: Annotated[str | None, typer.Option("--run-id", help="Defaults to a new ULID.")] = None,
    ttl_minutes: Annotated[
        int | None,
        typer.Option("--ttl-minutes", min=1, help="Defaults to the role timeout plus grace."),
    ] = None,
    config: ConfigPath = DEFAULT_CONFIG_PATH,
) -> None:
    """Mint a run token by hand, for testing before the orchestrator exists (M7).

    Prints only the token on stdout.
    """
    from datetime import timedelta

    from ulid import ULID

    from codeit.run_tokens import RunTokenStore, token_ttl

    cfg = _load(config)
    db.upgrade(cfg.db_path)
    store = RunTokenStore(db.make_engine(cfg.db_path))
    ttl = timedelta(minutes=ttl_minutes) if ttl_minutes else token_ttl(cfg, role)
    run = run_id or str(ULID())
    try:
        token = store.mint(role, run, ticket.upper() if ticket else None, ttl)
    except ValueError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"run {run}, role {role}, ticket {ticket or '-'}, expires in {ttl}", err=True)
    typer.echo(token)


@mcp_app.command("revoke")
def mcp_revoke(run_id: str, config: ConfigPath = DEFAULT_CONFIG_PATH) -> None:
    """Revoke every token of a run."""
    from codeit.run_tokens import RunTokenStore

    cfg = _load(config)
    db.upgrade(cfg.db_path)
    count = RunTokenStore(db.make_engine(cfg.db_path)).revoke_run(run_id)
    typer.echo(f"Revoked {count} token(s) for run {run_id}.")


# later milestones ----------------------------------------------------------------------------


@app.command("plan")
def plan(
    file: Annotated[
        Path,
        typer.Argument(
            help="A plan in markdown, or a saved plan (data/plans/<run>.json) to apply.",
            exists=True,
            dir_okay=False,
        ),
    ],
    epic: Annotated[
        str | None,
        typer.Option("--epic", help="Name for the new Epic, or an existing Epic key."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Draft and save the plan; create nothing.")
    ] = False,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Target repo checkout. Default: data/repos/<target repo>."),
    ] = None,
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    ids: IdsPath = DEFAULT_IDS_PATH,
) -> None:
    """Turn a plan into an Epic and Stories in Agent Draft (PRD 11.1)."""
    from codeit.agents.planner import PlannerError
    from codeit.agents.planner_run import apply_saved, run_planner
    from codeit.jira_client.discover import load_ids

    cfg = _load(config)
    try:
        jira_ids = load_ids(ids)
        if file.suffix == ".json":
            if dry_run or epic:
                typer.echo("--dry-run and --epic apply only to markdown plans.", err=True)
                raise typer.Exit(code=2)
            asyncio.run(apply_saved(cfg, Secrets(), jira_ids, file, echo=typer.echo))
            return
        outcome = asyncio.run(
            run_planner(
                cfg,
                Secrets(),
                jira_ids,
                file,
                dry_run=dry_run,
                epic=epic,
                repo_path=repo,
                echo=typer.echo,
            )
        )
    except (PlannerError, JiraError, FileNotFoundError) as e:
        typer.echo(f"Planner failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"\nSaved {outcome.path}")
    if dry_run:
        typer.echo(f"Nothing created. To create exactly this: codeit plan {outcome.path}")


@app.command("run")
def run(role: str, key: Annotated[str | None, typer.Argument()] = None) -> None:
    """Run one agent once, optionally on a ticket KEY."""
    _not_implemented("M5 and later")


@sandbox_app.command("build")
def sandbox_build(
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    context: Annotated[
        Path, typer.Option("--context", help="Build context with the Dockerfile.", file_okay=False)
    ] = Path("sandbox"),
) -> None:
    """Build the worker image (tag from sandbox.image in config)."""
    from codeit.sandbox.containers import build_image

    cfg = _load(config)
    code = build_image(context, cfg.sandbox.image)
    if code != 0:
        raise typer.Exit(code=code)
    typer.echo(f"Built {cfg.sandbox.image}")


@sandbox_app.command("smoke")
def sandbox_smoke(
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    keep: Annotated[bool, typer.Option("--keep", help="Keep the run directory.")] = False,
) -> None:
    """Run `claude -p` in a worker container with one Bash call (M4 acceptance)."""
    from codeit.sandbox.containers import SandboxError
    from codeit.sandbox.runner import MissingSecret, smoke_test

    cfg = _load(config)
    try:
        outcome = asyncio.run(smoke_test(cfg, Secrets(), keep=keep))
    except (MissingSecret, SandboxError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    r = outcome.result
    typer.echo(f"status: {r.status}, turns: {r.turns}, model: {r.model}")
    typer.echo(f"reply: {r.final_message.strip()[:200]}")
    typer.echo(f"transcript: {r.transcript_path}")
    typer.echo(f"container log: {outcome.container_log}")
    if not outcome.ok:
        raise typer.Exit(code=1)


@sandbox_app.command("gc")
def sandbox_gc(
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    days: Annotated[int, typer.Option("--days", min=1, help="Remove clones older than this.")] = 14,
    containers: Annotated[
        bool,
        typer.Option(
            "--containers", help="Also remove all worker containers (orchestrator stopped)."
        ),
    ] = False,
) -> None:
    """Remove clones of Done or Rejected tickets, and clones older than --days."""
    from codeit.sandbox.clone import CloneManager
    from codeit.sandbox.gc import collect_garbage

    cfg = _load(config)
    repo = cfg.project.target_repo
    clones = CloneManager(cfg.data_dir, repo.name, repo.url, repo.default_branch)
    try:
        removed = asyncio.run(collect_garbage(clones, Secrets(), max_age_days=days))
    except JiraConfigError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    for key, reason in removed:
        typer.echo(f"removed {key} ({reason})")
    typer.echo(f"{len(removed)} clone(s) removed.")
    if containers:
        from codeit.sandbox.containers import Sandbox

        for name in Sandbox().reap():
            typer.echo(f"removed container {name}")


@app.command("init-target")
def init_target(path: Path) -> None:
    """Copy the steering kit into a target repo without overwriting files."""
    _not_implemented("M5")


@app.command("up")
def up() -> None:
    """Start the orchestrator, API, watchers and cron jobs."""
    _not_implemented("M7")


@app.command("budget")
def budget() -> None:
    """Show backend budget state."""
    _not_implemented("M7")


@app.command("agents")
def agents() -> None:
    """Show agent instances and their state."""
    _not_implemented("M7")


@eval_app.command("run")
def eval_run(
    suite: Annotated[str, typer.Option("--suite")] = "golden",
    config_name: Annotated[str, typer.Option("--config")] = "default",
    repeats: Annotated[int, typer.Option("--repeats")] = 1,
) -> None:
    """Run an eval suite."""
    _not_implemented("M9")


@eval_app.command("report")
def eval_report(compare: Annotated[str | None, typer.Option("--compare")] = None) -> None:
    """Print or compare eval results."""
    _not_implemented("M9")
