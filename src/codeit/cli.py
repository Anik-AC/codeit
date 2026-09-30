"""Typer entrypoint for the `codeit` command (PRD 12.6).

Commands owned by later milestones are registered now as stubs so the CLI shape is stable;
each stub names the milestone that implements it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, NoReturn

import typer

from codeit import __version__, db
from codeit.config import DEFAULT_CONFIG_PATH, Config, ConfigError, Secrets, load_config
from codeit.jira_client import JiraClient, JiraConfigError, JiraError
from codeit.jira_client.discover import DEFAULT_IDS_PATH, DiscoveryError, discover, write_ids
from codeit.jira_client.doctor import Check, run_doctor

if TYPE_CHECKING:
    from codeit.evals.runner import EvalContext

app = typer.Typer(help="CodeIt: local multi-agent software delivery.", no_args_is_help=True)
config_app = typer.Typer(help="Inspect and validate configuration.", no_args_is_help=True)
db_app = typer.Typer(help="Database migrations.", no_args_is_help=True)
jira_app = typer.Typer(help="Jira setup checks and ID discovery.", no_args_is_help=True)
sandbox_app = typer.Typer(help="Worker image and clone management.", no_args_is_help=True)
eval_app = typer.Typer(help="Evaluation harness.", no_args_is_help=True)
mcp_app = typer.Typer(help="Host-side jira-mcp server and run tokens.", no_args_is_help=True)
dashboard_app = typer.Typer(help="The web dashboard (served by `codeit up`).", no_args_is_help=True)

app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(jira_app, name="jira")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(eval_app, name="eval")
app.add_typer(mcp_app, name="mcp")
app.add_typer(dashboard_app, name="dashboard")

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
def run(
    role: Annotated[str, typer.Argument(help="Agent role, e.g. coder.")],
    key: Annotated[str | None, typer.Argument(help="Ticket key, e.g. CODEIT-12.")] = None,
    file: Annotated[
        Path | None,
        typer.Option(
            "--file",
            help="Coder only: a ticket in markdown; runs locally, no Jira.",
            exists=True,
            dir_okay=False,
        ),
    ] = None,
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    ids: IdsPath = DEFAULT_IDS_PATH,
    any_time: Annotated[
        bool,
        typer.Option("--any-time", help="Rebase: let Claude resolve conflicts outside its window."),
    ] = False,
) -> None:
    """Run one agent once on a ticket."""
    if role == "reviewer":
        _run_reviewer(key, config, ids)
        return
    if role == "rebase":
        _run_rebase(key, config, ids, any_time)
        return
    if role != "coder":
        later = {"docs": "M11", "learning": "M12"}
        _not_implemented(later.get(role, "later milestones"))
    from codeit.agents.coder_run import CoderError, run_coder, run_coder_local
    from codeit.jira_client.discover import load_ids
    from codeit.sandbox.containers import SandboxError
    from codeit.sandbox.runner import MissingSecret

    cfg = _load(config)
    try:
        if file is not None:
            outcome = asyncio.run(
                run_coder_local(cfg, Secrets(), file, key or "LOCAL-1", echo=typer.echo)
            )
        elif key is None:
            typer.echo("Give a ticket KEY, or --file for a local run.", err=True)
            raise typer.Exit(code=2)
        else:
            outcome = asyncio.run(
                run_coder(cfg, Secrets(), load_ids(ids), key.upper(), echo=typer.echo)
            )
    except (CoderError, JiraError, MissingSecret, SandboxError, FileNotFoundError) as e:
        typer.echo(f"Coder failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"status: {outcome.outcome.status}")
    if outcome.outcome.pr_url:
        typer.echo(f"pr: {outcome.outcome.pr_url}")
    if outcome.agent is not None:
        a = outcome.agent
        typer.echo(f"turns: {a.turns}, model: {a.model}, transcript: {a.transcript_path}")
    typer.echo(f"workspace: {outcome.workspace}")
    if outcome.outcome.status not in ("pr_opened", "pr_updated", "committed"):
        raise typer.Exit(code=1)


def _run_rebase(key: str | None, config: Path, ids: Path, any_time: bool) -> None:
    from codeit.agents.rebase import RebaseError, run_rebase
    from codeit.jira_client.discover import load_ids
    from codeit.orchestrator.budget import Decision
    from codeit.sandbox.containers import SandboxError

    if key is None:
        typer.echo("Give a ticket KEY.", err=True)
        raise typer.Exit(code=2)
    cfg = _load(config)
    try:
        result = asyncio.run(
            run_rebase(
                cfg,
                Secrets(),
                load_ids(ids),
                key.upper(),
                echo=typer.echo,
                claude_check=(lambda: Decision(True)) if any_time else None,
            )
        )
    except (RebaseError, JiraError, SandboxError, FileNotFoundError) as e:
        typer.echo(f"Rebase failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"action: {result.action}")
    if result.new_head:
        typer.echo(f"new head: {result.new_head}")
    if result.action == "escalated":
        raise typer.Exit(code=1)


def _run_reviewer(key: str | None, config: Path, ids: Path) -> None:
    from codeit.agents.reviewer.run import ReviewerError, run_reviewer
    from codeit.jira_client.discover import load_ids
    from codeit.sandbox.containers import SandboxError

    if key is None:
        typer.echo("Give a ticket KEY.", err=True)
        raise typer.Exit(code=2)
    cfg = _load(config)
    try:
        jira_ids = load_ids(ids)
        result = asyncio.run(run_reviewer(cfg, Secrets(), jira_ids, key.upper(), echo=typer.echo))
    except (ReviewerError, JiraError, SandboxError, FileNotFoundError) as e:
        typer.echo(f"Reviewer failed: {e}", err=True)
        raise typer.Exit(code=1) from e
    typer.echo(f"verdict: {result.verdict.verdict if result.verdict else 'incomplete'}")
    typer.echo(f"route: {result.routing.route} ({result.routing.reason})")
    if result.review_url:
        typer.echo(f"review: {result.review_url}")


@sandbox_app.command("build")
def sandbox_build(
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    context: Annotated[
        Path, typer.Option("--context", help="Build context with the Dockerfile.", file_okay=False)
    ] = Path("sandbox"),
) -> None:
    """Build the worker image and the egress proxy image (tags from config)."""
    from codeit.sandbox.containers import build_image
    from codeit.sandbox.egress import PROXY_CONTEXT

    cfg = _load(config)
    for ctx, tag in ((context, cfg.sandbox.image), (PROXY_CONTEXT, cfg.sandbox.proxy_image)):
        code = build_image(ctx, tag)
        if code != 0:
            raise typer.Exit(code=code)
        typer.echo(f"Built {tag}")


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
def init_target_cmd(
    path: Annotated[Path, typer.Argument(help="The target repo checkout.", file_okay=False)],
    name: Annotated[str | None, typer.Option("--name", help="Project name for CLAUDE.md.")] = None,
) -> None:
    """Copy the steering kit (CLAUDE.md, skills, hooks) into a target repo. Never overwrites."""
    from codeit.init_target import init_target

    try:
        result = init_target(path, project_name=name)
    except FileNotFoundError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1) from e
    for rel in result.created:
        typer.echo(f"created  {rel}")
    for rel in result.skipped:
        typer.echo(f"exists   {rel} (left unchanged)")
    if result.created:
        typer.echo("Fill in the placeholder sections of CLAUDE.md, then commit.")


@app.command("up")
def up(
    config: ConfigPath = DEFAULT_CONFIG_PATH,
    ids: IdsPath = DEFAULT_IDS_PATH,
    any_time: Annotated[
        bool, typer.Option("--any-time", help="Ignore claude.run_window for this session.")
    ] = False,
) -> None:
    """Start the orchestrator: jira-mcp, the scheduler loop and the merge watcher."""
    from codeit.jira_client.discover import load_ids
    from codeit.log import configure_logging
    from codeit.orchestrator.serve import StartupError, serve

    cfg = _load(config)
    log_file = configure_logging(cfg.log_dir, console=False)
    typer.echo(f"log: {log_file}")
    try:
        asyncio.run(
            serve(
                cfg,
                Secrets(),
                load_ids(ids),
                config_path=config,
                any_time=any_time,
                echo=typer.echo,
            )
        )
    except (StartupError, JiraConfigError, FileNotFoundError) as e:
        typer.echo(f"Cannot start: {e}", err=True)
        raise typer.Exit(code=1) from e


@app.command("fast-lane")
def fast_lane(
    state: Annotated[
        str | None, typer.Argument(help="on or off; leave out to show the current state.")
    ] = None,
    config: ConfigPath = DEFAULT_CONFIG_PATH,
) -> None:
    """Skip the Reviewer agent: Agent Review tickets go straight to Human Review (ADR-0016).
    A running `codeit up` picks the change up on its next loop."""
    from codeit.orchestrator import settings as runtime_settings

    cfg = _load(config)
    if state is not None:
        if state not in ("on", "off"):
            typer.echo("Say on or off.", err=True)
            raise typer.Exit(code=2)
        runtime_settings.save(cfg.data_dir, by="cli", fast_lane=state == "on")
    s = runtime_settings.load(cfg.data_dir)
    since = f" (set by {s.changed_by}, {s.changed_at:%Y-%m-%d %H:%M})" if s.changed_at else ""
    typer.echo(f"fast lane: {'on' if s.fast_lane else 'off'}{since}")


@app.command("budget")
def budget(config: ConfigPath = DEFAULT_CONFIG_PATH) -> None:
    """Show backend budget state: Claude window and parking, OpenRouter spend today."""
    from codeit.orchestrator.budget import Budget

    cfg = _load(config)
    db.upgrade(cfg.db_path)
    status = Budget(
        cfg, db.make_engine(cfg.db_path), has_openrouter_key=Secrets().openrouter_key() is not None
    ).status()
    c = status["claude"]
    parked = f" until {c['parked_until']:%Y-%m-%d %H:%M}" if c["parked_until"] else ""
    typer.echo(f"Claude: {c['state']}{parked}; runs today {c['runs_today']}")
    typer.echo(f"  window {', '.join(c['window'])} ({'open' if c['in_window'] else 'closed'} now)")
    o = status["openrouter"]
    typer.echo(f"OpenRouter: key {'set' if o['key'] else 'MISSING'}")
    for role, (spent, cap) in o["spend"].items():
        typer.echo(f"  {role:<9} ${spent:.4f} of ${cap:.2f} today")
    used, cap = o["free_requests"]
    typer.echo(f"  free-model requests {used} of {cap} today")


@app.command("agents")
def agents(config: ConfigPath = DEFAULT_CONFIG_PATH) -> None:
    """Show agent instances, their state and the tickets they hold."""
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from codeit.db.models import AgentInstance, Run
    from codeit.orchestrator.leases import LeaseStore

    cfg = _load(config)
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    with Session(engine) as s:
        rows = list(s.scalars(select(AgentInstance).order_by(AgentInstance.name)))
        if not rows:
            typer.echo("No agent instances yet; they appear once `codeit up` has run.")
        for r in rows:
            run = s.get(Run, r.current_run_id) if r.current_run_id else None
            what = f" {run.ticket_key} (run {run.id})" if run else ""
            typer.echo(f"{r.name:<12} {r.state:<9}{what}  updated {r.updated_at:%H:%M:%S}")
    leases = LeaseStore(engine).all()
    if leases:
        typer.echo("Leases:")
        for lease in leases:
            typer.echo(
                f"  {lease.ticket_key:<12} {lease.instance:<12} run {lease.run_id} "
                f"heartbeat {lease.heartbeat_at:%H:%M:%S}"
            )


@dashboard_app.command("build")
def dashboard_build(
    path: Annotated[
        Path, typer.Option("--path", help="The dashboard project.", file_okay=False)
    ] = Path("dashboard"),
) -> None:
    """Install the dashboard's packages and export it to dashboard/out (needs Node 24)."""
    import shutil
    import subprocess

    npm = shutil.which("npm")
    if npm is None:
        typer.echo("npm not found; install Node 24 (for example with nvm).", err=True)
        raise typer.Exit(code=1)
    for args in (["ci", "--no-audit", "--no-fund"], ["run", "build"]):
        code = subprocess.call([npm, *args], cwd=path)  # noqa: S603
        if code != 0:
            raise typer.Exit(code=code)
    typer.echo(f"Built {path / 'out'}; `codeit up` serves it at the API address.")


