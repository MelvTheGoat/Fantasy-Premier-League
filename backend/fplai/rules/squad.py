"""Squad and formation legality.

Two separate questions live here:

* Is this set of 15 players a legal *squad*? (composition, budget, club limit)
* Is this set of 11 players a legal *starting XI*? (formation)

They are checked independently because a legal squad can be arranged into an
illegal XI, and the auto-substitution rules need the XI check on its own.
"""

from __future__ import annotations

from collections import Counter
from typing import Iterable, Sequence

from .constants import (
    BENCH_SIZE,
    FORMATION_MAX,
    FORMATION_MIN,
    MAX_PLAYERS_PER_CLUB,
    Position,
    SQUAD_COMPOSITION,
    SQUAD_SIZE,
    STARTING_BUDGET,
    STARTING_XI_SIZE,
)
from .types import (
    Lineup,
    Roster,
    Squad,
    ValidationResult,
    count_positions,
)


def formation_of(starters: Sequence[int], roster: Roster) -> tuple[int, int, int]:
    """Return the XI's shape as (defenders, midfielders, forwards).

    The goalkeeper is implicit -- a legal XI always has exactly one -- which is
    why FPL writes formations as 3-4-3 rather than 1-3-4-3.
    """
    counts = count_positions(starters, roster)
    return counts[Position.DEF], counts[Position.MID], counts[Position.FWD]


def format_formation(starters: Sequence[int], roster: Roster) -> str:
    return "-".join(str(n) for n in formation_of(starters, roster))


def is_valid_formation(starters: Sequence[int], roster: Roster) -> bool:
    """True if these 11 players can legally start.

    The formation is never fixed in advance: it falls out of whichever players
    are picked, so this only enforces the position bounds and the XI size.
    """
    if len(starters) != STARTING_XI_SIZE:
        return False
    counts = count_positions(starters, roster)
    return all(
        FORMATION_MIN[pos] <= counts[pos] <= FORMATION_MAX[pos] for pos in Position
    )


def validate_formation(starters: Sequence[int], roster: Roster) -> ValidationResult:
    """Same check as `is_valid_formation`, but reports every reason it failed."""
    result = ValidationResult()

    if len(starters) != STARTING_XI_SIZE:
        result.add(
            "xi_size",
            f"a starting XI must have {STARTING_XI_SIZE} players, got {len(starters)}",
        )
    if len(set(starters)) != len(starters):
        duplicates = [e for e, n in Counter(starters).items() if n > 1]
        result.add("xi_duplicate", f"player(s) named more than once in the XI: {duplicates}")

    missing = [e for e in starters if e not in roster]
    if missing:
        result.add("unknown_player", f"no roster entry for element(s) {missing}")
        return result

    counts = count_positions(starters, roster)
    for pos in Position:
        if counts[pos] < FORMATION_MIN[pos]:
            result.add(
                "formation_min",
                f"a starting XI needs at least {FORMATION_MIN[pos]} {pos.short_name}, "
                f"got {counts[pos]}",
            )
        elif counts[pos] > FORMATION_MAX[pos]:
            result.add(
                "formation_max",
                f"a starting XI may hold at most {FORMATION_MAX[pos]} {pos.short_name}, "
                f"got {counts[pos]}",
            )
    return result


