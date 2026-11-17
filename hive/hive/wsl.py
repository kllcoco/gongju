"""WSL detection, distro listing, Windows-side keepalive (design §12.4 and §20.7, WP4).

Phase 0 stub: every function raises unsupported until WP4 merges.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from .errors import unsupported


def _na(what: str):
    return unsupported(f"wsl.{what} not available yet (WP4)")


def is_wsl() -> bool:
    """Running inside WSL2."""
    raise _na("is_wsl")


def wsl_info() -> Optional[dict]:
    """{distro, networking, systemd} inside WSL, else None."""
    raise _na("wsl_info")


def list_distros() -> List[dict]:
    """Windows: installed distros."""
    raise _na("list_distros")


class Keepalive:
    """Windows-side WSL keepalive processes (spawned with proctree.spawn_detached)."""

    def start(self, entries: List[dict]) -> None:
        """Start/adopt keepalive processes."""
        raise _na("Keepalive.start")

    def check(self) -> None:
        """Restart dead keepalives."""
        raise _na("Keepalive.check")

    def status(self) -> List[dict]:
        """[{"distro","pid","running"}]."""
        raise _na("Keepalive.status")

    def pids(self) -> Dict[int, str]:
        """{pid: "wsl-keepalive:<distro>"}."""
        raise _na("Keepalive.pids")


__all__ = ["is_wsl", "wsl_info", "list_distros", "Keepalive"]