def _eval_context(suite: str, keep: bool = False) -> EvalContext:
    from codeit.evals.runner import EvalContext
    from codeit.evals.suite import load_suite

    cfg = _load(DEFAULT_CONFIG_PATH)
    return EvalContext(cfg, Secrets(), load_suite(suite), echo=typer.echo, keep=keep)


TasksOpt = Annotated[
    str | None, typer.Option("--tasks", help="Comma-separated task ids, e.g. T001,T005.")
]
SuiteOpt = Annotated[str, typer.Option("--suite", help="Suite under evals/suites/.")]
KeepOpt = Annotated[bool, typer.Option("--keep", help="Keep the workspaces for debugging.")]


@eval_app.command("verify")
def eval_verify(
    suite: SuiteOpt = "golden",
    tasks: TasksOpt = None,
    update: Annotated[
        bool, typer.Option("--update", help="Write each task's hidden_total.")
    ] = False,
    keep: KeepOpt = False,
) -> None:
    """Check the suite: base commits fail the hidden tests, reference patches pass them."""
    from codeit.evals.runner import verify

    ctx = _eval_context(suite, keep)
    chosen = ctx.suite.select(tasks.split(",") if tasks else None)
    results = asyncio.run(verify(ctx, chosen, update=update))
    bad = [r.task.id for r in results if not r.ok]
    typer.echo(f"{len(results) - len(bad)} of {len(results)} tasks OK")
    if bad:
        typer.echo(f"Check: {', '.join(bad)}", err=True)
        raise typer.Exit(code=1)


