"""Signals: feedback the Learning agent learns from (PRD 11.7, ADR-0019).

Human signals:
- Jira comments by people (not CodeIt's own), on tickets updated in the lookback window
- people's reviews and comments on the target repo's agent PRs

CodeIt signals:
- critical findings of Reviewer runs that failed a ticket
- failed eval results (Coder tasks that failed hidden tests, Reviewer misses and false
  fails), except the eval gate's own runs

Each signal has an `external_id`, so collecting again only adds what is new. A human marks
one as worth learning from even alone by writing `#learn` in it (or labelling the ticket
`learn`).
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from codeit.agents.coder import REVIEWER_MARK, is_own_comment
from codeit.db.models import EvalResult, EvalRun, Run, Signal
from codeit.github_client import GitHubClient
from codeit.github_client.prs import list_prs, pr_feedback
from codeit.jira_client import JiraClient, JiraIds
from codeit.jira_client.comments import list_comments
from codeit.jira_client.search import search_tickets

LEARN_TAG = re.compile(r"(?<![\w-])#learn\b", re.IGNORECASE)
LEARN_LABEL = "learn"
GATE_PREFIX = "gate-"  # eval gate runs are not signals
MAX_TEXT = 2000


@dataclass
class RawSignal:
    external_id: str
    source: str
    text: str
    human: bool = True
    ticket_key: str | None = None
    pr: int | None = None
    author: str | None = None
    url: str | None = None
    created_at: datetime | None = None
    learn: bool = False


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# Jira ---------------------------------------------------------------------------------------


async def jira_signals(
    jira: JiraClient, ids: JiraIds, project_key: str, base_url: str, since: datetime
) -> list[RawSignal]:
    days = max(1, (datetime.now(UTC) - since).days + 1)
    jql = f'project = {project_key} AND updated >= "-{days}d" ORDER BY updated DESC'
    out = []
    async for t in search_tickets(jira, jql, ids.fields):
        for c in await list_comments(jira, t.key):
            body = c.body_md.strip()
            if not body or is_own_comment(body) or REVIEWER_MARK in body:
                continue
            if _aware(c.created) < since:
                continue
            source = "jira_rejection" if t.status == "Rejected" else "jira_comment"
            out.append(
                RawSignal(
                    external_id=f"jira-comment:{c.id}",
                    source=source,
                    text=f"[{t.key}, {t.status}: {t.summary}] {body}"[:MAX_TEXT],
                    ticket_key=t.key,
                    author=c.author,
                    url=f"{base_url.rstrip('/')}/browse/{t.key}?focusedCommentId={c.id}",
                    created_at=c.created,
                    learn=bool(LEARN_TAG.search(body)) or LEARN_LABEL in t.labels,
                )
            )
    return out


# GitHub -------------------------------------------------------------------------------------


def _agent_pr_key(branch: str, project_key: str) -> str | None:
    m = re.match(rf"^({re.escape(project_key)}-\d+)-", branch)
    return m.group(1) if m else None


async def github_signals(
    gh: GitHubClient, slug: str, project_key: str, since: datetime
) -> list[RawSignal]:
    out = []
    for raw in await list_prs(gh, slug):
        if _aware(datetime.fromisoformat(raw["updated_at"])) < since:
            break  # sorted by update time, newest first
        key = _agent_pr_key(raw["head"]["ref"], project_key)
        if key is None:
            continue  # only PRs the Coder opened
        number = int(raw["number"])
        for c in await pr_feedback(gh, slug, number, since=since):
            body = c.body.strip()
            if not body or is_own_comment(body) or REVIEWER_MARK in body:
                continue
            where = f" on {c.path}:{c.line}" if c.path else ""
            out.append(
                RawSignal(
                    external_id=f"gh-{c.kind}:{c.id}",
                    source="pr_review" if c.kind != "comment" else "pr_comment",
                    text=f"[{key}, PR #{number}{where}] {body}"[:MAX_TEXT],
                    ticket_key=key,
                    pr=number,
                    author=c.author,
                    url=c.url,
                    created_at=c.created_at,
                    learn=bool(LEARN_TAG.search(body)),
                )
            )
    return out


# CodeIt's own records -----------------------------------------------------------------------


def reviewer_signals(engine: Engine, since: datetime) -> list[RawSignal]:
    """Critical findings of Reviewer runs that sent a ticket back. A failed check alone
    ("Check `unit` failed") says nothing to learn beyond the check, so it is left out."""
    out = []
    with Session(engine) as s:
        runs = s.scalars(
            select(Run).where(
                Run.role == "reviewer", Run.status == "fail_critical", Run.started_at >= since
            )
        ).all()
    for run in runs:
        verdict = (run.result_json or {}).get("verdict") or {}
        for i, f in enumerate(verdict.get("findings") or []):
            issue = str(f.get("issue") or "")
            if f.get("severity") != "critical" or issue.startswith("Check `"):
                continue
            suggestion = str(f.get("suggestion") or "")
            out.append(
                RawSignal(
                    external_id=f"review:{run.id}:{i}",
                    source="reviewer_finding",
                    text=f"[{run.ticket_key}] {issue} Suggestion: {suggestion}"[:MAX_TEXT],
                    human=False,
                    ticket_key=run.ticket_key,
                    created_at=run.started_at,
                )
            )
    return out


def latest_eval_runs(engine: Engine, since: datetime) -> list[EvalRun]:
    """The latest finished run of each suite since `since`, not counting gate runs. Older
    runs scored older prompts, whose failures may be fixed already."""
    with Session(engine) as s:
        runs = s.scalars(
            select(EvalRun)
            .where(EvalRun.started_at >= since, EvalRun.ended_at.is_not(None))
            .where(~EvalRun.config_name.startswith(GATE_PREFIX))
            .order_by(EvalRun.started_at.desc())
        ).all()
    latest: dict[str, EvalRun] = {}
    for run in runs:
        latest.setdefault(run.suite, run)
    return list(latest.values())


def eval_signals(engine: Engine, since: datetime) -> list[RawSignal]:
    out = []
    run_ids = [r.id for r in latest_eval_runs(engine, since)]
    with Session(engine) as s:
        rows = s.execute(
            select(EvalResult, EvalRun)
            .join(EvalRun, EvalRun.id == EvalResult.eval_run_id)
            .where(EvalRun.id.in_(run_ids), EvalResult.passed.is_(False))
        ).all()
    for result, run in rows:
        notes: dict[str, Any] = json.loads(result.notes or "{}")
        if run.suite.endswith("-review"):
            kind = notes.get("kind", "")
            if kind == "clean":
                text = (
                    f"Reviewer eval: the Reviewer failed a correct patch for {result.task_id}. "
                    f"Its findings: {notes.get('findings')}"
                )
            else:
                text = (
                    f"Reviewer eval: the Reviewer missed a planted {kind} bug in {result.task_id}."
                )
            target_text = text
        elif run.suite == "planner":
            target_text = f"Planner eval: plan {result.task_id} failed: {notes.get('error')}"
        else:
            failed = notes.get("hidden_failed") or []
            target_text = (
                f"Coder eval: task {result.task_id} failed hidden tests {failed[:5]} "
                f"(coder status {notes.get('coder')})."
            )
        out.append(
            RawSignal(
                external_id=f"eval:{run.id}:{result.task_id}:{result.repeat_idx}",
                source="eval_failure",
                text=target_text[:MAX_TEXT],
                human=False,
                created_at=run.started_at,
            )
        )
    return out


# storing ------------------------------------------------------------------------------------


def store(engine: Engine, raws: Sequence[RawSignal]) -> int:
    """Insert signals not seen before; returns how many were new."""
    if not raws:
        return 0
    ids = [r.external_id for r in raws]
    with Session(engine) as s, s.begin():
        seen = set(s.scalars(select(Signal.external_id).where(Signal.external_id.in_(ids))))
        new = [r for r in raws if r.external_id not in seen]
        for r in {r.external_id: r for r in new}.values():
            s.add(
                Signal(
                    external_id=r.external_id,
                    source=r.source,
                    ticket_key=r.ticket_key,
                    pr=r.pr,
                    author=r.author,
                    text=r.text,
                    human=r.human,
                    learn=r.learn,
                    url=r.url,
                    created_at=r.created_at,
                    status="new",
                )
            )
        return len({r.external_id for r in new})


def count_new_human(engine: Engine) -> int:
    with Session(engine) as s:
        return int(
            s.scalar(select(func.count()).where(Signal.status == "new", Signal.human.is_(True)))
            or 0
        )


def last_learning_run(engine: Engine) -> datetime | None:
    with Session(engine) as s:
        started = s.scalar(select(func.max(Run.started_at)).where(Run.role == "learning"))
    return _aware(started) if started else None


def lookback_start(engine: Engine, days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


# fixtures -----------------------------------------------------------------------------------


def load_fixture(path: Path) -> list[RawSignal]:
    """Synthetic signals from YAML (a list of {source, text, ticket_key?, learn?, human?}),
    for the acceptance test and for trying prompt changes."""
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [
        RawSignal(
            external_id=f"fixture:{path.stem}:{i}",
            source=str(item.get("source", "jira_comment")),
            text=str(item["text"]),
            human=bool(item.get("human", True)),
            ticket_key=item.get("ticket_key"),
            url=item.get("url"),
            learn=bool(item.get("learn", False)),
            created_at=datetime.now(UTC),
        )
        for i, item in enumerate(items, 1)
    ]
