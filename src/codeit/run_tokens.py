"""Per-run bearer tokens for the host-side jira-mcp server (PRD 15, ADR-0004).

The orchestrator mints a token when it launches a run that needs Jira. The token is bound
to one role, one ticket and one run, expires with the run's timeout plus a grace period,
and is revoked when the run ends. Only its SHA-256 is stored, in `mcp_tokens`.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, update
from sqlalchemy.orm import Session

from codeit.config import Config
from codeit.db.models import McpToken

# Roles that may hold a run token. `human` uses stdio on the host and needs none.
TOKEN_ROLES = frozenset({"planner", "coder", "reviewer", "rebase", "docs", "learning"})
DEFAULT_RUN_MINUTES = 60

Clock = Callable[[], datetime]


def _now() -> datetime:
    return datetime.now(UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes even for timezone-aware columns; they were stored as UTC.
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Grant:
    """What a valid token allows: one role, optionally bound to one ticket, until expiry."""

    role: str
    ticket_key: str | None
    run_id: str
    expires_at: datetime


def token_ttl(cfg: Config, role: str) -> timedelta:
    """The role's sandbox wall-clock timeout plus the configured grace period."""
    minutes = cfg.sandbox.timeouts_minutes.get(role, DEFAULT_RUN_MINUTES)  # type: ignore[call-overload]
    return timedelta(minutes=minutes + cfg.mcp.token_grace_minutes)


class RunTokenStore:
    def __init__(self, engine: Engine, clock: Clock = _now) -> None:
        self._engine = engine
        self._clock = clock

    def mint(self, role: str, run_id: str, ticket_key: str | None, ttl: timedelta) -> str:
        """Create a token and return its plaintext. The plaintext is not stored."""
        if role not in TOKEN_ROLES:
            raise ValueError(
                f"role {role!r} cannot hold a run token; expected {sorted(TOKEN_ROLES)}"
            )
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        token = secrets.token_urlsafe(32)
        now = self._clock()
        with Session(self._engine) as s, s.begin():
            s.add(
                McpToken(
                    token_hash=_hash(token),
                    role=role,
                    ticket_key=ticket_key,
                    run_id=run_id,
                    created_at=now,
                    expires_at=now + ttl,
                    revoked_at=None,
                )
            )
        return token

    def verify(self, token: str) -> Grant | None:
        """The token's grant, or None if it is unknown, expired or revoked."""
        with Session(self._engine) as s:
            row = s.get(McpToken, _hash(token))
            if row is None or row.revoked_at is not None:
                return None
            expires_at = _aware(row.expires_at)
            if expires_at <= self._clock():
                return None
            return Grant(row.role, row.ticket_key, row.run_id, expires_at)

    def revoke_run(self, run_id: str) -> int:
        """Revoke every token of a run. Returns how many were active."""
        with Session(self._engine) as s, s.begin():
            result = s.execute(
                update(McpToken)
                .where(McpToken.run_id == run_id, McpToken.revoked_at.is_(None))
                .values(revoked_at=self._clock())
            )
            return int(result.rowcount)  # type: ignore[attr-defined]

    def purge(self, older_than: timedelta = timedelta(days=1)) -> int:
        """Delete tokens that expired more than `older_than` ago."""
        cutoff = self._clock() - older_than
        with Session(self._engine) as s, s.begin():
            result = s.execute(delete(McpToken).where(McpToken.expires_at < cutoff))
            return int(result.rowcount)  # type: ignore[attr-defined]
