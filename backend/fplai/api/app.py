"""The HTTP API the frontend reads.

Read-only and small on purpose: the models run as scheduled jobs, and this
serves what they already decided. Nothing here computes a projection or picks a
squad, so a slow request can never delay a deadline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from ..config import settings
from ..data.db import connect, init_db
from ..jobs import scheduler
from ..jobs.seed import setup_progress
from . import views

#: Where `npm run build` puts the frontend. When it exists it is served from
#: this same app, so the whole thing deploys as one service on one URL and the
#: frontend's `/api` calls are same-origin. In development Vite serves the
#: frontend itself and proxies `/api` here, so this stays empty and unused.
FRONTEND_DIST = settings.frontend_dist


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start the scheduler alongside the API, when this process is the one
    running the jobs.

    On a host that gives the database a persistent disk there is nowhere else
    for them to run: the disk attaches to one service, and a scheduled-task
    service would get an empty filesystem of its own. Locally it stays off, so
    running the API does not start calling the FPL API on a timer.
    """
    stop = (
        scheduler.start(settings.scheduler_interval)
        if settings.run_scheduler
        else None
    )
    try:
        yield
    finally:
        if stop is not None:
            stop.set()


app = FastAPI(
    title="FPL AI Manager",
    description="Two models playing Fantasy Premier League, scored on real points.",
    version="0.1.0",
    lifespan=lifespan,
)

# The frontend is served separately in development, so it needs to be allowed
# to call this from another origin.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


def get_db() -> Iterator[sqlite3.Connection]:
    connection = connect()
    init_db(connection)
    try:
        yield connection
    finally:
        connection.close()


#: The request-scoped database handle. Written as an annotated dependency
#: rather than a default argument so it reads as part of the type.
Database = Annotated[sqlite3.Connection, Depends(get_db)]


def _require_model(model: str) -> str:
    if model not in views.MODELS:
        raise HTTPException(
            status_code=404,
            detail=f"unknown model '{model}'; expected one of {sorted(views.MODELS)}",
        )
    return model


@app.get("/api/health")
def health(db: Database) -> dict[str, Any]:
    players = db.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    return {
        "ok": True,
        "players": players,
        "current_gameweek": views.current_gameweek(db),
    }


@app.get("/api/setup")
def setup(db: Database) -> dict[str, Any]:
    """How far a fresh deployment has got in filling itself in.

    A new container starts with an empty database and takes a few minutes to
    pull the season down, which without this looks exactly like a broken site.
    """
    return {**setup_progress(db), "scheduler": settings.run_scheduler}


@app.get("/api/models")
def models() -> list[dict[str, str]]:
    return [{"id": key, "name": name} for key, name in views.MODELS.items()]


@app.get("/api/gameweeks")
def gameweeks(db: Database) -> dict[str, Any]:
    """Every gameweek, and which one the site should open on."""
    return {
        "current": views.current_gameweek(db),
        "gameweeks": views.gameweeks(db),
    }


@app.get("/api/{model}/gameweek/{gameweek}")
def gameweek_view(
    model: str, gameweek: int, db: Database
) -> dict[str, Any]:
    """One model's squad, points and explanations for one gameweek."""
    view = views.gameweek_view(db, _require_model(model), gameweek)
    if view is None:
        raise HTTPException(
            status_code=404, detail=f"no picks stored for {model} in GW{gameweek}"
        )
    return view


@app.get("/api/{model}/season")
def season_view(
    model: str, db: Database
) -> dict[str, Any]:
    """Season totals, the beat-the-average tally and the cumulative series."""
    return views.season_view(db, _require_model(model))


@app.get("/api/{model}/gameweek/{gameweek}/player/{element}")
def player_detail(
    model: str, gameweek: int, element: int, db: Database
) -> dict[str, Any]:
    """The detail sheet behind a tap on a player."""
    detail = views.player_detail(db, _require_model(model), gameweek, element)
    if detail is None:
        raise HTTPException(
            status_code=404,
            detail=f"player {element} is not in the {model} squad for GW{gameweek}",
        )
    return detail


@app.get("/api/summary")
def summary(db: Database) -> dict[str, Any]:
    """Both models side by side, for the season header."""
    return {
        "current_gameweek": views.current_gameweek(db),
        "models": {
            model: views.season_view(db, model) for model in views.MODELS
        },
    }


def _mount_frontend() -> None:
    """Serve the built frontend, if it has been built.

    Registered last so every `/api` route is matched first. Unknown paths fall
    back to `index.html` rather than 404ing, because the frontend is a single
    page app and a deep link like `/gameweek/7` is its route, not a file.
    """
    if not (FRONTEND_DIST / "index.html").exists():
        return

    app.mount(
        "/assets",
        StaticFiles(directory=FRONTEND_DIST / "assets"),
        name="assets",
    )

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        # An unmatched /api path is a mistake, not a page. Falling through to
        # index.html would answer a typo'd endpoint with HTML and a 200, which
        # is a miserable thing to debug from the frontend.
        if path.startswith("api/") or path == "api":
            raise HTTPException(status_code=404, detail=f"no such endpoint: /{path}")

        candidate = FRONTEND_DIST / path
        if path and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(FRONTEND_DIST / "index.html")


_mount_frontend()


def main() -> None:
    """Run the API, and the frontend too if it has been built.

    Also reachable as `python -m fplai.api.app`.
    """
    import os

    import uvicorn

    settings.ensure_directories()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))


if __name__ == "__main__":
    main()
