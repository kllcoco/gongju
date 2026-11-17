"""OpenAI-compatible model gateway routes for hub and node (design §8.7, §4.6, D48, WP5).

Phase 0: ``GatewayResponse`` is real; ``register_gateway_routes`` is a no-op stub (registers no route and logs a
warning) so WP1's ``run_node`` can call it before WP5b merges; the backends raise unsupported.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Dict, Iterator, Optional

from typing import Protocol, runtime_checkable

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .config import GatewayConfig
    from .httpserver import ApiServer
    from .hub import Hub
    from .node import NodeRuntime

log = logging.getLogger("hive.gateway")


@dataclass
class GatewayResponse:
    """Upstream response: exactly one of ``body`` / ``chunks`` is set."""
    status: int
    headers: Dict[str, str]
    body: Optional[bytes]
    chunks: Optional[Iterator[bytes]]


@runtime_checkable
class GatewayBackend(Protocol):
    """Authorize and forward gateway requests."""

    def authorize(self, identity: "Identity", node: str, service: str) -> None:
        """forbidden / not_found."""
        ...

    def forward(self, identity: "Identity", node: str, service: str, method: str, path: str, body: bytes, *,
                stream: bool, request_id: str, cancel: threading.Event) -> GatewayResponse:
        """Forward one request upstream."""
        ...


class HubGatewayBackend(GatewayBackend):
    """Gateway backend of the hub (WP5b)."""

    def __init__(self, hub: "Hub"):
        raise unsupported("gateway.HubGatewayBackend not available yet (WP5)")


class NodeGatewayBackend(GatewayBackend):
    """Gateway backend of a node (WP5b)."""

    def __init__(self, rt: "NodeRuntime"):
        raise unsupported("gateway.NodeGatewayBackend not available yet (WP5)")


def register_gateway_routes(server: "ApiServer", *, role: str, backend: GatewayBackend,
                            cfg: "GatewayConfig") -> None:
    """Register ``/openai/{node}/{service}/{rest*}`` routes (P0 stub: no-op with a warning)."""
    log.warning("model gateway not available yet (WP5); /openai routes not registered")


__all__ = ["GatewayResponse", "GatewayBackend", "HubGatewayBackend", "NodeGatewayBackend", "register_gateway_routes"]
