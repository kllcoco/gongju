"""Action handlers: node.info (composes sysinfo/gpu/proctree probes through lazy null-fallback imports) (design §9.2, WP1).

Phase 0 stub: no handler is registered yet, so these actions report ``unsupported`` until WP1 merges.
Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

from . import handler  # noqa: F401  (WP1 registers its handlers with @handler)

__all__: list = []
