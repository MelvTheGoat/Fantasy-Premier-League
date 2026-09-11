"""Expected points for one player in one fixture, and over a horizon.

The projection is built from components rather than fitted end-to-end, because
three gameweeks of a new season is nowhere near enough data to fit a model that
would beat a well-specified one. Each component is separately inspectable,
which is also what lets the frontend explain a transfer in terms a person
recognises -- "two home fixtures, on penalties, 0.6 xG per 90" -- instead of a
single opaque number.

Points come from the API's own scoring table (`ScoringTable`), so a change like
goalkeeper goals going from 6 to 10 flows through without a code change.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import exp

from ..rules.constants import Position
from ..rules.scoring_table import DEFAULT_SCORING, ScoringTable
from .player_rates import PlayerRates
from .team_strength import FixtureExpectation

#: Share of a team's goals that a player on penalties can expect on top of
#: their open-play rate, per penalty won. Applied only to designated takers.
PENALTY_CONVERSION = 0.79

#: Penalties won per match by an average Premier League side.
PENALTIES_PER_MATCH = 0.13

#: Maps expected BPS in a match to expected bonus points. BPS is only
#: meaningful relative to the other 21 players, so this is a calibration curve
#: rather than a rule: roughly, 20 BPS is a plausible bonus score, 30 usually
#: places, and 40 usually wins the three.
BONUS_CURVE = (
    (18.0, 0.10),
    (24.0, 0.45),
    (30.0, 1.05),
    (36.0, 1.85),
    (42.0, 2.45),
    (50.0, 2.80),
)


@dataclass(frozen=True, slots=True)
class PointsBreakdown:
    """Expected points split by source, for one player in one fixture.

    Every field is an expectation, not a realisation, so they are floats and
    they add up to `total`.
    """

    element: int
    gameweek: int
    appearance: float = 0.0
    goals: float = 0.0
    assists: float = 0.0
    clean_sheet: float = 0.0
    goals_conceded: float = 0.0
    saves: float = 0.0
    defensive_contribution: float = 0.0
    bonus: float = 0.0
    cards: float = 0.0

    expected_minutes: float = 0.0
    probability_of_starting: float = 0.0
    clean_sheet_probability: float = 0.0
    defcon_probability: float = 0.0
    opponent: int | None = None
    is_home: bool = True
    difficulty: int = 3

    @property
    def total(self) -> float:
        return (
            self.appearance
            + self.goals
            + self.assists
            + self.clean_sheet
            + self.goals_conceded
            + self.saves
            + self.defensive_contribution
            + self.bonus
            + self.cards
        )

    @property
    def attacking(self) -> float:
        return self.goals + self.assists

    def components(self) -> dict[str, float]:
        """Named components, for the frontend's explanation panel."""
        return {
            "appearance": self.appearance,
            "goals": self.goals,
            "assists": self.assists,
            "clean sheet": self.clean_sheet,
            "goals conceded": self.goals_conceded,
            "saves": self.saves,
            "defensive contribution": self.defensive_contribution,
            "bonus": self.bonus,
            "cards": self.cards,
        }


def expected_bonus(bps_in_match: float) -> float:
    """Expected bonus points from an expected BPS score, by interpolation."""
    if bps_in_match <= BONUS_CURVE[0][0]:
        # Below the bottom of the curve, fade linearly to zero rather than
        # jumping: a player on 9 BPS is not half as likely as one on 18.
        floor_bps, floor_bonus = BONUS_CURVE[0]
        return max(bps_in_match / floor_bps, 0.0) * floor_bonus

    for (low_bps, low_bonus), (high_bps, high_bonus) in zip(
        BONUS_CURVE, BONUS_CURVE[1:], strict=False
    ):
        if bps_in_match <= high_bps:
            span = high_bps - low_bps
            position = (bps_in_match - low_bps) / span
            return low_bonus + position * (high_bonus - low_bonus)

    return BONUS_CURVE[-1][1]


