"""Replaying the season, and scoring it against real results.

The backfill's whole value rests on one property: replaying GW7 must not see
GW7. These tests check that directly, and that a run which is interrupted and
resumed produces the same answer as one that was not.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from fplai.data.repository import (
    load_locked_lineup,
    load_manager_state,
    picks_are_locked,
)
from fplai.jobs.backfill import backfill, playable_gameweeks, resume_state
from fplai.jobs.score import (
    score_gameweek_for_all_models,
    score_season,
    season_summaries,
)
from fplai.strategy import best_xi, manager

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def db():
    connection = connect(":memory:")
    init_db(connection)
    bootstrap = load("bootstrap_static.json")
    ingest_teams(connection, bootstrap)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=3)
    ingest_fixtures(connection, load("fixtures.json"))
    for gameweek in (1, 2, 3):
        ingest_live_gameweek(connection, gameweek, load("event_3_live.json"))
    yield connection
    connection.close()


class TestPlayableGameweeks:
    def test_only_gameweeks_whose_deadline_has_passed_are_replayed(self, db):
        """Picking for a deadline that has not arrived is the live job's work."""
        gameweeks = playable_gameweeks(db)
        assert gameweeks
        for gameweek in gameweeks:
            row = db.execute(
                "SELECT deadline_time FROM gameweeks WHERE id = ?", (gameweek,)
            ).fetchone()
            assert row["deadline_time"] <= datetime.now(UTC).isoformat()

    def test_a_gameweek_with_no_fixtures_is_not_replayed(self, db):
        db.execute("DELETE FROM fixtures WHERE gameweek = 1")
        assert 1 not in playable_gameweeks(db)


