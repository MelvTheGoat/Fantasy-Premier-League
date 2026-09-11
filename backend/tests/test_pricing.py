"""Selling prices.

FPL returns half of any rise, rounded down to the nearest £0.1m, and the whole
of any fall. Prices are integer tenths, so the halving is exact floor division
and every boundary case can be asserted outright.
"""

from __future__ import annotations

import pytest

from fplai.rules.pricing import profit, selling_price, squad_selling_value, squad_value
from fplai.rules.types import Squad, SquadPick


class TestSellingPrice:
    def test_an_unchanged_price_sells_for_what_it_cost(self):
        assert selling_price(70, 70) == 70

    @pytest.mark.parametrize(
        ("bought", "now", "sells_for"),
        [
            (70, 71, 70),   # £0.1m rise: half rounds down to nothing
            (70, 72, 71),   # £0.2m rise: half is exactly £0.1m
            (70, 73, 71),   # £0.3m rise: £0.15m rounds down to £0.1m
            (70, 74, 72),   # £0.4m rise
            (70, 75, 72),   # £0.5m rise rounds down
            (45, 50, 47),   # £0.5m rise from a cheap price
            (130, 141, 135),  # £1.1m rise on a premium
        ],
    )
    def test_half_a_rise_is_kept_rounded_down(self, bought, now, sells_for):
        assert selling_price(bought, now) == sells_for

    @pytest.mark.parametrize(
        ("bought", "now"),
        [(70, 69), (70, 65), (130, 120), (45, 40)],
    )
    def test_a_fall_is_taken_in_full(self, bought, now):
        assert selling_price(bought, now) == now

    def test_rounding_never_favours_the_manager(self):
        """Every odd rise loses the odd tenth rather than gaining it."""
        for rise in range(1, 30, 2):
            assert selling_price(70, 70 + rise) == 70 + (rise - 1) // 2

    def test_profit_is_what_is_released_above_the_purchase_price(self):
        assert profit(70, 74) == 2
        assert profit(70, 71) == 0
        assert profit(70, 65) == 0, "a fall is a loss, not a negative profit"

    def test_a_negative_price_is_rejected(self):
        with pytest.raises(ValueError):
            selling_price(-1, 50)
        with pytest.raises(ValueError):
            selling_price(50, -1)


class TestSquadValue:
    def test_squad_value_uses_selling_prices_not_market_prices(self):
        squad = Squad(
            picks=(SquadPick(1, 70), SquadPick(2, 50), SquadPick(3, 100)),
            bank=25,
        )
        prices = {1: 74, 2: 45, 3: 103}
        # 70 + (4 // 2) = 72; a fall is taken in full at 45; 100 + (3 // 2) = 101
        assert squad_selling_value(squad, prices) == 72 + 45 + 101
        assert squad_value(squad, prices) == 72 + 45 + 101 + 25

    def test_a_squad_that_never_moved_is_worth_what_it_cost(self):
        squad = Squad(picks=(SquadPick(1, 70), SquadPick(2, 50)), bank=0)
        prices = {1: 70, 2: 50}
        assert squad_selling_value(squad, prices) == 120
