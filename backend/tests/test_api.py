"""The read-only HTTP API.

Everything the frontend needs to draw a gameweek is assembled server-side, so
these tests check the shape of what comes out rather than re-testing the rules
behind it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fplai.api import app as api_app
from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from fplai.jobs.backfill import backfill
from fplai.jobs.score import score_season

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
    backfill(connection, through_gameweek=3, horizon=3)
    score_season(connection)
    yield connection
    connection.close()


@pytest.fixture
def client(db):
    api_app.app.dependency_overrides[api_app.get_db] = lambda: db
    yield TestClient(api_app.app)
    api_app.app.dependency_overrides.clear()


class TestHealthAndIndex:
    def test_health_reports_what_is_loaded(self, client):
        body = client.get("/api/health").json()
        assert body["ok"] is True
        assert body["players"] > 0
        assert body["current_gameweek"] == 3

    def test_both_models_are_listed(self, client):
        ids = {row["id"] for row in client.get("/api/models").json()}
        assert ids == {"manager", "best_xi"}

    def test_gameweeks_say_which_have_picks(self, client):
        body = client.get("/api/gameweeks").json()
        assert body["current"] == 3
        with_picks = [g for g in body["gameweeks"] if g["has_picks"]]
        assert [g["id"] for g in with_picks] == [1, 2, 3]

    def test_the_landing_gameweek_is_the_latest_with_picks(self, client):
        """Not simply the API's current gameweek: between a deadline and the
        job that locks picks there is nothing to show for it."""
        assert client.get("/api/gameweeks").json()["current"] == 3


class TestGameweekView:
    def test_a_squad_comes_back_as_eleven_and_four(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        assert len(view["starters"]) == 11
        assert len(view["bench"]) == 4

    def test_the_bench_keeper_is_first(self, client):
        view = client.get("/api/best_xi/gameweek/3").json()
        assert view["bench"][0]["position"] == "GKP"

    def test_every_player_carries_what_the_pitch_needs(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        for player in view["starters"] + view["bench"]:
            assert set(player) >= {
                "name", "position", "team", "shirt", "points", "multiplier",
                "is_captain", "is_vice_captain", "subbed_on", "subbed_off",
                "fixtures", "reason",
            }

    def test_shirt_urls_use_the_team_code_and_keeper_variant(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        keeper = next(p for p in view["starters"] if p["position"] == "GKP")
        outfield = next(p for p in view["starters"] if p["position"] != "GKP")
        assert f"shirt_{keeper['team_code']}_1-" in keeper["shirt"]
        assert f"shirt_{outfield['team_code']}-" in outfield["shirt"]

    def test_the_captain_is_flagged_and_doubled(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        captains = [p for p in view["starters"] if p["is_captain"]]
        assert len(captains) == 1
        assert captains[0]["multiplier"] in (2, 3)

    def test_exactly_one_vice_captain_is_flagged(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        assert sum(1 for p in view["starters"] if p["is_vice_captain"]) == 1

    def test_bench_players_score_nothing_without_a_boost(self, client):
        view = client.get("/api/best_xi/gameweek/3").json()
        if view["chip"] != "bboost":
            for player in view["bench"]:
                if not player["subbed_on"]:
                    assert player["multiplier"] == 0
                    assert player["total"] == 0

    def test_a_gameweek_carries_its_points_and_the_official_average(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        assert view["points"] is not None
        assert view["average"] > 0
        assert view["margin"] == view["points"] - view["average"]
        assert view["beat_average"] == (view["points"] > view["average"])

    def test_the_provisional_or_final_label_is_present(self, client):
        view = client.get("/api/manager/gameweek/3").json()
        assert view["status"] in ("Provisional", "Final")

    def test_the_manager_view_carries_transfers_and_state(self, client):
        view = client.get("/api/manager/gameweek/2").json()
        assert "transfers" in view
        assert "bank" in view and "free_transfers" in view
        for transfer in view["transfers"]:
            assert set(transfer["comparison"]) >= {"out", "in", "projected_gain"}

    def test_best_xi_has_no_transfers_or_chips(self, client):
        view = client.get("/api/best_xi/gameweek/3").json()
        assert "transfers" not in view
        assert view["chip"] is None

    def test_an_unknown_model_is_rejected(self, client):
        response = client.get("/api/nonsense/gameweek/3")
        assert response.status_code == 404
        assert "unknown model" in response.json()["detail"]

    def test_a_gameweek_without_picks_is_a_404(self, client):
        assert client.get("/api/manager/gameweek/30").status_code == 404


class TestSeasonView:
    def test_season_totals_are_reported(self, client):
        body = client.get("/api/manager/season").json()
        assert body["gameweeks_played"] == 3
        assert body["total_points"] == sum(row["points"] for row in body["history"])

    def test_the_cumulative_series_is_aligned_for_charting(self, client):
        body = client.get("/api/best_xi/season").json()
        assert len(body["cumulative"]) == body["gameweeks_played"]
        for row in body["cumulative"]:
            assert {"gameweek", "points", "average"} <= set(row)

    def test_chips_are_reported_for_the_manager_only(self, client):
        assert client.get("/api/best_xi/season").json()["chips_used"] == []
        assert "chips_used" in client.get("/api/manager/season").json()

    def test_hits_are_tracked_separately(self, client):
        body = client.get("/api/manager/season").json()
        assert body["total_transfer_cost"] == sum(
            row["transfer_cost"] for row in body["history"]
        )

    def test_the_summary_puts_both_models_side_by_side(self, client):
        body = client.get("/api/summary").json()
        assert set(body["models"]) == {"manager", "best_xi"}
        assert body["current_gameweek"] == 3


class TestPlayerDetail:
    def test_a_player_sheet_includes_their_projection(self, client):
        view = client.get("/api/best_xi/gameweek/3").json()
        element = view["starters"][0]["element"]
        detail = client.get(f"/api/best_xi/gameweek/3/player/{element}").json()
        assert detail["element"] == element
        assert "reason" in detail
        assert set(detail["projection"]) >= {
            "expected_points", "expected_minutes", "probability_of_start",
            "clean_sheet_probability", "defcon_probability",
        }

    def test_a_player_not_in_the_squad_is_a_404(self, client):
        assert client.get("/api/best_xi/gameweek/3/player/999999").status_code == 404


class TestFrontendServing:
    """When the frontend has been built, the API serves it too, so the whole
    site runs as one service on one URL."""

    def test_an_unknown_api_path_is_a_json_404(self, client):
        """Not an HTML page with a 200. A typo'd endpoint answered with
        index.html is a miserable thing to debug from the frontend."""
        response = client.get("/api/does-not-exist")
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/json")

    def test_api_routes_still_win_over_the_catch_all(self, client):
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/manager/season").status_code == 200
