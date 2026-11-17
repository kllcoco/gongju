"""Process and listener listing: list_processes(), process_info(), listeners() (design §12.3, WP4).

Phase 0 stubs: ``listeners()`` -> ``([], "unavailable")``, ``process_info()`` -> None,
``list_processes()`` -> ``([], "unavailable")`` until WP4 merges.
"""
from __future__ import annotations

from typing import List, Optional, Tuple


def list_processes(*, cmdline: bool = True) -> Tuple[List[dict], str]:
    """(rows, method) (P0 stub: no rows)."""
    return [], "unavailable"


def process_info(pid: int) -> Optional[dict]:
    """``{"pid","ppid","name","cmdline","uid","user","create_time"}`` or None (P0 stub: None)."""
    return None


def listeners() -> Tuple[List[dict], str]:
    """(listening sockets, method) (P0 stub: none)."""
    return [], "unavailable"


__all__ = ["list_processes", "process_info", "listeners"]
