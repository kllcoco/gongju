"""Throttling and bounded maps: AuthThrottle, ConcurrencyGate, LRU (design §5.7, WP1).

- ``AuthThrottle``: per-IP authentication failures (10 / 60 s -> blocked for 300 s, reason ``auth_throttle``). The
  server consults it BEFORE the token lookup; a blocked IP whose token verifies still proceeds (v4), only failures get
  429. Loopback is throttled too. All maps are bounded (LRU 10 000 keys).
- ``ConcurrencyGate``: node ``max_concurrent_calls`` (the pipeline waits <= 5 s, then ``unavailable`` reason busy).
- ``LRU``: a thread-safe bounded map with an optional TTL.
"""
from __future__ import annotations

import collections
import threading
import time
from typing import Any, Deque, Dict, Iterator, List, Optional, Tuple

_MAX_KEYS = 10000


class LRU:
    """Bounded map with optional TTL (get/put/pop/__contains__); thread-safe."""

    def __init__(self, maxlen: int, ttl: Optional[float] = None):
        self.maxlen = max(1, int(maxlen))
        self.ttl = ttl
        self._d: "collections.OrderedDict[Any, Tuple[float, Any]]" = collections.OrderedDict()
        self._lock = threading.Lock()

    def _expired(self, ts: float, now: float) -> bool:
        return self.ttl is not None and now - ts > self.ttl

    def get(self, key: Any, default: Any = None) -> Any:
        """Value for ``key`` (refreshes its recency), or ``default`` when absent/expired."""
        now = time.monotonic()
        with self._lock:
            item = self._d.get(key)
            if item is None:
                return default
            if self._expired(item[0], now):
                del self._d[key]
                return default
            self._d.move_to_end(key)
            return item[1]

    def put(self, key: Any, value: Any) -> None:
        """Insert or replace a value; evicts the least recently used entry when full."""
        now = time.monotonic()
        with self._lock:
            self._d[key] = (now, value)
            self._d.move_to_end(key)
            while len(self._d) > self.maxlen:
                self._d.popitem(last=False)

    def pop(self, key: Any, default: Any = None) -> Any:
        """Remove and return a value (``default`` when absent or expired)."""
        now = time.monotonic()
        with self._lock:
            item = self._d.pop(key, None)
        if item is None or self._expired(item[0], now):
            return default
        return item[1]

    def __contains__(self, key: Any) -> bool:
        sentinel = object()
        return self.get(key, sentinel) is not sentinel

    def __len__(self) -> int:
        with self._lock:
            return len(self._d)

    def keys(self) -> List[Any]:
        """Snapshot of the live keys (oldest first)."""
        now = time.monotonic()
        with self._lock:
            return [k for k, (ts, _v) in self._d.items() if not self._expired(ts, now)]

    def items(self) -> Iterator[Tuple[Any, Any]]:
        """Snapshot of the live items (oldest first)."""
        now = time.monotonic()
        with self._lock:
            snap = [(k, v) for k, (ts, v) in self._d.items() if not self._expired(ts, now)]
        return iter(snap)

    def clear(self) -> None:
        """Drop every entry."""
        with self._lock:
            self._d.clear()


class AuthThrottle:
    """Per-IP auth failure throttle (10 failures / 60 s -> 300 s block)."""

    def __init__(self, max_failures: int = 10, window: float = 60, block: float = 300):
        self.max_failures, self.window, self.block = int(max_failures), float(window), float(block)
        self._fails = LRU(_MAX_KEYS)      # ip -> deque of failure times
        self._blocked = LRU(_MAX_KEYS)    # ip -> blocked-until (monotonic)
        self._lock = threading.Lock()

    def check(self, ip: str) -> Optional[float]:
        """Seconds until ``ip`` is unblocked, or None when it is not blocked."""
        until = self._blocked.get(ip)
        if until is None:
            return None
        left = until - time.monotonic()
        if left <= 0:
            self._blocked.pop(ip)
            return None
        return left

    def fail(self, ip: str) -> None:
        """Record an authentication failure for ``ip`` (may start a block)."""
        now = time.monotonic()
        with self._lock:
            q: Optional[Deque[float]] = self._fails.get(ip)
            if q is None:
                q = collections.deque()
            while q and now - q[0] > self.window:
                q.popleft()
            q.append(now)
            if len(q) >= self.max_failures:
                self._blocked.put(ip, now + self.block)
                q.clear()
            self._fails.put(ip, q)

    def ok(self, ip: str) -> None:
        """Record a successful authentication (clears the failure history, never an active block)."""
        with self._lock:
            self._fails.pop(ip)

    def snapshot(self) -> Dict[str, float]:
        """{ip: seconds left} of the currently blocked IPs (diagnostics)."""
        now = time.monotonic()
        return {k: round(v - now, 1) for k, v in self._blocked.items() if v > now}


class ConcurrencyGate:
    """Bounded concurrency (``max_concurrent_calls``); ``inflight`` counts the held slots."""

    def __init__(self, n: int):
        self.n = max(1, int(n))
        self._sem = threading.BoundedSemaphore(self.n)
        self._lock = threading.Lock()
        self._held = 0
        self._waiting = 0

    def acquire(self, timeout: float) -> bool:
        """Acquire a slot within ``timeout`` seconds; False when none became free."""
        with self._lock:
            self._waiting += 1
        try:
            ok = self._sem.acquire(timeout=max(0.0, float(timeout)))
        finally:
            with self._lock:
                self._waiting -= 1
        if ok:
            with self._lock:
                self._held += 1
        return ok

    def release(self) -> None:
        """Release a slot."""
        with self._lock:
            if self._held <= 0:
                return
            self._held -= 1
        self._sem.release()

    @property
    def inflight(self) -> int:
        """Slots currently held."""
        return self._held

    @property
    def queued(self) -> int:
        """Callers waiting for a slot."""
        return self._waiting


__all__ = ["AuthThrottle", "ConcurrencyGate", "LRU"]
