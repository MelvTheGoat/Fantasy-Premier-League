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
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from ..data.client import FPLClient
from ..data.db import connect, init_db
from ..data.repository import (
    deadline,
    gameweeks_underway,
    last_kickoff,
    next_gameweek,
    picks_are_locked,
    reference_refreshed_at,
)
from ..rules.scoring import is_final
from ..strategy import best_xi, manager
from .backfill import backfill
from .live import update_live
from .pick import lock_gameweek
from .refresh import finalise_gameweek, refresh_reference
from .results import missing_results, refresh_results
from .score import (
    gameweeks_with_a_stale_score,
    score_gameweek_for_all_models,
    score_season,
)
from .seed import current_stage, seed

logger = logging.getLogger(__name__)

#: How long before a deadline the picks are made.
#:
#: Eight hours, not two, because GitHub's scheduler is best-effort and drops
#: most of a half-hourly cron on a public repository: observed gaps between
#: runs are two to five hours. A two-hour window was missed outright about two
#: times in five, and a missed deadline is a gameweek the Manager sat out.
#:
#: Picking early costs nothing now that a squad can be refined until the
#: deadline: the first run inside the window banks a legal squad, and every
#: run after it improves on that as team news arrives.
PICK_LEAD = timedelta(hours=8)

#: How close to a deadline a pick will still be started. Locking a squad after
#: its own deadline would be reading the future, so the window closes first.
PICK_CUTOFF = timedelta(minutes=2)

#: Between ticks. Live points move slowly, and the pick window is wide enough
#: that this cannot miss it.
TICK_SECONDS = 300

#: How long the reference data -- players, gameweeks, fixtures -- may go
#: unrefreshed before a tick pulls it again whatever else is or is not due.
#:
#: This is the one job with no precondition, and it exists because every other
#: rule here is a question asked of this database. When the database is stale
#: the answers are stale with it, and a scheduler that only refreshes when it
#: already believes something is happening can never discover that something
#: happened. It sat idle for five days that way, holding a gameweek at nought
#: while the matches it was waiting for had long since been played.
REFRESH_INTERVAL = timedelta(hours=3)

#: How long a single run will keep ticking rather than exiting, when there is
#: something worth staying alive for. Well under GitHub's six-hour ceiling on
#: a job, because the season is stored in a step that runs *after* this one:
#: a job killed for running long would lose the work it just did.
WATCH_BUDGET = timedelta(hours=4)

#: Between ticks while watching. Longer than a tick because nothing being
#: waited on -- a deadline, a gameweek's bonus points -- moves faster.
WATCH_INTERVAL = 900

#: How many times one tick will re-derive what is due.
#:
#: A job changes the database, so the work that was due when a tick started is
#: not the work that is due once it has run: a refresh that discovers a
#: finished gameweek creates a finalisation that did not exist a moment
#: earlier. Acting on a single snapshot silently drops that. Three passes
#: covers the longest real chain -- refresh, finalise, score -- and bounds a
#: job that somehow stays due from spinning for ever.
MAX_PASSES = 3


