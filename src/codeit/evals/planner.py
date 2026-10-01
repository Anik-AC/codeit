"""Planner evals (PRD 17.5, ADR-0019): `codeit eval planner`.

The Planner drafts each sample plan in `evals/suites/planner/` without Jira, against a
fixed snapshot of the target's CLAUDE.md and file tree. Metrics:

- schema_valid: the share of drafts that produced a valid plan (at most one retry)
- first_try_valid: the share valid without the retry
- invest: the mean INVEST score of the stories, 0 to 1, from an LLM judge
  (`routing.ops`, rubric `evals/rubrics/invest.md`)
- coverage: how much of each plan the stories cover, 0 to 1

The owner's approval rate and the edit distance of approved tickets need real use and are
not part of the suite (ADR-0019).
"""

from __future__ import annotations

import json
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Engine
from ulid import ULID

from codeit.agents.planner import Draft, PlannerError, PlannerInputs, draft_plan
from codeit.backends.base import BackendUnavailable, ChatBackend, ChatMessage
from codeit.backends.structured import structured
from codeit.evals.runner import _add_result, _finish_run, _start_run
from codeit.evals.suite import SUITES_DIR
from codeit.prompts import REPO_ROOT, prompt_hash

RUBRIC = REPO_ROOT / "evals" / "rubrics" / "invest.md"
CRITERIA = ("independent", "negotiable", "valuable", "estimable", "small", "testable")
Score = Annotated[int, Field(ge=0, le=2)]
Echo = Callable[[str], None]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class StoryScore(_Strict):
    ref: str
    independent: Score
    negotiable: Score
    valuable: Score
    estimable: Score
    small: Score
    testable: Score
    note: str = ""

    @property
    def total(self) -> float:
        points: int = sum(getattr(self, c) for c in CRITERIA)
        return points / (2 * len(CRITERIA))


class InvestAnswer(_Strict):
    stories: list[StoryScore]
    coverage: Score
    missing: list[str] = []


@dataclass(frozen=True)
class PlanCase:
    id: str
    path: Path


@dataclass(frozen=True)
class PlannerSuite:
    dir: Path
    cases: list[PlanCase]
    claude_md: str
    tree: str

    def inputs(self, case: PlanCase) -> PlannerInputs:
        return PlannerInputs(
            plan_md=case.path.read_text(encoding="utf-8"),
            plan_file=f"evals/suites/planner/plans/{case.path.name}",
            claude_md=self.claude_md,
            repo_tree=self.tree,
        )


def load_planner_suite(root: Path = SUITES_DIR) -> PlannerSuite:
    base = root / "planner"
    spec = yaml.safe_load((base / "suite.yaml").read_text(encoding="utf-8"))
    cases = [PlanCase(p, base / "plans" / f"{p}.md") for p in spec["plans"]]
    return PlannerSuite(
        dir=base,
        cases=cases,
        claude_md=(base / "context" / "CLAUDE.md").read_text(encoding="utf-8"),
        tree=(base / "context" / "tree.txt").read_text(encoding="utf-8"),
    )


def _tickets_md(draft: Draft) -> str:
    lines = [f"Epic: {draft.plan.epic.summary}", ""]
    for s in draft.plan.stories:
        lines += [
            f"## {s.ref}: {s.summary} ({s.suggested_points} pt, risk {s.risk})",
            s.user_story,
            "Acceptance criteria:",
            *[f"- {c}" for c in s.acceptance_criteria],
            f"Depends on: {', '.join(s.depends_on) or 'none'}",
            f"Unit tests: {'; '.join(s.test_plan.unit) or 'none'}",
            f"E2E tests: {'; '.join(s.test_plan.e2e) or 'none'}",
        ]
        if s.technical_notes_md:
            lines += ["Technical notes:", s.technical_notes_md]
        lines.append("")
    return "\n".join(lines)


async def judge_invest(
    backends: Sequence[ChatBackend], plan_md: str, draft: Draft
) -> tuple[InvestAnswer, float]:
    messages = [
        ChatMessage("system", RUBRIC.read_text(encoding="utf-8")),
        ChatMessage(
            "user",
            f"<plan>\n{plan_md}\n</plan>\n\n<tickets>\n{_tickets_md(draft)}\n</tickets>",
        ),
    ]
    answer, call = await structured(backends, messages, InvestAnswer, "invest")
    return answer, call.cost_usd or 0.0