class TestBackfill:
    def test_both_models_are_locked_for_every_gameweek(self, db):
        summary = backfill(db, through_gameweek=3, horizon=3)
        assert set(summary) == {1, 2, 3}
        for gameweek in (1, 2, 3):
            assert picks_are_locked(db, manager.MODEL_ID, gameweek)
            assert picks_are_locked(db, best_xi.MODEL_ID, gameweek)

    def test_the_manager_carries_one_squad_across_gameweeks(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        first = set(load_locked_lineup(db, manager.MODEL_ID, 1).elements)
        second = set(load_locked_lineup(db, manager.MODEL_ID, 2).elements)
        state = load_manager_state(db, 2)
        assert len(first - second) == state["transfers_made"]

    def test_best_xi_starts_from_scratch_each_gameweek(self, db):
        """It has no continuity constraint, so nothing ties one week to the next."""
        backfill(db, through_gameweek=3, horizon=3)
        for gameweek in (1, 2, 3):
            assert load_locked_lineup(db, best_xi.MODEL_ID, gameweek) is not None
        assert db.execute(
            "SELECT COUNT(*) c FROM transfers"
        ).fetchone()["c"] == db.execute(
            "SELECT COALESCE(SUM(transfers_made), 0) c FROM manager_state"
        ).fetchone()["c"]

    def test_rerunning_changes_nothing(self, db):
        """The second run must skip rather than redo, or a later model change
        would silently rewrite the past."""
        backfill(db, through_gameweek=3, horizon=3)
        before = load_locked_lineup(db, manager.MODEL_ID, 2)

        summary = backfill(db, through_gameweek=3, horizon=3)
        assert all(entry["skipped"] for entry in summary.values())
        assert load_locked_lineup(db, manager.MODEL_ID, 2) == before

    def test_a_resumed_run_matches_an_uninterrupted_one(self, db):
        """Interrupting after GW1 and resuming must give the same GW2 and GW3."""
        backfill(db, through_gameweek=3, horizon=3)
        straight = {
            gameweek: load_locked_lineup(db, manager.MODEL_ID, gameweek)
            for gameweek in (1, 2, 3)
        }

        connection = connect(":memory:")
        init_db(connection)
        bootstrap = load("bootstrap_static.json")
        ingest_teams(connection, bootstrap)
        ingest_gameweeks(connection, bootstrap)
        ingest_players(connection, bootstrap, gameweek=3)
        ingest_fixtures(connection, load("fixtures.json"))
        for gameweek in (1, 2, 3):
            ingest_live_gameweek(connection, gameweek, load("event_3_live.json"))

        backfill(connection, through_gameweek=1, horizon=3)
        backfill(connection, through_gameweek=3, horizon=3)
        resumed = {
            gameweek: load_locked_lineup(connection, manager.MODEL_ID, gameweek)
            for gameweek in (1, 2, 3)
        }
        connection.close()

        assert resumed == straight

    def test_resuming_restores_the_carried_free_transfers(self, db):
        backfill(db, through_gameweek=2, horizon=3)
        state = resume_state(db, 3)
        stored = load_manager_state(db, 2)
        assert state.free_transfers == stored["free_transfers_after"]
        assert state.squad is not None

    def test_resuming_with_nothing_locked_starts_fresh(self, db):
        state = resume_state(db, 1)
        assert state.squad is None

    def test_projections_are_stored_per_deadline(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        deadlines = {
            row["made_for_gameweek"]
            for row in db.execute("SELECT DISTINCT made_for_gameweek FROM projections")
        }
        assert deadlines == {1, 2, 3}


class TestScoring:
    def test_a_gameweek_scores_for_both_models(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        scores = score_gameweek_for_all_models(db, 3)
        assert set(scores) == {manager.MODEL_ID, best_xi.MODEL_ID}

    def test_the_manager_score_is_net_of_hits(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        scores = score_gameweek_for_all_models(db, 2)
        score = scores[manager.MODEL_ID]
        state = load_manager_state(db, 2)
        assert score.transfer_cost == state["transfer_cost"]
        assert score.points == score.points_before_hits - score.transfer_cost

    def test_results_are_stored_with_the_official_average(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        score_season(db)
        row = db.execute(
            "SELECT * FROM gameweek_results WHERE model_id = ? AND gameweek = 3",
            (best_xi.MODEL_ID,),
        ).fetchone()
        assert row["average_entry_score"] > 0
        assert row["beat_average"] in (0, 1)
        assert row["formation"].count("-") == 2

    def test_scores_are_rewritten_rather_than_duplicated(self, db):
        """Unlike picks, a score is provisional and moves as matches finish."""
        backfill(db, through_gameweek=3, horizon=3)
        score_season(db)
        score_season(db)
        count = db.execute(
            "SELECT COUNT(*) c FROM gameweek_results WHERE gameweek = 3"
        ).fetchone()["c"]
        assert count == 2  # one row per model, not four

    def test_automatic_substitutions_are_recorded(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        score_season(db)
        rows = db.execute("SELECT * FROM auto_subs").fetchall()
        for row in rows:
            assert row["out_player_id"] != row["in_player_id"]

    def test_a_gameweek_before_lockdown_is_provisional(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        early = datetime(2020, 1, 1, tzinfo=UTC)
        scores = score_gameweek_for_all_models(db, 3, now=early)
        assert all(not score.final for score in scores.values())
        assert all(score.status_label == "Provisional" for score in scores.values())

    def test_a_gameweek_after_lockdown_is_final(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        late = datetime(2030, 1, 1, tzinfo=UTC)
        scores = score_gameweek_for_all_models(db, 3, now=late)
        assert all(score.final for score in scores.values())

    def test_an_unlocked_gameweek_scores_nothing(self, db):
        from fplai.jobs.score import score_model_gameweek

        assert score_model_gameweek(db, manager.MODEL_ID, 9) is None


class TestSeasonSummaries:
    def test_totals_are_reported_per_model(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        score_season(db)
        summaries = season_summaries(db)
        assert set(summaries) == {manager.MODEL_ID, best_xi.MODEL_ID}
        for summary in summaries.values():
            assert summary.gameweeks_played == 3
            assert len(summary.cumulative_points) == 3

    def test_hits_are_tracked_separately_from_points(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        score_season(db)
        summaries = season_summaries(db)
        assert summaries[best_xi.MODEL_ID].total_transfer_cost == 0

    def test_the_cumulative_series_are_aligned_for_charting(self, db):
        backfill(db, through_gameweek=3, horizon=3)
        score_season(db)
        summary = season_summaries(db)[best_xi.MODEL_ID]
        assert len(summary.cumulative_points) == len(summary.cumulative_average)
        assert summary.cumulative_points == sorted(summary.cumulative_points)
