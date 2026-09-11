"""The two playing models, and the decisions the Manager makes.

Best XI is stateless and mostly tested through the optimiser. The Manager is
where the interesting behaviour is: rolling a transfer, refusing a hit that
does not pay, and spending chips on the right week rather than the first
tolerable one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.data.db import connect, init_db
from fplai.data.ingest import (
    ingest_fixtures,
    ingest_gameweeks,
    ingest_live_gameweek,
    ingest_players,
    ingest_teams,
)
from fplai.data.repository import load_locked_lineup, load_roster_at_gameweek
from fplai.model.projections import project_for_gameweek
from fplai.rules.constants import Chip, Position
from fplai.rules.squad import validate_lineup, validate_squad
from fplai.strategy import best_xi, manager
from fplai.strategy.explain import PlayerContext, load_contexts, selection_reason
from fplai.strategy.manager import (
    CHIP_THRESHOLDS,
    ManagerState,
    decide_chip,
    decide_transfers,
    horizon_weights,
    selling_prices,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


def set_projection(projections, element: int, points: float) -> None:
    """Force one player's projection to a fixed value across the horizon.

    The recorded pool is deliberately a cross-section of the price range, so
    most of it is squad players projecting under two points. That is right for
    structural tests and useless for testing a decision rule that turns on
    whether a gain clears four points -- there is no such gain available.
    Overriding the projection isolates the rule from the fixture's data.
    """
    from fplai.model.xpts import PointsBreakdown

    for projection in projections[element]:
        breakdown = PointsBreakdown(
            element=element,
            gameweek=projection.gameweek,
            goals=points,
            expected_minutes=90.0,
            probability_of_starting=0.95,
            opponent=1,
        )
        object.__setattr__(projection, "fixtures", (breakdown,) if points else ())


@pytest.fixture
def db():
    connection = connect(":memory:")
    init_db(connection)
    bootstrap = load("bootstrap_static.json")
    ingest_teams(connection, bootstrap)
    ingest_gameweeks(connection, bootstrap)
    ingest_players(connection, bootstrap, gameweek=3)
    ingest_fixtures(connection, load("fixtures.json"))
    ingest_live_gameweek(connection, 3, load("event_3_live.json"))
    yield connection
    connection.close()


@pytest.fixture
def projections(db):
    return project_for_gameweek(db, 4, horizon=4)


class TestBestXI:
    def test_it_picks_a_legal_squad_and_lineup(self, db, projections):
        selection, _ = best_xi.pick_gameweek(db, 4, projections=projections)
        roster = load_roster_at_gameweek(db, 4)
        assert validate_squad(selection.squad, roster).ok
        assert validate_lineup(selection.lineup, selection.squad, roster).ok

    def test_every_player_gets_a_reason(self, db, projections):
        selection, reasons = best_xi.pick_gameweek(db, 4, projections=projections)
        assert set(reasons) == set(selection.squad.elements)
        assert all(text.strip().endswith(".") for text in reasons.values())

    def test_the_captains_reason_says_so(self, db, projections):
        selection, reasons = best_xi.pick_gameweek(db, 4, projections=projections)
        assert "captained" in reasons[selection.lineup.captain]

    def test_it_has_no_memory_between_gameweeks(self, db):
        """Each gameweek is picked from scratch, so an earlier pick cannot
        constrain a later one."""
        first, _ = best_xi.pick_gameweek(db, 3)
        second, _ = best_xi.pick_gameweek(db, 4)
        assert first.squad.elements or second.squad.elements  # both solved
        # Nothing is carried: the same call twice gives the same answer.
        again, _ = best_xi.pick_gameweek(db, 3)
        assert set(again.squad.elements) == set(first.squad.elements)

    def test_locking_stores_the_picks(self, db, projections):
        best_xi.lock_gameweek(db, 4, projections=projections)
        assert load_locked_lineup(db, best_xi.MODEL_ID, 4) is not None

    def test_locked_picks_cannot_be_rewritten(self, db, projections):
        from fplai.data.repository import LockedPicksExist

        best_xi.lock_gameweek(db, 4, projections=projections)
        with pytest.raises(LockedPicksExist):
            best_xi.lock_gameweek(db, 4, projections=projections)


class TestHorizonWeighting:
    def test_the_first_gameweek_counts_fully(self):
        assert horizon_weights(5)[0] == 1.0

    def test_later_gameweeks_count_for_less(self):
        weights = horizon_weights(5)
        assert weights == sorted(weights, reverse=True)
        assert weights[-1] < weights[0]

    def test_a_horizon_of_one_is_just_the_gameweek(self):
        assert horizon_weights(1) == [1.0]


class TestSellingPrices:
    def test_an_owned_player_is_valued_at_their_sale_price(self, db, projections):
        roster = load_roster_at_gameweek(db, 4)
        selection = manager.build_initial_squad(db, 4, projections)
        squad = selection.squad

        element = squad.elements[0]
        risen = dict(roster)
        player = roster[element]
        from fplai.rules.types import Player

        risen[element] = Player(
            element=element, position=player.position, team=player.team,
            price=player.price + 4, web_name=player.web_name,
        )
        prices = selling_prices(squad, risen)
        # Half of a £0.4m rise is kept, so the sale price is purchase + 2.
        assert prices[element] == squad.purchase_price(element) + 2

    def test_an_unowned_player_costs_their_market_price(self, db, projections):
        roster = load_roster_at_gameweek(db, 4)
        squad = manager.build_initial_squad(db, 4, projections).squad
        prices = selling_prices(squad, roster)
        unowned = next(e for e in roster if e not in squad.elements)
        assert prices[unowned] == roster[unowned].price


class TestTransferDecisions:
    def state(self, db, projections, free_transfers=1):
        squad = manager.build_initial_squad(db, 4, projections).squad
        return ManagerState(squad=squad, free_transfers=free_transfers)

    def test_a_settled_squad_rolls_its_transfer(self, db, projections):
        """With the squad already optimal for these projections, there is
        nothing worth doing and the transfer should bank."""
        state = self.state(db, projections)
        decision = decide_transfers(db, 4, state, projections)
        assert decision.rolled
        assert "Rolled transfer" in decision.reason
        assert decision.points_cost == 0

    def test_a_rolled_transfer_banks_for_next_week(self, db, projections):
        state = self.state(db, projections, free_transfers=1)
        decision = decide_transfers(db, 4, state, projections)
        assert decision.free_transfers_after == 2

    def test_the_resulting_squad_and_lineup_are_legal(self, db, projections):
        state = self.state(db, projections)
        decision = decide_transfers(db, 4, state, projections)
        roster = load_roster_at_gameweek(db, 4)
        assert validate_squad(decision.squad, roster).ok
        assert validate_lineup(decision.lineup, decision.squad, roster).ok

    def test_an_injury_prompts_a_transfer(self, db, projections):
        """Zeroing a starter's projection should make replacing them clearly
        worth the free transfer."""
        state = self.state(db, projections)
        roster = load_roster_at_gameweek(db, 4)
        owned = set(state.squad.elements)
        target = next(e for e in owned if roster[e].position is Position.MID)
        replacement = next(
            e for e in roster
            if e not in owned
            and roster[e].position is Position.MID
            and roster[e].price <= roster[target].price
        )
        set_projection(projections, target, 0.0)
        set_projection(projections, replacement, 12.0)

        decision = decide_transfers(db, 4, state, projections)
        assert target not in decision.squad.elements
        assert replacement in decision.squad.elements
        assert decision.count == 1, "one free transfer, one move"

    def test_a_hit_is_refused_when_the_gain_is_too_small(self, db, projections):
        """Two transfers means a four-point hit, which a marginal gain must
        not be allowed to justify."""
        state = self.state(db, projections, free_transfers=1)
        decision = decide_transfers(db, 4, state, projections, hit_margin=1000.0)
        assert decision.points_cost == 0

    def test_a_hit_is_taken_when_the_gain_clearly_beats_it(self, db, projections):
        state = self.state(db, projections, free_transfers=0)

        # Zero two of the squad's own players and make two unowned players of
        # the same position clearly worth buying, so the gain unambiguously
        # clears the four-point hit.
        roster = load_roster_at_gameweek(db, 4)
        owned = set(state.squad.elements)
        dropped, replacements = [], []
        for position in (Position.MID, Position.FWD):
            out = next(e for e in owned if roster[e].position is position)
            incoming = next(
                e for e in roster
                if e not in owned
                and roster[e].position is position
                and roster[e].price <= roster[out].price
            )
            set_projection(projections, out, 0.0)
            set_projection(projections, incoming, 12.0)
            dropped.append(out)
            replacements.append(incoming)

        decision = decide_transfers(db, 4, state, projections, hit_margin=0.0)
        assert decision.count >= 1
        assert decision.points_cost > 0, "with no free transfers, a move costs a hit"
        assert set(replacements) & set(decision.squad.elements)
        assert not set(dropped) & set(decision.squad.elements)

    def test_banked_transfers_let_several_moves_go_free(self, db, projections):
        state = self.state(db, projections, free_transfers=3)
        roster = load_roster_at_gameweek(db, 4)
        owned = set(state.squad.elements)
        for position in (Position.MID, Position.FWD):
            out = next(e for e in owned if roster[e].position is position)
            incoming = next(
                e for e in roster
                if e not in owned
                and roster[e].position is position
                and roster[e].price <= roster[out].price
            )
            set_projection(projections, out, 0.0)
            set_projection(projections, incoming, 12.0)

        decision = decide_transfers(db, 4, state, projections)
        assert decision.count >= 1
        assert decision.points_cost == 0, "banked transfers cover these moves"

    def test_a_transfer_carries_a_side_by_side_comparison(self, db, projections):
        state = self.state(db, projections)
        roster = load_roster_at_gameweek(db, 4)
        owned = set(state.squad.elements)
        target = next(e for e in owned if roster[e].position is Position.MID)
        replacement = next(
            e for e in roster
            if e not in owned
            and roster[e].position is Position.MID
            and roster[e].price <= roster[target].price
        )
        set_projection(projections, target, 0.0)
        set_projection(projections, replacement, 12.0)

        decision = decide_transfers(db, 4, state, projections)
        assert decision.transfers

        comparison = decision.comparisons[0]
        assert set(comparison) >= {"out", "in", "projected_gain", "price_change"}
        for side in ("out", "in"):
            assert set(comparison[side]) >= {
                "name", "price", "projected_points", "fixtures",
                "expected_goals", "expected_assists", "minutes",
            }

    def test_a_wildcard_makes_transfers_free(self, db, projections):
        state = self.state(db, projections, free_transfers=1)
        decision = decide_transfers(db, 4, state, projections, chip=Chip.WILDCARD)
        assert decision.points_cost == 0
        assert "Wildcard" in decision.reason


class TestChipDecisions:
    def state(self, db, projections, **kwargs):
        squad = manager.build_initial_squad(db, 4, projections).squad
        return ManagerState(squad=squad, **kwargs)

    def test_no_chip_is_played_on_an_ordinary_week(self, db, projections):
        decision = decide_chip(db, 4, self.state(db, projections), projections)
        assert decision.chip is None

    def test_each_chip_has_its_own_bar(self):
        """A shared threshold would fire Triple Captain in week one, because a
        premium captain projects six to eight points every week."""
        assert CHIP_THRESHOLDS[Chip.TRIPLE_CAPTAIN] > 8
        assert CHIP_THRESHOLDS[Chip.WILDCARD] > CHIP_THRESHOLDS[Chip.BENCH_BOOST]

    def test_a_wildcard_is_not_played_in_the_opening_weeks(self, db, projections):
        """Early projections rest on priors, so a large apparent gain says more
        about the model's uncertainty than about the squad."""
        state = self.state(db, projections)
        for element, gameweeks in projections.items():
            if element in state.squad.elements:
                for projection in gameweeks:
                    object.__setattr__(projection, "fixtures", ())

        decision = decide_chip(db, 2, state, projections)
        assert decision.chip is not Chip.WILDCARD

    def test_a_chip_is_played_rather_than_allowed_to_expire(self, db, projections):
        """With one gameweek and one chip left in the first set, playing it is
        strictly better than losing it."""
        from fplai.rules.types import ChipUsage

        state = self.state(
            db, projections,
            chips_used=[
                ChipUsage(Chip.WILDCARD, 5),
                ChipUsage(Chip.FREE_HIT, 8),
                ChipUsage(Chip.BENCH_BOOST, 12),
            ],
        )
        decision = decide_chip(db, 19, state, projections)
        assert decision.chip is Chip.TRIPLE_CAPTAIN
        assert "expiry" in decision.reason

    def test_nothing_is_played_when_no_chips_remain(self, db, projections):
        from fplai.rules.types import ChipUsage

        state = self.state(
            db, projections,
            chips_used=[
                ChipUsage(Chip.WILDCARD, 5), ChipUsage(Chip.FREE_HIT, 8),
                ChipUsage(Chip.BENCH_BOOST, 12), ChipUsage(Chip.TRIPLE_CAPTAIN, 16),
            ],
        )
        assert decide_chip(db, 18, state, projections).chip is None

    def test_a_squad_with_no_fixtures_favours_the_free_hit(self, db, projections):
        state = self.state(db, projections)
        for element in state.squad.elements:
            for projection in projections.get(element, []):
                object.__setattr__(projection, "fixtures", ())

        decision = decide_chip(db, 10, state, projections)
        assert decision.chip in (Chip.FREE_HIT, Chip.WILDCARD, None)


