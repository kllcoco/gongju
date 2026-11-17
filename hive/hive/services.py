"""ServiceManager + supervisor + health + container rules + panic_offline (design §10.5, WP2).

Phase 0 stubs: ``ServiceManager.endpoint_for`` (unsupported), ``ServiceManager.policy_view`` (-> empty view),
``panic_offline`` (-> the "services module not available" result). The lifecycle and probe methods the node runtime
calls are neutral (``start``/``shutdown`` no-ops, ``managed_pids`` -> ``{}``, ``endpoints`` -> ``[]``), so
``NodeRuntime.start()`` works before WP2 merges (§13.1); everything else is unsupported until WP2 merges.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .actions import ActionContext
    from .netutil import AuthSpec, NetGuard
    from .node import NodeRuntime


def _na(what: str):
    return unsupported(f"services.{what} not available yet (WP2)")


class ServiceManager:
    """Managed services (WP2; Phase 0 stub)."""

    def __init__(self, rt: "NodeRuntime"):
        self.rt = rt

    def start(self) -> None:
        """Load, adopt, autostart, start the supervisor (P0 stub: no-op)."""
        return None

    def shutdown(self) -> None:
        """Stop the supervisor; services keep running (P0 stub: no-op)."""
        return None

    def define(self, ctx: "ActionContext", d: dict) -> dict:
        """service.define."""
        raise _na("ServiceManager.define")

    def start_service(self, ctx: "ActionContext", name: str, wait: float) -> dict:
        """service.start."""
        raise _na("ServiceManager.start_service")

    def stop_service(self, ctx: "ActionContext", name: str, wait: float) -> dict:
        """service.stop."""
        raise _na("ServiceManager.stop_service")

    def restart(self, ctx: "ActionContext", name: str, wait: float) -> dict:
        """service.restart."""
        raise _na("ServiceManager.restart")

    def remove(self, ctx: "ActionContext", name: str, delete_logs: bool, wait: float) -> dict:
        """service.remove."""
        raise _na("ServiceManager.remove")

    def status(self, name: str, tail_lines: int = 30, wait: float = 0) -> dict:
        """service.status."""
        raise _na("ServiceManager.status")

    def list(self) -> dict:
        """service.list."""
        raise _na("ServiceManager.list")

    def logs(self, name: str, **job_logs_params: Any) -> dict:
        """service.logs."""
        raise _na("ServiceManager.logs")

    def endpoint_for(self, name: str) -> Tuple[str, str, Optional["AuthSpec"]]:
        """(base_url, api, auth) for model.* and the gateway."""
        raise _na("ServiceManager.endpoint_for")

    def endpoints(self) -> List[dict]:
        """Endpoints for model.endpoints (P0 stub: ``[]``)."""
        return []

    def policy_view(self) -> dict:
        """``{"endpoints": [...], "code_dirs": [...]}`` (P0 stub: both empty)."""
        return {"endpoints": [], "code_dirs": []}

    def managed_pids(self) -> Dict[int, str]:
        """pid -> "service:<name>" (P0 stub: ``{}``)."""
        return {}


def run_stop_command(definition: dict, *, timeout: float = 30.0) -> Optional[int]:
    """Run a definition's stop_command without a runtime (WP2)."""
    raise _na("run_stop_command")


def panic_offline(data_dir: Path, node_id: str, *, force_after: float = 10.0) -> dict:
    """Stop every job/service/container without a runtime (§5.10); P0 stub reports the module as unavailable."""
    return {"jobs": [], "services": [], "containers": [], "errors": ["services module not available"]}


def _http_status(url: str, timeout: float, guard: "NetGuard") -> Optional[int]:
    """Health-check seam (WP2 tests patch it)."""
    raise _na("_http_status")


__all__ = ["ServiceManager", "run_stop_command", "panic_offline"]
