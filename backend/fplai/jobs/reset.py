"""Discarding every decision the models have made, so a season can be replayed.

Normally this is exactly what must not happen. `save_locked_picks` refuses to
overwrite a stored gameweek precisely so that a past decision cannot quietly
improve with hindsight, and that refusal is the guarantee the whole project
rests on.

This is the deliberate exception, and it is narrow. It clears what the *models*
decided -- picks, transfers, chips, carried state, scores, and the projections
behind them -- and touches nothing that was *observed*: players, fixtures,
prices per gameweek, real results and prior seasons all stay. Replaying from
gameweek one then re-derives every decision from the same time-boxed data the
original run had, so the no-leakage guarantee survives a reset intact.

What it does not survive is honesty about what happened: a replayed season is
a different season from the one that was published, decided on a later date by
a newer model. That is a thing to say out loud, not to paper over.
"""

from __future__ import annotations

import logging
import sqlite3

logger = logging.getLogger(__name__)

#: Everything the models wrote. Ordered so that rows referencing others go
#: first, which keeps foreign keys satisfied on the way down.
DECISION_TABLES = (
    "auto_subs",
    "gameweek_results",
    "transfers",
    "chips_used",
    "manager_state",
    "locked_picks",
    "projections",
)

#: Everything that was observed rather than decided. Named here so the
#: distinction is visible, and asserted by a test.
OBSERVED_TABLES = (
    "teams",
    "players",
    "gameweeks",
    "fixtures",
    "player_prices",
    "player_gameweek_stats",
    "player_season_history",
)


def reset_decisions(connection: sqlite3.Connection) -> dict[str, int]:
    """Clear every model decision, keeping every observation.

    Returns how many rows went from each table, so a caller can report what it
    actually destroyed rather than claim success.
    """
    removed: dict[str, int] = {}
    for table in DECISION_TABLES:
        before = connection.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
        connection.execute(f"DELETE FROM {table}")
        removed[table] = before

    connection.execute("VACUUM")
    logger.warning("reset: cleared %s", removed)
    return removed


__all__ = ["DECISION_TABLES", "OBSERVED_TABLES", "reset_decisions"]
