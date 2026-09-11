"""Shaping stored results into what the frontend renders.

The frontend should not have to know the rules. Everything it needs to draw a
gameweek -- who started, who was subbed on, what the shirt URL is, whether the
score is final -- is assembled here, so the React side is presentation only.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from ..data.assets import shirt_url
from ..data.repository import (
    gameweek_average,
    gameweek_averages,
    load_locked_lineup,
    load_manager_state,
    load_results,
    load_roster,
    load_transfers,
    selection_reasons,
)
from ..rules.constants import BENCH_SIZE, STARTING_XI_SIZE, Chip
from ..rules.scoring import summarise_season
from ..strategy import best_xi, manager

MODELS = {
    manager.MODEL_ID: "The Manager",
    best_xi.MODEL_ID: "Best XI of the Week",
}

#: Human-readable chip names for the frontend's badges.
CHIP_LABELS = {
    Chip.WILDCARD: "Wildcard",
    Chip.FREE_HIT: "Free Hit",
    Chip.BENCH_BOOST: "Bench Boost",
    Chip.TRIPLE_CAPTAIN: "Triple Captain",
}


def _teams(connection: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {row["id"]: row for row in connection.execute("SELECT * FROM teams")}


def gameweeks(connection: sqlite3.Connection) -> list[dict]:
    """Every gameweek the site can show, newest state included."""
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "deadline": row["deadline_time"],
            "finished": bool(row["finished"]),
            "current": bool(row["is_current"]),
            "next": bool(row["is_next"]),
            "average": row["average_entry_score"] or None,
            "has_picks": bool(row["has_picks"]),
        }
        for row in connection.execute(
            "SELECT g.*,"
            " EXISTS (SELECT 1 FROM locked_picks p WHERE p.gameweek = g.id) AS has_picks"
            " FROM gameweeks g ORDER BY g.id"
        )
    ]


def current_gameweek(connection: sqlite3.Connection) -> int | None:
    """The gameweek to land on: the latest one that actually has picks.

    Not simply `is_current`, because before the season's first deadline, and in
    the gap between a deadline and the job that locks picks, there is nothing
    to show for it.
    """
    row = connection.execute(
        "SELECT MAX(gameweek) gw FROM locked_picks"
    ).fetchone()
    return row["gw"] if row and row["gw"] else None


def _auto_subs(
    connection: sqlite3.Connection, model_id: str, gameweek: int
) -> dict[str, set[int]]:
    rows = connection.execute(
        "SELECT out_player_id, in_player_id FROM auto_subs"
        " WHERE model_id = ? AND gameweek = ?",
        (model_id, gameweek),
    ).fetchall()
    return {
        "off": {row["out_player_id"] for row in rows},
        "on": {row["in_player_id"] for row in rows},
    }


def _player_view(
    element: int,
    slot: int,
    *,
    roster,
    teams,
    results,
    reasons: dict[int, str],
    subs: dict[str, set[int]],
    captain: int,
    vice_captain: int,
    chip: Chip | None,
    fixtures: dict[int, list[dict]],
) -> dict[str, Any]:
    player = roster[element]
    team = teams[player.team]
    result = results.get(element)

    started = slot <= STARTING_XI_SIZE
    subbed_on = element in subs["on"]
    subbed_off = element in subs["off"]

    # After substitutions, a starter who was subbed off no longer counts and a
    # bench player who came on does.
    counts = (started and not subbed_off) or subbed_on or chip is Chip.BENCH_BOOST

    multiplier = 0
    if counts:
        multiplier = 1
        if element == captain:
            multiplier = 3 if chip is Chip.TRIPLE_CAPTAIN else 2

    points = result.total_points if result else 0

    return {
        "element": element,
        "name": player.web_name,
        "position": player.position.name,
        "team": team["short_name"],
        "team_code": team["code"],
        "shirt": shirt_url(team["code"], player.position),
        "price": player.price,
        "slot": slot,
        "starting": started,
        "points": points,
        "multiplier": multiplier,
        "total": points * multiplier,
        "minutes": result.minutes if result else 0,
        "is_captain": element == captain,
        "is_vice_captain": element == vice_captain,
        "triple_captain": element == captain and chip is Chip.TRIPLE_CAPTAIN,
        "subbed_on": subbed_on,
        "subbed_off": subbed_off,
        "fixtures": fixtures.get(player.team, []),
        "reason": reasons.get(element),
    }


def _fixtures_for_gameweek(
    connection: sqlite3.Connection, gameweek: int, teams
) -> dict[int, list[dict]]:
    """Each club's fixtures that gameweek, for the label under a shirt."""
    out: dict[int, list[dict]] = {}
    for row in connection.execute(
        "SELECT team_h, team_a, team_h_difficulty, team_a_difficulty,"
        "       team_h_score, team_a_score, finished"
        " FROM fixtures WHERE gameweek = ? ORDER BY kickoff_time",
        (gameweek,),
    ):
        for team, opponent, difficulty, home in (
            (row["team_h"], row["team_a"], row["team_h_difficulty"], True),
            (row["team_a"], row["team_h"], row["team_a_difficulty"], False),
        ):
            out.setdefault(team, []).append(
                {
                    "opponent": teams[opponent]["short_name"],
                    "home": home,
                    "difficulty": difficulty,
                    "finished": bool(row["finished"]),
                }
            )
    return out


