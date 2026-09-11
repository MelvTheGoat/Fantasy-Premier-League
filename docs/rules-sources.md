# Where each rule came from

Every FPL rule the code enforces is listed here with the source it was checked
against, so a rule can be re-verified when the game changes.

The official FPL API (`fantasy.premierleague.com`) and the Premier League site
(`premierleague.com`) were **unreachable from the environment this was built
in** — both are blocked by an egress policy, for `curl` and for any other
client. Rules were therefore verified against secondary reporting of the
official announcements. Three values could not be confirmed at all and are
marked UNVERIFIED below; each is a named constant rather than a number buried
in the logic, so confirming it is a one-line change.

**Before trusting this in anger, run the checks in "What to re-verify" below
from a machine that can reach the FPL API.**

## Squad and formation

| Rule | Value | Source |
|---|---|---|
| Squad size and shape | 15: 2 GK, 5 DEF, 5 MID, 3 FWD | `element_types` in `bootstrap-static/` (`squad_select`) |
| Budget | £100.0m | FPL game rules |
| Players per club | 3 | FPL game rules |
| Starting XI | 11, with 1 GK, ≥3 DEF, ≥2 MID, ≥1 FWD | `squad_min_play` / `squad_max_play` |
| Formation | not fixed; follows from the XI | derived from the min/max bounds |

Positions are read from `element_type` on every refresh and are never cached in
code. Several players were reclassified for 2026/27, and
`test_a_reclassified_player_is_updated_not_kept` pins this down.

## Transfers

