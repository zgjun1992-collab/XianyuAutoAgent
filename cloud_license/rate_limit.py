import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Small single-process limiter suitable for the initial one-instance deployment."""

    def __init__(self, clock=None):
        self.clock = clock or time.monotonic
        self._events = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, bucket, key, limit, window_seconds):
        now = float(self.clock())
        identity = (str(bucket), str(key))
        with self._lock:
            events = self._events[identity]
            cutoff = now - float(window_seconds)
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= int(limit):
                retry_after = max(1, int(window_seconds - (now - events[0])))
                return False, retry_after
            events.append(now)
            if len(self._events) > 10_000:
                self._cleanup(now)
        return True, 0

    def _cleanup(self, now):
        stale = [key for key, events in self._events.items() if not events or now - events[-1] > 3600]
        for key in stale:
            self._events.pop(key, None)

