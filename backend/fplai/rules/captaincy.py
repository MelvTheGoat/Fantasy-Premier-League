"""Captaincy, including the Triple Captain chip.

The captain's score is doubled, or tripled under Triple Captain. If the captain
records zero minutes across the gameweek the armband passes to the
vice-captain, who is multiplied by the same factor. If neither plays, nobody is
multiplied.

The armband passes on minutes, not on whether the captain was substituted: a
captain who played one minute keeps it even if they scored nothing.
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import CAPTAIN_MULTIPLIER, TRIPLE_CAPTAIN_MULTIPLIER, Chip
from .types import Lineup, Results


@dataclass(frozen=True, slots=True)
class CaptaincyOutcome:
    """Who ended up wearing the armband, and what their score is multiplied by."""

    element: int | None
    multiplier: int
    passed_to_vice: bool
    triple: bool

    @property
    def extra_multiplier(self) -> int:
        """Points *added* per point the captain scored, i.e. 1 normally, 2 for TC."""
        return max(self.multiplier - 1, 0)


def captain_multiplier(chip: Chip | None) -> int:
    return TRIPLE_CAPTAIN_MULTIPLIER if chip is Chip.TRIPLE_CAPTAIN else CAPTAIN_MULTIPLIER


def resolve_captaincy(
    lineup: Lineup,
    results: Results,
    chip: Chip | None = None,
) -> CaptaincyOutcome:
    """Decide who the armband ends up on for this gameweek."""
    multiplier = captain_multiplier(chip)
    triple = chip is Chip.TRIPLE_CAPTAIN

    def played(element: int) -> bool:
        result = results.get(element)
        return result is not None and result.minutes > 0

    if played(lineup.captain):
        return CaptaincyOutcome(lineup.captain, multiplier, passed_to_vice=False, triple=triple)
    if played(lineup.vice_captain):
        return CaptaincyOutcome(
            lineup.vice_captain, multiplier, passed_to_vice=True, triple=triple
        )
    return CaptaincyOutcome(None, 1, passed_to_vice=False, triple=triple)


__all__ = ["CaptaincyOutcome", "captain_multiplier", "resolve_captaincy"]
