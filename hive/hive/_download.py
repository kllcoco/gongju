"""``hive _download --spec PATH`` subprocess: file and pull modes, Range resume, hashes, progress.json/result.json
(design §11.3, WP3).

Eager-import rule (§10.2): imports only stdlib, hive.util, hive.errors, hive.netproxy, hive.netutil and hive.hfhub,
all at module top (never a WP1 module). Phase 0 stub: ``main`` reports that the downloader is not available yet.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

from . import errors, hfhub, netproxy, netutil, util  # noqa: F401  (eager imports, §10.2)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point of ``hive _download`` (WP3; P0 stub exits 70)."""
    util.prune_cwd_path()
    if sys.stdout is None or sys.stderr is None:  # pythonw guard (§13.1)
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return 70


__all__ = ["main"]
