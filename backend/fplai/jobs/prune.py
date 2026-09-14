"""Keeping the stored season small enough to live in the repository.

Where the database is a file that gets committed after every run, its size is
not a detail -- it is multiplied by every commit for the rest of the season.

Only one table grows without bound, and most of it is never read back. The
Manager looks several gameweeks ahead to judge whether a hit pays for itself,
so each deadline writes a projection per player per gameweek in the horizon.
Once that deadline has passed, the decision is made and stored; the only
projection anyone looks at again is the one for the gameweek itself, behind a
tap on a player. The rest is working-out, and it is roughly four fifths of the
database.

Nothing here touches a decision. Picks, transfers, chips, scores and the
gameweek's own projection all stay exactly as they were.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)


def prunable(connection: sqlite3.Connection, before_gameweek: int) -> int:
    """How many lookahead rows are finished with, without deleting them."""
    return connection.execute(
        "SELECT COUNT(*) c FROM projections"
        " WHERE made_for_gameweek < ? AND target_gameweek != made_for_gameweek",
        (before_gameweek,),
    ).fetchone()["c"]


def prune_projections(
    connection: sqlite3.Connection,
    before_gameweek: int,
    *,
    vacuum: bool = True,
) -> dict[str, Any]:
    """Drop the lookahead projections made for gameweeks already played.

    `before_gameweek` is left alone along with everything after it, so the
    gameweek being decided right now keeps the horizon it needs.
    """
    doomed = prunable(connection, before_gameweek)
    connection.execute(
        "DELETE FROM projections"
        " WHERE made_for_gameweek < ? AND target_gameweek != made_for_gameweek",
        (before_gameweek,),
    )

    if vacuum and doomed:
        # Without this the rows go but the file does not shrink, and the file
        # is the thing being committed.
        connection.execute("VACUUM")

    logger.info("pruned %d lookahead projections before GW%d", doomed, before_gameweek)
    return {"deleted": doomed}


__all__ = ["prunable", "prune_projections"]
