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

Complete and running against the live FPL API.

- [x] Data ingestion — cached, rate-limited API client, SQLite schema, blank and
      double gameweek detection
- [x] Rules engine — squad validity, formations, automatic substitutions,
      captaincy, selling prices, transfer costs, chip constraints
- [x] Expected-points model — minutes, attacking returns, clean sheets, DefCon,
      bonus, over a multi-gameweek horizon
- [x] Best XI optimiser
- [x] The Manager — transfers, hits, chips, and an explanation for each
- [x] Backfill from GW1 with no leakage
- [x] Performance tracking against the official gameweek average
- [x] Frontend — half pitch, two tabs, gameweek history, player detail sheets
- [x] Scheduled jobs and live updates
- [x] One-command deployment that seeds and runs itself

Every rule is verified against the live FPL API, which publishes the game's own
settings — see [`docs/rules-sources.md`](docs/rules-sources.md) for the field
behind each one, two API changes for 2026/27 that invalidate older approaches,
and the single value still unconfirmed.

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

They need no network and no database. The rules engine is pure logic, and
ingestion runs against real recorded API responses in `tests/fixtures/`,
refreshed with:

```sh
python tests/fixtures/record_fixtures.py           # real, trimmed responses
python tests/fixtures/build_synthetic_fixtures.py  # blank/double gameweeks
```

Blank and double gameweeks are constructed rather than recorded, because the
published fixture list has none yet this season.

## Getting started

From an empty database to a working site:

```sh
cd backend
fplai seed           # everything, in the only order that works
python -m fplai.api.app
```

`seed` is the four steps below run in sequence, and each is skipped if its work
is already done, so an interrupted seed picks up where it stopped:

```sh
fplai refresh        # players, teams, prices, fixtures       (~2s)
fplai history        # per-player price history, past seasons (~1-10 min)
fplai backfill       # replay every gameweek so far           (~4s)
fplai score          # score both models against the average
```

```sh
cd frontend
npm install && npm run dev     # http://localhost:5173
```

`fplai history` is the slow one: it is a request per player, paced so the FPL
API is not hammered. It only needs running once, because a past gameweek's
prices never change.

## Running the jobs by hand

Every scheduled job is also a CLI command:

```sh
fplai init-db                 # create the schema
fplai refresh [--gameweek N]  # players, teams, prices, fixtures
fplai history [--limit N]     # per-player price history and past seasons
fplai backfill [--through N]  # replay past gameweeks and lock both models
fplai pick --gameweek N       # project and lock one gameweek, before its deadline
fplai live [--gameweek N]     # live points, then rescore
fplai score [--gameweek N]    # rescore stored picks
fplai finalise --gameweek N   # final points and the official average
fplai status                  # what the database currently knows
fplai seed                    # fill an empty database from scratch
fplai schedule [--once]       # run the scheduler, or a single tick and exit
fplai export --out DIR        # write the whole site out as static files
fplai prune                   # drop lookahead projections already used
```

`finalise` refuses to run before lockdown — 09:00 UK time on the day after the
gameweek's last match — and exits non-zero, so it can be scheduled
optimistically and will simply do nothing when it is early.

### The scheduler

In a deployment the jobs run inside the web process, on a timer, and there is
nothing to schedule by hand. That is not a preference: a host attaches a
persistent disk to exactly one service, and a platform cron job is a separate
service with its own empty filesystem, so it could not see the database the
jobs read and write.

The scheduler holds no state between ticks. Every five minutes it asks the
database what is owed and does that, so a restart, a redeploy or a week of
downtime all resolve the same way -- whatever is unfinished is simply still
due. A deadline that went by while the site was down is repaired by the
backfill, which reconstructs the gameweek from the data that existed before it,
rather than picked now with hindsight.

`FPLAI_SCHEDULER=1` turns it on. It is on in the container image and off
locally, so running the API on a laptop does not start calling the FPL API on a
timer. To watch it work, `fplai schedule` runs the same loop in the foreground.

### The three jobs it runs

| When | Command | What it does |
|---|---|---|
| Before each deadline | `fplai refresh && fplai pick --gameweek N` | Refresh data, snapshot prices, project, and lock both models' picks |
| During the gameweek | `fplai live` | Poll live points; apply automatic substitutions as matches complete |
| After lockdown | `fplai finalise --gameweek N` | Final points, the official gameweek average, season totals and new prices |

