"""Filling an empty database, so a fresh deployment comes up with a season.

A new container starts with nothing, and the site has nothing to show until
the season so far has been pulled down and replayed. Rather than making that a
manual step someone has to remember, it is a job like any other: the scheduler
notices an empty database and runs this.

The stages are ordered by what depends on what, and each is skipped when its
work is already done, so an interrupted seed picks up where it stopped rather
than starting again. That matters because the middle stage takes minutes.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from typing import Any

from ..data.client import FPLClient
from ..data.repository import gameweeks_underway
from .backfill import backfill
from .refresh import refresh_player_histories, refresh_reference
from .score import score_season

logger = logging.getLogger(__name__)

#: The stages in order, with the question each one answers about the database.
#: `setup_progress` walks these to say where a seed has got to, which is what
#: the site shows instead of an empty pitch while it waits.
STAGES: tuple[tuple[str, str], ...] = (
    ("reference", "Downloading players, teams and fixtures"),
    ("history", "Reading every player's price history"),
    ("backfill", "Replaying the season so far"),
    ("ready", "Ready"),
)


def _counts(connection: sqlite3.Connection) -> dict[str, int]:
    def count(table: str) -> int:
        return connection.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]

    return {
        "players": count("players"),
        "seasons": count("player_season_history"),
        "gameweeks": connection.execute(
            "SELECT COUNT(DISTINCT gameweek) c FROM locked_picks"
        ).fetchone()["c"],
    }


def current_stage(
    connection: sqlite3.Connection, now: datetime | None = None
) -> str:
    """Which stage a seed is up to, derived from the database itself.

    Deliberately not a stored flag: a flag can disagree with reality after a
    crash, and then the site lies about what it is doing. Counting the rows
    that each stage produces cannot.
    """
    counts = _counts(connection)
    if not counts["players"]:
        return "reference"
    # Locked picks are proof the whole pipeline ran, so they settle the
    # question before any earlier stage's marker is consulted.
    if counts["gameweeks"]:
        return "ready"
    if not counts["seasons"]:
        return "history"
    # Before the season's first deadline there is nothing to replay, so an
    # empty picks table is the finished state rather than an unstarted one.
    if gameweeks_underway(connection, now or datetime.now(UTC)):
        return "backfill"
    return "ready"


def setup_progress(
    connection: sqlite3.Connection, now: datetime | None = None
) -> dict[str, Any]:
    """What the frontend shows while a fresh deployment fills itself in."""
    stage = current_stage(connection, now)
    labels = dict(STAGES)
    names = [name for name, _ in STAGES]
    return {
        "ready": stage == "ready",
        "stage": stage,
        "message": labels[stage],
        "step": names.index(stage) + 1,
        "steps": len(names) - 1,
        **_counts(connection),
    }


def seed(
    connection: sqlite3.Connection,
    client: FPLClient,
    *,
    history_limit: int | None = None,
) -> dict[str, Any]:
    """Take an empty database to a working site.

    The same four commands the README gives for a manual start, in the same
    order, because there is only one correct order: prices before projections,
    projections before picks, picks before scores.
    """
    logger.info("seeding an empty database")

    if current_stage(connection) == "reference":
        refresh_reference(connection, client)

    if current_stage(connection) == "history":
        elements = None
        if history_limit:
            elements = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM players ORDER BY id LIMIT ?", (history_limit,)
                )
            ]
        # The slow one: a request per player, paced so the API is not hammered.
        refresh_player_histories(connection, client, elements=elements)

    if current_stage(connection) == "backfill":
        backfill(connection)
        score_season(connection)

    progress = setup_progress(connection)
    logger.info("seed finished at stage %s: %s", progress["stage"], progress)
    return progress


__all__ = ["STAGES", "current_stage", "seed", "setup_progress"]
