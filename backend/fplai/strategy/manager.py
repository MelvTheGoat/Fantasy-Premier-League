"""Model A: The Manager.

Carries one squad through the season under the same constraints a person
plays under: one free transfer a week, four points for each extra, a budget
that only grows through price rises, and two sets of chips that expire.

The central decision each week is not "what is the best squad" -- that is Best
XI's job -- but "is the best squad reachable from here worth what it costs to
get there". The Manager answers it by asking the optimiser for the best squad
at zero transfers, one, two and three, then comparing each against the hit it
would cost over the planning horizon. Rolling a transfer is a real answer and
often the right one.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from ..config import settings
from ..data.repository import (
    load_chips_used,
    load_roster_at_gameweek,
    save_chip_used,
    save_locked_picks,
    save_manager_state,
    save_transfer,
)
from ..model.projections import project_for_gameweek
from ..model.xpts import GameweekProjection
from ..optimise.squad import SquadSelection, optimise_lineup, optimise_squad
from ..rules.chips import available_chips, chip_deadline, chip_set_for_gameweek
from ..rules.constants import (
    INITIAL_FREE_TRANSFERS,
    STARTING_BUDGET,
    TRANSFER_HIT_COST,
    Chip,
)
from ..rules.pricing import selling_price
from ..rules.transfers import resolve_transfers
from ..rules.types import Lineup, Squad, Transfer
from .best_xi import team_names
from .explain import build_comparison, load_contexts, selection_reason

logger = logging.getLogger(__name__)

MODEL_ID = "manager"

#: How much each gameweek of the horizon counts toward a transfer decision.
#: The gameweek being picked counts fully; later ones count less, because a
#: projection four weeks out is a guess and the squad can change again before
#: it arrives.
HORIZON_DECAY = 0.82

#: The most transfers the Manager will consider making in one gameweek without
#: a chip. Beyond three the hits swamp any plausible gain.
MAX_TRANSFERS_CONSIDERED = 3

#: What each chip has to be worth before it is played, in projected points.
#:
#: One shared threshold does not work, because the chips measure different
#: things. Tripling a captain is worth the captain's score, and a premium
#: captain projects six to eight points in an ordinary week -- so a six-point
#: bar would fire Triple Captain in the first week of the season and waste it.
#: Each bar is instead set where that chip's *good* week starts, which in
#: practice means a double gameweek or a blank:
#:
#:   Bench Boost     a normal bench projects six to nine; sixteen means most of
#:                   the bench has two fixtures
#:   Triple Captain  ten-plus from one player is a double gameweek or a
#:                   premium in a very soft fixture
#:   Free Hit        twelve points better than the current squad means several
#:                   players are blanking
#:   Wildcard        twenty over the whole horizon, because a Wildcard is the
#:                   only chip whose benefit persists, and spending it early
#:                   forfeits the option later
CHIP_THRESHOLDS: dict[Chip, float] = {
    Chip.BENCH_BOOST: 16.0,
    Chip.TRIPLE_CAPTAIN: 10.0,
    Chip.FREE_HIT: 12.0,
    Chip.WILDCARD: 20.0,
}

#: The earliest gameweek the Manager will wildcard without being forced to.
#:
#: Before this, projections rest mostly on priors rather than on anything that
#: has happened, so a large apparent gain is a statement about the model's
#: uncertainty rather than about the squad. Wildcarding on it would burn the
#: chip to chase noise.
EARLIEST_VOLUNTARY_WILDCARD = 5


@dataclass(slots=True)
class ManagerState:
    """What the Manager carries from one deadline to the next."""

    squad: Squad | None = None
    free_transfers: int = INITIAL_FREE_TRANSFERS
    chips_used: list = field(default_factory=list)

    @property
    def has_squad(self) -> bool:
        return self.squad is not None


@dataclass(frozen=True, slots=True)
class TransferDecision:
    """The Manager's transfer choice for one gameweek, and why."""

    transfers: tuple[Transfer, ...]
    squad: Squad
    lineup: Lineup
    gain: float
    """Projected gain over the horizon, before the cost of any hit."""
    points_cost: int
    free_transfers_after: int
    reason: str
    comparisons: tuple[dict, ...] = ()

    @property
    def count(self) -> int:
        return len(self.transfers)

    @property
    def rolled(self) -> bool:
        return not self.transfers


