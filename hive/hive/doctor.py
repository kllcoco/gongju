"""``hive doctor`` checks (files, listen, network, secrets, docker, wsl, windows, linux, proxy, policy, relay)
(design §12.4, WP4).

Phase 0 stub: ``run`` raises unsupported until WP4 merges.
"""
from __future__ import annotations

from typing import List

from .errors import unsupported


def run(kind: str, *, network: bool = True, deep: bool = False) -> List[dict]:
    """``[{"check","status":"ok|warn|fail","detail","fix"}]``."""
    raise unsupported("doctor not available yet (WP4)")


__all__ = ["run"]
