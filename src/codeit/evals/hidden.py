"""Scoring with hidden tests (PRD 17.2 step 3): copy them into the workspace and run them in
a worker container, reading the counts from the test runners' JSON reports.

Unit files run with Vitest, files matching the suite's e2e globs with Playwright. A hidden
file that does not even load (for example it imports a module the change should have
created) counts all its tests as failed, through the task's `hidden_total`.
"""

from __future__ import annotations

import json
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codeit.agents.reviewer.checks import Runner
from codeit.evals.suite import Suite, Task

REPORT_DIR = ".codeit-eval"
INSTALL_TIMEOUT_S = 600
TEST_TIMEOUT_S = 600


@dataclass
class HiddenResult:
    passed: int = 0
    total: int = 0
    failed: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return self.total > 0 and self.passed == self.total and not self.errors

    @property
    def ratio(self) -> float:
        return self.passed / self.total if self.total else 0.0


def copy_hidden(task: Task, workspace: Path) -> list[str]:
    root = task.dir / "hidden_tests"
    files = task.hidden_files()
    for rel in files:
        dest = workspace / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / rel, dest)
    return files


def parse_vitest(report: dict[str, Any]) -> HiddenResult:
    result = HiddenResult(
        passed=int(report.get("numPassedTests", 0)), total=int(report.get("numTotalTests", 0))
    )
    for suite in report.get("testResults", []):
        for t in suite.get("assertionResults", []):
            if t.get("status") != "passed":
                result.failed.append(str(t.get("fullName") or t.get("title")))
        if suite.get("status") == "failed" and not suite.get("assertionResults"):
            result.errors.append(
                f"{Path(str(suite.get('name', ''))).name}: {suite.get('message', '')[:300]}"
            )
    return result


def parse_playwright(report: dict[str, Any]) -> HiddenResult:
    stats = report.get("stats", {})
    ok = int(stats.get("expected", 0)) + int(stats.get("flaky", 0))
    total = ok + int(stats.get("unexpected", 0)) + int(stats.get("skipped", 0))
    result = HiddenResult(passed=ok, total=total)

    def walk(suite: dict[str, Any]) -> None:
        for spec in suite.get("specs", []):
            if not spec.get("ok", False):
                result.failed.append(str(spec.get("title")))
        for child in suite.get("suites", []):
            walk(child)

    for s in report.get("suites", []):
        walk(s)
    for e in report.get("errors", []):
        result.errors.append(str(e.get("message", e))[:300])
    return result


def merge(a: HiddenResult, b: HiddenResult) -> HiddenResult:
    return HiddenResult(
        a.passed + b.passed, a.total + b.total, a.failed + b.failed, a.errors + b.errors
    )


async def score(
    suite: Suite, task: Task, workspace: Path, run: Runner, *, install: bool = True
) -> HiddenResult:
    """Copy the hidden tests in and run them. `run` executes a shell command in a container
    whose /workspace is `workspace`."""
    files = copy_hidden(task, workspace)
    (workspace / REPORT_DIR).mkdir(exist_ok=True)
    result = HiddenResult()
    if install:
        code, timed_out, tail = await run(suite.commands.install, INSTALL_TIMEOUT_S)
        if code != 0 or timed_out:
            return HiddenResult(0, task.hidden_total or 0, [], [f"install failed: {tail[-500:]}"])
    groups = {
        "unit": [f for f in files if not suite.is_e2e(f)],
        "e2e": [f for f in files if suite.is_e2e(f)],
    }
    for kind, group in groups.items():
        if not group:
            continue
        out = f"{REPORT_DIR}/{kind}.json"
        template = suite.commands.unit if kind == "unit" else suite.commands.e2e
        command = template.format(files=" ".join(shlex.quote(f) for f in group), out=out)
        _, timed_out, tail = await run(command, TEST_TIMEOUT_S)
        report_path = workspace / out
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            error = "timed out" if timed_out else f"no {kind} report: {tail[-500:]}"
            result.errors.append(error)
            continue
        part = parse_vitest(report) if kind == "unit" else parse_playwright(report)
        result = merge(result, part)
    if task.hidden_total is not None and result.total < task.hidden_total:
        result.total = task.hidden_total  # tests in files that failed to load count as failed
    return result
