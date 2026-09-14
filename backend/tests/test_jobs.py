"""The refresh jobs, driven through a mock transport rather than the live API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from fplai.config import Settings
from fplai.data.client import FPLClient
from fplai.data.db import connect, init_db
from fplai.data.repository import current_gameweek, gameweek_average, load_results
from fplai.jobs.refresh import (
    finalise_gameweek,
    price_snapshot_gameweek,
    refresh_live,
    refresh_reference,
)

FIXTURES = Path(__file__).parent / "fixtures"
UK = ZoneInfo("Europe/London")


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


#: Counts in the recorded payloads, and the gameweek they are current for.
RECORDED = {"teams": 20, "gameweeks": 38, "players": 43, "fixtures": 60}
CURRENT_GAMEWEEK = 3

#: GW3's last fixture kicked off on 6 September, so lockdown is 09:00 UK on
#: the 7th.
GW3_LOCKDOWN = datetime(2026, 9, 7, 9, 0, tzinfo=UK)


def routes(request: httpx.Request) -> httpx.Response:
    """Serves the recorded payloads at the real API's paths."""
    path = request.url.path
    if path.endswith("bootstrap-static/"):
        return httpx.Response(200, json=load("bootstrap_static.json"))
    if "fixtures" in path:
        return httpx.Response(200, json=load("fixtures.json"))
    if path.endswith("/event/3/live/"):
        return httpx.Response(200, json=load("event_3_live.json"))
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
        counts = refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        assert counts == RECORDED
        assert current_gameweek(db) == CURRENT_GAMEWEEK

    def test_prices_are_snapshotted_against_the_given_gameweek(self, db, client):
        refresh_reference(db, client, gameweek=5)
        row = db.execute(
            "SELECT COUNT(*) c FROM player_prices WHERE gameweek = 5"
        ).fetchone()
        assert row["c"] == RECORDED["players"]

    def test_running_it_twice_changes_nothing(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        for table in ("players", "fixtures"):
            expected = RECORDED[table]
            count = db.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()["c"]
            assert count == expected


class TestPriceSnapshotGameweek:
    """Which gameweek a refresh records prices against."""

    def test_the_next_deadline_is_used(self):
        events = [
            {"id": 3, "is_current": True, "is_next": False},
            {"id": 4, "is_current": False, "is_next": True},
        ]
        assert price_snapshot_gameweek({"events": events}) == 4

    def test_the_current_gameweek_is_the_fallback(self):
        """At the end of the season there is no next gameweek."""
        events = [{"id": 38, "is_current": True, "is_next": False}]
        assert price_snapshot_gameweek({"events": events}) == 38

    def test_an_empty_payload_yields_nothing(self):
        assert price_snapshot_gameweek({"events": []}) is None

    def test_a_cold_database_still_snapshots_prices(self, db, client):
        """The gameweek comes from the payload, not the database. On a first
        run the database has no gameweeks yet, so looking it up there would
        silently skip the snapshot and leave the models unable to price a
        past decision."""
        refresh_reference(db, client)
        rows = db.execute(
            "SELECT gameweek, COUNT(*) c FROM player_prices GROUP BY gameweek"
        ).fetchall()
        assert [(r["gameweek"], r["c"]) for r in rows] == [
            (CURRENT_GAMEWEEK + 1, RECORDED["players"])
        ]


class TestRefreshLive:
    def test_the_current_gameweek_is_used_by_default(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        assert refresh_live(db, client) > 0
        results = load_results(db, CURRENT_GAMEWEEK)
        assert any(r.minutes > 0 for r in results.values())

    def test_an_explicit_gameweek_overrides_it(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        refresh_live(db, client, gameweek=CURRENT_GAMEWEEK)
        assert load_results(db, CURRENT_GAMEWEEK)

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
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        refresh_live(db, client, gameweek=CURRENT_GAMEWEEK)
        assert (f"event/{CURRENT_GAMEWEEK}/live/", 0) in seen


class TestFinalise:
    def test_a_gameweek_before_lockdown_is_left_alone(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        before = GW3_LOCKDOWN - timedelta(minutes=1)
        assert finalise_gameweek(db, client, CURRENT_GAMEWEEK, now=before) is False

    def test_a_gameweek_past_lockdown_is_finalised(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        assert finalise_gameweek(db, client, CURRENT_GAMEWEEK, now=GW3_LOCKDOWN) is True
        results = load_results(db, CURRENT_GAMEWEEK)
        assert any(r.total_points > 0 for r in results.values())

    def test_finalising_records_the_official_average(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        assert finalise_gameweek(db, client, CURRENT_GAMEWEEK, now=GW3_LOCKDOWN) is True
        assert gameweek_average(db, CURRENT_GAMEWEEK) > 0

    def test_finalising_snapshots_the_next_gameweeks_prices(self, db, client):
        """Prices move overnight after a gameweek, and the next deadline's
        selling prices depend on catching them."""
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        finalise_gameweek(db, client, CURRENT_GAMEWEEK, now=GW3_LOCKDOWN)
        row = db.execute(
            "SELECT COUNT(*) c FROM player_prices WHERE gameweek = ?",
            (CURRENT_GAMEWEEK + 1,),
        ).fetchone()
        assert row["c"] == RECORDED["players"]

    def test_a_gameweek_with_no_fixtures_is_never_final(self, db, client):
        refresh_reference(db, client, gameweek=CURRENT_GAMEWEEK)
        now = datetime(2027, 6, 1, tzinfo=UTC)
        assert finalise_gameweek(db, client, 30, now=now) is False


class TestDefaultPaths:
    """Where the database goes when nothing says otherwise."""

    def test_a_source_checkout_keeps_its_data_beside_the_code(self):
        from fplai.config import PROJECT_ROOT, _default_data_root

        assert _default_data_root() == PROJECT_ROOT / "data"

    def test_an_installed_copy_does_not_write_into_site_packages(self, monkeypatch, tmp_path):
        """An installed package has no pyproject.toml beside it, and a season
        written into site-packages is silently discarded by any container or CI
        runner -- which is exactly how it fails: the jobs all succeed and the
        result disappears."""
        from fplai import config

        installed = tmp_path / "site-packages" / "fplai"
        installed.mkdir(parents=True)
        monkeypatch.setattr(config, "PROJECT_ROOT", installed.parent)
        monkeypatch.chdir(tmp_path)

        assert config._default_data_root() == tmp_path / "data"
