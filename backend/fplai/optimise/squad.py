"""Squad selection as an integer linear program.

Greedy picking cannot do this job. The constraints interact: taking the best
forward may leave too little money for a fifth defender, and the three-per-club
limit binds precisely where the best players cluster. An ILP handles all of it
at once and returns the genuine optimum rather than something that looks
reasonable.

The formulation, per player, is two binary variables:

    squad[i]   the player is one of the 15
    start[i]   the player is one of the 11

with `start[i] <= squad[i]`, so a starter is necessarily in the squad. The
objective weights starters fully and bench players at a fraction, which is what
makes the optimiser fill the bench with cheap, playing bodies rather than
spending £20m on players who never count.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pulp

from ..rules.constants import (
    FORMATION_MAX,
    FORMATION_MIN,
    MAX_PLAYERS_PER_CLUB,
    SQUAD_COMPOSITION,
    STARTING_BUDGET,
    STARTING_XI_SIZE,
    Position,
)
from ..rules.types import Lineup, Roster, Squad, SquadPick

logger = logging.getLogger(__name__)

#: How much a bench place is worth relative to a starting place. Non-zero
#: because substitutes do come on -- a bench of players who will not kick a ball
#: is worth nothing when a starter is rested. Low because most weeks they score
#: nothing for you.
BENCH_WEIGHT = 0.12

#: The first bench outfield slot is worth more than the last: it is reached
#: first when a starter blanks, so its weight is scaled up.
BENCH_SLOT_WEIGHTS = (0.20, 0.10, 0.06)

#: Solver time limit. The problem is small -- 655 players, a few dozen
#: constraints -- and normally solves in well under a second.
SOLVER_TIME_LIMIT = 30


class InfeasibleSquad(RuntimeError):
    """Raised when no legal squad exists within the constraints given."""


@dataclass(frozen=True, slots=True)
class SquadSelection:
    """An optimiser result: the 15, the 11, the bench order and the armband."""

    squad: Squad
    lineup: Lineup
    expected_points: float
    """Projected points for the starting XI, with the captain doubled."""
    total_cost: int
    objective: float
    """The optimiser's own objective value, bench weighting included."""

    @property
    def bank(self) -> int:
        return self.squad.bank


def _bench_order(
    bench: list[int], roster: Roster, points: dict[int, float]
) -> tuple[int, ...]:
    """Order the bench the way FPL expects: keeper first, then by usefulness.

    The outfield substitutes are ordered by projected points, because the first
    one is the most likely to be called on.
    """
    keepers = [e for e in bench if roster[e].position is Position.GKP]
    outfield = sorted(
        (e for e in bench if roster[e].position is not Position.GKP),
        key=lambda e: -points.get(e, 0.0),
    )
    return tuple(keepers + outfield)


def _pick_armband(
    starters: list[int], points: dict[int, float]
) -> tuple[int, int]:
    """Captain the highest projection, vice the second.

    The captain's points double, so this is simply the largest projection --
    there is no risk trade-off to make, because the vice-captaincy already
    covers the case where the captain does not play.
    """
    ranked = sorted(starters, key=lambda e: -points.get(e, 0.0))
    return ranked[0], ranked[1]