Each is also a CLI command, so a host with its own scheduler can drive them
from outside instead. A workable crontab, with the deadline job an hour early
so a late team-news change is still picked up:

```cron
# Refresh and lock picks before Saturday's deadline
0 12 * * SAT  cd /srv/fplai/backend && fplai refresh && fplai pick --gameweek $(fplai status | awk '/next/{print $2}' | tr -d GW)
# Poll live scores through match days
*/10 12-23 * * SAT,SUN  cd /srv/fplai/backend && fplai live
# Finalise after lockdown; exits non-zero and does nothing if it is early
30 9 * * *    cd /srv/fplai/backend && fplai finalise --gameweek N
```

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
| `FPLAI_SHIRT_BASE_URL` | FPL shirt CDN | Club shirt images, keyed by team code |
| `FPLAI_SCHEDULER` | `0` | Run the scheduled jobs in the web process |
| `FPLAI_SCHEDULER_INTERVAL` | `300` | Seconds between scheduler ticks |
| `FPLAI_FRONTEND_DIST` | `frontend/dist` | Built frontend to serve, if present |

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

## How the models decide

Both pick from the same projection, so the only difference between them is
what they are allowed to do with it.

**The expected-points model** is built from components rather than fitted end
to end — three gameweeks into a new season is nowhere near enough data to fit
something that would beat a well-specified model, and components are what let
the frontend explain a pick in terms a person recognises. Each player's rates
are shrunk toward a prior scaled by their price, which is FPL's own estimate of
their output, so a midfielder with one goal from 180 minutes is not projected
as the best player in the game. Team strength is derived from fixture
difficulty and actual results, blended by how much has been played, because
FPL stopped publishing attack and defence ratings this season.

**Best XI** runs the optimiser on one gameweek and stops.

**The Manager** asks the optimiser for the best squad reachable in 0, 1, 2 and
3 transfers, then takes whichever leaves the most points after hits. A hit has
to clear four points *plus* a margin for the projection being wrong, which it
routinely is. Rolling a transfer is a real answer and often the right one.
Chips each have their own bar rather than a shared one: tripling a captain is
worth the captain's score, and a premium captain projects six to eight points
in an ordinary week, so a shared threshold would fire Triple Captain in week
one and waste it.

## Layout

```
backend/
  fplai/
    rules/          the rules engine -- pure logic, no I/O
      constants.py    every FPL rule as a named constant
      scoring_table.py  what each action scores, read from the API
      squad.py        squad validity and formations
      autosubs.py     automatic substitutions
      captaincy.py    the armband, including Triple Captain
      pricing.py      selling prices
      transfers.py    free transfers, hits, applying transfers
      chips.py        the two chip sets, their windows and expiry
      scoring.py      assembling a gameweek score from real points
    model/          expected points
      team_strength.py  attack and defence, derived not published
      player_rates.py   minutes, returns and DefCon, with shrinkage
      xpts.py           points per fixture, split by source
      projections.py    the pipeline, and the no-leakage rule
    optimise/squad.py   ILP squad and lineup selection
    strategy/
      best_xi.py      Model B: best squad from scratch each week
      manager.py      Model A: one squad, transfers, hits, chips
      explain.py      the reasons behind every pick and transfer
    data/
      client.py       cached, rate-limited FPL API client
      ingest.py       API payloads -> database rows
      repository.py   database rows -> rules types
      assets.py       club shirt image URLs
      schema.sql      the schema, with the reasoning in comments
    jobs/           refresh, backfill, live, score
    api/            the read-only HTTP API
    cli.py          run any job by hand
  tests/            one module per area, ~490 tests
frontend/
  src/
    App.jsx           tabs, gameweek selector, live polling
    components/       pitch, player, score card, transfers, season, sheet
docs/
  rules-sources.md  where each rule came from, and what is unverified
```

The rules engine has no imports from `data`, and the data layer has no rule
logic. That separation is what lets every rule be tested without a database.

## API

Read-only, and small on purpose: the models run as scheduled jobs and this
serves what they already decided, so a slow request can never delay a deadline.

