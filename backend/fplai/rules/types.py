"""Value types shared by every rule.

These are deliberately plain: the rules engine must be testable without a
database, an HTTP client, or a solver. Everything an FPL rule needs about a
player is either its identity (element id), its position, its club, or a price.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable, Iterator, Mapping, Sequence

from .constants import BENCH_SIZE, Chip, Position, SQUAD_SIZE, STARTING_XI_SIZE


@dataclass(frozen=True, slots=True)
class Player:
    """Static facts about a player within one gameweek.

    `position` and `team` always come from the FPL API's ``element_type`` and
    ``team`` fields for the season being played -- never from a hardcoded map.
    """

    element: int
    position: Position
    team: int
    price: int
    """Current market price, in tenths of a million."""
    web_name: str = ""

    def __post_init__(self) -> None:
        if self.price < 0:
            raise ValueError(f"price must be non-negative, got {self.price}")


@dataclass(frozen=True, slots=True)
class SquadPick:
    """One of the 15 players a manager owns, with the price they paid for it."""

    element: int
    purchase_price: int
    """Price paid, in tenths. Needed to compute the selling price later."""


@dataclass(frozen=True, slots=True)
class Lineup:
    """How a 15-man squad is arranged for one gameweek.

    `bench` is in FPL's own order: index 0 is always the substitute
    goalkeeper, then the three outfield substitutes in the order they should be
    brought on.
    """

    starters: tuple[int, ...]
    bench: tuple[int, ...]
    captain: int
    vice_captain: int

    def __iter__(self) -> Iterator[int]:
        yield from self.starters
        yield from self.bench

    @property
    def elements(self) -> tuple[int, ...]:
        return self.starters + self.bench

    @property
    def bench_goalkeeper(self) -> int:
        return self.bench[0]

    @property
    def bench_outfield(self) -> tuple[int, ...]:
        return self.bench[1:]

    def squad_position(self, element: int) -> int:
        """FPL's 1-based squad slot: 1-11 start, 12-15 are the bench."""
        return self.elements.index(element) + 1


@dataclass(frozen=True, slots=True)
class Squad:
    """A manager's 15 players plus the cash left in the bank."""

    picks: tuple[SquadPick, ...]
    bank: int = 0
    """Unspent money, in tenths of a million."""

    @property
    def elements(self) -> tuple[int, ...]:
        return tuple(p.element for p in self.picks)

    def pick(self, element: int) -> SquadPick:
        for p in self.picks:
            if p.element == element:
                return p
        raise KeyError(f"element {element} is not in this squad")

    def purchase_price(self, element: int) -> int:
        return self.pick(element).purchase_price

    def with_picks(self, picks: Iterable[SquadPick], bank: int) -> Squad:
        return replace(self, picks=tuple(picks), bank=bank)


@dataclass(frozen=True, slots=True)
class PlayerGameweekResult:
    """What a player actually did in a gameweek, from ``event/{gw}/live/``.

    `minutes` and `total_points` are summed across every fixture the player's
    club played in the gameweek, so a double gameweek arrives here as one row
    with both fixtures already added together.

    `fixtures_finished` is False while any of the player's gameweek fixtures is
    still to be played or is in progress. Automatic substitutions must not be
    applied to a player until this is True, otherwise a player yet to kick off
    looks identical to one who was left out.
    """

    element: int
    minutes: int
    total_points: int
    fixtures_finished: bool = True
    fixture_count: int = 1


#: Convenience alias for the per-gameweek result lookup passed into scoring.
Results = Mapping[int, PlayerGameweekResult]

#: Convenience alias for the per-gameweek player lookup passed into the rules.
Roster = Mapping[int, Player]


@dataclass(frozen=True, slots=True)
class Transfer:
    """One player swapped for another at a deadline."""

    out_element: int
    in_element: int
    selling_price: int
    purchase_price: int

    @property
    def cash_delta(self) -> int:
        """Change to the bank, in tenths. Positive means money freed up."""
        return self.selling_price - self.purchase_price


@dataclass(frozen=True, slots=True)
class Substitution:
    """An automatic substitution the rules engine applied."""

    out_element: int
    in_element: int
    reason: str = "played 0 minutes"


@dataclass(frozen=True, slots=True)
class ChipUsage:
    """A chip played in a gameweek, tagged with which of the two sets it came from."""

    chip: Chip
    gameweek: int

    @property
    def chip_set(self) -> int:
        from .chips import chip_set_for_gameweek

        return chip_set_for_gameweek(self.gameweek)


@dataclass(frozen=True, slots=True)
class ValidationError:
    """A single broken rule. Collected rather than raised so a caller can show
    a user every problem with a squad at once instead of only the first."""

    code: str
    message: str


@dataclass(slots=True)
class ValidationResult:
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def __bool__(self) -> bool:
        return self.ok

    def add(self, code: str, message: str) -> None:
        self.errors.append(ValidationError(code, message))

    def merge(self, other: ValidationResult) -> ValidationResult:
        self.errors.extend(other.errors)
        return self

    def raise_if_invalid(self) -> None:
        if self.errors:
            joined = "; ".join(f"[{e.code}] {e.message}" for e in self.errors)
            raise RuleViolation(joined)

    @property
    def codes(self) -> list[str]:
        return [e.code for e in self.errors]


class RuleViolation(Exception):
    """Raised when code that should already have checked a rule did not."""


def positions_of(elements: Sequence[int], roster: Roster) -> list[Position]:
    return [roster[e].position for e in elements]


def count_positions(elements: Sequence[int], roster: Roster) -> dict[Position, int]:
    counts = dict.fromkeys(Position, 0)
    for element in elements:
        counts[roster[element].position] += 1
    return counts


__all__ = [
    "BENCH_SIZE",
    "SQUAD_SIZE",
    "STARTING_XI_SIZE",
    "ChipUsage",
    "Lineup",
    "Player",
    "PlayerGameweekResult",
    "Results",
    "Roster",
    "RuleViolation",
    "Squad",
    "SquadPick",
    "Substitution",
    "Transfer",
    "ValidationError",
    "ValidationResult",
    "count_positions",
    "positions_of",
]
