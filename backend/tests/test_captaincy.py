"""Captaincy, the vice-captain hand-over, and the Triple Captain chip."""

from __future__ import annotations

import pytest

from fplai.rules.captaincy import captain_multiplier, resolve_captaincy
from fplai.rules.constants import Chip

from .conftest import build_lineup, results_for

STARTERS = (101, 201, 202, 203, 301, 302, 303, 304, 401, 402, 403)
BENCH = (102, 204, 205, 305)


def lineup():
    return build_lineup(STARTERS, BENCH, captain=401, vice_captain=402)


class TestMultiplier:
    def test_the_captain_is_doubled(self):
        outcome = resolve_captaincy(lineup(), results_for(STARTERS + BENCH))
        assert outcome.element == 401
        assert outcome.multiplier == 2
        assert not outcome.passed_to_vice

    def test_triple_captain_triples_instead(self):
        outcome = resolve_captaincy(
            lineup(), results_for(STARTERS + BENCH), chip=Chip.TRIPLE_CAPTAIN
        )
        assert outcome.multiplier == 3
        assert outcome.triple

    @pytest.mark.parametrize(
        ("chip", "expected"),
        [
            (None, 2),
            (Chip.TRIPLE_CAPTAIN, 3),
            (Chip.BENCH_BOOST, 2),
            (Chip.WILDCARD, 2),
            (Chip.FREE_HIT, 2),
        ],
    )
    def test_only_triple_captain_changes_the_multiplier(self, chip, expected):
        assert captain_multiplier(chip) == expected


class TestVicePassing:
    def test_the_armband_passes_when_the_captain_plays_no_minutes(self):
        results = results_for(STARTERS + BENCH, overrides={401: {"minutes": 0}})
        outcome = resolve_captaincy(lineup(), results)
        assert outcome.element == 402
        assert outcome.multiplier == 2
        assert outcome.passed_to_vice

    def test_the_armband_stays_on_a_captain_who_played_one_minute(self):
        results = results_for(STARTERS + BENCH, overrides={401: {"minutes": 1}})
        outcome = resolve_captaincy(lineup(), results)
        assert outcome.element == 401
        assert not outcome.passed_to_vice

    def test_the_armband_stays_on_a_captain_who_played_but_scored_nothing(self):
        results = results_for(
            STARTERS + BENCH, overrides={401: {"minutes": 90, "total_points": 0}}
        )
        assert resolve_captaincy(lineup(), results).element == 401

    def test_the_vice_is_tripled_too_under_triple_captain(self):
        results = results_for(STARTERS + BENCH, overrides={401: {"minutes": 0}})
        outcome = resolve_captaincy(lineup(), results, chip=Chip.TRIPLE_CAPTAIN)
        assert outcome.element == 402
        assert outcome.multiplier == 3

    def test_nobody_is_multiplied_when_neither_captain_plays(self):
        results = results_for(
            STARTERS + BENCH, overrides={401: {"minutes": 0}, 402: {"minutes": 0}}
        )
        outcome = resolve_captaincy(lineup(), results)
        assert outcome.element is None
        assert outcome.multiplier == 1
        assert outcome.extra_multiplier == 0

    def test_a_captain_with_no_result_row_hands_over(self):
        """A blank gameweek leaves the captain without a live row at all."""
        results = results_for(STARTERS + BENCH)
        del results[401]
        assert resolve_captaincy(lineup(), results).element == 402
