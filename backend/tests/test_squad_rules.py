"""Squad composition, budget, club limits and formation legality."""

from __future__ import annotations

import pytest

from fplai.rules.constants import MAX_PLAYERS_PER_CLUB, STARTING_BUDGET, Position
from fplai.rules.squad import (
    format_formation,
    formation_of,
    is_valid_formation,
    validate_formation,
    validate_lineup,
    validate_squad,
)
from fplai.rules.types import Lineup, Squad, SquadPick

from .conftest import (
    DEFAULT_SQUAD_ELEMENTS,
    SPARE_TEAM,
    build_lineup,
    build_roster,
    build_squad,
)


class TestSquadComposition:
    def test_a_correctly_shaped_squad_is_valid(self, roster):
        built = build_squad(roster=roster)
        spend = sum(p.purchase_price for p in built.picks)
        squad = Squad(picks=built.picks, bank=STARTING_BUDGET - spend)
        assert validate_squad(squad, roster).ok

    def test_squad_must_have_fifteen_players(self, roster):
        squad = build_squad(DEFAULT_SQUAD_ELEMENTS[:14], roster=roster)
        assert "squad_size" in validate_squad(squad, roster).codes

    @pytest.mark.parametrize(
        ("elements", "why"),
        [
            (
                (101, 102, 103, 201, 202, 203, 204, 301, 302, 303, 304, 305, 401, 402, 403),
                "three keepers",
            ),
            (
                (101, 102, 201, 202, 203, 204, 301, 302, 303, 304, 305, 401, 402, 403, 404),
                "four defenders",
            ),
            (
                (101, 102, 201, 202, 203, 204, 205, 301, 302, 303, 304, 401, 402, 403, 404),
                "four midfielders",
            ),
        ],
    )
    def test_squad_must_match_2_5_5_3(self, roster, elements, why):
        squad = build_squad(elements, roster=roster)
        assert "squad_composition" in validate_squad(squad, roster).codes, why

    def test_a_player_cannot_be_picked_twice(self, roster):
        elements = DEFAULT_SQUAD_ELEMENTS[:-1] + (DEFAULT_SQUAD_ELEMENTS[0],)
        squad = build_squad(elements, roster=roster)
        assert "squad_duplicate" in validate_squad(squad, roster).codes


class TestClubLimit:
    def test_the_default_squad_spans_fifteen_clubs(self, roster):
        """Guards the fixture: the club tests only mean anything if the default
        squad has no incidental club clustering of its own."""
        teams = [roster[e].team for e in DEFAULT_SQUAD_ELEMENTS]
        assert len(set(teams)) == 15
        assert SPARE_TEAM not in teams

    def test_three_players_from_one_club_is_allowed(self):
        teams = dict.fromkeys((201, 202, 203), SPARE_TEAM)
        roster = build_roster(teams=teams)
        squad = build_squad(roster=roster)
        assert "club_limit" not in validate_squad(squad, roster).codes

    def test_four_players_from_one_club_is_rejected(self):
        teams = dict.fromkeys((201, 202, 203, 204), SPARE_TEAM)
        roster = build_roster(teams=teams)
        squad = build_squad(roster=roster)
        result = validate_squad(squad, roster)
        assert "club_limit" in result.codes
        assert f"at most {MAX_PLAYERS_PER_CLUB}" in result.errors[0].message

    def test_the_limit_counts_bench_players_too(self):
        # 204 and 205 sit on the bench in the default lineup but still count.
        teams = dict.fromkeys((201, 202, 204, 205), SPARE_TEAM)
        roster = build_roster(teams=teams)
        squad = build_squad(roster=roster)
        assert "club_limit" in validate_squad(squad, roster).codes


class TestBudget:
    def test_a_squad_within_budget_passes(self, roster):
        picks = tuple(SquadPick(e, 60) for e in DEFAULT_SQUAD_ELEMENTS)  # 15 x £6.0m = £90m
        squad = Squad(picks=picks, bank=100)
        assert validate_squad(squad, roster).ok

    def test_a_squad_over_budget_is_rejected(self, roster):
        picks = tuple(SquadPick(e, 70) for e in DEFAULT_SQUAD_ELEMENTS)  # £105m
        squad = Squad(picks=picks, bank=0)
        result = validate_squad(squad, roster)
        assert "budget" in result.codes
        assert "£5.0m over" in result.errors[0].message

    def test_spending_exactly_the_budget_is_allowed(self, roster):
        picks = tuple(SquadPick(e, 66) for e in DEFAULT_SQUAD_ELEMENTS[:14])
        picks += (SquadPick(DEFAULT_SQUAD_ELEMENTS[14], STARTING_BUDGET - 66 * 14),)
        squad = Squad(picks=picks, bank=0)
        assert sum(p.purchase_price for p in picks) == STARTING_BUDGET
        assert validate_squad(squad, roster).ok

    def test_the_bank_counts_against_the_budget(self, roster):
        picks = tuple(SquadPick(e, 66) for e in DEFAULT_SQUAD_ELEMENTS)  # £99.0m
        assert validate_squad(Squad(picks=picks, bank=10), roster).ok
        assert "budget" in validate_squad(Squad(picks=picks, bank=20), roster).codes

    def test_the_bank_cannot_go_negative(self, roster):
        picks = tuple(SquadPick(e, 60) for e in DEFAULT_SQUAD_ELEMENTS)
        assert "negative_bank" in validate_squad(Squad(picks=picks, bank=-5), roster).codes

    def test_building_a_squad_is_costed_at_market_price(self):
        roster = build_roster(prices={e: 70 for e in DEFAULT_SQUAD_ELEMENTS})
        # Purchase prices say £60m each, but a fresh build pays today's £70m.
        picks = tuple(SquadPick(e, 60) for e in DEFAULT_SQUAD_ELEMENTS)
        squad = Squad(picks=picks, bank=0)
        assert validate_squad(squad, roster).ok
        assert "budget" in validate_squad(squad, roster, use_purchase_prices=False).codes


