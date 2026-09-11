"""The expected-points model.

The model is built from separately inspectable components rather than fitted
end to end, so each one is tested for the behaviour it is supposed to have:
shrinkage that tames small samples, availability that zeroes a suspended
player, fixtures that move projections in the right direction, and blanks and
doubles that fall out of fixture counts rather than special cases.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fplai.model.player_rates import (
    POSITION_PRIORS,
    PlayerHistory,
    availability_multiplier,
    estimate_rates,
    price_factor,
)
from fplai.model.team_strength import (
    LEAGUE_MEAN_GOALS,
    TeamForm,
    expectation_for_fixture,
)
from fplai.model.xpts import (
    expected_bonus,
    expected_concession_points,
    project_fixture,
    project_gameweek,
)
from fplai.rules.constants import Position
from fplai.rules.scoring_table import DEFAULT_SCORING, ScoringTable

FIXTURES = Path(__file__).parent / "fixtures"


def history(position=Position.MID, **overrides) -> PlayerHistory:
    """A regular starter with three matches behind them, unless overridden."""
    defaults = dict(
        element=1, position=position, minutes=270, starts=3, appearances=3,
        matches_available=3, recent_starts=3, recent_appearances=3, recent_matches=3,
        price=70,
    )
    defaults.update(overrides)
    return PlayerHistory(**defaults)


def easy_home():
    return expectation_for_fixture(
        1, 2, is_home=True, difficulty=2, team_form=None, opponent_form=None
    )


def hard_away():
    return expectation_for_fixture(
        1, 2, is_home=False, difficulty=5, team_form=None, opponent_form=None
    )


class TestScoringTable:
    def test_the_table_is_read_from_the_api(self):
        bootstrap = json.loads((FIXTURES / "bootstrap_static.json").read_text())
        table = ScoringTable.from_bootstrap(bootstrap)
        assert table == DEFAULT_SCORING, "the coded defaults should match the live game"

    def test_goalkeeper_goals_are_worth_ten_in_2026_27(self):
        """Up from six. A table carried over from an earlier season would
        mis-project every goalkeeper without failing anything else."""
        assert DEFAULT_SCORING.goals_scored[Position.GKP] == 10
        assert DEFAULT_SCORING.goals_scored[Position.FWD] == 4

    def test_a_payload_without_a_scoring_block_falls_back_to_defaults(self):
        assert ScoringTable.from_bootstrap({}) == DEFAULT_SCORING

    def test_an_unknown_field_upstream_does_not_break_it(self):
        table = ScoringTable.from_bootstrap(
            {"game_config": {"scoring": {"assists": 3, "some_new_thing": 99}}}
        )
        assert table.assists == 3

    @pytest.mark.parametrize(
        ("saves", "points"), [(0, 0), (2, 0), (3, 1), (5, 1), (6, 2), (9, 3)]
    )
    def test_saves_score_once_per_three(self, saves, points):
        assert DEFAULT_SCORING.save_points(saves) == points

    @pytest.mark.parametrize(
        ("conceded", "points"), [(0, 0), (1, 0), (2, -1), (3, -1), (4, -2)]
    )
    def test_concessions_deduct_once_per_two(self, conceded, points):
        assert DEFAULT_SCORING.concession_points(Position.DEF, conceded) == points

    def test_midfielders_are_not_deducted_for_concessions(self):
        assert DEFAULT_SCORING.concession_points(Position.MID, 4) == 0


class TestAvailability:
    @pytest.mark.parametrize("status", ["s", "u", "n"])
    def test_a_player_who_cannot_feature_is_zeroed(self, status):
        assert availability_multiplier(history(status=status)) == 0.0

    def test_an_injured_player_is_zeroed(self):
        assert availability_multiplier(history(status="i")) == 0.0

    def test_the_apis_own_playing_chance_is_trusted_where_given(self):
        assert availability_multiplier(history(chance_of_playing=75)) == 0.75
        assert availability_multiplier(history(status="i", chance_of_playing=25)) == 0.25

    def test_a_fit_player_is_unaffected(self):
        assert availability_multiplier(history()) == 1.0

    def test_availability_flows_through_to_minutes(self):
        fit = estimate_rates(history())
        doubtful = estimate_rates(history(chance_of_playing=50))
        assert doubtful.expected_minutes == pytest.approx(fit.expected_minutes * 0.5)

    def test_a_suspended_player_projects_nothing(self):
        rates = estimate_rates(history(status="s", expected_goals=3.0))
        projection = project_fixture(rates, easy_home(), 4)
        assert projection.total == pytest.approx(0.0, abs=1e-9)


class TestStartProbability:
    def test_a_player_who_starts_every_match_is_modelled_as_a_starter(self):
        assert estimate_rates(history()).probability_of_starting > 0.8

    def test_a_rotation_risk_is_modelled_as_one(self):
        rates = estimate_rates(
            history(recent_starts=1, recent_appearances=3, starts=1)
        )
        assert 0.2 < rates.probability_of_starting < 0.5

    def test_a_substitute_rarely_reaches_sixty_minutes(self):
        rates = estimate_rates(
            history(minutes=25, starts=0, appearances=2, recent_starts=0, recent_appearances=2)
        )
        assert rates.probability_of_60_minutes < 0.2

    def test_a_player_with_no_record_is_a_squad_player_not_a_starter(self):
        rates = estimate_rates(
            PlayerHistory(element=1, position=Position.MID, price=50)
        )
        assert 0.2 < rates.probability_of_starting < 0.5

    def test_appearing_is_never_less_likely_than_starting(self):
        rates = estimate_rates(history(recent_starts=3, recent_appearances=1))
        assert rates.probability_of_appearing >= rates.probability_of_starting


class TestShrinkage:
    def test_a_hot_streak_is_pulled_toward_the_prior(self):
        """Two goals in three games is 0.67 per 90 raw, which would make this
        player the best in the game. Shrinkage has to tame it."""
        rates = estimate_rates(history(expected_goals=2.0, price=70))
        raw = 2.0 / 270 * 90
        assert rates.goals_per_90 < raw / 1.5

    def test_more_minutes_means_less_shrinkage(self):
        light = estimate_rates(history(minutes=270, expected_goals=2.0))
        heavy = estimate_rates(
            history(minutes=2700, expected_goals=20.0, starts=30, matches_available=30)
        )
        raw = 2.0 / 270 * 90
        assert abs(heavy.goals_per_90 - raw) < abs(light.goals_per_90 - raw)

    def test_a_player_with_no_minutes_gets_the_prior(self):
        rates = estimate_rates(
            PlayerHistory(element=1, position=Position.FWD, price=60)
        )
        assert rates.goals_per_90 == pytest.approx(POSITION_PRIORS[Position.FWD]["goals"])

    def test_prior_seasons_inform_a_player_with_little_record(self):
        """An established scorer should not be shrunk like a debutant."""
        debutant = estimate_rates(history(position=Position.FWD, minutes=90))
        established = estimate_rates(
            history(
                position=Position.FWD, minutes=90,
                prior_minutes=3000, prior_goals=22,
            )
        )
        assert established.goals_per_90 > debutant.goals_per_90

    def test_expected_goals_lead_over_actual_goals(self):
        """xG settles faster than finishing does."""
        lucky = estimate_rates(history(goals=3, expected_goals=0.5))
        unlucky = estimate_rates(history(goals=0, expected_goals=3.0))
        assert unlucky.goals_per_90 > lucky.goals_per_90


class TestPriceScaling:
    def test_a_dearer_forward_gets_a_higher_goal_prior(self):
        cheap = estimate_rates(
            PlayerHistory(element=1, position=Position.FWD, price=45)
        )
        premium = estimate_rates(
            PlayerHistory(element=2, position=Position.FWD, price=130)
        )
        assert premium.goals_per_90 > cheap.goals_per_90 * 2

    def test_defensive_contributions_do_not_scale_with_price(self):
        """DefCon is about role, not quality: a cheap holding midfielder racks
        them up and an expensive winger does not."""
        cheap = estimate_rates(PlayerHistory(element=1, position=Position.MID, price=45))
        premium = estimate_rates(PlayerHistory(element=2, position=Position.MID, price=130))
        assert cheap.defcon_per_90 == pytest.approx(premium.defcon_per_90)

    def test_the_adjustment_is_bounded(self):
        assert price_factor(Position.FWD, 400, "goals") <= 3.0
        assert price_factor(Position.FWD, 1, "goals") >= 0.35

    def test_an_unknown_price_leaves_the_prior_alone(self):
        assert price_factor(Position.FWD, 0, "goals") == 1.0


class TestTeamStrength:
    def test_an_easier_fixture_means_more_goals_and_fewer_conceded(self):
        easy, hard = easy_home(), hard_away()
        assert easy.expected_scored > hard.expected_scored
        assert easy.expected_conceded < hard.expected_conceded

    def test_home_advantage_raises_expected_goals(self):
        home = expectation_for_fixture(1, 2, is_home=True, difficulty=3,
                                       team_form=None, opponent_form=None)
        away = expectation_for_fixture(1, 2, is_home=False, difficulty=3,
                                       team_form=None, opponent_form=None)
        assert home.expected_scored > away.expected_scored

    def test_clean_sheet_probability_falls_as_concessions_rise(self):
        assert easy_home().clean_sheet_probability > hard_away().clean_sheet_probability

    def test_a_strong_defensive_record_raises_clean_sheet_odds(self):
        mean_form = TeamForm(1, 20, LEAGUE_MEAN_GOALS, LEAGUE_MEAN_GOALS,
                             LEAGUE_MEAN_GOALS, LEAGUE_MEAN_GOALS)
        stingy = TeamForm(1, 20, LEAGUE_MEAN_GOALS, 0.5, LEAGUE_MEAN_GOALS, 0.5)
        average = expectation_for_fixture(1, 2, is_home=True, difficulty=3,
                                          team_form=mean_form, opponent_form=None)
        good = expectation_for_fixture(1, 2, is_home=True, difficulty=3,
                                       team_form=stingy, opponent_form=None)
        assert good.clean_sheet_probability > average.clean_sheet_probability

    def test_a_thin_sample_is_shrunk_toward_the_league_mean(self):
        """Three clean sheets in three games must not model a club as
        impossible to score against."""
        one_match = TeamForm(1, 1, 3.0, 0.0, 3.0, 0.0)
        many = TeamForm(1, 30, 3.0, 0.0, 3.0, 0.0)
        thin = expectation_for_fixture(1, 2, is_home=True, difficulty=3,
                                       team_form=one_match, opponent_form=None)
        thick = expectation_for_fixture(1, 2, is_home=True, difficulty=3,
                                        team_form=many, opponent_form=None)
        assert thin.expected_conceded > thick.expected_conceded

    def test_expected_goals_never_go_to_zero(self):
        """A zero would make a clean sheet certain, which nothing is."""
        perfect = TeamForm(1, 40, 4.0, 0.0, 4.0, 0.0)
        fixture = expectation_for_fixture(1, 2, is_home=True, difficulty=1,
                                          team_form=perfect, opponent_form=None)
        assert fixture.expected_conceded > 0
        assert fixture.clean_sheet_probability < 1.0


class TestBonusCurve:
    def test_bonus_rises_with_bps(self):
        values = [expected_bonus(bps) for bps in (10, 20, 30, 40, 50)]
        assert values == sorted(values)

    def test_a_low_bps_score_earns_almost_nothing(self):
        assert expected_bonus(5) < 0.1

    def test_a_high_bps_score_approaches_three(self):
        assert 2.0 < expected_bonus(50) <= 3.0

    def test_the_curve_is_capped(self):
        assert expected_bonus(500) == expected_bonus(50)


class TestConcessionPoints:
    def test_a_defender_expecting_few_goals_loses_little(self):
        assert expected_concession_points(Position.DEF, 0.5, DEFAULT_SCORING) > -0.15

    def test_a_defender_facing_a_barrage_loses_more(self):
        heavy = expected_concession_points(Position.DEF, 3.0, DEFAULT_SCORING)
        light = expected_concession_points(Position.DEF, 1.0, DEFAULT_SCORING)
        assert heavy < light < 0

    def test_a_midfielder_loses_nothing(self):
        assert expected_concession_points(Position.MID, 3.0, DEFAULT_SCORING) == 0.0

    def test_conceding_one_costs_nothing(self):
        """The deduction steps at every second goal, so the expectation is not
        simply half the expected goals."""
        assert expected_concession_points(Position.DEF, 1.0, DEFAULT_SCORING) > -0.30


class TestProjection:
    def test_an_easier_fixture_projects_more_points(self):
        rates = estimate_rates(history(position=Position.FWD, expected_goals=1.5))
        assert (
            project_fixture(rates, easy_home(), 4).total
            > project_fixture(rates, hard_away(), 4).total
        )

    def test_penalty_duty_raises_a_forwards_projection(self):
        rates = estimate_rates(history(position=Position.FWD, expected_goals=1.5))
        on = project_fixture(rates, easy_home(), 4, on_penalties=True)
        off = project_fixture(rates, easy_home(), 4, on_penalties=False)
        assert on.total > off.total

    def test_a_goalkeeper_earns_from_saves_and_a_defender_does_not(self):
        keeper = estimate_rates(history(position=Position.GKP, saves=12, price=55))
        defender = estimate_rates(history(position=Position.DEF, price=55))
        assert project_fixture(keeper, easy_home(), 4).saves > 0
        assert project_fixture(defender, easy_home(), 4).saves == 0

    def test_clean_sheet_points_go_mostly_to_defenders_and_keepers(self):
        defender = project_fixture(
            estimate_rates(history(position=Position.DEF)), easy_home(), 4
        )
        midfielder = project_fixture(
            estimate_rates(history(position=Position.MID)), easy_home(), 4
        )
        assert defender.clean_sheet > midfielder.clean_sheet

    def test_components_add_up_to_the_total(self):
        projection = project_fixture(estimate_rates(history()), easy_home(), 4)
        assert sum(projection.components().values()) == pytest.approx(projection.total)

    def test_a_defensive_midfielder_earns_defcon_points(self):
        holder = estimate_rates(history(position=Position.MID, defcon_hits=3))
        winger = estimate_rates(history(position=Position.MID, defcon_hits=0))
        assert (
            project_fixture(holder, easy_home(), 4).defensive_contribution
            > project_fixture(winger, easy_home(), 4).defensive_contribution
        )

    def test_goalkeepers_earn_no_defcon_points(self):
        """The scoring table gives goalkeepers zero for defensive contributions."""
        keeper = estimate_rates(history(position=Position.GKP, defcon_hits=3))
        assert project_fixture(keeper, easy_home(), 4).defensive_contribution == 0


class TestGameweekProjection:
    def test_a_blank_gameweek_projects_nothing(self):
        projection = project_gameweek(estimate_rates(history()), [], 4)
        assert projection.is_blank
        assert projection.expected_points == 0.0

    def test_a_double_gameweek_adds_both_fixtures(self):
        rates = estimate_rates(history())
        single = project_gameweek(rates, [easy_home()], 4)
        double = project_gameweek(rates, [easy_home(), hard_away()], 4)
        assert double.is_double
        assert double.expected_points > single.expected_points
        assert double.fixture_count == 2

    def test_a_double_gameweek_raises_the_chance_of_starting_at_least_once(self):
        rates = estimate_rates(history(recent_starts=2, recent_appearances=3))
        single = project_gameweek(rates, [easy_home()], 4)
        double = project_gameweek(rates, [easy_home(), easy_home()], 4)
        assert double.probability_of_starting > single.probability_of_starting

    def test_a_double_gameweek_raises_the_chance_of_a_clean_sheet(self):
        rates = estimate_rates(history(position=Position.DEF))
        single = project_gameweek(rates, [easy_home()], 4)
        double = project_gameweek(rates, [easy_home(), easy_home()], 4)
        assert double.clean_sheet_probability > single.clean_sheet_probability

    def test_probabilities_stay_within_range(self):
        rates = estimate_rates(history())
        projection = project_gameweek(rates, [easy_home()] * 3, 4)
        assert 0 <= projection.probability_of_starting <= 1
        assert 0 <= projection.clean_sheet_probability <= 1

    def test_components_are_summed_across_fixtures(self):
        rates = estimate_rates(history())
        projection = project_gameweek(rates, [easy_home(), easy_home()], 4)
        assert sum(projection.components().values()) == pytest.approx(
            projection.expected_points
        )
