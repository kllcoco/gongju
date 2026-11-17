"""Throttling and bounded maps: AuthThrottle, ConcurrencyGate, LRU (design §5.7, WP1).

Phase 0 stub: the classes exist; their methods raise unsupported until WP1 merges.
"""
from __future__ import annotations

from typing import Any, Optional

from .errors import unsupported


def _na(what: str):
    return unsupported(f"limits.{what} not available yet (WP1)")


class AuthThrottle:
    """Per-IP auth failure throttle (10 failures / 60 s -> 300 s block)."""

    def __init__(self, max_failures: int = 10, window: float = 60, block: float = 300):
        self.max_failures, self.window, self.block = max_failures, window, block

    def check(self, ip: str) -> Optional[float]:
        """Seconds until unblocked, or None."""
        raise _na("AuthThrottle.check")

    def fail(self, ip: str) -> None:
        """Record a failure."""
        raise _na("AuthThrottle.fail")

    def ok(self, ip: str) -> None:
        """Record a success."""
        raise _na("AuthThrottle.ok")


class ConcurrencyGate:
    """Bounded concurrency (max_concurrent_calls)."""

    def __init__(self, n: int):
        self.n = n

    def acquire(self, timeout: float) -> bool:
        """Acquire a slot within timeout."""
        raise _na("ConcurrencyGate.acquire")

    def release(self) -> None:
        """Release a slot."""
        raise _na("ConcurrencyGate.release")


class LRU:
    """Bounded map with optional TTL (get/put/pop/__contains__)."""

    def __init__(self, maxlen: int, ttl: Optional[float] = None):
        self.maxlen, self.ttl = maxlen, ttl

    def get(self, key: Any, default: Any = None) -> Any:
        """Get a value."""
        raise _na("LRU.get")

    def put(self, key: Any, value: Any) -> None:
        """Insert a value."""
        raise _na("LRU.put")

    def pop(self, key: Any, default: Any = None) -> Any:
        """Remove a value."""
        raise _na("LRU.pop")

    def __contains__(self, key: Any) -> bool:
        raise _na("LRU.__contains__")


__all__ = ["AuthThrottle", "ConcurrencyGate", "LRU"]
