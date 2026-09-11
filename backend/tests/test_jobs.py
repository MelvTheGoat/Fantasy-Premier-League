"""The refresh jobs, driven through a mock transport rather than the live API."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from fplai.config import Settings
from fplai.data.client import FPLClient
from fplai.data.db import connect, init_db
from fplai.data.repository import current_gameweek, gameweek_average, load_results
from fplai.jobs.refresh import finalise_gameweek, refresh_live, refresh_reference

FIXTURES = Path(__file__).parent / "fixtures"
UK = ZoneInfo("Europe/London")


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def routes(request: httpx.Request) -> httpx.Response:
    """Serves the recorded payloads at the real API's paths."""
    path = request.url.path
    if path.endswith("bootstrap-static/"):
        return httpx.Response(200, json=load("bootstrap_static.json"))
    if "fixtures" in path:
        return httpx.Response(200, json=load("fixtures.json"))
    if path.endswith("/event/3/live/"):
        return httpx.Response(200, json=load("event_3_live.json"))
    if path.endswith("/event/4/live/"):
        return httpx.Response(200, json=load("event_4_live.json"))
    return httpx.Response(404)


@pytest.fixture
def db():
    connection = connect(":memory:")
    init_db(connection)
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


class TestRefreshReference:
    def test_everything_is_ingested_in_one_pass(self, db, client):
        counts = refresh_reference(db, client, gameweek=4)
        assert counts == {"teams": 6, "gameweeks": 38, "players": 26, "fixtures": 16}
        assert current_gameweek(db) == 4

    def test_prices_are_snapshotted_against_the_given_gameweek(self, db, client):
        refresh_reference(db, client, gameweek=5)
        row = db.execute(
            "SELECT COUNT(*) c FROM player_prices WHERE gameweek = 5"
        ).fetchone()
        assert row["c"] == 26

    def test_running_it_twice_changes_nothing(self, db, client):
        refresh_reference(db, client, gameweek=4)
        refresh_reference(db, client, gameweek=4)
        assert db.execute("SELECT COUNT(*) c FROM players").fetchone()["c"] == 26
        assert db.execute("SELECT COUNT(*) c FROM fixtures").fetchone()["c"] == 16


class TestRefreshLive:
    def test_the_current_gameweek_is_used_by_default(self, db, client):
        refresh_reference(db, client, gameweek=4)
        assert refresh_live(db, client) > 0
        assert load_results(db, 4)[1].minutes == 180

    def test_an_explicit_gameweek_overrides_it(self, db, client):
        refresh_reference(db, client, gameweek=4)
        refresh_live(db, client, gameweek=3)
        assert load_results(db, 3)[1].minutes == 90

    def test_nothing_happens_without_a_current_gameweek(self, db, client):
        assert refresh_live(db, client) == 0

    def test_live_data_is_never_served_from_cache(self, db, client, monkeypatch):
        """Scores move during a match, so a cached copy would be stale."""
        seen = []
        original = client.get

        def spy(path, *, ttl_seconds=None):
            seen.append((path, ttl_seconds))
            return original(path, ttl_seconds=ttl_seconds)

        monkeypatch.setattr(client, "get", spy)
        refresh_reference(db, client, gameweek=4)
        refresh_live(db, client, gameweek=4)
        assert ("event/4/live/", 0) in seen


class TestFinalise:
    def test_a_gameweek_before_lockdown_is_left_alone(self, db, client):
        refresh_reference(db, client, gameweek=4)
        # GW4's last kickoff is 19:00 on 14 September, so lockdown is 09:00 on
        # the 15th. An hour before that, nothing may be finalised.
        before = datetime(2026, 9, 15, 8, 0, tzinfo=UK)
        assert finalise_gameweek(db, client, 4, now=before) is False

    def test_a_gameweek_past_lockdown_is_finalised(self, db, client):
        refresh_reference(db, client, gameweek=4)
        after = datetime(2026, 9, 15, 9, 0, tzinfo=UK)
        assert finalise_gameweek(db, client, 4, now=after) is True
        assert load_results(db, 4)[1].total_points > 0

    def test_finalising_records_the_official_average(self, db, client):
        refresh_reference(db, client, gameweek=3)
        after = datetime(2026, 9, 1, 9, 0, tzinfo=UK)
        assert finalise_gameweek(db, client, 3, now=after) is True
        assert gameweek_average(db, 3) == 55

    def test_finalising_snapshots_the_next_gameweeks_prices(self, db, client):
        """Prices move overnight after a gameweek, and the next deadline's
        selling prices depend on catching them."""
        refresh_reference(db, client, gameweek=3)
        finalise_gameweek(db, client, 3, now=datetime(2026, 9, 1, 9, 0, tzinfo=UK))
        row = db.execute(
            "SELECT COUNT(*) c FROM player_prices WHERE gameweek = 4"
        ).fetchone()
        assert row["c"] == 26

    def test_a_gameweek_with_no_fixtures_is_never_final(self, db, client):
        refresh_reference(db, client, gameweek=4)
        now = datetime(2027, 6, 1, tzinfo=timezone.utc)
        assert finalise_gameweek(db, client, 30, now=now) is False
