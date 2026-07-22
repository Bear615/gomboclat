"""Per-user rate limiting for write actions.

Pure, in-memory, monotonic-clock based sliding window. Keyed by (guild_id,
user_id) so a member's budget is per-guild. Read-only tools are never limited;
only actions that mutate the server consume the budget.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque


class RateLimiter:
    """Sliding-window limiter: at most ``max_actions`` per ``window`` seconds."""

    def __init__(self, max_actions: int = 5, window_seconds: float = 60.0):
        self.max_actions = max_actions
        self.window = window_seconds
        self._hits: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self._last_sweep = self._now()

    def _now(self) -> float:
        # Monotonic so it's immune to wall-clock changes.
        return time.monotonic()

    def _prune(self, key: tuple[int, int], now: float) -> None:
        q = self._hits[key]
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()

    def _sweep(self, now: float) -> None:
        """Discard expired users so the key map cannot grow for process life."""
        # At most one linear scan per window; normal checks stay constant-time.
        if now - self._last_sweep < self.window:
            return
        for key in list(self._hits):
            self._prune(key, now)
            if not self._hits[key]:
                del self._hits[key]
        self._last_sweep = now

    def _prepare(self, key: tuple[int, int], now: float) -> deque[float]:
        self._sweep(now)
        self._prune(key, now)
        return self._hits[key]

    def check(self, guild_id: int, user_id: int) -> bool:
        """Return True if the user has budget right now (does not consume)."""
        now = self._now()
        key = (guild_id, user_id)
        return len(self._prepare(key, now)) < self.max_actions

    def consume(self, guild_id: int, user_id: int) -> bool:
        """Consume one unit if available. Returns True if allowed, False if over."""
        now = self._now()
        key = (guild_id, user_id)
        q = self._prepare(key, now)
        if len(q) >= self.max_actions:
            return False
        q.append(now)
        return True

    def retry_after(self, guild_id: int, user_id: int) -> float:
        """Seconds until the user regains at least one unit of budget."""
        now = self._now()
        key = (guild_id, user_id)
        q = self._prepare(key, now)
        if len(q) < self.max_actions:
            return 0.0
        return max(0.0, self.window - (now - q[0]))
