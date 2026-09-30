from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

import pytest
from sqlalchemy import Engine

from codeit.backends.state import BackendStateStore
from codeit.config import Config
from codeit.orchestrator.budget import Budget, in_window, record_spend

NOON = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
NIGHT = datetime(2026, 9, 29, 23, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("windows", "now", "inside"),
    [
        ([], time(12), True),
        (["23:00-08:00"], time(23, 30), True),
        (["23:00-08:00"], time(7, 59), True),
        (["23:00-08:00"], time(8, 0), False),
        (["23:00-08:00"], time(12), False),
        (["09:00-12:00", "13:00-17:00"], time(12, 30), False),
        (["09:00-12:00", "13:00-17:00"], time(13, 0), True),
    ],
)
def test_in_window(windows: list[str], now: time, inside: bool) -> None:
    assert in_window(windows, now) is inside


def budget(cfg: Config, engine: Engine, now: datetime, **kw: bool) -> Budget:
    return Budget(cfg, engine, has_openrouter_key=kw.pop("key", True), clock=lambda: now, **kw)


def test_claude_window_concurrency_and_parking(cfg: Config, engine: Engine) -> None:
    assert budget(cfg, engine, NIGHT).can_run("coder").ok
    day = budget(cfg, engine, NOON).can_run("coder")
    assert not day.ok and "run window (23:00-08:00)" in day.reason
    assert budget(cfg, engine, NOON, any_time=True).can_run("coder").ok
    busy = budget(cfg, engine, NIGHT).can_run("coder", claude_running=1)
    assert not busy.ok and "concurrency" in busy.reason

    BackendStateStore(engine).park("claude_code", NIGHT + timedelta(hours=2), "usage limit")
    parked = budget(cfg, engine, NIGHT).can_run("coder")
    assert not parked.ok and "parked" in parked.reason
    assert budget(cfg, engine, NIGHT + timedelta(hours=3)).can_run("coder").ok  # reset passed


def test_reviewer_daily_cap(cfg: Config, engine: Engine) -> None:
    b = budget(cfg, engine, datetime.now(UTC))
    assert b.can_run("reviewer").ok
    record_spend(engine, backend="openrouter_paid_review", role="reviewer", usd=0.3, requests=1)
    record_spend(engine, backend="openrouter_paid_review", role="reviewer", usd=0.25, requests=1)
    assert b.spent_today("reviewer") == pytest.approx(0.55)
    capped = b.can_run("reviewer")
    assert not capped.ok and "$0.55 of $0.50" in capped.reason
    assert b.spent_today("docs") == 0
    assert budget(cfg, engine, datetime.now(UTC) + timedelta(days=1)).can_run("reviewer").ok


def test_reviewer_needs_a_key(cfg: Config, engine: Engine) -> None:
    decision = budget(cfg, engine, NOON, key=False).can_run("reviewer")
    assert not decision.ok and "OpenRouter key" in decision.reason


def test_free_request_cap(cfg: Config, engine: Engine) -> None:
    free_docs = cfg.routing["docs"].model_copy(update={"primary": "openrouter_free"})
    cfg = cfg.model_copy(update={"routing": {**cfg.routing, "docs": free_docs}})
    b = budget(cfg, engine, datetime.now(UTC))
    record_spend(engine, backend="openrouter_free", role="docs", usd=0, requests=900)
    assert b.free_requests_today() == 900
    assert not b.can_run("docs").ok  # docs routes to the free list first
    assert b.can_run("reviewer").ok


def test_status(cfg: Config, engine: Engine) -> None:
    record_spend(engine, backend="openrouter_paid_review", role="reviewer", usd=0.1, requests=2)
    status = budget(cfg, engine, datetime.now(UTC)).status()
    assert status["claude"]["window"] == ["23:00-08:00"] and status["claude"]["runs_today"] == 0
    assert status["openrouter"]["spend"]["reviewer"] == (pytest.approx(0.1), 0.5)
    assert status["openrouter"]["free_requests"] == (0, 900)


def test_claude_chat_roles_are_not_capped(cfg: Config, engine: Engine) -> None:
    record_spend(engine, backend="openrouter_free", role="docs", usd=5, requests=900)
    b = budget(cfg, engine, NOON, key=False)
    assert b.can_run("docs").ok and b.can_run("learning").ok  # Claude subscription roles
    assert not b.can_run("reviewer").ok  # the Reviewer still needs its OpenRouter key
