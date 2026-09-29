from __future__ import annotations

from typing import Any

import pytest

from codeit.agents.reviewer.checks import (
    NO_TESTS,
    TAUTOLOGICAL,
    Phase1,
    changed_test_files,
    ci_status,
    matches,
    new_tests_fail_on_base,
    run_commands,
)
from codeit.target import TargetCommands, TargetConfig

TARGET = TargetConfig(
    commands=TargetCommands(
        install="npm ci",
        lint="npm run lint",
        typecheck="npm run typecheck",
        unit="npm test -- --run",
        e2e="npx playwright test",
    ),
    test_globs=["tests/**/*.test.ts", "tests/**/*.test.tsx", "e2e/**/*.ts"],
)


class FakeRunner:
    """Answers commands by prefix: {prefix: (exit code, timed out, output)}."""

    def __init__(self, answers: dict[str, tuple[int | None, bool, str]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    async def __call__(self, command: str, timeout_s: float) -> tuple[int | None, bool, str]:
        self.calls.append(command)
        for prefix, answer in self.answers.items():
            if command.startswith(prefix):
                return answer
        return 0, False, "ok"


def test_matches_and_changed_tests() -> None:
    assert matches("e2e/tasks.spec.ts", ["e2e/**"])
    assert matches("tests/unit/server/a.test.ts", ["tests/**/*.test.ts"])
    assert matches("tests/a.test.ts", ["tests/**/*.test.ts"])  # ** also matches no folder
    assert not matches("src/a.ts", ["tests/**/*.test.ts"])
    changed = ["src/server/app.ts", "tests/unit/server/a.test.ts", "e2e/tasks.spec.ts", "README.md"]
    assert changed_test_files(changed, TARGET) == [
        "tests/unit/server/a.test.ts",
        "e2e/tasks.spec.ts",
    ]


async def test_commands_run_in_order_with_status() -> None:
    runner = FakeRunner(
        {"npm run lint": (1, False, "2 problems"), "npx playwright": (None, True, "")}
    )
    results = await run_commands(TARGET, runner, 60)
    assert [(r.name, r.status, r.note) for r in results] == [
        ("install", "pass", ""),
        ("lint", "fail", "exit 1"),
        ("typecheck", "pass", ""),
        ("unit", "pass", ""),
        ("e2e", "fail", "timed out after 60s"),
    ]
    assert results[1].log_tail == "2 problems"
    phase1 = Phase1(checks=results)
    assert [c.name for c in phase1.blocking_failures] == ["e2e"]  # lint is not blocking


async def test_failed_install_skips_the_rest() -> None:
    results = await run_commands(TARGET, FakeRunner({"npm ci": (1, False, "ERESOLVE")}), 60)
    assert [r.status for r in results] == ["fail", "skipped", "skipped", "skipped", "skipped"]
    assert Phase1(checks=results).blocking_failures[0].name == "install"


async def test_new_tests_fail_on_base_passes_when_a_test_fails() -> None:
    runner = FakeRunner({"npm test": (1, False, "1 failed")})
    result = await new_tests_fail_on_base(
        TARGET,
        runner,
        test_files=["tests/unit/a.test.ts", "e2e/x.spec.ts"],
        head="HEAD1",
        base="BASE1",
        timeout_s=60,
    )
    assert result.status == "pass"
    setup, unit, e2e, restore = runner.calls
    assert setup == (
        "git checkout --force --detach BASE1 && git checkout HEAD1 -- "
        "tests/unit/a.test.ts e2e/x.spec.ts"
    )
    assert unit == "npm test -- --run tests/unit/a.test.ts"
    assert e2e == "npx playwright test e2e/x.spec.ts"
    assert restore == "git checkout --force --detach HEAD1 && git clean -fd"


async def test_tautological_tests_fail_the_check() -> None:
    runner = FakeRunner({})  # every command passes, even on the base
    result = await new_tests_fail_on_base(
        TARGET, runner, test_files=["tests/unit/a.test.ts"], head="H", base="B", timeout_s=60
    )
    assert result.status == "fail" and TAUTOLOGICAL in result.note
    assert runner.calls[-1].startswith("git checkout --force --detach H")  # always restored


async def test_no_test_files_is_skipped() -> None:
    result = await new_tests_fail_on_base(
        TARGET, FakeRunner({}), test_files=[], head="H", base="B", timeout_s=1
    )
    assert (result.status, result.note) == ("skipped", NO_TESTS)


async def test_base_checkout_failure() -> None:
    runner = FakeRunner({"git checkout --force --detach B": (1, False, "fatal: bad object")})
    result = await new_tests_fail_on_base(
        TARGET, runner, test_files=["tests/unit/a.test.ts"], head="H", base="B", timeout_s=1
    )
    assert result.status == "fail" and "could not check out the base" in result.note


async def no_sleep(_: float) -> None:
    return None


@pytest.mark.parametrize(
    ("runs", "status"),
    [
        ([("checks", "completed", "success"), ("e2e", "completed", "success")], "pass"),
        ([("checks", "completed", "success"), ("e2e", "completed", "failure")], "fail"),
        ([("checks", "completed", "skipped")], "pass"),
    ],
)
async def test_ci_status(runs: list[Any], status: str) -> None:
    async def fetch() -> list[Any]:
        return runs

    assert (await ci_status(fetch, sleep=no_sleep)).status == status


async def test_ci_status_waits_then_reports_pending() -> None:
    calls = 0

    async def fetch() -> list[Any]:
        nonlocal calls
        calls += 1
        return [("e2e", "in_progress", None)]

    result = await ci_status(fetch, wait_s=0, sleep=no_sleep)
    assert result.status == "pending" and "e2e: in_progress" in result.note and calls == 1


async def test_ci_status_polls_until_done() -> None:
    answers = [[("e2e", "queued", None)], [("e2e", "completed", "success")]]

    async def fetch() -> list[Any]:
        return answers.pop(0)

    assert (await ci_status(fetch, wait_s=60, sleep=no_sleep)).status == "pass"
