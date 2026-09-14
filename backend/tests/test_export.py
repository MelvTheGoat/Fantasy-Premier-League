"""Publishing the site as static files, and keeping the stored season small.

The two belong together: the export is what gets served, so it is also the
check that pruning the database did not quietly remove something the site
needs.
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
from fplai.jobs.export import SUFFIX, api_paths, export_site
from fplai.jobs.prune import prunable, prune_projections
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
def site(db, tmp_path):
    export_site(db, tmp_path / "site")
    return tmp_path / "site"


def read(site: Path, path: str):
    return json.loads((site / (path.lstrip("/") + SUFFIX)).read_text())


class TestExport:
    def test_every_stored_pick_is_reachable(self, db, site):
        """The site is built from what is stored, so a player in a squad always
        has a detail sheet behind them."""
        for path in api_paths(db):
            assert (site / (path.lstrip("/") + SUFFIX)).is_file(), path

    def test_a_gameweek_is_a_file_and_also_a_directory(self, site):
        """`/gameweek/3` is a response, and the prefix of
        `/gameweek/3/player/427`. One name cannot be both on a filesystem, so
        the extension is what keeps them apart -- lose it and the export half
        writes itself and then fails."""
        assert (site / "api/manager/gameweek/3.json").is_file()
        assert (site / "api/manager/gameweek/3").is_dir()

    def test_the_files_say_what_the_api_says(self, db, site):
        """Rendered through the API rather than rebuilt from the views, so the
        static copy cannot drift from the live one."""
        api_app.app.dependency_overrides[api_app.get_db] = lambda: db
        try:
            client = TestClient(api_app.app)
            for path in ("/api/gameweeks", "/api/summary", "/api/manager/season"):
                assert read(site, path) == client.get(path).json(), path
        finally:
            api_app.app.dependency_overrides.clear()

    def test_a_rebuild_does_not_leave_last_weeks_files_behind(self, db, tmp_path):
        destination = tmp_path / "site"
        export_site(db, destination)
        stale = destination / "api/manager/gameweek/99.json"
        stale.write_text("{}")

        export_site(db, destination)
        assert not stale.exists()

    def test_the_frontend_is_published_with_it(self, db, tmp_path):
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html>")
        (dist / "assets" / "app.js").write_text("//")

        destination = tmp_path / "site"
        counts = export_site(db, destination, frontend_dist=dist)

        assert (destination / "index.html").exists()
        assert (destination / "assets" / "app.js").exists()
        assert counts["frontend_files"] == 2

    def test_jekyll_is_kept_away_from_the_output(self, site):
        """GitHub Pages runs a static site through Jekyll unless told not to,
        and Jekyll drops anything whose name starts with an underscore."""
        assert (site / ".nojekyll").exists()


class TestPrune:
    def test_the_lookahead_is_what_gets_dropped(self, db):
        """Each deadline projects several gameweeks ahead to judge whether a
        hit pays for itself. Once the deadline passes, that working-out is
        spent -- and it is most of the database."""
        assert prunable(db, before_gameweek=4) > 0

        before = db.execute("SELECT COUNT(*) c FROM projections").fetchone()["c"]
        deleted = prune_projections(db, before_gameweek=4)["deleted"]
        after = db.execute("SELECT COUNT(*) c FROM projections").fetchone()["c"]

        assert after == before - deleted
        assert after > 0

    def test_the_gameweek_being_decided_keeps_its_horizon(self, db):
        """Pruning up to GW3 must not take GW3's own lookahead with it: that is
        the horizon the Manager is using right now."""
        prune_projections(db, before_gameweek=3)

        kept = db.execute(
            "SELECT COUNT(*) c FROM projections"
            " WHERE made_for_gameweek = 3 AND target_gameweek > 3"
        ).fetchone()["c"]
        assert kept > 0

    def test_pruning_changes_nothing_the_site_shows(self, db, tmp_path):
        """The whole safety argument in one test. If the published site is
        identical before and after, the rows that went were genuinely unread."""
        export_site(db, tmp_path / "before")
        prune_projections(db, before_gameweek=4)
        export_site(db, tmp_path / "after")

        for path in api_paths(db):
            relative = path.lstrip("/") + SUFFIX
            assert (tmp_path / "before" / relative).read_text() == (
                tmp_path / "after" / relative
            ).read_text(), path

    def test_pruning_twice_finds_nothing_the_second_time(self, db):
        prune_projections(db, before_gameweek=4)
        assert prunable(db, before_gameweek=4) == 0
