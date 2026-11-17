"""NodeRuntime (the single call pipeline / PEP), node routes, control.json, pause/resume/panic, run_node(),
supervise() (design §13, §5.10, D40, D55, WP1).

Phase 0 stub: the names exist and raise unsupported until WP1 merges.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .config import NodeConfig

EXIT_STOP = 0       # D55: graceful stop
EXIT_RESTART = 75   # D55: requested restart


def _na(what: str):
    return unsupported(f"node.{what} not available yet (WP1)")


class NodeRuntime:
    """The node runtime (WP1; Phase 0 stub)."""

    shutdown_event: threading.Event

    def __init__(self, config: "NodeConfig", *, config_path: Optional[Path] = None, start_managers: bool = True):
        raise _na("NodeRuntime")

    def start(self) -> None:
        """Start the runtime."""
        raise _na("NodeRuntime.start")

    def stop(self) -> None:
        """Stop the runtime (jobs/services keep running)."""
        raise _na("NodeRuntime.stop")

    def is_paused(self) -> bool:
        """PAUSED file present."""
        raise _na("NodeRuntime.is_paused")

    def call(self, identity: "Identity", action: str, params: Optional[dict] = None, *,
             timeout: Optional[float] = None, request_id: Optional[str] = None, approval_id: Optional[str] = None,
             cancel: Optional[threading.Event] = None, via: str = "direct", ip: str = "", progress: Any = None,
             bound_files: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """Full envelope; never raises."""
        raise _na("NodeRuntime.call")

    def cancel(self, request_id: str, identity: "Identity") -> bool:
        """Cancel an in-flight call."""
        raise _na("NodeRuntime.cancel")

    def actions_for(self, identity: "Identity") -> List[dict]:
        """/v1/actions rows."""
        raise _na("NodeRuntime.actions_for")

    def info(self, identity: "Identity") -> dict:
        """node.info result."""
        raise _na("NodeRuntime.info")

    def summary_row(self) -> dict:
        """NodeSummary row for GET /v1/nodes."""
        raise _na("NodeRuntime.summary_row")

    def managed_pids(self) -> Dict[int, str]:
        """jobs ∪ services ∪ keepalive pids."""
        raise _na("NodeRuntime.managed_pids")

    def keepalive_pids(self) -> Dict[int, str]:
        """WSL keepalive pids."""
        raise _na("NodeRuntime.keepalive_pids")

    def add_periodic(self, fn: Callable[[], None], interval_s: float, name: str) -> None:
        """Register a periodic task."""
        raise _na("NodeRuntime.add_periodic")


def run_node(config_path: Optional[str] = None, *, foreground: bool = True, relaunched: bool = False,
             supervise: bool = False) -> int:
    """Run the node daemon; exit 0 after stop, 75 after a requested restart."""
    raise _na("run_node")


def supervise(kind: str, argv: List[str], data_dir: Path) -> int:
    """Windows launcher loop (Task Action)."""
    raise _na("supervise")


def pause(data_dir: Path) -> None:
    """Write PAUSED."""
    raise _na("pause")


def resume(data_dir: Path, *, relay: bool = True) -> None:
    """Remove PAUSED (and relay.disabled unless relay=False)."""
    raise _na("resume")


def panic(cfg: "NodeConfig", data_dir: Path, *, force_after: float = 10.0) -> dict:
    """PAUSED + relay.disabled + services.panic_offline."""
    raise _na("panic")


__all__ = ["EXIT_STOP", "EXIT_RESTART", "NodeRuntime", "run_node", "supervise", "pause", "resume", "panic"]
