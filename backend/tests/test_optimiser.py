"""Squad optimisation.

The constraints are the point. A greedy pick can satisfy any one of them and
still fail the rest, so these tests push on each in turn -- budget, club limit,
composition, formation -- and check that the optimiser genuinely finds the best
legal answer rather than a plausible one.
"""

from __future__ import annotations

import pytest

from fplai.optimise.squad import (
    InfeasibleSquad,
    optimise_lineup,
    optimise_squad,
)
from fplai.rules.constants import MAX_PLAYERS_PER_CLUB, STARTING_BUDGET, Position
from fplai.rules.squad import (
    format_formation,
    is_valid_formation,
    validate_lineup,
    validate_squad,
)
from fplai.rules.types import Player

#: Enough players in every position and club to leave the optimiser real choices.
POSITION_COUNTS = {Position.GKP: 12, Position.DEF: 30, Position.MID: 30, Position.FWD: 18}


def build_roster(
    *, price: int = 50, prices: dict[int, int] | None = None, teams: int = 20
) -> dict[int, Player]:
    """A pool with real choices in every position.

    Clubs are assigned from a counter that runs across positions, not one that
    restarts per position. Restarting would put the best goalkeeper, defender,
    midfielder and forward all on club 1, so the three-per-club limit would bind
    on the top picks in every test and mask whatever each one meant to check.
    """
    prices = prices or {}
    roster: dict[int, Player] = {}
    element = 1
    for position, count in POSITION_COUNTS.items():
        for index in range(count):
            roster[element] = Player(
                element=element,
                position=position,
                team=((element - 1) % teams) + 1,
                price=prices.get(element, price),
                web_name=f"{position.name}{index}",
            )
            element += 1
    return roster


#: Prices spanning £4.0m to £9.9m, so a legal squad is affordable but the
#: budget still binds on the expensive end.
MIXED_PRICES = {e: 40 + (e % 60) for e in range(1, sum(POSITION_COUNTS.values()) + 1)}


def flat_points(roster, value: float = 1.0) -> dict[int, float]:
    return dict.fromkeys(roster, value)


def ranked_points(roster) -> dict[int, float]:
    """Points that descend with element id, so the best pick is unambiguous."""
    return {e: 100.0 - e for e in roster}


class TestLegality:
    def test_the_chosen_squad_is_legal(self):
        roster = build_roster()
        selection = optimise_squad(roster, ranked_points(roster))
        assert validate_squad(selection.squad, roster).ok

    def test_the_chosen_lineup_is_legal(self):
        roster = build_roster()
        selection = optimise_squad(roster, ranked_points(roster))
        assert validate_lineup(selection.lineup, selection.squad, roster).ok

    def test_the_formation_follows_from_the_players_chosen(self):
        roster = build_roster()
        selection = optimise_squad(roster, ranked_points(roster))
        assert is_valid_formation(selection.lineup.starters, roster)
        assert format_formation(selection.lineup.starters, roster).count("-") == 2

    def test_the_substitute_keeper_is_first_on_the_bench(self):
        roster = build_roster()
        selection = optimise_squad(roster, ranked_points(roster))
        assert roster[selection.lineup.bench[0]].position is Position.GKP
        assert all(
            roster[e].position is not Position.GKP for e in selection.lineup.bench[1:]
        )


class TestBudget:
    def test_the_budget_is_never_exceeded(self):
        roster = build_roster(prices=MIXED_PRICES)
        selection = optimise_squad(roster, ranked_points(roster))
        assert selection.total_cost <= STARTING_BUDGET

    def test_leftover_money_lands_in_the_bank(self):
        roster = build_roster(price=40)
        selection = optimise_squad(roster, ranked_points(roster))
        assert selection.total_cost + selection.bank == STARTING_BUDGET

    def test_a_smaller_budget_forces_cheaper_players(self):
        roster = build_roster(prices=MIXED_PRICES)
        points = {e: float(roster[e].price) for e in roster}  # dearer scores more
        rich = optimise_squad(roster, points, budget=STARTING_BUDGET)
        poor = optimise_squad(roster, points, budget=850)
        assert poor.total_cost <= 850
        assert poor.expected_points < rich.expected_points

    def test_an_impossible_budget_is_reported_not_fudged(self):
        roster = build_roster(price=100)  # 15 x £10.0m = £150m
        with pytest.raises(InfeasibleSquad, match="no legal squad"):
            optimise_squad(roster, flat_points(roster), budget=STARTING_BUDGET)

    def test_the_optimiser_spends_money_when_it_buys_points(self):
        """A budget that is never binding means the constraint is untested."""
        roster = build_roster(prices=MIXED_PRICES)
        points = {e: (roster[e].price - 40) / 10 for e in roster}
        selection = optimise_squad(roster, points)
        assert selection.total_cost > STARTING_BUDGET * 0.9


