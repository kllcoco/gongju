"""Windows ctypes helpers: Job Objects, spawn_mode, escape-once relaunch, resume_process, Toolhelp32,
GetProcessTimes, console ctrl, SetThreadExecutionState (design §10.1, D40, WP2).

Phase 0 stubs (imported across WP boundaries by util.boot_key and WP1's run_node/supervise):
``session_winlogon_ctime()`` -> None, ``spawn_mode()`` -> "free", ``relaunch_outside_job()`` raises unsupported.
Imports only the stdlib (the shim imports this module).
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .errors import unsupported

SPAWN_MODES = ("free", "silent", "breakaway", "job", "escaped")


def session_winlogon_ctime() -> Optional[float]:
    """Creation time of this logon session's winlogon.exe (Windows boot_key, D52); P0 stub -> None."""
    return None


def spawn_mode() -> str:
    """free | silent | breakaway | job, computed once at node start (D40); P0 stub -> "free"."""
    return "free"


def relaunch_outside_job(argv: List[str], env: Dict[str, str]) -> Tuple[int, Optional[float]]:
    """Relaunch once through WMI Win32_Process.Create, returning (pid, ctime) (D40; WP2)."""
    raise unsupported("WMI relaunch not available yet")


__all__ = ["SPAWN_MODES", "session_winlogon_ctime", "spawn_mode", "relaunch_outside_job"]