@eval_app.command("run")
def eval_run(
    suite: SuiteOpt = "golden",
    config_name: Annotated[str, typer.Option("--config", help="evals/configs/<name>.yaml")] = (
        "default"
    ),
    repeats: Annotated[int, typer.Option("--repeats", min=1, max=10)] = 1,
    tasks: TasksOpt = None,
    review: Annotated[
        bool, typer.Option("--review", help="Also run the Reviewer on each result.")
    ] = False,
    any_time: Annotated[bool, typer.Option("--any-time", help="Ignore claude.run_window.")] = False,
    keep: KeepOpt = False,
) -> None:
    """Run the Coder on the suite's tasks and score it with the hidden tests (PRD 17.2)."""
    from codeit.evals.report import run_report
    from codeit.evals.runner import EvalStopped, latest_runs, results_of, run_coder_eval
    from codeit.evals.suite import load_config as load_eval_config

    ctx = _eval_context(suite, keep)
    try:
        config = load_eval_config(config_name)
        chosen = ctx.suite.select(tasks.split(",") if tasks else None)
        eval_id = asyncio.run(
            run_coder_eval(ctx, config, chosen, repeats=repeats, any_time=any_time, review=review)
        )
    except (ValueError, EvalStopped) as e:
        typer.echo(f"Cannot run the eval: {e}", err=True)
        raise typer.Exit(code=1) from e
    run = next(r for r in latest_runs(ctx.engine) if r.id == eval_id)
    typer.echo(run_report(run, results_of(ctx.engine, eval_id)))


