"""Service installation: systemd / schtasks XML / launchd / watchdog from util.launcher_argv, restart per method
(design §20.1, D55, WP4).

Phase 0 stub: ``restart`` raises unsupported "service install not available yet" (WP6a's ``hive node restart``
reports that until WP4 merges); the other functions are unsupported too.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .errors import unsupported

METHODS = ("auto", "systemd", "launchd", "schtasks", "watchdog")


@dataclass
class InstallResult:
    """What an install/uninstall/restart did (or would do with dry_run)."""
    method: str
    files: List[str] = field(default_factory=list)
    commands: List[List[str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def _na() -> Exception:
    return unsupported("service install not available yet")


def detect_method() -> str:
    """auto-detected install method."""
    raise _na()


def install(kind: str, config_path: Path, *, method: str = "auto", system: bool = False, dry_run: bool = False,
            capture_path: bool = True) -> InstallResult:
    """Install the node/hub as a service."""
    raise _na()


def uninstall(kind: str, *, method: str = "auto", system: bool = False, dry_run: bool = False,
              stop_services: bool = False) -> InstallResult:
    """Remove the service installation."""
    raise _na()


def status(kind: str) -> dict:
    """Installation status."""
    raise _na()


def restart(kind: str, *, dry_run: bool = False) -> InstallResult:
    """§13.1 restart sequence (holder exit, method restart or start, verify, Windows retry)."""
    raise _na()


__all__ = ["METHODS", "InstallResult", "detect_method", "install", "uninstall", "status", "restart"]