def gameweek_view(
    connection: sqlite3.Connection, model_id: str, gameweek: int
) -> dict[str, Any] | None:
    """Everything needed to draw one model's gameweek."""
    lineup = load_locked_lineup(connection, model_id, gameweek)
    if lineup is None:
        return None

    roster = load_roster(connection)
    teams = _teams(connection)
    results = load_results(connection, gameweek)
    reasons = selection_reasons(connection, model_id, gameweek)
    subs = _auto_subs(connection, model_id, gameweek)
    fixtures = _fixtures_for_gameweek(connection, gameweek, teams)

    result_row = connection.execute(
        "SELECT * FROM gameweek_results WHERE model_id = ? AND gameweek = ?",
        (model_id, gameweek),
    ).fetchone()

    chip = None
    state = None
    if model_id == manager.MODEL_ID:
        state = load_manager_state(connection, gameweek)
        if state and state["chip"]:
            chip = Chip(state["chip"])

    players = [
        _player_view(
            element, slot,
            roster=roster, teams=teams, results=results, reasons=reasons,
            subs=subs, captain=lineup.captain, vice_captain=lineup.vice_captain,
            chip=chip, fixtures=fixtures,
        )
        for slot, element in enumerate(lineup.elements, start=1)
    ]

    average = gameweek_average(connection, gameweek)
    points = result_row["points"] if result_row else None

    view: dict[str, Any] = {
        "model": model_id,
        "model_name": MODELS.get(model_id, model_id),
        "gameweek": gameweek,
        "starters": players[:STARTING_XI_SIZE],
        "bench": players[STARTING_XI_SIZE : STARTING_XI_SIZE + BENCH_SIZE],
        "formation": result_row["formation"] if result_row else None,
        "points": points,
        "points_before_hits": result_row["points_before_hits"] if result_row else None,
        "transfer_cost": result_row["transfer_cost"] if result_row else 0,
        "bench_points": result_row["bench_points"] if result_row else None,
        "captain_points": result_row["captain_points"] if result_row else None,
        "average": average or None,
        "beat_average": (
            None
            if result_row is None or result_row["beat_average"] is None
            else bool(result_row["beat_average"])
        ),
        "margin": (
            None if points is None or not average else points - average
        ),
        "final": bool(result_row["is_final"]) if result_row else False,
        "status": "Final" if result_row and result_row["is_final"] else "Provisional",
        "chip": str(chip) if chip else None,
        "chip_label": CHIP_LABELS.get(chip) if chip else None,
        "auto_subs": [
            {"off": off, "on": on}
            for off, on in zip(sorted(subs["off"]), sorted(subs["on"]), strict=False)
        ],
    }

    if model_id == manager.MODEL_ID:
        view["transfers"] = load_transfers(connection, gameweek)
        view["chip_reason"] = state["chip_reason"] if state else None
        view["roll_reason"] = state["roll_reason"] if state else None
        view["bank"] = state["bank"] if state else None
        view["squad_value"] = state["squad_value"] if state else None
        view["free_transfers"] = state["free_transfers"] if state else None

    return view


