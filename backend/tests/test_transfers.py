"""Free-transfer accounting, hits, and applying transfers to a squad."""

from __future__ import annotations

import pytest

from fplai.rules.constants import Chip, MAX_BANKED_FREE_TRANSFERS
from fplai.rules.transfers import (
    apply_transfers,
    build_transfer,
    count_transfers,
    resolve_transfers,
    roll_free_transfers,
    transfer_cost,
)
from fplai.rules.types import RuleViolation, Squad, SquadPick, Transfer


class TestTransferCost:
    @pytest.mark.parametrize(
        ("made", "free", "cost"),
        [
            (0, 1, 0),
            (1, 1, 0),
            (2, 1, 4),
            (3, 1, 8),
            (2, 2, 0),
            (3, 2, 4),
            (5, 5, 0),
            (6, 5, 4),
            (0, 5, 0),
        ],
    )
    def test_each_transfer_beyond_the_free_ones_costs_four(self, made, free, cost):
        assert transfer_cost(made, free) == cost

    def test_making_fewer_transfers_than_free_ones_is_never_a_credit(self):
        assert transfer_cost(0, 5) == 0

    @pytest.mark.parametrize("chip", [Chip.WILDCARD, Chip.FREE_HIT])
    def test_unlimited_chips_remove_the_cost_entirely(self, chip):
        assert transfer_cost(11, 1, chip) == 0

    @pytest.mark.parametrize("chip", [Chip.BENCH_BOOST, Chip.TRIPLE_CAPTAIN])
    def test_other_chips_do_not_remove_the_cost(self, chip):
        assert transfer_cost(3, 1, chip) == 8


class TestRollover:
    def test_an_unused_transfer_rolls_over(self):
        assert resolve_transfers(0, 1).free_transfers_after == 2

    def test_using_the_free_transfer_leaves_one_for_next_week(self):
        assert resolve_transfers(1, 1).free_transfers_after == 1

    def test_banked_transfers_accumulate_week_by_week(self):
        free = 1
        for expected in (2, 3, 4, 5):
            free = roll_free_transfers(free, transfers_made=0)
            assert free == expected

    def test_banking_is_capped_at_five(self):
        assert roll_free_transfers(MAX_BANKED_FREE_TRANSFERS, 0) == MAX_BANKED_FREE_TRANSFERS
        assert roll_free_transfers(5, 0) == 5

    def test_taking_a_hit_leaves_one_free_transfer_next_week(self):
        outcome = resolve_transfers(3, 1)
        assert outcome.hits == 2
        assert outcome.points_cost == 8
        assert outcome.free_transfers_after == 1

    def test_spending_two_of_three_banked_transfers(self):
        outcome = resolve_transfers(2, 3)
        assert outcome.free_transfers_used == 2
        assert outcome.hits == 0
        assert outcome.free_transfers_after == 2

    def test_rolling_is_reported_so_it_can_be_explained(self):
        assert resolve_transfers(0, 2).rolled
        assert not resolve_transfers(1, 2).rolled

    @pytest.mark.parametrize(("made", "free"), [(-1, 1), (1, -1)])
    def test_negative_counts_are_rejected(self, made, free):
        with pytest.raises(ValueError):
            resolve_transfers(made, free)


class TestChipTransfers:
    @pytest.mark.parametrize("chip", [Chip.WILDCARD, Chip.FREE_HIT])
    def test_a_chip_gameweek_costs_nothing_however_many_moves(self, chip):
        outcome = resolve_transfers(15, 1, chip)
        assert outcome.points_cost == 0
        assert outcome.hits == 0
        assert outcome.unlimited

    @pytest.mark.parametrize("chip", [Chip.WILDCARD, Chip.FREE_HIT])
    def test_banked_transfers_survive_a_chip(self, chip):
        """2026/27: saved free transfers are maintained across Wildcard and
        Free Hit rather than being reset to one."""
        outcome = resolve_transfers(15, 3, chip)
        assert outcome.free_transfers_used == 0
        assert outcome.free_transfers_after >= 3

    def test_a_chip_gameweek_still_respects_the_bank_cap(self):
        assert resolve_transfers(15, 5, Chip.WILDCARD).free_transfers_after == 5

    def test_bench_boost_leaves_transfer_accounting_alone(self):
        normal = resolve_transfers(2, 1)
        boosted = resolve_transfers(2, 1, Chip.BENCH_BOOST)
        assert boosted.points_cost == normal.points_cost
        assert boosted.free_transfers_after == normal.free_transfers_after


