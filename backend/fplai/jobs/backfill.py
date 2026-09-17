"""Replaying the season so far, one deadline at a time.

The season is already under way, so both models have to be reconstructed from
GW1. The only thing that makes a reconstruction worth anything is that each
gameweek is decided using *only* what was known before its deadline, so this
walks forward one gameweek at a time and never looks ahead:

    for each gameweek:
        project using data from earlier gameweeks only
        pick both models' squads
        lock them permanently
        only then move on

`save_locked_picks` refuses to overwrite, so a backfill that is interrupted and
restarted resumes rather than rewriting what it already decided. That is also
what stops a later model change from silently improving the past.
"""

from __future__ import annotations

import logging
import sqlite3

from ..config import settings
from ..data.repository import (
    load_chips_used,
    load_locked_squad,
    load_manager_state,
    picks_are_locked,
)
from ..model.projections import project_for_gameweek, save_projections
from ..rules.constants import INITIAL_FREE_TRANSFERS
from ..strategy import best_xi, manager
from ..strategy.manager import ManagerState

logger = logging.getLogger(__name__)


def resume_state(connection: sqlite3.Connection, gameweek: int) -> ManagerState:
    """Rebuild the Manager's carried state from the last locked gameweek.

    Used when a backfill resumes: the squad, bank and free transfers all come
    from what was stored, not from a fresh run, so resuming mid-season produces
    exactly the same result as running straight through.
    """
    for previous in range(gameweek - 1, 0, -1):
        squad = load_locked_squad(connection, manager.MODEL_ID, previous)
        if squad is None:
            continue
        state = load_manager_state(connection, previous)
        return ManagerState(
            squad=squad,
            free_transfers=(
                state["free_transfers_after"] if state else INITIAL_FREE_TRANSFERS
            ),
            chips_used=load_chips_used(connection),
        )
    return ManagerState(chips_used=load_chips_used(connection))


def playable_gameweeks(connection: sqlite3.Connection) -> list[int]:
    """Gameweeks that have a deadline in the past and fixtures to play.

    A gameweek is only backfilled once its deadline has passed: picking for a
    deadline that has not arrived is the live job's work, not the backfill's.
    """
    return [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM gameweeks"
            " WHERE deadline_time <= datetime('now')"
            "   AND EXISTS (SELECT 1 FROM fixtures WHERE fixtures.gameweek = gameweeks.id)"
            " ORDER BY id"
        )
    ]


def backfill(
    connection: sqlite3.Connection,
    *,
    through_gameweek: int | None = None,
    horizon: int | None = None,
    store_projections: bool = True,
) -> dict[int, dict]:
    """Replay every gameweek whose deadline has passed.

    Returns a summary per gameweek, for reporting. Gameweeks already locked are
    skipped rather than redone -- that refusal is the no-leakage guarantee, not
    an optimisation.
    """
    horizon = horizon or settings.planning_horizon
    gameweeks = playable_gameweeks(connection)
    if through_gameweek is not None:
        gameweeks = [gw for gw in gameweeks if gw <= through_gameweek]

    if not gameweeks:
        logger.warning("no gameweeks are ready to backfill")
        return {}

    summary: dict[int, dict] = {}
    state = resume_state(connection, gameweeks[0])

    for gameweek in gameweeks:
        manager_locked = picks_are_locked(connection, manager.MODEL_ID, gameweek)
        best_xi_locked = picks_are_locked(connection, best_xi.MODEL_ID, gameweek)

        if manager_locked and best_xi_locked:
            logger.info("GW%d already locked; skipping", gameweek)
            state = resume_state(connection, gameweek + 1)
            summary[gameweek] = {"skipped": True}
            continue

        # A replay must not see today's injury list: those fields have no
        # history, so using them would be avoiding players who got hurt
        # weeks after the deadline being replayed.
        projections = project_for_gameweek(
            connection, gameweek, horizon=horizon, team_news=False
        )
        if store_projections:
            save_projections(connection, gameweek, projections)

        entry: dict = {"skipped": False}

        if not best_xi_locked:
            selection = best_xi.lock_gameweek(
                connection, gameweek, projections=projections
            )
            entry["best_xi_expected"] = round(selection.expected_points, 1)

        if not manager_locked:
            state = manager.lock_gameweek(
                connection, gameweek, state, projections=projections
            )
            manager_state = load_manager_state(connection, gameweek)
            entry["transfers"] = manager_state["transfers_made"] if manager_state else 0
            entry["transfer_cost"] = (
                manager_state["transfer_cost"] if manager_state else 0
            )
            entry["chip"] = manager_state["chip"] if manager_state else None
        else:
            state = resume_state(connection, gameweek + 1)

        summary[gameweek] = entry
        logger.info("backfilled GW%d: %s", gameweek, entry)

    return summary


__all__ = ["backfill", "playable_gameweeks", "resume_state"]
