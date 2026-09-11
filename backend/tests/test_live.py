"""The live-update job, which keeps scores current during a gameweek."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from fplai.config import Settings
from fplai.data.client import FPLClient
from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from fplai.jobs.backfill import backfill
from fplai.jobs.live import update_live

FIXTURES = Path(__file__).parent / "fixtures"
UK = ZoneInfo("Europe/London")


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def routes(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("bootstrap-static/"):
        return httpx.Response(200, json=load("bootstrap_static.json"))
    if "fixtures" in path:
        return httpx.Response(200, json=load("fixtures.json"))
    if "/live/" in path:
        return httpx.Response(200, json=load("event_3_live.json"))
    return httpx.Response(404)


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
    yield connection
    connection.close()


@pytest.fixture
def client(tmp_path):
    config = Settings(
        database_path=tmp_path / "db.sqlite3",
        cache_dir=tmp_path / "cache",
        min_request_interval=0.0,
    )
    with FPLClient(config, transport=httpx.MockTransport(routes)) as fpl:
        yield fpl


class TestUpdateLive:
    def test_a_pass_refreshes_points_and_rescores_both_models(self, db, client):
        result = update_live(db, client, gameweek=3)
        assert result["gameweek"] == 3
        assert result["rows"] > 0
        assert set(result["scores"]) == {"manager", "best_xi"}

    def test_scores_are_written_to_the_results_table(self, db, client):
        update_live(db, client, gameweek=3)
        rows = db.execute(
            "SELECT model_id, points FROM gameweek_results WHERE gameweek = 3"
        ).fetchall()
        assert len(rows) == 2

    def test_the_current_gameweek_is_used_by_default(self, db, client):
        assert update_live(db, client)["gameweek"] == 3

    def test_nothing_happens_without_a_current_gameweek(self, db, client):
        db.execute("UPDATE gameweeks SET is_current = 0")
        result = update_live(db, client)
        assert result["gameweek"] is None
        assert result["rows"] == 0

    def test_a_gameweek_before_lockdown_stays_provisional(self, db, client):
        early = datetime(2020, 1, 1, tzinfo=UTC)
        result = update_live(db, client, gameweek=3, now=early)
        assert result["final"] is False
        assert db.execute(
            "SELECT is_final f FROM gameweek_results WHERE gameweek = 3 LIMIT 1"
        ).fetchone()["f"] == 0

    def test_a_gameweek_past_lockdown_is_marked_final(self, db, client):
        late = datetime(2030, 1, 1, tzinfo=UTC)
        result = update_live(db, client, gameweek=3, now=late)
        assert result["final"] is True
        assert db.execute(
            "SELECT is_final f FROM gameweek_results WHERE gameweek = 3 LIMIT 1"
        ).fetchone()["f"] == 1

    def test_the_lockdown_time_is_reported(self, db, client):
        result = update_live(db, client, gameweek=3)
        assert result["lockdown"] is not None
        assert result["lockdown"].endswith("09:00:00+01:00")

    def test_picks_are_never_rewritten_by_a_live_pass(self, db, client):
        """Scores move during a gameweek; the squad that was locked does not."""
        before = db.execute(
            "SELECT player_id, squad_position, locked_at FROM locked_picks"
            " WHERE gameweek = 3 ORDER BY model_id, squad_position"
        ).fetchall()
        update_live(db, client, gameweek=3)
        after = db.execute(
            "SELECT player_id, squad_position, locked_at FROM locked_picks"
            " WHERE gameweek = 3 ORDER BY model_id, squad_position"
        ).fetchall()
        assert [tuple(row) for row in before] == [tuple(row) for row in after]

    def test_repeated_passes_do_not_duplicate_rows(self, db, client):
        for _ in range(3):
            update_live(db, client, gameweek=3)
        count = db.execute(
            "SELECT COUNT(*) c FROM gameweek_results WHERE gameweek = 3"
        ).fetchone()["c"]
        assert count == 2
