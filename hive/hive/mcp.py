"""MCP server: McpCore (stdlib JSON-RPC 2.0), stdio loop, Streamable HTTP handler for the hub (design §18, WP6).

Phase 0: ``SUPPORTED_VERSIONS`` is real; the rest raises unsupported until WP6 merges.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Union

from . import __version__
from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .tools import Toolkit

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]


def _na(what: str):
    return unsupported(f"mcp.{what} not available yet (WP6)")


@dataclass
class McpSession:
    """Per-connection MCP state."""
    protocol_version: str = SUPPORTED_VERSIONS[0]
    client_info: dict = field(default_factory=dict)
    initialized: bool = False
    inflight: Dict[Any, Any] = field(default_factory=dict)
    log_level: str = "info"


class McpCore:
    """JSON-RPC method handling over a Toolkit (WP6)."""

    def __init__(self, toolkit: "Toolkit", *, server_name: str = "hive", server_title: str = "Hive 蜂巢",
                 version: str = __version__, structured_content: bool = False, max_workers: int = 8):
        raise _na("McpCore")

    def handle(self, msg: Union[dict, list], session: McpSession,
               send: Callable[[dict], None]) -> Optional[Union[dict, list]]:
        """Handle one message."""
        raise _na("McpCore.handle")


def run_stdio(argv: List[str]) -> int:
    """``hive mcp …``."""
    raise _na("run_stdio")


def http_handler(core_factory: Callable[..., McpCore]) -> Callable[..., Any]:
    """Raw route for /mcp (hub only)."""
    raise _na("http_handler")


__all__ = ["SUPPORTED_VERSIONS", "McpSession", "McpCore", "run_stdio", "http_handler"]
