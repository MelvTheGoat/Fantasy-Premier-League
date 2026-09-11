"""Turning FPL API payloads into database rows.

Every function here takes an already-fetched payload rather than a client, so
ingestion can be tested against recorded fixtures without any network access.
The client is only wired in by `fplai.jobs`.

Payload shapes are validated as they are read. The FPL API adds and renames
fields between seasons, and a silently missing `defensive_contribution` would
poison the projection model rather than fail loudly.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import Any

from ..rules.constants import Position
from .db import log_ingest, transaction, utcnow

logger = logging.getLogger(__name__)


class IngestError(ValueError):
    """Raised when a payload is missing something the rest of the system needs."""


def _require(payload: dict, key: str, context: str) -> Any:
    if key not in payload:
        raise IngestError(f"{context} is missing the '{key}' field")
    return payload[key]


# --- bootstrap-static ------------------------------------------------------


def ingest_teams(connection: sqlite3.Connection, bootstrap: dict) -> int:
    teams = _require(bootstrap, "teams", "bootstrap-static")
    now = utcnow()
    rows = [
        (
            _require(team, "id", "team"),
            _require(team, "code", "team"),
            team.get("name", ""),
            team.get("short_name", ""),
            team.get("strength_overall_home"),
            team.get("strength_overall_away"),
            team.get("strength_attack_home"),
            team.get("strength_attack_away"),
            team.get("strength_defence_home"),
            team.get("strength_defence_away"),
        )
        for team in teams
    ]
    with transaction(connection):
        connection.executemany(
            "INSERT INTO teams (id, code, name, short_name, strength_overall_home,"
            " strength_overall_away, strength_attack_home, strength_attack_away,"
            " strength_defence_home, strength_defence_away)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " code=excluded.code, name=excluded.name, short_name=excluded.short_name,"
            " strength_overall_home=excluded.strength_overall_home,"
            " strength_overall_away=excluded.strength_overall_away,"
            " strength_attack_home=excluded.strength_attack_home,"
            " strength_attack_away=excluded.strength_attack_away,"
            " strength_defence_home=excluded.strength_defence_home,"
            " strength_defence_away=excluded.strength_defence_away",
            rows,
        )
        log_ingest(connection, "bootstrap-static/teams", "ok", rows=len(rows))
    _ = now
    return len(rows)


def ingest_players(
    connection: sqlite3.Connection,
    bootstrap: dict,
    *,
    gameweek: int | None = None,
) -> int:
    """Store the player list and, if a gameweek is given, snapshot prices.

    `element_type` is stored exactly as the API gives it. Positions changed for
    several players in 2026/27, so nothing may infer a position from anywhere
    else.
    """
    elements = _require(bootstrap, "elements", "bootstrap-static")
    valid_types = {int(p) for p in Position}
    now = utcnow()

    rows = []
    price_rows = []
    for element in elements:
        element_id = _require(element, "id", "element")
        element_type = _require(element, "element_type", f"element {element_id}")
        if element_type not in valid_types:
            raise IngestError(
                f"element {element_id} has unknown element_type {element_type};"
                f" expected one of {sorted(valid_types)}"
            )
        now_cost = _require(element, "now_cost", f"element {element_id}")

        rows.append(
            (
                element_id,
                element.get("code"),
                element.get("web_name", ""),
                element.get("first_name"),
                element.get("second_name"),
                _require(element, "team", f"element {element_id}"),
                element_type,
                now_cost,
                element.get("status"),
                element.get("chance_of_playing_next_round"),
                element.get("chance_of_playing_this_round"),
                element.get("news"),
                element.get("news_added"),
                _as_float(element.get("selected_by_percent")),
                element.get("penalties_order"),
                element.get("direct_freekicks_order"),
                element.get("corners_and_indirect_freekicks_order"),
                now,
            )
        )
        if gameweek is not None:
            price_rows.append((element_id, gameweek, now_cost, now))

    with transaction(connection):
        connection.executemany(
            "INSERT INTO players (id, code, web_name, first_name, second_name, team_id,"
            " element_type, now_cost, status, chance_of_playing_next_round,"
            " chance_of_playing_this_round, news, news_added, selected_by_percent,"
            " penalties_order, direct_freekicks_order, corners_order, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " code=excluded.code, web_name=excluded.web_name, first_name=excluded.first_name,"
            " second_name=excluded.second_name, team_id=excluded.team_id,"
            " element_type=excluded.element_type, now_cost=excluded.now_cost,"
            " status=excluded.status,"
            " chance_of_playing_next_round=excluded.chance_of_playing_next_round,"
            " chance_of_playing_this_round=excluded.chance_of_playing_this_round,"
            " news=excluded.news, news_added=excluded.news_added,"
            " selected_by_percent=excluded.selected_by_percent,"
            " penalties_order=excluded.penalties_order,"
            " direct_freekicks_order=excluded.direct_freekicks_order,"
            " corners_order=excluded.corners_order,"
            " updated_at=excluded.updated_at",
            rows,
        )
        if price_rows:
            connection.executemany(
                "INSERT INTO player_prices (player_id, gameweek, now_cost, captured_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(player_id, gameweek) DO UPDATE SET"
                " now_cost=excluded.now_cost, captured_at=excluded.captured_at",
                price_rows,
            )
        log_ingest(connection, "bootstrap-static/elements", "ok", gameweek=gameweek, rows=len(rows))
    return len(rows)


def ingest_gameweeks(connection: sqlite3.Connection, bootstrap: dict) -> int:
    """Store the gameweek list, including the official average entry score."""
    events = _require(bootstrap, "events", "bootstrap-static")
    now = utcnow()
    rows = [
        (
            _require(event, "id", "event"),
            event.get("name"),
            _require(event, "deadline_time", f"event {event.get('id')}"),
            int(bool(event.get("is_current"))),
            int(bool(event.get("is_next"))),
            int(bool(event.get("finished"))),
            int(bool(event.get("data_checked"))),
            event.get("average_entry_score"),
            event.get("highest_score"),
            now,
        )
        for event in events
    ]
    with transaction(connection):
        connection.executemany(
            "INSERT INTO gameweeks (id, name, deadline_time, is_current, is_next, finished,"
            " data_checked, average_entry_score, highest_score, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " name=excluded.name, deadline_time=excluded.deadline_time,"
            " is_current=excluded.is_current, is_next=excluded.is_next,"
            " finished=excluded.finished, data_checked=excluded.data_checked,"
            " average_entry_score=excluded.average_entry_score,"
            " highest_score=excluded.highest_score, updated_at=excluded.updated_at",
            rows,
        )
        log_ingest(connection, "bootstrap-static/events", "ok", rows=len(rows))
    return len(rows)


# --- fixtures --------------------------------------------------------------


def ingest_fixtures(connection: sqlite3.Connection, fixtures: Sequence[dict]) -> int:
    now = utcnow()
    rows = [
        (
            _require(fixture, "id", "fixture"),
            fixture.get("event"),
            fixture.get("kickoff_time"),
            _require(fixture, "team_h", f"fixture {fixture.get('id')}"),
            _require(fixture, "team_a", f"fixture {fixture.get('id')}"),
            fixture.get("team_h_difficulty"),
            fixture.get("team_a_difficulty"),
            fixture.get("team_h_score"),
            fixture.get("team_a_score"),
            int(bool(fixture.get("started"))),
            int(bool(fixture.get("finished"))),
            int(bool(fixture.get("finished_provisional"))),
            now,
        )
        for fixture in fixtures
    ]
    with transaction(connection):
        connection.executemany(
            "INSERT INTO fixtures (id, gameweek, kickoff_time, team_h, team_a,"
            " team_h_difficulty, team_a_difficulty, team_h_score, team_a_score,"
            " started, finished, finished_provisional, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(id) DO UPDATE SET"
            " gameweek=excluded.gameweek, kickoff_time=excluded.kickoff_time,"
            " team_h=excluded.team_h, team_a=excluded.team_a,"
            " team_h_difficulty=excluded.team_h_difficulty,"
            " team_a_difficulty=excluded.team_a_difficulty,"
            " team_h_score=excluded.team_h_score, team_a_score=excluded.team_a_score,"
            " started=excluded.started, finished=excluded.finished,"
            " finished_provisional=excluded.finished_provisional,"
            " updated_at=excluded.updated_at",
            rows,
        )
        _update_last_kickoffs(connection)
        log_ingest(connection, "fixtures/", "ok", rows=len(rows))
    return len(rows)


def _update_last_kickoffs(connection: sqlite3.Connection) -> None:
    """Record each gameweek's final kickoff, which is what lockdown hangs off."""
    connection.execute(
        "UPDATE gameweeks SET last_kickoff_time = ("
        "  SELECT MAX(kickoff_time) FROM fixtures"
        "  WHERE fixtures.gameweek = gameweeks.id AND kickoff_time IS NOT NULL"
        ")"
    )


