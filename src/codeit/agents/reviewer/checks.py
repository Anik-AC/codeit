"""Reviewer phase 1 (PRD 11.3): deterministic checks on the PR head, in a worker container.

The target's `codeit.yaml` commands run in order (install, lint, typecheck, unit, e2e), then
`new_tests_fail_on_base`, then the CI status from GitHub. Each check keeps its status,
duration and the last 200 lines of output.

`new_tests_fail_on_base`: check out the PR's merge base (so files the PR added are gone),
bring back only the test files the PR added or changed, and run them. At least one must fail;
if all pass, the tests do not exercise the change (`TESTS_DO_NOT_EXERCISE_CHANGE`).
"""

from __future__ import annotations

import asyncio
import fnmatch
import shlex
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from codeit.sandbox.containers import ExecResult, Sandbox
from codeit.target import TargetConfig

CheckStatus = Literal["pass", "fail", "skipped", "pending"]
LOG_LINES = 200
# A failure in any of these forces `fail_critical`, whatever the model says (PRD 11.3).
BLOCKING = frozenset({"install", "typecheck", "unit", "e2e", "new_tests_fail_on_base"})

NO_TESTS = "the PR changes no test files"
TAUTOLOGICAL = "TESTS_DO_NOT_EXERCISE_CHANGE"


@dataclass
class CheckResult:
    name: str
    status: CheckStatus
    duration_s: float = 0.0
    command: str = ""
    log_tail: str = ""
    note: str = ""

    @property
    def blocking_failure(self) -> bool:
        return self.name in BLOCKING and self.status == "fail"


@dataclass
class Phase1:
    checks: list[CheckResult] = field(default_factory=list)
    test_files: list[str] = field(default_factory=list)

    def get(self, name: str) -> CheckResult | None:
        return next((c for c in self.checks if c.name == name), None)

    @property
    def blocking_failures(self) -> list[CheckResult]:
        return [c for c in self.checks if c.blocking_failure]


def matches(path: str, globs: Sequence[str]) -> bool:
    """Glob match where `**` spans directories (so `e2e/**` matches `e2e/a/b.ts`)."""
    return any(
        fnmatch.fnmatch(path, g) or fnmatch.fnmatch(path, g.replace("**/", "")) for g in globs
    )


def changed_test_files(changed: Sequence[str], target: TargetConfig) -> list[str]:
    return [p for p in changed if matches(p, target.test_globs)]


Runner = Callable[[str, float], Awaitable[tuple[int | None, bool, str]]]


def container_runner(sandbox: Sandbox, container_id: str) -> Runner:
    """Runs a shell command in the container; returns (exit code, timed out, output tail)."""

    async def run(command: str, timeout_s: float) -> tuple[int | None, bool, str]:
        result = ExecResult()
        lines: list[str] = []
        async for line in sandbox.exec_lines(
            container_id, ["bash", "-lc", f"{command} 2>&1"], timeout_s=timeout_s, result=result
        ):
            lines.append(line)
            if len(lines) > LOG_LINES * 2:
                del lines[:LOG_LINES]
        return result.exit_code, result.timed_out, "\n".join(lines[-LOG_LINES:])

    return run


async def _timed(name: str, command: str, run: Runner, timeout_s: float) -> CheckResult:
    start = time.monotonic()
    code, timed_out, tail = await run(command, timeout_s)
    status: CheckStatus = "pass" if code == 0 and not timed_out else "fail"
    note = (
        f"timed out after {timeout_s:.0f}s" if timed_out else ("" if code == 0 else f"exit {code}")
    )
    return CheckResult(name, status, round(time.monotonic() - start, 1), command, tail, note)


async def run_commands(target: TargetConfig, run: Runner, timeout_s: float) -> list[CheckResult]:
    """install, lint, typecheck, unit, e2e. After a failed install the rest are skipped."""
    results: list[CheckResult] = []
    for name, command in target.commands.in_order():
        if results and results[0].status == "fail":
            results.append(CheckResult(name, "skipped", command=command, note="install failed"))
            continue
        results.append(await _timed(name, command, run, timeout_s))
    return results


async def new_tests_fail_on_base(
    target: TargetConfig,
    run: Runner,
    *,
    test_files: Sequence[str],
    head: str,
    base: str,
    timeout_s: float,
) -> CheckResult:
    name = "new_tests_fail_on_base"
    if not test_files:
        return CheckResult(name, "skipped", note=NO_TESTS)
    e2e = [f for f in test_files if matches(f, target.e2e_globs)]
    unit = [f for f in test_files if f not in e2e]
    files = " ".join(shlex.quote(f) for f in test_files)
    setup = (
        f"git checkout --force --detach {shlex.quote(base)}"
        f" && git checkout {shlex.quote(head)} -- {files}"
    )
    restore = f"git checkout --force --detach {shlex.quote(head)} && git clean -fd"
    commands = []
    if unit:
        commands.append(f"{target.commands.unit} {' '.join(shlex.quote(f) for f in unit)}")
    if e2e:
        commands.append(f"{target.commands.e2e} {' '.join(shlex.quote(f) for f in e2e)}")
    start = time.monotonic()
    code, timed_out, tail = await run(setup, 120)
    if code != 0:
        await run(restore, 120)
        return CheckResult(name, "fail", note=f"could not check out the base: {tail[-500:]}")
    outcomes = []
    tails = []
    try:
        for command in commands:
            code, timed_out, tail = await run(command, timeout_s)
            outcomes.append(code != 0 or timed_out)
            tails.append(f"$ {command}\n{tail}")
    finally:
        await run(restore, 120)
    duration = round(time.monotonic() - start, 1)
    shown = "\n\n".join(tails)[-8000:]
    if any(outcomes):
        return CheckResult(
            name,
            "pass",
            duration,
            " && ".join(commands),
            shown,
            "at least one new or changed test fails on the base, as it should",
        )
    return CheckResult(
        name,
        "fail",
        duration,
        " && ".join(commands),
        shown,
        f"{TAUTOLOGICAL}: every new or changed test also passes without the change",
    )


async def ci_status(
    fetch: Callable[[], Awaitable[list[tuple[str, str, str | None]]]],
    *,
    wait_s: float = 600,
    interval_s: float = 15,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> CheckResult:
    """GitHub check runs for the head: pass, fail, or pending if still running at `wait_s`."""
    start = time.monotonic()
    runs: list[tuple[str, str, str | None]] = []
    while True:
        runs = await fetch()
        if runs and all(status == "completed" for _, status, _ in runs):
            break
        if time.monotonic() - start >= wait_s:
            summary = ", ".join(f"{n}: {s}" for n, s, _ in runs) or "no check runs"
            return CheckResult("ci_status", "pending", note=summary)
        await sleep(interval_s)
    failed = [n for n, _, c in runs if c not in ("success", "skipped", "neutral")]
    summary = ", ".join(f"{n}: {c}" for n, _, c in runs)
    duration = round(time.monotonic() - start, 1)
    return CheckResult("ci_status", "fail" if failed else "pass", duration, note=summary)
