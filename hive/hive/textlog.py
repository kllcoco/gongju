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
import time
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
        st = _read_state_full(self.path)
        self.rotated = st[0]
        self._seq = st[1]
        if st[1] & 1:   # a writer died inside a rotation: settle the state from the files (§10.3 seqlock)
            self.rotated = _settle(self.path, st)
        self._f = open(self.path, "ab")
        try:
            self._size = os.fstat(self._f.fileno()).st_size
        except OSError:
            self._size = 0
        if st[1] & 1 or not self.path.with_name(self.path.name + STATE_SUFFIX).exists():
            self._write_state()

    def _write_state(self, rotating: Optional[int] = None) -> None:
        """Even ``seq``: settled; odd ``seq`` (with ``rotating`` = the size of the file being rotated): a rotation is
        in progress and readers wait (Phase 2 review: renames and the new state are not one atomic step)."""
        nxt = self._seq + 1
        if (nxt & 1) != (0 if rotating is None else 1):
            nxt += 1
        self._seq = nxt
        obj: dict = {"v": 1, "rotated_bytes": self.rotated, "seq": self._seq}
        if rotating is not None:
            obj["rotating"] = int(rotating)
        util.write_json_atomic(self.path.with_name(self.path.name + STATE_SUFFIX), obj)

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
            self._write_state(rotating=size)          # odd seq: readers wait until the even state below
        except OSError:
            self._f = open(self.path, "ab")
            return
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
            self._write_state()                       # nothing moved: settle with the old rotated_bytes
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

def _read_state_full(log: Path) -> Tuple[int, int, Optional[int]]:
    """(rotated_bytes, seq, rotating size or None); seq is odd while a rotation is in progress."""
    try:
        d = json.loads(Path(str(log) + STATE_SUFFIX).read_text(encoding="utf-8") or "{}")
        if not isinstance(d, dict):
            return 0, 0, None
        n = max(0, int(d.get("rotated_bytes", 0)))
        seq = d.get("seq", 0)
        seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq >= 0 else 0
        rot = d.get("rotating")
        rot = rot if isinstance(rot, int) and not isinstance(rot, bool) and rot >= 0 else None
        return n, seq, rot
    except (OSError, ValueError, TypeError):
        return 0, 0, None


def _read_state(log: Path) -> int:
    return _read_state_full(log)[0]


def _settle(log: Path, st: Tuple[int, int, Optional[int]]) -> int:
    """rotated_bytes for a state left odd (the writer died mid-rotation): the rotation counts as done when ``.1``
    holds the rotated size and the current file is smaller than it."""
    rotated, _seq, size = st
    if not size:
        return rotated
    try:
        one = log.with_name(f"{log.name}.1").stat().st_size
    except OSError:
        return rotated
    try:
        cur = log.stat().st_size
    except OSError:
        cur = 0
    return rotated + size if one == size and cur < size else rotated


ROTATION_WAIT_S = 2.0


def _segments(log: Path) -> Tuple[List[Tuple[Path, int, int]], int, int]:
    """([(path, logical start, length)] oldest first, earliest offset, logical size). A seqlock on ``.state``:
    while its ``seq`` is odd (a rotation in progress) or changed while the files were listed, read again."""
    log = Path(log)
    deadline = time.monotonic() + ROTATION_WAIT_S
    attempt = 0
    while True:
        attempt += 1
        st = _read_state_full(log)
        rotated = st[0]
        if st[1] & 1:
            if time.monotonic() < deadline:
                time.sleep(min(0.02, 0.001 * attempt))
                continue
            rotated = _settle(log, st)        # the writer is gone (or stuck): judge from the files
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
        if _read_state_full(log)[:2] != st[:2]:
            if time.monotonic() < deadline:
                continue   # a rotation happened while we looked; retry
            return [(log, 0, 0)], 0, 0
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
        # Phase 2 review: never move a follower backwards (a reader that saw a bigger size keeps its offset)
        return {"data": b"", "offset": offset, "next_offset": offset, "size": size, "skipped_bytes": skipped,
                "truncated": False}
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
    b = bytes(b)
    cut = _utf8_incomplete_tail(b)
    if cut:
        try:
            b[:len(b) - cut].decode("utf-8")
        except UnicodeDecodeError:
            cut = 0    # not UTF-8 text at all (e.g. GBK): nothing to hold back
    if not cut and util.IS_WINDOWS:
        try:
            b.decode("utf-8")
        except UnicodeDecodeError:
            # Phase 2 review: ANSI code page output (GBK, …) cut inside a double-byte character: hold back the
            # trailing lead byte, so neither this chunk nor the next one is decoded misaligned
            for enc in ("mbcs", "gbk"):
                try:
                    b.decode(enc)
                    break
                except LookupError:
                    continue
                except UnicodeDecodeError:
                    try:
                        b[:-1].decode(enc)
                    except UnicodeDecodeError:
                        break
                    cut = 1
                    break
    consumed = len(b) - cut
    return util.decode_text(b[:consumed]), consumed


def dbcs_skip(before: bytes, data: bytes, *, aligned_start: bool = False) -> int:
    """1 when ``data`` starts on the trail byte of a double-byte (GBK/ANSI code page) character whose lead byte is the
    last byte of ``before`` (the bytes preceding ``data`` in the log), else 0. Windows only, and only for text that is
    not UTF-8 (Phase 2 review: a tail cut at an arbitrary byte). Walks forward from the last byte below 0x40 in
    ``before`` (never a trail byte, so a character boundary), or from the start of ``before`` when
    ``aligned_start`` (it begins at the start of the log)."""
    if not util.IS_WINDOWS or not data or not before:
        return 0
    try:
        (before[-4:] + data[:64]).decode("utf-8")
        return 0
    except UnicodeDecodeError:
        pass
    start = -1
    for i in range(len(before) - 1, -1, -1):
        if before[i] < 0x40:
            start = i
            break
    if start < 0 and not aligned_start:
        return 0
    pos = start + 1
    n = len(before)
    while pos < n:
        pos += 2 if 0x81 <= before[pos] <= 0xFE else 1
    return 1 if pos > n else 0


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


__all__ = ["RotatingWriter", "read_range", "tail_lines", "decode_chunk", "dbcs_skip", "clean_terminal_text", "log_size",
           "rotated_bytes", "head_bytes", "last_line"]
