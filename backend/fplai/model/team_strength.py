"""Attacking and defensive strength per club, per fixture.

FPL stopped publishing `strength_attack_*` and `strength_defence_*` for
2026/27 -- they are zero for every club -- so strength has to be derived. Two
signals are available:

* **Fixture difficulty** (1-5), which FPL sets per fixture per side. It is
  available from GW1 and encodes the bookmakers' view, so it carries the early
  season when nothing has been played.
* **Actual results**: goals scored and conceded, and their expected-goals
  equivalents, which become the better signal as matches accumulate.

The two are blended by how much has actually been played, so GW2 leans almost
entirely on difficulty and GW30 leans almost entirely on results. Both are
shrunk toward the league average, which is what stops a club that kept three
clean sheets in three games from being modelled as impossible to score against.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

#: League-average goals per team per match. The Premier League has sat close to
#: this for years, and it is the mean that thin early-season samples are pulled
#: toward.
LEAGUE_MEAN_GOALS = 1.45

#: Matches of evidence needed before results and fixture difficulty carry equal
#: weight. Below it, difficulty dominates; above it, results do.
BLEND_HALF_LIFE_MATCHES = 8.0

#: Strength of the prior, in matches. A club's rate is shrunk as though it had
#: already played this many matches at the league average, so three clean
#: sheets in three games moves the estimate without dominating it.
SHRINKAGE_MATCHES = 6.0

#: Multiplier applied to expected goals at home and away. The Premier League's
#: home advantage has been worth roughly this much per match.
HOME_ADVANTAGE = 1.10
AWAY_DISADVANTAGE = 0.92

#: Expected goals conceded implied by each fixture difficulty rating, for the
#: side facing it. Difficulty 1 is the easiest fixture and 5 the hardest, so
#: the harder the fixture the more a side is expected to concede.
DIFFICULTY_GOALS_CONCEDED = {1: 0.85, 2: 1.15, 3: 1.45, 4: 1.80, 5: 2.20}

#: Expected goals scored implied by each difficulty rating, for the side facing
#: it -- the mirror of the table above.
DIFFICULTY_GOALS_SCORED = {1: 2.10, 2: 1.75, 3: 1.45, 4: 1.15, 5: 0.90}


@dataclass(frozen=True, slots=True)
class TeamForm:
    """What a club has actually done so far, per match played."""

    team: int
    matches: int
    goals_scored_per_match: float
    goals_conceded_per_match: float
    expected_scored_per_match: float
    expected_conceded_per_match: float


@dataclass(frozen=True, slots=True)
class FixtureExpectation:
    """Expected goals for and against, for one club in one fixture."""

    team: int
    opponent: int
    is_home: bool
    difficulty: int
    expected_scored: float
    expected_conceded: float

    @property
    def clean_sheet_probability(self) -> float:
        """Probability the club concedes nothing, under a Poisson goal model.

        Football scorelines are close enough to Poisson for this purpose, and
        the alternative -- a bivariate model fitted on three gameweeks -- would
        be false precision.
        """
        from math import exp

        return exp(-self.expected_conceded)


def _shrink(observed: float, matches: int, prior: float) -> float:
    """Pull a per-match rate toward the league mean by how little it rests on."""
    weight = matches / (matches + SHRINKAGE_MATCHES)
    return weight * observed + (1 - weight) * prior


def _results_weight(matches: int) -> float:
    """How far to trust results over fixture difficulty, from 0 to just under 1."""
    return matches / (matches + BLEND_HALF_LIFE_MATCHES)


def load_team_form(
    connection: sqlite3.Connection,
    before_gameweek: int,
) -> dict[int, TeamForm]:
    """Each club's scoring and conceding rate from finished matches.

    `before_gameweek` is exclusive: a projection made for GW7 may only see
    GW1-6, which is what keeps the backfill honest.
    """
    form: dict[int, TeamForm] = {}
    tallies: dict[int, dict[str, float]] = {}

    for row in connection.execute(
        "SELECT team_h, team_a, team_h_score, team_a_score FROM fixtures"
        " WHERE gameweek IS NOT NULL AND gameweek < ? AND finished = 1"
        "   AND team_h_score IS NOT NULL AND team_a_score IS NOT NULL",
        (before_gameweek,),
    ):
        for team, scored, conceded in (
            (row["team_h"], row["team_h_score"], row["team_a_score"]),
            (row["team_a"], row["team_a_score"], row["team_h_score"]),
        ):
            tally = tallies.setdefault(
                team, {"matches": 0, "scored": 0.0, "conceded": 0.0}
            )
            tally["matches"] += 1
            tally["scored"] += scored
            tally["conceded"] += conceded

    # Expected goals conceded is summed from the players' own live rows, which
    # is the only place the API exposes it per match.
    expected: dict[int, dict[str, float]] = {}
    for row in connection.execute(
        "SELECT p.team_id AS team,"
        "       SUM(s.expected_goals) AS xg,"
        "       SUM(s.expected_goals_conceded) AS xgc,"
        "       COUNT(DISTINCT s.fixture_id) AS fixtures"
        " FROM player_gameweek_stats s"
        " JOIN players p ON p.id = s.player_id"
        " WHERE s.gameweek < ?"
        " GROUP BY p.team_id",
        (before_gameweek,),
    ):
        expected[row["team"]] = {
            "xg": row["xg"] or 0.0,
            # Every player of a club carries the same team-level figure for a
            # match, so the sum is divided by the squad rows behind it.
            "xgc": row["xgc"] or 0.0,
            "fixtures": row["fixtures"] or 0,
        }

    for team, tally in tallies.items():
        matches = int(tally["matches"])
        if not matches:
            continue
        xg = expected.get(team, {})
        form[team] = TeamForm(
            team=team,
            matches=matches,
            goals_scored_per_match=tally["scored"] / matches,
            goals_conceded_per_match=tally["conceded"] / matches,
            expected_scored_per_match=(xg.get("xg", 0.0) / matches) if matches else 0.0,
            expected_conceded_per_match=(xg.get("xgc", 0.0) / matches) if matches else 0.0,
        )

    return form


def expectation_for_fixture(
    team: int,
    opponent: int,
    *,
    is_home: bool,
    difficulty: int,
    team_form: TeamForm | None,
    opponent_form: TeamForm | None,
) -> FixtureExpectation:
    """Expected goals for and against for one club in one fixture.

    Difficulty sets the baseline; the two clubs' own rates adjust it, weighted
    by how many matches those rates rest on.
    """
    difficulty = max(1, min(5, difficulty or 3))
    base_scored = DIFFICULTY_GOALS_SCORED[difficulty]
    base_conceded = DIFFICULTY_GOALS_CONCEDED[difficulty]

    matches = team_form.matches if team_form else 0
    weight = _results_weight(matches)

    if team_form:
        # Prefer expected goals to actual goals where they exist: xG settles
        # faster than scorelines do.
        scored_rate = team_form.expected_scored_per_match or team_form.goals_scored_per_match
        conceded_rate = (
            team_form.expected_conceded_per_match or team_form.goals_conceded_per_match
        )
        scored = _shrink(scored_rate, matches, LEAGUE_MEAN_GOALS)
        conceded = _shrink(conceded_rate, matches, LEAGUE_MEAN_GOALS)
    else:
        scored = conceded = LEAGUE_MEAN_GOALS

    expected_scored = weight * scored + (1 - weight) * base_scored
    expected_conceded = weight * conceded + (1 - weight) * base_conceded

    # The opponent's own record adjusts the other half of the fixture.
    if opponent_form and opponent_form.matches:
        opponent_weight = _results_weight(opponent_form.matches) * 0.5
        opponent_defence = _shrink(
            opponent_form.expected_conceded_per_match
            or opponent_form.goals_conceded_per_match,
            opponent_form.matches,
            LEAGUE_MEAN_GOALS,
        )
        opponent_attack = _shrink(
            opponent_form.expected_scored_per_match
            or opponent_form.goals_scored_per_match,
            opponent_form.matches,
            LEAGUE_MEAN_GOALS,
        )
        expected_scored = (
            (1 - opponent_weight) * expected_scored
            + opponent_weight * expected_scored * (opponent_defence / LEAGUE_MEAN_GOALS)
        )
        expected_conceded = (
            (1 - opponent_weight) * expected_conceded
            + opponent_weight * expected_conceded * (opponent_attack / LEAGUE_MEAN_GOALS)
        )

    venue = HOME_ADVANTAGE if is_home else AWAY_DISADVANTAGE
    expected_scored *= venue
    expected_conceded /= venue

    return FixtureExpectation(
        team=team,
        opponent=opponent,
        is_home=is_home,
        difficulty=difficulty,
        expected_scored=max(expected_scored, 0.05),
        expected_conceded=max(expected_conceded, 0.05),
    )


__all__ = [
    "AWAY_DISADVANTAGE",
    "BLEND_HALF_LIFE_MATCHES",
    "DIFFICULTY_GOALS_CONCEDED",
    "DIFFICULTY_GOALS_SCORED",
    "FixtureExpectation",
    "HOME_ADVANTAGE",
    "LEAGUE_MEAN_GOALS",
    "SHRINKAGE_MATCHES",
    "TeamForm",
    "expectation_for_fixture",
    "load_team_form",
]
