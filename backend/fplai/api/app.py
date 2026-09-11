"""The HTTP API the frontend reads.

Read-only and small on purpose: the models run as scheduled jobs, and this
serves what they already decided. Nothing here computes a projection or picks a
squad, so a slow request can never delay a deadline.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from ..config import settings
from ..data.db import connect, init_db
from . import views

app = FastAPI(
    title="FPL AI Manager",
    description="Two models playing Fantasy Premier League, scored on real points.",
    version="0.1.0",
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


def main() -> None:
    """Run the API. Also reachable as `python -m fplai.api.app`."""
    import uvicorn

    settings.ensure_directories()
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
