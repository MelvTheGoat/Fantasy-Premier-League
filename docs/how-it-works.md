# How this was built, and what it is for

This is the long version. The [README](../README.md) says how to run it; this
says why it is shaped the way it is, what each decision cost, and what went
wrong on the way. [`rules-sources.md`](rules-sources.md) holds the evidence for
every FPL rule encoded here.

---

## The question

Two models play the 2026/27 Fantasy Premier League season side by side.

**The Manager** carries one squad from gameweek one to thirty-eight under the
real constraints: one free transfer a week, −4 for each extra, a £100m budget
with proper selling prices, and two sets of chips. Everything it does in
January is shaped by what it bought in August.

**Best XI of the Week** rebuilds the best legal squad from scratch every
gameweek. No transfers, no hits, no chips, no memory. The £100m budget, the
2/5/5/3 squad and the three-per-club limit still apply — it is a legal FPL
squad, just one that owes nothing to last week.

They are not competing. Best XI has every advantage and will win. **The gap
between them is the point**: it is a measurement of what continuity costs.
Every FPL manager pays that price and nobody can see the bill, because you only
ever get to play one of the two. Running both makes the invisible half visible.

Both are scored with **real FPL points, pulled from the API and never
recalculated**, and tracked against the **official gameweek average**
(`average_entry_score`) — the same yardstick every human manager is measured
by. Nothing here is graded against a simulation of itself.

---

## The order things were built in, and why

The build went rules → data → projection → optimisation → strategy → interface
→ deployment. That order is not arbitrary. Every layer above the rules is an
opinion; the rules are facts. Getting an opinion wrong costs points. Getting a
rule wrong makes every number produced afterwards meaningless, and the failure
is silent — a squad that is illegal in a way the code does not notice still
produces a confident-looking score.

So the rules came first, and they came with tests before anything used them.

### 1. The rules, as facts

`fplai/rules/` is pure logic: squad validity, formations, automatic
substitutions, captaincy, selling prices, transfer costs, chip windows,
scoring. No I/O, no imports from the data layer. That separation is what lets
every rule be tested without a database, and it is why the rules have the
densest test coverage in the project.

Every rule is a named constant with a comment saying where it came from. The
FPL API publishes the game's own settings, so most of them are checked against
it rather than transcribed from a blog post. Four assumptions did not survive
that check:

| Assumption | Reality in 2026/27 |
|---|---|
| Goalkeepers score 6 for a goal | **10** — the same as nothing else in the game |
| Transfer chips are available from GW1 | **GW2**, per the API's own `start_event` |
| Team attack/defence ratings are published | **Zeroed out**, and rescaled 1–5 |
| Historical prices are unavailable | `element-summary[].value` has them, per gameweek |

The last one mattered most, and is the reason the backfill below is honest
rather than approximate.

One value remains unverified: whether a gameweek in which a chip is played
still accrues a free transfer. It is isolated as a single named constant,
`CHIP_GAMEWEEK_ACCRUES_FREE_TRANSFER`, so that when the season proves it either
way there is exactly one line to change. Guessing is fine. Guessing invisibly
is not.

### 2. The data layer

`fplai/data/` turns API payloads into rows and rows into rule types. The FPL
API is public and unauthenticated, which is precisely why the client is careful
with it: every response is cached on disk, requests are spaced by a minimum
interval, server errors back off exponentially, and a 4xx is never retried.

The tests do not touch the network. They run against **real recorded
responses**, trimmed to 43 players chosen at evenly spaced prices so that the
£100m budget actually binds — an early version sampled by index, got nothing
above £6.5m, and would have let a broken budget constraint pass every test.
Blank and double gameweeks are *constructed* rather than recorded, because the
published fixture list has none yet and the code has to handle them anyway.

### 3. The projection

`fplai/model/` estimates expected points per player per fixture, built from
components — minutes, goals, assists, clean sheets, defensive contribution,
bonus — rather than fitted end to end.

That was a deliberate choice with two reasons. Three gameweeks into a new
season is nowhere near enough data to fit something that would beat a
well-specified model. And components are what let the site explain a pick in
terms a person recognises: *7.9 projected, Coventry at home, easy, on
penalties, 0.71 xGI per 90* is a sentence. A gradient-boosted number is not.

Two pieces of machinery do most of the work:

**Shrinkage toward a price-scaled prior.** A midfielder with one goal from 180
minutes is not the best player in the game, but a naive rate says he is. Each
player's rates are pulled toward a prior scaled by their price, which is FPL's
own estimate of their output. `RATE_SHRINKAGE_MINUTES = 450` sets how fast a
player earns the right to be believed; `PRIOR_SEASON_WEIGHT = 0.45` sets how
much last season still counts.

