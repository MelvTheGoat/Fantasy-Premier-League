"""Writing the whole site out as static files.

The site is read-only -- nobody types into it, and the models run on a
schedule -- so it does not need a server at runtime, only a publisher. This
renders every URL the frontend can ask for to a file at that same path, which
a static host can then serve.

The responses are taken from the API itself rather than rebuilt from the views,
so the static copy cannot quietly drift from what the live API would have said.
If an endpoint changes shape, this changes with it.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

from ..api import views

#: Every exported response gets this extension, and the frontend is built
#: knowing it. Not decoration: `/api/manager/gameweek/1` is a response and also
#: the prefix of `/api/manager/gameweek/1/player/427`, and one name cannot be
#: both a file and a directory. The extension separates them, and it is what
#: makes the host serve them as JSON.
SUFFIX = ".json"

#: Files the frontend needs that are not API responses. `.nojekyll` stops
#: GitHub Pages running the output through Jekyll, which would drop any file
#: or directory whose name begins with an underscore.
MARKER_FILES = (".nojekyll",)


def _gameweeks_with_picks(connection: sqlite3.Connection) -> dict[str, list[int]]:
    rows = connection.execute(
        "SELECT model_id, gameweek FROM locked_picks"
        " GROUP BY model_id, gameweek ORDER BY model_id, gameweek"
    )
    found: dict[str, list[int]] = {model: [] for model in views.MODELS}
    for row in rows:
        found.setdefault(row["model_id"], []).append(row["gameweek"])
    return found


def _squad(connection: sqlite3.Connection, model: str, gameweek: int) -> list[int]:
    return [
        row["player_id"]
        for row in connection.execute(
            "SELECT player_id FROM locked_picks"
            " WHERE model_id = ? AND gameweek = ? ORDER BY squad_position",
            (model, gameweek),
        )
    ]


def api_paths(connection: sqlite3.Connection) -> list[str]:
    """Every API URL this database can answer.

    Built from the picks that are actually stored, so a gameweek nobody played
    and a player nobody owned are never requested -- the export is the site's
    real surface, not the route table's.
    """
    paths = ["/api/gameweeks", "/api/summary", "/api/setup", "/api/models"]

    for model, gameweeks in _gameweeks_with_picks(connection).items():
        paths.append(f"/api/{model}/season")
        for gameweek in gameweeks:
            paths.append(f"/api/{model}/gameweek/{gameweek}")
            paths.extend(
                f"/api/{model}/gameweek/{gameweek}/player/{element}"
                for element in _squad(connection, model, gameweek)
            )

    return paths


def export_api(
    connection: sqlite3.Connection, destination: Path
) -> dict[str, Any]:
    """Render every API response into `destination`, at its own path."""
    # Imported here because it is only needed when exporting, and it pulls in
    # the app -- which mounts the frontend -- as a side effect.
    from fastapi.testclient import TestClient

    from ..api.app import app, get_db

    app.dependency_overrides[get_db] = lambda: connection
    written, failed = 0, []
    try:
        client = TestClient(app)
        for path in api_paths(connection):
            response = client.get(path)
            if response.status_code != 200:
                failed.append((path, response.status_code))
                continue

            target = destination / (path.lstrip("/") + SUFFIX)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(response.json()))
            written += 1
    finally:
        app.dependency_overrides.clear()

    if failed:
        # Every path came from stored picks, so a non-200 means the database
        # and the API disagree about what exists. Publishing a site with holes
        # in it would hide that until someone tapped the wrong player.
        raise RuntimeError(f"the API refused paths it should serve: {failed}")

    return {"api_files": written}


def export_site(
    connection: sqlite3.Connection,
    destination: Path,
    *,
    frontend_dist: Path | None = None,
) -> dict[str, Any]:
    """Build a complete, deployable directory: the frontend and the API.

    Cleared first, so a gameweek's worth of stale files cannot survive into a
    deployment and be served alongside the current ones.
    """
    destination = Path(destination)
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    counts: dict[str, Any] = {"frontend_files": 0}

    if frontend_dist and (frontend_dist / "index.html").exists():
        shutil.copytree(frontend_dist, destination, dirs_exist_ok=True)
        counts["frontend_files"] = sum(
            1 for path in destination.rglob("*") if path.is_file()
        )

    counts.update(export_api(connection, destination))

    for marker in MARKER_FILES:
        (destination / marker).touch()

    return counts


__all__ = ["MARKER_FILES", "SUFFIX", "api_paths", "export_api", "export_site"]
