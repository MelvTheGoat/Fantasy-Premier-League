"""Gameweek scoring: multipliers, bench boost, hits, lockdown and season totals.

Player points always come from the FPL API, so these tests feed in raw points
and assert only on what the manager-level rules do with them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from fplai.rules.constants import Chip
from fplai.rules.scoring import (
    is_final,
    lockdown_time,
    score_gameweek,
    summarise_season,
)

from .conftest import build_lineup, results_for

STARTERS = (101, 201, 202, 203, 301, 302, 303, 304, 401, 402, 403)
BENCH = (102, 204, 205, 305)
ALL = STARTERS + BENCH
UK = ZoneInfo("Europe/London")


def lineup():
    return build_lineup(STARTERS, BENCH, captain=401, vice_captain=402)


def score(roster, results, **kwargs):
    return score_gameweek(5, lineup(), roster, results, **kwargs)


class TestBasicScoring:
    def test_only_the_eleven_starters_count(self, roster):
        results = results_for(ALL, points=2)
        gw = score(roster, results)
        # 11 starters at 2, plus the captain's doubled 2.
        assert gw.points_before_hits == 11 * 2 + 2

    def test_the_bench_scores_nothing_by_default(self, roster):
        results = results_for(ALL, points=2, overrides={e: {"total_points": 9} for e in BENCH})
        gw = score(roster, results)
        assert gw.points_before_hits == 11 * 2 + 2
        assert gw.bench_points == 4 * 9

    def test_points_are_taken_from_the_api_not_recalculated(self, roster):
        results = results_for(ALL, points=0, overrides={301: {"total_points": 17}})
        gw = score(roster, results)
        assert gw.score_for(301).points == 17

    def test_the_captain_is_doubled(self, roster):
        results = results_for(ALL, points=1, overrides={401: {"total_points": 10}})
        gw = score(roster, results)
        assert gw.score_for(401).multiplier == 2
        assert gw.captain_points == 20

    def test_a_negative_score_is_doubled_too(self, roster):
        results = results_for(ALL, points=0, overrides={401: {"total_points": -1}})
        assert score(roster, results).captain_points == -2


class TestChipScoring:
    def test_triple_captain_triples(self, roster):
        results = results_for(ALL, points=1, overrides={401: {"total_points": 10}})
        gw = score(roster, results, chip=Chip.TRIPLE_CAPTAIN)
        assert gw.captain_points == 30

    def test_bench_boost_counts_the_bench(self, roster):
        results = results_for(ALL, points=2)
        normal = score(roster, results)
        boosted = score(roster, results, chip=Chip.BENCH_BOOST)
        assert boosted.points_before_hits == normal.points_before_hits + 4 * 2

    def test_bench_boost_still_doubles_the_captain_only_once(self, roster):
        results = results_for(ALL, points=1, overrides={401: {"total_points": 10}})
        gw = score(roster, results, chip=Chip.BENCH_BOOST)
        assert gw.score_for(401).multiplier == 2
        # 14 players at 1 point each, plus the captain's 10 doubled.
        assert gw.points_before_hits == 14 * 1 + 10 * 2

    def test_a_bench_player_scores_zero_without_the_boost(self, roster):
        results = results_for(ALL, points=5)
        gw = score(roster, results)
        assert gw.score_for(102).multiplier == 0
        assert gw.score_for(102).total == 0


class TestHits:
    def test_a_hit_is_subtracted_from_the_total(self, roster):
        results = results_for(ALL, points=2)
        gw = score(roster, results, transfer_cost=4)
        assert gw.points_before_hits == 24
        assert gw.points == 20

    def test_hits_are_reported_separately_from_the_raw_score(self, roster):
        gw = score(roster, results_for(ALL, points=2), transfer_cost=8)
        assert (gw.points_before_hits, gw.transfer_cost, gw.points) == (24, 8, 16)


class TestScoringWithAutoSubs:
    def test_a_substitute_who_comes_on_scores(self, roster):
        results = results_for(
            ALL,
            points=1,
            overrides={301: {"minutes": 0, "total_points": 0}, 204: {"total_points": 7}},
        )
        gw = score(roster, results)
        assert gw.score_for(204).started
        assert gw.score_for(204).subbed_on
        assert gw.score_for(204).total == 7

    def test_a_substituted_starter_stops_counting(self, roster):
        results = results_for(
            ALL, points=1, overrides={301: {"minutes": 0, "total_points": 0}}
        )
        gw = score(roster, results)
        assert gw.score_for(301).subbed_off
        assert not gw.score_for(301).started
        assert gw.score_for(301).multiplier == 0

    def test_a_captain_on_zero_minutes_hands_the_double_to_the_vice(self, roster):
        results = results_for(
            ALL,
            points=1,
            overrides={401: {"minutes": 0, "total_points": 0}, 402: {"total_points": 9}},
        )
        gw = score(roster, results)
        assert gw.captaincy.passed_to_vice
        assert gw.score_for(402).multiplier == 2
        assert gw.captain_points == 18

    def test_a_substitute_can_take_the_armband_only_via_the_vice(self, roster):
        """The armband never passes to a player who was not captain or vice."""
        results = results_for(
            ALL, points=1, overrides={401: {"minutes": 0}, 402: {"minutes": 0}}
        )
        gw = score(roster, results)
        assert gw.captaincy.element is None
        assert all(s.multiplier <= 1 for s in gw.scores)

    def test_the_submitted_lineup_is_kept_alongside_the_resolved_one(self, roster):
        results = results_for(ALL, points=1, overrides={301: {"minutes": 0}})
        gw = score(roster, results)
        assert gw.submitted_lineup.starters == STARTERS
        assert gw.lineup.starters != STARTERS
        assert len(gw.substitutions) == 1

    def test_bench_boost_means_no_substitution_is_needed_to_score(self, roster):
        """Under Bench Boost a substitution changes nothing, because every
        player already counts once."""
        results = results_for(ALL, points=3, overrides={301: {"minutes": 0, "total_points": 0}})
        gw = score(roster, results, chip=Chip.BENCH_BOOST)
        assert gw.points_before_hits == 14 * 3 + 3


class TestLockdown:
    def test_lockdown_is_nine_in_the_morning_the_day_after(self):
        last = datetime(2026, 9, 13, 16, 30, tzinfo=UK)
        assert lockdown_time(last) == datetime(2026, 9, 14, 9, 0, tzinfo=UK)

    def test_a_late_kickoff_past_midnight_utc_uses_the_uk_day(self):
        """A 20:00 UK kickoff in summer is 19:00 UTC; the day after is still
        the day after in UK terms."""
        last = datetime(2026, 8, 22, 19, 0, tzinfo=timezone.utc)
        assert lockdown_time(last).date() == datetime(2026, 8, 23).date()

    def test_points_are_provisional_before_lockdown(self):
        last = datetime(2026, 9, 13, 16, 30, tzinfo=UK)
        assert not is_final(last, datetime(2026, 9, 14, 8, 59, tzinfo=UK))

    def test_points_are_final_from_lockdown_onwards(self):
        last = datetime(2026, 9, 13, 16, 30, tzinfo=UK)
        assert is_final(last, datetime(2026, 9, 14, 9, 0, tzinfo=UK))

    def test_a_gameweek_with_no_fixtures_played_is_never_final(self):
        assert not is_final(None, datetime(2026, 9, 14, 9, 0, tzinfo=UK))

    def test_the_score_carries_a_provisional_label(self, roster):
        provisional = score(roster, results_for(ALL), final=False)
        assert provisional.status_label == "Provisional"
        assert score(roster, results_for(ALL), final=True).status_label == "Final"


class TestSeasonSummary:
    def make(self, roster, gameweek, points, cost=0):
        results = results_for(ALL, points=0, overrides={301: {"total_points": points}})
        return score_gameweek(gameweek, lineup(), roster, results, transfer_cost=cost)

    def test_totals_accumulate_across_gameweeks(self, roster):
        scores = [self.make(roster, 1, 50), self.make(roster, 2, 60)]
        summary = summarise_season(scores, averages={})
        assert summary.total_points == 110
        assert summary.gameweeks_played == 2

    def test_hits_are_tracked_and_netted_off(self, roster):
        scores = [self.make(roster, 1, 50, cost=4), self.make(roster, 2, 60)]
        summary = summarise_season(scores, averages={})
        assert summary.total_transfer_cost == 4
        assert summary.total_points == 106

    def test_beating_the_average_is_counted(self, roster):
        scores = [self.make(roster, 1, 50), self.make(roster, 2, 30)]
        summary = summarise_season(scores, averages={1: 45.0, 2: 55.0})
        assert summary.gameweeks_beating_average == 1
        assert summary.beat_average_rate == 0.5

    def test_matching_the_average_exactly_does_not_count_as_beating_it(self, roster):
        summary = summarise_season([self.make(roster, 1, 50)], averages={1: 50.0})
        assert summary.gameweeks_beating_average == 0

    def test_the_cumulative_series_line_up_for_charting(self, roster):
        scores = [self.make(roster, 1, 50), self.make(roster, 2, 60)]
        summary = summarise_season(scores, averages={1: 45.0, 2: 55.0})
        assert summary.cumulative_points == [50, 110]
        assert summary.cumulative_average == [45.0, 100.0]

    def test_a_gameweek_without_a_recorded_average_still_counts_points(self, roster):
        scores = [self.make(roster, 1, 50), self.make(roster, 2, 60)]
        summary = summarise_season(scores, averages={1: 45.0})
        assert summary.total_points == 110
        assert summary.gameweeks_played == 2
        assert summary.gameweeks_beating_average == 1

    def test_gameweeks_are_summarised_in_order_however_they_arrive(self, roster):
        scores = [self.make(roster, 2, 60), self.make(roster, 1, 50)]
        summary = summarise_season(scores, averages={})
        assert summary.cumulative_points == [50, 110]

    def test_an_empty_season_summarises_to_zero(self):
        summary = summarise_season([], averages={})
        assert summary.total_points == 0
        assert summary.beat_average_rate == 0.0
