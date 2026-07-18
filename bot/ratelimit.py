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

    def _now(self) -> float:
        # Monotonic so it's immune to wall-clock changes.
        return time.monotonic()

    def _prune(self, key: tuple[int, int], now: float) -> None:
        q = self._hits[key]
        cutoff = now - self.window
        while q and q[0] <= cutoff:
            q.popleft()

    def check(self, guild_id: int, user_id: int) -> bool:
        """Return True if the user has budget right now (does not consume)."""
        now = self._now()
        key = (guild_id, user_id)
        self._prune(key, now)
        return len(self._hits[key]) < self.max_actions

    def consume(self, guild_id: int, user_id: int) -> bool:
        """Consume one unit if available. Returns True if allowed, False if over."""
        now = self._now()
        key = (guild_id, user_id)
        self._prune(key, now)
        if len(self._hits[key]) >= self.max_actions:
            return False
        self._hits[key].append(now)
        return True

    def retry_after(self, guild_id: int, user_id: int) -> float:
        """Seconds until the user regains at least one unit of budget."""
        now = self._now()
        key = (guild_id, user_id)
        self._prune(key, now)
        q = self._hits[key]
        if len(q) < self.max_actions:
            return 0.0
        return max(0.0, self.window - (now - q[0]))
