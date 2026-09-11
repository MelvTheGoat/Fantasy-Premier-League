"""Runtime configuration, all overridable by environment variable."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser() if value else default


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    database_path: Path = _env_path("FPLAI_DB", PROJECT_ROOT / "data" / "fplai.sqlite3")
    cache_dir: Path = _env_path("FPLAI_CACHE", PROJECT_ROOT / "data" / "cache")

    fpl_base_url: str = os.environ.get(
        "FPLAI_FPL_BASE_URL", "https://fantasy.premierleague.com/api"
    )

    #: Minimum seconds between requests to the FPL API. The API is public and
    #: unauthenticated, so the only polite thing to do is to go slowly.
    min_request_interval: float = _env_float("FPLAI_MIN_REQUEST_INTERVAL", 1.0)

    #: How long a cached response stays fresh. Reference data barely moves
    #: between deadlines; live data is fetched with a much shorter ttl by the
    #: live-update job, which passes its own value.
    cache_ttl_seconds: int = _env_int("FPLAI_CACHE_TTL", 3600)

    request_timeout: float = _env_float("FPLAI_REQUEST_TIMEOUT", 30.0)
    max_retries: int = _env_int("FPLAI_MAX_RETRIES", 4)

    #: How many gameweeks ahead the projection model looks. The Manager needs a
    #: horizon to judge whether a hit pays for itself.
    planning_horizon: int = _env_int("FPLAI_PLANNING_HORIZON", 5)

    #: Extra projected points a transfer must clear, on top of the four-point
    #: hit, before the Manager will take it. Covers projection uncertainty.
    hit_margin: float = _env_float("FPLAI_HIT_MARGIN", 2.0)

    final_gameweek: int = _env_int("FPLAI_FINAL_GAMEWEEK", 38)

    #: Whether the web process also runs the scheduled jobs. Off by default,
    #: because a developer running the API locally does not want it reaching
    #: out to the FPL API on a timer; on in the container image, where there is
    #: nowhere else for the jobs to run.
    run_scheduler: bool = _env_flag("FPLAI_SCHEDULER", False)

    #: Seconds between scheduler ticks.
    scheduler_interval: int = _env_int("FPLAI_SCHEDULER_INTERVAL", 300)

    #: The built frontend. When present, the API serves it too, so the whole
    #: site runs as one service on one URL.
    frontend_dist: Path = _env_path(
        "FPLAI_FRONTEND_DIST", PROJECT_ROOT.parent / "frontend" / "dist"
    )

    #: Base for club shirt images. The team *code* (not the team id) and the
    #: goalkeeper `_1` variant are substituted in by `fplai.data.assets`.
    #: Confirmed against the live CDN.
    shirt_base_url: str = os.environ.get(
        "FPLAI_SHIRT_BASE_URL",
        "https://fantasy.premierleague.com/dist/img/shirts/standard",
    )

    def ensure_directories(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


settings = Settings()

__all__ = ["PROJECT_ROOT", "Settings", "settings"]