def validate_squad(
    squad: Squad,
    roster: Roster,
    *,
    budget: int = STARTING_BUDGET,
    use_purchase_prices: bool = True,
) -> ValidationResult:
    """Check the 15-man squad against every squad-level rule.

    `use_purchase_prices` controls how the budget is measured. When a squad is
    being *built* the cost is what the manager pays, i.e. current market price.
    When an existing squad is being *re-checked*, the money already committed
    is what was paid for each player, which is what is stored on the picks.
    """
    result = ValidationResult()
    elements = squad.elements

    if len(elements) != SQUAD_SIZE:
        result.add("squad_size", f"a squad must have {SQUAD_SIZE} players, got {len(elements)}")
    if len(set(elements)) != len(elements):
        duplicates = [e for e, n in Counter(elements).items() if n > 1]
        result.add("squad_duplicate", f"player(s) picked more than once: {duplicates}")

    missing = [e for e in elements if e not in roster]
    if missing:
        result.add("unknown_player", f"no roster entry for element(s) {missing}")
        return result

    counts = count_positions(elements, roster)
    for pos, required in SQUAD_COMPOSITION.items():
        if counts[pos] != required:
            result.add(
                "squad_composition",
                f"a squad needs exactly {required} {pos.short_name}, got {counts[pos]}",
            )

    club_counts = Counter(roster[e].team for e in elements)
    for team, n in sorted(club_counts.items()):
        if n > MAX_PLAYERS_PER_CLUB:
            result.add(
                "club_limit",
                f"at most {MAX_PLAYERS_PER_CLUB} players may come from one club; "
                f"team {team} has {n}",
            )

    if use_purchase_prices:
        spend = sum(p.purchase_price for p in squad.picks)
    else:
        spend = sum(roster[e].price for e in elements)
    total = spend + squad.bank
    if total > budget:
        result.add(
            "budget",
            f"squad costs {_money(spend)} with {_money(squad.bank)} in the bank, "
            f"which is {_money(total - budget)} over the {_money(budget)} budget",
        )
    if squad.bank < 0:
        result.add("negative_bank", f"the bank cannot be negative, got {_money(squad.bank)}")

    return result


def validate_lineup(lineup: Lineup, squad: Squad, roster: Roster) -> ValidationResult:
    """Check that a lineup is a legal arrangement of exactly this squad."""
    result = ValidationResult()

    if len(lineup.starters) != STARTING_XI_SIZE:
        result.add(
            "xi_size",
            f"a starting XI must have {STARTING_XI_SIZE} players, got {len(lineup.starters)}",
        )
    if len(lineup.bench) != BENCH_SIZE:
        result.add(
            "bench_size",
            f"a bench must have {BENCH_SIZE} players, got {len(lineup.bench)}",
        )

    lineup_elements = lineup.elements
    if len(set(lineup_elements)) != len(lineup_elements):
        duplicates = [e for e, n in Counter(lineup_elements).items() if n > 1]
        result.add("lineup_duplicate", f"player(s) named more than once: {duplicates}")

    if set(lineup_elements) != set(squad.elements):
        stray = sorted(set(lineup_elements) - set(squad.elements))
        absent = sorted(set(squad.elements) - set(lineup_elements))
        if stray:
            result.add("lineup_not_in_squad", f"lineup names player(s) not owned: {stray}")
        if absent:
            result.add("lineup_missing_squad", f"squad player(s) left out of the lineup: {absent}")

    missing = [e for e in lineup_elements if e not in roster]
    if missing:
        result.add("unknown_player", f"no roster entry for element(s) {missing}")
        return result

    result.merge(validate_formation(lineup.starters, roster))

    # Bench slot 1 is reserved for the substitute goalkeeper, because a keeper
    # can only ever be replaced by the other keeper.
    if lineup.bench and roster[lineup.bench[0]].position is not Position.GKP:
        result.add(
            "bench_gk_slot",
            "the first bench slot must hold the substitute goalkeeper",
        )
    outfield_bench_keepers = [
        e for e in lineup.bench[1:] if roster[e].position is Position.GKP
    ]
    if outfield_bench_keepers:
        result.add(
            "bench_gk_slot",
            f"goalkeeper(s) {outfield_bench_keepers} cannot sit in an outfield bench slot",
        )

    if lineup.captain not in lineup.starters:
        result.add("captain_not_starting", "the captain must be in the starting XI")
    if lineup.vice_captain not in lineup.starters:
        result.add("vice_not_starting", "the vice-captain must be in the starting XI")
    if lineup.captain == lineup.vice_captain:
        result.add("captain_is_vice", "the captain and vice-captain must be different players")

    return result


def squad_cost(elements: Iterable[int], roster: Roster) -> int:
    """What it would cost to buy these players at today's prices, in tenths."""
    return sum(roster[e].price for e in elements)


def _money(tenths: int) -> str:
    return f"£{tenths / 10:.1f}m"


__all__ = [
    "format_formation",
    "formation_of",
    "is_valid_formation",
    "squad_cost",
    "validate_formation",
    "validate_lineup",
    "validate_squad",
]
