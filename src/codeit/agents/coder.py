"""Coder agent logic (PRD 11.2): prompts, rework feedback, the RESULT line, and what the
orchestrator does with each outcome. `coder_run.py` wires this to Jira, git and Docker.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ValidationError

from codeit.backends.base import AgenticResult
from codeit.github_client import PRComment
from codeit.jira_client import Comment, Ticket
from codeit.prompts import render

ROLE: Final = "coder"
NEEDS_HUMAN = "needs-human"

# Orchestrator comments end with this marker, so rework feedback can leave them out.
ORCHESTRATOR_MARK = "_(CodeIt orchestrator)_"
# Comments agents post through jira-mcp are signed like this (mcp_servers/jira/tools.py).
_OWN_SIGNATURES = ("_Posted by coder (run", "_Posted by rebase (run")

TOOLS = ["Read", "Edit", "Write", "Bash", "Glob", "Grep", "Skill", "TodoWrite"]
MCP_TOOLS = [
    "mcp__codeit-jira__get_ticket",
    "mcp__codeit-jira__get_comments",
    "mcp__codeit-jira__add_comment",
]

CoderStatus = Literal["pr_opened", "pr_updated", "blocked", "failed", "committed"]


class CoderResult(BaseModel):
    """The agent's final `RESULT: {...}` line."""

    status: CoderStatus
    pr_url: str | None = None
    notes: str = ""


_RESULT = re.compile(r"RESULT:\s*(\{.*\})", re.DOTALL)


def parse_result(text: str) -> CoderResult | None:
    """The last RESULT line in the agent's final message, or None if there is none."""
    for line in reversed(text.strip().splitlines()):
        m = _RESULT.search(line)
        if not m:
            continue
        try:
            return CoderResult.model_validate(json.loads(m.group(1)))
        except (json.JSONDecodeError, ValidationError):
            return None
    return None


def orchestrator_comment(text: str) -> str:
    return f"{text}\n\n{ORCHESTRATOR_MARK}"


def is_own_comment(body: str) -> bool:
    return ORCHESTRATOR_MARK in body or any(sig in body for sig in _OWN_SIGNATURES)


def ticket_markdown(ticket: Ticket) -> str:
    return f"# {ticket.key}: {ticket.summary}\n\n{ticket.description_md or '_No description._'}"


def feedback_items(jira: list[Comment], pr: list[PRComment]) -> list[str]:
    """Human and reviewer feedback, oldest first, without CodeIt's own status comments."""
    items: list[tuple[datetime, str]] = []
    for c in jira:
        if not is_own_comment(c.body_md):
            items.append((c.created, f"Jira comment by {c.author}: {c.body_md.strip()}"))
    for p in pr:
        if is_own_comment(p.body):
            continue
        where = f" on {p.path}:{p.line}" if p.path else ""
        label = {
            "review": f"PR review ({p.state})",
            "review_comment": "PR comment",
            "comment": "PR comment",
        }
        items.append((p.created_at, f"{label[p.kind]} by {p.author}{where}: {p.body.strip()}"))
    return [text for _, text in sorted(items, key=lambda i: i[0])]


def build_prompts(
    *,
    key: str,
    ticket_md: str,
    branch: str,
    pr_url: str | None,
    feedback: list[str],
    local: bool = False,
) -> tuple[str, str]:
    """(system_append, task prompt)."""
    system = render("coder/system.md", local=local)
    task = render(
        "coder/task.md",
        key=key,
        ticket_md=ticket_md,
        branch=branch,
        pr_url=pr_url,
        feedback=feedback,
        rework=bool(pr_url or feedback),
        local=local,
    )
    return system, task


# outcome ----------------------------------------------------------------------------------------

Action = Literal["agent_review", "human_review", "ready_for_dev", "none"]


@dataclass(frozen=True)
class Outcome:
    """What the orchestrator does after a run (PRD 11.2 result table)."""

    action: Action
    status: str  # recorded in runs.status
    comment: str
    pr_url: str | None = None


def decide(
    agent: AgenticResult,
    result: CoderResult | None,
    *,
    pr_url: str | None,
    pr_verified: bool,
    transcript_tail: str,
) -> Outcome:
    """Pure decision from the agent's run, its RESULT line and the GitHub check."""
    if agent.status == "usage_limited":
        until = agent.reset_at.isoformat() if agent.reset_at else "the limit resets"
        return Outcome(
            "ready_for_dev", "usage_limited", f"Claude usage limit reached; parked until {until}."
        )
    if agent.status == "completed" and result and result.status in ("pr_opened", "pr_updated"):
        if pr_verified and pr_url:
            verb = "Opened" if result.status == "pr_opened" else "Updated"
            note = f"\n\n{result.notes}" if result.notes else ""
            return Outcome("agent_review", result.status, f"{verb} {pr_url}{note}", pr_url)
        problem = "the agent reported a PR, but GitHub shows no open PR with new commits"
        return Outcome("human_review", "failed", _failure(problem, result.notes, transcript_tail))
    if agent.status == "completed" and result and result.status == "committed":
        return Outcome("none", "committed", result.notes)
    if result and result.status in ("blocked", "failed"):
        return Outcome(
            "human_review",
            result.status,
            _failure(f"Coder {result.status}", result.notes, transcript_tail),
        )
    reasons = {
        "max_turns": "the run hit its turn limit",
        "timeout": "the run hit its wall-clock timeout",
        "error": "the run failed",
        "completed": "the run ended without a RESULT line",
    }
    return Outcome(
        "human_review",
        agent.status if agent.status != "completed" else "failed",
        _failure(
            f"Coder stopped: {reasons.get(agent.status, agent.status)}",
            agent.final_message[:500],
            transcript_tail,
        ),
    )


def _failure(headline: str, notes: str, tail: str) -> str:
    parts = [f"**{headline}.**"]
    if notes:
        parts.append(f"Notes: {notes}")
    if tail:
        parts.append(f"Last output:\n\n```\n{tail}\n```")
    parts.append("Labelled `needs-human`.")
    return "\n\n".join(parts)
