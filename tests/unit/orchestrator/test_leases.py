from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from codeit import db
from codeit.orchestrator.leases import LeaseStore

TTL = timedelta(minutes=10)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 9, 25, 12, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def leases(tmp_path: Path, clock: Clock) -> LeaseStore:
    db.upgrade(tmp_path / "c.db")
    return LeaseStore(db.make_engine(tmp_path / "c.db"), clock=clock)


def test_one_holder_at_a_time(leases: LeaseStore) -> None:
    assert leases.acquire("K-1", "coder", "coder-1", "R1", TTL)
    assert not leases.acquire("K-1", "coder", "coder-2", "R2", TTL)
    assert leases.acquire("K-2", "coder", "coder-2", "R2", TTL)  # other tickets are free
    holder = leases.holder("K-1")
    assert holder is not None and (holder.instance, holder.run_id) == ("coder-1", "R1")


def test_expired_lease_can_be_taken(leases: LeaseStore, clock: Clock) -> None:
    leases.acquire("K-1", "coder", "coder-1", "R1", TTL)
    clock.now += TTL
    assert leases.holder("K-1") is None
    assert leases.acquire("K-1", "coder", "coder-2", "R2", TTL)
    holder = leases.holder("K-1")
    assert holder is not None and holder.run_id == "R2"


def test_heartbeat_extends(leases: LeaseStore, clock: Clock) -> None:
    leases.acquire("K-1", "coder", "coder-1", "R1", TTL)
    clock.now += timedelta(minutes=8)
    assert leases.heartbeat("K-1", "R1", TTL)
    clock.now += timedelta(minutes=8)
    assert not leases.acquire("K-1", "coder", "coder-2", "R2", TTL)  # still held
    assert not leases.heartbeat("K-1", "R-other", TTL)


def test_release_only_by_holder(leases: LeaseStore) -> None:
    leases.acquire("K-1", "coder", "coder-1", "R1", TTL)
    leases.release("K-1", "R2")
    assert leases.holder("K-1") is not None
    leases.release("K-1", "R1")
    assert leases.holder("K-1") is None


def test_dead_leases(leases: LeaseStore, clock: Clock) -> None:
    long = timedelta(hours=2)
    leases.acquire("K-1", "coder", "coder-1", "R1", long)  # heartbeats
    leases.acquire("K-2", "coder", "coder-2", "R2", long)  # its process died
    leases.acquire("K-3", "reviewer", "reviewer-1", "R3", timedelta(minutes=2))  # expires
    clock.now += timedelta(minutes=2)
    leases.heartbeat("K-1", "R1", long)
    clock.now += timedelta(minutes=2)
    assert {lease.ticket_key for lease in leases.dead()} == {"K-2", "K-3"}
    assert [lease.ticket_key for lease in leases.all()] == ["K-1", "K-2", "K-3"]


async def test_keep_alive_heartbeats(leases: LeaseStore, clock: Clock) -> None:
    leases.acquire("K-1", "coder", "coder-1", "R1", TTL)
    async with leases.keep_alive("K-1", "R1", TTL, every_s=0.01):
        clock.now += timedelta(minutes=5)
        await asyncio.sleep(0.05)
    holder = leases.holder("K-1")
    assert holder is not None and holder.heartbeat_at.replace(tzinfo=UTC) == clock.now