**Derived team strength.** FPL stopped publishing attack and defence ratings
this season, so they are reconstructed from fixture difficulty and actual
results, blended by how much has been played
(`BLEND_HALF_LIFE_MATCHES = 8.0`), shrunk toward the league mean early on
(`SHRINKAGE_MATCHES = 6.0`), with a home multiplier of `1.10`. Clean sheet
probability comes from a Poisson on the resulting expected goals against;
concession deductions are summed over the same distribution rather than applied
to a rounded expectation, because the deduction is stepped and the expectation
is not.

Both models read the *same* projection. That is what makes the comparison mean
anything: they differ in what they are allowed to do with a belief, not in what
they believe.

### 4. The optimiser

`fplai/optimise/squad.py` is an integer linear program (PuLP/CBC) with binary
variables for squad membership, starting XI and captaincy. Constraints are the
literal FPL rules — budget, 2/5/5/3, three per club, a legal formation.

The bench is weighted at `BENCH_WEIGHT = 0.12` rather than zero. A bench valued
at nothing produces four £4.0m players who never play, which is correct for one
gameweek and ruinous the moment somebody is injured. Weighted slightly, the
optimiser buys a bench that can cover.

One test here earns its place by being wrong first. `test_the_strongest_players
_start_rather_than_sit` looked obviously true and failed legitimately: the
formation minimum of one forward genuinely does force a weaker forward into the
XI ahead of a stronger midfielder. It was replaced by two tests that say what
is actually true — no bench player outranks a starter *in their own position*,
and the formation minimum *can* force a weaker player in.

### 5. The two strategies

`Best XI` runs the optimiser on one gameweek and stops.

`The Manager` asks the optimiser for the best squad reachable in 0, 1, 2 and 3
transfers, then takes whichever leaves the most points after hits. A hit must
clear the four points *plus* a margin for the projection being wrong, which it
routinely is. Rolling a transfer is a real answer and often the right one.

Chips each have their own bar rather than sharing one:

```
Bench Boost     16.0     Free Hit        12.0
Triple Captain  10.0     Wildcard        20.0
```

That is a correction. A single shared threshold of 6.0 played Triple Captain in
GW3 and a Wildcard in GW2, which is obviously wrong and was not obvious in
advance. The cause: a premium captain projects six to eight points in an
*ordinary* week, so a six-point bar fires immediately and the chip is gone.
A chip is worth the *excess* over a normal week, and each chip's normal week is
different. `EARLIEST_VOLUNTARY_WILDCARD = 5` stops the Manager wildcarding
before it has seen enough football to have an opinion worth acting on.

---

## The spine: no leakage

The season was already under way when this was built, so GW1 onward had to be
reconstructed. **A gameweek is simulated using only data that existed before
its own deadline.** Not as a convention — the schema enforces it in three
places:

- `player_prices` snapshots cost per gameweek, so a GW7 decision is costed at
  GW7 prices. `load_roster_at_gameweek` is the only way the models read prices.
- `projections` is keyed by `(player, made_for_gameweek, target_gameweek)`, so
  every projection records which deadline it was made before.
- `save_locked_picks` raises `LockedPicksExist` rather than overwriting. Once a
  gameweek's picks are written they are never regenerated.

That last refusal is the load-bearing one. Regenerating a past gameweek with
hindsight would quietly invalidate every result after it, and the result would
still look fine. So it is refused outright rather than left to discipline.

The same rule shapes the schedule: the picking window closes *two minutes
before* a deadline rather than at it, and a deadline missed entirely is
repaired by the backfill — which reconstructs from pre-deadline data — rather
than picked late with knowledge nobody had at the time.

**The honest limit.** Backfilled gameweeks read `status` and
`chance_of_playing` as they are *now*, because the API does not publish their
history. A squad rebuilt for GW1 today knows about an injury that was announced
in September. Prices, results and projections are properly time-boxed; team
news is not. It is the one place the wall is not airtight, and it is worth
knowing when reading early-season numbers.

---

## How it runs

The site is **read-only**. Nobody types into it. The models run on a schedule
and the site shows what they decided. That single fact determines the whole
deployment: it does not need a server at runtime, only something that thinks
once a week and leaves files behind.

Three jobs, all also CLI commands:

| When | What |
|---|---|
| Two hours before a deadline | Refresh, project, lock both squads |
| During matches | Poll live points, apply automatic substitutions |
| After lockdown | Final points, the official average, new prices |

A scheduler decides which are due. It holds **no state between ticks** — each
tick asks the database what is owed, so a restart, a redeploy or a month of
downtime all resolve identically: whatever is unfinished is still due.

It ships three ways, all from the same code:

- **GitHub Actions + Pages** — free, no server. The workflow runs the jobs and
  publishes static files. `fplai export` renders every URL the frontend can ask
  for into a file, driven *through the API itself* so the static copy cannot
  drift from what the live API would say.
