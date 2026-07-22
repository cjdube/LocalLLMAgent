"""Per-client failed-login limiter for the chat server's one internet-adjacent
surface. Pure and Flask-free (the caller supplies the client key — see
chat/server.py's _client_ip) so it stays unit-testable in isolation."""

import threading
import time


class LoginThrottle:
    """Per-client failed-login limiter — defense-in-depth on the one
    internet-adjacent surface. The 256-bit token is the real defense (brute
    force is infeasible), so this only aims to blunt automated guessing, not to
    lock the box down. After MAX_FAILURES failures inside WINDOW_S a client is
    locked out for a backoff that doubles on repeat offenses (capped at
    MAX_LOCKOUT_S), then the counter resets — short enough that the single
    legitimate user fat-fingering the token isn't durably self-DoSed.

    Keyed by caller identity (see chat/server.py:_client_ip): behind
    `tailscale serve` most requests arrive from loopback, so this is coarse, but
    it still slows a proxied guessing loop. `clock` is injectable for tests."""

    MAX_FAILURES = 5
    WINDOW_S = 300
    BASE_LOCKOUT_S = 30
    MAX_LOCKOUT_S = 900
    # Failed-only keys are otherwise never removed (success is the only other
    # cleanup), so a scanner cycling addresses could grow _state without bound.
    # Past this size, record_failure first drops entries that are neither
    # locked out nor inside an active failure window.
    MAX_TRACKED = 1000

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._state: dict[str, dict] = {}

    def _sweep_stale(self, now: float) -> None:
        # Caller holds self._lock.
        stale = [
            k for k, e in self._state.items()
            if e["locked_until"] <= now and now - e["window_start"] > self.WINDOW_S
        ]
        for k in stale:
            del self._state[k]

    def retry_after(self, key: str) -> float:
        """Seconds the caller must still wait, or 0 if an attempt is allowed now."""
        now = self._clock()
        with self._lock:
            entry = self._state.get(key)
            if not entry:
                return 0.0
            return max(0.0, entry["locked_until"] - now)

    def record_failure(self, key: str) -> None:
        now = self._clock()
        with self._lock:
            if len(self._state) >= self.MAX_TRACKED:
                self._sweep_stale(now)
            entry = self._state.get(key)
            if not entry or now - entry["window_start"] > self.WINDOW_S:
                entry = {"failures": 0, "window_start": now, "lockouts": 0, "locked_until": 0.0}
            entry["failures"] += 1
            if entry["failures"] >= self.MAX_FAILURES:
                entry["lockouts"] += 1
                backoff = min(self.BASE_LOCKOUT_S * 2 ** (entry["lockouts"] - 1), self.MAX_LOCKOUT_S)
                entry["locked_until"] = now + backoff
                entry["failures"] = 0
                entry["window_start"] = now
            self._state[key] = entry

    def record_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)
