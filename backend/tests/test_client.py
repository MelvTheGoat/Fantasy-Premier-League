"""The FPL API client: caching, pacing and retries.

Driven through an httpx mock transport, so no request leaves the machine.
"""

from __future__ import annotations

import dataclasses

import httpx
import pytest

from fplai.config import Settings
from fplai.data.client import FPLAPIError, FPLClient, RateLimiter, ResponseCache


@pytest.fixture
def config(tmp_path):
    return Settings(
        database_path=tmp_path / "db.sqlite3",
        cache_dir=tmp_path / "cache",
        min_request_interval=0.0,
        cache_ttl_seconds=3600,
        max_retries=3,
    )


class Recorder:
    """Counts requests and replays a scripted sequence of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.url.path)
        response = self.responses.pop(0) if self.responses else httpx.Response(200, json={})
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def count(self) -> int:
        return len(self.requests)


def client_for(recorder, config, **overrides):
    if overrides:
        config = dataclasses.replace(config, **overrides)
    return FPLClient(config, transport=httpx.MockTransport(recorder))


class TestEndpoints:
    def test_bootstrap_static_is_fetched_from_the_documented_path(self, config):
        recorder = Recorder(httpx.Response(200, json={"elements": []}))
        with client_for(recorder, config) as client:
            assert client.bootstrap_static() == {"elements": []}
        assert recorder.requests == ["/api/bootstrap-static/"]

    def test_fixtures_can_be_fetched_whole_or_per_gameweek(self, config):
        recorder = Recorder(
            httpx.Response(200, json=[{"id": 1}]),
            httpx.Response(200, json=[{"id": 2}]),
        )
        with client_for(recorder, config) as client:
            client.fixtures()
            client.fixtures(gameweek=7)
        assert recorder.requests == ["/api/fixtures/", "/api/fixtures/"]

    def test_live_and_summary_paths_include_their_identifier(self, config):
        recorder = Recorder(
            httpx.Response(200, json={"elements": []}),
            httpx.Response(200, json={"history": []}),
        )
        with client_for(recorder, config) as client:
            client.event_live(12)
            client.element_summary(427)
        assert recorder.requests == ["/api/event/12/live/", "/api/element-summary/427/"]


class TestCaching:
    def test_a_second_call_is_served_from_cache(self, config):
        recorder = Recorder(httpx.Response(200, json={"v": 1}))
        with client_for(recorder, config) as client:
            first = client.bootstrap_static()
            second = client.bootstrap_static()
        assert first == second == {"v": 1}
        assert recorder.count == 1, "the second call must not hit the network"

    def test_a_zero_ttl_always_refetches(self, config):
        """The live job needs the newest scores, not a cached copy."""
        recorder = Recorder(
            httpx.Response(200, json={"v": 1}),
            httpx.Response(200, json={"v": 2}),
        )
        with client_for(recorder, config) as client:
            assert client.event_live(4, ttl_seconds=0) == {"v": 1}
            assert client.event_live(4, ttl_seconds=0) == {"v": 2}
        assert recorder.count == 2

    def test_different_paths_are_cached_separately(self, config):
        recorder = Recorder(
            httpx.Response(200, json={"gw": 3}),
            httpx.Response(200, json={"gw": 4}),
        )
        with client_for(recorder, config) as client:
            assert client.event_live(3) == {"gw": 3}
            assert client.event_live(4) == {"gw": 4}

    def test_the_cache_survives_a_new_client(self, config):
        recorder = Recorder(httpx.Response(200, json={"v": 1}))
        with client_for(recorder, config) as client:
            client.bootstrap_static()
        with client_for(recorder, config) as client:
            assert client.bootstrap_static() == {"v": 1}
        assert recorder.count == 1

    def test_a_corrupt_cache_entry_is_refetched(self, config, tmp_path):
        recorder = Recorder(
            httpx.Response(200, json={"v": 1}),
            httpx.Response(200, json={"v": 2}),
        )
        with client_for(recorder, config) as client:
            client.bootstrap_static()
            for path in (tmp_path / "cache").glob("*.json"):
                path.write_text("{ truncated")
            assert client.bootstrap_static() == {"v": 2}

    def test_clearing_the_cache_forces_a_refetch(self, config):
        recorder = Recorder(
            httpx.Response(200, json={"v": 1}),
            httpx.Response(200, json={"v": 2}),
        )
        with client_for(recorder, config) as client:
            client.bootstrap_static()
            assert client.cache.clear() == 1
            assert client.bootstrap_static() == {"v": 2}


class TestRetries:
    def test_a_server_error_is_retried(self, config, monkeypatch):
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(
            httpx.Response(503),
            httpx.Response(200, json={"v": 1}),
        )
        with client_for(recorder, config) as client:
            assert client.bootstrap_static() == {"v": 1}
        assert recorder.count == 2

    def test_rate_limiting_is_retried(self, config, monkeypatch):
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(httpx.Response(429), httpx.Response(200, json={"v": 1}))
        with client_for(recorder, config) as client:
            assert client.bootstrap_static() == {"v": 1}

    def test_a_network_error_is_retried(self, config, monkeypatch):
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"v": 1}),
        )
        with client_for(recorder, config) as client:
            assert client.bootstrap_static() == {"v": 1}

    def test_retries_eventually_give_up(self, config, monkeypatch):
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(*[httpx.Response(503) for _ in range(5)])
        with client_for(recorder, config) as client:
            with pytest.raises(FPLAPIError, match="gave up"):
                client.bootstrap_static()
        assert recorder.count == 3, "capped by max_retries"

    def test_a_client_error_is_not_retried(self, config, monkeypatch):
        """A 404 will not fix itself, so retrying only wastes the API's time."""
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(httpx.Response(404))
        with client_for(recorder, config) as client:
            with pytest.raises(FPLAPIError, match="404"):
                client.element_summary(999999)
        assert recorder.count == 1

    def test_a_non_json_two_hundred_is_retried(self, config, monkeypatch):
        """An edge cache or maintenance page can return HTML with a 200."""
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(
            httpx.Response(200, text="<html>maintenance</html>"),
            httpx.Response(200, json={"v": 1}),
        )
        with client_for(recorder, config) as client:
            assert client.bootstrap_static() == {"v": 1}

    def test_a_failed_fetch_is_not_cached(self, config, monkeypatch):
        monkeypatch.setattr("fplai.data.client.time.sleep", lambda _: None)
        recorder = Recorder(*[httpx.Response(503) for _ in range(3)])
        with client_for(recorder, config) as client:
            with pytest.raises(FPLAPIError):
                client.bootstrap_static()
            assert client.cache.get("bootstrap-static/", 3600) is None


