"""Turning a projection into something a person can read.

Both models have to justify themselves: Best XI explains why each player is in
the side, and the Manager explains every transfer, hit and chip. The numbers
behind those explanations are the same numbers the optimiser used, pulled from
the projection rather than recomputed, so an explanation can never disagree
with the decision it is explaining.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..model.xpts import GameweekProjection
from ..rules.constants import Position

#: FPL's difficulty ratings, as words. The API gives 1 (easiest) to 5.
DIFFICULTY_WORDS = {1: "very easy", 2: "easy", 3: "even", 4: "hard", 5: "very hard"}


@dataclass(frozen=True, slots=True)
class PlayerContext:
    """Everything an explanation needs about one player, beyond the projection."""

    element: int
    web_name: str
    position: Position
    team: int
    team_short_name: str
    team_code: int
    price: int
    status: str = "a"
    news: str = ""
    chance_of_playing: int | None = None
    penalties_order: int | None = None
    direct_freekicks_order: int | None = None
    corners_order: int | None = None

    #: Season-to-date figures, as of the deadline being explained.
    minutes: int = 0
    starts: int = 0
    goals: int = 0
    assists: int = 0
    expected_goals: float = 0.0
    expected_assists: float = 0.0
    defcon_hits: int = 0
    points: int = 0

    @property
    def on_penalties(self) -> bool:
        return self.penalties_order == 1

    @property
    def roles(self) -> list[str]:
        """Set-piece duties, in the order a person would mention them."""
        roles = []
        if self.penalties_order == 1:
            roles.append("penalties")
        elif self.penalties_order == 2:
            roles.append("backup penalties")
        if self.direct_freekicks_order == 1:
            roles.append("free kicks")
        if self.corners_order == 1:
            roles.append("corners")
        return roles

    @property
    def availability_note(self) -> str | None:
        """A short phrase describing an injury or suspension, if there is one."""
        if self.status == "s":
            return "suspended"
        if self.status == "u":
            return "unavailable"
        if self.status == "n":
            return "not in the squad"
        if self.status == "i":
            return self.news or "injured"
        if self.chance_of_playing is not None and self.chance_of_playing < 100:
            return f"{self.chance_of_playing}% chance of playing"
        return None


def load_contexts(
    connection: sqlite3.Connection, before_gameweek: int
) -> dict[int, PlayerContext]:
    """Player context as of a deadline, with season figures up to that point.

    Like everything the models read, the stats are filtered to gameweeks before
    the deadline, so an explanation written for GW7 cannot quote GW7's numbers.
    """
    totals = {
        row["player_id"]: row
        for row in connection.execute(
            "SELECT player_id, SUM(minutes) minutes, SUM(starts) starts,"
            "       SUM(goals_scored) goals, SUM(assists) assists,"
            "       SUM(expected_goals) xg, SUM(expected_assists) xa,"
            "       SUM(total_points) points,"
            "       SUM(CASE WHEN defensive_contribution > 0 THEN 1 ELSE 0 END) defcons"
            " FROM player_gameweek_stats WHERE gameweek < ? GROUP BY player_id",
            (before_gameweek,),
        )
    }
    prices = {
        row["player_id"]: row["now_cost"]
        for row in connection.execute(
            "SELECT player_id, now_cost FROM player_prices WHERE gameweek = ?",
            (before_gameweek,),
        )
    }

    contexts: dict[int, PlayerContext] = {}
    for row in connection.execute(
        "SELECT p.*, t.short_name AS team_short_name, t.code AS team_code"
        " FROM players p JOIN teams t ON t.id = p.team_id"
    ):
        total = totals.get(row["id"])
        contexts[row["id"]] = PlayerContext(
            element=row["id"],
            web_name=row["web_name"],
            position=Position(row["element_type"]),
            team=row["team_id"],
            team_short_name=row["team_short_name"],
            team_code=row["team_code"],
            price=prices.get(row["id"], row["now_cost"]),
            status=row["status"] or "a",
            news=row["news"] or "",
            chance_of_playing=row["chance_of_playing_next_round"],
            penalties_order=row["penalties_order"],
            direct_freekicks_order=row["direct_freekicks_order"],
            corners_order=row["corners_order"],
            minutes=int(total["minutes"] or 0) if total else 0,
            starts=int(total["starts"] or 0) if total else 0,
            goals=int(total["goals"] or 0) if total else 0,
            assists=int(total["assists"] or 0) if total else 0,
            expected_goals=float(total["xg"] or 0) if total else 0.0,
            expected_assists=float(total["xa"] or 0) if total else 0.0,
            defcon_hits=int(total["defcons"] or 0) if total else 0,
            points=int(total["points"] or 0) if total else 0,
        )
    return contexts


def describe_fixtures(
    projection: GameweekProjection,
    team_names: dict[int, str],
) -> str:
    """The gameweek's fixtures, as `CHE (H)` or `ARS (A), LIV (H)`."""
    if projection.is_blank:
        return "no fixture"
    return ", ".join(
        f"{team_names.get(f.opponent, '?')} ({'H' if f.is_home else 'A'})"
        for f in projection.fixtures
    )


