"""Reading the database back out as the rules engine's own types.

This is the only place that knows both SQL and `fplai.rules`. Keeping it in one
module means the rules stay free of I/O and the ingestion stays free of rules.

The `as_of_gameweek` arguments exist for the backfill. A projection or a squad
decision for GW7 may only see data that existed before the GW7 deadline, so the
reads that feed the models take an explicit cut-off rather than trusting the
caller to have filtered.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from ..rules.constants import Chip, Position
from ..rules.pricing import selling_price
from ..rules.types import (
    ChipUsage,
    Lineup,
    Player,
    PlayerGameweekResult,
    Squad,
    SquadPick,
)
from .db import utcnow


class LockedPicksExist(RuntimeError):
    """Raised when something tries to rewrite picks that a deadline has locked."""


# --- reference data --------------------------------------------------------


def load_roster(connection: sqlite3.Connection) -> dict[int, Player]:
    """Every player, keyed by element id, at today's prices."""
    return {
        row["id"]: Player(
            element=row["id"],
            position=Position(row["element_type"]),
            team=row["team_id"],
            price=row["now_cost"],
            web_name=row["web_name"],
        )
        for row in connection.execute(
            "SELECT id, element_type, team_id, now_cost, web_name FROM players"
        )
    }


def load_roster_at_gameweek(
    connection: sqlite3.Connection, gameweek: int
) -> dict[int, Player]:
    """The roster as it stood at a gameweek's deadline.

    A player with no price in that gameweek's snapshot was not in the game
    then, and is left out rather than priced at today's cost. Both reasons
    matter: buying a January signing in gameweek one is not a legal squad, and
    pricing him from today is hindsight about a player who did not exist yet.

    When the gameweek has no snapshot at all -- an upcoming deadline whose
    refresh has not run -- there is nothing to be faithful to, so the current
    roster is returned whole.
    """
    prices = {
        row["player_id"]: row["now_cost"]
        for row in connection.execute(
            "SELECT player_id, now_cost FROM player_prices WHERE gameweek = ?", (gameweek,)
        )
    }
    roster = load_roster(connection)
    if not prices:
        return roster

    return {
        element: Player(
            element=player.element,
            position=player.position,
            team=player.team,
            price=prices[element],
            web_name=player.web_name,
        )
        for element, player in roster.items()
        if element in prices
    }