class TestFormation:
    @pytest.mark.parametrize(
        ("defenders", "midfielders", "forwards", "name"),
        [
            (3, 4, 3, "3-4-3"),
            (4, 4, 2, "4-4-2"),
            (5, 3, 2, "5-3-2"),
            (3, 5, 2, "3-5-2"),
            (4, 3, 3, "4-3-3"),
            (5, 4, 1, "5-4-1"),
            (4, 5, 1, "4-5-1"),
        ],
    )
    def test_legal_formations_are_accepted(self, roster, defenders, midfielders, forwards, name):
        starters = (
            (101,)
            + tuple(range(201, 201 + defenders))
            + tuple(range(301, 301 + midfielders))
            + tuple(range(401, 401 + forwards))
        )
        assert len(starters) == 11
        assert is_valid_formation(starters, roster)
        if name:
            assert format_formation(starters, roster) == name

    @pytest.mark.parametrize(
        ("starters", "code"),
        [
            # Two defenders: below the minimum of three.
            ((101, 201, 202, 301, 302, 303, 304, 305, 401, 402, 403), "formation_min"),
            # No forward.
            ((101, 201, 202, 203, 204, 205, 301, 302, 303, 304, 305), "formation_min"),
            # One midfielder: below the minimum of two.
            ((101, 201, 202, 203, 204, 205, 301, 401, 402, 403, 404), "formation_min"),
            # Two goalkeepers.
            ((101, 102, 201, 202, 203, 301, 302, 303, 304, 401, 402), "formation_max"),
            # No goalkeeper.
            ((201, 202, 203, 204, 205, 301, 302, 303, 304, 401, 402), "formation_min"),
        ],
    )
    def test_illegal_formations_are_rejected(self, roster, starters, code):
        assert not is_valid_formation(starters, roster)
        assert code in validate_formation(starters, roster).codes

    def test_an_xi_must_have_eleven_players(self, roster):
        starters = (101, 201, 202, 203, 301, 302, 303, 401, 402, 403)
        assert not is_valid_formation(starters, roster)
        assert "xi_size" in validate_formation(starters, roster).codes

    def test_formation_is_read_from_the_players_not_assumed(self, roster):
        starters = (101, 201, 202, 203, 204, 301, 302, 303, 401, 402, 403)
        assert formation_of(starters, roster) == (4, 3, 3)

    def test_position_comes_from_the_roster_not_the_element_id(self):
        """A player reclassified for the season must be read from the roster."""
        roster = build_roster()
        reclassified = roster[301]
        roster[301] = type(reclassified)(
            element=301, position=Position.DEF, team=reclassified.team, price=reclassified.price
        )
        # 301 now counts as a defender, making this a 5-2-3 -- still legal.
        starters = (101, 201, 202, 203, 204, 301, 302, 303, 401, 402, 403)
        assert formation_of(starters, roster) == (5, 2, 3)
        assert is_valid_formation(starters, roster)


class TestLineupValidation:
    def test_the_default_lineup_is_valid(self, roster, squad, lineup):
        assert validate_lineup(lineup, squad, roster).ok

    def test_the_lineup_must_use_exactly_the_squad(self, roster, squad):
        lineup = build_lineup(bench=(102, 204, 205, 306))  # 306 is not owned
        result = validate_lineup(lineup, squad, roster)
        assert "lineup_not_in_squad" in result.codes
        assert "lineup_missing_squad" in result.codes

    def test_the_substitute_keeper_must_sit_in_the_first_bench_slot(self, roster, squad):
        lineup = build_lineup(bench=(204, 102, 205, 305))
        assert "bench_gk_slot" in validate_lineup(lineup, squad, roster).codes

    def test_the_captain_must_start(self, roster, squad):
        lineup = build_lineup(captain=205)  # on the bench
        assert "captain_not_starting" in validate_lineup(lineup, squad, roster).codes

    def test_the_vice_captain_must_start(self, roster, squad):
        lineup = build_lineup(vice_captain=205)
        assert "vice_not_starting" in validate_lineup(lineup, squad, roster).codes

    def test_the_captain_and_vice_must_differ(self, roster, squad):
        lineup = build_lineup(captain=403, vice_captain=403)
        assert "captain_is_vice" in validate_lineup(lineup, squad, roster).codes

    def test_the_bench_must_hold_four_players(self, roster, squad):
        lineup = Lineup(
            starters=(101, 201, 202, 203, 301, 302, 303, 304, 401, 402, 403),
            bench=(102, 204, 205),
            captain=403,
            vice_captain=402,
        )
        assert "bench_size" in validate_lineup(lineup, squad, roster).codes

    def test_every_broken_rule_is_reported_not_just_the_first(self, roster, squad):
        lineup = Lineup(
            starters=(101, 102, 201, 202, 301, 302, 303, 304, 401, 402, 403),
            bench=(203, 204, 205, 305),
            captain=203,
            vice_captain=203,
        )
        codes = set(validate_lineup(lineup, squad, roster).codes)
        assert {"formation_min", "formation_max", "bench_gk_slot",
                "captain_not_starting", "captain_is_vice"} <= codes
