"""`codeit eval report` (PRD 17.6): one run's summary and results, or a comparison of the
latest run of several configs, which is also saved under `data/evals/reports/`."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from codeit.db.models import EvalResult, EvalRun

CODER_METRICS = (
    "pass@1",
    "pass^k",
    "hidden_pass_ratio",
    "reviewer_first_pass",
    "median_cost_usd",
    "median_turns",
    "median_duration_s",
    "median_diff_lines",
)
REVIEW_METRICS = ("critical_catch_rate", "false_fail_rate", "mutants", "clean", "total_cost_usd")
PLANNER_METRICS = (
    "schema_valid",
    "first_try_valid",
    "invest",
    "coverage",
    "median_stories",
    "total_cost_usd",
)


def _fmt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.2f}" if value >= 1 or value == 0 else f"{value:.3f}"
    return str(value)


def metric(summary: dict[str, Any], name: str) -> Any:
    if name == "pass^k":
        return next((v for k, v in summary.items() if k.startswith("pass^")), None)
    return summary.get(name)


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        widths = [max(w, len(c)) for w, c in zip(widths, row, strict=True)]

    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths, strict=True)) + " |"

    sep = "|" + "|".join("-" * (w + 2) for w in widths) + "|"
    return "\n".join([line(headers), sep, *(line(r) for r in rows)])


def run_report(run: EvalRun, results: Sequence[EvalResult]) -> str:
    s = run.summary_json or {}
    review = run.suite.endswith("-review")
    head = (
        f"Eval {run.id}: {run.suite} / {run.config_name}, model {run.model or 'default'}, "
        f"steering {run.steering_sha or '-'}, {run.started_at:%Y-%m-%d %H:%M}"
        + (f" (stopped: {s['stopped']})" if s.get("stopped") else "")
    )
    planner = run.suite == "planner"
    names = REVIEW_METRICS if review else PLANNER_METRICS if planner else CODER_METRICS
    summary = table(
        ["metric", "value"],
        [[n.replace("^k", f"^{s.get('repeats', 'k')}"), _fmt(metric(s, n))] for n in names],
    )
    if review:
        by_kind = s.get("catch_rate_by_kind", {})
        kinds = table(["mutant kind", "caught"], [[k, _fmt(v)] for k, v in by_kind.items()])
        detail = table(
            ["task/variant", "verdict", "correct"],
            [
                [r.task_id, r.reviewer_verdict or "none", "yes" if r.passed else "NO"]
                for r in results
            ],
        )
        return f"{head}\n\n{summary}\n\n{kinds}\n\n{detail}\n"
    if planner:
        detail = table(
            ["plan", "repeat", "valid", "INVEST", "cost", "notes"],
            [
                [
                    r.task_id,
                    str(r.repeat_idx + 1),
                    "yes" if r.passed else "NO",
                    _fmt(r.hidden_pass_ratio),
                    _fmt(r.cost_usd),
                    _planner_note(r.notes),
                ]
                for r in results
            ],
        )
        return f"{head}\n\n{summary}\n\n{detail}\n"
    detail = table(
        ["task", "repeat", "passed", "hidden", "turns", "cost", "time", "diff", "review"],
        [
            [
                r.task_id,
                str(r.repeat_idx + 1),
                "yes" if r.passed else "NO",
                _fmt(r.hidden_pass_ratio),
                _fmt(r.turns),
                _fmt(r.cost_usd),
                f"{r.duration_s:.0f}s" if r.duration_s else "-",
                _fmt(r.diff_lines),
                r.reviewer_verdict or "-",
            ]
            for r in results
        ],
    )
    return f"{head}\n\n{summary}\n\n{detail}\n"


def latest_by_config(engine: Engine, configs: Sequence[str]) -> list[EvalRun]:
    out = []
    with Session(engine) as s:
        for name in configs:
            run = s.scalars(
                select(EvalRun)
                .where(EvalRun.config_name == name, EvalRun.ended_at.is_not(None))
                .order_by(EvalRun.started_at.desc())
                .limit(1)
            ).first()
            if run is None:
                raise ValueError(f"no finished eval run for config {name!r}")
            out.append(run)
    return out


def compare(runs: Sequence[EvalRun], reports_dir: Path) -> tuple[str, Path]:
    headers = ["metric", *(f"{r.config_name} ({r.id[-6:]})" for r in runs)]
    names = REVIEW_METRICS if all(r.suite.endswith("-review") for r in runs) else CODER_METRICS
    rows = [[n, *(_fmt(metric(r.summary_json or {}, n)) for r in runs)] for n in names]
    rows.append(["steering", *(r.steering_sha or "-" for r in runs)])
    text = table(headers, rows)
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = reports_dir / f"compare-{'-vs-'.join(r.config_name for r in runs)}-{stamp}.md"
    raw = json.dumps({r.id: r.summary_json for r in runs}, indent=2)
    body = f"# Eval comparison {stamp}\n\n{text}\n\n```json\n{raw}\n```\n"
    path.write_text(body, encoding="utf-8")
    return text, path


def _planner_note(notes: str | None) -> str:
    data = json.loads(notes or "{}")
    parts = []
    if data.get("error"):
        parts.append(str(data["error"])[:80])
    if data.get("missing"):
        parts.append("missing: " + "; ".join(data["missing"])[:80])
    if data.get("weak"):
        parts.append("weak: " + "; ".join(data["weak"])[:80])
    return " | ".join(parts) or "-"
