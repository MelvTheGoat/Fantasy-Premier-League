"""Assembling a gameweek score from real FPL points.

Player points are never recalculated here -- they come straight from
``event/{gw}/live/``. What this module does is apply the *manager-level* rules
on top of them: automatic substitutions, the captain's multiplier, whether the
bench counts, and the cost of any hits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .autosubs import apply_auto_subs
from .captaincy import CaptaincyOutcome, resolve_captaincy
from .constants import Chip, LOCKDOWN_HOUR_UK, LOCKDOWN_TIMEZONE
from .types import Lineup, Results, Roster, Substitution


@dataclass(frozen=True, slots=True)
class PlayerScore:
    """One player's contribution to the gameweek total."""

    element: int
    points: int
    """Raw FPL points for the gameweek, before any multiplier."""
    multiplier: int
    """0 for a bench player who does not count, 1 normally, 2 or 3 for the captain."""
    minutes: int
    started: bool
    """Whether they were in the XI *after* automatic substitutions."""
    subbed_on: bool = False
    subbed_off: bool = False
    is_captain: bool = False
    is_vice_captain: bool = False

    @property
    def total(self) -> int:
        return self.points * self.multiplier


@dataclass(frozen=True, slots=True)
class GameweekScore:
    """A fully resolved gameweek for one model."""

    gameweek: int
    lineup: Lineup
    """The lineup after automatic substitutions."""
    submitted_lineup: Lineup
    """The lineup as it was locked at the deadline."""
    scores: tuple[PlayerScore, ...]
    substitutions: tuple[Substitution, ...]
    captaincy: CaptaincyOutcome
    chip: Chip | None
    points_before_hits: int
    transfer_cost: int
    final: bool
    """False while the gameweek is still provisional, i.e. before lockdown."""

    @property
    def points(self) -> int:
        """Net gameweek score, which is the number FPL shows."""
        return self.points_before_hits - self.transfer_cost

    @property
    def bench_points(self) -> int:
        bench = set(self.lineup.bench)
        return sum(s.points for s in self.scores if s.element in bench)

    @property
    def captain_points(self) -> int:
        return next((s.total for s in self.scores if s.is_captain), 0)

    def score_for(self, element: int) -> PlayerScore:
        for score in self.scores:
            if score.element == element:
                return score
        raise KeyError(f"element {element} did not feature in GW{self.gameweek}")

    @property
    def status_label(self) -> str:
        return "Final" if self.final else "Provisional"


def score_gameweek(
    gameweek: int,
    lineup: Lineup,
    roster: Roster,
    results: Results,
    *,
    chip: Chip | None = None,
    transfer_cost: int = 0,
    final: bool = True,
) -> GameweekScore:
    """Score one gameweek for one squad.

    Order matters and mirrors FPL's own: substitutions resolve first, then the
    armband is settled against who actually played, then multipliers are laid
    over the raw points.
    """
    resolved, substitutions = apply_auto_subs(lineup, roster, results)
    captaincy = resolve_captaincy(resolved, results, chip)
    bench_boost = chip is Chip.BENCH_BOOST

    subbed_on = {s.in_element for s in substitutions}
    subbed_off = {s.out_element for s in substitutions}

    scores: list[PlayerScore] = []
    for element in resolved.elements:
        result = results.get(element)
        points = result.total_points if result else 0
        minutes = result.minutes if result else 0
        started = element in resolved.starters

        if started:
            multiplier = 1
        elif bench_boost:
            multiplier = 1
        else:
            multiplier = 0

        if captaincy.element == element and multiplier > 0:
            multiplier = captaincy.multiplier

        scores.append(
            PlayerScore(
                element=element,
                points=points,
                multiplier=multiplier,
                minutes=minutes,
                started=started,
                subbed_on=element in subbed_on,
                subbed_off=element in subbed_off,
                is_captain=captaincy.element == element,
                is_vice_captain=element == resolved.vice_captain,
            )
        )

    points_before_hits = sum(s.total for s in scores)

    return GameweekScore(
        gameweek=gameweek,
        lineup=resolved,
        submitted_lineup=lineup,
        scores=tuple(scores),
        substitutions=tuple(substitutions),
        captaincy=captaincy,
        chip=chip,
        points_before_hits=points_before_hits,
        transfer_cost=transfer_cost,
        final=final,
    )


def lockdown_time(last_kickoff: datetime) -> datetime:
    """When a gameweek's scores stop being provisional.

    09:00 UK time on the day after the gameweek's final match. `last_kickoff`
    is converted to UK time first, so a late kickoff that runs past midnight
    UTC still resolves against the correct local day.
    """
    uk = ZoneInfo(LOCKDOWN_TIMEZONE)
    local = last_kickoff.astimezone(uk)
    next_day = (local + timedelta(days=1)).date()
    return datetime(
        next_day.year, next_day.month, next_day.day, LOCKDOWN_HOUR_UK, tzinfo=uk
    )


def is_final(last_kickoff: datetime | None, now: datetime) -> bool:
    """Whether a gameweek has passed lockdown and its points are final."""
    if last_kickoff is None:
        return False
    return now >= lockdown_time(last_kickoff)


@dataclass(slots=True)
class SeasonSummary:
    """Running totals for one model across the season."""

    total_points: int = 0
    gameweeks_played: int = 0
    gameweeks_beating_average: int = 0
    total_transfer_cost: int = 0
    cumulative_points: list[int] = field(default_factory=list)
    cumulative_average: list[float] = field(default_factory=list)

    @property
    def beat_average_rate(self) -> float:
        if not self.gameweeks_played:
            return 0.0
        return self.gameweeks_beating_average / self.gameweeks_played


def summarise_season(
    scores: list[GameweekScore],
    averages: dict[int, float],
) -> SeasonSummary:
    """Fold gameweek scores into a season summary against the official averages.

    `averages` is the FPL API's ``average_entry_score`` per gameweek. Gameweeks
    with no recorded average are still counted towards the points total but not
    towards the beat-the-average tally, since there is nothing to compare to.
    """
    summary = SeasonSummary()
    running_points = 0
    running_average = 0.0

    for score in sorted(scores, key=lambda s: s.gameweek):
        running_points += score.points
        summary.total_points = running_points
        summary.gameweeks_played += 1
        summary.total_transfer_cost += score.transfer_cost

        average = averages.get(score.gameweek)
        if average is not None:
            running_average += average
            if score.points > average:
                summary.gameweeks_beating_average += 1

        summary.cumulative_points.append(running_points)
        summary.cumulative_average.append(round(running_average, 1))

    return summary


__all__ = [
    "GameweekScore",
    "PlayerScore",
    "SeasonSummary",
    "is_final",
    "lockdown_time",
    "score_gameweek",
    "summarise_season",
]
