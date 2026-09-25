from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from codeit import db
from codeit.db.models import Base, Lease

PRD_TABLES = {
    "runs",
    "events",
    "leases",
    "agent_instances",
    "tickets_cache",
    "budget_ledger",
    "backend_state",
    "eval_runs",
    "eval_results",
    "signals",
    "mcp_tokens",  # ADR-0006
}


def test_upgrade_creates_all_prd_tables(tmp_path: Path) -> None:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    names = set(inspect(db.make_engine(path)).get_table_names())
    assert names - {"alembic_version"} == PRD_TABLES


def test_upgrade_records_version_and_is_rerunnable(tmp_path: Path) -> None:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    db.upgrade(path)  # a second run must be a no-op, not "table already exists"
    with db.make_engine(path).connect() as conn:
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
    assert version == ScriptDirectory.from_config(db.alembic_config(path)).get_current_head()


def test_upgrade_repairs_database_without_version(tmp_path: Path) -> None:
    """Databases made before the env.py fix have 0001 tables but no alembic_version row."""
    path = tmp_path / "codeit.db"
    db.upgrade(path, "0001")
    with db.make_engine(path).begin() as conn:
        conn.execute(text("DELETE FROM alembic_version"))
    db.upgrade(path)
    names = set(inspect(db.make_engine(path)).get_table_names())
    assert "mcp_tokens" in names


def test_migrations_match_models(tmp_path: Path) -> None:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    with db.make_engine(path).connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations differ; add a migration: {diff}"


def test_engine_uses_wal_and_foreign_keys(tmp_path: Path) -> None:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    with db.make_engine(path).connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_lease_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "codeit.db"
    db.upgrade(path)
    now = datetime.now(UTC)
    with Session(db.make_engine(path)) as s:
        s.add(
            Lease(
                ticket_key="CODEIT-1",
                role="coder",
                instance="coder-1",
                run_id="01J0000000000000000000000A",
                acquired_at=now,
                expires_at=now,
                heartbeat_at=now,
            )
        )
        s.commit()
        got = s.get(Lease, "CODEIT-1")
        assert got is not None
        assert got.instance == "coder-1"
