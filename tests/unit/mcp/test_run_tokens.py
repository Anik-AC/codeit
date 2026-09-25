from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from codeit import db
from codeit.config import load_config
from codeit.db.models import McpToken
from codeit.run_tokens import RunTokenStore, token_ttl
from tests.conftest import REPO_ROOT


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def tokens(tmp_path: Path, clock: Clock) -> RunTokenStore:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    return RunTokenStore(db.make_engine(path), clock=clock)


def test_mint_and_verify(tokens: RunTokenStore) -> None:
    token = tokens.mint("coder", "RUN1", "CODEIT-5", timedelta(minutes=65))
    grant = tokens.verify(token)
    assert grant is not None
    assert (grant.role, grant.ticket_key, grant.run_id) == ("coder", "CODEIT-5", "RUN1")
    assert grant.expires_at == datetime(2026, 9, 25, 13, 5, tzinfo=UTC)


def test_tokens_are_unique_and_long(tokens: RunTokenStore) -> None:
    a = tokens.mint("coder", "R", "CODEIT-1", timedelta(minutes=1))
    b = tokens.mint("coder", "R", "CODEIT-1", timedelta(minutes=1))
    assert a != b
    assert len(a) >= 43  # 32 random bytes, URL-safe base64


def test_only_hash_is_stored(tokens: RunTokenStore, tmp_path: Path) -> None:
    token = tokens.mint("coder", "R", "CODEIT-1", timedelta(minutes=1))
    with Session(db.make_engine(tmp_path / "codeit.db")) as s:
        hashes = list(s.scalars(select(McpToken.token_hash)))
    assert hashes == [hashlib.sha256(token.encode()).hexdigest()]
    assert token.encode() not in (tmp_path / "codeit.db").read_bytes()


def test_unknown_token(tokens: RunTokenStore) -> None:
    assert tokens.verify("nope") is None


def test_expired_token(tokens: RunTokenStore, clock: Clock) -> None:
    token = tokens.mint("coder", "R", "CODEIT-1", timedelta(minutes=10))
    clock.now += timedelta(minutes=10)
    assert tokens.verify(token) is None


def test_revoke_run(tokens: RunTokenStore) -> None:
    a = tokens.mint("coder", "R1", "CODEIT-1", timedelta(minutes=10))
    b = tokens.mint("rebase", "R1", "CODEIT-1", timedelta(minutes=10))
    other = tokens.mint("coder", "R2", "CODEIT-2", timedelta(minutes=10))
    assert tokens.revoke_run("R1") == 2
    assert tokens.revoke_run("R1") == 0
    assert tokens.verify(a) is None and tokens.verify(b) is None
    assert tokens.verify(other) is not None


def test_purge(tokens: RunTokenStore, clock: Clock) -> None:
    tokens.mint("coder", "R", "CODEIT-1", timedelta(minutes=1))
    clock.now += timedelta(days=2)
    fresh = tokens.mint("coder", "R2", "CODEIT-1", timedelta(minutes=1))
    assert tokens.purge() == 1
    assert tokens.verify(fresh) is not None


@pytest.mark.parametrize("role", ["human", "admin", ""])
def test_mint_rejects_roles_without_tokens(tokens: RunTokenStore, role: str) -> None:
    with pytest.raises(ValueError, match="cannot hold a run token"):
        tokens.mint(role, "R", None, timedelta(minutes=1))


def test_mint_rejects_non_positive_ttl(tokens: RunTokenStore) -> None:
    with pytest.raises(ValueError, match="ttl"):
        tokens.mint("coder", "R", None, timedelta(0))


def test_token_ttl_uses_role_timeout_plus_grace() -> None:
    cfg = load_config(REPO_ROOT / "config" / "config.yaml")
    assert token_ttl(cfg, "coder") == timedelta(minutes=65)
    assert token_ttl(cfg, "rebase") == timedelta(minutes=25)
    assert token_ttl(cfg, "planner") == timedelta(minutes=65)  # no sandbox timeout: default
