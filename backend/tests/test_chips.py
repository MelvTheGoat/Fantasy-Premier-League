"""Chip availability across the two 2026/27 sets."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.rules.chips import (
    available_chips,
    chip_deadline,
    chip_set_for_gameweek,
    chip_windows_from_bootstrap,
    default_chip_windows,
    expiring_chips,
    gameweeks_in_set,
    validate_chip_usage,
)
from fplai.rules.constants import Chip
from fplai.rules.types import ChipUsage

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def bootstrap():
    return json.loads((FIXTURES / "bootstrap_static.json").read_text())


ALL_CHIPS = {Chip.WILDCARD, Chip.FREE_HIT, Chip.BENCH_BOOST, Chip.TRIPLE_CAPTAIN}


class TestChipSets:
    @pytest.mark.parametrize("gameweek", [1, 5, 18, 19])
    def test_gameweeks_up_to_nineteen_use_the_first_set(self, gameweek):
        assert chip_set_for_gameweek(gameweek) == 1

    @pytest.mark.parametrize("gameweek", [20, 25, 38])
    def test_gameweeks_from_twenty_use_the_second_set(self, gameweek):
        assert chip_set_for_gameweek(gameweek) == 2

    def test_the_sets_cover_the_season_without_overlapping(self):
        first = set(gameweeks_in_set(1))
        second = set(gameweeks_in_set(2))
        assert first & second == set()
        assert first | second == set(range(1, 39))

    def test_gameweek_zero_is_rejected(self):
        with pytest.raises(ValueError):
            chip_set_for_gameweek(0)


class TestChipWindows:
    """The windows come from `start_event` / `stop_event` on bootstrap-static."""

    def test_the_coded_defaults_match_what_the_api_publishes(self, bootstrap):
        key = lambda window: (str(window.chip), window.chip_set)  # noqa: E731
        assert sorted(chip_windows_from_bootstrap(bootstrap), key=key) == sorted(
            default_chip_windows(), key=key
        )

    def test_transfer_chips_open_at_gameweek_two(self):
        windows = {(w.chip, w.chip_set): w for w in default_chip_windows()}
        assert windows[(Chip.WILDCARD, 1)].start_gameweek == 2
        assert windows[(Chip.FREE_HIT, 1)].start_gameweek == 2

    def test_team_chips_open_at_gameweek_one(self):
        windows = {(w.chip, w.chip_set): w for w in default_chip_windows()}
        assert windows[(Chip.BENCH_BOOST, 1)].start_gameweek == 1
        assert windows[(Chip.TRIPLE_CAPTAIN, 1)].start_gameweek == 1

    def test_an_unrecognised_chip_in_the_payload_is_ignored(self):
        """A new chip type must not break chip handling."""
        payload = {"chips": [
            {"name": "wildcard", "start_event": 2, "stop_event": 19},
            {"name": "some_new_chip", "start_event": 1, "stop_event": 38},
        ]}
        windows = chip_windows_from_bootstrap(payload)
        assert [w.chip for w in windows] == [Chip.WILDCARD]

    def test_a_chip_entry_missing_its_window_is_skipped(self):
        payload = {"chips": [{"name": "wildcard", "start_event": None, "stop_event": None}]}
        assert chip_windows_from_bootstrap(payload) == ()


class TestAvailability:
    def test_transfer_chips_cannot_be_played_in_gameweek_one(self):
        """Transfers before the opening deadline are unlimited anyway, so the
        game does not offer Wildcard or Free Hit until GW2."""
        assert available_chips(1, []) == {Chip.BENCH_BOOST, Chip.TRIPLE_CAPTAIN}

    def test_all_four_chips_are_available_from_gameweek_two(self):
        assert available_chips(2, []) == ALL_CHIPS

    def test_a_played_chip_is_gone_for_the_rest_of_its_set(self):
        used = [ChipUsage(Chip.WILDCARD, 8)]
        assert Chip.WILDCARD not in available_chips(12, used)
        assert available_chips(12, used) == ALL_CHIPS - {Chip.WILDCARD}

    def test_the_second_set_restores_every_chip(self):
        used = [
            ChipUsage(Chip.WILDCARD, 8),
            ChipUsage(Chip.FREE_HIT, 14),
            ChipUsage(Chip.BENCH_BOOST, 17),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 18),
        ]
        assert available_chips(20, used) == ALL_CHIPS

    def test_a_second_set_chip_does_not_come_back_again(self):
        used = [ChipUsage(Chip.WILDCARD, 8), ChipUsage(Chip.WILDCARD, 22)]
        assert Chip.WILDCARD not in available_chips(30, used)

    def test_no_chip_is_available_once_one_is_played_this_gameweek(self):
        assert available_chips(12, [ChipUsage(Chip.BENCH_BOOST, 12)]) == set()


class TestExpiry:
    def test_unplayed_first_set_chips_are_flagged_as_expiring(self):
        used = [ChipUsage(Chip.WILDCARD, 8)]
        assert expiring_chips(17, used) == ALL_CHIPS - {Chip.WILDCARD}

    def test_nothing_expires_once_the_first_set_is_spent(self):
        used = [
            ChipUsage(Chip.WILDCARD, 4),
            ChipUsage(Chip.FREE_HIT, 8),
            ChipUsage(Chip.BENCH_BOOST, 12),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 16),
        ]
        assert expiring_chips(18, used) == set()

    def test_the_second_set_never_expires_early(self):
        assert expiring_chips(25, []) == set()

    def test_gameweek_nineteen_is_the_last_chance_for_the_first_set(self):
        assert expiring_chips(19, []) == ALL_CHIPS
        assert expiring_chips(20, []) == set()


class TestChipDeadline:
    def test_the_first_set_runs_out_at_gameweek_nineteen(self):
        deadline = chip_deadline(15, [])
        assert deadline.chip_set == 1
        assert deadline.last_gameweek == 19
        assert deadline.gameweeks_remaining == 5
        assert deadline.unused == frozenset(ALL_CHIPS)

    def test_four_chips_and_four_gameweeks_means_one_must_be_played_now(self):
        assert chip_deadline(16, []).must_play_now

    def test_five_gameweeks_for_four_chips_still_leaves_slack(self):
        assert not chip_deadline(15, []).must_play_now

    def test_one_chip_and_one_gameweek_means_play_it(self):
        used = [
            ChipUsage(Chip.WILDCARD, 4),
            ChipUsage(Chip.FREE_HIT, 8),
            ChipUsage(Chip.BENCH_BOOST, 12),
        ]
        assert chip_deadline(19, used).must_play_now

    def test_nothing_is_urgent_when_no_chips_are_left(self):
        used = [
            ChipUsage(Chip.WILDCARD, 4),
            ChipUsage(Chip.FREE_HIT, 8),
            ChipUsage(Chip.BENCH_BOOST, 12),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 16),
        ]
        assert not chip_deadline(19, used).must_play_now


class TestValidation:
    def test_a_clean_season_of_chip_plays_is_valid(self):
        used = [
            ChipUsage(Chip.WILDCARD, 6),
            ChipUsage(Chip.BENCH_BOOST, 14),
            ChipUsage(Chip.FREE_HIT, 18),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 19),
            ChipUsage(Chip.WILDCARD, 24),
            ChipUsage(Chip.TRIPLE_CAPTAIN, 31),
        ]
        assert validate_chip_usage(used).ok

    def test_two_chips_in_one_gameweek_is_rejected(self):
        used = [ChipUsage(Chip.BENCH_BOOST, 14), ChipUsage(Chip.TRIPLE_CAPTAIN, 14)]
        assert "multiple_chips" in validate_chip_usage(used).codes

    def test_replaying_a_chip_within_a_set_is_rejected(self):
        used = [ChipUsage(Chip.WILDCARD, 6), ChipUsage(Chip.WILDCARD, 14)]
        result = validate_chip_usage(used)
        assert "chip_reused" in result.codes
        assert "GW6" in result.errors[0].message

    def test_the_same_chip_in_each_set_is_allowed(self):
        used = [ChipUsage(Chip.WILDCARD, 6), ChipUsage(Chip.WILDCARD, 25)]
        assert validate_chip_usage(used).ok

    def test_a_chip_outside_the_season_is_rejected(self):
        assert "chip_gameweek" in validate_chip_usage([ChipUsage(Chip.WILDCARD, 39)]).codes

    def test_the_chip_usage_knows_its_own_set(self):
        assert ChipUsage(Chip.WILDCARD, 6).chip_set == 1
        assert ChipUsage(Chip.WILDCARD, 25).chip_set == 2
