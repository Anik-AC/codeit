"""Eval metrics (PRD 17.3, 17.4).

Coder runs:
- pass@1: the mean share of repeats in which all hidden tests pass
- pass^k: the share of tasks where every one of the k repeats passes (consistency)
- hidden_pass_ratio: the mean share of hidden tests passing (partial credit)
- reviewer_first_pass: the share of reviewed runs whose verdict is not `fail_critical`
- medians of cost, turns, duration and diff size

Reviewer runs on the seeded-bug suite:
- critical catch rate: the share of mutants the Reviewer calls `fail_critical`
- false-fail rate: the share of clean reference patches it calls `fail_critical`
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CoderRow:
    task_id: str
    repeat_idx: int
    passed: bool
    hidden_pass_ratio: float
    turns: int | None = None
    cost_usd: float | None = None
    duration_s: float | None = None
    diff_lines: int | None = None
    reviewer_verdict: str | None = None


@dataclass(frozen=True)
class ReviewRow:
    task_id: str
    variant: str  # "clean" or a mutant id
    kind: str  # "clean" or the mutant's kind
    verdict: str | None  # None: no verdict (the model was unavailable)
    cost_usd: float | None = None

    @property
    def expected_fail(self) -> bool:
        return self.variant != "clean"

    @property
    def correct(self) -> bool:
        failed = self.verdict == "fail_critical"
        return failed if self.expected_fail else (self.verdict is not None and not failed)


def _median(values: Sequence[float | int | None]) -> float | None:
    present = [float(v) for v in values if v is not None]
    return round(statistics.median(present), 4) if present else None


def _share(hits: int, total: int) -> float | None:
    return round(hits / total, 4) if total else None


def coder_summary(rows: Sequence[CoderRow], repeats: int) -> dict[str, Any]:
    by_task: dict[str, list[CoderRow]] = defaultdict(list)
    for r in rows:
        by_task[r.task_id].append(r)
    complete = {t: rs for t, rs in by_task.items() if len(rs) >= repeats}
    reviewed = [r for r in rows if r.reviewer_verdict is not None]
    return {
        "runs": len(rows),
        "tasks": len(by_task),
        "repeats": repeats,
        "pass@1": _share(sum(r.passed for r in rows), len(rows)),
        f"pass^{repeats}": _share(
            sum(all(r.passed for r in rs) for rs in complete.values()), len(complete)
        ),
        "hidden_pass_ratio": round(statistics.fmean(r.hidden_pass_ratio for r in rows), 4)
        if rows
        else None,
        "reviewer_first_pass": _share(
            sum(r.reviewer_verdict != "fail_critical" for r in reviewed), len(reviewed)
        ),
        "median_cost_usd": _median([r.cost_usd for r in rows]),
        "median_turns": _median([r.turns for r in rows]),
        "median_duration_s": _median([r.duration_s for r in rows]),
        "median_diff_lines": _median([r.diff_lines for r in rows]),
        "per_task": {
            t: {
                "passed": sum(r.passed for r in rs),
                "runs": len(rs),
                "hidden_pass_ratio": round(statistics.fmean(r.hidden_pass_ratio for r in rs), 4),
            }
            for t, rs in sorted(by_task.items())
        },
    }


def review_summary(rows: Sequence[ReviewRow]) -> dict[str, Any]:
    mutants = [r for r in rows if r.expected_fail]
    clean = [r for r in rows if not r.expected_fail]
    by_kind: dict[str, list[ReviewRow]] = defaultdict(list)
    for r in mutants:
        by_kind[r.kind].append(r)
    return {
        "mutants": len(mutants),
        "clean": len(clean),
        "critical_catch_rate": _share(sum(r.correct for r in mutants), len(mutants)),
        "false_fail_rate": _share(sum(r.verdict == "fail_critical" for r in clean), len(clean)),
        "no_verdict": sum(r.verdict is None for r in rows),
        "total_cost_usd": round(sum(r.cost_usd or 0 for r in rows), 4),
        "catch_rate_by_kind": {
            k: _share(sum(r.correct for r in rs), len(rs)) for k, rs in sorted(by_kind.items())
        },
    }
