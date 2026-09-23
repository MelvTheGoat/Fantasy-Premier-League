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
from fplai.jobs.results import missing_results
from fplai.jobs.scheduler import Job, Task, due_work
from fplai.jobs.score import gameweeks_with_a_stale_score

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
    checked: bool = False,
    with_fixture: bool = True,
) -> None:
    connection.execute(
        "INSERT INTO gameweeks (id, name, deadline_time, is_current, is_next,"
        " finished, data_checked, average_entry_score, last_kickoff_time,"
        " updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            gameweek,
            f"Gameweek {gameweek}",
            deadline.isoformat(),
            int(current),
            int(next_up),
            int(checked),
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


def add_result(connection, gameweek: int, element: int = 1) -> None:
    """A real result. Without one, a database full of squads scores as zeros."""
    connection.execute(
        "INSERT INTO player_gameweek_stats (player_id, gameweek, fixture_id,"
        " minutes, total_points, updated_at) VALUES (?, ?, ?, 90, 5, ?)",
        (element, gameweek, gameweek, utcnow()),
    )


def add_score(connection, gameweek: int, points: int = 55) -> None:
    """A stored score. Written after the result it derives from, as the jobs
    write it -- a score older than its own results is stale by definition."""
    for model in ("manager", "best_xi"):
        connection.execute(
            "INSERT INTO gameweek_results (model_id, gameweek, points_before_hits,"
            " transfer_cost, points, bench_points, captain_points, is_final,"
            " updated_at) VALUES (?, ?, ?, 0, ?, 0, 0, 0, ?)",
            (model, gameweek, points, points, utcnow()),
        )


def fresh(connection, moment: datetime) -> None:
    """Say the reference data was pulled a minute before `moment`.

    A fixture that writes its rows now and then asks what is due ten days
    later is describing a database nobody has looked at for ten days, and the
    scheduler is right to answer "go and look again". When the data was last
    pulled is part of describing the world, not a workaround.
    """
    connection.execute(
        "UPDATE gameweeks SET updated_at = ?",
        ((moment - timedelta(minutes=1)).isoformat(),),
    )


def ready(connection, played: int | None = None) -> None:
    """The minimum that makes a database look seeded rather than empty."""
    add_player(connection)
    add_history(connection)
    if played is not None:
        add_result(connection, played)
        add_score(connection, played)


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
    ready(db, played=7)
    add_gameweek(
        db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, average=52, checked=True
    )
    lock(db, 7)
    add_gameweek(db, 8, deadline=DEADLINE + timedelta(days=7), next_up=True)

    quiet = LAST_KICKOFF + timedelta(days=2)
    fresh(db, quiet)
    assert due_work(db, quiet) == []


# --- picking ---------------------------------------------------------------


def test_picks_are_made_in_the_hours_before_a_deadline(db):
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    an_hour_before = DEADLINE - timedelta(hours=1)
    fresh(db, an_hour_before)
    assert due_work(db, an_hour_before) == [Job(Task.PICK, 8)]


def test_no_pick_is_made_before_the_window_opens(db):
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    too_early = DEADLINE - timedelta(hours=12)
    fresh(db, too_early)
    assert due_work(db, too_early) == []


def test_no_pick_is_made_once_its_own_deadline_has_passed(db):
    """The leakage rule, stated as a schedule: a squad locked after its own
    deadline would have been picked knowing the team news it was meant to
    guess. The window shuts before the deadline rather than at it."""
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

    assert Job(Task.PICK, 8) not in due_work(db, DEADLINE - timedelta(seconds=30))
    assert Job(Task.PICK, 8) not in due_work(db, DEADLINE + timedelta(minutes=1))


def test_a_locked_gameweek_is_still_refined_until_its_deadline(db):
    """Before the deadline a squad is provisional. Re-picking it is what a
    human does when a striker is ruled out an hour before kick-off, and it
    reads nothing the deadline had not already published."""
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)
    lock(db, 8)

    an_hour_before = DEADLINE - timedelta(hours=1)
    fresh(db, an_hour_before)
    assert due_work(db, an_hour_before) == [Job(Task.PICK, 8)]


