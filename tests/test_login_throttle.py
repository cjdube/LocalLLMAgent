"""Tests for chat.server.LoginThrottle — the per-client failed-login limiter.

The token's entropy is the real defense; this only aims to slow automated
guessing while staying lenient enough that a mistyped token doesn't durably
lock the single user out. A fake clock makes the backoff/expiry deterministic.

Importing chat.server runs its module-level env check, so the two required
secrets are stubbed into the environment before the import.
"""

import os

os.environ.setdefault("WREN_CHAT_TOKEN", "test-token")
os.environ.setdefault("FLASK_SECRET_KEY", "test-secret")

from chat.server import LoginThrottle


class FakeClock:
    """A monotonic clock we can advance by hand."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _throttle():
    clock = FakeClock()
    return LoginThrottle(clock=clock), clock


def test_allows_until_threshold():
    t, _ = _throttle()
    # One under the limit: still allowed.
    for _ in range(LoginThrottle.MAX_FAILURES - 1):
        t.record_failure("ip")
    assert t.retry_after("ip") == 0.0


def test_locks_out_at_threshold():
    t, _ = _throttle()
    for _ in range(LoginThrottle.MAX_FAILURES):
        t.record_failure("ip")
    assert t.retry_after("ip") == LoginThrottle.BASE_LOCKOUT_S


def test_lockout_expires_after_backoff():
    t, clock = _throttle()
    for _ in range(LoginThrottle.MAX_FAILURES):
        t.record_failure("ip")
    clock.advance(LoginThrottle.BASE_LOCKOUT_S + 1)
    assert t.retry_after("ip") == 0.0


def test_backoff_doubles_on_repeat_offense():
    t, clock = _throttle()
    # First lockout.
    for _ in range(LoginThrottle.MAX_FAILURES):
        t.record_failure("ip")
    assert t.retry_after("ip") == LoginThrottle.BASE_LOCKOUT_S
    # Wait it out, then trip it again — the second lockout is twice as long.
    clock.advance(LoginThrottle.BASE_LOCKOUT_S + 1)
    for _ in range(LoginThrottle.MAX_FAILURES):
        t.record_failure("ip")
    assert t.retry_after("ip") == LoginThrottle.BASE_LOCKOUT_S * 2


def test_backoff_capped():
    t, clock = _throttle()
    # Trip repeated lockouts, each just inside the window so the offense counter
    # keeps climbing: backoff doubles 30,60,120,240,480, then the sixth would be
    # 960 but saturates at the 900s cap. Advancing only 1s between batches keeps
    # window_start fresh so nothing resets the escalation.
    for i in range(6):
        for _ in range(LoginThrottle.MAX_FAILURES):
            t.record_failure("ip")
        if i < 5:
            clock.advance(1)
    assert t.retry_after("ip") == LoginThrottle.MAX_LOCKOUT_S


def test_stale_failures_fall_out_of_window():
    t, clock = _throttle()
    # A few failures, then a long gap resets the window so they don't accumulate
    # toward a lockout with fresh ones.
    for _ in range(LoginThrottle.MAX_FAILURES - 1):
        t.record_failure("ip")
    clock.advance(LoginThrottle.WINDOW_S + 1)
    t.record_failure("ip")
    assert t.retry_after("ip") == 0.0


def test_success_clears_state():
    t, _ = _throttle()
    for _ in range(LoginThrottle.MAX_FAILURES):
        t.record_failure("ip")
    assert t.retry_after("ip") > 0
    t.record_success("ip")
    assert t.retry_after("ip") == 0.0


def test_keys_are_independent():
    t, _ = _throttle()
    for _ in range(LoginThrottle.MAX_FAILURES):
        t.record_failure("attacker")
    assert t.retry_after("attacker") > 0
    assert t.retry_after("victim") == 0.0
