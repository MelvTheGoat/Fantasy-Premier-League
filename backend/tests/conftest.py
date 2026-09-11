"""Shared builders for the rules tests.

The rules engine deals in element ids, positions, clubs and prices, so the
tests build tiny synthetic rosters rather than loading real FPL data. That
keeps each test readable and lets a test state exactly the situation it cares
about -- a blank gameweek, a 0-minute captain -- without any other noise.
"""

from __future__ import annotations

import pytest

from fplai.rules.constants import Position
from fplai.rules.types import Lineup, Player, PlayerGameweekResult, Squad, SquadPick

#: Element id ranges by position, so a test can read `201` as "a defender".
GK_IDS = range(101, 111)
DEF_IDS = range(201, 221)
MID_IDS = range(301, 321)
FWD_IDS = range(401, 421)


def _default_team(element: int) -> int:
    """Spread players across the twenty clubs without clustering by position.

    Keying on the position prefix as well as the offset stops 101, 201, 301 and
    401 -- one of each position, all offset zero -- from landing on one club.
    """
    return ((element // 100) * 5 + (element % 100)) % 20 + 1


#: A club that no player in `DEFAULT_SQUAD_ELEMENTS` occupies by default, so a
#: test can move players onto it and control the club count exactly.
SPARE_TEAM = 9


def make_player(
    element: int,
    position: Position,
    team: int = 1,
    price: int = 50,
    web_name: str = "",
) -> Player:
    return Player(
        element=element,
        position=position,
        team=team,
        price=price,
        web_name=web_name or f"P{element}",
    )


def build_roster(
    *,
    teams: dict[int, int] | None = None,
    prices: dict[int, int] | None = None,
) -> dict[int, Player]:
    """A roster large enough for any squad the tests assemble.

    Clubs are spread so that the fifteen players of `DEFAULT_SQUAD_ELEMENTS`
    land on fifteen different clubs, and the three-per-club limit is never
    tripped by accident. A test that wants to exercise the limit passes `teams`
    to force players together -- use `SPARE_TEAM`, which no default-squad
    player occupies. `test_the_default_squad_spans_fifteen_clubs` guards this.
    """
    teams = teams or {}
    prices = prices or {}
    roster: dict[int, Player] = {}

    for group, position in (
        (GK_IDS, Position.GKP),
        (DEF_IDS, Position.DEF),
        (MID_IDS, Position.MID),
        (FWD_IDS, Position.FWD),
    ):
        for offset, element in enumerate(group):
            roster[element] = make_player(
                element,
                position,
                team=teams.get(element, _default_team(element)),
                price=prices.get(element, 40 + offset),
            )
    return roster


#: A legal 15-man squad: 2 GK, 5 DEF, 5 MID, 3 FWD.
DEFAULT_SQUAD_ELEMENTS = (
    101, 102,
    201, 202, 203, 204, 205,
    301, 302, 303, 304, 305,
    401, 402, 403,
)


def build_squad(
    elements: tuple[int, ...] = DEFAULT_SQUAD_ELEMENTS,
    *,
    purchase_prices: dict[int, int] | None = None,
    roster: dict[int, Player] | None = None,
    bank: int = 0,
) -> Squad:
    """A squad whose purchase prices default to the roster's current prices."""
    purchase_prices = purchase_prices or {}
    picks = []
    for element in elements:
        if element in purchase_prices:
            price = purchase_prices[element]
        elif roster is not None:
            price = roster[element].price
        else:
            price = 50
        picks.append(SquadPick(element=element, purchase_price=price))
    return Squad(picks=tuple(picks), bank=bank)


def build_lineup(
    starters: tuple[int, ...] = (101, 201, 202, 203, 301, 302, 303, 304, 401, 402, 403),
    bench: tuple[int, ...] = (102, 204, 205, 305),
    captain: int | None = None,
    vice_captain: int | None = None,
) -> Lineup:
    """A 4-4-2-ish lineup; bench slot 0 is the substitute keeper, as FPL requires."""
    return Lineup(
        starters=starters,
        bench=bench,
        captain=captain if captain is not None else starters[-1],
        vice_captain=vice_captain if vice_captain is not None else starters[-2],
    )


def results_for(
    elements,
    *,
    minutes: int = 90,
    points: int = 2,
    finished: bool = True,
    overrides: dict[int, dict] | None = None,
) -> dict[int, PlayerGameweekResult]:
    """Build a live-results map, with per-player overrides for the interesting cases.

        results_for(squad.elements, overrides={205: {"minutes": 0}})
    """
    overrides = overrides or {}
    results = {}
    for element in elements:
        spec = {
            "minutes": minutes,
            "total_points": points,
            "fixtures_finished": finished,
            "fixture_count": 1,
        }
        spec.update(overrides.get(element, {}))
        results[element] = PlayerGameweekResult(element=element, **spec)
    return results


@pytest.fixture
def roster() -> dict[int, Player]:
    return build_roster()


@pytest.fixture
def squad(roster) -> Squad:
    return build_squad(roster=roster)


@pytest.fixture
def lineup() -> Lineup:
    return build_lineup()