@dataclass(frozen=True, slots=True)
class ChipDecision:
    """A chip the Manager chose to play, with the reasoning behind it."""

    chip: Chip | None
    reason: str
    gain: float = 0.0


def horizon_weights(horizon: int) -> list[float]:
    """Declining weights across the planning horizon."""
    return [HORIZON_DECAY**offset for offset in range(horizon)]


def weighted_points(
    projections: dict[int, list[GameweekProjection]],
    weights: list[float],
) -> dict[int, float]:
    """Collapse a player's horizon into one number the optimiser can use."""
    return {
        element: sum(
            weight * projection.expected_points
            for weight, projection in zip(weights, gameweeks, strict=False)
        )
        for element, gameweeks in projections.items()
    }


def immediate_points(
    projections: dict[int, list[GameweekProjection]],
) -> dict[int, float]:
    """Just the gameweek being picked, for lineup and captain choices."""
    return {
        element: gameweeks[0].expected_points if gameweeks else 0.0
        for element, gameweeks in projections.items()
    }


def selling_prices(squad: Squad, roster) -> dict[int, int]:
    """What each owned player would sell for, and what each other costs to buy."""
    prices = {element: player.price for element, player in roster.items()}
    for pick in squad.picks:
        if pick.element in roster:
            prices[pick.element] = selling_price(
                pick.purchase_price, roster[pick.element].price
            )
    return prices


def build_initial_squad(
    connection: sqlite3.Connection,
    gameweek: int,
    projections: dict[int, list[GameweekProjection]],
) -> SquadSelection:
    """The opening squad, picked on the horizon rather than one gameweek.

    Unlike Best XI, this squad has to last: it is chosen on weighted projected
    points across the planning horizon, so a player with one flattering fixture
    does not displace one who is good all season.
    """
    roster = load_roster_at_gameweek(connection, gameweek)
    weights = horizon_weights(len(next(iter(projections.values()), [])))
    return optimise_squad(roster, weighted_points(projections, weights), budget=STARTING_BUDGET)


def decide_transfers(
    connection: sqlite3.Connection,
    gameweek: int,
    state: ManagerState,
    projections: dict[int, list[GameweekProjection]],
    *,
    chip: Chip | None = None,
    hit_margin: float | None = None,
) -> TransferDecision:
    """Choose how many transfers to make, and which.

    Works by asking the optimiser for the best squad reachable in 0, 1, 2 and 3
    transfers, then picking whichever leaves the most points after hits. A
    transfer that gains less than four points plus a margin is not worth taking,
    and one that gains nothing is not worth making at all -- the free transfer
    banks instead.
    """
    hit_margin = settings.hit_margin if hit_margin is None else hit_margin
    squad = state.squad
    assert squad is not None, "decide_transfers needs an existing squad"

    roster = load_roster_at_gameweek(connection, gameweek)
    prices = selling_prices(squad, roster)
    weights = horizon_weights(len(next(iter(projections.values()), [])))
    horizon_points = weighted_points(projections, weights)
    now_points = immediate_points(projections)

    owned = set(squad.elements)
    budget = sum(prices[e] for e in owned if e in prices) + squad.bank

    unlimited = chip in (Chip.WILDCARD, Chip.FREE_HIT)
    limits = (
        [len(owned)] if unlimited else list(range(MAX_TRANSFERS_CONSIDERED + 1))
    )

    best: tuple[float, int, SquadSelection] | None = None
    baseline = None

    for limit in limits:
        try:
            selection = optimise_squad(
                roster,
                horizon_points,
                budget=budget,
                prices=prices,
                owned=owned,
                max_transfers=limit,
            )
        except Exception as error:  # noqa: BLE001 - infeasible at this limit
            logger.debug("no squad at %d transfers: %s", limit, error)
            continue

        moved = len(set(selection.squad.elements) - owned)
        outcome = resolve_transfers(moved, state.free_transfers, chip)
        net = selection.objective - outcome.points_cost

        if limit == 0:
            baseline = selection.objective

        # A hit has to clear four points *plus* a margin for the projection
        # being wrong, which it routinely is.
        if outcome.points_cost and baseline is not None:
            if selection.objective - baseline < outcome.points_cost + hit_margin:
                continue

        if best is None or net > best[0]:
            best = (net, moved, selection)

    if best is None:
        raise RuntimeError(f"no feasible squad for GW{gameweek}")

    _, moved, selection = best
    outcome = resolve_transfers(moved, state.free_transfers, chip)

    transfers, comparisons = _describe_transfers(
        connection, gameweek, squad, selection.squad, prices, projections, roster
    )

    lineup = optimise_lineup(
        selection.squad,
        roster,
        now_points,
        captain_multiplier=3 if chip is Chip.TRIPLE_CAPTAIN else 2,
        bench_counts=chip is Chip.BENCH_BOOST,
    )

    gain = (selection.objective - baseline) if baseline is not None else 0.0
    reason = _transfer_reason(moved, outcome.points_cost, gain, state.free_transfers, chip)

    return TransferDecision(
        transfers=transfers,
        squad=selection.squad,
        lineup=lineup,
        gain=gain,
        points_cost=outcome.points_cost,
        free_transfers_after=outcome.free_transfers_after,
        reason=reason,
        comparisons=comparisons,
    )


