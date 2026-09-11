"""Automatic substitutions.

FPL replaces any starter who finishes the gameweek on zero minutes with the
first bench player who can legally take their place. The rules that make this
fiddly:

* A goalkeeper can only be replaced by the substitute goalkeeper, and the
  substitute goalkeeper can only replace a goalkeeper.
* Outfield substitutes are tried strictly in bench order, and a substitute is
  skipped if bringing them on would leave an illegal formation. Skipping one
  does not disqualify it from covering a later vacancy.
* A substitute who themselves played zero minutes cannot come on.
* Nothing is decided until a player's gameweek fixtures have all finished. In a
  double gameweek a player who is an unused substitute in the first match may
  still start the second, and mid-gameweek a player who has not kicked off yet
  is indistinguishable from one who was dropped.

The outfield pass is greedy in bench order rather than an optimisation: FPL
does not search for the substitution set that scores most, it walks the bench.
"""

from __future__ import annotations

from .constants import Position
from .squad import is_valid_formation
from .types import Lineup, Results, Roster, Substitution


def _played(element: int, results: Results) -> bool:
    """Did this player record any minutes across the whole gameweek?"""
    result = results.get(element)
    return result is not None and result.minutes > 0


def _settled(element: int, results: Results) -> bool:
    """Are all of this player's gameweek fixtures finished?

    A player with no result row at all is treated as settled on zero minutes:
    that is how a blank gameweek arrives, since the club simply has no fixture.
    """
    result = results.get(element)
    return result is None or result.fixtures_finished


def apply_auto_subs(
    lineup: Lineup,
    roster: Roster,
    results: Results,
) -> tuple[Lineup, list[Substitution]]:
    """Return the lineup after automatic substitutions, plus what changed.

    The returned lineup keeps FPL's slot semantics: a player substituted on
    takes the outgoing player's place in `starters`, and the outgoing player
    moves into the bench slot that was vacated. That keeps bench order stable
    for anything rendered afterwards.
    """
    starters = list(lineup.starters)
    bench = list(lineup.bench)
    substitutions: list[Substitution] = []

    # --- Goalkeeper -------------------------------------------------------
    # Handled first and separately: the two keepers can only swap with each
    # other, so this can never interact with the outfield pass.
    gk_index = next(
        (i for i, e in enumerate(starters) if roster[e].position is Position.GKP),
        None,
    )
    if gk_index is not None:
        starting_gk = starters[gk_index]
        bench_gk = bench[0]
        if (
            _settled(starting_gk, results)
            and not _played(starting_gk, results)
            and _played(bench_gk, results)
        ):
            starters[gk_index], bench[0] = bench_gk, starting_gk
            substitutions.append(Substitution(out_element=starting_gk, in_element=bench_gk))

    # --- Outfield ---------------------------------------------------------
    # Bench slots 2-4 (indices 1-3), tried in order for each vacancy.
    used_bench_slots: set[int] = set()

    for xi_index, element in enumerate(starters):
        if roster[element].position is Position.GKP:
            continue
        if not _settled(element, results) or _played(element, results):
            continue

        for bench_index in range(1, len(bench)):
            if bench_index in used_bench_slots:
                continue
            candidate = bench[bench_index]
            if not _played(candidate, results):
                continue

            trial = list(starters)
            trial[xi_index] = candidate
            if not is_valid_formation(trial, roster):
                # Bringing this substitute on would break the formation, so FPL
                # moves down the bench. The substitute stays available for a
                # later vacancy where they would fit.
                continue

            starters[xi_index] = candidate
            bench[bench_index] = element
            used_bench_slots.add(bench_index)
            substitutions.append(Substitution(out_element=element, in_element=candidate))
            break

    new_lineup = Lineup(
        starters=tuple(starters),
        bench=tuple(bench),
        captain=lineup.captain,
        vice_captain=lineup.vice_captain,
    )
    return new_lineup, substitutions


__all__ = ["apply_auto_subs"]
