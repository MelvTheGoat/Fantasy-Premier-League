# FPL AI Manager

Two models that play Fantasy Premier League for the 2026/27 season, scored with
real FPL points and tracked against the official gameweek average.

- **The Manager** carries one squad all season under real constraints: one free
  transfer a week, −4 hits, a £100m budget with proper selling prices, and two
  sets of chips.
- **Best XI of the Week** rebuilds the best legal squad from scratch every
  gameweek, with no transfers, no hits and no chips.

They are not competing. Running both side by side shows what realistic
management costs against perfect freedom.

## Status

Built so far, with tests:

- [x] **Data ingestion** — FPL API client, SQLite schema, ingestion with blank
      and double gameweek detection
- [x] **Rules engine** — squad validity, formations, automatic substitutions,
      captaincy, selling prices, transfer costs and chip constraints
- [ ] Expected-points (xPts) model
- [ ] Best XI optimiser
- [ ] The Manager: transfers, hits, chips and explanations
- [ ] Backfill from GW1 with no leakage
- [ ] Performance tracking
- [ ] Frontend
- [ ] Scheduled jobs and live updates

See [`docs/rules-sources.md`](docs/rules-sources.md) for where every rule came
from, and for the three values that could not be verified against the official
source at build time.

## Setup

```sh
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
```

Run the tests:

```sh
pytest
```

They need no network and no database — the rules engine is pure logic, and
ingestion runs against recorded API payloads in `tests/fixtures/`.

## Running the jobs by hand

Every scheduled job is also a CLI command:

```sh
fplai init-db                 # create the schema
fplai refresh                 # players, teams, prices, fixtures
fplai refresh --gameweek 6    # snapshot prices against a specific gameweek
fplai live                    # live points for the current gameweek
fplai live --gameweek 5
fplai finalise --gameweek 5   # final points and the official average, after lockdown
fplai status                  # what the database currently knows
```

`finalise` refuses to run before lockdown — 09:00 UK time on the day after the
gameweek's last match — and exits non-zero, so it can be scheduled
optimistically and will simply do nothing when it is early.

### The three scheduled jobs

| When | Command | What it does |
|---|---|---|
| Before each deadline | `fplai refresh --gameweek N` | Refresh data, snapshot prices, then generate projections and lock both models' picks |
| During the gameweek | `fplai live` | Poll live points; apply automatic substitutions as matches complete |
| After lockdown | `fplai finalise --gameweek N` | Final points, the official gameweek average, season totals and new prices |

## Configuration

Everything is an environment variable with a sensible default (see
`backend/fplai/config.py`):

| Variable | Default | Purpose |
|---|---|---|
| `FPLAI_DB` | `backend/data/fplai.sqlite3` | Database location |
| `FPLAI_CACHE` | `backend/data/cache` | On-disk API response cache |
| `FPLAI_MIN_REQUEST_INTERVAL` | `1.0` | Minimum seconds between API requests |
| `FPLAI_CACHE_TTL` | `3600` | Default cache freshness, in seconds |
| `FPLAI_PLANNING_HORIZON` | `5` | Gameweeks the projections look ahead |
| `FPLAI_HIT_MARGIN` | `2.0` | Points a transfer must clear *above* the 4-point hit |
| `FPLAI_SHIRT_BASE_URL` | — | Club shirt CDN; **unverified**, see the rules doc |

The API is public and unauthenticated, which is exactly why the client is
careful with it: every response is cached on disk, requests are spaced by
`FPLAI_MIN_REQUEST_INTERVAL`, server errors back off exponentially, and a 4xx
is never retried.

## How the backfill works

The season is already under way, so GW1 to the current gameweek have to be
reconstructed. The rule is that **a gameweek is simulated using only data that
existed before its own deadline**, and the schema enforces it in two places:

- `player_prices` snapshots `now_cost` per gameweek, so a GW7 decision is
  costed at GW7 prices rather than today's. `load_roster_at_gameweek` is the
  only way the models read prices.
- `projections` is keyed by `(player, made_for_gameweek, target_gameweek)`, so
  a projection records which deadline it was made before.

Once a gameweek's picks are written to `locked_picks` they are never
regenerated: `save_locked_picks` raises `LockedPicksExist` rather than
overwriting. Regenerating a past gameweek with hindsight would quietly
invalidate every result after it, so it is refused outright rather than left to
a convention.

## Layout

```
backend/
  fplai/
    rules/          the rules engine -- pure logic, no I/O
      constants.py    every FPL rule as a named constant
      squad.py        squad validity and formations
      autosubs.py     automatic substitutions
      captaincy.py    the armband, including Triple Captain
      pricing.py      selling prices
      transfers.py    free transfers, hits, applying transfers
      chips.py        the two chip sets and their expiry
      scoring.py      assembling a gameweek score from real points
    data/
      client.py       cached, rate-limited FPL API client
      ingest.py       API payloads -> database rows
      repository.py   database rows -> rules types
      schema.sql      the schema, with the reasoning in comments
    jobs/refresh.py   the three scheduled jobs
    cli.py            run any job by hand
  tests/            one test module per rule, plus integration tests
docs/
  rules-sources.md  where each rule came from, and what is unverified
```

The rules engine has no imports from `data`, and the data layer has no rule
logic. That separation is what lets every rule be tested without a database.

## Deployment

Not yet wired up. The backend is a FastAPI app and a SQLite file, so it runs on
anything that can hold a disk; the three jobs above want a scheduler (cron,
systemd timers, or a hosted equivalent). This section will be filled in once
the API and frontend exist.
