"""Constructs synthetic payloads for cases the real season does not contain yet.

Most ingestion tests run against genuine recorded API responses, written by
`record_fixtures.py`. Blank and double gameweeks cannot come from there: the
published fixture list is currently a clean 380 rows with one fixture per club
per gameweek, because blanks and doubles only appear later in a season as cup
progress forces postponements and rearrangements.

So these are built by hand, deliberately, to the same shapes:

    synthetic_bootstrap.json      six clubs, enough players to fill a squad
    synthetic_fixtures.json       GW4 is a double for two clubs, a blank for two
    synthetic_event_4_live.json   the matching live payload, with two `explain`
                                  entries per player for the doubled clubs

Regenerate with:

    python tests/fixtures/build_synthetic_fixtures.py

Once the real fixture list contains a blank or a double, these can be replaced
by a recording of it.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

TEAMS = [
    {"id": 1, "code": 3, "name": "Arsenal", "short_name": "ARS"},
    {"id": 2, "code": 7, "name": "Aston Villa", "short_name": "AVL"},
    {"id": 3, "code": 91, "name": "Bournemouth", "short_name": "BOU"},
    {"id": 4, "code": 94, "name": "Brentford", "short_name": "BRE"},
    {"id": 5, "code": 8, "name": "Chelsea", "short_name": "CHE"},
    {"id": 6, "code": 31, "name": "Crystal Palace", "short_name": "CRY"},
]

for _index, _team in enumerate(TEAMS):
    _base = 1350 - _index * 40
    _team.update({
        "strength_overall_home": _base, "strength_overall_away": _base - 20,
        "strength_attack_home": _base + 10, "strength_attack_away": _base - 10,
        "strength_defence_home": _base - 10, "strength_defence_away": _base - 30,
    })

# element_type: 1 GK, 2 DEF, 3 MID, 4 FWD -- always read from the API.
# Twenty-six players across six clubs, which is enough to assemble a legal
# 2/5/5/3 squad inside the three-per-club limit.
PLAYERS = [
    (1, 1, 1, "Raya", 55, "a", None, ""),
    (5, 1, 2, "Martinez", 50, "i", 0, "Hamstring injury - expected back 20 Sep"),
    (11, 1, 3, "Petrovic", 45, "a", None, ""),
    (12, 1, 4, "Flekken", 45, "a", None, ""),
    (21, 1, 5, "Sanchez", 47, "a", None, ""),
    (22, 1, 6, "Henderson", 48, "a", None, ""),
    (2, 2, 1, "Saliba", 60, "a", None, ""),
    (23, 2, 1, "White", 55, "a", None, ""),
    (13, 2, 2, "Konsa", 44, "a", None, ""),
    (24, 2, 2, "Digne", 45, "a", None, ""),
    (7, 2, 3, "Kerkez", 48, "a", None, ""),
    (14, 2, 4, "Collins", 45, "a", None, ""),
    (15, 2, 5, "Cucurella", 55, "a", None, ""),
    (16, 2, 6, "Munoz", 52, "a", None, ""),
    (3, 3, 1, "Saka", 100, "a", 75, "Knock - 75% chance of playing"),
    (6, 3, 2, "Rogers", 65, "a", None, ""),
    (8, 3, 3, "Semenyo", 72, "a", None, ""),
    (10, 3, 4, "Mbeumo", 80, "a", None, ""),
    (17, 3, 5, "Palmer", 105, "a", None, ""),
    (25, 3, 5, "Enzo", 55, "a", None, ""),
    (18, 3, 6, "Eze", 68, "a", None, ""),
    (26, 3, 6, "Sarr", 62, "a", None, ""),
    (4, 4, 2, "Watkins", 90, "a", None, ""),
    (9, 4, 4, "Wissa", 62, "s", 0, "Suspended - one match"),
    (19, 4, 5, "Jackson", 75, "a", None, ""),
    (20, 4, 6, "Mateta", 74, "a", None, ""),
]


def element(player, now_cost_override=None):
    element_id, element_type, team, name, cost, status, chance, news = player
    return {
        "id": element_id,
        "code": 100000 + element_id,
        "web_name": name,
        "first_name": name,
        "second_name": name,
        "team": team,
        "element_type": element_type,
        "now_cost": now_cost_override if now_cost_override is not None else cost,
        "status": status,
        "chance_of_playing_next_round": chance,
        "chance_of_playing_this_round": chance,
        "news": news,
        "news_added": "2026-09-05T10:00:00Z" if news else None,
        "selected_by_percent": "12.3",
        "form": "4.2",
        "total_points": 20,
        "minutes": 270,
        "expected_goals": "1.20",
        "expected_assists": "0.80",
    }


def events():
    rows = []
    for gameweek in range(1, 39):
        rows.append({
            "id": gameweek,
            "name": f"Gameweek {gameweek}",
            "deadline_time": f"2026-{8 + (gameweek // 5):02d}-{(gameweek % 28) + 1:02d}T17:30:00Z",
            "is_current": gameweek == 4,
            "is_next": gameweek == 5,
            "finished": gameweek < 4,
            "data_checked": gameweek < 4,
            "average_entry_score": 52 + gameweek if gameweek < 4 else 0,
            "highest_score": 120 + gameweek if gameweek < 4 else None,
        })
    return rows


def fixtures():
    """Four clubs over five gameweeks.

    GW4 is the interesting one: clubs 1 and 2 play twice (a double) and clubs 3
    and 4 do not play at all (a blank).
    """
    rows = [
        # GW1-3: ordinary weeks, one fixture per club.
        {"id": 1, "event": 1, "team_h": 1, "team_a": 2, "kickoff_time": "2026-08-15T14:00:00Z"},
        {"id": 2, "event": 1, "team_h": 3, "team_a": 4, "kickoff_time": "2026-08-15T16:30:00Z"},
        {"id": 3, "event": 1, "team_h": 5, "team_a": 6, "kickoff_time": "2026-08-16T14:00:00Z"},
        {"id": 4, "event": 2, "team_h": 2, "team_a": 3, "kickoff_time": "2026-08-22T14:00:00Z"},
        {"id": 5, "event": 2, "team_h": 4, "team_a": 1, "kickoff_time": "2026-08-22T16:30:00Z"},
        {"id": 6, "event": 2, "team_h": 6, "team_a": 5, "kickoff_time": "2026-08-23T14:00:00Z"},
        {"id": 7, "event": 3, "team_h": 1, "team_a": 3, "kickoff_time": "2026-08-29T14:00:00Z"},
        {"id": 8, "event": 3, "team_h": 2, "team_a": 4, "kickoff_time": "2026-08-29T16:30:00Z"},
        {"id": 9, "event": 3, "team_h": 5, "team_a": 6, "kickoff_time": "2026-08-30T14:00:00Z"},
        # GW4: clubs 1 and 2 play each other twice, so it is a double for them;
        # clubs 3 and 4 have no fixture at all, so it is a blank for them.
        {"id": 10, "event": 4, "team_h": 1, "team_a": 2, "kickoff_time": "2026-09-12T14:00:00Z"},
        {"id": 11, "event": 4, "team_h": 2, "team_a": 1, "kickoff_time": "2026-09-14T19:00:00Z"},
        {"id": 12, "event": 4, "team_h": 5, "team_a": 6, "kickoff_time": "2026-09-12T16:30:00Z"},
        # GW5: back to normal.
        {"id": 13, "event": 5, "team_h": 3, "team_a": 1, "kickoff_time": "2026-09-19T14:00:00Z"},
        {"id": 14, "event": 5, "team_h": 4, "team_a": 2, "kickoff_time": "2026-09-19T16:30:00Z"},
        {"id": 15, "event": 5, "team_h": 6, "team_a": 5, "kickoff_time": "2026-09-20T14:00:00Z"},
        # An unscheduled fixture, which the API reports with a null event.
        {"id": 16, "event": None, "team_h": 3, "team_a": 2, "kickoff_time": None},
    ]
    for row in rows:
        finished = row["event"] is not None and row["event"] < 4
        row.update({
            "team_h_difficulty": 3,
            "team_a_difficulty": 3,
            "team_h_score": 2 if finished else None,
            "team_a_score": 1 if finished else None,
            "started": finished,
            "finished": finished,
            "finished_provisional": finished,
        })
    return rows


def stat_block(**kwargs):
    block = {
        "minutes": 0, "goals_scored": 0, "assists": 0, "clean_sheets": 0,
        "goals_conceded": 0, "own_goals": 0, "penalties_saved": 0,
        "penalties_missed": 0, "yellow_cards": 0, "red_cards": 0, "saves": 0,
        "bonus": 0, "bps": 0, "starts": 0, "total_points": 0,
        "clearances_blocks_interceptions": 0, "tackles": 0, "recoveries": 0,
        "defensive_contribution": 0,
        "expected_goals": "0.00", "expected_assists": "0.00",
        "expected_goal_involvements": "0.00", "expected_goals_conceded": "0.00",
    }
    block.update(kwargs)
    return block


def live_single_gameweek():
    """GW3: one fixture each, including a player who did not feature."""
    return {"elements": [
        {"id": 1, "stats": stat_block(minutes=90, saves=4, clean_sheets=1, bonus=2, bps=32,
                                      total_points=9, starts=1),
         "explain": [{"fixture": 7, "stats": [
             {"identifier": "minutes", "points": 2, "value": 90},
             {"identifier": "clean_sheets", "points": 4, "value": 1},
             {"identifier": "saves", "points": 1, "value": 4},
             {"identifier": "bonus", "points": 2, "value": 2},
         ]}]},
        {"id": 2, "stats": stat_block(minutes=90, clean_sheets=1, total_points=8, starts=1,
                                      clearances_blocks_interceptions=8, tackles=3,
                                      defensive_contribution=2, bps=28),
         "explain": [{"fixture": 7, "stats": [
             {"identifier": "minutes", "points": 2, "value": 90},
             {"identifier": "clean_sheets", "points": 4, "value": 1},
             {"identifier": "defensive_contribution", "points": 2, "value": 1},
         ]}]},
        {"id": 3, "stats": stat_block(minutes=0, total_points=0),
         "explain": [{"fixture": 7, "stats": [
             {"identifier": "minutes", "points": 0, "value": 0},
         ]}]},
        {"id": 8, "stats": stat_block(minutes=78, goals_scored=1, total_points=7, starts=1,
                                      expected_goals="0.64", bps=25),
         "explain": [{"fixture": 7, "stats": [
             {"identifier": "minutes", "points": 2, "value": 78},
             {"identifier": "goals_scored", "points": 5, "value": 1},
         ]}]},
    ]}


def live_double_gameweek():
    """GW4: clubs 1 and 2 play twice, so their players carry two explain rows."""
    return {"elements": [
        {"id": 1, "stats": stat_block(minutes=180, saves=7, clean_sheets=1, total_points=11,
                                      starts=2, bps=55),
         "explain": [
             {"fixture": 10, "stats": [
                 {"identifier": "minutes", "points": 2, "value": 90},
                 {"identifier": "clean_sheets", "points": 4, "value": 1},
                 {"identifier": "saves", "points": 1, "value": 3},
             ]},
             {"fixture": 11, "stats": [
                 {"identifier": "minutes", "points": 2, "value": 90},
                 {"identifier": "saves", "points": 1, "value": 4},
                 {"identifier": "goals_conceded", "points": -1, "value": 2},
             ]},
         ]},
        {"id": 4, "stats": stat_block(minutes=155, goals_scored=2, assists=1, total_points=17,
                                      starts=2, expected_goals="1.42", bps=61),
         "explain": [
             {"fixture": 10, "stats": [
                 {"identifier": "minutes", "points": 2, "value": 90},
                 {"identifier": "goals_scored", "points": 4, "value": 1},
                 {"identifier": "assists", "points": 3, "value": 1},
             ]},
             {"fixture": 11, "stats": [
                 {"identifier": "minutes", "points": 2, "value": 65},
                 {"identifier": "goals_scored", "points": 4, "value": 1},
             ]},
         ]},
        # A player who played the second fixture only.
        {"id": 6, "stats": stat_block(minutes=62, total_points=2, starts=1),
         "explain": [
             {"fixture": 10, "stats": [{"identifier": "minutes", "points": 0, "value": 0}]},
             {"fixture": 11, "stats": [{"identifier": "minutes", "points": 2, "value": 62}]},
         ]},
        # A blank-gameweek player: their club has no fixture, so no explain rows.
        {"id": 8, "stats": stat_block(), "explain": []},
    ]}


def main() -> None:
    bootstrap = {
        "teams": TEAMS,
        "elements": [element(p) for p in PLAYERS],
        "element_types": [
            {"id": 1, "singular_name_short": "GKP", "squad_select": 2,
             "squad_min_play": 1, "squad_max_play": 1},
            {"id": 2, "singular_name_short": "DEF", "squad_select": 5,
             "squad_min_play": 3, "squad_max_play": 5},
            {"id": 3, "singular_name_short": "MID", "squad_select": 5,
             "squad_min_play": 2, "squad_max_play": 5},
            {"id": 4, "singular_name_short": "FWD", "squad_select": 3,
             "squad_min_play": 1, "squad_max_play": 3},
        ],
        "events": events(),
        "total_players": 11_000_000,
    }

    # A second snapshot with prices moved, for the price-tracking tests.
    moved = {
        **bootstrap,
        "elements": [
            element(p, now_cost_override=p[4] + (3 if p[0] == 3 else -1 if p[0] == 9 else 0))
            for p in PLAYERS
        ],
    }

    write("synthetic_bootstrap.json", bootstrap)
    write("synthetic_bootstrap_gw5.json", moved)
    write("synthetic_fixtures.json", fixtures())
    write("synthetic_event_3_live.json", live_single_gameweek())
    write("synthetic_event_4_live.json", live_double_gameweek())


def write(name: str, payload) -> None:
    (HERE / name).write_text(json.dumps(payload, indent=2) + "\n")
    print(f"wrote {name}")


if __name__ == "__main__":
    main()
