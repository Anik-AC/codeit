"""`codeit eval verify | run | review` (PRD 17.2, 17.4).

- verify: for each task, the base commit fails at least one hidden test and the reference
  patch passes them all. `--update` writes each task's `hidden_total`.
- run: the Coder on each task, `repeats` times, from a fresh workspace at the base commit,
  through the production code path minus Jira (the ticket comes from `ticket.md`; the agent
  commits locally). Optionally the Reviewer on the result; then the hidden tests.
- review: the Reviewer on each task's clean reference patch and its seeded-bug mutants.

Coder evals respect the Claude run window (unless `any_time`), parking, and the concurrency
limit: they hold a coder lease for their whole duration, so the orchestrator counts them.
Results go to `eval_runs` and `eval_results`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session
from ulid import ULID

from codeit import db
from codeit.agents.coder_run import run_coder_in
from codeit.agents.reviewer.checks import Runner, container_runner
from codeit.agents.reviewer.run import judge, run_phase1, workspace_diff
from codeit.backends.base import ChatBackend
from codeit.backends.registry import chat_route
from codeit.config import Config, Secrets
from codeit.db.models import EvalResult, EvalRun
from codeit.evals.hidden import HiddenResult, score
from codeit.evals.metrics import CoderRow, ReviewRow, coder_summary, review_summary
from codeit.evals.suite import EvalConfig, Suite, Task, steering_sha
from codeit.evals.workspace import Workspaces
from codeit.git import Git
from codeit.model_env import effective_models
from codeit.orchestrator.budget import CLAUDE_ROLES, Budget
from codeit.orchestrator.leases import LeaseStore
from codeit.sandbox.containers import ContainerSpec, Sandbox

Echo = Callable[[str], None]
ROLE = "eval"
LEASE_TTL = timedelta(minutes=120)
WAIT_FOR_CLAUDE_S = 30.0


class EvalStopped(Exception):
    """The eval cannot go on now (outside the run window, Claude parked)."""


@dataclass
class EvalContext:
    cfg: Config
    secrets: Secrets
    suite: Suite
    sandbox: Sandbox = field(default_factory=Sandbox)
    echo: Echo = print
    keep: bool = False

    def __post_init__(self) -> None:
        db.upgrade(self.cfg.db_path)
        self.engine: Engine = db.make_engine(self.cfg.db_path)
        token = self.secrets.github_token_readonly or self.secrets.github_token_agent
        self.git = Git(token.get_secret_value() if token else None)
        self.workspaces = Workspaces(self.cfg, self.suite.repo, self.git)


@contextlib.asynccontextmanager
async def container(ctx: EvalContext, run_id: str, workspace: Path) -> AsyncIterator[Runner]:
    """A worker container on `workspace` with no credentials, for installs and tests."""
    spec = ContainerSpec.for_role(
        ctx.cfg,
        run_id=run_id,
        role=ROLE,
        workspace=workspace,
        run_dir=ctx.cfg.data_dir / "runs" / run_id,
        env={"CI": "1"},
    )
    handle = ctx.sandbox.start(spec)
    try:
        yield container_runner(ctx.sandbox, str(handle.id))
    finally:
        ctx.sandbox.stop(handle, ctx.cfg.data_dir / "logs" / "containers" / f"{run_id}.log")


# verify ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Verified:
    task: Task
    base: HiddenResult
    reference: HiddenResult

    @property
    def ok(self) -> bool:
        return self.reference.all_passed and not self.base.all_passed


async def verify(
    ctx: EvalContext, tasks: Sequence[Task], *, update: bool = False
) -> list[Verified]:
    out = []
    for task in tasks:
        run_id = str(ULID())
        prepared = await ctx.workspaces.prepare(
            f"verify-{task.id}", ctx.suite.base_commit, f"verify-{task.id.lower()}"
        )
        try:
            async with container(ctx, run_id, prepared.path) as run:
                base = await score(
                    ctx.suite, task.model_copy(update={"hidden_total": None}), prepared.path, run
                )
                await ctx.workspaces.apply(
                    prepared.path, task.reference_patch, f"{task.id}: reference"
                )
                reference = await score(
                    ctx.suite, task.model_copy(update={"hidden_total": None}), prepared.path, run,
                    install=False,
                )  # fmt: skip
        finally:
            if not ctx.keep:
                ctx.workspaces.remove(prepared.path)
        result = Verified(task, base, reference)
        ctx.echo(
            f"{task.id}: {'OK' if result.ok else 'PROBLEM'}  base {base.passed}/{base.total}, "
            f"reference {reference.passed}/{reference.total}"
            + (
                f"  failing: {reference.failed + reference.errors}"
                if not reference.all_passed
                else ""
            )
        )
        if update and result.ok and task.hidden_total != reference.total:
            _write_hidden_total(task, reference.total)
        out.append(result)
    return out


def _write_hidden_total(task: Task, total: int) -> None:
    path = task.dir / "task.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["hidden_total"] = total
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# eval_runs rows ---------------------------------------------------------------------------


def _start_run(
    engine: Engine, run_id: str, suite: str, config: str, sha: str | None, model: str | None
) -> None:
    with Session(engine) as s, s.begin():
        s.add(
            EvalRun(
                id=run_id,
                suite=suite,
                config_name=config,
                steering_sha=sha,
                model=model,
                started_at=datetime.now(UTC),
            )
        )


def _add_result(engine: Engine, run_id: str, **values: Any) -> None:
    with Session(engine) as s, s.begin():
        s.add(EvalResult(eval_run_id=run_id, **values))


def _finish_run(engine: Engine, run_id: str, summary: dict[str, Any]) -> None:
    with Session(engine) as s, s.begin():
        run = s.get(EvalRun, run_id)
        assert run is not None
        run.ended_at = datetime.now(UTC)
        run.summary_json = summary


def _set_sha(engine: Engine, run_id: str, sha: str) -> None:
    with Session(engine) as s, s.begin():
        run = s.get(EvalRun, run_id)
        if run is not None and run.steering_sha is None:
            run.steering_sha = sha


# the Coder eval ---------------------------------------------------------------------------


def _other_claude_runs(leases: LeaseStore, own_key: str) -> int:
    now = datetime.now(UTC)
    count = 0
    for lease in leases.all():
        expires = (
            lease.expires_at if lease.expires_at.tzinfo else lease.expires_at.replace(tzinfo=UTC)
        )
        if lease.role in CLAUDE_ROLES and lease.ticket_key != own_key and expires > now:
            count += 1
    return count


async def _wait_for_claude(
    ctx: EvalContext, budget: Budget, leases: LeaseStore, own_key: str
) -> None:
    announced = False
    while True:
        decision = budget.can_run("coder", claude_running=_other_claude_runs(leases, own_key))
        if decision.ok:
            return
        if "concurrency" not in decision.reason:
            raise EvalStopped(decision.reason)
        if not announced:
            ctx.echo(f"waiting: {decision.reason}")
            announced = True
        await asyncio.sleep(WAIT_FOR_CLAUDE_S)


async def _commit_leftovers(ctx: EvalContext, path: Path, task: Task) -> None:
    """Score what the agent produced, committed or not."""
    if await ctx.git.run("status", "--porcelain", cwd=path):
        await ctx.git.run("add", "-A", cwd=path)
        await ctx.git.run(
            "commit", "--quiet", "--no-verify", "-m", f"{task.id}: uncommitted work", cwd=path
        )


async def run_coder_eval(
    ctx: EvalContext,
    config: EvalConfig,
    tasks: Sequence[Task],
    *,
    repeats: int,
    any_time: bool = False,
    review: bool = False,
    reviewer_backends: Sequence[ChatBackend] | None = None,
) -> str:
    if config.backend != "claude_code":
        raise ValueError(f"backend {config.backend!r} is not supported yet (M13 adds opencode)")
    if config.model:
        os.environ["CLAUDE_MODEL"] = config.model
    eval_id = str(ULID())
    lease_key = f"EVAL-{eval_id[-10:]}"
    leases = LeaseStore(ctx.engine)
    budget = Budget(
        ctx.cfg, ctx.engine, has_openrouter_key=ctx.secrets.openrouter_key() is not None,
        any_time=any_time,
    )  # fmt: skip
    await _wait_for_claude(ctx, budget, leases, lease_key)
    if not leases.acquire(lease_key, "coder", "eval", eval_id, LEASE_TTL):
        raise EvalStopped("could not take the eval lease")
    _start_run(ctx.engine, eval_id, ctx.suite.name, config.name, None, config.model)
    ctx.echo(f"eval {eval_id}: {len(tasks)} task(s) x {repeats} on {ctx.suite.name}/{config.name}")
    rows: list[CoderRow] = []
    stopped: str | None = None
    try:
        async with leases.keep_alive(lease_key, eval_id, LEASE_TTL):
            for task in tasks:
                for repeat in range(repeats):
                    await _wait_for_claude(ctx, budget, leases, lease_key)
                    row = await _one_coder_run(
                        ctx,
                        eval_id,
                        task,
                        repeat,
                        review=review,
                        reviewer_backends=reviewer_backends,
                    )
                    rows.append(row)
    except EvalStopped as e:
        stopped = str(e)
        ctx.echo(f"eval stopped early: {stopped}")
    finally:
        leases.release(lease_key, eval_id)
        summary = coder_summary(rows, repeats)
        if stopped:
            summary["stopped"] = stopped
        _finish_run(ctx.engine, eval_id, summary)
    return eval_id


async def _one_coder_run(
    ctx: EvalContext,
    eval_id: str,
    task: Task,
    repeat: int,
    *,
    review: bool,
    reviewer_backends: Sequence[ChatBackend] | None,
) -> CoderRow:
    name = f"{eval_id[-8:]}-{task.id}-{repeat}"
    prepared = await ctx.workspaces.prepare(name, ctx.suite.base_commit, f"{task.id}-eval-{repeat}")
    _set_sha(ctx.engine, eval_id, _steering_sha(prepared.path))
    started = time.monotonic()
    notes: dict[str, Any] = {}
    run_id = str(ULID())
    try:
        coder = await run_coder_in(
            ctx.cfg, ctx.secrets, prepared, task.id, task.ticket_md,
            instance="eval", run_id=run_id, sandbox=ctx.sandbox, echo=ctx.echo,
        )  # fmt: skip
        agent = coder.agent
        notes["coder"] = coder.outcome.status
        notes["run_id"] = run_id
        if agent is not None and agent.status == "usage_limited":
            raise EvalStopped("Claude hit its usage limit")
        await _commit_leftovers(ctx, prepared.path, task)
        diff_lines = await ctx.workspaces.diff_lines(prepared.path, ctx.suite.base_commit)
        verdict = None
        if review:
            verdict = await _review(ctx, prepared.path, task, reviewer_backends, notes)
        async with container(ctx, str(ULID()), prepared.path) as run:
            hidden = await score(ctx.suite, task, prepared.path, run)
    finally:
        if not ctx.keep:
            ctx.workspaces.remove(prepared.path)
    duration = round(time.monotonic() - started, 1)
    if hidden.failed:
        notes["hidden_failed"] = hidden.failed[:20]
    if hidden.errors:
        notes["hidden_errors"] = hidden.errors[:5]
    row = CoderRow(
        task_id=task.id,
        repeat_idx=repeat,
        passed=hidden.all_passed,
        hidden_pass_ratio=round(hidden.ratio, 4),
        turns=agent.turns if agent else None,
        cost_usd=agent.cost_usd if agent else None,
        duration_s=duration,
        diff_lines=diff_lines,
        reviewer_verdict=verdict,
    )
    _add_result(
        ctx.engine, eval_id,
        task_id=task.id, repeat_idx=repeat, passed=row.passed,
        hidden_pass_ratio=row.hidden_pass_ratio, turns=row.turns, cost_usd=row.cost_usd,
        duration_s=duration, diff_lines=diff_lines, reviewer_verdict=verdict,
        notes=json.dumps(notes),
    )  # fmt: skip
    ctx.echo(
        f"{task.id} #{repeat + 1}: {'PASS' if row.passed else 'FAIL'} "
        f"hidden {hidden.passed}/{hidden.total}, coder {notes['coder']}, {duration:.0f}s"
        + (f", review {verdict}" if review else "")
    )
    return row


def _steering_sha(workspace: Path) -> str:
    prompts = Path(__file__).resolve().parents[3] / "prompts" / "coder"
    files = [*prompts.glob("*.md"), workspace / "CLAUDE.md", *(workspace / ".claude").rglob("*")]
    return steering_sha(files)


# the Reviewer eval ------------------------------------------------------------------------


async def _review(
    ctx: EvalContext,
    path: Path,
    task: Task,
    backends: Sequence[ChatBackend] | None,
    notes: dict[str, Any],
) -> str | None:
    base = ctx.suite.base_commit
    head = await ctx.git.run("rev-parse", "HEAD", cwd=path)
    changed, diff = await workspace_diff(ctx.git, path, base)
    review_id = str(ULID())
    phase1 = await run_phase1(
        ctx.cfg, ctx.secrets, ctx.sandbox,
        run_id=review_id, path=path, head=head, base=base, changed=changed,
    )  # fmt: skip
    verdict, call = await judge(
        backends or chat_route(ctx.cfg, ctx.secrets, "reviewer"),
        ctx.engine,
        run_id=review_id,
        key=task.id,
        ticket_md=task.ticket_md,
        diff=diff,
        phase1=phase1,
        echo=ctx.echo,
    )
    notes["checks"] = {c.name: c.status for c in phase1.checks}
    notes["review_cost_usd"] = call.cost_usd if call else None
    if verdict is not None:
        notes["findings"] = [
            f"{f.severity}: {f.issue}"
            for f in verdict.findings
            if f.severity in ("critical", "major")
        ][:10]
    return verdict.verdict if verdict else None


async def run_review_eval(
    ctx: EvalContext,
    tasks: Sequence[Task],
    *,
    backends: Sequence[ChatBackend] | None = None,
) -> str:
    """The seeded-bug suite: each task's clean reference and its mutants."""
    eval_id = str(ULID())
    models = effective_models(ctx.cfg).get(ctx.cfg.routing["reviewer"].primary)
    model = models[0] if isinstance(models, list) and models else None
    _start_run(ctx.engine, eval_id, f"{ctx.suite.name}-review", "reviewer", None, model)
    rows: list[ReviewRow] = []
    try:
        for task in tasks:
            variants = [("clean", "clean", task.reference_patch)] + [
                (m.id, m.kind, m.patch) for m in task.mutants()
            ]
            for variant, kind, patch in variants:
                rows.append(await _one_review(ctx, eval_id, task, variant, kind, patch, backends))
    finally:
        _finish_run(ctx.engine, eval_id, review_summary(rows))
    return eval_id


