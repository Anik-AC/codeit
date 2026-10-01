"""The eval gate (PRD 11.7 step 5, ADR-0019): score a proposed steering change before and
after, on the eval that covers the agent it changes, and report it on the proposal.

- Coder (`CLAUDE.md`): the golden Coder eval on `learning.gate_coder_tasks`, with the
  current and the proposed file written over each workspace. Needs Claude, so it waits
  for the run window; until then that part stays pending.
- Reviewer (`checklist.md`): the seeded-bug eval on `learning.gate_review_tasks`.
- Planner (`prompts/planner/system.md`): the Planner eval.

The proposed prompts are used through `prompts.overlay`, so nothing is checked out. A
metric that gets worse by more than `eval.regression_tolerance` marks a regression; the
PR gets the `regression` label. The gate never blocks or merges anything.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from codeit.backends.base import BackendError, ChatBackend
from codeit.backends.registry import chat_route
from codeit.config import Config, Secrets
from codeit.db.models import EvalRun, LearningProposal
from codeit.evals.planner import load_planner_suite, run_planner_eval
from codeit.evals.runner import EvalContext, EvalStopped, run_coder_eval, run_review_eval
from codeit.evals.suite import EvalConfig, load_suite
from codeit.prompts import overlay
from codeit.sandbox.containers import Sandbox

Echo = Callable[[str], None]
Direction = Literal["higher", "lower", "info"]  # info: shown, never a regression

METRICS: dict[str, list[tuple[str, str, Direction]]] = {
    "coder": [
        ("pass@1", "Coder pass@1", "higher"),
        ("hidden_pass_ratio", "Coder: hidden tests passed", "higher"),
        ("median_diff_lines", "Coder: median diff lines", "info"),
        ("median_turns", "Coder: median turns", "info"),
        ("median_cost_usd", "Coder: median cost (USD)", "info"),
    ],
    "reviewer": [
        ("critical_catch_rate", "Reviewer: planted bugs caught", "higher"),
        ("false_fail_rate", "Reviewer: correct patches failed", "lower"),
    ],
    "planner": [
        ("schema_valid", "Planner: valid plans", "higher"),
        ("invest", "Planner: INVEST score", "higher"),
        ("coverage", "Planner: plan coverage", "higher"),
        ("median_stories", "Planner: median stories", "info"),
    ],
}  # fmt: skip
SUITE_NAME = {"coder": "golden", "reviewer": "golden-review", "planner": "planner"}


@dataclass
class Row:
    target: str
    label: str
    before: float | None
    after: float | None
    direction: Direction
    worse: bool

    @property
    def change(self) -> str:
        if self.before is None or self.after is None:
            return "-"
        delta = self.after - self.before
        return "0" if abs(delta) < 1e-9 else f"{delta:+.2f}"


def compare(
    target: str, before: dict[str, Any], after: dict[str, Any], tolerance: float
) -> list[Row]:
    rows = []
    for key, label, direction in METRICS[target]:
        b, a = before.get(key), after.get(key)
        worse = False
        if direction != "info" and isinstance(b, int | float) and isinstance(a, int | float):
            worse = a < b - tolerance if direction == "higher" else a > b + tolerance
        rows.append(Row(target, label, b, a, direction, worse))
    return rows


_NOTE = {"higher": "", "lower": " (lower is better)", "info": " (for information)"}


def table(rows: Sequence[Row], tolerance: float) -> str:
    def fmt(v: float | None) -> str:
        return "-" if v is None else f"{v:.2f}"

    lines = [
        "| Metric | Before | After | Change |",
        "|---|---|---|---|",
        *[
            f"| {r.label}{_NOTE[r.direction]} "
            f"| {fmt(r.before)} | {fmt(r.after)} | {r.change}{' **worse**' if r.worse else ''} |"
            for r in rows
        ],
    ]
    return "\n".join(lines) + f"\n\nA change counts as worse beyond {tolerance:.2f}."


def partial_table(state: dict[str, Any], tolerance: float) -> str:
    rows = [Row(**r) for part in (state.get("parts") or {}).values() for r in part["rows"]]
    return table(rows, tolerance) if rows else ""


def _summary(engine: Engine, eval_id: str) -> dict[str, Any]:
    with Session(engine) as s:
        run = s.get(EvalRun, eval_id)
        return dict(run.summary_json or {}) if run else {}


def _overlay_dir(workdir: Path, files: dict[str, str]) -> Path:
    """Write `prompts/...` files into `workdir/prompts`, laid out for `prompts.overlay`."""
    root = workdir / "prompts"
    for rel, text in files.items():
        dest = root / rel.removeprefix("prompts/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return root


@dataclass
class GateDeps:
    cfg: Config
    secrets: Secrets
    engine: Engine
    sandbox: Sandbox
    echo: Echo
    any_time: bool = False
    reviewer_backends: Sequence[ChatBackend] | None = None
    planner_backends: Sequence[ChatBackend] | None = None
    judge_backends: Sequence[ChatBackend] | None = None


async def run_part(
    deps: GateDeps, proposal_id: str, target: str, change: dict[str, str], workdir: Path
) -> tuple[str, str] | None:
    """Before and after eval ids for one target, or None if it cannot run now."""
    cfg, tag = deps.cfg, proposal_id[-6:]
    path, before, after = change["path"], change["before"], change["after"]
    if target == "coder":
        suite = load_suite("golden")
        tasks = suite.select(cfg.learning.gate_coder_tasks)
        ids = []
        for name, text in (("before", before), ("after", after)):
            ctx = EvalContext(
                cfg, deps.secrets, suite, deps.sandbox, deps.echo, steering={path: text}
            )
            try:
                eval_id = await run_coder_eval(
                    ctx, EvalConfig(name=f"gate-{name}-{tag}"), tasks,
                    repeats=cfg.learning.gate_repeats, any_time=deps.any_time,
                )  # fmt: skip
            except EvalStopped as e:
                deps.echo(f"gate {tag}: the Coder part waits ({e})")
                return None
            if _summary(deps.engine, eval_id).get("stopped"):
                deps.echo(f"gate {tag}: the Coder eval stopped early; it runs again later")
                return None
            ids.append(eval_id)
        return ids[0], ids[1]
    after_dir = _overlay_dir(workdir / "after", {path: after})
    if target == "reviewer":
        suite = load_suite("golden")
        tasks = suite.select(cfg.learning.gate_review_tasks)
        ctx = EvalContext(cfg, deps.secrets, suite, deps.sandbox, deps.echo)
        before_id = await run_review_eval(
            ctx, tasks, backends=deps.reviewer_backends, config_name=f"gate-before-{tag}"
        )
        with overlay(after_dir):
            after_id = await run_review_eval(
                ctx, tasks, backends=deps.reviewer_backends, config_name=f"gate-after-{tag}"
            )
        return before_id, after_id
    planner_suite = load_planner_suite()
    planner = deps.planner_backends or chat_route(cfg, deps.secrets, "planner")
    judge = deps.judge_backends or chat_route(cfg, deps.secrets, "ops")
    before_id = await run_planner_eval(
        deps.engine, planner_suite, planner, judge, config_name=f"gate-before-{tag}", echo=deps.echo
    )
    with overlay(after_dir):
        after_id = await run_planner_eval(
            deps.engine, planner_suite, planner, judge, config_name=f"gate-after-{tag}",
            echo=deps.echo,
        )  # fmt: skip
    return before_id, after_id


async def run_gate(deps: GateDeps, proposal_id: str, workdir: Path) -> LearningProposal:
    """Run the parts of a proposal's gate that are not done yet; finish it when all are."""
    with Session(deps.engine) as s:
        p = s.get(LearningProposal, proposal_id)
        assert p is not None
        changes: dict[str, dict[str, str]] = dict(p.changes_json.get("files") or {})
        state: dict[str, Any] = dict(p.gate_json or {"parts": {}})
    parts: dict[str, Any] = dict(state.get("parts") or {})
    tolerance = deps.cfg.eval.regression_tolerance
    for target, change in changes.items():
        if target in parts:
            continue
        deps.echo(f"gate {proposal_id[-6:]}: {SUITE_NAME[target]} eval before and after")
        try:
            ids = await run_part(deps, proposal_id, target, change, workdir)
        except BackendError as e:
            # A usage limit or an outage: this part runs again on a later run.
            deps.echo(f"gate {proposal_id[-6:]}: the {target} part waits ({str(e)[:200]})")
            continue
        if ids is None:
            continue
        models = [_summary(deps.engine, i).get("models") for i in ids]
        if models[0] and models[1] and models[0] != models[1]:
            # e.g. Claude was limited and the free backup answered one side.
            deps.echo(
                f"gate {proposal_id[-6:]}: {target} runs used different models; it runs again"
            )
            continue
        rows = compare(
            target, _summary(deps.engine, ids[0]), _summary(deps.engine, ids[1]), tolerance
        )
        parts[target] = {
            "before": ids[0],
            "after": ids[1],
            "rows": [r.__dict__ for r in rows],
        }
        _save(deps.engine, proposal_id, {**state, "parts": parts}, None)
    done = all(t in parts for t in changes)
    if not done:
        return _save(deps.engine, proposal_id, {**state, "parts": parts}, "pending")
    rows = [Row(**r) for part in parts.values() for r in part["rows"]]
    regression = any(r.worse for r in rows)
    state = {**state, "parts": parts, "table": table(rows, tolerance), "regression": regression}
    return _save(deps.engine, proposal_id, state, "regression" if regression else "passed")


def _save(
    engine: Engine, proposal_id: str, state: dict[str, Any], status: str | None
) -> LearningProposal:
    with Session(engine, expire_on_commit=False) as s, s.begin():
        p = s.get(LearningProposal, proposal_id)
        assert p is not None
        p.gate_json = state
        if status is not None:
            p.gate_status = status
            if status in ("passed", "regression"):
                p.gated_at = datetime.now(UTC)
        return p