def test_once_the_deadline_passes_the_squad_is_not_touched_again(db):
    """The other half of the same rule, and the one that matters: after the
    deadline, rewriting picks would be hindsight."""
    ready(db)
    add_gameweek(db, 8, deadline=DEADLINE, next_up=True)
    lock(db, 8)

    assert Job(Task.PICK, 8) not in due_work(db, DEADLINE + timedelta(minutes=1))


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

    an_hour_before = DEADLINE - timedelta(hours=1)
    fresh(db, an_hour_before)
    assert due_work(db, an_hour_before) == [Job(Task.PICK, 8)]


# --- live ------------------------------------------------------------------


def test_live_points_are_pulled_while_the_gameweek_is_being_played(db):
    ready(db, played=7)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    mid_gameweek = DEADLINE + timedelta(hours=4)
    fresh(db, mid_gameweek)
    assert due_work(db, mid_gameweek) == [Job(Task.LIVE, 7)]


def test_live_polling_continues_between_the_last_match_and_lockdown(db):
    """Bonus points are not settled when the final whistle goes."""
    ready(db, played=7)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    after_the_last_match = LAST_KICKOFF + timedelta(hours=3)
    fresh(db, after_the_last_match)
    assert due_work(db, after_the_last_match) == [Job(Task.LIVE, 7)]


def test_live_polling_continues_past_lockdown_until_fpl_confirms(db):
    """Lockdown is a time this code works out; `data_checked` is FPL saying so.

    Handing a gameweek from live polling to finalisation at a computed moment
    left a gap that neither job owned, and a gameweek sat in it at nought for
    five days. Polling now overlaps finalisation rather than giving way to it.
    """
    ready(db, played=7)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    after_lockdown = LOCKDOWN + timedelta(minutes=1)
    fresh(db, after_lockdown)
    assert Job(Task.LIVE, 7) in due_work(db, after_lockdown)


def test_live_polling_stops_once_fpl_has_checked_the_gameweek(db):
    ready(db, played=7)
    add_gameweek(
        db,
        7,
        deadline=DEADLINE,
        last_kickoff=LAST_KICKOFF,
        current=True,
        average=48,
        checked=True,
    )
    lock(db, 7)

    after_lockdown = LOCKDOWN + timedelta(minutes=1)
    fresh(db, after_lockdown)
    assert due_work(db, after_lockdown) == []


# --- finalising ------------------------------------------------------------


def test_a_gameweek_past_lockdown_is_finalised(db):
    ready(db, played=7)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)

    after_lockdown = LOCKDOWN + timedelta(minutes=1)
    fresh(db, after_lockdown)
    assert due_work(db, after_lockdown) == [
        Job(Task.FINALISE, 7),
        Job(Task.LIVE, 7),
    ]


def test_a_checked_gameweek_is_left_alone(db):
    """`data_checked` is FPL's own statement that the points have settled."""
    ready(db, played=7)
    add_gameweek(
        db,
        7,
        deadline=DEADLINE,
        last_kickoff=LAST_KICKOFF,
        average=48,
        checked=True,
        current=True,
    )
    lock(db, 7)

    settled = LOCKDOWN + timedelta(hours=5)
    fresh(db, settled)
    assert due_work(db, settled) == []


def test_finalising_comes_before_the_next_gameweeks_picks(db):
    """The Manager's bank, squad value and free transfers carry forward, so a
    gameweek has to be closed before the next one is decided."""
    ready(db, played=7)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)
    add_gameweek(db, 8, deadline=LOCKDOWN + timedelta(hours=2), next_up=True)

    moment = LOCKDOWN + timedelta(minutes=30)
    fresh(db, moment)
    jobs = due_work(db, moment)
    assert jobs == [Job(Task.FINALISE, 7), Job(Task.PICK, 8), Job(Task.LIVE, 7)]


# --- catching up -----------------------------------------------------------


