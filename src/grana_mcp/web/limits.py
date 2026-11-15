"""In-memory rate limits for the chat page (reset when the server restarts).

* per user: questions per hour and per day (each question spends Claude quota);
* per client IP: failed token attempts, then a temporary block.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

HOUR = 3600
DAY = 86400


@dataclass
class Window:
    """Sliding-window counter: at most ``limit`` events per ``seconds``."""

    limit: int
    seconds: int
    events: deque[float] = field(default_factory=deque)

    def _trim(self, now: float) -> None:
        while self.events and self.events[0] <= now - self.seconds:
            self.events.popleft()

    def retry_after(self, now: float) -> int:
        """Seconds until one more event fits (0 = allowed now)."""
        self._trim(now)
        if self.limit <= 0 or len(self.events) < self.limit:
            return 0
        return max(1, int(self.events[0] + self.seconds - now) + 1)

    def add(self, now: float) -> None:
        self.events.append(now)


@dataclass
class ChatLimits:
    per_hour: int = 30
    per_day: int = 150
    clock: Callable[[], float] = time.monotonic
    _hour: dict[str, Window] = field(default_factory=dict)
    _day: dict[str, Window] = field(default_factory=dict)

    def take(self, user: str) -> int:
        """Record a question for ``user``; returns 0, or seconds to wait if over a limit."""
        now = self.clock()
        hour = self._hour.setdefault(user, Window(self.per_hour, HOUR))
        day = self._day.setdefault(user, Window(self.per_day, DAY))
        wait = max(hour.retry_after(now), day.retry_after(now))
        if wait:
            return wait
        hour.add(now)
        day.add(now)
        return 0


@dataclass
class AuthGuard:
    """Blocks an IP for ``block_seconds`` after ``max_failures`` bad tokens in ``window``."""

    max_failures: int = 10
    window: int = 15 * 60
    block_seconds: int = 15 * 60
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, Window] = field(default_factory=dict)
    _blocked_until: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    def blocked(self, ip: str) -> int:
        left = self._blocked_until[ip] - self.clock()
        return int(left) + 1 if left > 0 else 0

    def failed(self, ip: str) -> None:
        now = self.clock()
        w = self._failures.setdefault(ip, Window(self.max_failures, self.window))
        w.add(now)
        if w.retry_after(now):
            self._blocked_until[ip] = now + self.block_seconds
            w.events.clear()

    def succeeded(self, ip: str) -> None:
        self._failures.pop(ip, None)