class TestClubLimit:
    def test_no_more_than_three_come_from_one_club(self):
        roster = build_roster()
        selection = optimise_squad(roster, ranked_points(roster))
        counts: dict[int, int] = {}
        for element in selection.squad.elements:
            counts[roster[element].team] = counts.get(roster[element].team, 0) + 1
        assert max(counts.values()) <= MAX_PLAYERS_PER_CLUB

    def test_the_limit_binds_when_the_best_players_share_a_club(self):
        """Every top scorer on one club: the optimiser can only take three."""
        roster = build_roster()
        for element in list(roster)[:10]:
            player = roster[element]
            roster[element] = Player(
                element=element, position=player.position, team=1,
                price=player.price, web_name=player.web_name,
            )
        points = {e: (50.0 if roster[e].team == 1 else 1.0) for e in roster}
        selection = optimise_squad(roster, points)
        from_club_one = sum(
            1 for e in selection.squad.elements if roster[e].team == 1
        )
        assert from_club_one == MAX_PLAYERS_PER_CLUB

    def test_too_few_clubs_makes_a_squad_impossible(self):
        roster = build_roster(teams=4)  # 4 clubs x 3 players = 12 < 15
        with pytest.raises(InfeasibleSquad):
            optimise_squad(roster, flat_points(roster))


class TestOptimality:
    def test_the_best_available_players_are_chosen(self):
        """With points descending by id and every price equal, the optimum is
        simply the lowest ids that fit the composition."""
        roster = build_roster(teams=20)
        selection = optimise_squad(roster, ranked_points(roster))
        for position, required in (
            (Position.GKP, 2), (Position.DEF, 5), (Position.MID, 5), (Position.FWD, 3)
        ):
            chosen = sorted(
                e for e in selection.squad.elements if roster[e].position is position
            )
            best = sorted(e for e in roster if roster[e].position is position)[:required]
            assert chosen == best

    def test_no_bench_player_outranks_a_starter_in_their_own_position(self):
        """The XI is the best legal one, which is not the same as the eleven
        highest projections.

        A bench player may well outscore a starter in a different position:
        an XI needs at least one forward, so a weak forward starts ahead of a
        strong midfielder once five midfielders are already on. Within a
        position, though, there is never a reason to bench the better player.
        """
        roster = build_roster()
        points = ranked_points(roster)
        selection = optimise_squad(roster, points)

        for benched in selection.lineup.bench:
            position = roster[benched].position
            same_position_starters = [
                e for e in selection.lineup.starters if roster[e].position is position
            ]
            if not same_position_starters:
                continue
            assert points[benched] <= min(points[e] for e in same_position_starters)

    def test_the_formation_minimum_can_force_a_weaker_player_into_the_xi(self):
        """Documents the behaviour above from the other side: with points
        descending by id, the forwards are the worst players available, and one
        still has to start."""
        roster = build_roster()
        points = ranked_points(roster)
        selection = optimise_squad(roster, points)

        forwards = [
            e for e in selection.lineup.starters if roster[e].position is Position.FWD
        ]
        assert len(forwards) >= 1
        benched_midfielders = [
            e for e in selection.lineup.bench if roster[e].position is Position.MID
        ]
        assert benched_midfielders
        assert points[benched_midfielders[0]] > points[forwards[0]]

    def test_the_captain_is_the_highest_projected_starter(self):
        roster = build_roster()
        points = ranked_points(roster)
        selection = optimise_squad(roster, points)
        assert selection.lineup.captain == max(
            selection.lineup.starters, key=lambda e: points[e]
        )

    def test_the_vice_captain_is_the_second_highest(self):
        roster = build_roster()
        points = ranked_points(roster)
        selection = optimise_squad(roster, points)
        ranked = sorted(selection.lineup.starters, key=lambda e: -points[e])
        assert selection.lineup.vice_captain == ranked[1]

    def test_the_bench_is_ordered_by_projection(self):
        roster = build_roster()
        points = ranked_points(roster)
        selection = optimise_squad(roster, points)
        outfield = selection.lineup.bench[1:]
        assert list(outfield) == sorted(outfield, key=lambda e: -points[e])

    def test_the_captains_points_are_counted_twice(self):
        roster = build_roster()
        points = ranked_points(roster)
        selection = optimise_squad(roster, points)
        raw = sum(points[e] for e in selection.lineup.starters)
        assert selection.expected_points == pytest.approx(
            raw + points[selection.lineup.captain]
        )

    def test_a_cheap_bench_is_preferred_to_an_expensive_one(self):
        """Bench players rarely score, so money spent there is mostly wasted.

        Two equally-projected players, one cheap: the optimiser should bench
        the cheap one and spend the difference on the XI.
        """
        roster = build_roster(price=40)
        for element in roster:
            if roster[element].position is Position.FWD:
                player = roster[element]
                roster[element] = Player(
                    element=element, position=player.position, team=player.team,
                    price=120 if element % 2 else 40, web_name=player.web_name,
                )
        points = {e: 5.0 if roster[e].price > 100 else 1.0 for e in roster}
        selection = optimise_squad(roster, points)
        bench_spend = sum(roster[e].price for e in selection.lineup.bench)
        starter_spend = sum(roster[e].price for e in selection.lineup.starters)
        assert bench_spend < starter_spend


