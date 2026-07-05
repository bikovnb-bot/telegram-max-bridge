import time

from bridge.main import RateLimiter


def test_rate_limiter_allows_up_to_limit():
    rl = RateLimiter(max_events=3, window_seconds=60)
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is True
    assert rl.allow("k") is False


def test_rate_limiter_keys_are_independent():
    rl = RateLimiter(max_events=1, window_seconds=60)
    assert rl.allow("a") is True
    assert rl.allow("b") is True
    assert rl.allow("a") is False
    assert rl.allow("b") is False


def test_rate_limiter_window_expires():
    rl = RateLimiter(max_events=1, window_seconds=0.2)
    assert rl.allow("k") is True
    assert rl.allow("k") is False
    time.sleep(0.25)
    assert rl.allow("k") is True


def test_should_alert_throttles():
    rl = RateLimiter(max_events=1, window_seconds=60)
    assert rl.should_alert("k") is True
    assert rl.should_alert("k") is False