def squad_of(*pairs: tuple[int, int], bank: int = 0) -> Squad:
    return Squad(picks=tuple(SquadPick(e, p) for e, p in pairs), bank=bank)


class TestApplyingTransfers:
    def test_a_transfer_swaps_the_player_and_updates_the_bank(self):
        squad = squad_of((1, 70), (2, 50), bank=5)
        transfer = Transfer(out_element=1, in_element=3, selling_price=72, purchase_price=75)
        after = apply_transfers(squad, [transfer])
        assert after.elements == (3, 2)
        assert after.bank == 5 + 72 - 75

    def test_the_incoming_player_is_recorded_at_the_price_paid(self):
        """The price paid is what the selling-price rule needs next time."""
        squad = squad_of((1, 70), bank=10)
        transfer = Transfer(out_element=1, in_element=3, selling_price=72, purchase_price=75)
        after = apply_transfers(squad, [transfer])
        assert after.purchase_price(3) == 75

    def test_selling_a_risen_player_frees_only_half_the_rise(self):
        squad = squad_of((1, 70), bank=0)
        prices = {1: 74, 3: 72}
        transfer = build_transfer(squad, out_element=1, in_element=3, prices=prices)
        assert transfer.selling_price == 72, "70 + (4 // 2)"
        assert apply_transfers(squad, [transfer]).bank == 0

    def test_a_transfer_that_overspends_is_rejected(self):
        squad = squad_of((1, 70), bank=0)
        transfer = Transfer(out_element=1, in_element=3, selling_price=70, purchase_price=75)
        with pytest.raises(RuleViolation, match="overspend"):
            apply_transfers(squad, [transfer])

    def test_several_transfers_apply_in_order(self):
        squad = squad_of((1, 70), (2, 50), (3, 60), bank=20)
        transfers = [
            Transfer(out_element=1, in_element=4, selling_price=70, purchase_price=80),
            Transfer(out_element=2, in_element=5, selling_price=50, purchase_price=45),
        ]
        after = apply_transfers(squad, transfers)
        assert set(after.elements) == {4, 5, 3}
        assert after.bank == 20 - 10 + 5

    def test_buying_a_player_already_owned_is_rejected(self):
        squad = squad_of((1, 70), (2, 50))
        transfer = Transfer(out_element=1, in_element=2, selling_price=70, purchase_price=50)
        with pytest.raises(RuleViolation, match="already in the squad"):
            apply_transfers(squad, [transfer])

    def test_selling_a_player_not_owned_is_rejected(self):
        squad = squad_of((1, 70))
        transfer = Transfer(out_element=9, in_element=3, selling_price=70, purchase_price=50)
        with pytest.raises(RuleViolation, match="not in the squad"):
            apply_transfers(squad, [transfer])

    def test_the_original_squad_is_left_untouched(self):
        squad = squad_of((1, 70), (2, 50), bank=5)
        apply_transfers(
            squad, [Transfer(out_element=1, in_element=3, selling_price=70, purchase_price=70)]
        )
        assert squad.elements == (1, 2)
        assert squad.bank == 5


class TestCountingTransfers:
    def test_counting_ignores_players_who_stayed(self):
        before = squad_of((1, 70), (2, 50), (3, 60))
        after = squad_of((1, 70), (4, 50), (5, 60))
        assert count_transfers(before, after) == 2

    def test_an_unchanged_squad_counts_as_none(self):
        squad = squad_of((1, 70), (2, 50))
        assert count_transfers(squad, squad) == 0
