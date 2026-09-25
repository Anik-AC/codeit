"""Database access: engine creation (SQLite in WAL mode) and Alembic migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event, inspect, text

ALEMBIC_DIR = Path(__file__).parent / "alembic"


def sqlite_url(db_path: Path) -> str:
    return f"sqlite:///{db_path}"


def make_engine(db_path: Path) -> Engine:
    """Create an engine with WAL, foreign keys and a busy timeout on every connection."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(sqlite_url(db_path))

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn: Any, _record: Any) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def alembic_config(db_path: Path) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", sqlite_url(db_path))
    return cfg


# Tables that first appear in each revision, newest first. Used only to repair databases
# created before `alembic_version` was committed (fixed in M2, ADR-0006).
_FIRST_TABLE = (("0002", "mcp_tokens"), ("0001", "runs"))


def _repair_unversioned(db_path: Path) -> None:
    """Stamp a database that has tables but no recorded revision, so upgrade can proceed."""
    if not db_path.exists():
        return
    engine = make_engine(db_path)
    try:
        with engine.connect() as conn:
            tables = set(inspect(conn).get_table_names())
            if (
                "alembic_version" in tables
                and conn.execute(text("SELECT count(*) FROM alembic_version")).scalar()
            ):
                return
    finally:
        engine.dispose()
    for revision, table in _FIRST_TABLE:
        if table in tables:
            command.stamp(alembic_config(db_path), revision)
            return


def upgrade(db_path: Path, revision: str = "head") -> None:
    """Apply migrations up to `revision`, creating the database file if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _repair_unversioned(db_path)
    command.upgrade(alembic_config(db_path), revision)