def describe_upcoming(
    projections: list[GameweekProjection],
    team_names: dict[int, str],
    limit: int = 4,
) -> list[dict]:
    """The next few gameweeks as structured rows, for the frontend's fixture strip."""
    rows = []
    for projection in projections[:limit]:
        rows.append(
            {
                "gameweek": projection.gameweek,
                "fixtures": [
                    {
                        "opponent": team_names.get(f.opponent, "?"),
                        "home": f.is_home,
                        "difficulty": f.difficulty,
                    }
                    for f in projection.fixtures
                ],
                "expected_points": round(projection.expected_points, 2),
                "blank": projection.is_blank,
                "double": projection.is_double,
            }
        )
    return rows


def selection_reason(
    context: PlayerContext,
    projection: GameweekProjection,
    team_names: dict[int, str],
    *,
    is_captain: bool = False,
) -> str:
    """One sentence saying why this player is in the side.

    Leads with the projection, then names whichever two or three facts actually
    drove it -- a double gameweek, penalty duty, a clean-sheet-friendly fixture,
    a hot underlying run -- rather than reciting every statistic available.
    """
    parts: list[str] = []

    if projection.is_blank:
        return f"{context.web_name} has no fixture this gameweek."

    fixture_text = describe_fixtures(projection, team_names)
    if projection.is_double:
        parts.append(f"Double gameweek: {fixture_text}")
    else:
        difficulty = projection.fixtures[0].difficulty
        parts.append(f"{fixture_text}, {DIFFICULTY_WORDS.get(difficulty, 'even')}")

    components = projection.components()

    if context.on_penalties:
        parts.append("on penalties")
    elif context.roles:
        parts.append("on " + " and ".join(context.roles))

    per_90 = (
        (context.expected_goals + context.expected_assists) / context.minutes * 90
        if context.minutes
        else 0.0
    )
    if per_90 >= 0.45:
        parts.append(f"{per_90:.2f} xGI per 90")

    # A clean sheet is most of a defender's or keeper's upside, so it is
    # always worth stating for them. For a midfielder it is worth one point and
    # only worth mentioning when it is genuinely likely.
    clean_sheet = projection.clean_sheet_probability
    if context.position in (Position.GKP, Position.DEF):
        parts.append(f"{clean_sheet:.0%} clean sheet chance")
    elif context.position is Position.MID and clean_sheet >= 0.40:
        parts.append(f"{clean_sheet:.0%} clean sheet chance")
    if components.get("defensive contribution", 0) >= 0.8:
        parts.append(f"{projection.defcon_probability:.0%} defensive contribution chance")

    if projection.probability_of_starting < 0.7:
        parts.append(f"{projection.probability_of_starting:.0%} chance of starting")

    note = context.availability_note
    if note:
        parts.append(note)

    lead = f"{projection.expected_points:.1f} projected"
    if is_captain:
        lead += " and captained"
    return f"{lead}. " + ". ".join(parts) + "."


@dataclass(frozen=True, slots=True)
class PlayerComparison:
    """One side of a transfer comparison, as the frontend renders it."""

    element: int
    web_name: str
    position: str
    team: str
    team_code: int
    price: int
    projected_points: float
    """Over the planning horizon, not just the next gameweek."""
    next_gameweek_points: float
    fixtures: list[dict] = field(default_factory=list)
    form_points: int = 0
    minutes: int = 0
    starts: int = 0
    goals: int = 0
    assists: int = 0
    expected_goals: float = 0.0
    expected_assists: float = 0.0
    defcon_rate: float = 0.0
    availability: str | None = None

    def as_dict(self) -> dict:
        return {
            "element": self.element,
            "name": self.web_name,
            "position": self.position,
            "team": self.team,
            "team_code": self.team_code,
            "price": self.price,
            "projected_points": round(self.projected_points, 2),
            "next_gameweek_points": round(self.next_gameweek_points, 2),
            "fixtures": self.fixtures,
            "season_points": self.form_points,
            "minutes": self.minutes,
            "starts": self.starts,
            "goals": self.goals,
            "assists": self.assists,
            "expected_goals": round(self.expected_goals, 2),
            "expected_assists": round(self.expected_assists, 2),
            "defcon_rate": round(self.defcon_rate, 2),
            "availability": self.availability,
        }


def build_comparison(
    context: PlayerContext,
    projections: list[GameweekProjection],
    team_names: dict[int, str],
) -> PlayerComparison:
    """Assemble one player's side of a transfer comparison."""
    matches = max(context.starts, 1)
    return PlayerComparison(
        element=context.element,
        web_name=context.web_name,
        position=context.position.name,
        team=context.team_short_name,
        team_code=context.team_code,
        price=context.price,
        projected_points=sum(p.expected_points for p in projections),
        next_gameweek_points=projections[0].expected_points if projections else 0.0,
        fixtures=describe_upcoming(projections, team_names),
        form_points=context.points,
        minutes=context.minutes,
        starts=context.starts,
        goals=context.goals,
        assists=context.assists,
        expected_goals=context.expected_goals,
        expected_assists=context.expected_assists,
        defcon_rate=context.defcon_hits / matches if context.minutes else 0.0,
        availability=context.availability_note,
    )


__all__ = [
    "DIFFICULTY_WORDS",
    "PlayerComparison",
    "PlayerContext",
    "build_comparison",
    "describe_fixtures",
    "describe_upcoming",
    "load_contexts",
    "selection_reason",
]
