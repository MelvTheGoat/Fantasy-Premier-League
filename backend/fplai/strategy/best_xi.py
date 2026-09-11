"""Model B: Best XI of the Week.

Rebuilds the best legal squad from scratch every gameweek, as though playing a
Free Hit every week. No transfers, no hits, no chips, and no memory of last
week's squad.

The constraints still apply -- £100m, 2/5/5/3, three per club -- and they are
the whole point. Without them the answer is just the fifteen most expensive
players, which would say nothing about anything. With them, the gap between
this model and the Manager is the cost of continuity: what it is worth, in
points, to be free of the squad you already own.
"""

from __future__ import annotations

import logging
import sqlite3

from ..data.repository import load_roster_at_gameweek, save_locked_picks
from ..model.projections import project_for_gameweek
from ..model.xpts import GameweekProjection
from ..optimise.squad import SquadSelection, optimise_squad
from ..rules.constants import STARTING_BUDGET
from .explain import load_contexts, selection_reason

logger = logging.getLogger(__name__)

MODEL_ID = "best_xi"


def team_names(connection: sqlite3.Connection) -> dict[int, str]:
    return {
        row["id"]: row["short_name"]
        for row in connection.execute("SELECT id, short_name FROM teams")
    }


def pick_gameweek(
    connection: sqlite3.Connection,
    gameweek: int,
    *,
    projections: dict[int, list[GameweekProjection]] | None = None,
    budget: int = STARTING_BUDGET,
) -> tuple[SquadSelection, dict[int, str]]:
    """Choose the best squad for one gameweek, with a reason for every player.

    Returns the selection and the per-player explanations, which are stored
    alongside the picks and revealed in the frontend when a player is tapped.
    """
    if projections is None:
        projections = project_for_gameweek(connection, gameweek, horizon=4)

    roster = load_roster_at_gameweek(connection, gameweek)
    points = {
        element: gameweeks[0].expected_points
        for element, gameweeks in projections.items()
        if gameweeks
    }

    # Players whose club has no fixture cannot contribute, so they are left out
    # rather than relied on to be substituted.
    selection = optimise_squad(roster, points, budget=budget)

    contexts = load_contexts(connection, gameweek)
    names = team_names(connection)
    reasons = {
        element: selection_reason(
            contexts[element],
            projections[element][0],
            names,
            is_captain=element == selection.lineup.captain,
        )
        for element in selection.squad.elements
        if element in contexts and projections.get(element)
    }

    return selection, reasons


def lock_gameweek(
    connection: sqlite3.Connection,
    gameweek: int,
    *,
    projections: dict[int, list[GameweekProjection]] | None = None,
) -> SquadSelection:
    """Pick and permanently store a gameweek's squad.

    Storing is what makes the result meaningful: once written, the picks are
    never regenerated, so they cannot quietly improve with hindsight.
    """
    selection, reasons = pick_gameweek(connection, gameweek, projections=projections)
    roster = load_roster_at_gameweek(connection, gameweek)

    save_locked_picks(
        connection,
        MODEL_ID,
        gameweek,
        selection.lineup,
        selection.squad,
        prices={e: p.price for e, p in roster.items()},
        reasons=reasons,
    )
    logger.info(
        "locked Best XI for GW%d: %.1f xPts, £%.1fm spent",
        gameweek,
        selection.expected_points,
        selection.total_cost / 10,
    )
    return selection


__all__ = ["MODEL_ID", "lock_gameweek", "pick_gameweek", "team_names"]
