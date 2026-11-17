"""Action handlers: job.start, job.status, job.logs, job.list, job.stop (design §9.5, WP2).

Phase 0 stub: no handler is registered yet, so these actions report ``unsupported`` until WP2 merges.
Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

from . import handler  # noqa: F401  (WP2 registers its handlers with @handler)

__all__: list = []
