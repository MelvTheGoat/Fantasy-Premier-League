"""Automatic substitutions.

The cases that matter are the ones where FPL's bench walk differs from "bring
on whoever scored most": a substitute skipped for breaking the formation, a
keeper who can only be covered by the other keeper, and a gameweek that is not
yet settled.
"""

from __future__ import annotations

from fplai.rules.autosubs import apply_auto_subs
from fplai.rules.squad import format_formation, is_valid_formation

from .conftest import build_lineup, results_for

#: The default lineup is 1 GK, 3 DEF, 5 MID, 2 FWD on the pitch, with a
#: substitute keeper plus two defenders and a midfielder on the bench.
STARTERS = (101, 201, 202, 203, 301, 302, 303, 304, 401, 402, 403)
BENCH = (102, 204, 205, 305)


def lineup():
    return build_lineup(starters=STARTERS, bench=BENCH)


class TestNoSubstitution:
    def test_nobody_is_substituted_when_everyone_plays(self, roster):
        results = results_for(STARTERS + BENCH)
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []
        assert new.starters == STARTERS

    def test_a_one_minute_cameo_keeps_a_starter_in(self, roster):
        results = results_for(STARTERS + BENCH, overrides={303: {"minutes": 1}})
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []

    def test_a_starter_who_played_but_scored_nothing_stays_in(self, roster):
        results = results_for(STARTERS + BENCH, overrides={303: {"total_points": 0}})
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []


class TestOutfieldSubstitution:
    def test_the_first_eligible_bench_player_comes_on(self, roster):
        results = results_for(STARTERS + BENCH, overrides={301: {"minutes": 0}})
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert [(s.out_element, s.in_element) for s in subs] == [(301, 204)]
        assert 204 in new.starters and 301 not in new.starters

    def test_the_outgoing_player_takes_the_substitutes_bench_slot(self, roster):
        results = results_for(STARTERS + BENCH, overrides={301: {"minutes": 0}})
        new, _ = apply_auto_subs(lineup(), roster, results)
        assert new.bench == (102, 301, 205, 305)

    def test_a_substitute_who_did_not_play_is_skipped(self, roster):
        results = results_for(
            STARTERS + BENCH, overrides={301: {"minutes": 0}, 204: {"minutes": 0}}
        )
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert [(s.out_element, s.in_element) for s in subs] == [(301, 205)]

    def test_nobody_comes_on_when_no_substitute_played(self, roster):
        blank_bench = dict.fromkeys(BENCH, None)
        results = results_for(
            STARTERS + BENCH,
            overrides={301: {"minutes": 0}, **{e: {"minutes": 0} for e in blank_bench}},
        )
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []
        assert new.starters == STARTERS

    def test_two_starters_are_replaced_in_bench_order(self, roster):
        results = results_for(
            STARTERS + BENCH, overrides={301: {"minutes": 0}, 302: {"minutes": 0}}
        )
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert [(s.out_element, s.in_element) for s in subs] == [(301, 204), (302, 205)]

    def test_three_starters_exhaust_the_outfield_bench(self, roster):
        results = results_for(
            STARTERS + BENCH,
            overrides={301: {"minutes": 0}, 302: {"minutes": 0}, 401: {"minutes": 0}},
        )
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert len(subs) == 3
        assert is_valid_formation(new.starters, roster)

    def test_a_fourth_absentee_cannot_be_covered(self, roster):
        results = results_for(
            STARTERS + BENCH,
            overrides={
                301: {"minutes": 0},
                302: {"minutes": 0},
                303: {"minutes": 0},
                401: {"minutes": 0},
            },
        )
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert len(subs) == 3, "only three outfield substitutes exist"


class TestFormationConstraint:
    def test_a_substitute_is_skipped_when_they_would_break_the_formation(self, roster):
        """A 3-5-2 losing a defender cannot take a fourth midfielder on.

        Bench order is (GK, MID, DEF, MID). The first outfield substitute is a
        midfielder, which would leave two defenders, so FPL moves past them to
        the defender in the next slot.
        """
        starters = (101, 201, 202, 203, 301, 302, 303, 304, 305, 401, 402)
        bench = (102, 306, 204, 307)
        assert format_formation(starters, roster) == "3-5-2"

        results = results_for(starters + bench, overrides={201: {"minutes": 0}})
        new, subs = apply_auto_subs(build_lineup(starters, bench), roster, results)

        assert [(s.out_element, s.in_element) for s in subs] == [(201, 204)]
        assert format_formation(new.starters, roster) == "3-5-2"

    def test_a_skipped_substitute_still_covers_a_later_vacancy(self, roster):
        """Being skipped for one vacancy does not take a substitute out of play.

        The bench midfielder cannot replace the missing defender, but when a
        midfielder is also missing they come on for that one instead.
        """
        starters = (101, 201, 202, 203, 301, 302, 303, 304, 305, 401, 402)
        bench = (102, 306, 204, 307)

        results = results_for(
            starters + bench, overrides={201: {"minutes": 0}, 301: {"minutes": 0}}
        )
        new, subs = apply_auto_subs(build_lineup(starters, bench), roster, results)

        assert [(s.out_element, s.in_element) for s in subs] == [(201, 204), (301, 306)]
        assert format_formation(new.starters, roster) == "3-5-2"

    def test_the_resulting_formation_is_always_legal(self, roster):
        """A 5-3-2 losing two forwards cannot end up with none."""
        starters = (101, 201, 202, 203, 204, 205, 301, 302, 303, 401, 402)
        bench = (102, 304, 305, 206)
        assert format_formation(starters, roster) == "5-3-2"

        results = results_for(
            starters + bench, overrides={401: {"minutes": 0}, 402: {"minutes": 0}}
        )
        new, _ = apply_auto_subs(build_lineup(starters, bench), roster, results)
        assert is_valid_formation(new.starters, roster)


