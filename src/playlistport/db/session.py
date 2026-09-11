"""SQLite engine and session handling."""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import load_config
from .models import Base

_engine = None
_Session: sessionmaker[Session] | None = None


def init_db():
    """Create the engine and schema on first use."""
    global _engine, _Session
    if _engine is not None:
        return _engine

    config = load_config()
    path = config.data_dir / "playlistport.db"
    _engine = create_engine(f"sqlite:///{path}", future=True)

    @event.listens_for(_engine, "connect")
    def _pragmas(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        # WAL survives an abrupt kill mid-transfer without corrupting the file,
        # which is the whole point of persisting per-track progress.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(_engine)
    _ensure_columns(_engine)
    _Session = sessionmaker(bind=_engine, future=True, expire_on_commit=False)
    return _engine


#: Columns added after the first release. `create_all` only creates missing
#: *tables*, so an existing database silently keeps the old shape — which would
#: surface as an OperationalError on the first query, not at startup.
_ADDED_COLUMNS = {
    "match_cache": {"no_match": "BOOLEAN NOT NULL DEFAULT 0"},
    "transfer_items": {"source_isrc": "VARCHAR(24)"},
}


def _ensure_columns(engine) -> None:
    """Apply additive schema changes to an existing database.

    Deliberately minimal: a full migration tool is not warranted for a
    single-user local database, but silently running against a stale schema is
    not acceptable either.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in _ADDED_COLUMNS.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


@contextmanager
def get_session() -> Iterator[Session]:
    init_db()
    assert _Session is not None
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