def season_view(connection: sqlite3.Connection, model_id: str) -> dict[str, Any]:
    """Season totals and the cumulative series the chart draws."""
    averages = gameweek_averages(connection)
    rows = connection.execute(
        "SELECT * FROM gameweek_results WHERE model_id = ? ORDER BY gameweek",
        (model_id,),
    ).fetchall()

    class _Score:
        __slots__ = ("gameweek", "points", "transfer_cost")

        def __init__(self, row):
            self.gameweek = row["gameweek"]
            self.points = row["points"]
            self.transfer_cost = row["transfer_cost"]

    summary = summarise_season([_Score(row) for row in rows], averages)

    chips = [
        {
            "chip": row["chip"],
            "label": CHIP_LABELS.get(Chip(row["chip"]), row["chip"]),
            "gameweek": row["gameweek"],
            "reason": row["reason"],
        }
        for row in connection.execute(
            "SELECT * FROM chips_used ORDER BY gameweek"
        )
    ] if model_id == manager.MODEL_ID else []

    return {
        "model": model_id,
        "model_name": MODELS.get(model_id, model_id),
        "total_points": summary.total_points,
        "gameweeks_played": summary.gameweeks_played,
        "gameweeks_beating_average": summary.gameweeks_beating_average,
        "beat_average_rate": round(summary.beat_average_rate, 3),
        "total_transfer_cost": summary.total_transfer_cost,
        "chips_used": chips,
        "history": [
            {
                "gameweek": row["gameweek"],
                "points": row["points"],
                "transfer_cost": row["transfer_cost"],
                "average": row["average_entry_score"],
                "beat_average": (
                    None if row["beat_average"] is None else bool(row["beat_average"])
                ),
                "chip": row["chip"],
                "final": bool(row["is_final"]),
            }
            for row in rows
        ],
        "cumulative": [
            {
                "gameweek": row["gameweek"],
                "points": points,
                "average": average,
            }
            for row, points, average in zip(
                rows, summary.cumulative_points, summary.cumulative_average,
                strict=False,
            )
        ],
    }


def player_detail(
    connection: sqlite3.Connection, model_id: str, gameweek: int, element: int
) -> dict[str, Any] | None:
    """The detail sheet shown when a player is tapped."""
    view = gameweek_view(connection, model_id, gameweek)
    if view is None:
        return None

    player = next(
        (p for p in view["starters"] + view["bench"] if p["element"] == element),
        None,
    )
    if player is None:
        return None

    projection = connection.execute(
        "SELECT * FROM projections"
        " WHERE player_id = ? AND made_for_gameweek = ? AND target_gameweek = ?",
        (element, gameweek, gameweek),
    ).fetchone()

    detail = dict(player)
    if projection:
        detail["projection"] = {
            "expected_points": round(projection["expected_points"], 2),
            "expected_minutes": round(projection["expected_minutes"] or 0, 1),
            "probability_of_start": round(projection["probability_of_start"] or 0, 3),
            "expected_goals": round(projection["expected_goals"] or 0, 2),
            "expected_assists": round(projection["expected_assists"] or 0, 2),
            "clean_sheet_probability": round(
                projection["clean_sheet_probability"] or 0, 3
            ),
            "defcon_probability": round(projection["defcon_probability"] or 0, 3),
            "expected_bonus": round(projection["expected_bonus"] or 0, 2),
            "fixture_count": projection["fixture_count"],
        }
    return detail


__all__ = [
    "CHIP_LABELS",
    "MODELS",
    "current_gameweek",
    "gameweek_view",
    "gameweeks",
    "player_detail",
    "season_view",
]