| Rule | Value | Source |
|---|---|---|
| Free transfers per gameweek | 1 | FPL game rules |
| Maximum banked | 5 | [OneFPL, "How Many Free Transfers Can You Bank in FPL? 2026/27"](https://onefpl.com/blog/how-many-free-transfers-can-you-bank-fpl) |
| Cost of an extra transfer | −4 points | FPL game rules |
| Selling price | purchase price + half of any rise, rounded down to £0.1m; falls taken in full | [OneFPL, "How FPL Price Changes Work"](https://onefpl.com/fpl-price-changes) |

Prices are held in integer tenths of a million throughout, so "round down to
the nearest £0.1m" is exact integer division rather than a float rounding that
could drift over a season.

## Chips (2026/27)

| Rule | Value | Source |
|---|---|---|
| Two full sets per season | Wildcard, Free Hit, Bench Boost, Triple Captain in each half | [Premier League, "What's happening with FPL chips in 2026/27"](https://www.premierleague.com/en/news/4679879/whats-happening-with-fpl-chips-in-202627) |
| First set expires | at the GW19 deadline, 13:30 GMT Sat 2 January; no carry-over | as above |
| Second set available | from GW20 (deadline 18:30 UTC Wed 6 January 2027) | as above |
| One chip per gameweek | yes | as above |
| Banked free transfers across Wildcard / Free Hit | maintained, not reset | [Fantasy Football Scout, "Do I keep my saved transfers when using the Free Hit chip?"](https://www.fantasyfootballscout.co.uk/2026/04/24/do-i-keep-my-saved-transfers-when-using-the-free-hit-chip-3) |

### UNVERIFIED — `CHIP_GAMEWEEK_ACCRUES_FREE_TRANSFER`

Banked free transfers are confirmed to survive a Wildcard or Free Hit. What
could not be confirmed is whether the usual +1 is *also* credited for the
following gameweek — i.e. whether a manager with 3 banked transfers who plays a
Free Hit starts the next gameweek on 3 or on 4.

The code assumes **4** (normal accrual still applies, capped at 5), which
matches how the game grants the transfer unconditionally at every deadline.
The constant is in `fplai/rules/constants.py`; flipping it to `False` switches
to the other reading, and `test_banked_transfers_survive_a_chip` covers both.

## Scoring

Player points are taken from `event/{gw}/live/` and are never recalculated.
The projection model needs to understand the scoring system, so these are
recorded for it:

| Rule | Value | Source |
|---|---|---|
| DefCon threshold, defenders | 10 clearances + blocks + interceptions + tackles | [Premier League, "How do players score defensive contribution points"](https://www.premierleague.com/en/news/4361991/whats-happening-with-defensive-contribution-points-in-202627-fantasy) |
| DefCon threshold, MID and FWD | 12 of the above plus ball recoveries | as above |
| DefCon award | 2 points, once per match | as above |
| Lockdown | 09:00 UK on the day after the gameweek's final match | [Fantasy Football Scout, "FPL 2026/27: 5 rule changes + new features"](https://www.fantasyfootballscout.co.uk/2026/07/20/fpl-2026-27-5-rule-changes-new-features-announced) |

### 2026/27 Bonus Points System changes

Relevant to the expected-bonus part of the projection model, per
[Premier League, "Changes to Bonus Points System"](https://www.premierleague.com/en/news/4679946/whats-new-in-202627-fantasy-changes-to-bonus-points-system):

- Goalkeepers get 2 BPS for any save, plus 1 for a save inside the box. The
  "save from outside the box" metric is removed.
- A new +1 BPS for saving a big chance. A saved penalty is now 7 BPS, down
  from 8, with the difference returned through the big-chance bonus.
- 1 BPS per **three** clearances, blocks and interceptions, up from every two.
  This narrows the gap between centre-backs and attacking full-backs.
- The −1 BPS for being successfully tackled is removed.

Net effect: goalkeepers, full-backs and attacking players pick up more bonus.
The xPts model must learn these weights from 2026/27 data rather than carrying
over weights fitted on earlier seasons.

## Automatic substitutions

| Rule | Behaviour | Source |
|---|---|---|
| Trigger | a starter finishing the gameweek on 0 minutes | FPL game rules |
| Order | bench order, left to right | [OneFPL, "FPL Auto Subs Explained"](https://onefpl.com/blog/fpl-auto-subs-bench-order-rules) |
| Formation | a substitute is skipped if they would leave an illegal XI | as above |
| Goalkeepers | the bench keeper only ever replaces the starting keeper | as above |
| Timing | applied once the player's gameweek fixtures have finished | derived: a player yet to kick off is indistinguishable from one dropped |

The outfield pass is greedy in bench order rather than an optimisation. FPL
walks the bench; it does not search for the substitution set that scores most.
A substitute skipped for one vacancy stays available for a later one, which
`test_a_skipped_substitute_still_covers_a_later_vacancy` pins down.

## UNVERIFIED — club shirt image URLs

The frontend needs club shirts keyed by **team code** (`teams[].code`, not
`teams[].id`), with a separate goalkeeper variant. The CDN was unreachable, so
the pattern in `Settings.shirt_base_url` is a placeholder. Confirm it from a
browser's network tab on the FPL site and set `FPLAI_SHIRT_BASE_URL`.

## UNVERIFIED — API field names

The ingestion code reads `defensive_contribution`, `tackles`, `recoveries` and
`clearances_blocks_interceptions` from `event/{gw}/live/`. These are the names
the API used when defensive contributions were introduced, but they were not
confirmed against a live response. `ingest_live_gameweek` defaults unknown
stats to zero rather than failing, so a rename would show up as DefCon
projections collapsing to zero rather than as an error — check them first.

## What to re-verify

From a machine that can reach the API:

```sh
curl -s 'https://fantasy.premierleague.com/api/bootstrap-static/' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["element_types"]); print(d.get("chips"))'

curl -s 'https://fantasy.premierleague.com/api/event/1/live/' \
  | python3 -c 'import json,sys; print(sorted(json.load(sys.stdin)["elements"][0]["stats"]))'
```

Then check the three UNVERIFIED items above, and replace the hand-written
payloads in `backend/tests/fixtures/` with recorded real responses — the
ingestion tests assert on shape and derived values, not on the made-up numbers,
so they should keep passing.