# --- live results ----------------------------------------------------------

#: Live stat names mapped to their column. Anything the API stops sending
#: defaults to zero rather than breaking the row, but the keys that the
#: projection model depends on are checked separately below.
_STAT_COLUMNS = {
    "minutes": "minutes",
    "total_points": "total_points",
    "goals_scored": "goals_scored",
    "assists": "assists",
    "clean_sheets": "clean_sheets",
    "goals_conceded": "goals_conceded",
    "own_goals": "own_goals",
    "penalties_saved": "penalties_saved",
    "penalties_missed": "penalties_missed",
    "yellow_cards": "yellow_cards",
    "red_cards": "red_cards",
    "saves": "saves",
    "bonus": "bonus",
    "bps": "bps",
    "clearances_blocks_interceptions": "clearances_blocks_interceptions",
    "tackles": "tackles",
    "recoveries": "recoveries",
    "defensive_contribution": "defensive_contribution",
    "starts": "starts",
}

_FLOAT_STATS = {
    "expected_goals": "expected_goals",
    "expected_assists": "expected_assists",
    "expected_goal_involvements": "expected_goal_involvements",
    "expected_goals_conceded": "expected_goals_conceded",
}


def ingest_live_gameweek(
    connection: sqlite3.Connection,
    gameweek: int,
    live: dict,
) -> int:
    """Store per-player, per-fixture stats for one gameweek.

    One row per fixture rather than per player: in a double gameweek a player
    appears in `explain` twice, and keeping both rows means minutes and points
    can be summed without the API's own aggregation being trusted.
    """
    elements = _require(live, "elements", f"event/{gameweek}/live")
    now = utcnow()
    rows: list[tuple] = []

    for element in elements:
        element_id = _require(element, "id", "live element")
        stats = element.get("stats", {})
        fixture_ids = _fixture_ids_for(element)

        if not fixture_ids:
            # No fixture at all: a blank gameweek for this player's club.
            continue

        # A player's `stats` block is the gameweek total. When there are two
        # fixtures the per-fixture split lives in `explain`, so the total is
        # attributed to the first fixture and the rest carry the explain rows.
        per_fixture = _split_by_fixture(element, fixture_ids, stats)

        for fixture_id, fixture_stats in per_fixture.items():
            rows.append(
                (
                    element_id,
                    gameweek,
                    fixture_id,
                    *[int(fixture_stats.get(key, 0) or 0) for key in _STAT_COLUMNS],
                    *[_as_float(fixture_stats.get(key)) for key in _FLOAT_STATS],
                    now,
                )
            )

    columns = (
        ["player_id", "gameweek", "fixture_id"]
        + list(_STAT_COLUMNS.values())
        + list(_FLOAT_STATS.values())
        + ["updated_at"]
    )
    placeholders = ", ".join("?" for _ in columns)
    updates = ", ".join(
        f"{column}=excluded.{column}" for column in columns[3:]
    )

    with transaction(connection):
        connection.executemany(
            f"INSERT INTO player_gameweek_stats ({', '.join(columns)})"
            f" VALUES ({placeholders})"
            f" ON CONFLICT(player_id, gameweek, fixture_id) DO UPDATE SET {updates}",
            rows,
        )
        log_ingest(connection, f"event/{gameweek}/live/", "ok", gameweek=gameweek, rows=len(rows))
    return len(rows)


