"""A seed run end to end, against recorded API responses.

The unit tests around the setup stages check what each step decides. This
checks the thing the site actually depends on: that a database seeded from
nothing ends up with real points in it. Both bugs that reached the published
site would have been caught here -- the results never being fetched, and then
the scoring being skipped once they were.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from fplai.config import Settings
from fplai.data.client import FPLClient
from fplai.data.db import connect, init_db
from fplai.jobs.score import gameweeks_with_a_stale_score
from fplai.jobs.seed import current_stage, seed

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def routes(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("bootstrap-static/"):
        return httpx.Response(200, json=load("bootstrap_static.json"))
    if "fixtures" in path:
        return httpx.Response(200, json=load("fixtures.json"))
    if "/event/" in path and path.endswith("/live/"):
        return httpx.Response(200, json=load("event_3_live.json"))
    if "element-summary" in path:
        return httpx.Response(
            200,
            json={
                "history": [],
                "history_past": [{"season_name": "2025/26", "minutes": 900}],
                "fixtures": [],
            },
        )
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


class TestSeedEndToEnd:
    def test_a_seeded_database_holds_real_points(self, db, client):
        """The one that matters. Every step can report success and still leave
        a season of noughts, which is what got published: scoring a squad
        against no results does not fail, it just comes out as nothing."""
        seed(db, client)

        scored = db.execute(
            "SELECT COUNT(*) c FROM gameweek_results WHERE points > 0"
        ).fetchone()["c"]
        assert scored > 0, "a seeded season scored nothing"

    def test_the_results_themselves_are_stored(self, db, client):
        seed(db, client)
        rows = db.execute(
            "SELECT COUNT(*) c FROM player_gameweek_stats"
        ).fetchone()["c"]
        assert rows > 0

    def test_nothing_is_left_stale(self, db, client):
        """Fetching the results moves the setup on, so a check made afterwards
        sees a finished database and skips the scoring those results were
        fetched for. That is the exact shape of the second bug."""
        seed(db, client)
        assert gameweeks_with_a_stale_score(db) == []

    def test_it_reports_itself_finished(self, db, client):
        seed(db, client)
        assert current_stage(db) == "ready"

    def test_running_it_again_is_harmless(self, db, client):
        seed(db, client)
        before = db.execute("SELECT COUNT(*) c FROM locked_picks").fetchone()["c"]

        seed(db, client)
        after = db.execute("SELECT COUNT(*) c FROM locked_picks").fetchone()["c"]
        assert after == before
