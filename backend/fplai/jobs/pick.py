"""Locking one gameweek's picks, before its deadline.

Kept apart from the backfill because the two answer different questions. The
backfill replays deadlines that have already gone, reading prices as they were
at the time; this runs against a deadline that has not arrived yet, so what it
reads is simply the current state of the world.
"""

from __future__ import annotations

import logging
import sqlite3

from ..data.repository import load_transfers
from ..model.projections import project_for_gameweek, save_projections
from ..strategy import best_xi, manager
from .backfill import resume_state

logger = logging.getLogger(__name__)


def lock_gameweek(
    connection: sqlite3.Connection,
    gameweek: int,
    *,
    horizon: int | None = None,
) -> dict[str, float]:
    """Project, then lock both models' picks for `gameweek`.

    One projection serves both models, which is the point: they differ in what
    they are allowed to do with it, not in what they believe. Locking a
    gameweek that is already locked is refused downstream, so running this
    twice changes nothing.
    """
    # A gameweek picked more than once before its deadline -- refining as team
    # news lands -- must not accumulate a transfer row per attempt. The picks
    # themselves are replaced in place by `save_locked_picks`; these are not.
    connection.execute("DELETE FROM transfers WHERE gameweek = ?", (gameweek,))
    connection.execute("DELETE FROM chips_used WHERE gameweek = ?", (gameweek,))

    projections = project_for_gameweek(connection, gameweek, horizon=horizon)
    save_projections(connection, gameweek, projections)

    selection = best_xi.lock_gameweek(connection, gameweek, projections=projections)

    state = resume_state(connection, gameweek)
    manager.lock_gameweek(connection, gameweek, state, projections=projections)

    transfers = len(load_transfers(connection, gameweek))
    logger.info(
        "GW%d locked: Best XI %.1f projected, Manager made %d transfer(s)",
        gameweek,
        selection.expected_points,
        transfers,
    )
    return {
        "best_xi_expected": selection.expected_points,
        "manager_transfers": transfers,
    }


__all__ = ["lock_gameweek"]
