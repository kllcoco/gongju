"""Rotation-aware log writing/reading and clean_terminal_text (design §10.2 step 4, §10.3, WP2).

Log layout (written only by the shim): ``x.log`` (current), ``x.log.1`` … ``x.log.<keep>`` (older, ``.1`` newest) and
``x.log.state`` ``{"v":1,"rotated_bytes":N}`` = the logical offset at which the current ``x.log`` starts, so absolute
(logical) offsets stay valid across rotations. Readers compute the earliest available offset from the rotated files.
Imports only stdlib and ``hive.util`` (the shim imports this module eagerly, §10.2).
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import List, Optional, Tuple

from . import util

STATE_SUFFIX = ".state"
BLOCK = 65536


# ---------------------------------------------------------------------------------------------------------------
# writing (shim side)

class RotatingWriter:
    """Append-only log writer with size rotation and a ``.state`` file holding ``rotated_bytes`` (thread-safe)."""

    def __init__(self, path: Path, *, max_bytes: int, keep: int):
        self.path = Path(path)
        self.keep = max(0, int(keep))
        total = max(4096, int(max_bytes))
        self.limit = max(1024, total // (self.keep + 1))
        self._lock = threading.Lock()
        self.rotated = _read_state(self.path)
        self._f = open(self.path, "ab")
        try:
            self._size = os.fstat(self._f.fileno()).st_size
        except OSError:
            self._size = 0
        if not self.path.with_name(self.path.name + STATE_SUFFIX).exists():
            self._write_state()

    def _write_state(self) -> None:
        util.write_json_atomic(self.path.with_name(self.path.name + STATE_SUFFIX),
                               {"v": 1, "rotated_bytes": self.rotated})

    def write(self, data: bytes) -> None:
        """Append ``data`` (flushed), rotating first when the current file is full."""
        if not data:
            return
        with self._lock:
            if self._f is None:
                return
            if self._size > 0 and self._size + len(data) > self.limit:
                self._rotate()
            self._f.write(data)
            self._f.flush()
            self._size += len(data)

    def _rotate(self) -> None:
        assert self._f is not None
        self._f.close()
        size = self._size
        try:
            if self.keep > 0:
                oldest = self.path.with_name(f"{self.path.name}.{self.keep}")
                if oldest.exists():
                    oldest.unlink()
                for i in range(self.keep - 1, 0, -1):
                    src = self.path.with_name(f"{self.path.name}.{i}")
                    if src.exists():
                        util.replace_with_retry(src, self.path.with_name(f"{self.path.name}.{i + 1}"))
                util.replace_with_retry(self.path, self.path.with_name(f"{self.path.name}.1"))
                self._f = open(self.path, "ab")
            else:
                self._f = open(self.path, "wb")
        except OSError:
            self._f = open(self.path, "ab")
            return
        self.rotated += size
        self._size = 0
        self._write_state()

    def close(self) -> None:
        """Close the file (idempotent)."""
        with self._lock:
            if self._f is not None:
                try:
                    self._f.close()
                finally:
                    self._f = None


# ---------------------------------------------------------------------------------------------------------------
# reading

def _read_state(log: Path) -> int:
    try:
        d = json.loads(Path(str(log) + STATE_SUFFIX).read_text(encoding="utf-8") or "{}")
        n = int(d.get("rotated_bytes", 0)) if isinstance(d, dict) else 0
        return max(0, n)
    except (OSError, ValueError, TypeError):
        return 0


def _segments(log: Path) -> Tuple[List[Tuple[Path, int, int]], int, int]:
    """([(path, logical start, length)] oldest first, earliest offset, logical size)."""
    log = Path(log)
    for _attempt in range(3):
        rotated = _read_state(log)
        try:
            cur = log.stat().st_size
        except OSError:
            cur = 0
        rotated_files: List[Tuple[Path, int]] = []
        i = 1
        while True:
            p = log.with_name(f"{log.name}.{i}")
            try:
                rotated_files.append((p, p.stat().st_size))
            except OSError:
                break
            i += 1
        if _read_state(log) != rotated:
            continue   # a rotation happened while we looked; retry
        segs: List[Tuple[Path, int, int]] = []
        start = rotated
        for p, n in rotated_files:
            if start - n < 0:
                break
            start -= n
            segs.insert(0, (p, start, n))
        segs.append((log, rotated, cur))
        earliest = segs[0][1] if segs else rotated
        return segs, earliest, rotated + cur
    return [(log, 0, 0)], 0, 0


def _read_logical(segs: List[Tuple[Path, int, int]], start: int, length: int) -> bytes:
    out = bytearray()
    end = start + max(0, length)
    for p, s, n in segs:
        if n <= 0 or s + n <= start or s >= end:
            continue
        a = max(start, s) - s
        b = min(end, s + n) - s
        try:
            with open(p, "rb") as f:
                f.seek(a)
                out += f.read(b - a)
        except OSError:
            continue
    return bytes(out)


def log_size(log: Path) -> int:
    """Logical size of a log (rotated bytes + current file)."""
    return _segments(Path(log))[2]


def rotated_bytes(log: Path) -> int:
    """Bytes rotated out of the current file (the ``.state`` value)."""
    return _read_state(Path(log))


def read_range(log: Path, offset: Optional[int], max_bytes: int, tail_lines: Optional[int]) -> dict:
    """``{"data": bytes, "offset", "next_offset", "size", "skipped_bytes", "truncated"}`` across rotated files.

    ``offset`` None reads the last ``tail_lines`` lines (at most ``max_bytes``); otherwise up to ``max_bytes`` from the
    logical ``offset`` (a rotated-away offset starts at the earliest byte still present and reports ``skipped_bytes``).
    """
    segs, earliest, size = _segments(Path(log))
    max_bytes = max(1, int(max_bytes))
    if offset is None:
        data = _tail(segs, earliest, size, int(tail_lines or 0), max_bytes)
        start = size - len(data)
        truncated = start > earliest and (tail_lines is None or data.count(b"\n") < int(tail_lines or 0))
        return {"data": data, "offset": start, "next_offset": size, "size": size, "skipped_bytes": 0,
                "truncated": bool(truncated)}
    offset = max(0, int(offset))
    skipped = 0
    start = offset
    if start < earliest:
        skipped = earliest - start
        start = earliest
    if start > size:
        start = size
    n = min(max_bytes, size - start)
    data = _read_logical(segs, start, n)
    nxt = start + len(data)
    return {"data": data, "offset": start, "next_offset": nxt, "size": size, "skipped_bytes": skipped,
            "truncated": nxt < size}


def _tail(segs: List[Tuple[Path, int, int]], earliest: int, size: int, n: int, max_bytes: int) -> bytes:
    if n <= 0:
        return b""
    pos = size
    buf = b""
    while pos > earliest and len(buf) < max_bytes:
        body = buf[:-1] if buf.endswith(b"\n") else buf
        if body.count(b"\n") >= n:
            break
        blk = min(BLOCK, pos - earliest)
        pos -= blk
        buf = _read_logical(segs, pos, blk) + buf
    body_end = len(buf) - 1 if buf.endswith(b"\n") else len(buf)
    idx = body_end
    found = 0
    while found < n:
        j = buf.rfind(b"\n", 0, idx)
        if j < 0:
            idx = -1
            break
        found += 1
        idx = j
    out = buf[idx + 1:] if idx >= 0 else buf
    if len(out) > max_bytes:
        out = out[-max_bytes:]
    return out


def tail_lines(log: Path, n: int, max_bytes: int = 262144) -> bytes:
    """The last ``n`` lines of a log (reading backwards in 64 KiB blocks across rotation), at most ``max_bytes``."""
    segs, earliest, size = _segments(Path(log))
    return _tail(segs, earliest, size, int(n), int(max_bytes))


def read_at(log: Path, start: int, length: int) -> bytes:
    """Raw logical bytes ``[start, start+length)`` still present (clamped; used for mask_edges context)."""
    segs, earliest, size = _segments(Path(log))
    a = max(earliest, int(start))
    b = min(size, int(start) + max(0, int(length)))
    return _read_logical(segs, a, b - a) if b > a else b""


def head_bytes(log: Path, n: int) -> Tuple[bytes, int]:
    """(first ``n`` bytes still present, their logical offset)."""
    segs, earliest, size = _segments(Path(log))
    return _read_logical(segs, earliest, min(n, size - earliest)), earliest


def last_line(log: Path) -> str:
    """Last non-empty log line, cleaned (used as a job error when a helper wrote no result)."""
    raw = tail_lines(Path(log), 3, 16384)
    lines = [x for x in clean_terminal_text(util.decode_text(raw)).split("\n") if x.strip()]
    return lines[-1].strip()[:500] if lines else ""


def _utf8_incomplete_tail(b: bytes) -> int:
    """Number of trailing bytes that form an incomplete UTF-8 sequence (0..3)."""
    n = len(b)
    for back in range(1, min(4, n) + 1):
        c = b[n - back]
        if c & 0xC0 == 0x80:          # continuation byte, keep looking for the lead
            continue
        if c & 0x80 == 0:              # ASCII
            return 0
        if c & 0xE0 == 0xC0:
            need = 2
        elif c & 0xF0 == 0xE0:
            need = 3
        elif c & 0xF8 == 0xF0:
            need = 4
        else:
            return 0
        return back if back < need else 0
    return 0


def decode_chunk(b: bytes) -> Tuple[str, int]:
    """(text, bytes_consumed): drops an incomplete trailing UTF-8 sequence, decodes the rest (UTF-8, then mbcs/gbk)."""
    cut = _utf8_incomplete_tail(b)
    if cut:
        try:
            bytes(b[:len(b) - cut]).decode("utf-8")
        except UnicodeDecodeError:
            cut = 0    # not UTF-8 text at all (e.g. GBK): nothing to hold back
    consumed = len(b) - cut
    return util.decode_text(bytes(b[:consumed])), consumed


_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC = re.compile(r"\x1b.?", re.DOTALL)


def _apply_backspaces(line: str) -> str:
    if "\b" not in line:
        return line
    out: List[str] = []
    for ch in line:
        if ch == "\b":
            if out:
                out.pop()
        else:
            out.append(ch)
    return "".join(out)


def clean_terminal_text(s: str) -> str:
    """CRLF -> LF; strip ANSI CSI/OSC/other escapes; keep the text after the last CR per line; apply backspaces;
    collapse more than 3 identical consecutive lines into a "⟨previous line repeated N times⟩" marker."""
    if not s:
        return s
    s = s.replace("\r\n", "\n")
    s = _OSC.sub("", s)
    s = _CSI.sub("", s)
    s = _ESC.sub("", s)
    lines = []
    for line in s.split("\n"):
        if "\r" in line:
            parts = line.split("\r")
            # keep the final state of a progress bar: the last non-empty segment after a carriage return
            line = parts[-1] if parts[-1] else next((p for p in reversed(parts) if p), "")
        lines.append(_apply_backspaces(line))
    out: List[str] = []
    i = 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[i]:
            j += 1
        run = j - i + 1
        if run > 3 and not (j == len(lines) - 1 and lines[i] == ""):
            out.append(lines[i])
            out.append(f"⟨previous line repeated {run - 1} times⟩")
        else:
            out.extend(lines[i:j + 1])
        i = j + 1
    return "\n".join(out)


__all__ = ["RotatingWriter", "read_range", "tail_lines", "decode_chunk", "clean_terminal_text", "log_size",
           "rotated_bytes", "head_bytes", "last_line"]
