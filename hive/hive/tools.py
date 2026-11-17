"""Agent tool layer: ToolSpec, Toolkit, profiles, exporters, register_agent_routes(), RuntimeTarget (node /v1/tools)
(design §17, §4.6, WP6).

Phase 0: ``ToolSpec``, ``ToolResult``, ``CallCtx`` (and ``RenderCtx``, re-exported from render.py) are real
dataclasses; ``register_agent_routes`` is a no-op stub (registers nothing, logs a warning) so WP1's ``run_node`` and
WP5's ``run_hub`` can call it before WP6 merges; ``Toolkit``, ``RuntimeTarget`` and ``dispatch_tool_call`` raise
unsupported.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple, Union

from . import render
from .client import Target  # §4.6: RuntimeTarget(Target); client imports tools only under TYPE_CHECKING (no cycle)
from .errors import unsupported
from .render import RenderCtx

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .client import Client
    from .httpserver import ApiServer, Request
    from .node import NodeRuntime

log = logging.getLogger("hive.tools")

PROFILES = ("minimal", "standard", "full")


def _na(what: str):
    return unsupported(f"tools.{what} not available yet (WP6)")


@dataclass
class CallCtx:
    """Per-call context for composite tools (§4.6)."""
    request_id: str
    cancel: threading.Event
    progress: Optional[Callable[[float, Optional[float], str], None]]
    wait: float
    node_mode: str                 # direct | relay


@dataclass
class ToolResult:
    """What a tool call returns to the model."""
    text: str
    is_error: bool
    data: Optional[dict] = None


@dataclass
class ToolSpec:
    """One agent tool (§17.1)."""
    name: str
    title: str
    description: str
    params: dict
    actions: Tuple[str, ...]
    profiles: FrozenSet[str]
    action: Optional[str] = None
    keep: Optional[Tuple[str, ...]] = None
    param_docs: Dict[str, str] = field(default_factory=dict)
    node_param: str = "single"
    to_action: Optional[Callable[[dict], Tuple[str, dict]]] = None
    run: Optional[Callable[["Toolkit", dict, CallCtx], ToolResult]] = None
    render: Callable[[dict, dict, RenderCtx], str] = render.generic
    annotations: dict = field(default_factory=dict)


class Toolkit:
    """Curated agent tools over a Client (WP6)."""

    def __init__(self, client: "Client", *, profile: str = "standard", include: Sequence[str] = (),
                 exclude: Sequence[str] = (), prefix: str = "", wait_budget: Optional[float] = None,
                 char_budget: int = 20000, default_node: Optional[str] = None,
                 local_roots: Optional[Sequence[str]] = None, server_hosted: bool = False):
        raise _na("Toolkit")

    def refresh(self) -> bool:
        """Re-read whoami/nodes/actions."""
        raise _na("Toolkit.refresh")

    def tools(self) -> List[ToolSpec]:
        """Filtered tools."""
        raise _na("Toolkit.tools")

    def call(self, name: str, arguments: Union[str, dict], *, request_id: Optional[str] = None,
             cancel: Optional[threading.Event] = None, progress: Any = None) -> ToolResult:
        """Run a tool; never raises for tool errors."""
        raise _na("Toolkit.call")

    def cancel(self, request_id: str) -> None:
        """Cancel a call."""
        raise _na("Toolkit.cancel")

    def instructions(self) -> str:
        """§17.8 instructions text."""
        raise _na("Toolkit.instructions")

    def mcp_tools(self, protocol_version: str) -> List[dict]:
        """MCP tool list."""
        raise _na("Toolkit.mcp_tools")

    def anthropic_tools(self) -> List[dict]:
        """Anthropic tool list."""
        raise _na("Toolkit.anthropic_tools")

    def openai_tools(self) -> List[dict]:
        """OpenAI Chat Completions tool list."""
        raise _na("Toolkit.openai_tools")

    def openai_responses_tools(self) -> List[dict]:
        """OpenAI Responses tool list."""
        raise _na("Toolkit.openai_responses_tools")


class RuntimeTarget(Target):
    """In-process Target over a NodeRuntime for the node's GET /v1/tools (§4.6; WP6)."""

    kind = "node"

    def __init__(self, rt: "NodeRuntime", identity: "Identity", ip: str):
        raise _na("RuntimeTarget")


def register_agent_routes(server: "ApiServer", target_factory: Callable[["Identity", "Request"], "Target"], *,
                          role: str, profile: str = "standard", mcp: bool = False) -> None:
    """Register GET /v1/tools (and /mcp on the hub) (P0 stub: no-op with a warning)."""
    if mcp and role == "node":
        raise ValueError("/mcp is served by the hub only")
    log.warning("agent tool routes not available yet (WP6); /v1/tools not registered")


def dispatch_tool_call(client: "Client", name: str, arguments: Union[str, dict], **toolkit_kw: Any) -> ToolResult:
    """One-shot tool call with a fresh Toolkit."""
    raise _na("dispatch_tool_call")


__all__ = ["PROFILES", "CallCtx", "RenderCtx", "ToolResult", "ToolSpec", "Toolkit", "RuntimeTarget",
           "register_agent_routes", "dispatch_tool_call"]
