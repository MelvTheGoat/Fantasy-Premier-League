"""Scoring locked picks against what actually happened.

Points come from the FPL API and are never recalculated. What happens here is
the manager-level resolution on top of them -- automatic substitutions, the
captain's multiplier, whether the bench counted, the cost of any hits -- and
then the comparison against the official gameweek average.

Scores are rewritten every time this runs, unlike picks, which are written
once. That is deliberate: a gameweek's score is provisional until lockdown and
moves as matches finish and bonus points settle.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from ..data.db import utcnow
from ..data.repository import (
    gameweek_average,
    gameweek_averages,
    last_kickoff,
    load_locked_lineup,
    load_manager_state,
    load_results,
    load_roster,
)
from ..rules.constants import Chip
from ..rules.scoring import GameweekScore, SeasonSummary, is_final, score_gameweek, summarise_season
from ..rules.squad import format_formation
from ..strategy import best_xi, manager

logger = logging.getLogger(__name__)

MODEL_IDS = (manager.MODEL_ID, best_xi.MODEL_ID)


def score_model_gameweek(
    connection: sqlite3.Connection,
    model_id: str,
    gameweek: int,
    *,
    now: datetime | None = None,
) -> GameweekScore | None:
    """Resolve one model's gameweek. Returns None if nothing was locked."""
    lineup = load_locked_lineup(connection, model_id, gameweek)
    if lineup is None:
        return None

    roster = load_roster(connection)
    results = load_results(connection, gameweek)

    chip = None
    transfer_cost = 0
    if model_id == manager.MODEL_ID:
        state = load_manager_state(connection, gameweek)
        if state:
            transfer_cost = state["transfer_cost"] or 0
            chip = Chip(state["chip"]) if state["chip"] else None

    moment = now or datetime.now(UTC)
    final = is_final(last_kickoff(connection, gameweek), moment)

    return score_gameweek(
        gameweek, lineup, roster, results,
        chip=chip, transfer_cost=transfer_cost, final=final,
    )


def store_gameweek_score(
    connection: sqlite3.Connection,
    model_id: str,
    score: GameweekScore,
) -> None:
    """Persist a resolved gameweek, including the automatic substitutions.

    The substitutions are stored so the frontend can mark who came on and who
    went off, which is otherwise impossible to reconstruct from the picks alone.
    """
    roster = load_roster(connection)
    average = gameweek_average(connection, score.gameweek)
    beat = None if average in (None, 0) else int(score.points > average)

    connection.execute(
        "INSERT INTO gameweek_results (model_id, gameweek, points_before_hits,"
        " transfer_cost, points, bench_points, captain_points, average_entry_score,"
        " beat_average, is_final, chip, formation, updated_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        " ON CONFLICT(model_id, gameweek) DO UPDATE SET"
        " points_before_hits=excluded.points_before_hits,"
        " transfer_cost=excluded.transfer_cost, points=excluded.points,"
        " bench_points=excluded.bench_points, captain_points=excluded.captain_points,"
        " average_entry_score=excluded.average_entry_score,"
        " beat_average=excluded.beat_average, is_final=excluded.is_final,"
        " chip=excluded.chip, formation=excluded.formation,"
        " updated_at=excluded.updated_at",
        (
            model_id, score.gameweek, score.points_before_hits, score.transfer_cost,
            score.points, score.bench_points, score.captain_points,
            average if average else None, beat, int(score.final),
            str(score.chip) if score.chip else None,
            format_formation(score.lineup.starters, roster),
            utcnow(),
        ),
    )

    connection.execute(
        "DELETE FROM auto_subs WHERE model_id = ? AND gameweek = ?",
        (model_id, score.gameweek),
    )
    if score.substitutions:
        connection.executemany(
            "INSERT INTO auto_subs (model_id, gameweek, out_player_id, in_player_id)"
            " VALUES (?, ?, ?, ?)",
            [
                (model_id, score.gameweek, s.out_element, s.in_element)
                for s in score.substitutions
            ],
        )


def score_gameweek_for_all_models(
    connection: sqlite3.Connection,
    gameweek: int,
    *,
    now: datetime | None = None,
) -> dict[str, GameweekScore]:
    """Score and store one gameweek for both models."""
    scores: dict[str, GameweekScore] = {}
    for model_id in MODEL_IDS:
        score = score_model_gameweek(connection, model_id, gameweek, now=now)
        if score is None:
            continue
        store_gameweek_score(connection, model_id, score)
        scores[model_id] = score
    return scores


def score_season(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
) -> dict[str, list[GameweekScore]]:
    """Score every gameweek that has locked picks."""
    gameweeks = [
        row["gameweek"]
        for row in connection.execute(
            "SELECT DISTINCT gameweek FROM locked_picks ORDER BY gameweek"
        )
    ]
    scores: dict[str, list[GameweekScore]] = {model: [] for model in MODEL_IDS}
    for gameweek in gameweeks:
        for model_id, score in score_gameweek_for_all_models(
            connection, gameweek, now=now
        ).items():
            scores[model_id].append(score)
    return scores


def season_summaries(
    connection: sqlite3.Connection,
) -> dict[str, SeasonSummary]:
    """Season totals per model, measured against the official averages."""
    averages = gameweek_averages(connection)
    summaries: dict[str, SeasonSummary] = {}

    for model_id in MODEL_IDS:
        scores = []
        for row in connection.execute(
            "SELECT * FROM gameweek_results WHERE model_id = ? ORDER BY gameweek",
            (model_id,),
        ):
            # A lightweight stand-in: `summarise_season` only reads points,
            # transfer cost and gameweek, so a full re-resolution is wasted work.
            scores.append(_StoredScore(row))
        summaries[model_id] = summarise_season(scores, averages)

    return summaries


class _StoredScore:
    """Adapts a stored result row to what `summarise_season` reads."""

    __slots__ = ("gameweek", "points", "transfer_cost")

    def __init__(self, row: sqlite3.Row) -> None:
        self.gameweek = row["gameweek"]
        self.points = row["points"]
        self.transfer_cost = row["transfer_cost"]


__all__ = [
    "MODEL_IDS",
    "score_gameweek_for_all_models",
    "score_model_gameweek",
    "score_season",
    "season_summaries",
    "store_gameweek_score",
]
