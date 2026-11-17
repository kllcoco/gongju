"""Action handlers: system.info (incl. `which`), proc.list, proc.kill (kill via proctree.kill_tree from WP2) (design §9.2–§9.3, WP4).

Phase 0 stub: no handler is registered yet, so these actions report ``unsupported`` until WP4 merges.
Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

from . import handler  # noqa: F401  (WP4 registers its handlers with @handler)

__all__: list = []
