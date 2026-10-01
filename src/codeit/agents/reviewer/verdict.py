"""Reviewer phase 2 (PRD 11.3): the model's verdict, the phase 1 override, and routing.

One model call, or two when the diff is over 60 KB (per-file summaries, then the verdict).
An invalid answer is retried once with the errors. A phase 1 failure among install,
typecheck, unit, e2e and new_tests_fail_on_base forces `fail_critical`, whatever the model
said. With no model available, the phase 1 result alone decides a failure; a clean phase 1
without a model review goes to a human.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from codeit.agents.reviewer.checks import NO_TESTS, Phase1
from codeit.backends.base import (
    ChatBackend,
    ChatMessage,
)
from codeit.backends.structured import ModelCall, structured
from codeit.log import get_logger
from codeit.prompts import prompt_path, render

log = get_logger(__name__)

NO_MODEL_REVIEW = (
    "The model review was not available, and the automated checks passed. "
    "A human should review this PR."
)
BIG_DIFF_BYTES = 60_000
MAX_DIFF_BYTES = 400_000
Severity = Literal["critical", "major", "minor", "nit"]
VerdictName = Literal["pass", "pass_with_notes", "fail_critical"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ACCoverage(_Strict):
    criterion: str
    status: Literal["met", "partial", "unmet"]
    evidence: str = ""


class Finding(_Strict):
    severity: Severity
    file: str = ""
    line: int | None = None
    issue: str
    suggestion: str = ""


class Verdict(_Strict):
    verdict: VerdictName
    ac_coverage: list[ACCoverage] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    summary_md: Annotated[str, Field(min_length=1)]


class ReviewAnswer(Verdict):
    """What the model must return for a ticket with acceptance criteria: the coverage and
    findings lists are required, and every criterion must be listed. A model that explains
    the problems only in `summary_md` gets the answer back for a retry."""

    ac_coverage: Annotated[list[ACCoverage], Field(min_length=1)]
    findings: list[Finding]


class FileSummary(_Strict):
    path: str
    summary: str
    concerns: list[str] = Field(default_factory=list)


class FileSummaries(_Strict):
    files: list[FileSummary]


def checks_markdown(phase1: Phase1) -> str:
    rows = ["| Check | Result | Time | Note |", "|---|---|---|---|"]
    for c in phase1.checks:
        rows.append(f"| {c.name} | {c.status} | {c.duration_s:.0f}s | {c.note or '-'} |")
    failed = [c for c in phase1.checks if c.status == "fail" and c.log_tail]
    for c in failed:
        tail = "\n".join(c.log_tail.splitlines()[-40:])
        rows.append(f"\n**{c.name} output (last lines):**\n\n```\n{tail}\n```")
    return "\n".join(rows)


_structured = structured


async def model_verdict(
    backends: Sequence[ChatBackend],
    *,
    key: str,
    ticket_md: str,
    diff: str,
    phase1: Phase1,
    suggestions: Sequence[str] = (),
) -> tuple[Verdict, ModelCall]:
    checklist = prompt_path("reviewer/checklist.md").read_text(encoding="utf-8")
    system = ChatMessage("system", render("reviewer/system.md", checklist=checklist))
    diff = diff[:MAX_DIFF_BYTES]
    summaries = ""
    calls = 0
    cost = 0.0
    if len(diff.encode()) > BIG_DIFF_BYTES:
        prompt = render("reviewer/summarize.md", key=key, ticket_md=ticket_md, diff=diff)
        files, call = await _structured(
            backends, [system, ChatMessage("user", prompt)], FileSummaries, "files"
        )
        calls, cost = call.calls, call.cost_usd or 0
        summaries = "\n".join(
            f"- `{f.path}`: {f.summary}"
            + (f" Concerns: {'; '.join(f.concerns)}" if f.concerns else "")
            for f in files.files
        )
    prompt = render(
        "reviewer/review.md",
        key=key,
        ticket_md=ticket_md,
        checks_md=checks_markdown(phase1),
        suggestions=list(suggestions),
        diff=diff,
        file_summaries=summaries,
    )
    shape: type[Verdict] = ReviewAnswer if has_criteria(ticket_md) else Verdict
    answer, call = await _structured(
        backends, [system, ChatMessage("user", prompt)], shape, "verdict"
    )
    verdict = Verdict.model_validate(answer.model_dump())
    return consistent(verdict), ModelCall(
        call.backend, call.model, calls + call.calls, cost + (call.cost_usd or 0)
    )


def has_criteria(ticket_md: str) -> bool:
    return "acceptance criteria" in ticket_md.lower()


def consistent(verdict: Verdict) -> Verdict:
    """Make the verdict follow the model's own findings (ADR-0015). `fail_critical` needs an
    unmet criterion or a critical finding; `pass` allows no major finding and no partial
    criterion. The seeded-bug eval showed models failing patches over major notes alone.

    A `fail_critical` is only lowered when the model gave its evidence (a coverage list);
    with no evidence there is nothing to overrule it with."""
    critical = any(f.severity == "critical" for f in verdict.findings)
    unmet = any(c.status == "unmet" for c in verdict.ac_coverage)
    notes = any(f.severity == "major" for f in verdict.findings) or any(
        c.status == "partial" for c in verdict.ac_coverage
    )
    if critical or unmet:
        wanted = "fail_critical"
    elif (verdict.verdict == "fail_critical" and verdict.ac_coverage) or (
        verdict.verdict == "pass" and notes
    ):
        wanted = "pass_with_notes"
    else:
        wanted = verdict.verdict
    return verdict if wanted == verdict.verdict else verdict.model_copy(update={"verdict": wanted})


def apply_override(
    verdict: Verdict | None, phase1: Phase1, *, has_criteria: bool
) -> Verdict | None:
    """Phase 1 failures force fail_critical and appear as critical findings (PRD 11.3)."""
    forced: list[Finding] = [
        Finding(
            severity="critical",
            issue=f"Check `{c.name}` failed" + (f": {c.note}" if c.note else "."),
            suggestion=f"Make `{c.command}` pass." if c.command else "",
        )
        for c in phase1.blocking_failures
    ]
    new_tests = phase1.get("new_tests_fail_on_base")
    if new_tests and new_tests.status == "skipped" and new_tests.note == NO_TESTS and has_criteria:
        forced.append(
            Finding(
                severity="critical",
                issue="The PR adds or changes no tests, but the ticket has acceptance criteria.",
                suggestion="Add a test for each acceptance criterion.",
            )
        )
    lint = phase1.get("lint")
    extra = (
        [
            Finding(
                severity="major", issue="Lint fails.", suggestion=f"Run `{lint.command}` and fix."
            )
        ]
        if lint and lint.status == "fail"
        else []
    )
    if verdict is None:
        if not forced:
            return None
        return Verdict(
            verdict="fail_critical",
            findings=forced + extra,
            summary_md="The automated checks failed; the model review was not available.",
        )
    if not forced and not extra:
        return verdict
    return verdict.model_copy(
        update={
            "verdict": "fail_critical" if forced else verdict.verdict,
            "findings": forced + extra + list(verdict.findings),
        }
    )


# routing ----------------------------------------------------------------------------------------

Route = Literal["ready_for_dev", "human_review"]


@dataclass(frozen=True)
class Routing:
    route: Route
    review_loop: int
    needs_human: bool
    reason: str


def route(verdict: Verdict | None, review_loop: int, max_loops: int) -> Routing:
    """PRD 6.2: a failing review goes back to the Coder until the loop cap; anything else
    goes to a human. `review_loop` counts Reviewer bounces only."""
    if verdict is None:
        return Routing("human_review", review_loop, True, "the automatic review could not complete")
    if verdict.verdict != "fail_critical":
        return Routing("human_review", review_loop, False, f"review verdict {verdict.verdict}")
    loops = review_loop + 1
    if loops < max_loops:
        return Routing("ready_for_dev", loops, False, f"review loop {loops} of {max_loops}")
    return Routing("human_review", loops, True, f"review loop cap reached ({loops} of {max_loops})")


def render_review(
    verdict: Verdict | None, phase1: Phase1, *, run_id: str, call: ModelCall | None
) -> str:
    """The PR review body: verdict, summary, criteria, findings, checks."""
    title = verdict.verdict.replace("_", " ") if verdict else "incomplete"
    parts = [
        f"## CodeIt review: **{title}**",
        verdict.summary_md if verdict else NO_MODEL_REVIEW,
    ]
    if verdict and verdict.ac_coverage:
        rows = ["| Criterion | Status | Evidence |", "|---|---|---|"]
        rows += [
            f"| {_cell(a.criterion)} | {a.status} | {_cell(a.evidence) or '-'} |"
            for a in verdict.ac_coverage
        ]
        parts.append("### Acceptance criteria\n\n" + "\n".join(rows))
    if verdict and verdict.findings:
        lines = []
        for f in verdict.findings:
            where = f" `{f.file}{':' + str(f.line) if f.line else ''}`" if f.file else ""
            fix = f" Suggestion: {f.suggestion}" if f.suggestion else ""
            lines.append(f"- **{f.severity}**{where}: {f.issue}{fix}")
        parts.append("### Findings\n\n" + "\n".join(lines))
    parts.append("### Checks\n\n" + checks_markdown(phase1))
    model = f", model {call.model or call.backend}" if call else ""
    parts.append(f"_(CodeIt reviewer, run {run_id}{model})_")
    return "\n\n".join(parts)


def render_jira(
    verdict: Verdict | None, phase1: Phase1, routing: Routing, *, run_id: str, pr_url: str
) -> str:
    """The condensed Jira comment. It is rework feedback for the Coder, so it names every
    critical and major finding."""
    title = verdict.verdict.replace("_", " ") if verdict else "incomplete"
    lines = [f"**Review: {title}** ({routing.reason}). Full review on {pr_url}"]
    failed = [c for c in phase1.checks if c.status == "fail"]
    if failed:
        lines.append("Failed checks: " + ", ".join(f"`{c.name}`" for c in failed))
    if verdict:
        important = [f for f in verdict.findings if f.severity in ("critical", "major")]
        lines += [
            f"- **{f.severity}** {f.file + ': ' if f.file else ''}{f.issue}"
            + (f" Suggestion: {f.suggestion}" if f.suggestion else "")
            for f in important
        ]
        unmet = [a.criterion for a in verdict.ac_coverage if a.status != "met"]
        if unmet:
            lines.append("Criteria not fully met: " + "; ".join(unmet))
    lines.append(f"_(CodeIt reviewer, run {run_id})_")
    return "\n\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def as_dict(verdict: Verdict | None) -> dict[str, Any] | None:
    return verdict.model_dump() if verdict else None