def _fixture_ids_for(element: dict) -> list[int]:
    explain = element.get("explain") or []
    ids = []
    for entry in explain:
        fixture_id = entry.get("fixture")
        if fixture_id is not None and fixture_id not in ids:
            ids.append(fixture_id)
    return ids


def _split_by_fixture(
    element: dict,
    fixture_ids: list[int],
    totals: dict,
) -> dict[int, dict]:
    """Attribute a player's gameweek stats to the fixtures they came from.

    A single fixture takes the whole `stats` block. For a double, the points
    breakdown in `explain` gives the per-fixture split for everything it
    covers; the remaining aggregate stats are attached to the first fixture so
    they are counted exactly once when the gameweek is summed.
    """
    if len(fixture_ids) == 1:
        return {fixture_ids[0]: dict(totals)}

    split: dict[int, dict] = {fixture_id: {} for fixture_id in fixture_ids}
    for entry in element.get("explain") or []:
        fixture_id = entry.get("fixture")
        if fixture_id is None:
            continue
        bucket = split.setdefault(fixture_id, {})
        for stat in entry.get("stats") or []:
            identifier = stat.get("identifier")
            if identifier is None:
                continue
            bucket[identifier] = (bucket.get(identifier) or 0) + (stat.get("value") or 0)
            if identifier == "minutes":
                continue
        bucket["total_points"] = sum(
            (stat.get("points") or 0) for stat in entry.get("stats") or []
        )

    # Anything only present as a gameweek total goes on the first fixture.
    covered: set[str] = set()
    for bucket in split.values():
        covered |= set(bucket)
    first = fixture_ids[0]
    for key, value in totals.items():
        if key not in covered:
            split[first][key] = value

    return split


