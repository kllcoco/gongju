"""Action handlers: net.http, net.download, net.ports (listener lookup from procinfo, WP4) (design §9.8, WP3).

Phase 0 stub: no handler is registered yet, so these actions report ``unsupported`` until WP3 merges.
Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

from . import handler  # noqa: F401  (WP3 registers its handlers with @handler)

__all__: list = []