def _transfer_reason(
    moved: int, cost: int, gain: float, free_transfers: int, chip: Chip | None
) -> str:
    if chip is Chip.WILDCARD:
        return f"Wildcard: {moved} transfers, no hits, {gain:+.1f} projected over the horizon."
    if chip is Chip.FREE_HIT:
        return f"Free Hit: {moved} transfers for this gameweek only, {gain:+.1f} projected."
    if not moved:
        banked = min(free_transfers + 1, 5)
        return (
            "Rolled transfer: no move beat holding by enough. "
            f"{banked} free transfer{'s' if banked != 1 else ''} banked."
        )
    if cost:
        return (
            f"{moved} transfers, {cost // TRANSFER_HIT_COST} hit"
            f"{'s' if cost > TRANSFER_HIT_COST else ''} costing {cost} pts: "
            f"{gain:+.1f} projected over the horizon, clearing the hit by "
            f"{gain - cost:+.1f}."
        )
    plural = "s" if moved != 1 else ""
    return f"{moved} free transfer{plural}: {gain:+.1f} projected over the horizon."


def _describe_transfers(
    connection: sqlite3.Connection,
    gameweek: int,
    before: Squad,
    after: Squad,
    prices: dict[int, int],
    projections: dict[int, list[GameweekProjection]],
    roster,
) -> tuple[tuple[Transfer, ...], tuple[dict, ...]]:
    """Pair each departure with an arrival and build the side-by-side stats.

    The pairing is by position, which is how a person describes a transfer --
    "Saka out, Mbeumo in" -- even though the optimiser only produced a set.
    """
    out_elements = sorted(set(before.elements) - set(after.elements))
    in_elements = sorted(set(after.elements) - set(before.elements))

    contexts = load_contexts(connection, gameweek)
    names = team_names(connection)

    transfers: list[Transfer] = []
    comparisons: list[dict] = []

    remaining_in = list(in_elements)
    for out_element in out_elements:
        position = roster[out_element].position
        match = next(
            (e for e in remaining_in if roster[e].position is position),
            remaining_in[0] if remaining_in else None,
        )
        if match is None:
            break
        remaining_in.remove(match)

        transfers.append(
            Transfer(
                out_element=out_element,
                in_element=match,
                selling_price=prices[out_element],
                purchase_price=roster[match].price,
            )
        )

        out_comparison = build_comparison(
            contexts[out_element], projections.get(out_element, []), names
        )
        in_comparison = build_comparison(
            contexts[match], projections.get(match, []), names
        )
        comparisons.append(
            {
                "out": out_comparison.as_dict(),
                "in": in_comparison.as_dict(),
                "projected_gain": round(
                    in_comparison.projected_points - out_comparison.projected_points, 2
                ),
                "price_change": roster[match].price - prices[out_element],
            }
        )

    return tuple(transfers), tuple(comparisons)