def current_gameweek(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT id FROM gameweeks WHERE is_current = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def next_gameweek(connection: sqlite3.Connection) -> int | None:
    row = connection.execute(
        "SELECT id FROM gameweeks WHERE is_next = 1 ORDER BY id LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


def finished_gameweeks(connection: sqlite3.Connection) -> list[int]:
    return [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM gameweeks WHERE finished = 1 ORDER BY id"
        )
    ]


def deadline(connection: sqlite3.Connection, gameweek: int) -> datetime | None:
    row = connection.execute(
        "SELECT deadline_time FROM gameweeks WHERE id = ?", (gameweek,)
    ).fetchone()
    if not row or not row["deadline_time"]:
        return None
    return _parse(row["deadline_time"])


def last_kickoff(connection: sqlite3.Connection, gameweek: int) -> datetime | None:
    row = connection.execute(
        "SELECT last_kickoff_time FROM gameweeks WHERE id = ?", (gameweek,)
    ).fetchone()
    if not row or not row["last_kickoff_time"]:
        return None
    return _parse(row["last_kickoff_time"])


def gameweeks_underway(
    connection: sqlite3.Connection, now: datetime
) -> list[int]:
    """Gameweeks whose deadline has passed and which have fixtures to play.

    Deadlines are parsed and compared as datetimes rather than as the text they
    are stored in, so a caller can ask the question against any moment -- which
    is what lets the scheduler's timing be tested without moving the clock.
    """
    candidates = [
        row["id"]
        for row in connection.execute(
            "SELECT id FROM gameweeks"
            " WHERE EXISTS (SELECT 1 FROM fixtures WHERE fixtures.gameweek = gameweeks.id)"
            " ORDER BY id"
        )
    ]
    return [
        gameweek
        for gameweek in candidates
        if (when := deadline(connection, gameweek)) is not None and when <= now
    ]


def reference_refreshed_at(connection: sqlite3.Connection) -> datetime | None:
    """When the reference data was last pulled from the API.

    The scheduler decides what is due by reading this database, so every rule
    it applies is only as true as the last refresh. Exposing the age of that
    refresh is what lets it notice that it is reasoning about a stale world
    and go and look again.
    """
    row = connection.execute(
        "SELECT MAX(updated_at) AS seen FROM gameweeks"
    ).fetchone()
    if not row or not row["seen"]:
        return None
    return _parse(row["seen"])


def gameweek_is_checked(connection: sqlite3.Connection, gameweek: int) -> bool:
    """Whether FPL has confirmed a gameweek's points are settled.

    `data_checked` is the API's own statement that it has finished with a
    gameweek, which makes it the one honest marker for "final". Points, bonus
    and the official average all stop moving at that moment and not before.
    """
    row = connection.execute(
        "SELECT data_checked FROM gameweeks WHERE id = ?", (gameweek,)
    ).fetchone()
    return bool(row and row["data_checked"])


def gameweek_average(connection: sqlite3.Connection, gameweek: int) -> int | None:
    """The official FPL average, straight from `average_entry_score`."""
    row = connection.execute(
        "SELECT average_entry_score FROM gameweeks WHERE id = ?", (gameweek,)
    ).fetchone()
    return row["average_entry_score"] if row else None


def gameweek_averages(connection: sqlite3.Connection) -> dict[int, float]:
    return {
        row["id"]: float(row["average_entry_score"])
        for row in connection.execute(
            "SELECT id, average_entry_score FROM gameweeks"
            " WHERE average_entry_score IS NOT NULL AND average_entry_score > 0"
        )
    }


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# --- results ---------------------------------------------------------------


def load_results(
    connection: sqlite3.Connection, gameweek: int
) -> dict[int, PlayerGameweekResult]:
    """Per-player gameweek results, with a double gameweek's fixtures summed.

    `fixtures_finished` is only true once every fixture the player's club had in
    the gameweek has finished, which is the condition the auto-substitution
    rules wait for.
    """
    rows = connection.execute(
        "SELECT s.player_id, SUM(s.minutes) AS minutes,"
        "       SUM(s.total_points) AS total_points,"
        "       COUNT(*) AS fixture_count,"
        "       MIN(COALESCE(f.finished, 0)) AS all_finished"
        " FROM player_gameweek_stats s"
        " LEFT JOIN fixtures f ON f.id = s.fixture_id"
        " WHERE s.gameweek = ?"
        " GROUP BY s.player_id",
        (gameweek,),
    )
    return {
        row["player_id"]: PlayerGameweekResult(
            element=row["player_id"],
            minutes=row["minutes"] or 0,
            total_points=row["total_points"] or 0,
            fixtures_finished=bool(row["all_finished"]),
            fixture_count=row["fixture_count"],
        )
        for row in rows
    }


# --- locked picks ----------------------------------------------------------


def picks_are_locked(connection: sqlite3.Connection, model_id: str, gameweek: int) -> bool:
    row = connection.execute(
        "SELECT COUNT(*) c FROM locked_picks WHERE model_id = ? AND gameweek = ?",
        (model_id, gameweek),
    ).fetchone()
    return row["c"] > 0


def save_locked_picks(
    connection: sqlite3.Connection,
    model_id: str,
    gameweek: int,
    lineup: Lineup,
    squad: Squad,
    prices: dict[int, int],
    reasons: dict[int, str] | None = None,
    now: datetime | None = None,
) -> None:
    """Write a gameweek's picks, refusing once its deadline has passed.

    The deadline is what locks a squad, not the act of writing it. Before the
    deadline a squad is provisional and may be rewritten as often as team news
    arrives -- that is what every human manager does, and it uses no
    information the deadline had not already made available. After it, the
    refusal is absolute: regenerating with hindsight would quietly invalidate
    every result that followed.

    A gameweek with no recorded deadline is treated as closed. Being unable to
    prove a write is legitimate is a reason to refuse it, not to allow it.
    """
    if picks_are_locked(connection, model_id, gameweek):
        moment = now or datetime.now(UTC)
        when = deadline(connection, gameweek)
        if when is None or moment >= when:
            raise LockedPicksExist(
                f"{model_id} picks for GW{gameweek} are already locked and cannot "
                "be regenerated"
            )
        connection.execute(
            "DELETE FROM locked_picks WHERE model_id = ? AND gameweek = ?",
            (model_id, gameweek),
        )

    reasons = reasons or {}
    now = utcnow()
    rows = []
    for slot, element in enumerate(lineup.elements, start=1):
        purchase = squad.purchase_price(element)
        rows.append(
            (
                model_id,
                gameweek,
                element,
                slot,
                int(element == lineup.captain),
                int(element == lineup.vice_captain),
                purchase,
                selling_price(purchase, prices.get(element, purchase)),
                reasons.get(element),
                now,
            )
        )

    connection.executemany(
        "INSERT INTO locked_picks (model_id, gameweek, player_id, squad_position,"
        " is_captain, is_vice_captain, purchase_price, selling_price, selection_reason,"
        " locked_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def load_locked_lineup(
    connection: sqlite3.Connection, model_id: str, gameweek: int
) -> Lineup | None:
    rows = connection.execute(
        "SELECT player_id, squad_position, is_captain, is_vice_captain"
        " FROM locked_picks WHERE model_id = ? AND gameweek = ?"
        " ORDER BY squad_position",
        (model_id, gameweek),
    ).fetchall()
    if not rows:
        return None

    elements = [row["player_id"] for row in rows]
    captain = next((r["player_id"] for r in rows if r["is_captain"]), elements[0])
    vice = next((r["player_id"] for r in rows if r["is_vice_captain"]), elements[1])
    return Lineup(
        starters=tuple(elements[:11]),
        bench=tuple(elements[11:]),
        captain=captain,
        vice_captain=vice,
    )


def load_locked_squad(
    connection: sqlite3.Connection, model_id: str, gameweek: int
) -> Squad | None:
    rows = connection.execute(
        "SELECT player_id, purchase_price FROM locked_picks"
        " WHERE model_id = ? AND gameweek = ? ORDER BY squad_position",
        (model_id, gameweek),
    ).fetchall()
    if not rows:
        return None
    state = connection.execute(
        "SELECT bank FROM manager_state WHERE gameweek = ?", (gameweek,)
    ).fetchone()
    return Squad(
        picks=tuple(SquadPick(r["player_id"], r["purchase_price"]) for r in rows),
        bank=state["bank"] if state else 0,
    )


def selection_reasons(
    connection: sqlite3.Connection, model_id: str, gameweek: int
) -> dict[int, str]:
    return {
        row["player_id"]: row["selection_reason"]
        for row in connection.execute(
            "SELECT player_id, selection_reason FROM locked_picks"
            " WHERE model_id = ? AND gameweek = ? AND selection_reason IS NOT NULL",
            (model_id, gameweek),
        )
    }


# --- manager state ---------------------------------------------------------


def load_chips_used(connection: sqlite3.Connection) -> list[ChipUsage]:
    return [
        ChipUsage(chip=Chip(row["chip"]), gameweek=row["gameweek"])
        for row in connection.execute(
            "SELECT chip, gameweek FROM chips_used ORDER BY gameweek"
        )
    ]


def save_chip_used(
    connection: sqlite3.Connection, chip: Chip, gameweek: int, chip_set: int, reason: str
) -> None:
    connection.execute(
        "INSERT OR REPLACE INTO chips_used (chip, gameweek, chip_set, reason, locked_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (str(chip), gameweek, chip_set, reason, utcnow()),
    )


def save_transfer(
    connection: sqlite3.Connection,
    gameweek: int,
    out_element: int,
    in_element: int,
    selling_price_value: int,
    purchase_price_value: int,
    comparison: dict,
    reason: str,
    was_hit: bool,
) -> None:
    """Store a transfer with the stat comparison the frontend shows beside it."""
    connection.execute(
        "INSERT OR REPLACE INTO transfers (gameweek, out_player_id, in_player_id,"
        " selling_price, purchase_price, comparison, reason, was_hit, locked_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            gameweek,
            out_element,
            in_element,
            selling_price_value,
            purchase_price_value,
            json.dumps(comparison),
            reason,
            int(was_hit),
            utcnow(),
        ),
    )


