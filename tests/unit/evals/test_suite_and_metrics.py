from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from codeit.evals.hidden import HiddenResult, parse_playwright, parse_vitest, score
from codeit.evals.metrics import CoderRow, ReviewRow, coder_summary, review_summary
from codeit.evals.suite import load_config, load_suite, steering_sha

MUTANT_KINDS = {
    "off_by_one",
    "missing_check",
    "missing_null_check",
    "wrong_status",
    "test_asserts_nothing",
    "ui_not_wired",
    "logic_error",
    "migration",
    "sql_injection",
}


def test_golden_suite_shape() -> None:
    suite = load_suite("golden")
    assert len(suite.tasks) == 12
    assert Counter(t.points for t in suite.tasks) == {1: 4, 2: 5, 3: 3}  # PRD 17.1
    assert {t.kind for t in suite.tasks} == {"api", "ui", "full-stack"}
    for task in suite.tasks:
        assert task.dir.name.startswith(task.id)
        assert task.ticket_md.startswith(f"# {task.id}: ")
        assert "## Acceptance criteria" in task.ticket_md
        assert task.reference_patch.read_text().startswith("diff --git")
        assert task.hidden_files(), task.id
        assert all(f.startswith(("tests/unit/hidden/", "e2e/hidden-")) for f in task.hidden_files())
        mutants = task.mutants()
        assert [m.id for m in mutants] == ["M1", "M2"], task.id
        assert {m.kind for m in mutants} <= MUTANT_KINDS
        assert all(m.patch.read_text().startswith("diff --git") for m in mutants)
    assert {"test_asserts_nothing", "off_by_one", "wrong_status", "ui_not_wired"} <= {
        m.kind for t in suite.tasks for m in t.mutants()
    }
    assert suite.is_e2e("e2e/hidden-T010.spec.ts") and not suite.is_e2e("tests/unit/x.test.ts")
    assert [t.id for t in suite.select(["t003", "T001"])] == ["T001", "T003"]
    with pytest.raises(ValueError, match="T099"):
        suite.select(["T099"])


def test_config_and_steering_sha(tmp_path: Path) -> None:
    assert load_config("default").backend == "claude_code"
    with pytest.raises(ValueError, match="no eval config"):
        load_config("nope")
    a = tmp_path / "a.md"
    a.write_text("one")
    first = steering_sha([a, tmp_path / "missing.md"])
    assert len(first) == 12 and first == steering_sha([a])
    a.write_text("two")
    assert steering_sha([a]) != first


VITEST = {
    "numPassedTests": 2,
    "numTotalTests": 3,
    "testResults": [
        {
            "name": "/workspace/tests/unit/hidden/T001.test.ts",
            "status": "failed",
            "assertionResults": [
                {"fullName": "a passes", "status": "passed"},
                {"fullName": "b passes", "status": "passed"},
                {"fullName": "c fails", "status": "failed"},
            ],
        },
        {
            "name": "/workspace/tests/unit/hidden/T001b.test.ts",
            "status": "failed",
            "message": "Failed to load url ../../src/x.ts",
            "assertionResults": [],
        },
    ],
}
PLAYWRIGHT = {
    "stats": {"expected": 1, "unexpected": 1, "flaky": 0, "skipped": 0},
    "suites": [
        {
            "specs": [{"title": "ok", "ok": True}],
            "suites": [{"specs": [{"title": "bad", "ok": False}]}],
        }
    ],
    "errors": [],
}


def test_parse_reports() -> None:
    v = parse_vitest(VITEST)
    assert (v.passed, v.total, v.failed) == (2, 3, ["c fails"])
    assert v.errors == ["T001b.test.ts: Failed to load url ../../src/x.ts"]
    p = parse_playwright(PLAYWRIGHT)
    assert (p.passed, p.total, p.failed) == (1, 2, ["bad"])
    assert HiddenResult(3, 3).all_passed and not HiddenResult(0, 0).all_passed


