"""A corrected VM clock must not put the game-log parser to sleep for an hour."""

from types import SimpleNamespace

import pytest

from arenamcp.scryfall import API_RATE_LIMIT_MS, ScryfallCache


@pytest.mark.parametrize("wall_adjustment", [-3360, 3360, 0])
def test_rate_limit_uses_elapsed_time_across_wall_clock_adjustment(monkeypatch, wall_adjustment):
    clock = SimpleNamespace(wall=1_793_568_000.0, elapsed=500.0)
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        clock.elapsed += seconds
        clock.wall += seconds

    monkeypatch.setattr("arenamcp.scryfall.time.time", lambda: clock.wall)
    monkeypatch.setattr("arenamcp.scryfall.time.monotonic", lambda: clock.elapsed)
    monkeypatch.setattr("arenamcp.scryfall.time.sleep", sleep)
    # Avoid cache initialization, background downloads and real network calls.
    cache = object.__new__(ScryfallCache)
    cache._last_api_call = 0.0
    cache._rate_limit_api()
    assert sleeps == []

    clock.wall += 0.025 + wall_adjustment
    clock.elapsed += 0.025
    cache._rate_limit_api()
    assert sleeps == pytest.approx([API_RATE_LIMIT_MS / 1000 - 0.025])

    # Once the interval has passed there is no artificial extra wait.
    clock.elapsed += 0.2
    clock.wall += 0.2
    cache._rate_limit_api()
    assert len(sleeps) == 1
