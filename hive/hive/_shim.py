"""Process wrapper ``hive _shim --spec PATH``: log pump + rotation, exit.json, stop file, timeouts,
stop_argv/post_exit_argv (design §10.2, WP2).

Eager-import rule (§10.2): every hive.* module the shim can use is imported here at module top; no function-level
``hive.*`` imports. Imports only stdlib, hive.util, hive.errors, hive.proctree, hive.textlog and hive.winapi.
Phase 0 stub: ``main`` reports that the shim is not available yet.
"""
from __future__ import annotations

import os
import sys
from typing import List, Optional

from . import errors, proctree, textlog, util, winapi  # noqa: F401  (eager imports, §10.2)


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point of ``hive _shim`` (WP2; P0 stub exits 70)."""
    util.prune_cwd_path()
    if sys.stdout is None or sys.stderr is None:  # pythonw guard (§13.1)
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return 70


__all__ = ["main"]
