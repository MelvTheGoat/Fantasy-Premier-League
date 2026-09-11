"""The points a player earns for each action.

FPL publishes its own scoring table on `bootstrap-static/` under
`game_config.scoring`, so this reads it rather than hardcoding it. That is not
pedantry: goalkeeper goals are worth 10 points in 2026/27, up from 6, and a
hardcoded table carried over from an earlier season would mis-project every
goalkeeper without ever failing a test.

Only the *rates* the API omits are constants here -- the number of saves per
point and the number of goals conceded per deduction -- and they are named and
commented rather than inlined.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .constants import Position

#: Saves needed for one point. Not published in `game_config.scoring`, which
#: gives only the value of the award.
SAVES_PER_POINT = 3

#: Goals conceded per deduction, for goalkeepers and defenders.
GOALS_CONCEDED_PER_DEDUCTION = 2

#: How the API names positions in the scoring table.
_POSITION_KEYS = {
    "GKP": Position.GKP,
    "DEF": Position.DEF,
    "MID": Position.MID,
    "FWD": Position.FWD,
}


def _by_position(raw: dict | int | None, default: int = 0) -> dict[Position, int]:
    """Read a per-position scoring entry, tolerating a flat value.

    Some entries are a single number that applies to every position, others a
    per-position mapping. Both shapes appear in the same table.
    """
    if raw is None:
        return dict.fromkeys(Position, default)
    if isinstance(raw, (int, float)):
        return dict.fromkeys(Position, int(raw))
    return {
        position: int(raw.get(key, default)) for key, position in _POSITION_KEYS.items()
    }


@dataclass(frozen=True)
class ScoringTable:
    """What each action is worth, per position where that matters."""

    long_play: int = 2
    """Points for playing 60 minutes or more."""
    short_play: int = 1
    """Points for playing at least one minute but under 60."""

    goals_scored: dict[Position, int] = field(
        default_factory=lambda: {
            Position.GKP: 10,
            Position.DEF: 6,
            Position.MID: 5,
            Position.FWD: 4,
        }
    )
    assists: int = 3
    clean_sheets: dict[Position, int] = field(
        default_factory=lambda: {
            Position.GKP: 4,
            Position.DEF: 4,
            Position.MID: 1,
            Position.FWD: 0,
        }
    )
    goals_conceded: dict[Position, int] = field(
        default_factory=lambda: {
            Position.GKP: -1,
            Position.DEF: -1,
            Position.MID: 0,
            Position.FWD: 0,
        }
    )
    defensive_contribution: dict[Position, int] = field(
        default_factory=lambda: {
            Position.GKP: 0,
            Position.DEF: 2,
            Position.MID: 2,
            Position.FWD: 2,
        }
    )

    saves: int = 1
    penalties_saved: int = 5
    penalties_missed: int = -2
    yellow_cards: int = -1
    red_cards: int = -3
    own_goals: int = -2
    bonus: int = 1

    saves_per_point: int = SAVES_PER_POINT
    goals_conceded_per_deduction: int = GOALS_CONCEDED_PER_DEDUCTION

    @classmethod
    def from_bootstrap(cls, bootstrap: dict) -> ScoringTable:
        """Build the table from `bootstrap-static/`.

        Falls back to the dataclass defaults for anything the payload omits, so
        a new field appearing upstream cannot break projection.
        """
        scoring = (bootstrap.get("game_config") or {}).get("scoring") or {}
        if not scoring:
            return cls()

        defaults = cls()
        return cls(
            long_play=int(scoring.get("long_play", defaults.long_play)),
            short_play=int(scoring.get("short_play", defaults.short_play)),
            goals_scored=_by_position(scoring.get("goals_scored")),
            assists=int(scoring.get("assists", defaults.assists)),
            clean_sheets=_by_position(scoring.get("clean_sheets")),
            goals_conceded=_by_position(scoring.get("goals_conceded")),
            defensive_contribution=_by_position(scoring.get("defensive_contribution")),
            saves=int(scoring.get("saves", defaults.saves)),
            penalties_saved=int(scoring.get("penalties_saved", defaults.penalties_saved)),
            penalties_missed=int(
                scoring.get("penalties_missed", defaults.penalties_missed)
            ),
            yellow_cards=int(scoring.get("yellow_cards", defaults.yellow_cards)),
            red_cards=int(scoring.get("red_cards", defaults.red_cards)),
            own_goals=int(scoring.get("own_goals", defaults.own_goals)),
            bonus=int(scoring.get("bonus", defaults.bonus)),
        )

    def appearance_points(self, minutes: float) -> int:
        if minutes >= 60:
            return self.long_play
        if minutes >= 1:
            return self.short_play
        return 0

    def goal_points(self, position: Position) -> int:
        return self.goals_scored[position]

    def clean_sheet_points(self, position: Position) -> int:
        return self.clean_sheets[position]

    def defcon_points(self, position: Position) -> int:
        return self.defensive_contribution[position]

    def concession_points(self, position: Position, goals: int) -> int:
        """Deduction for goals conceded, applied per whole pair."""
        return (goals // self.goals_conceded_per_deduction) * self.goals_conceded[position]

    def save_points(self, saves: int) -> int:
        return (saves // self.saves_per_point) * self.saves


#: The 2026/27 table as published, used when no payload is to hand.
DEFAULT_SCORING = ScoringTable()

__all__ = [
    "DEFAULT_SCORING",
    "GOALS_CONCEDED_PER_DEDUCTION",
    "SAVES_PER_POINT",
    "ScoringTable",
]