- **Docker** — one image serving API, jobs and frontend on one URL.
- **Render** — the same image with a blueprint, if you want it always-on.

Two details in the free path took thought. The season's SQLite file is stored
on a branch as a **single force-pushed commit** rather than appended to: at
half-hourly runs, keeping the history of a binary would add gigabytes over a
season that nobody would ever read a diff of. And `fplai prune` drops the
lookahead projections once their deadline has passed — four fifths of the
database, never read again — verified by a test that publishes the whole site
before and after and compares every file.

---

## Testing

574 tests, no network, no database required for most of them.

```
model 65 · transfers 39 · squad rules 39 · ingest 39 · chips 35 · strategy 33
scoring 31 · scheduler 30 · api 30 · optimiser 27 · autosubs 24 · repository 23
client 22 · projections 21 · backfill 21 · jobs 18 · pricing 17 · captaincy 13
season 12 · assets 11 · export 10 · live 9 · seed 5
```

The weighting is deliberate: the rules and the model carry the most, because
they are where a silent error does the most damage.

Some tests exist to fail on purpose if the world changes.
`test_granular_attack_and_defence_ratings_are_no_longer_published` will fail
the day FPL starts publishing them again, which is exactly when the derived
team strength should be reconsidered. `test_the_default_squad_spans_fifteen
_clubs` guards the test fixtures themselves, after a bug where synthetic
players clustered onto one club and silently tripped the three-per-club limit
in unrelated tests.

---

## What went wrong

Four bugs reached the published site. All four shared a shape worth naming:
**they produced plausible output instead of an error.**

**The database that wasn't there.** The first deployment built an entire season
and lost it. `fplai` was pip-installed on the runner, so its default database
path resolved inside `site-packages`, which the runner discards. Every step
reported success. Fixed twice over — the workflow sets the path explicitly, and
the default now falls back to the working directory rather than writing into an
installed package.

**The season of zeros.** The site published GW1–3 as *final* at nought points.
Nothing in the seed or catch-up path ever called `event/{gw}/live/` — only the
live and finalise jobs did, and each covers a single gameweek. Neither helps a
database that arrives with a season already in progress, which is exactly what
a fresh deployment is. Scoring a squad against no results does not fail; it
returns nought, and a season of zeros reads as a season that went badly.

**The scoring that was skipped.** The fix for the above fetched the results —
and the next line re-read the setup stage to decide whether to score. Fetching
the results had just moved that stage to "ready", so the scoring those results
were fetched for was skipped. A guard invalidated by the line above it.

**The silent 200.** An unknown `/api/…` path fell through to the single-page
app and returned HTML with a 200, which would have been miserable to debug from
the frontend.

What came out of it, beyond the fixes:

- **Stale-score detection.** A score is *derived*, so it can be wrong while
  nothing is missing. The scheduler now compares each score's timestamp against
  the results behind it and re-scores when the results are newer — catching a
  late fixture correction, an interrupted run, or a bug in job ordering alike.
- **Setup stages that gate on results before picks.** A database holding squads
  with no results now reports itself unfinished instead of publishing zeros.
- **An end-to-end seed test.** Both data bugs would have failed it. They got
  through because the pieces were tested and the thing you actually see was
  not.

---

## Where it stands

After four gameweeks of the 2026/27 season:

| | Manager | Best XI |
|---|---|---|
| Season points | 171 | 196 |
| Beat the average | 0 / 4 | 0 / 4 |

The 25-point gap is the measurement the project exists to take, and it is early
enough to mean very little yet. Less comfortably, neither model has beaten the
FPL average yet. Small sample, and GW4 was still in progress at the time of
writing — but it is not noise-free good news either, and it is worth watching
rather than explaining away.

One caveat on continuity: the first deployment lost its database, so GW1–3 were
re-decided from scratch on a later date. Still legitimate — the backfill reads
only pre-deadline data — but those squads were chosen in September, not August,
with the team-news caveat above applying in full. From that point on the picks
are locked and never regenerated.

---

## What would make it better

- **A real backtest.** A held-out prior season would say whether the projection
  is good or merely sensible. Component weights are currently reasoned, not
  fitted.
- **Historical team news.** The one leak. Recording `status` per gameweek from
  now on would close it for future seasons.
- **Blank and double gameweeks against real data.** Handled and tested, but
  only against constructed fixtures — the real ones arrive with the cup draws.
- **Chip thresholds worth trusting.** The current numbers are corrections to an
  obvious failure, not values anybody has validated over a season.
- **Effective ownership.** Neither model knows what anyone else owns, so
  "beating the average" is measured but never played for. A real manager
  captains differently when 60% of the field owns the same player.
