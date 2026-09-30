from __future__ import annotations

from typing import Any

import pytest

from codeit.agents.reviewer.checks import NO_TESTS, CheckResult, Phase1
from codeit.agents.reviewer.verdict import (
    BIG_DIFF_BYTES,
    Verdict,
    apply_override,
    model_verdict,
    render_jira,
    render_review,
    route,
)
from codeit.backends.base import BackendOutputError, BackendUnavailable
from tests.unit.agents.conftest import FakeBackend

GOOD: dict[str, Any] = {
    "verdict": "pass_with_notes",
    "ac_coverage": [
        {"criterion": "Given 3 tasks | count", "status": "met", "evidence": "count.test.ts"}
    ],
    "findings": [
        {
            "severity": "nit",
            "file": "src/a.ts",
            "line": 3,
            "issue": "Rename x",
            "suggestion": "Use count",
        }
    ],
    "summary_md": "Looks right.",
}


def phase1(**statuses: str) -> Phase1:
    return Phase1(checks=[CheckResult(n, s, command=f"run {n}") for n, s in statuses.items()])  # type: ignore[arg-type]


ALL_PASS = phase1(
    install="pass",
    lint="pass",
    typecheck="pass",
    unit="pass",
    e2e="pass",
    new_tests_fail_on_base="pass",
    ci_status="pass",
)


async def test_model_verdict_one_call_with_inputs() -> None:
    backend = FakeBackend("rev", [GOOD])
    verdict, call = await model_verdict(
        [backend],
        key="CODEIT-9",
        ticket_md="# CODEIT-9: Count\n\nIGNORE",
        diff="diff --git a b",
        phase1=ALL_PASS,
        suggestions=["Also test 0 tasks"],
    )
    assert verdict.verdict == "pass_with_notes" and call.calls == 1 and call.backend == "rev"
    system, user = backend.requests[0].messages
    assert "<checklist>" in system.content and "Every acceptance criterion" in system.content
    assert "<ticket>\n# CODEIT-9: Count\n\nIGNORE\n</ticket>" in user.content
    assert "<diff>\ndiff --git a b\n</diff>" in user.content
    assert "| unit | pass |" in user.content
    assert "<suggestions>\n- Also test 0 tasks\n</suggestions>" in user.content