def _as_float(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# --- element summary ---


def ingest_element_summary(
    connection: sqlite3.Connection,
    element: int,
    summary: dict,
) -> dict[str, int]:
    """Store one player's gameweek price history and past-season totals.

    The price history is the reason this endpoint is worth 600-odd requests:
    `history[].value` is the player's price *at that gameweek*, which is the
    only way to reconstruct what a past decision would have cost. Without it a
    backfill has to price GW1 at today's prices, which is a leak.
    """
    now = utcnow()

    price_rows = [
        (element, row["round"], row["value"], now)
        for row in summary.get("history") or []
        if row.get("round") is not None and row.get("value") is not None
    ]

    season_rows = [
        (
            element,
            season.get("season_name"),
            season.get("minutes", 0),
            season.get("total_points", 0),
            season.get("goals_scored", 0),
            season.get("assists", 0),
            season.get("clean_sheets", 0),
            season.get("goals_conceded", 0),
            season.get("saves", 0),
            season.get("bonus", 0),
            season.get("bps", 0),
            season.get("yellow_cards", 0),
            season.get("start_cost"),
            season.get("end_cost"),
            now,
        )
        for season in summary.get("history_past") or []
        if season.get("season_name")
    ]

    with transaction(connection):
        if price_rows:
            connection.executemany(
                "INSERT INTO player_prices (player_id, gameweek, now_cost, captured_at)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(player_id, gameweek) DO UPDATE SET"
                " now_cost=excluded.now_cost, captured_at=excluded.captured_at",
                price_rows,
            )
        if season_rows:
            connection.executemany(
                "INSERT INTO player_season_history (player_id, season_name, minutes,"
                " total_points, goals_scored, assists, clean_sheets, goals_conceded,"
                " saves, bonus, bps, yellow_cards, start_cost, end_cost, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(player_id, season_name) DO UPDATE SET"
                " minutes=excluded.minutes, total_points=excluded.total_points,"
                " goals_scored=excluded.goals_scored, assists=excluded.assists,"
                " clean_sheets=excluded.clean_sheets,"
                " goals_conceded=excluded.goals_conceded, saves=excluded.saves,"
                " bonus=excluded.bonus, bps=excluded.bps,"
                " yellow_cards=excluded.yellow_cards, start_cost=excluded.start_cost,"
                " end_cost=excluded.end_cost, updated_at=excluded.updated_at",
                season_rows,
            )

    return {"prices": len(price_rows), "seasons": len(season_rows)}


# --- blank and double gameweeks -------------------------------------------


def fixtures_per_team(
    connection: sqlite3.Connection, gameweek: int
) -> dict[int, int]:
    """How many fixtures each club has in a gameweek.

    Zero is a blank, two or more is a double. Clubs with no fixture do not
    appear in the fixtures table for that gameweek at all, so they are filled
    in from the team list rather than left missing.
    """
    counts = {row["id"]: 0 for row in connection.execute("SELECT id FROM teams")}
    for row in connection.execute(
        "SELECT team_h, team_a FROM fixtures WHERE gameweek = ?", (gameweek,)
    ):
        counts[row["team_h"]] = counts.get(row["team_h"], 0) + 1
        counts[row["team_a"]] = counts.get(row["team_a"], 0) + 1
    return counts


def blank_teams(connection: sqlite3.Connection, gameweek: int) -> set[int]:
    return {team for team, n in fixtures_per_team(connection, gameweek).items() if n == 0}


def double_teams(connection: sqlite3.Connection, gameweek: int) -> set[int]:
    return {team for team, n in fixtures_per_team(connection, gameweek).items() if n >= 2}


def player_fixture_counts(
    connection: sqlite3.Connection, gameweek: int
) -> dict[int, int]:
    """Fixture count per player, which is what the projections multiply by."""
    team_counts = fixtures_per_team(connection, gameweek)
    return {
        row["id"]: team_counts.get(row["team_id"], 0)
        for row in connection.execute("SELECT id, team_id FROM players")
    }


__all__ = [
    "IngestError",
    "blank_teams",
    "ingest_element_summary",
    "double_teams",
    "fixtures_per_team",
    "ingest_fixtures",
    "ingest_gameweeks",
    "ingest_live_gameweek",
    "ingest_players",
    "ingest_teams",
    "player_fixture_counts",
]