def test_a_missed_deadline_is_repaired_by_the_backfill(db):
    """If the site was down over a deadline, the gameweek is reconstructed from
    the data that existed before it rather than picked with hindsight."""
    ready(db, played=6)
    add_result(db, 7)
    add_gameweek(
        db,
        6,
        deadline=DEADLINE - timedelta(days=7),
        last_kickoff=LAST_KICKOFF - timedelta(days=7),
        average=51,
    )
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
    ready(db, played=7)
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
    lock(db, 7)
    add_gameweek(db, 8, deadline=LOCKDOWN + timedelta(hours=2), next_up=True)

    attempted: list[Job] = []

    def run(connection, client, job):
        attempted.append(job)
        if job.task is Task.FINALISE:
            raise RuntimeError("the API is having a moment")

    monkeypatch.setattr(scheduler, "run_job", run)
    moment = LOCKDOWN + timedelta(minutes=30)
    fresh(db, moment)
    done = scheduler.tick(db, StubClient(), moment)

    assert attempted == [
        Job(Task.FINALISE, 7),
        Job(Task.PICK, 8),
        Job(Task.LIVE, 7),
    ]
    assert done == [Job(Task.PICK, 8), Job(Task.LIVE, 7)]


# --- what the site says while it is filling itself in ----------------------


def test_setup_progress_reports_each_stage_in_turn(db):
    add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)

    assert seed.current_stage(db, NOW) == "reference"
    add_player(db)
    assert seed.current_stage(db, NOW) == "history"
    add_history(db)
    assert seed.current_stage(db, NOW) == "results"
    add_result(db, 7)
    assert seed.current_stage(db, NOW) == "backfill"
    lock(db, 7)
    assert seed.current_stage(db, NOW) == "ready"


def test_a_stage_is_read_from_the_rows_it_produced(db):
    """Not from a stored flag: a flag survives a crash that the work did not,
    and then the site reports progress it never made."""
    ready(db, played=7)
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
    fresh(db, pre_season)
    assert due_work(db, pre_season) == []


# --- the season of zeros ---------------------------------------------------


class TestResultsAreNeverAssumed:
    """A database can hold a full set of squads and no results at all. That
    scores as nought rather than as a failure, which is the kind of wrong that
    gets published and believed -- it is what the first live deployment did.
    """

    def test_squads_without_results_are_not_a_finished_setup(self, db):
        ready(db)  # players and history, but nothing played ingested
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        lock(db, 7)

        assert seed.current_stage(db, NOW) == "results"
        assert seed.setup_progress(db, NOW)["ready"] is False

    def test_the_scheduler_goes_and_gets_them(self, db):
        ready(db)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        lock(db, 7)

        assert Job(Task.SEED) in due_work(db, NOW)

    def test_a_played_gameweek_with_no_result_is_reported_missing(self, db):
        ready(db)
        add_gameweek(
            db,
            6,
            deadline=DEADLINE - timedelta(days=7),
            last_kickoff=LAST_KICKOFF - timedelta(days=7),
        )
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        add_result(db, 6)

        assert missing_results(db, NOW) == [7]

    def test_a_gameweek_not_yet_played_is_not_missing_anything(self, db):
        ready(db)
        add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

        assert missing_results(db, DEADLINE - timedelta(days=3)) == []

    def test_catching_up_collects_results_before_replaying(self, db, monkeypatch):
        """Order matters: the replay decides the squads, and the scoring that
        follows needs the real points to already be there."""
        ready(db)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        lock(db, 7)

        order: list[str] = []
        for name in ("refresh_reference", "refresh_results", "backfill", "score_season"):
            monkeypatch.setattr(
                scheduler, name, lambda *a, _n=name, **k: order.append(_n)
            )

        scheduler.run_job(db, StubClient(), Job(Task.CATCH_UP))
        assert order.index("refresh_results") < order.index("score_season")


class TestAStaleScoreIsNoticed:
    """A score is derived, so it can be wrong while nothing is missing. The
    published site once showed a full set of results next to a season of
    noughts, and nothing in the schedule would ever have corrected it.
    """

    def test_results_newer_than_the_score_make_it_stale(self, db):
        ready(db, played=7)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        lock(db, 7)
        assert gameweeks_with_a_stale_score(db) == []

        # A correction lands after the gameweek was scored.
        db.execute(
            "UPDATE player_gameweek_stats SET total_points = 9, updated_at = ?"
            " WHERE gameweek = 7",
            ("2099-01-01T00:00:00+00:00",),
        )
        assert gameweeks_with_a_stale_score(db) == [7]

    def test_results_with_no_score_at_all_count_as_stale(self, db):
        ready(db)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        add_result(db, 7)
        lock(db, 7)

        assert gameweeks_with_a_stale_score(db) == [7]

    def test_the_scheduler_rescores_it(self, db):
        ready(db)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        add_result(db, 7)
        lock(db, 7)

        assert Job(Task.SCORE) in due_work(db, NOW)

    def test_a_gameweek_nobody_picked_is_not_waiting_to_be_scored(self, db):
        """Results arrive for every player in the league, most of whom are in
        neither squad."""
        ready(db)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        add_result(db, 7)

        assert gameweeks_with_a_stale_score(db) == []


