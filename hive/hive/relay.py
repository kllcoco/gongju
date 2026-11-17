"""RelayAgent: the node side of the long-poll relay (design §7.5, WP5).

Phase 0 stub: ``RelayAgent`` raises unsupported until WP5b merges.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .config import RelayConfig
    from .node import NodeRuntime


class RelayAgent:
    """Polls the hub for tasks and runs them as the relay identity (WP5b)."""

    def __init__(self, runtime: "NodeRuntime", cfg: "RelayConfig"):
        raise unsupported("relay.RelayAgent not available yet (WP5)")

    def start(self) -> None:
        """Start polling."""
        raise unsupported("relay.RelayAgent not available yet (WP5)")

    def stop(self) -> None:
        """Stop polling."""
        raise unsupported("relay.RelayAgent not available yet (WP5)")

    @property
    def status(self) -> dict:
        """{"connected","session_id","last_poll","failures","hub_url"}."""
        raise unsupported("relay.RelayAgent not available yet (WP5)")


__all__ = ["RelayAgent"]
