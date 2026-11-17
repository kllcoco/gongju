"""Python client: Target protocol, HttpTarget, HubTarget, Client, NodeHandle, JobHandle, ServiceHandle, connect()
(design §15.2, WP5).

Phase 0 stub: ``Target`` (Protocol) and the class names exist; constructors and ``connect`` raise unsupported until
WP5a merges.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator, List, Optional

from typing import Protocol, runtime_checkable

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .tools import Toolkit


def _na(what: str):
    return unsupported(f"client.{what} not available yet (WP5)")


@runtime_checkable
class Target(Protocol):
    """Something calls can be sent to (hub or node)."""

    kind: str

    def call(self, action: str, params: Optional[dict], *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        """Result dict (meta is not returned)."""
        ...

    def cancel(self, request_id: str) -> bool:
        """Cancel a call."""
        ...

    def whoami(self) -> dict:
        """GET /v1/whoami."""
        ...

    def nodes(self) -> List[dict]:
        """NodeSummary rows."""
        ...

    def actions(self, node: Optional[str] = None) -> List[dict]:
        """Action rows."""
        ...


class HttpTarget(Target):
    """Target over HttpTransport (WP5a)."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("HttpTarget")


class HubTarget(Target):
    """In-process Hub + Identity target (WP5b)."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("HubTarget")


class Client:
    """High-level client (WP5a)."""

    def __init__(self, target: Target, *, default_node: Optional[str] = None):
        raise _na("Client")

    @classmethod
    def from_config(cls, path: Optional[str] = None, connection: Optional[str] = None) -> "Client":
        """Client from client.json."""
        raise _na("Client.from_config")

    def call(self, action: str, params: Optional[dict] = None, *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        """Call an action."""
        raise _na("Client.call")

    def nodes(self) -> List[dict]:
        """NodeSummary rows."""
        raise _na("Client.nodes")

    def node(self, name: Optional[str] = None) -> "NodeHandle":
        """Handle for one node."""
        raise _na("Client.node")

    def whoami(self) -> dict:
        """Identity and server info."""
        raise _na("Client.whoami")

    def actions(self, node: Optional[str] = None) -> List[dict]:
        """Action rows."""
        raise _na("Client.actions")

    def cancel(self, request_id: str) -> bool:
        """Cancel a call."""
        raise _na("Client.cancel")

    def toolkit(self, **kw: Any) -> "Toolkit":
        """Agent Toolkit over this client."""
        raise _na("Client.toolkit")

    def transfer(self, src: str, dst: str, *, overwrite: bool = False, progress: Any = None,
                 cancel: Any = None) -> dict:
        """Copy "node:path" / "local:path"."""
        raise _na("Client.transfer")

    def close(self) -> None:
        """Close connections."""
        raise _na("Client.close")


class NodeHandle:
    """Convenience methods for one node (WP5a)."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("NodeHandle")

    def follow_logs(self, job_id: Optional[str] = None, service: Optional[str] = None,
                    poll: float = 1.0) -> Iterator[str]:
        """Yield new log text."""
        raise _na("NodeHandle.follow_logs")


class JobHandle:
    """status() / wait() / logs() / stop() of one job (WP5a)."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("JobHandle")


class ServiceHandle:
    """status() / start() / stop() / restart() / logs() / remove() of one service (WP5a)."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("ServiceHandle")


def connect(target: Optional[str] = None, token: Optional[str] = None, **kw: Any) -> Client:
    """Client for a URL, a connection name, "local" or the client.json default."""
    raise _na("connect")


__all__ = ["Target", "HttpTarget", "HubTarget", "Client", "NodeHandle", "JobHandle", "ServiceHandle", "connect"]