def load_transfers(connection: sqlite3.Connection, gameweek: int) -> list[dict]:
    return [
        {
            "out": row["out_player_id"],
            "in": row["in_player_id"],
            "selling_price": row["selling_price"],
            "purchase_price": row["purchase_price"],
            "comparison": json.loads(row["comparison"]),
            "reason": row["reason"],
            "was_hit": bool(row["was_hit"]),
        }
        for row in connection.execute(
            "SELECT * FROM transfers WHERE gameweek = ? ORDER BY id", (gameweek,)
        )
    ]


def load_manager_state(connection: sqlite3.Connection, gameweek: int) -> dict | None:
    row = connection.execute(
        "SELECT * FROM manager_state WHERE gameweek = ?", (gameweek,)
    ).fetchone()
    return dict(row) if row else None


def save_manager_state(connection: sqlite3.Connection, gameweek: int, **fields) -> None:
    columns = [
        "bank", "squad_value", "free_transfers", "free_transfers_after",
        "transfers_made", "transfer_cost", "chip", "chip_reason", "roll_reason",
    ]
    values = [fields.get(column) for column in columns]
    connection.execute(
        f"INSERT OR REPLACE INTO manager_state (gameweek, {', '.join(columns)}, locked_at)"
        f" VALUES (?, {', '.join('?' for _ in columns)}, ?)",
        (gameweek, *values, utcnow()),
    )


__all__ = [
    "LockedPicksExist",
    "current_gameweek",
    "deadline",
    "finished_gameweeks",
    "gameweek_average",
    "gameweek_averages",
    "gameweek_is_checked",
    "gameweeks_underway",
    "last_kickoff",
    "load_chips_used",
    "load_locked_lineup",
    "load_locked_squad",
    "load_manager_state",
    "load_results",
    "load_roster",
    "load_roster_at_gameweek",
    "load_transfers",
    "next_gameweek",
    "picks_are_locked",
    "reference_refreshed_at",
    "save_chip_used",
    "save_locked_picks",
    "save_manager_state",
    "save_transfer",
    "selection_reasons",
]
