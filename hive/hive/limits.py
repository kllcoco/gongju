"""Throttling and bounded maps: AuthThrottle, ConcurrencyGate, LRU (design §5.7, WP1).

- ``AuthThrottle``: per-IP authentication failures (10 / 60 s -> blocked for 300 s, reason ``auth_throttle``). The
  server consults it BEFORE the token lookup; a blocked IP whose token verifies still proceeds (v4), only failures get
  429. Loopback is throttled too. All maps are bounded (LRU 10 000 keys).
- ``ConcurrencyGate``: node ``max_concurrent_calls`` (the pipeline waits <= 5 s, then ``unavailable`` reason busy).
  Audit round 4: ``owner_reserve`` slots only owner identities may take, and a per-identity cap (default: every
  non-reserved slot) so one token cannot hold every call slot.
- ``BodyBudget``: the HTTP server's global byte budget for buffered request bodies (a quarter reserved for owners).
- ``KeyedLimit``: a per-key concurrency counter (the HTTP server's per-identity cap on requests with a body).
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
    """Bounded concurrency (``max_concurrent_calls``); ``inflight`` counts the held slots.

    Audit round 4: ``owner_reserve`` slots (capped at ``n - 1``) are reserved for owner identities, and one non-owner
    ``key`` (an identity) holds at most ``per_identity`` slots (default and maximum: every non-reserved slot). Owners
    may take any free slot. ``acquire``/``release`` take the same ``key``/``owner`` pair."""

    def __init__(self, n: int, *, owner_reserve: int = 0, per_identity: Optional[int] = None):
        self.n = max(1, int(n))
        self.owner_reserve = max(0, min(int(owner_reserve or 0), self.n - 1))
        self.shared = self.n - self.owner_reserve
        cap = self.shared if per_identity is None else int(per_identity)
        self.per_identity = max(1, min(cap, self.shared))
        self._cond = threading.Condition()
        self._held = 0
        self._held_other = 0
        self._by_key: Dict[Any, int] = {}
        self._waiting = 0

    def _free(self, key: Any, owner: bool) -> bool:
        if self._held >= self.n:
            return False
        if owner:
            return True
        if self._held_other >= self.shared:
            return False
        return key is None or self._by_key.get(key, 0) < self.per_identity

    def acquire(self, timeout: float, key: Any = None, owner: bool = False) -> bool:
        """Acquire a slot within ``timeout`` seconds; False when none became free."""
        end = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            self._waiting += 1
            try:
                while not self._free(key, owner):
                    left = end - time.monotonic()
                    if left <= 0:
                        return False
                    self._cond.wait(left)
                self._held += 1
                if not owner:
                    self._held_other += 1
                    if key is not None:
                        self._by_key[key] = self._by_key.get(key, 0) + 1
                return True
            finally:
                self._waiting -= 1

    def release(self, key: Any = None, owner: bool = False) -> None:
        """Release a slot (the ``key``/``owner`` it was acquired with)."""
        with self._cond:
            if self._held <= 0:
                return
            self._held -= 1
            if not owner:
                self._held_other = max(0, self._held_other - 1)
                if key is not None:
                    n = self._by_key.get(key, 0) - 1
                    if n > 0:
                        self._by_key[key] = n
                    else:
                        self._by_key.pop(key, None)
            self._cond.notify_all()

    def held_by(self, key: Any) -> int:
        """Slots ``key`` holds now."""
        with self._cond:
            return self._by_key.get(key, 0)

    @property
    def inflight(self) -> int:
        """Slots currently held."""
        return self._held

    @property
    def queued(self) -> int:
        """Callers waiting for a slot."""
        return self._waiting


class BodyBudget:
    """Audit round 4: a global byte budget for request bodies the HTTP server buffers (charged before the body is
    read, released after the response). Non-owners may use ``total - reserve`` bytes, owners all of it; a body larger
    than that is admitted only while nothing else is buffered (so a big route limit cannot deadlock)."""

    def __init__(self, total: int, reserve: Optional[int] = None):
        self.total = max(1, int(total))
        self.reserve = max(0, min(int(self.total // 4 if reserve is None else reserve), self.total - 1))
        self._cond = threading.Condition()
        self.used = 0

    def acquire(self, n: int, *, owner: bool = False, timeout: float = 0.0) -> bool:
        """Charge ``n`` bytes, waiting up to ``timeout`` seconds; False when the budget stayed exhausted."""
        n = max(0, int(n))
        cap = self.total if owner else self.total - self.reserve
        end = time.monotonic() + max(0.0, float(timeout))
        with self._cond:
            while not (self.used + n <= cap or self.used == 0):
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
            self.used += n
            return True

    def release(self, n: int) -> None:
        """Return ``n`` bytes to the budget."""
        with self._cond:
            self.used = max(0, self.used - max(0, int(n)))
            self._cond.notify_all()


class KeyedLimit:
    """At most ``cap`` concurrent holders per key (thread-safe; the map only holds keys in use)."""

    def __init__(self, cap: int):
        self.cap = max(1, int(cap))
        self._lock = threading.Lock()
        self._d: Dict[Any, int] = {}

    def acquire(self, key: Any) -> bool:
        """Take one unit for ``key``; False when it already holds ``cap``."""
        with self._lock:
            n = self._d.get(key, 0)
            if n >= self.cap:
                return False
            self._d[key] = n + 1
            return True

    def release(self, key: Any) -> None:
        """Give back one unit of ``key``."""
        with self._lock:
            n = self._d.get(key, 0) - 1
            if n > 0:
                self._d[key] = n
            else:
                self._d.pop(key, None)

    def held(self, key: Any) -> int:
        """Units ``key`` holds now."""
        with self._lock:
            return self._d.get(key, 0)


__all__ = ["AuthThrottle", "BodyBudget", "ConcurrencyGate", "KeyedLimit", "LRU"]
