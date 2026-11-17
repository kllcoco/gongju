"""Guide topics for the ``guide`` tool: static text + the generated ``presets`` topic (design §17.8, WP6).

Phase 0: ``TOPICS`` (the 10 topic names) is real; ``topic_text`` raises unsupported until WP6 merges.
"""
from __future__ import annotations

from .errors import unsupported

TOPICS = ("overview", "model-serving", "presets", "gateway", "windows", "docker", "network", "cloud-pods",
          "transfers", "troubleshooting")


def topic_text(topic: str) -> str:
    """Text of one guide topic (``presets`` generated from presets.PRESETS)."""
    raise unsupported("guides not available yet (WP6)")


__all__ = ["TOPICS", "topic_text"]
