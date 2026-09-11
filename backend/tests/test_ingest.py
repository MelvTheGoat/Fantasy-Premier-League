"""Ingestion from FPL API payloads into the database.

Most of these run against **genuine recorded responses** in `tests/fixtures/`,
written by `record_fixtures.py` and trimmed to 43 players so a diff stays
readable. Every field the API sends for those players is kept, so a rename or
a type change upstream fails a test here rather than silently zeroing a column.

Blank and double gameweeks use hand-built payloads instead: the real fixture
list has none yet this season. `build_synthetic_fixtures.py` explains why.
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

#: Counts in the recorded payloads. Change these when re-recording.
RECORDED_TEAMS = 20
RECORDED_PLAYERS = 43
RECORDED_FIXTURES = 60
CURRENT_GAMEWEEK = 3


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
    """A real, trimmed `bootstrap-static/` response."""
    return load("bootstrap_static.json")


@pytest.fixture
def loaded(db, bootstrap):
    """A database with the real reference data and fixtures ingested."""
    ingest_teams(db, bootstrap)
    ingest_gameweeks(db, bootstrap)
    ingest_players(db, bootstrap, gameweek=CURRENT_GAMEWEEK)
    ingest_fixtures(db, load("fixtures.json"))
    return db


@pytest.fixture
def synthetic(db):
    """A database loaded with the constructed blank/double gameweek season."""
    bootstrap = load("synthetic_bootstrap.json")
    ingest_teams(db, bootstrap)
    ingest_gameweeks(db, bootstrap)
    ingest_players(db, bootstrap, gameweek=4)
    ingest_fixtures(db, load("synthetic_fixtures.json"))
    return db


class TestTeams:
    def test_teams_are_stored_with_their_code(self, db, bootstrap):
        assert ingest_teams(db, bootstrap) == RECORDED_TEAMS
        row = db.execute("SELECT * FROM teams WHERE id = 1").fetchone()
        assert row["short_name"]
        assert row["code"] != row["id"], (
            "the shirt CDN is keyed by team code, which is not the team id"
        )

    def test_overall_strength_is_a_one_to_five_rating(self, db, bootstrap):
        """2026/27 publishes `strength_overall_*` on a 1-5 scale, not the
        roughly 1000-1350 scale earlier seasons used."""
        ingest_teams(db, bootstrap)
        ratings = [
            row["strength_overall_home"]
            for row in db.execute("SELECT strength_overall_home FROM teams")
        ]
        assert all(1 <= rating <= 5 for rating in ratings)
        assert len(set(ratings)) > 1, "the ratings should discriminate between clubs"

    def test_granular_attack_and_defence_ratings_are_no_longer_published(self, db, bootstrap):
        """FPL zeroed `strength_attack_*` and `strength_defence_*` for 2026/27.

        This is a guard, not an endorsement: the projection model must derive
        attacking and defensive strength from fixture difficulty and actual
        results instead. If this test starts failing, the API has begun
        publishing them again and the model can use them directly.
        """
        ingest_teams(db, bootstrap)
        row = db.execute(
            "SELECT SUM(strength_attack_home + strength_attack_away"
            " + strength_defence_home + strength_defence_away) total FROM teams"
        ).fetchone()
        assert row["total"] == 0

    def test_reingesting_updates_rather_than_duplicates(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        bootstrap["teams"][0]["strength_overall_home"] = 1400
        ingest_teams(db, bootstrap)
        assert db.execute("SELECT COUNT(*) c FROM teams").fetchone()["c"] == RECORDED_TEAMS
        assert db.execute(
            "SELECT strength_overall_home s FROM teams WHERE id = 1"
        ).fetchone()["s"] == 1400


class TestPlayers:
    def test_players_are_stored_with_positions_from_the_api(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        assert ingest_players(db, bootstrap) == RECORDED_PLAYERS
        types = {
            row["element_type"]
            for row in db.execute("SELECT DISTINCT element_type FROM players")
        }
        assert types == {1, 2, 3, 4}

    def test_a_reclassified_player_is_updated_not_kept(self, db, bootstrap):
        """Positions change between seasons, so the stored value must follow
        the API rather than whatever was ingested first."""
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap)

        midfielder = next(e for e in bootstrap["elements"] if e["element_type"] == 3)
        midfielder["element_type"] = 2
        ingest_players(db, bootstrap)

        assert db.execute(
            "SELECT element_type t FROM players WHERE id = ?", (midfielder["id"],)
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

        injured = db.execute(
            "SELECT * FROM players WHERE status = 'i' LIMIT 1"
        ).fetchone()
        assert injured is not None, "the recording should include an injured player"
        assert injured["news"], "an injured player carries news explaining why"

    def test_a_doubtful_player_keeps_their_playing_chance(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap)
        doubtful = db.execute(
            "SELECT * FROM players"
            " WHERE chance_of_playing_next_round IS NOT NULL"
            " AND chance_of_playing_next_round NOT IN (0, 100) LIMIT 1"
        ).fetchone()
        assert doubtful is not None
        assert 0 < doubtful["chance_of_playing_next_round"] < 100

    def test_prices_are_snapshotted_per_gameweek(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap, gameweek=3)

        moved = load("bootstrap_static.json")
        target = moved["elements"][0]
        target["now_cost"] += 3
        ingest_players(db, moved, gameweek=4)

        prices = {
            (r["player_id"], r["gameweek"]): r["now_cost"]
            for r in db.execute("SELECT * FROM player_prices")
        }
        assert prices[(target["id"], 4)] == prices[(target["id"], 3)] + 3

    def test_no_price_snapshot_is_written_without_a_gameweek(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_players(db, bootstrap)
        assert db.execute("SELECT COUNT(*) c FROM player_prices").fetchone()["c"] == 0


class TestGameweeks:
    def test_all_thirty_eight_gameweeks_are_stored(self, db, bootstrap):
        assert ingest_gameweeks(db, bootstrap) == 38

    def test_the_official_average_is_read_from_the_api_data(self, db, bootstrap):
        ingest_gameweeks(db, bootstrap)
        row = db.execute("SELECT * FROM gameweeks WHERE id = 1").fetchone()
        assert row["average_entry_score"] > 0
        assert row["finished"] == 1

    def test_the_current_gameweek_is_flagged(self, db, bootstrap):
        ingest_gameweeks(db, bootstrap)
        current = db.execute("SELECT id FROM gameweeks WHERE is_current = 1").fetchone()
        assert current["id"] == CURRENT_GAMEWEEK

    def test_an_unplayed_gameweek_has_no_average_yet(self, db, bootstrap):
        ingest_gameweeks(db, bootstrap)
        row = db.execute(
            "SELECT average_entry_score a FROM gameweeks WHERE id = 38"
        ).fetchone()
        assert not row["a"]


class TestFixtures:
    def test_every_fixture_is_stored(self, loaded):
        assert loaded.execute(
            "SELECT COUNT(*) c FROM fixtures"
        ).fetchone()["c"] == RECORDED_FIXTURES

    def test_difficulty_ratings_are_kept(self, loaded):
        row = loaded.execute("SELECT * FROM fixtures LIMIT 1").fetchone()
        assert 1 <= row["team_h_difficulty"] <= 5
        assert 1 <= row["team_a_difficulty"] <= 5

    def test_the_last_kickoff_of_each_gameweek_is_recorded(self, loaded):
        """Lockdown is 09:00 the day after this, so it must be the latest
        kickoff of the gameweek, not the first."""
        row = loaded.execute(
            "SELECT last_kickoff_time k FROM gameweeks WHERE id = 3"
        ).fetchone()
        latest = loaded.execute(
            "SELECT MAX(kickoff_time) k FROM fixtures WHERE gameweek = 3"
        ).fetchone()
        assert row["k"] == latest["k"]

    def test_an_unscheduled_fixture_keeps_a_null_gameweek(self, synthetic):
        """The API reports a fixture with no date as `event: null`."""
        row = synthetic.execute(
            "SELECT gameweek FROM fixtures WHERE gameweek IS NULL"
        ).fetchone()
        assert row is not None


class TestBlanksAndDoubles:
    """Built from constructed payloads: the real season has none yet."""

    def test_an_ordinary_gameweek_gives_every_club_one_fixture(self, synthetic):
        assert set(fixtures_per_team(synthetic, 1).values()) == {1}

    def test_a_double_gameweek_is_detected(self, synthetic):
        assert double_teams(synthetic, 4) == {1, 2}

    def test_a_blank_gameweek_is_detected(self, synthetic):
        """Clubs without a fixture have no row at all, so they must be filled
        in from the team list rather than silently omitted."""
        assert blank_teams(synthetic, 4) == {3, 4}

    def test_players_inherit_their_clubs_fixture_count(self, synthetic):
        counts = player_fixture_counts(synthetic, 4)
        assert counts[1] == 2, "this player's club plays twice"
        assert counts[8] == 0, "this player's club blanks"

    def test_a_gameweek_with_no_fixtures_at_all_blanks_everyone(self, synthetic):
        assert blank_teams(synthetic, 30) == {1, 2, 3, 4, 5, 6}

    def test_the_real_season_currently_has_neither(self, loaded):
        """A guard on the recording: if this starts failing, the fixture list
        has gained a blank or a double and can replace the synthetic payloads."""
        for gameweek in range(1, 7):
            counts = fixtures_per_team(loaded, gameweek)
            assert set(counts.values()) == {1}, f"GW{gameweek} is no longer a clean week"


class TestLiveResults:
    def test_a_single_gameweek_stores_one_row_per_player(self, loaded):
        rows = ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        assert rows > 0
        played = loaded.execute(
            "SELECT * FROM player_gameweek_stats"
            " WHERE gameweek = 3 AND minutes > 0 LIMIT 1"
        ).fetchone()
        assert played["total_points"] != 0

    def test_defensive_contribution_stats_are_kept(self, loaded):
        """The four DefCon columns are what the projection model learns from,
        so a rename upstream must fail here rather than zero them silently."""
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        row = loaded.execute(
            "SELECT * FROM player_gameweek_stats"
            " WHERE gameweek = 3 AND defensive_contribution > 0 LIMIT 1"
        ).fetchone()
        assert row is not None, "no player recorded a defensive contribution"
        assert row["clearances_blocks_interceptions"] + row["tackles"] + row["recoveries"] > 0

    def test_expected_stats_are_stored_as_numbers(self, loaded):
        """The API sends these as strings like \"0.64\"."""
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        row = loaded.execute(
            "SELECT expected_goals x FROM player_gameweek_stats"
            " WHERE gameweek = 3 AND expected_goals > 0 LIMIT 1"
        ).fetchone()
        assert isinstance(row["x"], float) and row["x"] > 0

    def test_saves_and_bonus_are_kept(self, loaded):
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        row = loaded.execute(
            "SELECT * FROM player_gameweek_stats WHERE gameweek = 3 AND saves > 0 LIMIT 1"
        ).fetchone()
        assert row is not None
        assert row["bps"] > 0

    def test_a_player_on_zero_minutes_still_gets_a_row(self, loaded):
        """They had a fixture and did not play, which is what the auto-sub
        rules need to distinguish from a blank."""
        ingest_live_gameweek(loaded, 3, load("event_3_live.json"))
        row = loaded.execute(
            "SELECT * FROM player_gameweek_stats WHERE gameweek = 3 AND minutes = 0 LIMIT 1"
        ).fetchone()
        assert row is not None

    def test_reingesting_live_data_updates_in_place(self, loaded):
        """The live job polls repeatedly during a gameweek, so the same rows
        are rewritten as scores move."""
        payload = load("event_3_live.json")
        first = ingest_live_gameweek(loaded, 3, payload)

        target = next(e for e in payload["elements"] if e["stats"]["minutes"] > 0)
        target["stats"]["total_points"] += 4
        target["stats"]["bonus"] = 3
        second = ingest_live_gameweek(loaded, 3, payload)

        assert first == second, "polling must not duplicate rows"
        row = loaded.execute(
            "SELECT total_points p FROM player_gameweek_stats"
            " WHERE player_id = ? AND gameweek = 3",
            (target["id"],),
        ).fetchone()
        assert row["p"] == target["stats"]["total_points"]

    def test_a_malformed_live_payload_is_rejected(self, loaded):
        with pytest.raises(IngestError, match="elements"):
            ingest_live_gameweek(loaded, 3, {})