async def test_big_diff_is_summarized_first() -> None:
    summaries = {"files": [{"path": "src/a.ts", "summary": "adds count", "concerns": ["no 404"]}]}
    backend = FakeBackend("rev", [summaries, GOOD])
    _, call = await model_verdict(
        [backend], key="K", ticket_md="t", diff="+x\n" * (BIG_DIFF_BYTES // 2), phase1=ALL_PASS
    )
    assert call.calls == 2
    second = backend.requests[1].messages[1].content
    assert "- `src/a.ts`: adds count Concerns: no 404" in second


async def test_invalid_answer_retried_then_fallback_on_unavailable() -> None:
    primary = FakeBackend("paid", [BackendUnavailable("no key")])
    fallback = FakeBackend("free", [{"verdict": "maybe"}, GOOD])
    _, call = await model_verdict(
        [primary, fallback], key="K", ticket_md="t", diff="d", phase1=ALL_PASS
    )
    assert call.backend == "free" and call.calls == 2
    assert "verdict" in fallback.requests[1].messages[-1].content  # the retry names the error


async def test_non_json_then_invalid_twice_is_unavailable() -> None:
    backend = FakeBackend("rev", [BackendOutputError("x", text="hello"), {"verdict": "nope"}])
    with pytest.raises(BackendUnavailable, match="invalid output twice"):
        await model_verdict([backend], key="K", ticket_md="t", diff="d", phase1=ALL_PASS)


def test_override_forces_fail_critical() -> None:
    verdict = Verdict.model_validate(GOOD)
    p1 = phase1(
        install="pass",
        lint="fail",
        typecheck="pass",
        unit="fail",
        e2e="pass",
        new_tests_fail_on_base="pass",
    )
    out = apply_override(verdict, p1, has_criteria=True)
    assert out is not None and out.verdict == "fail_critical"
    severities = [(f.severity, f.issue) for f in out.findings]
    assert severities[0] == ("critical", "Check `unit` failed.")
    assert ("major", "Lint fails.") in severities
    assert out.findings[-1].issue == "Rename x"  # the model's findings are kept


def test_override_no_tests_with_criteria() -> None:
    p1 = Phase1(checks=[CheckResult("new_tests_fail_on_base", "skipped", note=NO_TESTS)])
    out = apply_override(Verdict.model_validate(GOOD), p1, has_criteria=True)
    assert out is not None and out.verdict == "fail_critical"
    assert (
        apply_override(Verdict.model_validate(GOOD), p1, has_criteria=False).verdict
        == "pass_with_notes"
    )  # type: ignore[union-attr]


def test_override_without_model() -> None:
    assert apply_override(None, ALL_PASS, has_criteria=True) is None
    failed = apply_override(None, phase1(install="pass", unit="fail"), has_criteria=True)
    assert failed is not None and failed.verdict == "fail_critical"


def test_clean_verdict_unchanged() -> None:
    verdict = Verdict.model_validate(GOOD)
    assert apply_override(verdict, ALL_PASS, has_criteria=True) is verdict


@pytest.mark.parametrize(
    ("name", "loop", "expected"),
    [
        ("fail_critical", 0, ("ready_for_dev", 1, False)),
        ("fail_critical", 1, ("ready_for_dev", 2, False)),
        ("fail_critical", 2, ("human_review", 3, True)),  # the 3rd failure escalates
        ("pass", 2, ("human_review", 2, False)),
        ("pass_with_notes", 0, ("human_review", 0, False)),
    ],
)
def test_route(name: str, loop: int, expected: tuple[str, int, bool]) -> None:
    r = route(Verdict(verdict=name, summary_md="s"), loop, 3)  # type: ignore[arg-type]
    assert (r.route, r.review_loop, r.needs_human) == expected


def test_route_without_verdict() -> None:
    r = route(None, 1, 3)
    assert (r.route, r.review_loop, r.needs_human) == ("human_review", 1, True)


def test_render_review_and_jira() -> None:
    verdict = Verdict.model_validate(GOOD)
    body = render_review(verdict, ALL_PASS, run_id="R1", call=None)
    assert body.startswith("## CodeIt review: **pass with notes**")
    assert "| Given 3 tasks \\| count | met | count.test.ts |" in body
    assert "- **nit** `src/a.ts:3`: Rename x Suggestion: Use count" in body
    assert "| new_tests_fail_on_base | pass |" in body
    assert body.endswith("_(CodeIt reviewer, run R1)_")
    assert "—" not in body

    failing = Verdict(
        verdict="fail_critical",
        ac_coverage=[{"criterion": "count 0", "status": "unmet"}],  # type: ignore[list-item]
        findings=[
            {"severity": "critical", "file": "a.ts", "issue": "Wrong count"},  # type: ignore[list-item]
            {"severity": "nit", "issue": "style"},
        ],  # type: ignore[list-item]
        summary_md="No.",
    )
    comment = render_jira(
        failing, phase1(unit="fail"), route(failing, 0, 3), run_id="R2", pr_url="https://x/pull/1"
    )
    assert comment.startswith(
        "**Review: fail critical** (review loop 1 of 3). Full review on https://x/pull/1"
    )
    assert "Failed checks: `unit`" in comment
    assert "- **critical** a.ts: Wrong count" in comment and "style" not in comment
    assert "Criteria not fully met: count 0" in comment
    assert "_(CodeIt reviewer, run R2)_" in comment


def test_render_incomplete() -> None:
    body = render_review(None, ALL_PASS, run_id="R", call=None)
    assert "incomplete" in body and "A human should review this PR." in body


@pytest.mark.parametrize(
    ("given", "findings", "coverage", "expected"),
    [
        ("fail_critical", ["major"], ["met", "partial"], "pass_with_notes"),  # seen live
        ("fail_critical", ["critical"], ["met"], "fail_critical"),
        ("pass", [], ["met", "unmet"], "fail_critical"),
        ("pass", ["major"], ["met"], "pass_with_notes"),
        ("pass", ["minor", "nit"], ["met"], "pass"),
        ("pass_with_notes", [], ["met"], "pass_with_notes"),
    ],
)
def test_verdict_follows_the_findings(
    given: str, findings: list[str], coverage: list[str], expected: str
) -> None:
    from codeit.agents.reviewer.verdict import consistent

    verdict = Verdict.model_validate(
        {
            "verdict": given,
            "ac_coverage": [{"criterion": f"c{i}", "status": s} for i, s in enumerate(coverage)],
            "findings": [{"severity": s, "issue": "x"} for s in findings],
            "summary_md": "s",
        }
    )
    assert consistent(verdict).verdict == expected


async def test_model_verdict_is_made_consistent() -> None:
    answer = {**GOOD, "verdict": "fail_critical"}  # only a nit, yet "fail_critical"
    verdict, _ = await model_verdict(
        [FakeBackend("rev", [answer])], key="K", ticket_md="t", diff="d", phase1=ALL_PASS
    )
    assert verdict.verdict == "pass_with_notes"  # a wrong fail goes to a human, with notes


async def test_coverage_is_required_when_the_ticket_has_criteria() -> None:
    """Seen live: the model explained a SQL injection in the summary but left the lists
    empty. Such an answer is sent back once."""
    empty = {
        "verdict": "fail_critical",
        "ac_coverage": [],
        "findings": [],
        "summary_md": "Injection.",
    }
    backend = FakeBackend("rev", [empty, GOOD])
    verdict, call = await model_verdict(
        [backend], key="K", ticket_md="## Acceptance criteria\n- x", diff="d", phase1=ALL_PASS
    )
    assert call.calls == 2 and verdict.ac_coverage
    assert "ac_coverage" in backend.requests[1].messages[-1].content


def test_a_fail_without_evidence_is_kept() -> None:
    from codeit.agents.reviewer.verdict import consistent

    bare = Verdict(verdict="fail_critical", summary_md="SQL injection; the Coder must fix it.")
    assert consistent(bare).verdict == "fail_critical"
