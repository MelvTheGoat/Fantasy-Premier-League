"""Pulling the FPL API into the database.

Three jobs, matching the three moments that matter in a gameweek:

    refresh_reference   before a deadline: players, teams, prices, fixtures
    refresh_live        during a gameweek: points as matches finish
    finalise_gameweek   after lockdown: the official average and final points

Each is idempotent, so re-running one after a failure is safe.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from ..data.client import FPLClient
from ..data.ingest import (
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from ..data.repository import current_gameweek, last_kickoff
from ..rules.scoring import is_final

logger = logging.getLogger(__name__)

#: Live scores move throughout a match, so the cache must not hold them.
LIVE_CACHE_TTL = 0


def refresh_reference(
    connection: sqlite3.Connection,
    client: FPLClient,
    *,
    gameweek: int | None = None,
) -> dict[str, int]:
    """Refresh players, teams, gameweeks and fixtures.

    `gameweek` is the week whose prices are being snapshotted -- normally the
    one whose deadline is next, since that is the price the models will pay.
    """
    bootstrap = client.bootstrap_static()
    fixtures = client.fixtures()

    counts = {
        "teams": ingest_teams(connection, bootstrap),
        "gameweeks": ingest_gameweeks(connection, bootstrap),
        "players": ingest_players(connection, bootstrap, gameweek=gameweek),
        "fixtures": ingest_fixtures(connection, fixtures),
    }
    logger.info("reference refresh: %s", counts)
    return counts


def refresh_live(
    connection: sqlite3.Connection,
    client: FPLClient,
    gameweek: int | None = None,
) -> int:
    """Pull the current gameweek's live points.

    Also refreshes fixtures, because whether a fixture has finished is what
    tells the auto-substitution rules they may act.
    """
    target = gameweek if gameweek is not None else current_gameweek(connection)
    if target is None:
        logger.warning("no current gameweek; skipping live refresh")
        return 0

    ingest_fixtures(connection, client.fixtures(ttl_seconds=LIVE_CACHE_TTL))
    live = client.event_live(target, ttl_seconds=LIVE_CACHE_TTL)
    rows = ingest_live_gameweek(connection, target, live)
    logger.info("live refresh for GW%d: %d rows", target, rows)
    return rows


def finalise_gameweek(
    connection: sqlite3.Connection,
    client: FPLClient,
    gameweek: int,
    *,
    now: datetime | None = None,
) -> bool:
    """Take a gameweek's final points and average once lockdown has passed.

    Returns False and changes nothing if lockdown has not been reached, so the
    job can be scheduled optimistically and simply do nothing when early.
    """
    moment = now or datetime.now(timezone.utc)
    kickoff = last_kickoff(connection, gameweek)

    if not is_final(kickoff, moment):
        logger.info("GW%d has not reached lockdown yet; leaving it provisional", gameweek)
        return False

    # bootstrap-static is re-read because `average_entry_score` is only filled
    # in once the gameweek is checked.
    bootstrap = client.bootstrap_static(ttl_seconds=LIVE_CACHE_TTL)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=gameweek + 1)
    ingest_fixtures(connection, client.fixtures(ttl_seconds=LIVE_CACHE_TTL))
    ingest_live_gameweek(connection, gameweek, client.event_live(gameweek, ttl_seconds=LIVE_CACHE_TTL))

    logger.info("GW%d finalised", gameweek)
    return True


__all__ = ["LIVE_CACHE_TTL", "finalise_gameweek", "refresh_live", "refresh_reference"]
