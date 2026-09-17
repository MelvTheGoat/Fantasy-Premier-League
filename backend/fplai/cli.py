"""Command line entry point, so every scheduled job can also be run by hand.

    fplai init-db                     create the schema
    fplai refresh [--gameweek N]      players, teams, prices, fixtures
    fplai history [--limit N]         per-player price history and past seasons
    fplai backfill [--through N]      replay past gameweeks and lock both models
    fplai pick --gameweek N           project and lock one gameweek's picks
    fplai live [--gameweek N]         live points, then rescore
    fplai score [--gameweek N]        rescore stored picks
    fplai finalise --gameweek N       final points and the official average
    fplai status                      what the database currently knows
    fplai seed                        fill an empty database from scratch
    fplai schedule [--once]           run the scheduler, or a single tick
    fplai export --out DIR            write the whole site out as static files
    fplai prune [--before N]          drop lookahead projections already used
    fplai results                     real points for gameweeks already played
    fplai reset --confirm             discard every decision and replay from GW1
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import settings
from .data.client import FPLAPIError, FPLClient
from .data.db import connect, init_db
from .data.repository import (
    current_gameweek,
    finished_gameweeks,
    gameweek_average,
    next_gameweek,
)
from .jobs.backfill import backfill
from .jobs.export import export_site
from .jobs.live import update_live
from .jobs.pick import lock_gameweek
from .jobs.prune import prune_projections
from .jobs.refresh import (
    finalise_gameweek,
    refresh_player_histories,
    refresh_reference,
)
from .jobs.reset import reset_decisions
from .jobs.results import refresh_results
from .jobs.scheduler import TICK_SECONDS, run_forever, tick
from .jobs.score import score_gameweek_for_all_models, score_season, season_summaries
from .jobs.seed import seed, setup_progress


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fplai", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="create the database schema")

    refresh = subparsers.add_parser("refresh", help="refresh players, teams and fixtures")
    refresh.add_argument("--gameweek", type=int, help="gameweek to snapshot prices against")

    history = subparsers.add_parser(
        "history", help="pull per-player price history and past seasons"
    )
    history.add_argument("--limit", type=int, help="only fetch this many players")

    fill = subparsers.add_parser(
        "backfill", help="replay past gameweeks and lock both models' picks"
    )
    fill.add_argument("--through", type=int, help="stop after this gameweek")
    fill.add_argument("--horizon", type=int, help="gameweeks to project ahead")

    pick = subparsers.add_parser(
        "pick", help="project and lock picks for one gameweek, before its deadline"
    )
    pick.add_argument("--gameweek", type=int, required=True)
    pick.add_argument("--horizon", type=int)

    live = subparsers.add_parser("live", help="pull live points, then rescore")
    live.add_argument("--gameweek", type=int)

    score = subparsers.add_parser("score", help="rescore stored picks")
    score.add_argument("--gameweek", type=int, help="default: every locked gameweek")

    finalise = subparsers.add_parser("finalise", help="finalise a gameweek after lockdown")
    finalise.add_argument("--gameweek", type=int, required=True)

    subparsers.add_parser("status", help="show what the database currently knows")

    seeding = subparsers.add_parser(
        "seed", help="fill an empty database: reference data, history, backfill, scores"
    )
    seeding.add_argument(
        "--limit", type=int, help="only fetch this many players' histories"
    )

    schedule = subparsers.add_parser(
        "schedule", help="run the scheduler in the foreground"
    )
    schedule.add_argument(
        "--interval", type=int, default=TICK_SECONDS, help="seconds between ticks"
    )
    schedule.add_argument(
        "--once",
        action="store_true",
        help="run a single tick and exit, for an external scheduler",
    )

    export = subparsers.add_parser(
        "export", help="write the whole site out as static files"
    )
    export.add_argument("--out", type=Path, required=True, help="directory to write")
    export.add_argument(
        "--frontend",
        type=Path,
        default=settings.frontend_dist,
        help="built frontend to publish alongside the API",
    )

    subparsers.add_parser(
        "results", help="pull real points for gameweeks that have been played"
    )

    reset = subparsers.add_parser(
        "reset",
        help="discard every model decision so the season can be replayed",
    )
    reset.add_argument(
        "--confirm",
        action="store_true",
        help="required: this destroys every stored pick, transfer and score",
    )

    prune = subparsers.add_parser(
        "prune", help="drop lookahead projections for gameweeks already played"
    )
    prune.add_argument(
        "--before",
        type=int,
        help="keep everything from this gameweek on (default: the next one)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    settings.ensure_directories()
    connection = connect()
    init_db(connection)

    try:
        return _dispatch(args, connection)
    except FPLAPIError as error:
        print(f"FPL API error: {error}", file=sys.stderr)
        return 1
    finally:
        connection.close()


def _dispatch(args, connection) -> int:
    """Route to the job. Commands that need the network open a client; the
    rest work entirely from what has already been ingested."""
    if args.command == "schedule" and not args.once:
        # Opens its own connection and its own client each tick, because it
        # outlives this call rather than being one job.
        print(f"scheduler running, ticking every {args.interval}s -- ^C to stop")
        run_forever(interval=args.interval)
        return 0

    offline = {
        "init-db": _init_db,
        "status": _status,
        "backfill": _backfill,
        "pick": _pick,
        "score": _score,
        "export": _export,
        "prune": _prune,
        "reset": _reset,
    }
    if args.command in offline:
        return offline[args.command](args, connection)

    with FPLClient() as client:
        if args.command == "refresh":
            counts = refresh_reference(connection, client, gameweek=args.gameweek)
            gameweek = args.gameweek or next_gameweek(connection)
            print(
                f"refreshed for GW{gameweek}: "
                + ", ".join(f"{key}={value}" for key, value in counts.items())
            )
            return 0

        if args.command == "history":
            elements = None
            if args.limit:
                elements = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM players ORDER BY id LIMIT ?", (args.limit,)
                    )
                ]
            counts = refresh_player_histories(connection, client, elements=elements)
            print(
                f"{counts['players']} players, {counts['prices']} price rows, "
                f"{counts['seasons']} past seasons"
            )
            return 0

        if args.command == "live":
            result = update_live(connection, client, gameweek=args.gameweek)
            if result["gameweek"] is None:
                print("no current gameweek")
                return 1
            scores = ", ".join(f"{m}={p}" for m, p in result["scores"].items())
            label = "final" if result["final"] else "provisional"
            print(f"GW{result['gameweek']}: {result['rows']} rows, {scores} ({label})")
            return 0

        if args.command == "schedule":
            done = tick(connection, client)
            print(", ".join(str(job) for job in done) if done else "nothing due")
            return 0

        if args.command == "results":
            counts = refresh_results(connection, client)
            score_season(connection)
            print(f"{counts['gameweeks']} gameweeks, {counts['rows']} rows")
            return 0

        if args.command == "seed":
            progress = seed(connection, client, history_limit=args.limit)
            print(f"{progress['message'].lower()}: {progress['stage']}")
            return 0

        if args.command == "finalise":
            if finalise_gameweek(connection, client, args.gameweek):
                score_gameweek_for_all_models(connection, args.gameweek)
                average = gameweek_average(connection, args.gameweek)
                print(f"GW{args.gameweek} finalised; official average {average}")
                return 0
            print(f"GW{args.gameweek} has not reached lockdown yet")
            return 1

    return 0


def _init_db(args, connection) -> int:
    print(f"schema applied to {settings.database_path}")
    return 0


def _backfill(args, connection) -> int:
    summary = backfill(connection, through_gameweek=args.through, horizon=args.horizon)
    if not summary:
        print("nothing to backfill yet")
        return 0
    for gameweek, entry in summary.items():
        if entry.get("skipped"):
            print(f"GW{gameweek}: already locked")
        else:
            print(f"GW{gameweek}: {entry}")
    return 0


def _pick(args, connection) -> int:
    result = lock_gameweek(connection, args.gameweek, horizon=args.horizon)
    print(
        f"GW{args.gameweek} locked: Best XI {result['best_xi_expected']:.1f} "
        f"projected, Manager made {int(result['manager_transfers'])} transfer(s)"
    )
    return 0


def _score(args, connection) -> int:
    if args.gameweek:
        for model, score in score_gameweek_for_all_models(
            connection, args.gameweek
        ).items():
            print(
                f"GW{args.gameweek} {model}: {score.points} pts ({score.status_label})"
            )
    else:
        score_season(connection)

    for model, summary in season_summaries(connection).items():
        print(
            f"{model:<9} {summary.total_points} pts over "
            f"{summary.gameweeks_played} gameweeks, beat the average "
            f"{summary.gameweeks_beating_average} times, hits "
            f"{summary.total_transfer_cost}"
        )
    return 0


def _export(args, connection) -> int:
    counts = export_site(connection, args.out, frontend_dist=args.frontend)
    print(
        f"{args.out}: {counts['api_files']} API files, "
        f"{counts['frontend_files']} frontend files"
    )
    return 0


def _reset(args, connection) -> int:
    if not args.confirm:
        print(
            "refusing to reset without --confirm: this discards every stored\n"
            "pick, transfer, chip and score. The observed data -- results,\n"
            "prices per gameweek, prior seasons -- is kept.",
            file=sys.stderr,
        )
        return 1

    removed = reset_decisions(connection)
    for table, count in removed.items():
        print(f"  cleared {count:>6} rows from {table}")
    print("replay with `fplai backfill`")
    return 0


def _prune(args, connection) -> int:
    before = args.before or next_gameweek(connection) or settings.final_gameweek
    counts = prune_projections(connection, before)
    print(f"dropped {counts['deleted']} lookahead projections made before GW{before}")
    return 0


def _status(args, connection) -> int:
    players = connection.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    fixtures = connection.execute("SELECT COUNT(*) c FROM fixtures").fetchone()["c"]
    locked = connection.execute(
        "SELECT COUNT(DISTINCT gameweek) c FROM locked_picks"
    ).fetchone()["c"]
    finished = finished_gameweeks(connection)

    progress = setup_progress(connection)

    print(f"database:  {settings.database_path}")
    print(f"setup:     {progress['message']}")
    print(f"players:   {players}")
    print(f"fixtures:  {fixtures}")
    print(f"current:   GW{current_gameweek(connection)}")
    print(f"next:      GW{next_gameweek(connection)}")
    print(f"finished:  {len(finished)} gameweeks")
    print(f"locked:    {locked} gameweeks of picks")

    if not progress["ready"]:
        print("\nnot ready yet -- run `fplai seed`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
