# Where each rule came from

Every FPL rule the code enforces, with the source it was checked against.

Most are now confirmed **directly against the live FPL API**, which publishes
the game's own settings. Where the API states a rule, that is the source used
and the citation is the field name, because it cannot drift from the game.

The one remaining unconfirmed item is listed under [Still
unconfirmed](#still-unconfirmed).

## Confirmed from `bootstrap-static/` → `game_settings`

These are the game's own numbers, read straight from the API:

| Rule | Constant | API field | Value |
|---|---|---|---|
| Squad size | `SQUAD_SIZE` | `squad_squadsize` | 15 |
| Starting XI size | `STARTING_XI_SIZE` | `squad_squadplay` | 11 |
| Budget | `STARTING_BUDGET` | `squad_total_spend` | 1000 (£100.0m) |
| Players per club | `MAX_PLAYERS_PER_CLUB` | `squad_team_limit` | 3 |
| Selling fee | — | `transfers_sell_on_fee` | 0.5 (half the rise) |
| Max banked free transfers | `MAX_BANKED_FREE_TRANSFERS` | `max_extra_free_transfers` + 1 | 5 |

Note the last one: the API says *extra* transfers, capped at 4, on top of the
standard one — so five in total, which is what the constant holds.

## Confirmed from `bootstrap-static/` → `element_types`

| Position | `squad_select` | `squad_min_play` | `squad_max_play` |
|---|---|---|---|
| GKP | 2 | 1 | 1 |
| DEF | 5 | 3 | 5 |
| MID | 5 | 2 | 5 |
| FWD | 3 | 1 | 3 |

`SQUAD_COMPOSITION`, `FORMATION_MIN` and `FORMATION_MAX` match these exactly.
The formation is never fixed in advance; it falls out of the min/max bounds.

Positions are read from `element_type` on every refresh and never cached in
code — several players were reclassified for 2026/27, and
`test_a_reclassified_player_is_updated_not_kept` pins this down.

## Confirmed from `bootstrap-static/` → `chips`

The API publishes each chip's window as `start_event` / `stop_event`:

| Chip | Set 1 | Set 2 |
|---|---|---|
| Wildcard | GW2–19 | GW20–38 |
| Free Hit | GW2–19 | GW20–38 |
| Bench Boost | GW1–19 | GW20–38 |
| Triple Captain | GW1–19 | GW20–38 |

Two things fall out of this that are easy to get wrong:

- **Transfer chips do not open until GW2.** Transfers before the opening
  deadline are already unlimited, so there is nothing for a Wildcard or Free
  Hit to buy. `available_chips(1, [])` returns only Bench Boost and Triple
  Captain.
- The GW19/GW20 split is the game's own, not an inference from the announced
  2 January reset date.

`default_chip_windows()` encodes these, and
`test_the_coded_defaults_match_what_the_api_publishes` asserts they equal
`chip_windows_from_bootstrap()` on the recorded payload. Prefer the latter
wherever the payload is to hand, so a mid-season change is followed
automatically.

One chip per gameweek, and unused first-set chips expire at the GW19 deadline:
[Premier League, "What's happening with FPL chips in 2026/27"](https://www.premierleague.com/en/news/4679879/whats-happening-with-fpl-chips-in-202627).

## Selling price

`transfers_sell_on_fee: 0.5` confirms the halving. The rounding direction —
down to the nearest £0.1m, with falls taken in full — is from
[OneFPL, "How FPL Price Changes Work"](https://onefpl.com/fpl-price-changes).

Prices are held in integer tenths of a million throughout, so "round down to
the nearest £0.1m" is exact integer division rather than a float rounding that
would drift across a season of transfers.

## Scoring

Player points come from `event/{gw}/live/` and are never recalculated. What the
recorded payload confirms about that endpoint:

- `explain[].stats[].points` sums exactly to `stats.total_points` for every one
  of the 655 players in GW3 — so attributing points per fixture from `explain`
  is sound, which is what makes double gameweeks work.
- `stats.played` tracks `minutes > 0` exactly. The auto-sub rules use minutes,
  per the game's wording, and this field agrees with them.
- `explain[].stats[].points_modification` exists and is 0 throughout. It
  appears to be for retrospective adjustments; nothing depends on it yet.

### Defensive contribution

The four columns the projection model learns from are confirmed present in
`element_stats` and in every live payload: `clearances_blocks_interceptions`,
`tackles`, `recoveries`, `defensive_contribution`.

| Rule | Value | Source |
|---|---|---|
| Threshold, defenders | 10 clearances + blocks + interceptions + tackles | [Premier League](https://www.premierleague.com/en/news/4361991/whats-happening-with-defensive-contribution-points-in-202627-fantasy) |
| Threshold, MID and FWD | 12 of the above plus ball recoveries | as above |
| Award | 2 points, once per match | as above |

### 2026/27 Bonus Points System changes

Relevant to the expected-bonus part of the projection model, per
[Premier League, "Changes to Bonus Points System"](https://www.premierleague.com/en/news/4679946/whats-new-in-202627-fantasy-changes-to-bonus-points-system):

- Goalkeepers get 2 BPS for any save, plus 1 for a save inside the box. The
  "save from outside the box" metric is removed.
- A new +1 BPS for saving a big chance. A saved penalty is now 7 BPS, down from
  8, with the difference returned through the big-chance bonus.
- 1 BPS per **three** clearances, blocks and interceptions, up from every two.
  This narrows the gap between centre-backs and attacking full-backs.
- The −1 BPS for being successfully tackled is removed.

Net effect: goalkeepers, full-backs and attacking players pick up more bonus.
The xPts model must learn these weights from 2026/27 data rather than carrying
over weights fitted on earlier seasons.

### Lockdown

09:00 UK time on the day after the gameweek's final match:
[Fantasy Football Scout](https://www.fantasyfootballscout.co.uk/2026/07/20/fpl-2026-27-5-rule-changes-new-features-announced).
Derived from `MAX(kickoff_time)` per gameweek, stored on `gameweeks`.

## Automatic substitutions

| Rule | Behaviour | Source |
|---|---|---|
| Trigger | a starter finishing the gameweek on 0 minutes | FPL game rules |
| Order | bench order, left to right | [OneFPL](https://onefpl.com/blog/fpl-auto-subs-bench-order-rules) |
| Formation | a substitute is skipped if they would leave an illegal XI | as above |
| Goalkeepers | the bench keeper only ever replaces the starting keeper | as above |
| Timing | applied once the player's gameweek fixtures have finished | derived: a player yet to kick off is indistinguishable from one dropped |

The outfield pass is greedy in bench order rather than an optimisation. FPL
walks the bench; it does not search for the substitution set that scores most.
A substitute skipped for one vacancy stays available for a later one, which
`test_a_skipped_substitute_still_covers_a_later_vacancy` pins down.

## Club shirt images

Confirmed against the live CDN — all 40 URLs (20 clubs × outfield and
goalkeeper) return 200:

```
https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_{team_code}-{size}.webp
https://fantasy.premierleague.com/dist/img/shirts/standard/shirt_{team_code}_1-{size}.webp
```

The key is `teams[].code`, **not** `teams[].id` — Arsenal are id 1 but code 3.
The `_1` suffix is the goalkeeper variant. Sizes 66, 110 and 220 are served.
`resources.premierleague.com` returns 403 for these paths and is the wrong host.

## What changed in the API for 2026/27

Two findings worth recording, because they invalidate approaches that worked in
earlier seasons:

### Team strength ratings are gone

`strength_attack_home`, `strength_attack_away`, `strength_defence_home` and
`strength_defence_away` are **0 for all 20 clubs**. `strength` is `null`.
`strength_overall_home` / `_away` survive but on a **1–5 scale**, not the
roughly 1000–1350 scale earlier seasons used.

This matters: the clean-sheet component of the projection model cannot lean on
published defensive strength. It has to be derived from fixture difficulty
(`team_h_difficulty` / `team_a_difficulty`, 1–5), the 1–5 overall ratings, and
actual results — goals conceded and `expected_goals_conceded` from the live
stats. `test_granular_attack_and_defence_ratings_are_no_longer_published`
guards this, and will fail if FPL starts publishing them again.

### No blanks or doubles scheduled yet

The published fixture list is a clean 380 rows, one fixture per club per
gameweek, with no `event: null` entries. Blanks and doubles appear later in a
season as cup progress forces rearrangement. The blank/double tests therefore
run against constructed payloads from `build_synthetic_fixtures.py`;
`test_the_real_season_currently_has_neither` will fail once real ones appear,
at which point they can be recorded instead.

## Still unconfirmed

### `CHIP_GAMEWEEK_ACCRUES_FREE_TRANSFER`

Banked free transfers survive a Wildcard or Free Hit rather than resetting to
one ([Fantasy Football Scout](https://www.fantasyfootballscout.co.uk/2026/04/24/do-i-keep-my-saved-transfers-when-using-the-free-hit-chip-3)).
What is still not confirmed is whether the usual +1 is *also* credited — i.e.
whether a manager with 3 banked transfers who plays a Free Hit starts the next
gameweek on 3 or on 4.

`game_settings` does not express it, and it cannot be observed without watching
a real entry play the chip. The code assumes **4** (normal accrual still
applies, capped at 5), which matches the game granting the transfer
unconditionally at every deadline. The constant is in
`fplai/rules/constants.py`; flipping it to `False` switches to the other
reading, and `test_banked_transfers_survive_a_chip` covers both.

The cheap way to settle it: once the Manager plays its first Free Hit, compare
the free transfers the model expects against a real entry's
`entry/{id}/event/{gw}/picks/` the following gameweek.

## Re-recording the fixtures

```sh
cd backend
python tests/fixtures/record_fixtures.py        # real, trimmed API responses
python tests/fixtures/build_synthetic_fixtures.py  # blank/double gameweeks
pytest
```

The recorder keeps 43 players spanning the full price range across all 20
clubs, including injured, doubtful and unavailable players, so a legal £100m
squad is buildable from the subset and the budget constraint actually binds.
`recording_meta.json` notes when it was taken and against which gameweek.
