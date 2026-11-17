"""Rotation-aware log reading and clean_terminal_text (design §10.3, WP2).

Phase 0 stub: every function raises unsupported until WP2 merges.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from .errors import unsupported


def _na(what: str):
    return unsupported(f"textlog.{what} not available yet (WP2)")


def read_range(log: Path, offset: Optional[int], max_bytes: int, tail_lines: Optional[int]) -> dict:
    """``{"data","offset","next_offset","size","skipped_bytes","truncated"}`` across rotated files."""
    raise _na("read_range")


def tail_lines(log: Path, n: int, max_bytes: int = 262144) -> bytes:
    """Last n lines across rotation."""
    raise _na("tail_lines")


def decode_chunk(b: bytes) -> Tuple[str, int]:
    """(text, bytes_consumed) dropping an incomplete trailing UTF-8 sequence."""
    raise _na("decode_chunk")


def clean_terminal_text(s: str) -> str:
    """Strip ANSI/OSC codes, apply \\r and backspaces, collapse repeated lines."""
    raise _na("clean_terminal_text")


__all__ = ["read_range", "tail_lines", "decode_chunk", "clean_terminal_text"]
