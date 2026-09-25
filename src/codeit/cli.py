"""Typer entrypoint for the `codeit` command (PRD 12.6).

Commands owned by later milestones are registered now as stubs so the CLI shape is stable;
each stub names the milestone that implements it.
"""

from __future__ import annotations

import asyncio
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

app.add_typer(config_app, name="config")
app.add_typer(db_app, name="db")
app.add_typer(jira_app, name="jira")
app.add_typer(sandbox_app, name="sandbox")
app.add_typer(eval_app, name="eval")

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
    cfg = _load(config)
    typer.echo(
        f"OK: {config} (project {cfg.project.jira_project_key}, "
        f"target repo {cfg.project.target_repo.name})"
    )


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


# later milestones ----------------------------------------------------------------------------


@app.command("plan")
def plan(
    file: Path,
    epic: Annotated[str | None, typer.Option("--epic")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    """Turn a plan markdown file into Jira tickets."""
    _not_implemented("M3")


@app.command("run")
def run(role: str, key: Annotated[str | None, typer.Argument()] = None) -> None:
    """Run one agent once, optionally on a ticket KEY."""
    _not_implemented("M5 and later")


@sandbox_app.command("build")
def sandbox_build() -> None:
    """Build the worker image."""
    _not_implemented("M4")


@sandbox_app.command("gc")
def sandbox_gc() -> None:
    """Remove clones for terminal or stale tickets."""
    _not_implemented("M4")


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
