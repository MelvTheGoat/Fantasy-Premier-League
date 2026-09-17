"""Assembling projections for every player, for a horizon of gameweeks.

The one rule this module exists to enforce: **a projection made for gameweek N
may only see data from before gameweek N's deadline.** Every read here takes
`before_gameweek` and filters on it, rather than trusting callers to have
filtered first. A backfill that leaked would produce a season of picks that
look brilliant and mean nothing.

Projections are written to `projections` keyed by
`(player, made_for_gameweek, target_gameweek)`, so the record of what was known
when survives, and a past gameweek's projection can be re-read exactly as it
was rather than recomputed with hindsight.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from ..config import settings
from ..data.db import transaction, utcnow
from ..rules.constants import Position
from ..rules.scoring_table import DEFAULT_SCORING, ScoringTable
from .player_rates import RECENT_MATCHES, PlayerHistory, estimate_rates
from .team_strength import expectation_for_fixture, load_team_form
from .xpts import GameweekProjection, project_gameweek

logger = logging.getLogger(__name__)

#: Bumped whenever the model changes in a way that makes old projections
#: incomparable. Stored on every row so a mixed database stays interpretable.
MODEL_VERSION = "rates-v1"


@dataclass(frozen=True, slots=True)
class FixtureSlot:
    """One fixture a club has in a gameweek."""

    fixture_id: int
    team: int
    opponent: int
    is_home: bool
    difficulty: int


def fixtures_by_team(
    connection: sqlite3.Connection, gameweek: int
) -> dict[int, list[FixtureSlot]]:
    """Every club's fixtures in a gameweek, keyed by club.

    A club with no entry has a blank; a club with two has a double. Both fall
    out of the length of the list rather than needing a special case.
    """
    slots: dict[int, list[FixtureSlot]] = {}
    for row in connection.execute(
        "SELECT id, team_h, team_a, team_h_difficulty, team_a_difficulty"
        " FROM fixtures WHERE gameweek = ? ORDER BY kickoff_time, id",
        (gameweek,),
    ):
        slots.setdefault(row["team_h"], []).append(
            FixtureSlot(row["id"], row["team_h"], row["team_a"], True, row["team_h_difficulty"])
        )
        slots.setdefault(row["team_a"], []).append(
            FixtureSlot(row["id"], row["team_a"], row["team_h"], False, row["team_a_difficulty"])
        )
    return slots


def build_histories(
    connection: sqlite3.Connection,
    before_gameweek: int,
    *,
    team_news: bool = True,
) -> dict[int, PlayerHistory]:
    """Each player's record from gameweeks strictly before `before_gameweek`.

    Prices come from the snapshot for `before_gameweek` where one exists, so a
    replayed gameweek is priced as it was, not as it is now.

    `team_news` controls the one thing the API only ever publishes in the
    present tense: whether a player is injured, doubtful or suspended. Those
    fields have no history, so replaying gameweek one with them switched on
    means avoiding players who got injured in September -- hindsight, plainly.
    A replay therefore sets this False and takes everyone as fit, which is
    less information than the original run had rather than more. A pick made
    before a deadline that has not arrived sets it True, because then the
    news is genuinely current.
    """
    prices = {
        row["player_id"]: row["now_cost"]
        for row in connection.execute(
            "SELECT player_id, now_cost FROM player_prices WHERE gameweek = ?",
            (before_gameweek,),
        )
    }

    #: Matches each club has actually played, so a player's starts can be read
    #: as a share of the matches they could have started.
    club_matches: dict[int, int] = {}
    for row in connection.execute(
        "SELECT team_h, team_a FROM fixtures"
        " WHERE gameweek IS NOT NULL AND gameweek < ? AND finished = 1",
        (before_gameweek,),
    ):
        for team in (row["team_h"], row["team_a"]):
            club_matches[team] = club_matches.get(team, 0) + 1

    recent_floor = max(before_gameweek - RECENT_MATCHES, 0)

    totals = {
        row["player_id"]: row
        for row in connection.execute(
            "SELECT player_id,"
            "       SUM(minutes) minutes, SUM(starts) starts,"
            "       SUM(CASE WHEN minutes > 0 THEN 1 ELSE 0 END) appearances,"
            "       SUM(goals_scored) goals, SUM(assists) assists,"
            "       SUM(expected_goals) xg, SUM(expected_assists) xa,"
            "       SUM(saves) saves, SUM(bps) bps,"
            "       SUM(yellow_cards) yellows,"
            "       SUM(CASE WHEN defensive_contribution > 0 THEN 1 ELSE 0 END) defcons"
            " FROM player_gameweek_stats WHERE gameweek < ? GROUP BY player_id",
            (before_gameweek,),
        )
    }

    recent = {
        row["player_id"]: row
        for row in connection.execute(
            "SELECT player_id,"
            "       SUM(starts) starts,"
            "       SUM(CASE WHEN minutes > 0 THEN 1 ELSE 0 END) appearances,"
            "       COUNT(*) matches"
            " FROM player_gameweek_stats"
            " WHERE gameweek < ? AND gameweek >= ? GROUP BY player_id",
            (before_gameweek, recent_floor),
        )
    }

    past = {
        row["player_id"]: row
        for row in connection.execute(
            "SELECT player_id, COUNT(*) seasons, SUM(minutes) minutes,"
            "       SUM(goals_scored) goals, SUM(assists) assists,"
            "       SUM(saves) saves, SUM(bps) bps"
            " FROM player_season_history GROUP BY player_id"
        )
    }

    histories: dict[int, PlayerHistory] = {}
    for row in connection.execute(
        "SELECT id, element_type, team_id, now_cost, status,"
        "       chance_of_playing_next_round FROM players"
    ):
        element = row["id"]
        total = totals.get(element)
        recent_row = recent.get(element)
        past_row = past.get(element)

        histories[element] = PlayerHistory(
            element=element,
            position=Position(row["element_type"]),
            minutes=int(total["minutes"] or 0) if total else 0,
            starts=int(total["starts"] or 0) if total else 0,
            appearances=int(total["appearances"] or 0) if total else 0,
            matches_available=club_matches.get(row["team_id"], 0),
            recent_starts=int(recent_row["starts"] or 0) if recent_row else 0,
            recent_appearances=int(recent_row["appearances"] or 0) if recent_row else 0,
            recent_matches=min(club_matches.get(row["team_id"], 0), RECENT_MATCHES),
            goals=float(total["goals"] or 0) if total else 0.0,
            assists=float(total["assists"] or 0) if total else 0.0,
            expected_goals=float(total["xg"] or 0) if total else 0.0,
            expected_assists=float(total["xa"] or 0) if total else 0.0,
            saves=int(total["saves"] or 0) if total else 0,
            defcon_hits=int(total["defcons"] or 0) if total else 0,
            bps=int(total["bps"] or 0) if total else 0,
            yellow_cards=int(total["yellows"] or 0) if total else 0,
            prior_seasons=int(past_row["seasons"] or 0) if past_row else 0,
            prior_minutes=int(past_row["minutes"] or 0) if past_row else 0,
            prior_goals=float(past_row["goals"] or 0) if past_row else 0.0,
            prior_assists=float(past_row["assists"] or 0) if past_row else 0.0,
            prior_saves=int(past_row["saves"] or 0) if past_row else 0,
            prior_bps=int(past_row["bps"] or 0) if past_row else 0,
            price=prices.get(element, row["now_cost"]),
            chance_of_playing=(
                row["chance_of_playing_next_round"] if team_news else None
            ),
            status=(row["status"] or "a") if team_news else "a",
        )

    return histories


def penalty_takers(connection: sqlite3.Connection) -> set[int]:
    """Players on first-choice penalties, who get an extra goal term."""
    return {
        row["id"]
        for row in connection.execute(
            "SELECT id FROM players WHERE penalties_order = 1"
        )
    }


def project_for_gameweek(
    connection: sqlite3.Connection,
    made_for_gameweek: int,
    *,
    horizon: int | None = None,
    scoring: ScoringTable = DEFAULT_SCORING,
    team_news: bool = True,
) -> dict[int, list[GameweekProjection]]:
    """Project every player over the horizon starting at `made_for_gameweek`.

    Only data from before `made_for_gameweek` is read. The returned lists are
    ordered by target gameweek, so the first entry is the gameweek being picked
    for and the rest are the planning horizon behind the Manager's decisions.

    `team_news` is passed through to `build_histories`, and additionally
    decides whether penalty duty is known: who takes them is published only
    for today, so a replay must not assume the current taker held the job
    back in August.
    """
    horizon = horizon or settings.planning_horizon

    histories = build_histories(connection, made_for_gameweek, team_news=team_news)
    rates = {element: estimate_rates(h) for element, h in histories.items()}
    form = load_team_form(connection, made_for_gameweek)
    takers = penalty_takers(connection) if team_news else set()

    teams = {
        row["id"]: row["team_id"]
        for row in connection.execute("SELECT id, team_id FROM players")
    }

    projections: dict[int, list[GameweekProjection]] = {e: [] for e in rates}

    for offset in range(horizon):
        target = made_for_gameweek + offset
        slots = fixtures_by_team(connection, target)

        for element, player_rates in rates.items():
            team = teams[element]
            expectations = [
                expectation_for_fixture(
                    slot.team,
                    slot.opponent,
                    is_home=slot.is_home,
                    difficulty=slot.difficulty,
                    team_form=form.get(slot.team),
                    opponent_form=form.get(slot.opponent),
                )
                for slot in slots.get(team, [])
            ]
            projections[element].append(
                project_gameweek(
                    player_rates,
                    expectations,
                    target,
                    scoring=scoring,
                    on_penalties=element in takers,
                )
            )

    return projections


def save_projections(
    connection: sqlite3.Connection,
    made_for_gameweek: int,
    projections: dict[int, list[GameweekProjection]],
    *,
    model_version: str = MODEL_VERSION,
) -> int:
    """Persist a projection run. Replaces any previous run for the same deadline."""
    now = utcnow()
    rows = []
    for element, gameweeks in projections.items():
        for projection in gameweeks:
            rows.append(
                (
                    element,
                    made_for_gameweek,
                    projection.gameweek,
                    projection.expected_points,
                    projection.expected_minutes,
                    projection.probability_of_starting,
                    sum(f.goals for f in projection.fixtures),
                    sum(f.assists for f in projection.fixtures),
                    projection.clean_sheet_probability,
                    projection.defcon_probability,
                    sum(f.bonus for f in projection.fixtures),
                    projection.fixture_count,
                    model_version,
                    now,
                )
            )

    with transaction(connection):
        connection.executemany(
            "INSERT INTO projections (player_id, made_for_gameweek, target_gameweek,"
            " expected_points, expected_minutes, probability_of_start, expected_goals,"
            " expected_assists, clean_sheet_probability, defcon_probability,"
            " expected_bonus, fixture_count, model_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT(player_id, made_for_gameweek, target_gameweek) DO UPDATE SET"
            " expected_points=excluded.expected_points,"
            " expected_minutes=excluded.expected_minutes,"
            " probability_of_start=excluded.probability_of_start,"
            " expected_goals=excluded.expected_goals,"
            " expected_assists=excluded.expected_assists,"
            " clean_sheet_probability=excluded.clean_sheet_probability,"
            " defcon_probability=excluded.defcon_probability,"
            " expected_bonus=excluded.expected_bonus,"
            " fixture_count=excluded.fixture_count,"
            " model_version=excluded.model_version,"
            " created_at=excluded.created_at",
            rows,
        )
    logger.info("wrote %d projection rows for GW%d", len(rows), made_for_gameweek)
    return len(rows)


def load_projections(
    connection: sqlite3.Connection,
    made_for_gameweek: int,
) -> dict[int, dict[int, float]]:
    """Stored expected points as `{element: {target_gameweek: xPts}}`."""
    result: dict[int, dict[int, float]] = {}
    for row in connection.execute(
        "SELECT player_id, target_gameweek, expected_points FROM projections"
        " WHERE made_for_gameweek = ?",
        (made_for_gameweek,),
    ):
        result.setdefault(row["player_id"], {})[row["target_gameweek"]] = row[
            "expected_points"
        ]
    return result


__all__ = [
    "MODEL_VERSION",
    "FixtureSlot",
    "build_histories",
    "fixtures_by_team",
    "load_projections",
    "penalty_takers",
    "project_for_gameweek",
    "save_projections",
]