class Task(StrEnum):
    SEED = "seed"
    REFRESH = "refresh"
    CATCH_UP = "catch-up"
    SCORE = "score"
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

    The marker is `data_checked`, FPL's own statement that it has finished
    with a gameweek. It used to be `average_entry_score IS NULL`, on the
    reasoning that the average only appears once the gameweek is checked.
    That reasoning was sound and the field was not: the API returns a literal
    0 for a gameweek in progress, so the first refresh after a deadline
    overwrote the marker with a number and the gameweek could never be
    finalised again. GW5 sat at nought for five days behind that 0.

    A field that means "unknown" in one state and "zero" in another cannot
    carry a decision. `data_checked` is a flag and only ever means one thing.
    """
    candidates = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM gameweeks"
            " WHERE COALESCE(data_checked, 0) = 0"
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

    # Deliberately not "unless already locked". Before its deadline a squad is
    # provisional, and re-picking it is what a human does when a striker is
    # ruled out an hour before kick-off. It reads nothing the deadline had not
    # already published, so it is not hindsight.
    return True


def _live_gameweeks(
    connection: sqlite3.Connection, moment: datetime
) -> list[int]:
    """Gameweeks whose points are still being counted.

    A gameweek stays here until FPL marks it checked, rather than until a
    lockdown time this code works out for itself. The old rule dropped a
    gameweek the moment computed lockdown passed and left finalisation to take
    it from there; when finalisation could not fire, no job owned the gameweek
    at all and its score stayed at nought. Overlapping with finalisation is
    the point -- it costs one request, and the alternative is a gap between
    two jobs that nothing covers.

    Every unchecked gameweek is returned, not just the current one, because
    `is_current` moves on at the next deadline whether or not the gameweek it
    is leaving behind was ever scored.
    """
    live = []
    for row in connection.execute(
        "SELECT id FROM gameweeks WHERE COALESCE(data_checked, 0) = 0 ORDER BY id"
    ):
        gameweek = row["id"]
        when = deadline(connection, gameweek)
        if when is None or moment < when:
            continue
        if not _both_models_locked(connection, gameweek):
            continue
        live.append(gameweek)
    return live


def _reference_is_stale(connection: sqlite3.Connection, moment: datetime) -> bool:
    """Whether the reference data is old enough to be worth pulling again."""
    last = reference_refreshed_at(connection)
    return last is None or moment - last >= REFRESH_INTERVAL


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

    jobs: list[Job] = []

    # First, and on nothing but a clock. Every rule below is a question asked
    # of this database, so they are all worth exactly as much as the last
    # refresh was recent.
    if _reference_is_stale(connection, moment):
        jobs.append(Job(Task.REFRESH))

    jobs += [
        Job(Task.FINALISE, gameweek)
        for gameweek in _awaiting_finalisation(connection, moment)
    ]

    if _needs_catch_up(connection, moment) or missing_results(connection, moment):
        jobs.append(Job(Task.CATCH_UP))

    # A score can be stale without anything being missing, so this is checked
    # on its own rather than folded into the job that happened to write it.
    if gameweeks_with_a_stale_score(connection):
        jobs.append(Job(Task.SCORE))

    upcoming = next_gameweek(connection)
    if upcoming is not None and _pick_is_due(connection, upcoming, moment):
        jobs.append(Job(Task.PICK, upcoming))

    jobs += [
        Job(Task.LIVE, gameweek)
        for gameweek in _live_gameweeks(connection, moment)
    ]

    return jobs


def run_job(
    connection: sqlite3.Connection, client: FPLClient, job: Job
) -> None:
    """Do one job. Every branch is also a CLI command, and does the same thing."""
    if job.task is Task.SEED:
        seed(connection, client)

    elif job.task is Task.REFRESH:
        refresh_reference(connection, client)

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

    elif job.task is Task.SCORE:
        score_season(connection)

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

    What is due is re-derived after each pass rather than read once at the
    start, because a job changes the database it was chosen from. A refresh
    that learns a gameweek has finished makes a finalisation due that was not
    due a moment earlier, and a snapshot taken before the refresh cannot see
    it. A job is attempted at most once per tick, failures included, so a
    persistent failure cannot hold the loop open.
    """
    done: list[Job] = []
    attempted: set[Job] = set()

    for _ in range(MAX_PASSES):
        outstanding = [
            job for job in due_work(connection, now) if job not in attempted
        ]
        if not outstanding:
            break

        for job in outstanding:
            attempted.add(job)
            logger.info("running %s", job)
            try:
                run_job(connection, client, job)
            except Exception:
                # Broad on purpose. This thread outlives every individual
                # failure: a timeout against the FPL API, a malformed payload,
                # a job with a bug in it -- none of them should take the
                # scheduler down with them, because then nothing runs until
                # someone notices.
                logger.exception("%s failed", job)
            else:
                done.append(job)

    return done


def watch_reason(
    connection: sqlite3.Connection, now: datetime | None = None
) -> str | None:
    """Why one run should stay alive and keep ticking, or None to just exit.

    GitHub runs a scheduled workflow on a public repository when it suits
    GitHub: measured gaps between runs of this one are two to seven hours
    against a half-hourly request. Most of the week that costs nothing,
    because most of the week there is nothing to do -- no work falls between
    a Monday night and the following Friday.

    It costs a gameweek in the hours before a deadline, and only there. A
    deadline missed is a squad that never entered; a score read late is
    corrected by the next run that happens along. So this waits for the one
    and not for the other.

    Deliberately not "while a gameweek is being scored", though that is the
    other moment things are moving. The site is published in a step that runs
    after this one, so a run that stays alive for four hours is a run that
    publishes nothing for four hours. Waiting through a gameweek would buy a
    fresher database at the exact cost of a staler page, which is the only
    part anyone sees.
    """
    moment = now or datetime.now(UTC)

    upcoming = next_gameweek(connection)
    if upcoming is not None:
        when = deadline(connection, upcoming)
        if when is not None and PICK_CUTOFF <= when - moment <= PICK_LEAD:
            return f"GW{upcoming} picks are due"

    return None


def watch(
    connection: sqlite3.Connection,
    client: FPLClient,
    *,
    budget: timedelta = WATCH_BUDGET,
    interval: int = WATCH_INTERVAL,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> list[Job]:
    """Tick until there is nothing left worth waiting around for.

    The clock and the sleep are arguments so the loop can be tested without
    one, which is the only way a rule about time is ever worth trusting.
    """
    started = clock()
    done: list[Job] = []

    while True:
        done.extend(tick(connection, client))

        reason = watch_reason(connection, clock())
        if reason is None:
            return done

        if clock() - started >= budget:
            logger.info(
                "watch budget spent with %s; leaving it to the next run", reason
            )
            return done

        logger.info("%s -- ticking again in %ds", reason, interval)
        sleep(interval)


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
    "MAX_PASSES",
    "PICK_CUTOFF",
    "PICK_LEAD",
    "REFRESH_INTERVAL",
    "TICK_SECONDS",
    "WATCH_BUDGET",
    "WATCH_INTERVAL",
    "Job",
    "Task",
    "due_work",
    "run_forever",
    "run_job",
    "start",
    "tick",
    "watch",
    "watch_reason",
]