def optimise_squad(
    roster: Roster,
    points: dict[int, float],
    *,
    budget: int = STARTING_BUDGET,
    prices: dict[int, int] | None = None,
    excluded: set[int] | None = None,
    required: set[int] | None = None,
    owned: set[int] | None = None,
    max_transfers: int | None = None,
    bench_weight: float = BENCH_WEIGHT,
    captain_multiplier: int = 2,
) -> SquadSelection:
    """Pick the best legal 15 and the best legal XI within them.

    `points` is projected points per player for the gameweek being picked.
    `prices` overrides the roster's prices, which is how the Manager prices a
    transfer at selling price rather than market price.

    `owned` and `max_transfers` together cap how far the answer may move from a
    squad already held. Without them the optimiser returns the unconstrained
    best squad, which is right for Best XI and useless for the Manager -- it
    would rebuild from scratch every week. With them, the Manager can ask "what
    is the best squad reachable in one transfer" and compare that against two,
    three, or none.

    Raises `InfeasibleSquad` when the constraints cannot all be met -- most
    often because the budget is too small for the players still required.
    """
    prices = prices or {e: p.price for e, p in roster.items()}
    excluded = excluded or set()
    required = required or set()

    candidates = [e for e in roster if e not in excluded]
    if not candidates:
        raise InfeasibleSquad("no players available to pick from")

    problem = pulp.LpProblem("fpl_squad", pulp.LpMaximize)
    in_squad = {e: pulp.LpVariable(f"squad_{e}", cat="Binary") for e in candidates}
    in_xi = {e: pulp.LpVariable(f"start_{e}", cat="Binary") for e in candidates}
    is_captain = {e: pulp.LpVariable(f"captain_{e}", cat="Binary") for e in candidates}

    # A starter must be in the squad; a captain must be starting.
    for e in candidates:
        problem += in_xi[e] <= in_squad[e]
        problem += is_captain[e] <= in_xi[e]

    problem += pulp.lpSum(in_squad.values()) == sum(SQUAD_COMPOSITION.values())
    problem += pulp.lpSum(in_xi.values()) == STARTING_XI_SIZE
    problem += pulp.lpSum(is_captain.values()) == 1

    for position, required_count in SQUAD_COMPOSITION.items():
        members = [e for e in candidates if roster[e].position is position]
        problem += pulp.lpSum(in_squad[e] for e in members) == required_count
        problem += pulp.lpSum(in_xi[e] for e in members) >= FORMATION_MIN[position]
        problem += pulp.lpSum(in_xi[e] for e in members) <= FORMATION_MAX[position]

    clubs: dict[int, list[int]] = {}
    for e in candidates:
        clubs.setdefault(roster[e].team, []).append(e)
    for members in clubs.values():
        problem += pulp.lpSum(in_squad[e] for e in members) <= MAX_PLAYERS_PER_CLUB

    problem += pulp.lpSum(prices[e] * in_squad[e] for e in candidates) <= budget

    for e in required:
        if e not in in_squad:
            raise InfeasibleSquad(f"required player {e} is not available to pick")
        problem += in_squad[e] == 1

    if max_transfers is not None:
        if owned is None:
            raise ValueError("max_transfers needs `owned` to count transfers against")
        incoming = [e for e in candidates if e not in owned]
        problem += pulp.lpSum(in_squad[e] for e in incoming) <= max_transfers

    # Starters count fully, bench players at a fraction, and the captain's
    # projection is counted an extra time for the doubling.
    problem += pulp.lpSum(
        points.get(e, 0.0) * in_xi[e]
        + points.get(e, 0.0) * bench_weight * (in_squad[e] - in_xi[e])
        + points.get(e, 0.0) * (captain_multiplier - 1) * is_captain[e]
        for e in candidates
    )

    status = problem.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=SOLVER_TIME_LIMIT))
    if pulp.LpStatus[status] != "Optimal":
        raise InfeasibleSquad(
            f"no legal squad within £{budget / 10:.1f}m ({pulp.LpStatus[status]})"
        )

    squad_elements = [e for e in candidates if in_squad[e].value() > 0.5]
    starters = [e for e in squad_elements if in_xi[e].value() > 0.5]
    bench = [e for e in squad_elements if e not in starters]

    captain, vice_captain = _pick_armband(starters, points)
    ordered_starters = sorted(
        starters, key=lambda e: (roster[e].position, -points.get(e, 0.0))
    )

    total_cost = sum(prices[e] for e in squad_elements)
    squad = Squad(
        picks=tuple(SquadPick(e, prices[e]) for e in squad_elements),
        bank=budget - total_cost,
    )
    lineup = Lineup(
        starters=tuple(ordered_starters),
        bench=_bench_order(bench, roster, points),
        captain=captain,
        vice_captain=vice_captain,
    )

    expected = sum(points.get(e, 0.0) for e in starters) + points.get(captain, 0.0) * (
        captain_multiplier - 1
    )

    return SquadSelection(
        squad=squad,
        lineup=lineup,
        expected_points=expected,
        total_cost=total_cost,
        objective=pulp.value(problem.objective) or 0.0,
    )


def optimise_lineup(
    squad: Squad,
    roster: Roster,
    points: dict[int, float],
    *,
    captain_multiplier: int = 2,
    bench_counts: bool = False,
) -> Lineup:
    """Pick the best legal XI from a squad already owned.

    `bench_counts` is for Bench Boost, where every player scores and the only
    thing the XI choice still affects is who can be captained and who is
    exposed to automatic substitution.
    """
    elements = list(squad.elements)
    problem = pulp.LpProblem("fpl_lineup", pulp.LpMaximize)
    in_xi = {e: pulp.LpVariable(f"start_{e}", cat="Binary") for e in elements}

    problem += pulp.lpSum(in_xi.values()) == STARTING_XI_SIZE
    for position in Position:
        members = [e for e in elements if roster[e].position is position]
        problem += pulp.lpSum(in_xi[e] for e in members) >= FORMATION_MIN[position]
        problem += pulp.lpSum(in_xi[e] for e in members) <= FORMATION_MAX[position]

    weight = 1.0 if bench_counts else 0.0
    problem += pulp.lpSum(
        points.get(e, 0.0) * in_xi[e] + points.get(e, 0.0) * weight * (1 - in_xi[e])
        for e in elements
    )

    status = problem.solve(pulp.PULP_CBC_CMD(msg=0, timeLimit=SOLVER_TIME_LIMIT))
    if pulp.LpStatus[status] != "Optimal":
        raise InfeasibleSquad(f"no legal XI from this squad ({pulp.LpStatus[status]})")

    starters = [e for e in elements if in_xi[e].value() > 0.5]
    bench = [e for e in elements if e not in starters]
    captain, vice_captain = _pick_armband(starters, points)

    return Lineup(
        starters=tuple(sorted(starters, key=lambda e: (roster[e].position, -points.get(e, 0.0)))),
        bench=_bench_order(bench, roster, points),
        captain=captain,
        vice_captain=vice_captain,
    )


__all__ = [
    "BENCH_SLOT_WEIGHTS",
    "BENCH_WEIGHT",
    "InfeasibleSquad",
    "SquadSelection",
    "optimise_lineup",
    "optimise_squad",
]