# --- the five days nothing happened ----------------------------------------


class TestTheSchedulerCanNoticeTheWorldMovedOn:
    """The failure this guards against contained no error at all.

    Every rule about what is due is a question asked of this database, and
    nothing refreshed the database unless some rule already believed something
    was happening. So once the stored view of the season fell behind, the
    scheduler had no way to find out: twenty consecutive runs decided there
    was nothing to do, reported success, and left a gameweek that had been
    played days earlier showing nought.
    """

    def test_stale_reference_data_is_refreshed_on_its_own(self, db):
        ready(db, played=7)
        add_gameweek(
            db,
            7,
            deadline=DEADLINE,
            last_kickoff=LAST_KICKOFF,
            average=52,
            checked=True,
        )
        lock(db, 7)

        quiet = LAST_KICKOFF + timedelta(days=2)
        fresh(db, quiet)
        assert due_work(db, quiet) == []

        assert due_work(db, quiet + scheduler.REFRESH_INTERVAL) == [Job(Task.REFRESH)]

    def test_a_zero_average_is_not_a_checked_gameweek(self, db):
        """The bug itself, in one line of data.

        `average_entry_score` was the marker for "FPL has settled this
        gameweek", on the reasoning that the field only appears once it has.
        The API returns a literal 0 while the gameweek is in progress, so the
        first refresh after the deadline wrote a number into the marker and
        the gameweek could never be finalised again.
        """
        ready(db, played=5)
        add_gameweek(
            db, 5, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, average=0, current=True
        )
        lock(db, 5)

        days_later = LOCKDOWN + timedelta(days=5)
        fresh(db, days_later)
        jobs = due_work(db, days_later)

        assert Job(Task.FINALISE, 5) in jobs
        assert Job(Task.LIVE, 5) in jobs

    def test_a_gameweek_the_current_flag_has_moved_past_is_still_scored(self, db):
        """`is_current` advances at the next deadline whether or not the
        gameweek it leaves behind was ever finished."""
        ready(db, played=5)
        add_gameweek(db, 5, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, average=0)
        lock(db, 5)
        add_gameweek(
            db, 6, deadline=LAST_KICKOFF + timedelta(days=1), current=True
        )

        moment = LAST_KICKOFF + timedelta(days=2)
        fresh(db, moment)
        assert Job(Task.LIVE, 5) in due_work(db, moment)


class TestATickActsOnWhatItLearns:
    def test_work_revealed_by_a_job_is_run_in_the_same_tick(self, db, monkeypatch):
        """A refresh that learns a gameweek has been played makes a
        finalisation due that was not due when the tick began. Reading what is
        owed once, at the top, drops it silently."""
        ready(db, played=7)
        add_gameweek(db, 7, deadline=DEADLINE, current=True)  # no kickoff known yet
        lock(db, 7)

        moment = LOCKDOWN + timedelta(hours=1)
        attempted: list[Job] = []

        def run(connection, client, job):
            attempted.append(job)
            if job.task is Task.REFRESH:
                connection.execute(
                    "UPDATE gameweeks SET last_kickoff_time = ?, updated_at = ?"
                    " WHERE id = 7",
                    (
                        LAST_KICKOFF.isoformat(),
                        (moment - timedelta(minutes=1)).isoformat(),
                    ),
                )

        monkeypatch.setattr(scheduler, "run_job", run)
        done = scheduler.tick(db, StubClient(), moment)

        assert Job(Task.REFRESH) in done
        assert Job(Task.FINALISE, 7) in done

    def test_a_job_is_attempted_at_most_once_per_tick(self, db, monkeypatch):
        """A job that stays due however often it runs must not hold the loop
        open for ever."""
        ready(db, played=7)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        lock(db, 7)

        moment = LOCKDOWN + timedelta(minutes=1)
        fresh(db, moment)
        attempted: list[Job] = []
        monkeypatch.setattr(
            scheduler, "run_job", lambda c, cl, job: attempted.append(job)
        )

        scheduler.tick(db, StubClient(), moment)
        assert attempted == [Job(Task.FINALISE, 7), Job(Task.LIVE, 7)]


