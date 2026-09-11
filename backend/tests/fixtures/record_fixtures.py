"""Records real FPL API responses, trimmed, for the ingestion tests.

The point of these is *shape* fidelity: field names, types, nesting and the
things the API does that a hand-written payload would not think to do. They are
trimmed to a few dozen players so the files stay readable in a diff, but every
field the API sends for those players is kept untouched.

Regenerate with:

    python tests/fixtures/record_fixtures.py

Blank and double gameweeks are *not* covered here, because the published
fixture list has none yet this season. Those cases live in
`build_synthetic_fixtures.py`, which constructs them deliberately.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
BASE = "https://fantasy.premierleague.com/api"

#: The gameweek whose live payload is recorded. Must be finished, so the stats
#: are settled and the test expectations do not move under us.
LIVE_GAMEWEEK = 3

#: Fixtures are trimmed to this many gameweeks; the full list is 380 rows.
FIXTURE_GAMEWEEKS = 6

#: How many players of each position to keep. Enough to build a legal
#: 2/5/5/3 squad inside £100m and the three-per-club limit, with room to spare
#: for an optimiser to actually have a choice.
KEEP_PER_POSITION = {1: 6, 2: 14, 3: 14, 4: 8}


def fetch(path: str):
    response = httpx.get(f"{BASE}/{path}", timeout=60, follow_redirects=True)
    response.raise_for_status()
    return response.json()


#: At most this many recorded players per club per position, so the selection
#: does not cluster on one squad and leave the club limit untestable.
MAX_PER_CLUB_PER_POSITION = 2


def select_elements(elements: list[dict]) -> list[dict]:
    """Pick a spread of players across the whole price range and many clubs.

    Deterministic, and deliberately spans cheap to premium: a subset that is
    all budget players would make a £100m squad trivially affordable, so the
    budget constraint would never bind and the optimiser tests downstream would
    prove nothing.
    """
    selected: list[dict] = []

    for element_type, keep in KEEP_PER_POSITION.items():
        pool = sorted(
            (e for e in elements if e["element_type"] == element_type),
            key=lambda e: (e["now_cost"], e["id"]),
        )
        if not pool:
            continue

        picked: list[dict] = []
        seen_teams: dict[int, int] = {}
        taken: set[int] = set()

        # Evenly spaced targets across the whole price-sorted pool, so the
        # cheapest and the most expensive both make it in.
        for slot in range(keep):
            target = round(slot * (len(pool) - 1) / max(keep - 1, 1))
            # Walk outward from the target for the nearest player whose club is
            # not already over-represented.
            for offset in range(len(pool)):
                for index in (target + offset, target - offset):
                    if not 0 <= index < len(pool) or index in taken:
                        continue
                    candidate = pool[index]
                    if seen_teams.get(candidate["team"], 0) >= MAX_PER_CLUB_PER_POSITION:
                        continue
                    picked.append(candidate)
                    taken.add(index)
                    seen_teams[candidate["team"]] = seen_teams.get(candidate["team"], 0) + 1
                    break
                else:
                    continue
                break

        selected.extend(picked)

    return selected


def ensure_interesting(elements: list[dict], selected: list[dict]) -> list[dict]:
    """Make sure the awkward availability cases are in the recording.

    A squad-building test only exercises the injury and suspension handling if
    at least one recorded player actually carries those flags.
    """
    chosen = {e["id"] for e in selected}
    wanted = [
        ("injured", lambda e: e["status"] == "i"),
        ("doubtful", lambda e: e.get("chance_of_playing_next_round") not in (None, 0, 100)),
        ("unavailable", lambda e: e["status"] in ("u", "s", "n")),
        ("has news", lambda e: bool(e.get("news"))),
    ]
    for label, predicate in wanted:
        if any(predicate(e) for e in selected):
            continue
        extra = next((e for e in elements if predicate(e) and e["id"] not in chosen), None)
        if extra:
            selected.append(extra)
            chosen.add(extra["id"])
            print(f"  added {extra['web_name']} to cover: {label}")
    return selected


def main() -> None:
    print("fetching bootstrap-static/ ...")
    bootstrap = fetch("bootstrap-static/")
    print("fetching fixtures/ ...")
    fixtures = fetch("fixtures/")
    print(f"fetching event/{LIVE_GAMEWEEK}/live/ ...")
    live = fetch(f"event/{LIVE_GAMEWEEK}/live/")

    selected = ensure_interesting(bootstrap["elements"], select_elements(bootstrap["elements"]))
    keep_ids = {e["id"] for e in selected}
    print(f"  keeping {len(keep_ids)} of {len(bootstrap['elements'])} players")

    trimmed = {
        **bootstrap,
        "elements": [e for e in bootstrap["elements"] if e["id"] in keep_ids],
    }

    trimmed_fixtures = [
        f for f in fixtures if f["event"] is not None and f["event"] <= FIXTURE_GAMEWEEKS
    ]

    trimmed_live = {
        "elements": [e for e in live["elements"] if e["id"] in keep_ids],
    }

    write("bootstrap_static.json", trimmed)
    write("fixtures.json", trimmed_fixtures)
    write(f"event_{LIVE_GAMEWEEK}_live.json", trimmed_live)

    meta = {
        "recorded_from": BASE,
        "live_gameweek": LIVE_GAMEWEEK,
        "fixture_gameweeks": FIXTURE_GAMEWEEKS,
        "player_count": len(keep_ids),
        "team_count": len(bootstrap["teams"]),
        "current_gameweek": next(
            (e["id"] for e in bootstrap["events"] if e["is_current"]), None
        ),
    }
    write("recording_meta.json", meta)


def write(name: str, payload) -> None:
    path = HERE / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {name} ({path.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