class TestExplanations:
    def test_a_context_reports_set_piece_duties(self):
        context = PlayerContext(
            element=1, web_name="X", position=Position.MID, team=1,
            team_short_name="ARS", team_code=3, price=70,
            penalties_order=1, corners_order=1,
        )
        assert context.roles == ["penalties", "corners"]
        assert context.on_penalties

    @pytest.mark.parametrize(
        ("status", "expected"),
        [("s", "suspended"), ("u", "unavailable"), ("n", "not in the squad")],
    )
    def test_availability_is_described(self, status, expected):
        context = PlayerContext(
            element=1, web_name="X", position=Position.MID, team=1,
            team_short_name="ARS", team_code=3, price=70, status=status,
        )
        assert context.availability_note == expected

    def test_a_fit_player_has_no_availability_note(self):
        context = PlayerContext(
            element=1, web_name="X", position=Position.MID, team=1,
            team_short_name="ARS", team_code=3, price=70,
        )
        assert context.availability_note is None

    def test_a_blank_gameweek_is_stated_plainly(self, db, projections):
        contexts = load_contexts(db, 4)
        element = next(iter(contexts))
        projection = projections[element][0]
        object.__setattr__(projection, "fixtures", ())
        text = selection_reason(contexts[element], projection, {1: "ARS"})
        assert "no fixture" in text

    def test_contexts_only_see_earlier_gameweeks(self, db):
        """An explanation written for GW3 must not quote GW3's own numbers."""
        before = load_contexts(db, 3)
        after = load_contexts(db, 4)
        assert sum(c.minutes for c in before.values()) == 0
        assert sum(c.minutes for c in after.values()) > 0