| Endpoint | Returns |
|---|---|
| `GET /api/gameweeks` | Every gameweek, and which one to open on |
| `GET /api/{model}/gameweek/{gw}` | Squad, points, auto-subs, transfers, explanations |
| `GET /api/{model}/season` | Totals, beat-the-average tally, cumulative series |
| `GET /api/{model}/gameweek/{gw}/player/{id}` | The player detail sheet |
| `GET /api/summary` | Both models side by side |

`{model}` is `manager` or `best_xi`.

## Deployment

Once the frontend is built, the API serves it too, so the whole site is **one
service on one URL** — no CORS, no separate static host:

```sh
cd frontend && npm run build        # -> frontend/dist
cd backend && python -m fplai.api.app
```

Open `http://localhost:8000`. In development Vite serves the frontend itself
and proxies `/api` to port 8000, so the frontend uses same-origin paths in both
cases and nothing changes between them.

### GitHub Actions and Pages

The deployment that costs nothing. There is no server, because the site is
read-only: it does not need one at runtime, only something that thinks once a
week and leaves files behind. `.github/workflows/season.yml` does that on
GitHub's runners, which are [free and unlimited for public
repositories](https://docs.github.com/en/actions/concepts/billing-and-usage).

Two settings, once:

1. **Settings → Pages → Source: GitHub Actions.**
2. **Settings → Actions → General → Workflow permissions: Read and write.**

Then run the workflow (Actions → Season → Run workflow). The first run finds no
database, builds one from scratch, and publishes to
`https://<user>.github.io/<repo>/`. Every half hour after that it asks the
database what is due, does it, and republishes.

Three things about it are worth knowing, because they are not the obvious
choices:

**It runs every half hour rather than aiming at deadlines.** Scheduled
workflows are queued, not guaranteed, and GitHub's scheduler runs late under
load. The picking window is two hours wide so several runs land inside it. If
every one of them is missed, the next run repairs it through the backfill,
which reconstructs the gameweek from data that existed before its own deadline
rather than picking with hindsight.

**The season lives on a `season-data` branch, force-pushed as one commit.** The
record is a SQLite file, and it has to outlive the runner. Keeping its history
at half-hourly granularity would add gigabytes to the repository over a season
for a binary nobody would ever read a diff of, so each run replaces it. The
backup is the artifact every run uploads, kept for 30 days.

**`fplai prune` runs before the file is stored.** The Manager projects several
gameweeks ahead to judge whether a hit pays for itself, which is four fifths of
the database and is spent the moment the deadline passes. The only projection
read again is the gameweek's own, behind a tap on a player, and that stays.
`test_pruning_changes_nothing_the_site_shows` is the guarantee: it publishes
the site before and after and compares every file.

The one maintenance task: [GitHub disables scheduled workflows after 60 days of
repository
inactivity](https://docs.github.com/actions/managing-workflow-runs/disabling-and-enabling-a-workflow),
which the summer between seasons will trigger. Re-enable it in August.

Because the repository is public, so is the published database. It holds picks
and scores, and nothing else.

### Render

### With Docker

```sh
docker build -t fplai .
docker run -p 8000:8000 -v fplai-data:/data fplai
```

The volume matters: the season's entire record is one SQLite file, and a
container without it starts empty. Back up that file and everything travels
with it.

The container seeds itself on first boot and runs the jobs on a timer, so there
is nothing to run by hand. To do it explicitly instead — to watch it, or to
seed a partial database — every job is a command:

```sh
docker exec -it <container> fplai seed      # reference data, history, backfill, scores
docker exec -it <container> fplai status    # what the database currently knows
```

### Hosting it elsewhere

Anything that runs a container with a persistent disk will do. The app is one
small process with a database measured in megabytes, so the smallest tier of
anything is enough. What it needs:

- **A persistent volume** mounted at `/data`. Without one, every restart wipes
  the season. This is the one that bites: everything else is recoverable.
- **Outbound HTTPS** to `fantasy.premierleague.com`.
- **One instance**, not several. A second would be a second writer to the same
  SQLite file.

`PORT` is read from the environment, which is what most platforms set, and
`FPLAI_SCHEDULER=1` — already set in the image — is what makes the process run
the jobs as well as serve the site. A host that would rather drive the jobs
itself can set it to `0` and run the CLI commands above on its own schedule.
