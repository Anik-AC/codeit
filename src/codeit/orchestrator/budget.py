"""Budget guard (PRD 9.4): may a role start a run now?

- Claude (coder, rebase): not parked, inside `claude.run_window`, and fewer than
  `claude.max_concurrent_runs` Claude runs going.
- OpenRouter (reviewer and chat roles): a key is set, the role's USD spent today is under
  `openrouter.daily_usd_cap`, and free-model requests are under their daily cap.

Spend is written to `budget_ledger` when a run ends. "Today" is the local day, like the
run window.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from codeit.backends.state import BackendStateStore
from codeit.config import Config, Role
from codeit.db.models import BudgetLedger, Run

CLAUDE_ROLES = frozenset({"coder", "rebase"})
CLAUDE_BACKEND = "claude_code"
FREE_BACKEND = "openrouter_free"

Clock = Callable[[], datetime]


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _parse(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def in_window(windows: Sequence[str], now: time) -> bool:
    """True if `now` falls in any 'HH:MM-HH:MM' window; windows may cross midnight.
    No windows means any time."""
    if not windows:
        return True
    for w in windows:
        start, end = (_parse(p) for p in w.split("-"))
        if start <= end:
            if start <= now < end:
                return True
        elif now >= start or now < end:
            return True
    return False


def record_spend(
    engine: Engine,
    *,
    backend: str,
    role: str,
    usd: float | None,
    requests: int,
    note: str | None = None,
) -> None:
    with Session(engine) as s, s.begin():
        s.add(
            BudgetLedger(
                ts=datetime.now(UTC),
                backend=backend,
                role=role,
                key_name="OPENROUTER_API_KEY",
                usd=usd or 0.0,
                requests=requests,
                note=note,
            )
        )


@dataclass(frozen=True)
class Decision:
    ok: bool
    reason: str = ""


class Budget:
    def __init__(
        self,
        cfg: Config,
        engine: Engine,
        *,
        has_openrouter_key: bool,
        any_time: bool = False,
        clock: Clock = _local_now,
    ) -> None:
        self.cfg = cfg
        self._engine = engine
        self._has_key = has_openrouter_key
        self.any_time = any_time
        self._clock = clock
        self._backends = BackendStateStore(engine)

    def _day_start(self) -> datetime:
        now = self._clock()
        return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)

    def spent_today(self, role: str) -> float:
        with Session(self._engine) as s:
            total = s.scalar(
                select(func.coalesce(func.sum(BudgetLedger.usd), 0.0)).where(
                    BudgetLedger.role == role, BudgetLedger.ts >= self._day_start()
                )
            )
        return float(total or 0.0)

    def free_requests_today(self) -> int:
        with Session(self._engine) as s:
            total = s.scalar(
                select(func.coalesce(func.sum(BudgetLedger.requests), 0)).where(
                    BudgetLedger.backend == FREE_BACKEND, BudgetLedger.ts >= self._day_start()
                )
            )
        return int(total or 0)

    def in_claude_window(self) -> bool:
        return self.any_time or in_window(self.cfg.claude.run_window, self._clock().time())

    def can_run(self, role: Role, *, claude_running: int = 0) -> Decision:
        if role in CLAUDE_ROLES:
            state = self._backends.get(CLAUDE_BACKEND, self._clock().astimezone(UTC))
            if state.state != "ok":
                until = f" until {state.until:%H:%M}" if state.until else ""
                return Decision(False, f"Claude is {state.state}{until}")
            if not self.in_claude_window():
                windows = ", ".join(self.cfg.claude.run_window)
                return Decision(False, f"outside the Claude run window ({windows})")
            if claude_running >= self.cfg.claude.max_concurrent_runs:
                return Decision(False, "Claude concurrency limit reached")
            return Decision(True)
        route = self.cfg.routing.get(role)
        if route is not None and route.primary == "claude_code_chat":
            # Short chat calls on the subscription; if Claude is usage-limited the route
            # falls back to the free OpenRouter list, so there is nothing to refuse here.
            return Decision(True)
        if not self._has_key:
            return Decision(False, "no OpenRouter key in .env")
        cap = self.cfg.openrouter.daily_usd_cap.get(role)
        if cap is not None:
            spent = self.spent_today(role)
            if spent >= cap:
                return Decision(False, f"{role} spent ${spent:.2f} of ${cap:.2f} today")
        if route is not None and route.primary == FREE_BACKEND:
            used = self.free_requests_today()
            if used >= self.cfg.openrouter.free_requests_daily_cap:
                return Decision(False, f"free-model requests used up ({used} today)")
        return Decision(True)

    def status(self) -> dict[str, Any]:
        """For `codeit budget`."""
        now = self._clock()
        claude = self._backends.get(CLAUDE_BACKEND, now.astimezone(UTC))
        roles = sorted(set(self.cfg.openrouter.daily_usd_cap))
        eval_spend = self.spent_today("eval")
        return {
            "claude": {
                "state": claude.state,
                "parked_until": claude.until,
                "window": self.cfg.claude.run_window or ["any time"],
                "in_window": self.in_claude_window(),
                "runs_today": self._claude_runs_today(),
            },
            "openrouter": {
                "key": self._has_key,
                "spend": {
                    r: (self.spent_today(r), self.cfg.openrouter.daily_usd_cap[r]) for r in roles
                },
                "eval_spend": eval_spend,
                "free_requests": (
                    self.free_requests_today(),
                    self.cfg.openrouter.free_requests_daily_cap,
                ),
            },
        }

    def _claude_runs_today(self) -> int:
        with Session(self._engine) as s:
            return int(
                s.scalar(
                    select(func.count())
                    .select_from(Run)
                    .where(Run.backend == CLAUDE_BACKEND, Run.started_at >= self._day_start())
                )
                or 0
            )
