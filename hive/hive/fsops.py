"""Encoding detection, line paging, edit matching, search walker, check_regex, part tracking (design §11.1, WP3).

Phase 0: ``check_regex`` is a stub = ``re.compile`` + the length limit (bad_request on rejection); ``PartTracker``
is a no-op stub (``prune`` returns 0); the other helpers are unsupported until WP3 merges.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator, List, Tuple

from .errors import bad_request, unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .policy import EffectivePolicy


def _na(what: str):
    return unsupported(f"fsops.{what} not available yet (WP3)")


def check_regex(pattern: str, *, max_len: int = 200) -> "re.Pattern[str]":
    """Compile an agent regex after the §9.7 safety rules (P0 stub: length limit + compile only)."""
    if not isinstance(pattern, str):
        raise bad_request("pattern must be a string")
    if len(pattern) > max_len:
        raise bad_request(f"pattern longer than {max_len} characters", hint="simplify or use literal=true")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise bad_request(f"invalid regex: {e}", hint="simplify or use literal=true")


def detect_encoding(head: bytes) -> Tuple[str, bool]:
    """("utf-8"|"utf-8-sig"|"utf-16-le"|"utf-16-be"|"locale", is_binary)."""
    raise _na("detect_encoding")


def read_lines(path: Path, start_line: int, max_lines: int, max_bytes: int) -> dict:
    """fs.read core."""
    raise _na("read_lines")


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> Tuple[str, int, List[int]]:
    """fs.edit core."""
    raise _na("apply_edit")


def closest_window(text: str, old: str) -> dict:
    """``{"start_line","lines"}`` for not_found details."""
    raise _na("closest_window")


def walk(root: Path, *, depth: int, include_hidden: bool, guard: "EffectivePolicy",
         deadline: float) -> Iterator[os.DirEntry]:
    """Walk skipping denied entries."""
    raise _na("walk")


def is_hidden(entry: Any) -> bool:
    """Dotfile or Windows FILE_ATTRIBUTE_HIDDEN."""
    raise _na("is_hidden")


def long_path(p: Path) -> str:
    """Windows ``\\\\?\\`` prefix when len > 240."""
    raise _na("long_path")


class PartTracker:
    """``state/parts.json`` {path: last_write_iso} (P0 no-op stub)."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def touch(self, part: Path) -> None:
        """Record a write to a part file."""
        return None

    def done(self, part: Path) -> None:
        """Forget a finished part file."""
        return None

    def prune(self, max_idle_s: int = 86400) -> int:
        """Delete idle part files; returns the count."""
        return 0


__all__ = ["check_regex", "detect_encoding", "read_lines", "apply_edit", "closest_window", "walk", "is_hidden",
           "long_path", "PartTracker"]
