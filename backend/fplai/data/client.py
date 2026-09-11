"""A cached, rate-limited client for the public FPL API.

The API is unauthenticated and free, which is exactly why it deserves care: a
backfill touches `element-summary/` once per player, so several hundred
requests in a row. Every response is written to disk and re-read while it is
still fresh, and requests are spaced by a minimum interval.

Endpoints used, all relative to `https://fantasy.premierleague.com/api`:

    bootstrap-static/        players, teams, positions, prices, gameweeks,
                             gameweek averages and chip definitions
    fixtures/                every fixture, which is how blanks and doubles
                             are detected
    event/{gw}/live/         per-player points and stats for one gameweek
    element-summary/{id}/    one player's history and remaining fixtures

These are the documented public endpoints, but they are not a contract -- the
shapes are validated on ingest rather than trusted.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, settings as default_settings

logger = logging.getLogger(__name__)


class FPLAPIError(RuntimeError):
    """Raised when the FPL API cannot be reached or returns something unusable."""


class RateLimiter:
    """Spaces requests by at least `min_interval` seconds.

    Thread-safe because the live-update job may fetch several gameweeks in
    parallel and they must share one budget.
    """

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._lock = threading.Lock()
        #: None until the first request, so the first one is never delayed.
        #: A 0.0 sentinel would compare against monotonic()'s arbitrary epoch.
        self._last_request: float | None = None

    def wait(self) -> None:
        with self._lock:
            if self._last_request is not None:
                remaining = self.min_interval - (time.monotonic() - self._last_request)
                if remaining > 0:
                    time.sleep(remaining)
            self._last_request = time.monotonic()


class ResponseCache:
    """A plain on-disk JSON cache keyed by URL.

    Entries carry the time they were written so a caller can ask for a tighter
    freshness window than the default -- the live job wants a minute, the
    pre-deadline job is happy with an hour.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode()).hexdigest()[:32]
        return self.directory / f"{digest}.json"

    def get(self, key: str, ttl_seconds: int) -> Any | None:
        path = self._path(key)
        if not path.exists():
            return None
        if ttl_seconds <= 0:
            return None
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if time.time() - payload.get("cached_at", 0) > ttl_seconds:
            return None
        return payload.get("body")

    def set(self, key: str, body: Any) -> None:
        path = self._path(key)
        # Written to a sibling then moved, so a crash mid-write cannot leave a
        # truncated file that later parses as valid-but-wrong JSON.
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"cached_at": time.time(), "key": key, "body": body}))
        temporary.replace(path)

    def clear(self) -> int:
        removed = 0
        for path in self.directory.glob("*.json"):
            path.unlink()
            removed += 1
        return removed


class FPLClient:
    """Fetches FPL API resources, caching and pacing as it goes."""

    def __init__(
        self,
        config: Settings | None = None,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = config or default_settings
        self.cache = ResponseCache(self.settings.cache_dir)
        self.limiter = RateLimiter(self.settings.min_request_interval)
        self._client = httpx.Client(
            base_url=self.settings.fpl_base_url,
            timeout=self.settings.request_timeout,
            transport=transport,
            headers={"User-Agent": "fpl-ai-manager/0.1 (+https://github.com/)"},
            follow_redirects=True,
        )

    def __enter__(self) -> FPLClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # --- fetching ---------------------------------------------------------

    def get(self, path: str, *, ttl_seconds: int | None = None) -> Any:
        """GET a path relative to the API base, via the cache."""
        ttl = self.settings.cache_ttl_seconds if ttl_seconds is None else ttl_seconds
        cached = self.cache.get(path, ttl)
        if cached is not None:
            logger.debug("cache hit for %s", path)
            return cached

        body = self._fetch_with_retries(path)
        self.cache.set(path, body)
        return body

    def _fetch_with_retries(self, path: str) -> Any:
        last_error: Exception | None = None

        for attempt in range(self.settings.max_retries):
            self.limiter.wait()
            try:
                response = self._client.get(path)
            except httpx.HTTPError as exc:
                last_error = exc
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        # A 200 that is not JSON usually means an edge cache or
                        # a maintenance page, which a retry may get past.
                        last_error = FPLAPIError(f"{path} returned non-JSON: {exc}")
                elif response.status_code == 429:
                    last_error = FPLAPIError(f"{path} rate limited")
                elif 500 <= response.status_code < 600:
                    last_error = FPLAPIError(f"{path} returned {response.status_code}")
                else:
                    # A 4xx other than 429 will not fix itself.
                    raise FPLAPIError(f"{path} returned {response.status_code}")

            backoff = 2**attempt
            logger.warning(
                "fetch of %s failed (attempt %d/%d): %s; retrying in %ds",
                path, attempt + 1, self.settings.max_retries, last_error, backoff,
            )
            time.sleep(backoff)

        raise FPLAPIError(f"gave up fetching {path}: {last_error}")

    # --- endpoints --------------------------------------------------------

    def bootstrap_static(self, *, ttl_seconds: int | None = None) -> dict[str, Any]:
        """Players, teams, positions, prices, gameweeks and gameweek averages."""
        return self.get("bootstrap-static/", ttl_seconds=ttl_seconds)

    def fixtures(self, *, gameweek: int | None = None, ttl_seconds: int | None = None) -> list[dict]:
        """Every fixture, or just one gameweek's.

        Fetched whole by default, because detecting blanks and doubles means
        counting each club's fixtures across the season rather than one week.
        """
        path = "fixtures/" if gameweek is None else f"fixtures/?event={gameweek}"
        return self.get(path, ttl_seconds=ttl_seconds)

    def event_live(self, gameweek: int, *, ttl_seconds: int | None = None) -> dict[str, Any]:
        """Live and final per-player points and stats for one gameweek."""
        return self.get(f"event/{gameweek}/live/", ttl_seconds=ttl_seconds)

    def element_summary(self, element: int, *, ttl_seconds: int | None = None) -> dict[str, Any]:
        """One player's per-gameweek history, past seasons and coming fixtures."""
        return self.get(f"element-summary/{element}/", ttl_seconds=ttl_seconds)


__all__ = ["FPLAPIError", "FPLClient", "RateLimiter", "ResponseCache"]
