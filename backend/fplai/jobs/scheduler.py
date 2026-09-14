"""The scheduler, running inside the web process.

Container hosts attach a persistent disk to exactly one service, and a
platform cron job is a separate service with its own empty filesystem, so it
cannot see this database -- the season's entire record is one SQLite file on
that one disk. The jobs therefore run in a thread beside the API rather than in
a service of their own.

Nothing here remembers what it did last time. Each tick asks the database what
is due, so a restart, a redeploy or a week of downtime all resolve the same
way: whatever is unfinished is simply still due. That is also what makes it
testable -- `due_work` is a pure reading of the database against a clock, and
every rule about when a job should run is stated there rather than in a crontab.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ..data.client import FPLClient
from ..data.db import connect, init_db
from ..data.repository import (
    current_gameweek,
    deadline,
    gameweeks_underway,
    last_kickoff,
    next_gameweek,
    picks_are_locked,
)
from ..rules.scoring import is_final
from ..strategy import best_xi, manager
from .backfill import backfill
from .live import update_live
from .pick import lock_gameweek
from .refresh import finalise_gameweek, refresh_reference
from .results import missing_results, refresh_results
from .score import score_gameweek_for_all_models, score_season
from .seed import current_stage, seed

logger = logging.getLogger(__name__)

#: How long before a deadline the picks are made. Late enough that team news
#: and a price change are in, early enough that several attempts fit inside the
#: window if one fails.
PICK_LEAD = timedelta(hours=2)

#: How close to a deadline a pick will still be started. Locking a squad after
#: its own deadline would be reading the future, so the window closes first.
PICK_CUTOFF = timedelta(minutes=2)

#: Between ticks. Live points move slowly, and the pick window is wide enough
#: that this cannot miss it.
TICK_SECONDS = 300


class Task(StrEnum):
    SEED = "seed"
    CATCH_UP = "catch-up"
    PICK = "pick"
    LIVE = "live"
    FINALISE = "finalise"


@dataclass(frozen=True, slots=True)
class Job:
    task: Task
    gameweek: int | None = None

    def __str__(self) -> str:
        return f"{self.task}" + (f" GW{self.gameweek}" if self.gameweek else "")


def _both_models_locked(connection: sqlite3.Connection, gameweek: int) -> bool:
    return picks_are_locked(
        connection, manager.MODEL_ID, gameweek
    ) and picks_are_locked(connection, best_xi.MODEL_ID, gameweek)


def _awaiting_finalisation(
    connection: sqlite3.Connection, moment: datetime
) -> list[int]:
    """Gameweeks that are past lockdown but still carry provisional points.

    `average_entry_score` is the marker because it is the one field that only
    exists once FPL has checked the gameweek, which is the same moment the
    points stop moving.
    """
    candidates = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM gameweeks"
            " WHERE average_entry_score IS NULL"
            "   AND EXISTS (SELECT 1 FROM locked_picks WHERE gameweek = gameweeks.id)"
            " ORDER BY id"
        )
    ]
    return [
        gameweek
        for gameweek in candidates
        if is_final(last_kickoff(connection, gameweek), moment)
    ]


def _needs_catch_up(connection: sqlite3.Connection, moment: datetime) -> bool:
    """Whether a deadline has gone by with no picks stored against it.

    Only happens if the site was down over a deadline. The backfill is the
    repair, because it reconstructs a gameweek from the prices and data that
    existed before its own deadline rather than from today's.
    """
    return any(
        not _both_models_locked(connection, gameweek)
        for gameweek in gameweeks_underway(connection, moment)
    )


def _pick_is_due(
    connection: sqlite3.Connection, gameweek: int, moment: datetime
) -> bool:
    when = deadline(connection, gameweek)
    if when is None:
        return False
    if not PICK_CUTOFF <= when - moment <= PICK_LEAD:
        return False
    return not _both_models_locked(connection, gameweek)


def _live_gameweek(
    connection: sqlite3.Connection, moment: datetime
) -> int | None:
    """The gameweek currently being played, if its points are still moving."""
    gameweek = current_gameweek(connection)
    if gameweek is None:
        return None

    when = deadline(connection, gameweek)
    if when is None or moment < when:
        return None
    if is_final(last_kickoff(connection, gameweek), moment):
        return None
    if not _both_models_locked(connection, gameweek):
        return None
    return gameweek


def due_work(
    connection: sqlite3.Connection, now: datetime | None = None
) -> list[Job]:
    """Everything the database says is owed, at this moment.

    Ordered by dependency: a gameweek is finalised before the next one's picks
    are made, because the Manager's squad value and free transfers carry
    forward from a finished gameweek.
    """
    moment = now or datetime.now(UTC)

    if current_stage(connection, moment) != "ready":
        # Seeding subsumes every other job -- it ends with the season replayed
        # and scored -- so nothing else is worth deciding until it is done.
        return [Job(Task.SEED)]

    jobs = [
        Job(Task.FINALISE, gameweek)
        for gameweek in _awaiting_finalisation(connection, moment)
    ]

    if _needs_catch_up(connection, moment) or missing_results(connection, moment):
        jobs.append(Job(Task.CATCH_UP))

    upcoming = next_gameweek(connection)
    if upcoming is not None and _pick_is_due(connection, upcoming, moment):
        jobs.append(Job(Task.PICK, upcoming))

    live = _live_gameweek(connection, moment)
    if live is not None:
        jobs.append(Job(Task.LIVE, live))

    return jobs


def run_job(
    connection: sqlite3.Connection, client: FPLClient, job: Job
) -> None:
    """Do one job. Every branch is also a CLI command, and does the same thing."""
    if job.task is Task.SEED:
        seed(connection, client)

    elif job.task is Task.CATCH_UP:
        refresh_reference(connection, client)
        # Before the replay, because a squad scored against no results at all
        # comes out as nought rather than as a failure.
        refresh_results(connection, client)
        backfill(connection)
        score_season(connection)

    elif job.task is Task.PICK:
        refresh_reference(connection, client, gameweek=job.gameweek)
        lock_gameweek(connection, job.gameweek)

    elif job.task is Task.LIVE:
        update_live(connection, client, gameweek=job.gameweek)

    elif job.task is Task.FINALISE:
        if finalise_gameweek(connection, client, job.gameweek):
            score_gameweek_for_all_models(connection, job.gameweek)


def tick(
    connection: sqlite3.Connection,
    client: FPLClient,
    now: datetime | None = None,
) -> list[Job]:
    """Run everything that is due, and report what ran.

    A job that fails does not stop the ones after it: they are independent, and
    the FPL API being briefly unreachable should not mean a gameweek goes
    unscored as well as unrefreshed. Whatever failed is simply still due next
    tick.
    """
    done: list[Job] = []
    for job in due_work(connection, now):
        logger.info("running %s", job)
        try:
            run_job(connection, client, job)
        except Exception:
            # Broad on purpose. This thread outlives every individual failure:
            # a timeout against the FPL API, a malformed payload, a job with a
            # bug in it -- none of them should take the scheduler down with
            # them, because then nothing runs until someone notices.
            logger.exception("%s failed", job)
        else:
            done.append(job)
    return done


def run_forever(
    stop: threading.Event | None = None, interval: int = TICK_SECONDS
) -> None:
    """The loop itself. Opens its own connection, because it owns its thread."""
    stop = stop or threading.Event()
    connection = connect()
    init_db(connection)

    try:
        while not stop.is_set():
            with FPLClient() as client:
                tick(connection, client)
            stop.wait(interval)
    finally:
        connection.close()


def start(interval: int = TICK_SECONDS) -> threading.Event:
    """Start the scheduler in a daemon thread and return its stop switch.

    A daemon thread so a shutdown is never held up by a sleeping scheduler; any
    job it was midway through is due again when the process comes back.
    """
    stop = threading.Event()
    thread = threading.Thread(
        target=run_forever,
        args=(stop, interval),
        name="fplai-scheduler",
        daemon=True,
    )
    thread.start()
    logger.info("scheduler started, ticking every %ds", interval)
    return stop


__all__ = [
    "PICK_CUTOFF",
    "PICK_LEAD",
    "TICK_SECONDS",
    "Job",
    "Task",
    "due_work",
    "run_forever",
    "run_job",
    "start",
    "tick",
]
