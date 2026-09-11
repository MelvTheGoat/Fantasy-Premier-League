"""Rules working together over several gameweeks.

The unit tests pin each rule down on its own. These exercise the interactions
that only show up in sequence: purchase prices surviving transfers so the
selling-price rule stays correct weeks later, Free Hit reverting a squad, and
free transfers banking and being spent across a run of gameweeks.
"""

from __future__ import annotations

from fplai.rules.chips import available_chips, validate_chip_usage
from fplai.rules.constants import Chip, STARTING_BUDGET
from fplai.rules.pricing import selling_price, squad_value
from fplai.rules.scoring import score_gameweek, summarise_season
from fplai.rules.squad import validate_lineup, validate_squad
from fplai.rules.transfers import (
    apply_transfers,
    build_transfer,
    count_transfers,
    resolve_transfers,
)
from fplai.rules.types import ChipUsage, Squad, SquadPick

from .conftest import DEFAULT_SQUAD_ELEMENTS, build_lineup, build_roster, results_for

STARTERS = (101, 201, 202, 203, 301, 302, 303, 304, 401, 402, 403)
BENCH = (102, 204, 205, 305)


def starting_squad(roster, bank: int = 0) -> Squad:
    picks = tuple(SquadPick(e, roster[e].price) for e in DEFAULT_SQUAD_ELEMENTS)
    return Squad(picks=picks, bank=bank)


class TestSellingPriceAcrossTransfers:
    def test_a_player_bought_after_a_rise_is_priced_from_what_was_paid(self):
        """The classic trap: sell price must follow the purchase price, not the
        price the previous owner paid or the original listing."""
        roster = build_roster()
        squad = starting_squad(roster, bank=200)

        # GW2: buy element 306 at its list price of £6.5m.
        prices = {e: roster[e].price for e in roster}
        prices[306] = 65
        transfer = build_transfer(squad, out_element=305, in_element=306, prices=prices)
        squad = apply_transfers(squad, [transfer])
        assert squad.purchase_price(306) == 65

        # By GW10 it has risen to £7.1m, so it sells for £6.8m, not £7.1m.
        assert selling_price(squad.purchase_price(306), 71) == 68

    def test_profit_does_not_compound_by_rebuying_the_same_player(self):
        """Selling and rebuying resets the purchase price upward, which costs
        money rather than banking the rise twice."""
        roster = build_roster()
        squad = starting_squad(roster, bank=100)
        prices = {e: roster[e].price for e in roster}
        original_purchase = squad.purchase_price(301)

        prices[301] = original_purchase + 4  # a £0.4m rise
        out = build_transfer(squad, out_element=301, in_element=306, prices=prices)
        squad = apply_transfers(squad, [out])

        back = build_transfer(squad, out_element=306, in_element=301, prices=prices)
        squad = apply_transfers(squad, [back])

        # Sold at 40 + (4 // 2) = 42, bought back at the full 44, so the two
        # tenths that were never released are simply gone.
        assert squad.purchase_price(301) == prices[301] == original_purchase + 4
        assert squad.bank == 100 - 2, "the round trip costs the unreleased half of the rise"

    def test_squad_value_tracks_sale_prices_not_market_prices(self):
        roster = build_roster()
        squad = starting_squad(roster, bank=50)
        risen = {e: p.purchase_price + 4 for e, p in zip(squad.elements, squad.picks)}
        # Every player keeps half of a £0.4m rise, so £0.2m each across fifteen.
        assert squad_value(squad, risen) == sum(
            p.purchase_price + 2 for p in squad.picks
        ) + 50


class TestFreeTransferRun:
    def test_banking_then_spending_across_five_gameweeks(self):
        """Roll for three weeks, then make three transfers for free."""
        free = 1
        for _ in range(3):
            free = resolve_transfers(0, free).free_transfers_after
        assert free == 4

        outcome = resolve_transfers(3, free)
        assert outcome.points_cost == 0
        assert outcome.free_transfers_after == 2

    def test_a_long_roll_stops_at_five(self):
        free = 1
        for _ in range(10):
            free = resolve_transfers(0, free).free_transfers_after
        assert free == 5

    def test_a_wildcard_week_does_not_spend_the_bank(self):
        free = 1
        for _ in range(2):
            free = resolve_transfers(0, free).free_transfers_after
        assert free == 3

        after_wildcard = resolve_transfers(9, free, Chip.WILDCARD)
        assert after_wildcard.points_cost == 0
        assert after_wildcard.free_transfers_after >= 3


