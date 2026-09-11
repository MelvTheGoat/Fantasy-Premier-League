"""Fantasy Premier League rule constants for the 2026/27 season.

Every value here is a *game rule*, not a tuning knob. Values that could not be
confirmed against the official rules page at build time are marked UNVERIFIED
and are referenced from `docs/rules-sources.md`.

Money is stored the way the FPL API stores it: integer tenths of a million.
`75` means £7.5m. Working in tenths keeps the selling-price rounding exact and
avoids float drift when summing 15 prices against a 1000 budget.
"""

from __future__ import annotations

from enum import IntEnum, StrEnum

# --- Squad shape -----------------------------------------------------------

SQUAD_SIZE = 15
STARTING_XI_SIZE = 11
BENCH_SIZE = SQUAD_SIZE - STARTING_XI_SIZE  # 4: one GK + three outfield

#: Total budget for the initial squad, in tenths of a million (£100.0m).
STARTING_BUDGET = 1000

#: Maximum players allowed from any single Premier League club.
MAX_PLAYERS_PER_CLUB = 3


class Position(IntEnum):
    """FPL element types.

    The integer values match ``element_type`` in the FPL API. Positions are
    always read from the API per season -- several players were reclassified
    for 2026/27, so nothing downstream may hardcode a player-to-position map.
    """

    GKP = 1
    DEF = 2
    MID = 3
    FWD = 4

    @property
    def short_name(self) -> str:
        return self.name


#: How many of each position a legal 15-man squad contains.
SQUAD_COMPOSITION: dict[Position, int] = {
    Position.GKP: 2,
    Position.DEF: 5,
    Position.MID: 5,
    Position.FWD: 3,
}

#: Minimum number of each position in a legal starting XI.
FORMATION_MIN: dict[Position, int] = {
    Position.GKP: 1,
    Position.DEF: 3,
    Position.MID: 2,
    Position.FWD: 1,
}

#: Maximum number of each position in a legal starting XI. A GK is capped at
#: one because the squad only holds two and the second is always the bench GK.
FORMATION_MAX: dict[Position, int] = {
    Position.GKP: 1,
    Position.DEF: 5,
    Position.MID: 5,
    Position.FWD: 3,
}

# --- Transfers -------------------------------------------------------------

#: Free transfers granted at each deadline from GW2 onwards.
FREE_TRANSFERS_PER_GAMEWEEK = 1

#: Hard ceiling on banked free transfers.
MAX_BANKED_FREE_TRANSFERS = 5

#: Free transfers available in the very first gameweek. Transfers before the
#: GW1 deadline are unlimited, so the counter only starts mattering in GW2.
INITIAL_FREE_TRANSFERS = 1

#: Points deducted for each transfer beyond the free allowance.
TRANSFER_HIT_COST = 4

# --- Captaincy -------------------------------------------------------------

CAPTAIN_MULTIPLIER = 2
TRIPLE_CAPTAIN_MULTIPLIER = 3

# --- Chips -----------------------------------------------------------------


class Chip(StrEnum):
    """Chip identifiers, using the FPL API's ``name`` values."""

    WILDCARD = "wildcard"
    FREE_HIT = "freehit"
    BENCH_BOOST = "bboost"
    TRIPLE_CAPTAIN = "3xc"


#: Chips that replace the normal transfer rules for one gameweek.
UNLIMITED_TRANSFER_CHIPS = frozenset({Chip.WILDCARD, Chip.FREE_HIT})

#: 2026/27 runs two full chip sets. The first must be played on or before the
#: GW19 deadline and does not carry over; a fresh set unlocks at GW20.
FIRST_CHIP_SET_LAST_GAMEWEEK = 19
SECOND_CHIP_SET_FIRST_GAMEWEEK = 20

#: Every chip is available once per set.
CHIPS_PER_SET: frozenset[str] = frozenset(
    {Chip.WILDCARD, Chip.FREE_HIT, Chip.BENCH_BOOST, Chip.TRIPLE_CAPTAIN}
)

#: Only one chip may be played in any single gameweek.
MAX_CHIPS_PER_GAMEWEEK = 1

#: The first gameweek a transfer chip may be played in. Wildcard and Free Hit
#: open at GW2, not GW1: transfers before the opening deadline are already
#: unlimited, so there is nothing for the chip to buy. Confirmed from
#: `start_event` on the `chips` block of `bootstrap-static/`.
TRANSFER_CHIP_FIRST_GAMEWEEK = 2

#: Team chips -- Bench Boost and Triple Captain -- are available from GW1.
TEAM_CHIP_FIRST_GAMEWEEK = 1

#: Which chips the API classes as `transfer` rather than `team` chips.
TRANSFER_CHIPS: frozenset[str] = frozenset({Chip.WILDCARD, Chip.FREE_HIT})

#: Whether a gameweek in which Wildcard or Free Hit was played still accrues
#: the normal free transfer for the following gameweek.
#:
#: Banked free transfers are *maintained* across a Wildcard or Free Hit rather
#: than being reset to one. Whether the usual +1 is also credited on top is not
#: stated in `game_settings`, so it stays a named constant: flipping it to
#: False switches to the other reading. See docs/rules-sources.md.
CHIP_GAMEWEEK_ACCRUES_FREE_TRANSFER = True

# --- Scoring ---------------------------------------------------------------

#: Defensive-contribution thresholds, unchanged for 2026/27. Defenders need 10
#: combined clearances, blocks, interceptions and tackles; midfielders and
#: forwards need 12 of the same plus ball recoveries.
DEFCON_THRESHOLD: dict[Position, int] = {
    Position.DEF: 10,
    Position.MID: 12,
    Position.FWD: 12,
}

#: Points awarded once the threshold is met, capped at one award per match.
DEFCON_POINTS = 2

#: Gameweek scores stop being provisional at 09:00 UK time on the day after the
#: gameweek's final match, when Opta's post-match review is folded in.
LOCKDOWN_HOUR_UK = 9
LOCKDOWN_TIMEZONE = "Europe/London"