def decide_chip(
    connection: sqlite3.Connection,
    gameweek: int,
    state: ManagerState,
    projections: dict[int, list[GameweekProjection]],
) -> ChipDecision:
    """Decide whether to play a chip, and which.

    Each available chip is valued at what it would add *this* gameweek, and the
    best is played only if it clears a threshold -- chips are scarce, and a
    marginal week is not worth one. The exception is expiry: when the first set
    has as many gameweeks left as unused chips, the best available is played
    regardless, because letting one expire unused is strictly worse.
    """
    available = available_chips(gameweek, state.chips_used)
    if not available or state.squad is None:
        return ChipDecision(None, "")

    roster = load_roster_at_gameweek(connection, gameweek)
    now_points = immediate_points(projections)
    squad = state.squad

    baseline_lineup = optimise_lineup(squad, roster, now_points)
    baseline = sum(now_points.get(e, 0.0) for e in baseline_lineup.starters)
    baseline += now_points.get(baseline_lineup.captain, 0.0)

    values: dict[Chip, tuple[float, str]] = {}

    if Chip.BENCH_BOOST in available:
        bench_points = sum(now_points.get(e, 0.0) for e in baseline_lineup.bench)
        doubles = sum(
            1 for e in squad.elements
            if projections.get(e) and projections[e][0].is_double
        )
        note = f"bench projects {bench_points:.1f} pts"
        if doubles:
            note = f"{doubles} players with two fixtures, {note}"
        values[Chip.BENCH_BOOST] = (bench_points, note)

    if Chip.TRIPLE_CAPTAIN in available:
        captain_points = now_points.get(baseline_lineup.captain, 0.0)
        name = roster[baseline_lineup.captain].web_name
        doubled = (
            projections.get(baseline_lineup.captain)
            and projections[baseline_lineup.captain][0].is_double
        )
        note = f"{name} projects {captain_points:.1f} pts"
        if doubled:
            note += " across two fixtures"
        values[Chip.TRIPLE_CAPTAIN] = (captain_points, note)

    if Chip.FREE_HIT in available or Chip.WILDCARD in available:
        prices = selling_prices(squad, roster)
        budget = sum(prices[e] for e in squad.elements if e in prices) + squad.bank
        try:
            free_squad = optimise_squad(roster, now_points, budget=budget, prices=prices)
            unlimited_gain = free_squad.expected_points - baseline
        except Exception:  # noqa: BLE001 - fall back to not playing the chip
            unlimited_gain = 0.0

        blanks = sum(
            1 for e in squad.elements
            if projections.get(e) and projections[e][0].is_blank
        )
        if Chip.FREE_HIT in available:
            note = f"{unlimited_gain:.1f} pts better with a free squad"
            if blanks:
                note = f"{blanks} players without a fixture, {note}"
            values[Chip.FREE_HIT] = (unlimited_gain, note)

        if Chip.WILDCARD in available:
            # A Wildcard is worth its gain across the whole horizon, not one
            # week, because the new squad is kept.
            weights = horizon_weights(len(next(iter(projections.values()), [])))
            horizon = weighted_points(projections, weights)
            try:
                wildcard_squad = optimise_squad(
                    roster, horizon, budget=budget, prices=prices
                )
                current = optimise_squad(
                    roster, horizon, budget=budget, prices=prices,
                    owned=set(squad.elements), max_transfers=0,
                )
                wildcard_gain = wildcard_squad.objective - current.objective
            except Exception:  # noqa: BLE001
                wildcard_gain = 0.0
            moved = 0
            if wildcard_gain:
                moved = len(set(wildcard_squad.squad.elements) - set(squad.elements))
            values[Chip.WILDCARD] = (
                wildcard_gain,
                f"{moved} changes worth {wildcard_gain:.1f} pts over the horizon",
            )

    if not values:
        return ChipDecision(None, "")

    deadline = chip_deadline(gameweek, state.chips_used)
    forced = deadline.must_play_now and chip_set_for_gameweek(gameweek) == 1

    if (
        Chip.WILDCARD in values
        and gameweek < EARLIEST_VOLUNTARY_WILDCARD
        and not forced
    ):
        del values[Chip.WILDCARD]

    if not values:
        return ChipDecision(None, "")

    # Rank chips by how far each clears its own bar, not by raw points: ten
    # points from Triple Captain is a good week, ten from Bench Boost is not.
    def margin(item) -> float:
        chip, (gain, _) = item
        return gain - CHIP_THRESHOLDS[chip]

    chip, (gain, note) = max(values.items(), key=margin)

    if gain >= CHIP_THRESHOLDS[chip]:
        return ChipDecision(chip, f"GW{gameweek}: {note}.", gain)

    if forced:
        return ChipDecision(
            chip,
            f"GW{gameweek}: played before the GW{deadline.last_gameweek} expiry rather "
            f"than lose it -- {note}.",
            gain,
        )

    return ChipDecision(None, "")


