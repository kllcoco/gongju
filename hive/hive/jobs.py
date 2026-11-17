"""JobManager + reaper: background jobs under the shim, adoption, logs, prune (design §10.4, WP2).

Phase 0 stub. The lifecycle and probe methods the node runtime calls are neutral (``start``/``shutdown`` are no-ops,
``managed_pids`` returns ``{}``), so ``NodeRuntime.start()`` works before WP2 merges (§13.1 degrade path); every
other method raises unsupported until WP2 merges (``start_job`` is imported across WP boundaries by
net.download / model.pull).
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Optional

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .actions import ActionContext
    from .node import NodeRuntime


def _na(what: str):
    return unsupported(f"jobs.{what} not available yet (WP2)")


class JobManager:
    """Background jobs (WP2; Phase 0 stub)."""

    def __init__(self, rt: "NodeRuntime"):
        self.rt = rt

    def start(self) -> None:
        """Load records, adopt, start the reaper (P0 stub: no-op)."""
        return None

    def shutdown(self) -> None:
        """Stop the reaper; never kills jobs (P0 stub: no-op)."""
        return None

    def start_job(self, ctx: "ActionContext", *, kind: str = "job", argv_or_cmdline: Any, shell: str, cwd: Path,
                  env: Dict[str, str], stdin: Optional[str], name: Optional[str], timeout: float,
                  timeout_grace: float = 10.0, merge_stderr: bool, log_max_bytes: int, log_keep: int = 1,
                  keep_awake: bool = False, tags: Any = (), command_display: str = "",
                  extra_files: Optional[Dict[str, bytes]] = None) -> dict:
        """Start a job under the shim; returns JobInfo."""
        raise _na("JobManager.start_job")

    def get(self, job_id: str) -> dict:
        """JobInfo."""
        raise _na("JobManager.get")

    def list(self, state: str = "all", limit: int = 20, include_shell: bool = False) -> dict:
        """job.list."""
        raise _na("JobManager.list")

    def wait(self, job_id: str, timeout: float, cancel: threading.Event) -> dict:
        """Block until the job ends or the timeout."""
        raise _na("JobManager.wait")

    def logs(self, job_id: str, **job_logs_params: Any) -> dict:
        """job.logs."""
        raise _na("JobManager.logs")

    def stop(self, job_id: str, force: bool = False, grace: float = 10.0) -> dict:
        """job.stop."""
        raise _na("JobManager.stop")

    def promote(self, job_id: str, *, name: str) -> None:
        """shell.run yield -> kind "job"."""
        raise _na("JobManager.promote")

    def remove(self, job_id: Optional[str] = None, older_than_hours: Optional[float] = None) -> dict:
        """Internal removal (prune, tests)."""
        raise _na("JobManager.remove")

    def set_progress(self, job_id: str, progress: dict) -> None:
        """Progress of downloads and pulls."""
        raise _na("JobManager.set_progress")

    def running_count(self, kind: str) -> int:
        """Running jobs of a kind (model.pull cap)."""
        raise _na("JobManager.running_count")

    def managed_pids(self) -> Dict[int, str]:
        """pid -> "job:<id>" (P0 stub: ``{}``)."""
        return {}

    def prune(self) -> None:
        """Retention pruning."""
        raise _na("JobManager.prune")


__all__ = ["JobManager"]
