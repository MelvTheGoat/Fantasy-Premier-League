"""SQLite access.

Deliberately thin: the rules engine and the models take plain dataclasses, so
this only has to turn rows into those and back. Connections use row factories
so callers can read columns by name.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from ..config import settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def utcnow() -> str:
    """Timestamps are stored as ISO-8601 UTC so they sort as text."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    target = Path(path) if path is not None else settings.database_path
    if str(target) != ":memory:":
        target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(connection: sqlite3.Connection) -> None:
    """Apply the schema. Safe to run against an existing database."""
    connection.executescript(SCHEMA_PATH.read_text())


@contextmanager
def transaction(connection: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block atomically, rolling back if it raises.

    Ingestion writes thousands of rows per run; a half-applied refresh would
    leave projections keyed to prices that were never fully written.
    """
    connection.execute("BEGIN")
    try:
        yield connection
    except Exception:
        connection.execute("ROLLBACK")
        raise
    else:
        connection.execute("COMMIT")


def log_ingest(
    connection: sqlite3.Connection,
    endpoint: str,
    status: str,
    *,
    gameweek: int | None = None,
    rows: int | None = None,
    message: str | None = None,
) -> None:
    connection.execute(
        "INSERT INTO ingest_log (endpoint, gameweek, status, rows, message, ran_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (endpoint, gameweek, status, rows, message, utcnow()),
    )


__all__ = ["SCHEMA_PATH", "connect", "init_db", "log_ingest", "transaction", "utcnow"]