class TestHoldingTheLineWhenTheCronDoesNot:
    """GitHub runs a scheduled workflow on a public repository when it suits
    GitHub: measured gaps on this one were two to seven hours against a
    half-hourly request. Most of the week that costs nothing, because most of
    the week nothing is happening. It costs a gameweek in the hours around a
    deadline, so a run that lands near one stays alive instead of betting on
    the next arriving in time.
    """

    def test_nothing_imminent_means_one_tick_and_out(self, db):
        ready(db, played=7)
        add_gameweek(
            db,
            7,
            deadline=DEADLINE,
            last_kickoff=LAST_KICKOFF,
            average=52,
            checked=True,
        )
        lock(db, 7)
        add_gameweek(db, 8, deadline=DEADLINE + timedelta(days=7), next_up=True)

        assert scheduler.watch_reason(db, LAST_KICKOFF + timedelta(days=2)) is None

    def test_a_gameweek_being_scored_is_not_worth_waiting_for(self, db):
        """Though it is the other moment things are moving.

        The site is published in a step that runs after the scheduler, so a
        run that stays alive for four hours publishes nothing for four hours.
        Waiting through a gameweek buys a fresher database at the precise
        cost of a staler page, and the page is the part anyone sees.
        """
        ready(db, played=7)
        add_gameweek(db, 7, deadline=DEADLINE, last_kickoff=LAST_KICKOFF, current=True)
        lock(db, 7)

        moment = LAST_KICKOFF + timedelta(hours=2)
        assert Job(Task.LIVE, 7) in due_work(db, moment)  # the work still happens
        assert scheduler.watch_reason(db, moment) is None  # it just does not linger

    def test_an_approaching_deadline_is_worth_waiting_for(self, db):
        ready(db)
        add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

        reason = scheduler.watch_reason(db, DEADLINE - timedelta(hours=3))
        assert reason == "GW8 picks are due"

    def test_the_vigil_ends_at_the_deadline(self, db, monkeypatch):
        """Which is the point: the last pick before a deadline is the one made
        with the most team news, and a dropped cron must not be what decides
        whether it happens."""
        ready(db)
        add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

        calls: list[None] = []

        def clock():
            calls.append(None)
            # Three hours out, creeping towards the deadline an hour at a time.
            return DEADLINE - timedelta(hours=3) + timedelta(hours=len(calls) // 2)

        ticks: list[object] = []
        monkeypatch.setattr(
            scheduler, "tick", lambda *a, **k: ticks.append(None) or []
        )
        slept: list[float] = []
        scheduler.watch(db, StubClient(), clock=clock, sleep=slept.append)

        # Ticked its way to the deadline and then stopped of its own accord:
        # three hours out, two hours out, one hour out, and then the deadline
        # itself, which is where the pick window shuts.
        assert len(ticks) == 3
        assert slept == [scheduler.WATCH_INTERVAL] * 2

    def test_the_budget_ends_the_vigil(self, db, monkeypatch):
        """A job killed for overrunning would lose the work it had just done,
        because the season is stored in a later step. The vigil gives up well
        short of that."""
        ready(db)
        add_gameweek(db, 8, deadline=DEADLINE, next_up=True)

        start = DEADLINE - timedelta(hours=8)
        calls: list[None] = []

        def clock():
            calls.append(None)
            return start + timedelta(hours=len(calls))

        monkeypatch.setattr(scheduler, "tick", lambda *a, **k: [])
        slept: list[float] = []
        scheduler.watch(
            db,
            StubClient(),
            budget=timedelta(hours=4),
            clock=clock,
            sleep=slept.append,
        )

        # The deadline is still ahead, so only the budget can have stopped it.
        assert scheduler.watch_reason(db, start) == "GW8 picks are due"
        assert len(slept) == 1
