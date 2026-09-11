"""Ingestion from FPL API payloads into the database.

Run against recorded payloads rather than the live API, so the tests are
deterministic and need no network. `tests/fixtures/build_fixtures.py` documents
where they came from.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    IngestError,
    blank_teams,
    double_teams,
    fixtures_per_team,
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
    player_fixture_counts,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def db():
    connection = connect(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def bootstrap():
    return load("bootstrap_static.json")


@pytest.fixture
def loaded(db, bootstrap):
    """A database with reference data and fixtures already ingested."""
    ingest_teams(db, bootstrap)
    ingest_gameweeks(db, bootstrap)
    ingest_players(db, bootstrap, gameweek=4)
    ingest_fixtures(db, load("fixtures.json"))
    return db


class TestTeams:
    def test_teams_are_stored_with_their_code(self, db, bootstrap):
        assert ingest_teams(db, bootstrap) == 6
        row = db.execute("SELECT * FROM teams WHERE id = 1").fetchone()
        assert row["short_name"] == "ARS"
        assert row["code"] == 3, "the code, not the id, builds shirt image URLs"

    def test_reingesting_updates_rather_than_duplicates(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        bootstrap["teams"][0]["strength_overall_home"] = 1400
        ingest_teams(db, bootstrap)
        assert db.execute("SELECT COUNT(*) c FROM teams").fetchone()["c"] == 6
        assert db.execute(
            "SELECT strength_overall_home s FROM teams WHERE id = 1"
        ).fetchone()["s"] == 1400


class TestPlayers:
    def test_players_are_stored_with_positions_from_the_api(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        assert ingest_players(db, bootstrap) == 26
        rows = {r["id"]: r for r in db.execute("SELECT * FROM players")}
        assert rows[1]["element_type"] == 1, "Raya is a goalkeeper"
        assert rows[4]["element_type"] == 4, "Watkins is a forward"

    def test_a_reclassified_player_is_updated_not_kept(self, db, bootstrap):
        """Positions change between seasons, so the stored value must follow
        the API rather than whatever was ingested first."""
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap)
        rogers = next(e for e in bootstrap["elements"] if e["id"] == 6)
        assert rogers["element_type"] == 3
        rogers["element_type"] = 2  # Rogers, MID -> DEF
        ingest_players(db, bootstrap)
        assert db.execute(
            "SELECT element_type t FROM players WHERE id = 6"
        ).fetchone()["t"] == 2

    def test_an_unknown_position_is_rejected(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        bootstrap["elements"][0]["element_type"] = 9
        with pytest.raises(IngestError, match="unknown element_type"):
            ingest_players(db, bootstrap)

    def test_a_missing_required_field_is_rejected(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        del bootstrap["elements"][0]["now_cost"]
        with pytest.raises(IngestError, match="now_cost"):
            ingest_players(db, bootstrap)

    def test_availability_flags_are_kept_for_the_projection_model(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap)
        injured = db.execute("SELECT * FROM players WHERE id = 5").fetchone()
        assert injured["status"] == "i"
        assert injured["chance_of_playing_next_round"] == 0
        assert "Hamstring" in injured["news"]

    def test_prices_are_snapshotted_per_gameweek(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap, gameweek=4)
        ingest_players(db, load("bootstrap_static_gw5.json"), gameweek=5)

        prices = {
            (r["player_id"], r["gameweek"]): r["now_cost"]
            for r in db.execute("SELECT * FROM player_prices")
        }
        assert prices[(3, 4)] == 100
        assert prices[(3, 5)] == 103, "Saka rose £0.3m"
        assert prices[(9, 5)] == 61, "Wissa fell £0.1m"

    def test_no_price_snapshot_is_written_without_a_gameweek(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap)
        assert db.execute("SELECT COUNT(*) c FROM player_prices").fetchone()["c"] == 0


class TestGameweeks:
    def test_gameweeks_are_stored_with_the_official_average(self, db, bootstrap):
        assert ingest_gameweeks(db, bootstrap) == 38
        row = db.execute("SELECT * FROM gameweeks WHERE id = 3").fetchone()
        assert row["average_entry_score"] == 55
        assert row["finished"] == 1

    def test_the_current_gameweek_is_flagged(self, db, bootstrap):
        ingest_gameweeks(db, bootstrap)
        current = db.execute("SELECT id FROM gameweeks WHERE is_current = 1").fetchone()
        assert current["id"] == 4


class TestFixtures:
    def test_every_fixture_is_stored(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_gameweeks(db, bootstrap)
        assert ingest_fixtures(db, load("fixtures.json")) == 16

    def test_an_unscheduled_fixture_keeps_a_null_gameweek(self, loaded):
        row = loaded.execute("SELECT gameweek FROM fixtures WHERE id = 16").fetchone()
        assert row["gameweek"] is None

    def test_the_last_kickoff_of_each_gameweek_is_recorded(self, loaded):
        """Lockdown is 09:00 the day after this, so it has to be the latest
        kickoff, not the first."""
        row = loaded.execute("SELECT last_kickoff_time k FROM gameweeks WHERE id = 4").fetchone()
        assert row["k"] == "2026-09-14T19:00:00Z"


class TestBlanksAndDoubles:
    def test_an_ordinary_gameweek_gives_every_club_one_fixture(self, loaded):
        assert fixtures_per_team(loaded, 1) == {1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1}

    def test_a_double_gameweek_is_detected(self, loaded):
        assert double_teams(loaded, 4) == {1, 2}

    def test_a_blank_gameweek_is_detected(self, loaded):
        """Clubs without a fixture have no row at all, so they must be filled
        in from the team list rather than silently omitted."""
        assert blank_teams(loaded, 4) == {3, 4}

    def test_players_inherit_their_clubs_fixture_count(self, loaded):
        counts = player_fixture_counts(loaded, 4)
        assert counts[1] == 2, "Raya's club plays twice"
        assert counts[8] == 0, "Semenyo's club blanks"

    def test_a_gameweek_with_no_fixtures_at_all_blanks_everyone(self, loaded):
        assert blank_teams(loaded, 30) == {1, 2, 3, 4, 5, 6}


class TestLiveResults:
    def test_a_single_gameweek_stores_one_row_per_player(self, loaded):
        assert ingest_live_gameweek(loaded, 3, load("event_3_live.json")) == 4
        row = loaded.execute(
            "SELECT * FROM player_gameweek_stats WHERE player_id = 1 AND gameweek = 3"
        ).fetchone()
        assert (row["minutes"], row["total_points"], row["saves"]) == (90, 9, 4)

    def test_defensive_contribution_stats_are_kept(self, loaded):
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        row = loaded.execute(
            "SELECT * FROM player_gameweek_stats WHERE player_id = 2 AND gameweek = 3"
        ).fetchone()
        assert row["clearances_blocks_interceptions"] == 8
        assert row["tackles"] == 3
        assert row["defensive_contribution"] == 2

    def test_expected_stats_are_stored_as_numbers(self, loaded):
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        row = loaded.execute(
            "SELECT expected_goals x FROM player_gameweek_stats"
            " WHERE player_id = 8 AND gameweek = 3"
        ).fetchone()
        assert row["x"] == pytest.approx(0.64)

    def test_a_double_gameweek_stores_a_row_per_fixture(self, loaded):
        ingest_live_gameweek(loaded, 4, load("event_4_live.json"))
        rows = loaded.execute(
            "SELECT fixture_id FROM player_gameweek_stats"
            " WHERE player_id = 1 AND gameweek = 4 ORDER BY fixture_id"
        ).fetchall()
        assert [r["fixture_id"] for r in rows] == [10, 11]

    def test_double_gameweek_minutes_sum_to_the_gameweek_total(self, loaded):
        ingest_live_gameweek(loaded, 4, load("event_4_live.json"))
        row = loaded.execute(
            "SELECT SUM(minutes) m, SUM(total_points) p FROM player_gameweek_stats"
            " WHERE player_id = 1 AND gameweek = 4"
        ).fetchone()
        assert row["m"] == 180

    def test_a_blank_player_gets_no_row(self, loaded):
        """No fixture means nothing to store, which is what makes them read as
        zero minutes when the auto-sub rules run."""
        ingest_live_gameweek(loaded, 4, load("event_4_live.json"))
        rows = loaded.execute(
            "SELECT COUNT(*) c FROM player_gameweek_stats WHERE player_id = 8 AND gameweek = 4"
        ).fetchone()
        assert rows["c"] == 0

    def test_a_player_who_missed_the_first_of_two_fixtures(self, loaded):
        ingest_live_gameweek(loaded, 4, load("event_4_live.json"))
        rows = {
            r["fixture_id"]: r["minutes"]
            for r in loaded.execute(
                "SELECT fixture_id, minutes FROM player_gameweek_stats"
                " WHERE player_id = 6 AND gameweek = 4"
            )
        }
        assert rows == {10: 0, 11: 62}

    def test_reingesting_live_data_updates_in_place(self, loaded):
        """The live job polls repeatedly during a gameweek, so the same rows
        are rewritten as scores move."""
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        payload = load("event_3_live.json")
        payload["elements"][0]["stats"]["total_points"] = 13
        payload["elements"][0]["stats"]["bonus"] = 3
        ingest_live_gameweek(loaded, 3, payload)

        assert loaded.execute(
            "SELECT COUNT(*) c FROM player_gameweek_stats WHERE gameweek = 3"
        ).fetchone()["c"] == 4
        assert loaded.execute(
            "SELECT total_points p FROM player_gameweek_stats"
            " WHERE player_id = 1 AND gameweek = 3"
        ).fetchone()["p"] == 13

    def test_a_malformed_live_payload_is_rejected(self, loaded):
        with pytest.raises(IngestError, match="elements"):
            ingest_live_gameweek(loaded, 3, {})


class TestIngestLog:
    def test_each_ingest_is_logged(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_gameweeks(db, bootstrap)
        rows = db.execute("SELECT endpoint, status, rows FROM ingest_log ORDER BY id").fetchall()
        assert [r["endpoint"] for r in rows] == [
            "bootstrap-static/teams", "bootstrap-static/events"
        ]
        assert all(r["status"] == "ok" for r in rows)

    def test_a_failed_ingest_leaves_no_partial_rows(self, db, bootstrap):
        """The whole refresh is one transaction, so a bad element cannot leave
        half the player table updated."""
        ingest_teams(db, bootstrap)
        bootstrap["elements"][4]["element_type"] = 9  # any element will do
        with pytest.raises(IngestError):
            ingest_players(db, bootstrap)
        assert db.execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 0
