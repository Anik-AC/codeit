"""`codeit plan` end to end: draft, save, show, apply, and record the run (PRD 11.1, 13)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session
from ulid import ULID

from codeit import db
from codeit.agents.planner import (
    ROLE,
    CreatedPlan,
    PlannedIssues,
    PlannerError,
    PlanRecord,
    apply_plan,
    draft_plan,
    gather_inputs,
    plan_issues,
)
from codeit.backends.base import ChatBackend
from codeit.backends.registry import chat_route
from codeit.config import Config, Secrets
from codeit.db.models import Run
from codeit.jira_client import JiraClient, JiraIds

Echo = Callable[[str], None]


@dataclass(frozen=True)
class PlanOutcome:
    record: PlanRecord
    path: Path


def plans_dir(cfg: Config) -> Path:
    return cfg.data_dir / "plans"


def default_repo(cfg: Config) -> Path:
    return cfg.data_dir / "repos" / cfg.project.target_repo.name


def describe(record: PlanRecord, planned: PlannedIssues) -> list[str]:
    """A table of what the plan creates, printed for dry runs and real runs alike."""
    epic = planned.epic_key or f"new Epic: {planned.epic.summary if planned.epic else '?'}"
    lines = [f"Plan {record.run_id} ({record.backend}, {record.attempts} attempt(s))", epic, ""]
    header = f"{'REF':<5}{'PTS':>4}  {'RISK':<7}{'DEPENDS ON':<12}{'LABELS':<22}SUMMARY"
    lines.append(header)
    drafts = {s.ref: s for s in record.draft.stories}
    for story in planned.stories:
        d = drafts[story.ref]
        deps = ",".join(story.depends_on) or "-"
        lines.append(
            f"{story.ref:<5}{d.suggested_points:>4}  {d.risk:<7}{deps:<12}"
            f"{','.join(story.spec.labels):<22}{story.spec.summary}"
        )
    if record.created:
        lines += [
            "",
            f"Created {record.created.epic_key}: "
            + ", ".join(f"{ref}={key}" for ref, key in record.created.stories.items()),
        ]
    return lines


def _record_run(
    cfg: Config,
    *,
    run_id: str,
    started: datetime,
    status: str,
    record: PlanRecord | None = None,
    error: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    db.upgrade(cfg.db_path)
    result: dict[str, Any] = dict(extra or {})
    if record is not None:
        result["plan_path"] = str(plans_dir(cfg) / f"{record.run_id}.json")
        if record.created is not None:
            result["created"] = record.created.model_dump()
    with Session(db.make_engine(cfg.db_path)) as s, s.begin():
        s.merge(
            Run(
                id=run_id,
                role=ROLE,
                instance=f"{ROLE}-1",
                ticket_key=None,
                backend=record.backend if record else None,
                model=record.model if record else None,
                prompt_hash=record.prompt_hash if record else None,
                status=status,
                started_at=started,
                ended_at=datetime.now(UTC),
                turns=record.attempts if record else None,
                cost_usd=record.cost_usd if record else None,
                result_json=result or None,
                error=error,
            )
        )


async def run_planner(
    cfg: Config,
    secrets: Secrets,
    ids: JiraIds,
    plan_path: Path,
    *,
    dry_run: bool,
    epic: str | None = None,
    repo_path: Path | None = None,
    backends: Sequence[ChatBackend] | None = None,
    echo: Echo = print,
) -> PlanOutcome:
    run_id = str(ULID())
    started = datetime.now(UTC)
    project = cfg.project.jira_project_key
    record: PlanRecord | None = None
    try:
        async with JiraClient.from_secrets(secrets) as client:
            inputs = await gather_inputs(plan_path, repo_path or default_repo(cfg), client, project)
            draft = await draft_plan(inputs, backends or chat_route(cfg, secrets, ROLE))
            record = PlanRecord.from_draft(run_id, inputs, draft, epic)
            path = record.save(plans_dir(cfg))
            planned = plan_issues(
                draft.plan, ids, project, run_id=run_id, plan_file=inputs.plan_file, epic=epic
            )
            if not dry_run:
                record.created = await apply_plan(client, planned)
                record.save(plans_dir(cfg))
    except Exception as e:
        _record_run(
            cfg, run_id=run_id, started=started, status="failed", record=record, error=str(e)
        )
        raise
    _record_run(
        cfg,
        run_id=run_id,
        started=started,
        status="dry_run" if dry_run else "completed",
        record=record,
    )
    for line in describe(record, planned):
        echo(line)
    return PlanOutcome(record, path)


async def apply_saved(
    cfg: Config, secrets: Secrets, ids: JiraIds, path: Path, *, echo: Echo = print
) -> CreatedPlan:
    """Create the tickets of a saved dry run, exactly as it was shown."""
    record = PlanRecord.load(path)
    if record.created is not None:
        raise PlannerError(f"{path} was already applied as {record.created.epic_key}")
    planned = plan_issues(
        record.draft,
        ids,
        cfg.project.jira_project_key,
        run_id=record.run_id,
        plan_file=record.plan_file,
        epic=record.epic_arg,
    )
    started = datetime.now(UTC)
    async with JiraClient.from_secrets(secrets) as client:
        record.created = await apply_plan(client, planned)
    record.save(path.parent)
    _record_run(
        cfg,
        run_id=str(ULID()),
        started=started,
        status="completed",
        record=record,
        extra={"applied_from": str(path)},
    )
    for line in describe(record, planned):
        echo(line)
    return record.created