class TestConstraints:
    def test_an_excluded_player_is_never_picked(self):
        roster = build_roster()
        points = ranked_points(roster)
        best = max(roster, key=lambda e: points[e])
        selection = optimise_squad(roster, points, excluded={best})
        assert best not in selection.squad.elements

    def test_a_required_player_is_always_picked(self):
        roster = build_roster()
        points = ranked_points(roster)
        worst = min(roster, key=lambda e: points[e])
        selection = optimise_squad(roster, points, required={worst})
        assert worst in selection.squad.elements

    def test_requiring_an_excluded_player_is_reported(self):
        roster = build_roster()
        with pytest.raises(InfeasibleSquad, match="required player"):
            optimise_squad(roster, flat_points(roster), excluded={1}, required={1})

    def test_selling_prices_can_override_market_prices(self):
        """The Manager values a player it already owns at their sale price."""
        roster = build_roster(price=70)
        prices = {e: 70 for e in roster}
        prices[1] = 10
        selection = optimise_squad(roster, ranked_points(roster), prices=prices)
        assert selection.total_cost == sum(prices[e] for e in selection.squad.elements)

    def test_a_triple_captain_weighting_changes_the_armband_value(self):
        roster = build_roster()
        points = ranked_points(roster)
        doubled = optimise_squad(roster, points, captain_multiplier=2)
        tripled = optimise_squad(roster, points, captain_multiplier=3)
        assert tripled.expected_points > doubled.expected_points


class TestLineupOnly:
    def test_the_best_xi_is_chosen_from_a_fixed_squad(self):
        roster = build_roster()
        points = ranked_points(roster)
        squad = optimise_squad(roster, points).squad

        # Flip the projections so a different XI becomes optimal.
        flipped = {e: -v for e, v in points.items()}
        lineup = optimise_lineup(squad, roster, flipped)
        assert set(lineup.starters) <= set(squad.elements)
        assert is_valid_formation(lineup.starters, roster)
        assert validate_lineup(lineup, squad, roster).ok

    def test_bench_boost_makes_the_xi_choice_stop_mattering(self):
        """Under Bench Boost every player scores, so the objective is flat."""
        roster = build_roster()
        points = ranked_points(roster)
        squad = optimise_squad(roster, points).squad
        lineup = optimise_lineup(squad, roster, points, bench_counts=True)
        assert is_valid_formation(lineup.starters, roster)
        assert set(lineup.starters) | set(lineup.bench) == set(squad.elements)
