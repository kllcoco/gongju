"""Hub: routing to direct/relay NodeLinks, hub.nodes, hub approvals, run_hub() (design §8, §4.6, WP5).

Phase 0 stub: names exist and raise unsupported until WP5b merges; ``HUB_HANDLERS`` is empty until then (WP5 sets
``{"hub.nodes": Hub._act_nodes}``).
"""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .config import HubConfig
    from .gateway import GatewayResponse


def _na(what: str):
    return unsupported(f"hub.{what} not available yet (WP5)")


class Hub:
    """The hub (WP5b; Phase 0 stub)."""

    def __init__(self, cfg: "HubConfig", *, config_path: Optional[Path] = None):
        raise _na("Hub")

    def start(self) -> None:
        """Start links, prober and reaper."""
        raise _na("Hub.start")

    def stop(self) -> None:
        """Stop the hub."""
        raise _na("Hub.stop")

    def call(self, identity: "Identity", action: str, params: Optional[dict] = None, *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None, approval_id: Optional[str] = None,
             cancel: Optional[threading.Event] = None, ip: str = "") -> dict:
        """Envelope (§6.2); never raises."""
        raise _na("Hub.call")

    def cancel(self, request_id: str, identity: "Identity") -> bool:
        """Cancel a routed call."""
        raise _na("Hub.cancel")

    def nodes_for(self, identity: "Identity", include_offline: bool = True) -> List[dict]:
        """NodeSummary rows."""
        raise _na("Hub.nodes_for")

    def actions_for(self, identity: "Identity", node: Optional[str] = None) -> dict:
        """/v1/actions body."""
        raise _na("Hub.actions_for")

    def link_for(self, identity: "Identity", node: str) -> "NodeLink":
        """§8.1 resolution + access check."""
        raise _na("Hub.link_for")


class NodeLink(ABC):
    """A hub's link to one node (§8.3)."""

    name: str
    mode: str

    @abstractmethod
    def online(self) -> bool:
        """Node reachable."""

    @abstractmethod
    def summary(self) -> dict:
        """NodeSummary row."""

    @abstractmethod
    def actions(self) -> List[str]:
        """Implemented actions."""

    @abstractmethod
    def catalog(self) -> List[dict]:
        """Public dicts of the node's actions."""

    @abstractmethod
    def call(self, identity: "Identity", action: str, params: dict, *, timeout: float, request_id: str,
             exec_gated: bool, bound_files: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """Envelope; never raises for remote errors."""

    @abstractmethod
    def cancel(self, request_id: str) -> None:
        """Cancel a call."""

    @abstractmethod
    def ensure_fresh(self) -> None:
        """DirectLink synchronous probe; RelayLink no-op."""


class RelayLink(NodeLink):
    """Relay queue/pending per node (§7.4; WP5b)."""

    def __init__(self, *args: object, **kwargs: object):
        raise _na("RelayLink")


class DirectLink(NodeLink):
    """Transport to a direct node with the stored hub token (WP5b)."""

    def __init__(self, *args: object, **kwargs: object):
        raise _na("DirectLink")

    def open_gateway(self, service: str, method: str, path: str, body: bytes, *, agent: str, timeout: float,
                     cancel: threading.Event) -> "GatewayResponse":
        """Raw gateway passthrough to the node."""
        raise _na("DirectLink.open_gateway")


def run_hub(config_path: Optional[str] = None, *, relaunched: bool = False, supervise: bool = False) -> int:
    """Run the hub daemon."""
    raise _na("run_hub")


HUB_HANDLERS: Dict[str, Callable[["Hub", "Identity", dict], dict]] = {}

__all__ = ["Hub", "NodeLink", "RelayLink", "DirectLink", "run_hub", "HUB_HANDLERS"]
