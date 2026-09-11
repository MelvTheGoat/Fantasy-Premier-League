"""Command line entry point, so every scheduled job can also be run by hand.

    fplai init-db
    fplai refresh [--gameweek N]
    fplai live [--gameweek N]
    fplai finalise --gameweek N
    fplai status
"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import settings
from .data.client import FPLAPIError, FPLClient
from .data.db import connect, init_db
from .data.repository import (
    current_gameweek,
    finished_gameweeks,
    gameweek_average,
    next_gameweek,
)
from .jobs.refresh import finalise_gameweek, refresh_live, refresh_reference


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fplai", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init-db", help="create the database schema")

    refresh = subparsers.add_parser("refresh", help="refresh players, teams and fixtures")
    refresh.add_argument("--gameweek", type=int, help="gameweek to snapshot prices against")

    live = subparsers.add_parser("live", help="pull live points for a gameweek")
    live.add_argument("--gameweek", type=int)

    finalise = subparsers.add_parser("finalise", help="finalise a gameweek after lockdown")
    finalise.add_argument("--gameweek", type=int, required=True)

    subparsers.add_parser("status", help="show what the database currently knows")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

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
    if args.command == "init-db":
        print(f"schema applied to {settings.database_path}")
        return 0

    if args.command == "status":
        return _status(connection)

    with FPLClient() as client:
        if args.command == "refresh":
            # Left unset, the job reads the target gameweek from the payload,
            # which is the only thing that works on a cold database.
            counts = refresh_reference(connection, client, gameweek=args.gameweek)
            gameweek = args.gameweek or next_gameweek(connection)
            print(f"refreshed for GW{gameweek}: " + ", ".join(
                f"{k}={v}" for k, v in counts.items()
            ))
            return 0

        if args.command == "live":
            rows = refresh_live(connection, client, args.gameweek)
            print(f"{rows} live rows written")
            return 0

        if args.command == "finalise":
            if finalise_gameweek(connection, client, args.gameweek):
                average = gameweek_average(connection, args.gameweek)
                print(f"GW{args.gameweek} finalised; official average {average}")
                return 0
            print(f"GW{args.gameweek} has not reached lockdown yet")
            return 1

    return 0


def _status(connection) -> int:
    players = connection.execute("SELECT COUNT(*) c FROM players").fetchone()["c"]
    fixtures = connection.execute("SELECT COUNT(*) c FROM fixtures").fetchone()["c"]
    finished = finished_gameweeks(connection)

    print(f"database:  {settings.database_path}")
    print(f"players:   {players}")
    print(f"fixtures:  {fixtures}")
    print(f"current:   GW{current_gameweek(connection)}")
    print(f"next:      GW{next_gameweek(connection)}")
    print(f"finished:  {len(finished)} gameweeks")
    if not players:
        print("\nnothing ingested yet -- run `fplai refresh`")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