class FakeRunner:
    """Writes the report a real test run would, into the workspace."""

    def __init__(
        self, workspace: Path, reports: dict[str, dict[str, object]], install: int = 0
    ) -> None:
        self.workspace = workspace
        self.reports = reports
        self.install = install
        self.commands: list[str] = []

    async def __call__(self, command: str, timeout_s: float) -> tuple[int | None, bool, str]:
        self.commands.append(command)
        if command == "npm ci":
            return self.install, False, "npm ERR!" if self.install else ""
        for kind, report in self.reports.items():
            if f".codeit-eval/{kind}.json" in command:
                (self.workspace / ".codeit-eval" / f"{kind}.json").write_text(json.dumps(report))
                return 1, False, ""
        return 1, False, "no report"


async def test_score_runs_unit_and_e2e(tmp_path: Path) -> None:
    suite = load_suite("golden")
    task = next(t for t in suite.tasks if t.id == "T010")
    run = FakeRunner(tmp_path, {"unit": VITEST, "e2e": PLAYWRIGHT})
    result = await score(suite, task.model_copy(update={"hidden_total": 9}), tmp_path, run)
    assert (tmp_path / "tests/unit/hidden/T010.api.test.ts").exists()
    assert (tmp_path / "e2e/hidden-T010.spec.ts").exists()
    assert run.commands[0] == "npm ci"
    assert "npx vitest run" in run.commands[1] and "T010.api.test.ts" in run.commands[1]
    assert run.commands[2].startswith("npx playwright test") and "hidden-T010" in run.commands[2]
    assert (result.passed, result.total) == (3, 9)  # hidden_total covers tests that never loaded


async def test_score_install_failure(tmp_path: Path) -> None:
    suite = load_suite("golden")
    task = suite.tasks[0].model_copy(update={"hidden_total": 9})
    result = await score(suite, task, tmp_path, FakeRunner(tmp_path, {}, install=1))
    assert (result.passed, result.total) == (0, 9) and "install failed" in result.errors[0]


def test_coder_metrics() -> None:
    rows = [
        CoderRow(
            "T001",
            0,
            True,
            1.0,
            turns=10,
            cost_usd=0.2,
            duration_s=100,
            diff_lines=40,
            reviewer_verdict="pass",
        ),
        CoderRow("T001", 1, True, 1.0, turns=12, cost_usd=0.3, duration_s=120, diff_lines=50),
        CoderRow(
            "T001",
            2,
            False,
            0.5,
            turns=20,
            cost_usd=0.5,
            duration_s=300,
            diff_lines=90,
            reviewer_verdict="fail_critical",
        ),
        CoderRow("T002", 0, True, 1.0),
        CoderRow("T002", 1, True, 1.0),
        CoderRow("T002", 2, True, 1.0),
    ]
    s = coder_summary(rows, 3)
    assert s["pass@1"] == pytest.approx(5 / 6, abs=1e-4)
    assert s["pass^3"] == 0.5  # T002 passed all three, T001 did not
    assert s["hidden_pass_ratio"] == pytest.approx(5.5 / 6, abs=1e-4)
    assert s["reviewer_first_pass"] == 0.5
    assert (s["median_turns"], s["median_cost_usd"], s["median_diff_lines"]) == (12, 0.3, 50)
    assert s["per_task"]["T001"] == {
        "passed": 2,
        "runs": 3,
        "hidden_pass_ratio": pytest.approx(0.8333, abs=1e-4),
    }
    assert coder_summary([], 3)["pass@1"] is None


def test_review_metrics() -> None:
    rows = [
        ReviewRow("T001", "clean", "clean", "pass"),
        ReviewRow("T002", "clean", "clean", "fail_critical"),
        ReviewRow("T001", "M1", "wrong_status", "fail_critical", cost_usd=0.01),
        ReviewRow("T001", "M2", "test_asserts_nothing", "pass_with_notes"),
        ReviewRow("T002", "M1", "wrong_status", None),
    ]
    s = review_summary(rows)
    assert s["critical_catch_rate"] == pytest.approx(1 / 3, abs=1e-4)
    assert s["false_fail_rate"] == 0.5
    assert s["catch_rate_by_kind"] == {"test_asserts_nothing": 0.0, "wrong_status": 0.5}
    assert s["no_verdict"] == 1 and s["total_cost_usd"] == 0.01
    assert not ReviewRow("T", "clean", "clean", None).correct  # no verdict is never right