class TestFreeHitReversion:
    def test_the_squad_reverts_after_a_free_hit(self):
        """A Free Hit squad exists for one gameweek only; the next deadline
        starts from the squad that was in place before it."""
        roster = build_roster()
        before = starting_squad(roster, bank=100)
        prices = {e: roster[e].price for e in roster}

        free_hit_squad = apply_transfers(
            before,
            [
                build_transfer(before, out_element=301, in_element=306, prices=prices),
                build_transfer(before, out_element=302, in_element=307, prices=prices),
                build_transfer(before, out_element=401, in_element=404, prices=prices),
            ],
        )
        assert count_transfers(before, free_hit_squad) == 3

        after = before  # reversion is simply keeping the pre-chip squad
        assert count_transfers(before, after) == 0
        assert after.elements == before.elements
        assert after.bank == before.bank

    def test_a_free_hit_squad_still_obeys_every_squad_rule(self):
        roster = build_roster()
        before = starting_squad(roster, bank=STARTING_BUDGET - sum(
            roster[e].price for e in DEFAULT_SQUAD_ELEMENTS
        ))
        prices = {e: roster[e].price for e in roster}
        free_hit_squad = apply_transfers(
            before,
            [build_transfer(before, out_element=301, in_element=306, prices=prices)],
        )
        assert validate_squad(free_hit_squad, roster).ok


class TestChipsAcrossTheSeason:
    def test_a_full_two_set_season_validates(self):
        used = [
            ChipUsage(Chip.WILDCARD, 7),
            ChipUsage(Chip.FREE_HIT, 13),
            ChipUsage(Chip.BENCH_BOOST, 16),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 19),
            ChipUsage(Chip.WILDCARD, 23),
            ChipUsage(Chip.FREE_HIT, 28),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 34),
            ChipUsage(Chip.BENCH_BOOST, 37),
        ]
        assert validate_chip_usage(used).ok
        assert available_chips(38, used) == set()

    def test_the_first_set_cannot_be_stretched_past_gameweek_nineteen(self):
        used = [ChipUsage(Chip.WILDCARD, 7)]
        # In GW20 the second set is live, so the Wildcard is available again --
        # but the GW7 one is spent and the unplayed first-set chips are gone.
        assert Chip.WILDCARD in available_chips(20, used)
        assert validate_chip_usage(used).ok


class TestFullGameweek:
    def test_a_gameweek_with_a_hit_a_substitution_and_a_vice_captain(self):
        """One gameweek touching most of the rules at once."""
        roster = build_roster()
        squad = starting_squad(roster)
        lineup = build_lineup(STARTERS, BENCH, captain=401, vice_captain=402)
        assert validate_lineup(lineup, squad, roster).ok

        results = results_for(
            STARTERS + BENCH,
            points=2,
            overrides={
                401: {"minutes": 0, "total_points": 0},  # captain blanks entirely
                402: {"total_points": 8},                 # vice takes the armband
                301: {"minutes": 0, "total_points": 0},   # replaced from the bench
                204: {"total_points": 6},                 # the substitute who comes on
            },
        )

        gw = score_gameweek(9, lineup, roster, results, transfer_cost=4)

        assert gw.captaincy.passed_to_vice and gw.captaincy.element == 402

        # Vacancies are covered in XI order, so the midfielder is replaced
        # before the forward, and each takes the next bench slot that keeps the
        # formation legal: 3-4-3 becomes 4-3-3 and then 5-3-2.
        assert [(s.out_element, s.in_element) for s in gw.substitutions] == [
            (301, 204),
            (401, 205),
        ]

        # Nine untouched starters at 2, the substitute on 6, the vice doubled
        # to 16, and the bench pair now scoring nothing.
        assert gw.points_before_hits == 9 * 2 + 6 + 8 * 2
        assert gw.points == gw.points_before_hits - 4
        assert gw.score_for(301).multiplier == 0
        assert gw.score_for(401).multiplier == 0

    def test_a_short_season_summarises_against_the_official_averages(self):
        roster = build_roster()
        lineup = build_lineup(STARTERS, BENCH, captain=401, vice_captain=402)
        scores = []
        for gameweek, points, cost in ((1, 4, 0), (2, 8, 0), (3, 2, 4)):
            results = results_for(
                STARTERS + BENCH, points=0, overrides={301: {"total_points": points * 10}}
            )
            scores.append(
                score_gameweek(gameweek, lineup, roster, results, transfer_cost=cost)
            )

        summary = summarise_season(scores, averages={1: 45.0, 2: 60.0, 3: 50.0})
        assert summary.total_points == 40 + 80 + (20 - 4)
        assert summary.total_transfer_cost == 4
        assert summary.gameweeks_beating_average == 1
        assert summary.cumulative_points == [40, 120, 136]
