"""Chip availability and constraints.

2026/27 runs two full sets of chips. Every manager gets a Wildcard, Free Hit,
Bench Boost and Triple Captain for the first half of the season, all of which
expire unused at the GW19 deadline, and a fresh set of the same four from GW20.
Only one chip may be played in any gameweek.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .constants import (
    CHIPS_PER_SET,
    FIRST_CHIP_SET_LAST_GAMEWEEK,
    SECOND_CHIP_SET_FIRST_GAMEWEEK,
    TEAM_CHIP_FIRST_GAMEWEEK,
    TRANSFER_CHIP_FIRST_GAMEWEEK,
    TRANSFER_CHIPS,
    Chip,
)
from .types import ChipUsage, ValidationResult


@dataclass(frozen=True, slots=True)
class ChipWindow:
    """The gameweeks one chip from one set may be played in.

    The API publishes these on the `chips` block of `bootstrap-static/` as
    `start_event` and `stop_event`, so `chip_windows_from_bootstrap` can
    replace these defaults with whatever the game currently says.
    """

    chip: Chip
    chip_set: int
    start_gameweek: int
    stop_gameweek: int

    def covers(self, gameweek: int) -> bool:
        return self.start_gameweek <= gameweek <= self.stop_gameweek


def default_chip_windows(final_gameweek: int = 38) -> tuple[ChipWindow, ...]:
    """The 2026/27 windows: two sets, split at GW19/GW20.

    Transfer chips open at GW2 rather than GW1, because transfers before the
    opening deadline are unlimited anyway and there is nothing to buy.
    """
    windows = []
    for chip in sorted(CHIPS_PER_SET):
        chip = Chip(chip)
        first = (
            TRANSFER_CHIP_FIRST_GAMEWEEK
            if chip in TRANSFER_CHIPS
            else TEAM_CHIP_FIRST_GAMEWEEK
        )
        windows.append(ChipWindow(chip, 1, first, FIRST_CHIP_SET_LAST_GAMEWEEK))
        windows.append(
            ChipWindow(chip, 2, SECOND_CHIP_SET_FIRST_GAMEWEEK, final_gameweek)
        )
    return tuple(windows)


def chip_windows_from_bootstrap(bootstrap: dict) -> tuple[ChipWindow, ...]:
    """Read the windows straight from `bootstrap-static/`.

    Preferred over `default_chip_windows` wherever the payload is to hand: if
    the game moves a chip's window mid-season, this follows it. Chips the rules
    engine does not model are ignored rather than rejected, so a new chip type
    cannot break chip validation.
    """
    known = {str(c) for c in CHIPS_PER_SET}
    windows = []
    for entry in bootstrap.get("chips") or []:
        name = entry.get("name")
        start, stop = entry.get("start_event"), entry.get("stop_event")
        if name not in known or start is None or stop is None:
            continue
        chip_set = 1 if stop <= FIRST_CHIP_SET_LAST_GAMEWEEK else 2
        windows.append(ChipWindow(Chip(name), chip_set, start, stop))
    return tuple(windows)


def chip_set_for_gameweek(gameweek: int) -> int:
    """Which of the two chip sets a gameweek draws from: 1 or 2."""
    if gameweek < 1:
        raise ValueError(f"gameweek must be 1 or greater, got {gameweek}")
    return 1 if gameweek <= FIRST_CHIP_SET_LAST_GAMEWEEK else 2


def gameweeks_in_set(chip_set: int, final_gameweek: int = 38) -> range:
    if chip_set == 1:
        return range(1, FIRST_CHIP_SET_LAST_GAMEWEEK + 1)
    if chip_set == 2:
        return range(SECOND_CHIP_SET_FIRST_GAMEWEEK, final_gameweek + 1)
    raise ValueError(f"there are only two chip sets, got {chip_set}")


def available_chips(
    gameweek: int,
    used: Sequence[ChipUsage],
    windows: Sequence[ChipWindow] | None = None,
) -> set[Chip]:
    """Chips that may still be played in this gameweek.

    Only usage from the same set counts: a Wildcard played in GW8 does not stop
    the second-set Wildcard being played in GW25. A chip whose window has not
    opened is excluded -- in GW1 that means Wildcard and Free Hit, which the
    game does not offer until GW2.
    """
    if any(u.gameweek == gameweek for u in used):
        # A chip is already down for this gameweek, and only one is allowed.
        return set()

    current_set = chip_set_for_gameweek(gameweek)
    spent = {u.chip for u in used if chip_set_for_gameweek(u.gameweek) == current_set}
    windows = windows if windows is not None else default_chip_windows()

    return {
        window.chip
        for window in windows
        if window.chip_set == current_set
        and window.covers(gameweek)
        and window.chip not in spent
    }


def expiring_chips(gameweek: int, used: Sequence[ChipUsage]) -> set[Chip]:
    """First-set chips that will be lost if they are not played by GW19.

    Returns an empty set once the first set has expired, since the second set
    runs to the end of the season and cannot expire early.
    """
    if gameweek > FIRST_CHIP_SET_LAST_GAMEWEEK:
        return set()
    spent = {u.chip for u in used if chip_set_for_gameweek(u.gameweek) == 1}
    return {Chip(c) for c in CHIPS_PER_SET} - spent


def gameweeks_left_in_set(gameweek: int, final_gameweek: int = 38) -> int:
    """Gameweeks remaining in which the current set's chips can still be played."""
    return len([gw for gw in gameweeks_in_set(chip_set_for_gameweek(gameweek), final_gameweek)
                if gw >= gameweek])