async def _one_review(
    ctx: EvalContext,
    eval_id: str,
    task: Task,
    variant: str,
    kind: str,
    patch: Path,
    backends: Sequence[ChatBackend] | None,
) -> ReviewRow:
    prepared = await ctx.workspaces.prepare(
        f"{eval_id[-8:]}-{task.id}-{variant}", ctx.suite.base_commit, f"{task.id}-{variant}".lower()
    )
    started = time.monotonic()
    notes: dict[str, Any] = {"kind": kind}
    try:
        await ctx.workspaces.apply(prepared.path, patch, f"{task.id}: {variant}")
        verdict = await _review(ctx, prepared.path, task, backends, notes)
    finally:
        if not ctx.keep:
            ctx.workspaces.remove(prepared.path)
    row = ReviewRow(task.id, variant, kind, verdict, notes.get("review_cost_usd"))
    _add_result(
        ctx.engine, eval_id,
        task_id=f"{task.id}/{variant}", repeat_idx=0, passed=row.correct,
        duration_s=round(time.monotonic() - started, 1), cost_usd=row.cost_usd,
        reviewer_verdict=verdict, notes=json.dumps(notes),
    )  # fmt: skip
    expected = "fail_critical" if row.expected_fail else "not fail_critical"
    ctx.echo(
        f"{task.id}/{variant} [{kind}]: {verdict} (expected {expected}) "
        f"{'OK' if row.correct else 'MISSED' if row.expected_fail else 'FALSE FAIL'}"
    )
    return row


# reading results --------------------------------------------------------------------------


def latest_runs(engine: Engine, limit: int = 50) -> list[EvalRun]:
    with Session(engine) as s:
        return list(s.scalars(select(EvalRun).order_by(EvalRun.started_at.desc()).limit(limit)))


def results_of(engine: Engine, eval_id: str) -> list[EvalResult]:
    with Session(engine) as s:
        return list(
            s.scalars(
                select(EvalResult)
                .where(EvalResult.eval_run_id == eval_id)
                .order_by(EvalResult.task_id, EvalResult.repeat_idx)
            )
        )