def expected_concession_points(
    position: Position, expected_conceded: float, scoring: ScoringTable
) -> float:
    """Expected deduction for goals conceded, summed over a Poisson.

    FPL deducts one point per two goals conceded, so the expectation is not
    simply half the expected goals: conceding one costs nothing, and the
    deduction steps at every second goal.
    """
    per_deduction = scoring.goals_conceded_per_deduction
    points = scoring.goals_conceded[position]
    if not points:
        return 0.0

    # Sum over plausible scorelines; beyond eight the probability mass is
    # negligible and the tail contributes nothing meaningful.
    total = 0.0
    probability = exp(-expected_conceded)
    for goals in range(0, 9):
        if goals:
            probability *= expected_conceded / goals
        total += probability * (goals // per_deduction) * points
    return total


def project_fixture(
    rates: PlayerRates,
    fixture: FixtureExpectation,
    gameweek: int,
    *,
    scoring: ScoringTable = DEFAULT_SCORING,
    on_penalties: bool = False,
) -> PointsBreakdown:
    """Expected points for one player in one fixture."""
    position = rates.position
    minutes_share = rates.expected_minutes / 90.0
    p60 = rates.probability_of_60_minutes

    appearance = (
        p60 * scoring.long_play
        + max(rates.probability_of_appearing - p60, 0.0) * scoring.short_play
    )

    # Attacking output scales with minutes and with how much the team is
    # expected to score relative to an average match.
    attack_scale = fixture.expected_scored / 1.45
    goals = rates.goals_per_90 * minutes_share * attack_scale
    if on_penalties:
        goals += PENALTIES_PER_MATCH * PENALTY_CONVERSION * attack_scale * minutes_share
    assists = rates.assists_per_90 * minutes_share * attack_scale

    # Clean sheets only count for a player who reaches 60 minutes.
    clean_sheet_probability = fixture.clean_sheet_probability
    clean_sheet = (
        clean_sheet_probability * p60 * scoring.clean_sheet_points(position)
    )

    conceded = expected_concession_points(position, fixture.expected_conceded, scoring)
    conceded *= p60

    saves = 0.0
    if position is Position.GKP:
        # Saves scale with what the opposition is expected to create.
        expected_saves = (
            rates.saves_per_90 * minutes_share * (fixture.expected_conceded / 1.45)
        )
        saves = expected_saves / scoring.saves_per_point * scoring.saves

    defcon_probability = min(rates.defcon_per_90 * minutes_share, 1.0)
    defensive_contribution = defcon_probability * scoring.defcon_points(position)

    # Bonus is conditioned on the player actually featuring rather than
    # applying the curve to a minutes-diluted BPS figure: the curve is
    # non-linear, so E[bonus(BPS)] is not bonus(E[BPS]), and diluting first
    # systematically understates every rotation risk's bonus.
    bonus = p60 * expected_bonus(rates.bps_per_90) * scoring.bonus
    cards = rates.yellow_per_90 * minutes_share * scoring.yellow_cards

    return PointsBreakdown(
        element=rates.element,
        gameweek=gameweek,
        appearance=appearance,
        goals=goals * scoring.goal_points(position),
        assists=assists * scoring.assists,
        clean_sheet=clean_sheet,
        goals_conceded=conceded,
        saves=saves,
        defensive_contribution=defensive_contribution,
        bonus=bonus,
        cards=cards,
        expected_minutes=rates.expected_minutes,
        probability_of_starting=rates.probability_of_starting,
        clean_sheet_probability=clean_sheet_probability,
        defcon_probability=defcon_probability,
        opponent=fixture.opponent,
        is_home=fixture.is_home,
        difficulty=fixture.difficulty,
    )


@dataclass(frozen=True, slots=True)
class GameweekProjection:
    """A player's projection for a whole gameweek, across all their fixtures.

    A blank gameweek has no fixtures and projects zero. A double gameweek has
    two, and their expectations add -- which is exactly why Bench Boost and
    Triple Captain are worth playing on one.
    """

    element: int
    gameweek: int
    fixtures: tuple[PointsBreakdown, ...]

    @property
    def expected_points(self) -> float:
        return sum(f.total for f in self.fixtures)

    @property
    def fixture_count(self) -> int:
        return len(self.fixtures)

    @property
    def is_blank(self) -> bool:
        return not self.fixtures

    @property
    def is_double(self) -> bool:
        return len(self.fixtures) > 1

    @property
    def expected_minutes(self) -> float:
        return sum(f.expected_minutes for f in self.fixtures)

    @property
    def probability_of_starting(self) -> float:
        """Chance of starting at least one of the gameweek's fixtures."""
        missing = 1.0
        for fixture in self.fixtures:
            missing *= 1 - fixture.probability_of_starting
        return 1 - missing

    @property
    def clean_sheet_probability(self) -> float:
        """Chance of at least one clean sheet across the gameweek."""
        missing = 1.0
        for fixture in self.fixtures:
            missing *= 1 - fixture.clean_sheet_probability
        return 1 - missing

    @property
    def defcon_probability(self) -> float:
        missing = 1.0
        for fixture in self.fixtures:
            missing *= 1 - fixture.defcon_probability
        return 1 - missing

    def components(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for fixture in self.fixtures:
            for name, value in fixture.components().items():
                totals[name] = totals.get(name, 0.0) + value
        return totals


def project_gameweek(
    rates: PlayerRates,
    fixtures: list[FixtureExpectation],
    gameweek: int,
    *,
    scoring: ScoringTable = DEFAULT_SCORING,
    on_penalties: bool = False,
) -> GameweekProjection:
    """Project a player across every fixture their club has in a gameweek."""
    return GameweekProjection(
        element=rates.element,
        gameweek=gameweek,
        fixtures=tuple(
            project_fixture(
                rates, fixture, gameweek, scoring=scoring, on_penalties=on_penalties
            )
            for fixture in fixtures
        ),
    )


__all__ = [
    "BONUS_CURVE",
    "GameweekProjection",
    "PENALTIES_PER_MATCH",
    "PENALTY_CONVERSION",
    "PointsBreakdown",
    "expected_bonus",
    "expected_concession_points",
    "project_fixture",
    "project_gameweek",
]
