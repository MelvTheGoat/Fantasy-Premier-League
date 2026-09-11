"""Per-player rates: minutes, attacking output, defensive contributions, bonus.

Everything here is a *rate per 90 minutes*, shrunk toward a positional prior.
Shrinkage matters more than usual this season: three gameweeks in, a midfielder
with one goal from 180 minutes has a raw rate of 0.5 goals per 90, which would
project him as the best player in the game. Pulling every rate toward what a
player of his position and price normally does keeps small samples honest, and
the pull weakens automatically as minutes accumulate.

Where a player has prior-season totals, those form the prior instead of the
positional average, so an established player is not treated as an unknown.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..rules.constants import Position

#: Minutes of evidence at which a player's own rate and the prior carry equal
#: weight. Roughly five full matches.
RATE_SHRINKAGE_MINUTES = 450.0

#: Minutes of prior-season evidence treated as equivalent to this season's.
#: Prior seasons count for less: squads, roles and managers change.
PRIOR_SEASON_WEIGHT = 0.45

#: The price at which each position's priors below apply. A player costing
#: more than this is expected to do more; one costing less, less.
REFERENCE_PRICE: dict[Position, int] = {
    Position.GKP: 45,
    Position.DEF: 45,
    Position.MID: 55,
    Position.FWD: 60,
}

#: How strongly price scales each prior. Price is FPL's own estimate of a
#: player's output, so a £13.0m forward should not be shrunk toward the same
#: goal rate as a £4.5m one -- that is the single biggest error available in
#: early season, when there is barely any evidence to shrink from. Attacking
#: returns scale steeply with price, defensive contributions barely at all.
PRICE_ELASTICITY = {"goals": 1.6, "assists": 1.1, "bps": 0.7, "saves": 0.3, "defcon": 0.0}

#: Bounds on the price adjustment, so a bargain or a record signing cannot
#: produce an absurd prior.
PRICE_FACTOR_BOUNDS = (0.35, 3.0)

#: League-average per-90 rates by position at `REFERENCE_PRICE`, used when
#: nothing else is known. Derived from typical Premier League seasons, not
#: fitted -- they exist to stop a three-match sample running away, not to be
#: precise.
POSITION_PRIORS: dict[Position, dict[str, float]] = {
    Position.GKP: {
        "goals": 0.002, "assists": 0.01, "saves": 3.1,
        "defcon": 0.0, "bps": 17.0, "yellow": 0.04,
    },
    Position.DEF: {
        "goals": 0.06, "assists": 0.07, "saves": 0.0,
        "defcon": 0.30, "bps": 17.0, "yellow": 0.14,
    },
    Position.MID: {
        "goals": 0.14, "assists": 0.13, "saves": 0.0,
        "defcon": 0.16, "bps": 16.0, "yellow": 0.13,
    },
    Position.FWD: {
        "goals": 0.32, "assists": 0.12, "saves": 0.0,
        "defcon": 0.04, "bps": 17.0, "yellow": 0.10,
    },
}

#: A start is worth this many expected minutes on average, allowing for early
#: substitutions and the occasional injury.
MINUTES_PER_START = 78.0

#: Minutes a player who appears without starting tends to get.
MINUTES_PER_CAMEO = 22.0

#: How many recent matches the start-probability estimate looks at. Short
#: enough to catch a player losing their place, long enough not to panic at one
#: rested match.
RECENT_MATCHES = 6

#: Strength of the prior on starting, in matches. One pseudo-match: enough to
#: stop a single appearance reading as nailed-on, weak enough that a player who
#: has started every match is modelled as a starter rather than a rotation
#: risk. Three starts from three lands at 0.84.
START_PRIOR_MATCHES = 1.0

#: Baseline start and appearance rates for a player with no record at all --
#: a squad player, not a starter.
BASELINE_START_RATE = 0.35
BASELINE_APPEARANCE_RATE = 0.55


@dataclass(frozen=True, slots=True)
class PlayerHistory:
    """A player's record so far, as of some gameweek.

    `recent_starts` and `recent_appearances` count only the last
    `RECENT_MATCHES` of their club's fixtures, so a player who has just broken
    into the side is not held back by earlier matches he was not in.
    """

    element: int
    position: Position
    minutes: int = 0
    starts: int = 0
    appearances: int = 0
    matches_available: int = 0
    recent_starts: int = 0
    recent_appearances: int = 0
    recent_matches: int = 0

    goals: float = 0.0
    assists: float = 0.0
    expected_goals: float = 0.0
    expected_assists: float = 0.0
    saves: int = 0
    defcon_hits: int = 0
    bps: int = 0
    yellow_cards: int = 0

    prior_minutes: int = 0
    prior_goals: float = 0.0
    prior_assists: float = 0.0
    prior_saves: int = 0
    prior_bps: int = 0

    #: Current price in tenths, used to scale the priors.
    price: int = 0

    #: From the API: 0-100, or None when the player is fully fit.
    chance_of_playing: int | None = None
    #: `a` available, `d` doubtful, `i` injured, `s` suspended, `u` unavailable.
    status: str = "a"


@dataclass(frozen=True, slots=True)
class PlayerRates:
    """Shrunk per-90 rates and the minutes model for one player."""

    element: int
    position: Position
    probability_of_appearing: float
    probability_of_starting: float
    expected_minutes: float
    goals_per_90: float
    assists_per_90: float
    saves_per_90: float
    defcon_per_90: float
    bps_per_90: float
    yellow_per_90: float

    @property
    def probability_of_60_minutes(self) -> float:
        """Chance of reaching the 60-minute mark, where appearance points and
        clean-sheet points both step up.

        A starter usually gets there; a substitute rarely does.
        """
        cameo = max(self.probability_of_appearing - self.probability_of_starting, 0.0)
        return self.probability_of_starting * 0.86 + cameo * 0.08


def price_factor(position: Position, price: int, metric: str) -> float:
    """How much a player's price moves the prior for one metric.

    Returns 1.0 when the price is unknown, so a missing price degrades to the
    positional average rather than to zero.
    """
    reference = REFERENCE_PRICE[position]
    elasticity = PRICE_ELASTICITY.get(metric, 0.0)
    if not price or not elasticity:
        return 1.0
    low, high = PRICE_FACTOR_BOUNDS
    return max(low, min(high, (price / reference) ** elasticity))


def _shrink(
    observed_total: float,
    minutes: float,
    prior_per_90: float,
    *,
    prior_total: float = 0.0,
    prior_minutes: float = 0.0,
) -> float:
    """Blend a player's own per-90 rate with a prior, by minutes played.

    Prior-season minutes are discounted by `PRIOR_SEASON_WEIGHT` before being
    pooled, so last season informs the estimate without overwhelming evidence
    from this one.
    """
    effective_prior_minutes = prior_minutes * PRIOR_SEASON_WEIGHT
    pooled_minutes = minutes + effective_prior_minutes
    pooled_total = observed_total + prior_total * PRIOR_SEASON_WEIGHT

    if pooled_minutes <= 0:
        return prior_per_90

    own_rate = pooled_total / pooled_minutes * 90.0
    weight = pooled_minutes / (pooled_minutes + RATE_SHRINKAGE_MINUTES)
    return weight * own_rate + (1 - weight) * prior_per_90


def availability_multiplier(history: PlayerHistory) -> float:
    """How much a player's injury or suspension flag suppresses their minutes.

    `chance_of_playing` is the API's own number and is trusted where present.
    Status codes are the fallback: suspended and unavailable are zero, because
    the player cannot feature at all.
    """
    if history.status in ("s", "u"):
        return 0.0
    if history.status == "n":  # not in the squad, e.g. out on loan
        return 0.0
    if history.chance_of_playing is not None:
        return max(0.0, min(1.0, history.chance_of_playing / 100.0))
    if history.status == "i":
        return 0.0
    if history.status == "d":
        return 0.5
    return 1.0


def estimate_rates(history: PlayerHistory) -> PlayerRates:
    """Turn a player's record into the rates the points model needs."""
    base = POSITION_PRIORS[history.position]
    priors = {
        metric: value * price_factor(history.position, history.price, metric)
        for metric, value in base.items()
    }

    if history.recent_matches:
        raw_start = history.recent_starts / history.recent_matches
        raw_appear = history.recent_appearances / history.recent_matches
    elif history.matches_available:
        raw_start = history.starts / history.matches_available
        raw_appear = history.appearances / history.matches_available
    else:
        # Nothing to go on: assume a squad player rather than a starter.
        raw_start, raw_appear = BASELINE_START_RATE, BASELINE_APPEARANCE_RATE

    # A player with no minutes at all is a genuine unknown, not a confirmed
    # non-starter, so the estimate is pulled toward a squad-player baseline.
    sample = history.recent_matches or history.matches_available
    confidence = sample / (sample + START_PRIOR_MATCHES) if sample else 0.0
    start_probability = confidence * raw_start + (1 - confidence) * BASELINE_START_RATE
    appear_probability = (
        confidence * raw_appear + (1 - confidence) * BASELINE_APPEARANCE_RATE
    )
    appear_probability = max(appear_probability, start_probability)

    available = availability_multiplier(history)
    start_probability *= available
    appear_probability *= available

    cameo_probability = max(appear_probability - start_probability, 0.0)
    expected_minutes = (
        start_probability * MINUTES_PER_START + cameo_probability * MINUTES_PER_CAMEO
    )

    minutes = float(history.minutes)

    # Expected goals and assists are better predictors than the goals and
    # assists themselves, so they lead where the API provides them.
    goal_signal = history.expected_goals or history.goals
    assist_signal = history.expected_assists or history.assists

    return PlayerRates(
        element=history.element,
        position=history.position,
        probability_of_appearing=min(appear_probability, 1.0),
        probability_of_starting=min(start_probability, 1.0),
        expected_minutes=expected_minutes,
        goals_per_90=_shrink(
            goal_signal, minutes, priors["goals"],
            prior_total=history.prior_goals, prior_minutes=history.prior_minutes,
        ),
        assists_per_90=_shrink(
            assist_signal, minutes, priors["assists"],
            prior_total=history.prior_assists, prior_minutes=history.prior_minutes,
        ),
        saves_per_90=_shrink(
            history.saves, minutes, priors["saves"],
            prior_total=history.prior_saves, prior_minutes=history.prior_minutes,
        ),
        defcon_per_90=_shrink(history.defcon_hits, minutes, priors["defcon"]),
        bps_per_90=_shrink(
            history.bps, minutes, priors["bps"],
            prior_total=history.prior_bps, prior_minutes=history.prior_minutes,
        ),
        yellow_per_90=_shrink(history.yellow_cards, minutes, priors["yellow"]),
    )


__all__ = [
    "MINUTES_PER_CAMEO",
    "PRICE_ELASTICITY",
    "PRICE_FACTOR_BOUNDS",
    "REFERENCE_PRICE",
    "price_factor",
    "MINUTES_PER_START",
    "POSITION_PRIORS",
    "PRIOR_SEASON_WEIGHT",
    "PlayerHistory",
    "PlayerRates",
    "RATE_SHRINKAGE_MINUTES",
    "RECENT_MATCHES",
    "availability_multiplier",
    "estimate_rates",
]
