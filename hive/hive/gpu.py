"""NVIDIA/Apple/Windows GPU detection (AMD names only) (design §12.2, WP4).

Phase 0 stub: ``detect`` returns the neutral "no GPU" result (node.info composes it, §9.2); the other functions raise
unsupported until WP4 merges.
"""
from __future__ import annotations

from typing import Dict, Optional

from .errors import unsupported


def _na(what: str):
    return unsupported(f"gpu.{what} not available yet (WP4)")


def detect(processes: bool = True, timeout: float = 8.0) -> dict:
    """{"backend","gpus","processes","errors","driver","cuda"} (P0 stub: no GPU, ``errors`` names the stub)."""
    return {"backend": None, "gpus": [], "processes": [], "errors": ["gpu module not available"], "driver": None,
            "cuda": None}


def process_memory() -> Dict[int, int]:
    """pid -> MiB (nvidia compute apps)."""
    raise _na("process_memory")


def find_tool(name: str) -> Optional[str]:
    """Locate a GPU tool through the trusted path and fixed locations."""
    raise _na("find_tool")


__all__ = ["detect", "process_memory", "find_tool"]