def planner_summary(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in rows if r["valid"]]
    scored = [r["invest"] for r in valid if r["invest"] is not None]
    covered = [r["coverage"] for r in valid if r["coverage"] is not None]

    def share(n: int) -> float:
        return round(n / len(rows), 4) if rows else 0.0

    return {
        "plans": len({r["plan"] for r in rows}),
        "runs": len(rows),
        "schema_valid": share(len(valid)),
        "first_try_valid": share(sum(r["attempts"] == 1 for r in valid)),
        "invest": round(statistics.mean(scored), 4) if scored else None,
        "coverage": round(statistics.mean(covered), 4) if covered else None,
        "median_stories": statistics.median(r["stories"] for r in valid) if valid else None,
        "total_cost_usd": round(sum(r["cost_usd"] for r in rows), 4),
        "models": sorted({r["model"] for r in rows if r.get("model")}),
    }


async def run_planner_eval(
    engine: Engine,
    suite: PlannerSuite,
    planner: Sequence[ChatBackend],
    judge: Sequence[ChatBackend],
    *,
    config_name: str = "default",
    repeats: int = 1,
    cases: Sequence[PlanCase] | None = None,
    echo: Echo = print,
) -> str:
    eval_id = str(ULID())
    _start_run(engine, eval_id, "planner", config_name, prompt_hash("planner")[:12], None)
    rows: list[dict[str, Any]] = []
    try:
        for case in cases or suite.cases:
            for repeat in range(repeats):
                rows.append(await _one(engine, eval_id, suite, case, repeat, planner, judge, echo))
    finally:
        _finish_run(engine, eval_id, planner_summary(rows))
    return eval_id


async def _one(
    engine: Engine,
    eval_id: str,
    suite: PlannerSuite,
    case: PlanCase,
    repeat: int,
    planner: Sequence[ChatBackend],
    judge: Sequence[ChatBackend],
    echo: Echo,
) -> dict[str, Any]:
    started = time.monotonic()
    inputs = suite.inputs(case)
    notes: dict[str, Any] = {}
    row: dict[str, Any] = {
        "plan": case.id, "valid": False, "attempts": 0, "invest": None, "coverage": None,
        "stories": 0, "cost_usd": 0.0,
    }  # fmt: skip
    try:
        draft = await draft_plan(inputs, planner)
    except (PlannerError, BackendUnavailable) as e:
        notes["error"] = str(e)[:500]
    else:
        row.update(valid=True, attempts=draft.attempts, stories=len(draft.plan.stories))
        row["cost_usd"] += draft.cost_usd or 0.0
        notes["model"] = row["model"] = draft.model
        try:
            answer, cost = await judge_invest(judge, inputs.plan_md, draft)
        except BackendUnavailable as e:
            notes["judge_error"] = str(e)[:500]
        else:
            row["cost_usd"] += cost
            if answer.stories:
                row["invest"] = round(statistics.mean(s.total for s in answer.stories), 4)
            row["coverage"] = answer.coverage / 2
            notes["missing"] = answer.missing[:10]
            notes["weak"] = [
                f"{s.ref}: {', '.join(c for c in CRITERIA if getattr(s, c) == 0)}"
                for s in answer.stories
                if any(getattr(s, c) == 0 for c in CRITERIA)
            ][:10]
    _add_result(
        engine, eval_id,
        task_id=case.id, repeat_idx=repeat, passed=row["valid"],
        hidden_pass_ratio=row["invest"], cost_usd=round(row["cost_usd"], 4),
        duration_s=round(time.monotonic() - started, 1), notes=json.dumps(notes),
    )  # fmt: skip
    invest = "-" if row["invest"] is None else f"{row['invest']:.2f}"
    echo(
        f"{case.id} #{repeat + 1}: {'valid' if row['valid'] else 'INVALID'}"
        f" ({row['attempts']} attempt(s), {row['stories']} stories), INVEST {invest}"
        + (f", missing {notes['missing']}" if notes.get("missing") else "")
    )
    return row
