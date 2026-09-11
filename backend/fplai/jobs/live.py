"""Keeping a gameweek's scores current while it is being played.

Runs on a timer during a gameweek. Each pass refreshes the live points and
re-runs the manager-level rules over them, which is where automatic
substitutions land as matches finish, then rewrites the stored scores.

Nothing here touches picks. Those were locked at the deadline and stay locked;
only the scoring on top of them moves.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from ..data.client import FPLClient
from ..data.repository import current_gameweek, last_kickoff
from ..rules.scoring import is_final, lockdown_time
from .refresh import refresh_live
from .score import score_gameweek_for_all_models

logger = logging.getLogger(__name__)


def update_live(
    connection: sqlite3.Connection,
    client: FPLClient,
    *,
    gameweek: int | None = None,
    now: datetime | None = None,
) -> dict:
    """One live pass: pull the latest points, then rescore both models."""
    target = gameweek if gameweek is not None else current_gameweek(connection)
    if target is None:
        logger.warning("no current gameweek; nothing to update")
        return {"gameweek": None, "rows": 0, "final": False, "scores": {}}

    moment = now or datetime.now(UTC)
    rows = refresh_live(connection, client, target)
    scores = score_gameweek_for_all_models(connection, target, now=moment)

    kickoff = last_kickoff(connection, target)
    final = is_final(kickoff, moment)

    logger.info(
        "GW%d live update: %d rows, %s",
        target,
        rows,
        ", ".join(f"{model}={score.points}" for model, score in scores.items()),
    )

    return {
        "gameweek": target,
        "rows": rows,
        "final": final,
        "lockdown": lockdown_time(kickoff).isoformat() if kickoff else None,
        "scores": {model: score.points for model, score in scores.items()},
    }


__all__ = ["update_live"]