class TestDoubleGameweekLive:
    """Constructed: no double gameweek exists in the real data yet."""

    def test_a_double_gameweek_stores_a_row_per_fixture(self, synthetic):
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        rows = synthetic.execute(
            "SELECT fixture_id FROM player_gameweek_stats"
            " WHERE player_id = 1 AND gameweek = 4 ORDER BY fixture_id"
        ).fetchall()
        assert [r["fixture_id"] for r in rows] == [10, 11]

    def test_minutes_sum_to_the_gameweek_total(self, synthetic):
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        row = synthetic.execute(
            "SELECT SUM(minutes) m FROM player_gameweek_stats"
            " WHERE player_id = 1 AND gameweek = 4"
        ).fetchone()
        assert row["m"] == 180

    def test_a_blank_player_gets_no_row(self, synthetic):
        """No fixture means nothing to store, which is what makes them read as
        zero minutes when the auto-sub rules run."""
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        row = synthetic.execute(
            "SELECT COUNT(*) c FROM player_gameweek_stats"
            " WHERE player_id = 8 AND gameweek = 4"
        ).fetchone()
        assert row["c"] == 0

    def test_a_player_who_missed_the_first_of_two_fixtures(self, synthetic):
        ingest_live_gameweek(synthetic, 4, load("synthetic_event_4_live.json"))
        rows = {
            r["fixture_id"]: r["minutes"]
            for r in synthetic.execute(
                "SELECT fixture_id, minutes FROM player_gameweek_stats"
                " WHERE player_id = 6 AND gameweek = 4"
            )
        }
        assert rows == {10: 0, 11: 62}


class TestIngestLog:
    def test_each_ingest_is_logged(self, db, bootstrap):
        ingest_teams(db, bootstrap)
        ingest_gameweeks(db, bootstrap)
        rows = db.execute("SELECT endpoint, status FROM ingest_log ORDER BY id").fetchall()
        assert [r["endpoint"] for r in rows] == [
            "bootstrap-static/teams", "bootstrap-static/events"
        ]
        assert all(r["status"] == "ok" for r in rows)

    def test_a_failed_ingest_leaves_no_partial_rows(self, db, bootstrap):
        """The whole refresh is one transaction, so a bad element cannot leave
        half the player table updated."""
        ingest_teams(db, bootstrap)
        bootstrap["elements"][4]["element_type"] = 9
        with pytest.raises(IngestError):
            ingest_players(db, bootstrap)
        assert db.execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 0
