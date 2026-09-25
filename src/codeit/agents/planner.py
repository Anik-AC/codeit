"""Planner agent (PRD 11.1): a plan in markdown becomes one Epic and its Stories in Jira.

Three steps, kept apart so a dry run shows exactly what would be created:
1. `draft_plan`: ask the model (primary backend, then fallback) for a `PlanDraft`. An
   answer that fails validation is retried once, with the errors attached.
2. `plan_issues`: turn the draft into Jira payloads. Pure; dry runs and real runs share it.
3. `apply_plan`: create the Epic, the Stories (bulk) and the "blocks" links. On failure,
   delete whatever this run created, so Jira never holds half a plan.

Every plan is saved to `data/plans/{run_id}.json`; `codeit plan <that file>` applies a
saved dry run later without calling the model again.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ValidationError

from codeit.agents.planner_schema import PlanDraft, plan_json_schema
from codeit.backends.base import (
    BackendOutputError,
    BackendUnavailable,
    ChatBackend,
    ChatMessage,
    ChatRequest,
)
from codeit.jira_client import JiraClient, JiraError, JiraIds
from codeit.jira_client.issues import (
    IssueSpec,
    JiraBulkError,
    bulk_create,
    create_issue,
    delete_issue,
    link_issues,
)
from codeit.jira_client.search import search
from codeit.log import get_logger
from codeit.prompts import prompt_hash, render

log = get_logger(__name__)

ROLE: Final = "planner"
DRAFT_LABEL = "agent-draft"
SPLIT_LABEL = "split-me"
TREE_DEPTH = 3
TREE_LIMIT = 400
TREE_SKIP = frozenset(
    {".git", "node_modules", "dist", "build", ".next", ".venv", "__pycache__", "coverage"}
)


class PlannerError(Exception):
    pass


# inputs -----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannerInputs:
    plan_md: str
    plan_file: str
    claude_md: str | None = None
    repo_tree: str | None = None
    open_tickets: list[str] = field(default_factory=list)


def repo_tree(root: Path, depth: int = TREE_DEPTH, limit: int = TREE_LIMIT) -> str:
    """Indented file tree, skipping dependency and build folders."""
    lines: list[str] = []

    def walk(path: Path, level: int) -> None:
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if entry.name in TREE_SKIP or len(lines) >= limit:
                continue
            lines.append("  " * level + entry.name + ("/" if entry.is_dir() else ""))
            if entry.is_dir() and level + 1 < depth:
                walk(entry, level + 1)

    walk(root, 0)
    if len(lines) >= limit:
        lines.append(f"... (truncated at {limit} entries)")
    return "\n".join(lines)


async def open_ticket_lines(client: JiraClient, project_key: str, limit: int = 50) -> list[str]:
    jql = f"project = {project_key} AND statusCategory != Done ORDER BY created DESC"
    return [
        f"- {i['key']} [{(i['fields'].get('status') or {}).get('name', '?')}] "
        f"{i['fields'].get('summary', '')}"
        async for i in search(client, jql, ["summary", "status"], limit)
    ]


def _local_context(plan_path: Path, repo_path: Path | None) -> tuple[str, str | None, str | None]:
    claude_md = tree = None
    if repo_path is not None and repo_path.is_dir():
        claude_file = repo_path / "CLAUDE.md"
        claude_md = claude_file.read_text(encoding="utf-8") if claude_file.is_file() else None
        tree = repo_tree(repo_path)
    return plan_path.read_text(encoding="utf-8"), claude_md, tree


async def gather_inputs(
    plan_path: Path, repo_path: Path | None, client: JiraClient, project_key: str
) -> PlannerInputs:
    plan_md, claude_md, tree = await asyncio.to_thread(_local_context, plan_path, repo_path)
    return PlannerInputs(
        plan_md=plan_md,
        plan_file=plan_path.as_posix(),
        claude_md=claude_md,
        repo_tree=tree,
        open_tickets=await open_ticket_lines(client, project_key),
    )


# 1. draft ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Draft:
    plan: PlanDraft
    backend: str
    model: str | None
    attempts: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    cost_estimated: bool
    prompt_hash: str


def _validation_message(err: ValidationError) -> str:
    return "\n".join(
        f"- {'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in err.errors()
    )


def _add(a: int | float | None, b: int | float | None) -> Any:
    return None if a is None and b is None else (a or 0) + (b or 0)


async def draft_plan(inputs: PlannerInputs, backends: Sequence[ChatBackend]) -> Draft:
    """Ask each backend in turn until one is available; validate, retrying once."""
    system = render("planner/system.md")
    user = render(
        "planner/user.md",
        plan_md=inputs.plan_md,
        claude_md=inputs.claude_md,
        repo_tree=inputs.repo_tree,
        open_tickets=inputs.open_tickets,
    )
    reasons: list[str] = []
    for backend in backends:
        try:
            return await _draft_with(backend, system, user)
        except BackendUnavailable as e:
            log.warning("planner.backend_unavailable", backend=backend.name, reason=str(e))
            reasons.append(str(e))
    raise PlannerError("no backend could draft the plan: " + "; ".join(reasons))


async def _draft_with(backend: ChatBackend, system: str, user: str) -> Draft:
    messages = [ChatMessage("system", system), ChatMessage("user", user)]
    schema = plan_json_schema()
    tokens_in = tokens_out = cost = None
    for attempt in (1, 2):
        req = ChatRequest(messages=list(messages), json_schema=schema, schema_name="plan")
        try:
            result = await backend.complete(req)
        except BackendOutputError as e:
            answer, error = e.text, f"- the answer was not valid JSON: {e}"
        else:
            tokens_in = _add(tokens_in, result.input_tokens)
            tokens_out = _add(tokens_out, result.output_tokens)
            cost = _add(cost, result.cost_usd)
            try:
                plan = PlanDraft.model_validate(result.data)
            except ValidationError as err:
                answer, error = result.text, _validation_message(err)
            else:
                return Draft(
                    plan=plan,
                    backend=backend.name,
                    model=result.model,
                    attempts=attempt,
                    input_tokens=tokens_in,
                    output_tokens=tokens_out,
                    cost_usd=cost,
                    cost_estimated=result.cost_estimated,
                    prompt_hash=prompt_hash(ROLE),
                )
        log.warning("planner.invalid_output", backend=backend.name, attempt=attempt, error=error)
        messages += [
            ChatMessage("assistant", answer),
            ChatMessage("user", render("planner/retry.md", error=error)),
        ]
    raise PlannerError(f"{backend.name} returned an invalid plan twice:\n{error}")


# 2. plan issues ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedStory:
    ref: str
    spec: IssueSpec
    depends_on: list[str]


@dataclass(frozen=True)
class PlannedIssues:
    epic: IssueSpec | None  # None when stories go under an existing Epic
    epic_key: str | None
    stories: list[PlannedStory]


def plan_issues(
    draft: PlanDraft,
    ids: JiraIds,
    project_key: str,
    *,
    run_id: str,
    plan_file: str,
    epic: str | None = None,
) -> PlannedIssues:
    """Jira payloads for a draft. `epic` is an existing Epic key, or a name for the new one."""
    existing = epic if epic and re.fullmatch(rf"{re.escape(project_key)}-\d+", epic) else None
    epic_spec = None
    if existing is None:
        epic_spec = IssueSpec(
            project_key=project_key,
            issue_type_id=ids.issue_type_id("Epic"),
            summary=epic or draft.epic.summary,
            description_md=render(
                "planner/epic.md", epic=draft.epic, plan_file=plan_file, run_id=run_id
            ),
            labels=[DRAFT_LABEL],
        )
    stories = [
        PlannedStory(
            ref=s.ref,
            spec=IssueSpec(
                project_key=project_key,
                issue_type_id=ids.issue_type_id("Story"),
                summary=s.summary,
                description_md=render("planner/story.md", story=s, run_id=run_id),
                labels=[DRAFT_LABEL, SPLIT_LABEL] if s.needs_split else [DRAFT_LABEL],
                parent_key=existing,
            ),
            depends_on=list(s.depends_on),
        )
        for s in draft.stories
    ]
    return PlannedIssues(epic=epic_spec, epic_key=existing, stories=stories)


# 3. apply ---------------------------------------------------------------------------------------


class CreatedPlan(BaseModel):
    epic_key: str
    stories: dict[str, str]  # ref -> key
    links: list[tuple[str, str]]  # (blocker, blocked)


async def apply_plan(client: JiraClient, planned: PlannedIssues) -> CreatedPlan:
    created: list[str] = []
    try:
        epic_key = planned.epic_key
        if epic_key is None:
            assert planned.epic is not None
            epic_key = await create_issue(client, planned.epic)
            created.append(epic_key)
        specs = [s.spec.model_copy(update={"parent_key": epic_key}) for s in planned.stories]
        try:
            keys = await bulk_create(client, specs)
        except JiraBulkError as e:
            created += e.created
            raise
        created += keys
        by_ref = dict(zip((s.ref for s in planned.stories), keys, strict=True))
        links = [(by_ref[dep], by_ref[s.ref]) for s in planned.stories for dep in s.depends_on]
        for blocker, blocked in links:
            await link_issues(client, blocker=blocker, blocked=blocked)
    except JiraError:
        await _rollback(client, created)
        raise
    return CreatedPlan(epic_key=epic_key, stories=by_ref, links=links)


async def _rollback(client: JiraClient, keys: list[str]) -> None:
    for key in reversed(keys):
        try:
            await delete_issue(client, key)
            log.info("planner.rollback_deleted", key=key)
        except JiraError as e:
            log.error("planner.rollback_failed", key=key, error=str(e))


# saved plans ------------------------------------------------------------------------------------


class PlanRecord(BaseModel):
    """What `data/plans/{run_id}.json` holds: the draft, how it was made, and the result."""

    run_id: str
    created_at: datetime
    plan_file: str
    epic_arg: str | None
    backend: str
    model: str | None
    attempts: int
    prompt_hash: str
    cost_usd: float | None
    draft: PlanDraft
    created: CreatedPlan | None = None

    @classmethod
    def from_draft(
        cls, run_id: str, inputs: PlannerInputs, draft: Draft, epic: str | None
    ) -> PlanRecord:
        return cls(
            run_id=run_id,
            created_at=datetime.now(UTC),
            plan_file=inputs.plan_file,
            epic_arg=epic,
            backend=draft.backend,
            model=draft.model,
            attempts=draft.attempts,
            prompt_hash=draft.prompt_hash,
            cost_usd=draft.cost_usd,
            draft=draft.plan,
        )

    def save(self, plans_dir: Path) -> Path:
        plans_dir.mkdir(parents=True, exist_ok=True)
        path = plans_dir / f"{self.run_id}.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: Path) -> PlanRecord:
        return cls.model_validate_json(path.read_text(encoding="utf-8"))