class TestGoalkeeper:
    def test_the_bench_keeper_replaces_the_starting_keeper(self, roster):
        results = results_for(STARTERS + BENCH, overrides={101: {"minutes": 0}})
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert [(s.out_element, s.in_element) for s in subs] == [(101, 102)]
        assert new.starters[0] == 102
        assert new.bench[0] == 101

    def test_an_outfield_substitute_never_replaces_a_keeper(self, roster):
        """With the bench keeper also on zero minutes, nobody comes on."""
        results = results_for(
            STARTERS + BENCH, overrides={101: {"minutes": 0}, 102: {"minutes": 0}}
        )
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []
        assert new.starters == STARTERS

    def test_the_bench_keeper_never_replaces_an_outfield_player(self, roster):
        """Only the outfield bench slots cover an outfield absence."""
        results = results_for(
            STARTERS + BENCH,
            overrides={
                301: {"minutes": 0},
                204: {"minutes": 0},
                205: {"minutes": 0},
                305: {"minutes": 0},
            },
        )
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []
        assert 102 not in new.starters

    def test_keeper_and_outfield_substitutions_happen_together(self, roster):
        results = results_for(
            STARTERS + BENCH, overrides={101: {"minutes": 0}, 301: {"minutes": 0}}
        )
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert {(s.out_element, s.in_element) for s in subs} == {(101, 102), (301, 204)}
        assert is_valid_formation(new.starters, roster)


class TestTiming:
    def test_no_substitution_while_a_fixture_is_still_to_come(self, roster):
        """Mid-gameweek, a player yet to kick off looks like a player on zero
        minutes. Nothing may be decided until their fixtures have finished."""
        results = results_for(
            STARTERS + BENCH, overrides={301: {"minutes": 0, "fixtures_finished": False}}
        )
        new, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []
        assert new.starters == STARTERS

    def test_the_substitution_lands_once_the_fixture_finishes(self, roster):
        results = results_for(
            STARTERS + BENCH, overrides={301: {"minutes": 0, "fixtures_finished": True}}
        )
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert len(subs) == 1

    def test_a_double_gameweek_blank_in_the_first_match_is_not_subbed_yet(self, roster):
        """Zero minutes in the first of two fixtures settles nothing."""
        results = results_for(
            STARTERS + BENCH,
            overrides={301: {"minutes": 0, "fixtures_finished": False, "fixture_count": 2}},
        )
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []

    def test_a_double_gameweek_player_who_plays_the_second_match_stays_in(self, roster):
        results = results_for(
            STARTERS + BENCH,
            overrides={301: {"minutes": 62, "fixture_count": 2, "fixtures_finished": True}},
        )
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert subs == []

    def test_a_blank_gameweek_player_is_substituted(self, roster):
        """A club with no fixture produces no live row at all for its players."""
        results = results_for(STARTERS + BENCH)
        del results[301]
        _, subs = apply_auto_subs(lineup(), roster, results)
        assert [(s.out_element, s.in_element) for s in subs] == [(301, 204)]


class TestPurity:
    def test_the_submitted_lineup_is_not_mutated(self, roster):
        original = lineup()
        results = results_for(STARTERS + BENCH, overrides={301: {"minutes": 0}})
        apply_auto_subs(original, roster, results)
        assert original.starters == STARTERS
        assert original.bench == BENCH

    def test_the_armband_is_carried_through_unchanged(self, roster):
        original = build_lineup(STARTERS, BENCH, captain=401, vice_captain=402)
        results = results_for(STARTERS + BENCH, overrides={301: {"minutes": 0}})
        new, _ = apply_auto_subs(original, roster, results)
        assert (new.captain, new.vice_captain) == (401, 402)
