"""ApiServer: bounded ThreadingHTTPServer, TLS, transport checks, router, envelopes, multi-address bind
(design §6.1, WP1).

Phase 0: ``Request`` (dataclass) is real; ``ApiServer`` is a stub. Route registration from other WPs
(``tools.register_agent_routes``, ``gateway.register_gateway_routes``) targets ``ApiServer.route``.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Tuple

from .errors import bad_request, unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .audit import AuditLog
    from .auth import Identity, TokenStore
    from .config import ListenConfig
    from .limits import AuthThrottle

AUTH_MODES = ("none", "token", "owner", "relay", "models")
ROUTE_MODES = ("result", "envelope", "raw")


@dataclass
class Request:
    """One HTTP request as seen by a route handler."""
    method: str
    path: str
    query: Dict[str, str]
    headers: Mapping[str, str]
    client_ip: str
    body: bytes
    identity: Optional["Identity"]

    def json(self) -> Any:
        """Body as JSON; bad_request on invalid JSON."""
        try:
            return json.loads(self.body.decode("utf-8") if self.body else "null")
        except (ValueError, UnicodeDecodeError) as e:
            raise bad_request(f"invalid JSON body: {e}")


class ApiServer:
    """HTTP server for node and hub (WP1; Phase 0 stub)."""

    def __init__(self, listen: "ListenConfig", *, role: str, tokens: Callable[[], "TokenStore"],
                 relay_tokens: Optional[Callable[[], "TokenStore"]] = None, throttle: "AuthThrottle",
                 logger: logging.Logger, audit: Optional["AuditLog"] = None,
                 owner_check: Optional[Callable[["Identity"], bool]] = None):
        raise unsupported("httpserver.ApiServer not available yet (WP1)")

    def route(self, method: str, pattern: str, fn: Callable[[Request], Any], *, auth: str = "token",
              mode: str = "result", max_body: Optional[int] = None, error_style: str = "hive",
              cors: bool = False) -> None:
        """Register a route."""
        raise unsupported("httpserver.ApiServer not available yet (WP1)")

    def start(self) -> List[Tuple[str, int]]:
        """Start listening; returns bound addresses."""
        raise unsupported("httpserver.ApiServer not available yet (WP1)")

    def stop(self) -> None:
        """Stop the server."""
        raise unsupported("httpserver.ApiServer not available yet (WP1)")


__all__ = ["AUTH_MODES", "ROUTE_MODES", "Request", "ApiServer"]
