"""Database access: engine creation (SQLite in WAL mode) and Alembic migrations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event

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


def upgrade(db_path: Path, revision: str = "head") -> None:
    """Apply migrations up to `revision`, creating the database file if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(db_path), revision)
