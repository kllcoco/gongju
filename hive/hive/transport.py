"""HttpTransport: HTTP(S) client, TLS pinning, keep-alive, proxy, connect-only retry (design §15.1, §6.5, WP5).

Phase 0 stub: ``HttpTransport`` raises unsupported until WP5a merges.
"""
from __future__ import annotations

from typing import Any, Optional

from .errors import unsupported


class HttpTransport:
    """Client transport to a node or hub (WP5a)."""

    def __init__(self, base_url: str, token: Optional[str] = None, *, ca_file: Optional[str] = None,
                 tls_pin: Optional[str] = None, timeout: float = 120.0, proxy: str = "auto"):
        raise unsupported("transport.HttpTransport not available yet (WP5)")

    def request(self, method: str, path: str, body: Optional[dict] = None, *, timeout: Optional[float] = None,
                headers: Optional[dict] = None, raw: bool = False) -> Any:
        """Envelope result (or (status, headers, bytes) when raw)."""
        raise unsupported("transport.HttpTransport not available yet (WP5)")


__all__ = ["HttpTransport"]