class TestRateLimiter:
    def test_the_first_request_is_not_delayed(self, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("fplai.data.client.time.sleep", slept.append)
        monkeypatch.setattr("fplai.data.client.time.monotonic", lambda: 0.0)
        RateLimiter(min_interval=10.0).wait()
        assert slept == [], "there is nothing to pace against yet"

    def test_a_following_request_waits_out_the_interval(self, monkeypatch):
        limiter = RateLimiter(min_interval=1.0)
        slept: list[float] = []
        monkeypatch.setattr("fplai.data.client.time.sleep", slept.append)
        clock = iter([0.0, 0.2, 1.0])
        monkeypatch.setattr("fplai.data.client.time.monotonic", lambda: next(clock))

        limiter.wait()
        limiter.wait()
        assert slept == [pytest.approx(0.8)]

    def test_requests_are_paced_through_the_client(self, config, monkeypatch):
        slept: list[float] = []
        monkeypatch.setattr("fplai.data.client.time.sleep", slept.append)
        recorder = Recorder(
            httpx.Response(200, json={"v": 1}),
            httpx.Response(200, json={"v": 2}),
        )
        with client_for(recorder, config, min_request_interval=1.0) as client:
            client.event_live(1)
            client.event_live(2)
        assert any(s > 0 for s in slept), "the second request must be paced"


class TestResponseCache:
    def test_an_expired_entry_is_ignored(self, tmp_path, monkeypatch):
        cache = ResponseCache(tmp_path)
        cache.set("k", {"v": 1})
        assert cache.get("k", ttl_seconds=3600) == {"v": 1}
        monkeypatch.setattr("fplai.data.client.time.time", lambda: 1e12)
        assert cache.get("k", ttl_seconds=3600) is None

    def test_a_missing_entry_returns_nothing(self, tmp_path):
        assert ResponseCache(tmp_path).get("never-fetched", 3600) is None

    def test_no_temporary_files_are_left_behind(self, tmp_path):
        cache = ResponseCache(tmp_path)
        cache.set("k", {"v": 1})
        assert list(tmp_path.glob("*.tmp")) == []
