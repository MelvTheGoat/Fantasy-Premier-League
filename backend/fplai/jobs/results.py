"""Pulling the real points for gameweeks that have already been played.

Every other job that touches results looks at one gameweek: the live job polls
the one being played, and `finalise_gameweek` closes the one that just ended.
Neither helps a database that arrives with a season already in progress -- a
fresh deployment, or one that was switched off for a month -- and without this
such a database scores every stored squad against no results at all, which
comes out as a season of zeros rather than as an error.

Nothing here decides anything. It is the actual points as FPL reports them,
which is the only thing this project ever scores with.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from ..data.client import FPLClient
from ..data.ingest import ingest_fixtures, ingest_live_gameweek
from ..data.repository import gameweeks_underway
from .refresh import LIVE_CACHE_TTL

logger = logging.getLogger(__name__)


def missing_results(
    connection: sqlite3.Connection, now: datetime | None = None
) -> list[int]:
    """Gameweeks that have been played but have no stored results."""
    played = gameweeks_underway(connection, now or datetime.now(UTC))
    stored = {
        row["gameweek"]
        for row in connection.execute(
            "SELECT DISTINCT gameweek FROM player_gameweek_stats"
        )
    }
    return [gameweek for gameweek in played if gameweek not in stored]


def refresh_results(
    connection: sqlite3.Connection,
    client: FPLClient,
    *,
    gameweeks: list[int] | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """Ingest real points for each gameweek given, or each one still missing.

    Fixtures come along too, because whether a fixture has finished is what
    tells the automatic substitutions they are allowed to act.
    """
    targets = gameweeks if gameweeks is not None else missing_results(connection, now)
    if not targets:
        return {"gameweeks": 0, "rows": 0}

    ingest_fixtures(connection, client.fixtures(ttl_seconds=LIVE_CACHE_TTL))

    rows = 0
    for gameweek in targets:
        rows += ingest_live_gameweek(
            connection, gameweek, client.event_live(gameweek, ttl_seconds=LIVE_CACHE_TTL)
        )

    logger.info("results for GW%s: %d rows", targets, rows)
    return {"gameweeks": len(targets), "rows": rows}


__all__ = ["missing_results", "refresh_results"]