def validate_chip_usage(
    used: Iterable[ChipUsage],
    final_gameweek: int = 38,
) -> ValidationResult:
    """Check a season's worth of chip plays against every chip rule."""
    result = ValidationResult()
    usages = list(used)

    by_gameweek: dict[int, list[ChipUsage]] = {}
    for usage in usages:
        by_gameweek.setdefault(usage.gameweek, []).append(usage)

    for gameweek, plays in sorted(by_gameweek.items()):
        if len(plays) > 1:
            names = ", ".join(str(p.chip) for p in plays)
            result.add(
                "multiple_chips",
                f"only one chip may be played per gameweek; GW{gameweek} has {names}",
            )

    for chip_set in (1, 2):
        in_set = [u for u in usages if chip_set_for_gameweek(u.gameweek) == chip_set]
        seen: dict[Chip, int] = {}
        for usage in sorted(in_set, key=lambda u: u.gameweek):
            if usage.chip in seen:
                result.add(
                    "chip_reused",
                    f"{usage.chip} was already played in GW{seen[usage.chip]}; "
                    f"it cannot be played again in GW{usage.gameweek} "
                    f"(both are in chip set {chip_set})",
                )
            else:
                seen[usage.chip] = usage.gameweek

    for usage in usages:
        if usage.gameweek < 1 or usage.gameweek > final_gameweek:
            result.add(
                "chip_gameweek",
                f"GW{usage.gameweek} is outside the season",
            )

    return result


@dataclass(frozen=True, slots=True)
class ChipDeadline:
    """How much runway a chip set has left, for the Manager's planning."""

    chip_set: int
    last_gameweek: int
    gameweeks_remaining: int
    unused: frozenset[Chip]

    @property
    def must_play_now(self) -> bool:
        """True when there are exactly as many gameweeks left as unused chips,
        so every remaining gameweek has to carry one or a chip will expire."""
        return bool(self.unused) and self.gameweeks_remaining <= len(self.unused)


def chip_deadline(
    gameweek: int, used: Sequence[ChipUsage], final_gameweek: int = 38
) -> ChipDeadline:
    chip_set = chip_set_for_gameweek(gameweek)
    last = FIRST_CHIP_SET_LAST_GAMEWEEK if chip_set == 1 else final_gameweek
    spent = {u.chip for u in used if chip_set_for_gameweek(u.gameweek) == chip_set}
    return ChipDeadline(
        chip_set=chip_set,
        last_gameweek=last,
        gameweeks_remaining=last - gameweek + 1,
        unused=frozenset({Chip(c) for c in CHIPS_PER_SET} - spent),
    )


__all__ = [
    "ChipDeadline",
    "ChipWindow",
    "available_chips",
    "chip_windows_from_bootstrap",
    "default_chip_windows",
    "chip_deadline",
    "chip_set_for_gameweek",
    "expiring_chips",
    "gameweeks_in_set",
    "gameweeks_left_in_set",
    "validate_chip_usage",
]