def lock_gameweek(
    connection: sqlite3.Connection,
    gameweek: int,
    state: ManagerState,
    *,
    projections: dict[int, list[GameweekProjection]] | None = None,
) -> ManagerState:
    """Run one gameweek: chip, transfers, lineup, then store it permanently."""
    if projections is None:
        projections = project_for_gameweek(
            connection, gameweek, horizon=settings.planning_horizon
        )

    roster = load_roster_at_gameweek(connection, gameweek)
    now_points = immediate_points(projections)
    contexts = load_contexts(connection, gameweek)
    names = team_names(connection)

    if not state.has_squad:
        selection = build_initial_squad(connection, gameweek, projections)
        lineup = optimise_lineup(selection.squad, roster, now_points)
        decision = TransferDecision(
            transfers=(), squad=selection.squad, lineup=lineup, gain=0.0,
            points_cost=0, free_transfers_after=INITIAL_FREE_TRANSFERS,
            reason="Opening squad.",
        )
        chip_decision = ChipDecision(None, "")
    else:
        chip_decision = decide_chip(connection, gameweek, state, projections)
        decision = decide_transfers(
            connection, gameweek, state, projections, chip=chip_decision.chip
        )

    reasons = {
        element: selection_reason(
            contexts[element],
            projections[element][0],
            names,
            is_captain=element == decision.lineup.captain,
        )
        for element in decision.squad.elements
        if element in contexts and projections.get(element)
    }

    save_locked_picks(
        connection, MODEL_ID, gameweek, decision.lineup, decision.squad,
        prices={e: p.price for e, p in roster.items()}, reasons=reasons,
    )

    for transfer, comparison in zip(
        decision.transfers, decision.comparisons, strict=False
    ):
        save_transfer(
            connection, gameweek,
            out_element=transfer.out_element, in_element=transfer.in_element,
            selling_price_value=transfer.selling_price,
            purchase_price_value=transfer.purchase_price,
            comparison=comparison, reason=decision.reason,
            was_hit=bool(decision.points_cost),
        )

    if chip_decision.chip:
        save_chip_used(
            connection, chip_decision.chip, gameweek,
            chip_set_for_gameweek(gameweek), chip_decision.reason,
        )

    squad_value = sum(
        selling_price(p.purchase_price, roster[p.element].price)
        for p in decision.squad.picks if p.element in roster
    )
    save_manager_state(
        connection, gameweek,
        bank=decision.squad.bank, squad_value=squad_value,
        free_transfers=state.free_transfers,
        free_transfers_after=decision.free_transfers_after,
        transfers_made=decision.count, transfer_cost=decision.points_cost,
        chip=str(chip_decision.chip) if chip_decision.chip else None,
        chip_reason=chip_decision.reason or None,
        roll_reason=decision.reason if decision.rolled else None,
    )

    logger.info(
        "GW%d: %d transfers, %d pts cost, chip=%s",
        gameweek, decision.count, decision.points_cost, chip_decision.chip,
    )

    # A Free Hit squad lasts one gameweek only: the squad carried forward is
    # the one that was in place before the chip was played.
    carried = state.squad if chip_decision.chip is Chip.FREE_HIT else decision.squad

    return ManagerState(
        squad=carried,
        free_transfers=decision.free_transfers_after,
        chips_used=load_chips_used(connection),
    )


__all__ = [
    "CHIP_THRESHOLDS",
    "EARLIEST_VOLUNTARY_WILDCARD",
    "HORIZON_DECAY",
    "MAX_TRANSFERS_CONSIDERED",
    "MODEL_ID",
    "ChipDecision",
    "ManagerState",
    "TransferDecision",
    "build_initial_squad",
    "decide_chip",
    "decide_transfers",
    "horizon_weights",
    "immediate_points",
    "lock_gameweek",
    "selling_prices",
    "weighted_points",
]
