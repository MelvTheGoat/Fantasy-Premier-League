"""What the scheduler decides to run, and when.

Every rule about timing lives in `due_work`, which is a reading of the database
against a clock and nothing else, so these tests set up a database in a
particular state, wind the clock to a particular moment, and check what comes
back. No thread is started and nothing touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from fplai.data.db import connect, init_db, utcnow
from fplai.jobs import scheduler, seed
from fplai.jobs.scheduler import Job, Task, due_work

UK = ZoneInfo("Europe/London")

#: A Saturday deadline, and the last match of that gameweek two days later.
DEADLINE = datetime(2026, 10, 3, 10, 0, tzinfo=UTC)
LAST_KICKOFF = datetime(2026, 10, 5, 19, 0, tzinfo=UTC)

#: 09:00 UK on the day after the last match, which is when points stop moving.
LOCKDOWN = datetime(2026, 10, 6, 9, 0, tzinfo=UK)

#: A moment inside that gameweek, for the questions that need one.
NOW = LAST_KICKOFF + timedelta(hours=1)


@pytest.fixture
def db():
    connection = connect(":memory:")
    init_db(connection)
    connection.execute(
        "INSERT INTO teams (id, code, name, short_name)"
        " VALUES (1, 1, 'Club', 'CLB')"
    )
    yield connection
    connection.close()


def add_player(connection, element: int = 1) -> None:
    connection.execute(
        "INSERT INTO players (id, code, web_name, team_id, element_type,"
        " now_cost, updated_at) VALUES (?, ?, ?, 1, 3, 50, ?)",
        (element, element, f"P{element}", utcnow()),
    )


def add_history(connection, element: int = 1) -> None:
    """A past season, which is what marks the slow history stage as done."""
    connection.execute(
        "INSERT INTO player_season_history (player_id, season_name, updated_at)"
        " VALUES (?, '2025/26', ?)",
        (element, utcnow()),
    )


def add_gameweek(
    connection,
    gameweek: int,
    *,
    deadline: datetime,
    last_kickoff: datetime | None = None,
    current: bool = False,
    next_up: bool = False,
    average: int | None = None,
    with_fixture: bool = True,
) -> None:
    connection.execute(
        "INSERT INTO gameweeks (id, name, deadline_time, is_current, is_next,"
        " finished, average_entry_score, last_kickoff_time, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (
            gameweek,
            f"Gameweek {gameweek}",
            deadline.isoformat(),
            int(current),
            int(next_up),
            average,
            last_kickoff.isoformat() if last_kickoff else None,
            utcnow(),
        ),
    )
    if with_fixture:
        connection.execute(
            "INSERT INTO fixtures (id, gameweek, kickoff_time, team_h, team_a,"
            " updated_at) VALUES (?, ?, ?, 1, 1, ?)",
            (gameweek, gameweek, deadline.isoformat(), utcnow()),
        )


def lock(connection, gameweek: int, *, element: int = 1) -> None:
    """Store a pick for both models, which is what 'this gameweek is done' means."""
    for model in ("manager", "best_xi"):
        connection.execute(
            "INSERT INTO locked_picks (model_id, gameweek, player_id,"
            " squad_position, purchase_price, selling_price, locked_at)"
            " VALUES (?, ?, ?, 1, 50, 50, ?)",
            (model, gameweek, element, utcnow()),
        )


def ready(connection) -> None:
    """The minimum that makes a database look seeded rather than empty."""
    add_player(connection)
    add_history(connection)


# --- seeding ---------------------------------------------------------------


def test_an_empty_database_asks_to_be_seeded(db):
    assert due_work(db, DEADLINE) == [Job(Task.SEED)]


def test_a_half_seeded_database_is_still_seeding(db):
    add_player(db)  # reference data arrived; the slow history stage did not
    assert due_work(db, DEADLINE) == [Job(Task.SEED)]


def test_seeding_displaces_every_other_job(db):
    """The seed ends with the season replayed and scored, so nothing else is
    worth deciding until it finishes."""
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    assert due_work(db, LAST_KICKOFF) == [Job(Task.SEED)]


def test_a_seeded_database_has_nothing_to_do_between_gameweeks(db):
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, average=52)
    lock(db, 7)
    add_gameweek(db, 8, deadline=DEADLINE + timedelta(days=7), next_up=True)

    quiet = LAST_KICKOFF + timedelta(days=2)
    assert due_work(db, quiet) == []


# --- picking ---------------------------------------------------------------


def test_picks_are_made_in_the_hours_before_a_deadline(db):
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    an_hour_before = DEADLINE - timedelta(hours=1)
    assert due_work(db, an_hour_before) == [Job(Task.PICK, 8)]


def test_no_pick_is_made_before_the_window_opens(db):
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    assert due_work(db, DEADLINE - timedelta(hours=6)) == []


def test_no_pick_is_made_once_its_own_deadline_has_passed(db):
    """The leakage rule, stated as a schedule: a squad locked after its own
    deadline would have been picked knowing the team news it was meant to
    guess. The window shuts before the deadline rather than at it."""
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    assert Job(Task.PICK, 8) not in due_work(db, DEADLINE - timedelta(seconds=30))
    assert Job(Task.PICK, 8) not in due_work(db, DEADLINE + timedelta(minutes=1))


def test_a_gameweek_that_is_already_locked_is_not_picked_again(db):
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)
    lock(db, 8)

    assert due_work(db, DEADLINE - timedelta(hours=1)) == []


def test_one_model_locked_is_not_enough(db):
    """A half-finished pick run has to be finished, not counted as done."""
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)
    db.execute(
        "INSERT INTO locked_picks (model_id, gameweek, player_id, squad_position,"
        " purchase_price, selling_price, locked_at)"
        " VALUES ('best_xi', 8, 1, 1, 50, 50, ?)",
        (utcnow(),),
    )

    assert due_work(db, DEADLINE - timedelta(hours=1)) == [Job(Task.PICK, 8)]


# --- live ------------------------------------------------------------------


def test_live_points_are_pulled_while_the_gameweek_is_being_played(db):
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    mid_gameweek = DEADLINE + timedelta(hours=4)
    assert due_work(db, mid_gameweek) == [Job(Task.LIVE, 7)]


def test_live_polling_continues_between_the_last_match_and_lockdown(db):
    """Bonus points are not settled when the final whistle goes."""
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    after_the_last_match = LAST_KICKOFF + timedelta(hours=3)
    assert due_work(db, after_the_last_match) == [Job(Task.LIVE, 7)]


def test_live_polling_stops_at_lockdown(db):
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    jobs = due_work(db, LOCKDOWN + timedelta(minutes=1))
    assert Job(Task.LIVE, 7) not in jobs


# --- finalising ------------------------------------------------------------


def test_a_gameweek_past_lockdown_is_finalised(db):
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    assert due_work(db, LOCKDOWN + timedelta(minutes=1)) == [Job(Task.FINALISE, 7)]


def test_a_gameweek_with_an_official_average_is_left_alone(db):
    """`average_entry_score` only exists once FPL has checked the gameweek,
    which is the same moment its points stop moving."""
    ready(db)
    add_gameweek(
        db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, average=48, current=True
    )
    lock(db, 7)

    assert due_work(db, LOCKDOWN + timedelta(hours=5)) == []


def test_finalising_comes_before_the_next_gameweeks_picks(db):
    """The Manager's bank, squad value and free transfers carry forward, so a
    gameweek has to be closed before the next one is decided."""
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)
    add_gameweek(db, 8, deadline=LOCKDOWN + timedelta(hours=2), next_up=True)

    jobs = due_work(db, LOCKDOWN + timedelta(minutes=30))
    assert jobs == [Job(Task.FINALISE, 7), Job(Task.PICK, 8)]


# --- catching up -----------------------------------------------------------


def test_a_missed_deadline_is_repaired_by_the_backfill(db):
    """If the site was down over a deadline, the gameweek is reconstructed from
    the data that existed before it rather than picked with hindsight."""
    ready(db)
    add_gameweek(db, 6, deadline=DEADLINE - timedelta(days=7), last_kickoff=LAST_KICKOFF - timedelta(days=7), average=51)
    lock(db, 6)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)

    jobs = due_work(db, DEADLINE + timedelta(hours=4))
    assert Job(Task.CATCH_UP) in jobs


def test_an_unplayed_gameweek_is_not_treated_as_missed(db):
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    assert Job(Task.CATCH_UP) not in due_work(db, DEADLINE - timedelta(days=3))


# --- the tick --------------------------------------------------------------


class StubClient:
    """Stands in for the FPL client; no job in these tests reaches the network."""


def test_a_failing_job_does_not_stop_the_ones_after_it(db, monkeypatch):
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)
    add_gameweek(db, 8, deadline=LOCKDOWN + timedelta(hours=2), next_up=True)

    attempted: list[Job] = []

    def run(connection, client, job):
        attempted.append(job)
        if job.task is Task.FINALISE:
            raise RuntimeError("the API is having a moment")

    monkeypatch.setattr(scheduler, "run_job", run)
    done = scheduler.tick(db, StubClient(), LOCKDOWN + timedelta(minutes=30))

    assert attempted == [Job(Task.FINALISE, 7), Job(Task.PICK, 8)]
    assert done == [Job(Task.PICK, 8)]


# --- what the site says while it is filling itself in ----------------------


def test_setup_progress_reports_each_stage_in_turn(db):
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)

    assert seed.current_stage(db, NOW) == "reference"
    add_player(db)
    assert seed.current_stage(db, NOW) == "history"
    add_history(db)
    assert seed.current_stage(db, NOW) == "backfill"
    lock(db, 7)
    assert seed.current_stage(db, NOW) == "ready"


def test_a_stage_is_read_from_the_rows_it_produced(db):
    """Not from a stored flag: a flag survives a crash that the work did not,
    and then the site reports progress it never made."""
    ready(db)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)

    progress = seed.setup_progress(db, NOW)
    assert progress["ready"] is False
    assert progress["stage"] == "backfill"
    assert progress["players"] == 1
    assert progress["gameweeks"] == 0

    lock(db, 7)
    assert seed.setup_progress(db, NOW)["ready"] is True


def test_before_the_first_deadline_an_empty_picks_table_is_the_finished_state(db):
    """A database seeded in pre-season has nothing to replay, and should not
    sit there claiming to be mid-backfill until the season starts."""
    ready(db)
    add_gameweek(db, 1, deadline=DEADLINE, next_up=True)
    pre_season = DEADLINE - timedelta(days=10)

    assert seed.current_stage(db, pre_season) == "ready"
    assert due_work(db, pre_season) == []
