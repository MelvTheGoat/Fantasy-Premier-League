"""The projection pipeline, and the no-leakage rule it exists to enforce.

The season is already under way, so gameweeks have to be replayed. A projection
for GW7 that saw GW7's results would produce a season of picks that look
brilliant and mean nothing, and nothing about the output would reveal it. These
tests are the guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    ingest_element_summary,
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from fplai.model.projections import (
    build_histories,
    fixtures_by_team,
    load_projections,
    penalty_takers,
    project_for_gameweek,
    save_projections,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def db():
    """Real recorded data with GW1-3 results ingested."""
    connection = connect(":memory:")
    init_db(connection)
    bootstrap = load("bootstrap_static.json")
    ingest_teams(connection, bootstrap)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=3)
    ingest_fixtures(connection, load("fixtures.json"))
    ingest_live_gameweek(connection, 3, load("event_3_live.json"))
    yield connection
    connection.close()


class TestFixtureLookup:
    def test_every_club_appears_for_an_ordinary_gameweek(self, db):
        slots = fixtures_by_team(db, 3)
        assert len(slots) == 20
        assert all(len(v) == 1 for v in slots.values())

    def test_home_and_away_are_recorded_from_each_clubs_side(self, db):
        slots = fixtures_by_team(db, 3)
        for team, fixtures in slots.items():
            for fixture in fixtures:
                assert fixture.team == team
                assert fixture.opponent != team

    def test_a_gameweek_with_no_fixtures_yields_nothing(self, db):
        assert fixtures_by_team(db, 99) == {}


class TestNoLeakage:
    def test_a_projection_cannot_see_its_own_gameweek(self, db):
        """The heart of it: GW3's results must be invisible to a GW3 projection."""
        before = build_histories(db, 3)
        after = build_histories(db, 4)

        played = [e for e, h in after.items() if h.minutes > 0]
        assert played, "GW3 results should be visible from GW4"
        assert all(before[e].minutes == 0 for e in played), (
            "no GW3 minutes may be visible to a projection made for GW3"
        )

    def test_later_results_do_not_change_an_earlier_projection(self, db):
        """Ingesting a future gameweek must leave a past projection identical."""
        before = project_for_gameweek(db, 3, horizon=1)

        ingest_live_gameweek(db, 4, load("event_3_live.json"))
        after = project_for_gameweek(db, 3, horizon=1)

        assert {e: p[0].expected_points for e, p in before.items()} == {
            e: p[0].expected_points for e, p in after.items()
        }

    def test_a_projection_is_priced_at_that_gameweeks_prices(self, db):
        """A past decision must be costed as it was, not as it is now."""
        element = next(iter(build_histories(db, 3)))
        db.execute(
            "INSERT OR REPLACE INTO player_prices (player_id, gameweek, now_cost,"
            " captured_at) VALUES (?, 3, 999, '2026-01-01')",
            (element,),
        )
        assert build_histories(db, 3)[element].price == 999

    def test_todays_price_is_the_fallback_when_no_snapshot_exists(self, db):
        histories = build_histories(db, 9)
        assert all(h.price > 0 for h in histories.values())

    def test_team_form_only_counts_finished_earlier_matches(self, db):
        from fplai.model.team_strength import load_team_form

        assert load_team_form(db, 1) == {}
        later = load_team_form(db, 4)
        assert later and all(f.matches == 3 for f in later.values())


class TestHistories:
    def test_every_player_gets_a_history(self, db):
        assert len(build_histories(db, 4)) == 43

    def test_availability_flags_reach_the_model(self, db):
        histories = build_histories(db, 4)
        assert any(h.status == "i" for h in histories.values())
        assert any(h.chance_of_playing is not None for h in histories.values())

    def test_past_seasons_are_picked_up_as_priors(self, db):
        element = next(iter(build_histories(db, 4)))
        ingest_element_summary(db, element, {
            "history": [],
            "history_past": [
                {"season_name": "2025/26", "minutes": 3000, "goals_scored": 20,
                 "assists": 5, "saves": 0, "bps": 700},
            ],
        })
        history = build_histories(db, 4)[element]
        assert history.prior_minutes == 3000
        assert history.prior_goals == 20

    def test_element_summary_supplies_historical_prices(self, db):
        """`history[].value` is the only source of what a player cost in a past
        gameweek, which is what makes a leak-free backfill possible at all."""
        element = next(iter(build_histories(db, 4)))
        ingest_element_summary(db, element, {
            "history": [
                {"round": 1, "value": 55}, {"round": 2, "value": 56},
            ],
            "history_past": [],
        })
        assert build_histories(db, 1)[element].price == 55
        assert build_histories(db, 2)[element].price == 56


class TestProjectionRun:
    def test_every_player_is_projected_over_the_horizon(self, db):
        projections = project_for_gameweek(db, 3, horizon=3)
        assert len(projections) == 43
        assert all(len(p) == 3 for p in projections.values())

    def test_the_horizon_runs_forward_from_the_deadline(self, db):
        projections = project_for_gameweek(db, 3, horizon=4)
        element = next(iter(projections))
        assert [p.gameweek for p in projections[element]] == [3, 4, 5, 6]

    def test_penalty_takers_are_identified(self, db):
        takers = penalty_takers(db)
        assert takers, "the recording should contain first-choice penalty takers"
        orders = {
            row["penalties_order"]
            for row in db.execute(
                "SELECT penalties_order FROM players WHERE id IN"
                f" ({','.join('?' * len(takers))})", tuple(takers)
            )
        }
        assert orders == {1}

    def test_projections_are_ordered_sensibly(self, db):
        """Not a correctness assertion so much as a smoke test: the best
        players should project above the worst."""
        projections = project_for_gameweek(db, 3, horizon=1)
        points = sorted(p[0].expected_points for p in projections.values())
        assert points[-1] > points[0]
        assert points[-1] > 1.0

    def test_an_unavailable_player_projects_nothing(self, db):
        projections = project_for_gameweek(db, 3, horizon=1)
        for row in db.execute("SELECT id FROM players WHERE status IN ('s', 'u', 'i')"):
            assert projections[row["id"]][0].expected_points == pytest.approx(0, abs=1e-9)


class TestPersistence:
    def test_projections_round_trip(self, db):
        projections = project_for_gameweek(db, 3, horizon=2)
        rows = save_projections(db, 3, projections)
        assert rows == 43 * 2

        stored = load_projections(db, 3)
        element = next(iter(projections))
        assert stored[element][3] == pytest.approx(
            projections[element][0].expected_points
        )

    def test_rerunning_replaces_rather_than_duplicates(self, db):
        projections = project_for_gameweek(db, 3, horizon=2)
        save_projections(db, 3, projections)
        save_projections(db, 3, projections)
        count = db.execute("SELECT COUNT(*) c FROM projections").fetchone()["c"]
        assert count == 43 * 2

    def test_runs_for_different_deadlines_are_kept_apart(self, db):
        """Each deadline's projection is a record of what was known then, so
        one must never overwrite another."""
        save_projections(db, 3, project_for_gameweek(db, 3, horizon=1))
        save_projections(db, 4, project_for_gameweek(db, 4, horizon=1))
        assert len(load_projections(db, 3)) == 43
        assert len(load_projections(db, 4)) == 43

    def test_the_model_version_is_recorded(self, db):
        save_projections(db, 3, project_for_gameweek(db, 3, horizon=1))
        versions = {
            row["model_version"]
            for row in db.execute("SELECT DISTINCT model_version FROM projections")
        }
        assert len(versions) == 1 and versions != {None}
