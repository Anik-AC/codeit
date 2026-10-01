"""SQLAlchemy models for every table in PRD section 13.

Jira stays the source of truth for ticket state; `tickets_cache` exists only for dashboard speed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    type_annotation_map = {  # noqa: RUF012
        datetime: DateTime(timezone=True),
        dict[str, Any]: JSON,
    }


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)  # ULID
    role: Mapped[str] = mapped_column(String(32), index=True)
    instance: Mapped[str] = mapped_column(String(64))
    ticket_key: Mapped[str | None] = mapped_column(String(32), index=True)
    backend: Mapped[str | None] = mapped_column(String(64))
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_hash: Mapped[str | None] = mapped_column(String(64))
    steering_sha: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(32), index=True)
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    turns: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    result_json: Mapped[dict[str, Any] | None]
    transcript_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    # tool_call | message | check | transition | comment | budget | error
    type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]]


class Lease(Base):
    __tablename__ = "leases"

    ticket_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    instance: Mapped[str] = mapped_column(String(64))
    run_id: Mapped[str] = mapped_column(String(26))
    acquired_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    heartbeat_at: Mapped[datetime]


class AgentInstance(Base):
    __tablename__ = "agent_instances"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))  # idle | busy | parked | disabled
    current_run_id: Mapped[str | None] = mapped_column(String(26))
    updated_at: Mapped[datetime]


class TicketCache(Base):
    __tablename__ = "tickets_cache"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text)
    priority: Mapped[str | None] = mapped_column(String(32))
    points: Mapped[float | None] = mapped_column(Float)
    review_loop: Mapped[int] = mapped_column(Integer, default=0)
    human_returns: Mapped[int] = mapped_column(Integer, default=0)
    pr_url: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime]


class BudgetLedger(Base):
    __tablename__ = "budget_ledger"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(index=True)
    backend: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(32))
    key_name: Mapped[str | None] = mapped_column(String(64))
    usd: Mapped[float] = mapped_column(Float, default=0.0)
    requests: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text)


class BackendState(Base):
    __tablename__ = "backend_state"

    backend: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))  # ok | parked | disabled
    parked_until: Mapped[datetime | None]
    last_error: Mapped[str | None] = mapped_column(Text)


class EvalRun(Base):
    __tablename__ = "eval_runs"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    suite: Mapped[str] = mapped_column(String(64))
    config_name: Mapped[str] = mapped_column(String(64))
    steering_sha: Mapped[str | None] = mapped_column(String(40))
    model: Mapped[str | None] = mapped_column(String(128))
    started_at: Mapped[datetime]
    ended_at: Mapped[datetime | None]
    summary_json: Mapped[dict[str, Any] | None]


class EvalResult(Base):
    __tablename__ = "eval_results"

    eval_run_id: Mapped[str] = mapped_column(ForeignKey("eval_runs.id"), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    repeat_idx: Mapped[int] = mapped_column(Integer, primary_key=True)
    passed: Mapped[bool] = mapped_column(Boolean)
    hidden_pass_ratio: Mapped[float | None] = mapped_column(Float)
    turns: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Float)
    duration_s: Mapped[float | None] = mapped_column(Float)
    diff_lines: Mapped[int | None] = mapped_column(Integer)
    reviewer_verdict: Mapped[str | None] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text)


class Signal(Base):
    """Feedback the Learning agent learns from (PRD 11.7, ADR-0019)."""

    __tablename__ = "signals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32))
    ticket_key: Mapped[str | None] = mapped_column(String(32), index=True)
    pr: Mapped[int | None] = mapped_column(Integer)
    author: Mapped[str | None] = mapped_column(String(128))
    text: Mapped[str] = mapped_column(Text)
    theme: Mapped[str | None] = mapped_column(String(32))
    lesson: Mapped[str | None] = mapped_column(Text)
    used_in_learning_run: Mapped[str | None] = mapped_column(String(26))
    # Where it came from, for dedup: e.g. `jira-comment:10231`, `gh-review:991`.
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    human: Mapped[bool] = mapped_column(Boolean, default=True)
    learn: Mapped[bool] = mapped_column(Boolean, default=False)  # the human wrote `#learn`
    url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None]
    # new -> classified | ignored -> addressed (a proposal took it up)
    status: Mapped[str] = mapped_column(String(16), default="new", index=True)
    target: Mapped[str | None] = mapped_column(String(16))  # coder | reviewer | planner
    lesson_key: Mapped[str | None] = mapped_column(String(64), index=True)
    proposal_id: Mapped[str | None] = mapped_column(String(26))


class LearningProposal(Base):
    """One steering change the Learning agent proposed, and its eval gate."""

    __tablename__ = "learning_proposals"

    id: Mapped[str] = mapped_column(String(26), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(26), index=True)
    repo: Mapped[str] = mapped_column(String(16))  # target | codeit
    branch: Mapped[str] = mapped_column(String(128))
    pr_url: Mapped[str | None] = mapped_column(Text)
    patch_path: Mapped[str | None] = mapped_column(Text)  # when there is no PR
    changes_json: Mapped[dict[str, Any]]  # {"targets": [...], "lessons": [...]}
    gate_status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    gate_json: Mapped[dict[str, Any] | None]
    created_at: Mapped[datetime]
    gated_at: Mapped[datetime | None]


class McpToken(Base):
    """Per-run bearer tokens for the host-side jira-mcp server (PRD 15, ADR-0004).

    Only the SHA-256 of the token is stored; the plaintext exists once, in the run's
    `/run/mcp.json`.
    """

    __tablename__ = "mcp_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(32))
    ticket_key: Mapped[str | None] = mapped_column(String(32))
    run_id: Mapped[str] = mapped_column(String(26), index=True)
    created_at: Mapped[datetime]
    expires_at: Mapped[datetime] = mapped_column(index=True)
    revoked_at: Mapped[datetime | None]