@eval_app.command("review")
def eval_review(suite: SuiteOpt = "golden", tasks: TasksOpt = None, keep: KeepOpt = False) -> None:
    """Run the Reviewer on each task's reference patch and its seeded bugs (PRD 17.4)."""
    from codeit.evals.report import run_report
    from codeit.evals.runner import latest_runs, results_of, run_review_eval

    ctx = _eval_context(suite, keep)
    chosen = ctx.suite.select(tasks.split(",") if tasks else None)
    eval_id = asyncio.run(run_review_eval(ctx, chosen))
    run = next(r for r in latest_runs(ctx.engine) if r.id == eval_id)
    typer.echo(run_report(run, results_of(ctx.engine, eval_id)))


@eval_app.command("report")
def eval_report(
    run_id: Annotated[
        str | None, typer.Argument(help="An eval run id; default the latest.")
    ] = None,
    compare: Annotated[
        str | None, typer.Option("--compare", help="Config names: compare their latest runs.")
    ] = None,
) -> None:
    """Print an eval run's results, or compare configs (PRD 17.6)."""
    from codeit.evals import report
    from codeit.evals.runner import latest_runs, results_of

    cfg = _load(DEFAULT_CONFIG_PATH)
    db.upgrade(cfg.db_path)
    engine = db.make_engine(cfg.db_path)
    if compare:
        try:
            runs = report.latest_by_config(engine, [c.strip() for c in compare.split(",")])
        except ValueError as e:
            typer.echo(str(e), err=True)
            raise typer.Exit(code=1) from e
        text, path = report.compare(runs, cfg.data_dir / "evals" / "reports")
        typer.echo(text)
        typer.echo(f"\nsaved {path}")
        return
    runs = latest_runs(engine)
    run = next((r for r in runs if r.id == run_id), None) if run_id else (runs[0] if runs else None)
    if run is None:
        typer.echo("No eval runs yet." if not run_id else f"No eval run {run_id}.", err=True)
        raise typer.Exit(code=1)
    typer.echo(report.run_report(run, results_of(engine, run.id)))
