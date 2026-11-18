"""Regex worker subprocess: agent-supplied regexes never run in the node process (design §9.7, audit round 4).

A regex with polynomial backtracking holds the GIL inside CPython's ``re`` for as long as it runs; in the node that
freezes every request. ``fs.search`` (regex mode) and service ``ready_pattern`` checks therefore send the lines to
match to a short-lived ``python -I -c WORKER_SOURCE`` child and get back the indices of matching lines. The child
is killed at the caller's deadline (a watchdog thread; POSIX children also arm ``alarm()`` for the same deadline,
so an orphan never outlives it). ``-I`` keeps the cwd, PYTHONPATH and user site out of the child's ``sys.path``;
the child imports only the stdlib and never imports ``hive``.

Wire format (both ways): 4-byte big-endian length + ASCII JSON. Parent → child: ``{"pattern","flags","alarm_s"}``,
then ``{"lines": [...]}`` frames; child → parent: ``{"ok": true}`` or ``{"error": "..."}``, then ``{"hits": [i…]}``
per frame. EOF on the child's stdin ends it.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
import threading
import time
from typing import List, Optional

MAX_FRAME = 64 << 20

WORKER_SOURCE = r'''
import json, os, re, struct, sys

def _read(f, n):
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf

def _frame(f):
    h = _read(f, 4)
    if h is None:
        return None
    n = struct.unpack(">I", h)[0]
    if n > %d:
        return None
    body = _read(f, n)
    return None if body is None else json.loads(body.decode("ascii"))

def _send(f, obj):
    body = json.dumps(obj, ensure_ascii=True).encode("ascii")
    f.write(struct.pack(">I", len(body)) + body)
    f.flush()

def main():
    inp, out = sys.stdin.buffer, sys.stdout.buffer
    hdr = _frame(inp)
    if not isinstance(hdr, dict):
        return 2
    alarm = int(hdr.get("alarm_s") or 0)
    if alarm > 0 and hasattr(os, "fork"):
        import signal
        signal.alarm(alarm)        # default SIGALRM action ends the process, even inside a running match
    try:
        rx = re.compile(hdr["pattern"], int(hdr.get("flags") or 0))
    except Exception as e:
        _send(out, {"error": "%%s: %%s" %% (type(e).__name__, e)})
        return 1
    _send(out, {"ok": True})
    search = rx.search
    while True:
        req = _frame(inp)
        if req is None:
            return 0
        _send(out, {"hits": [i for i, s in enumerate(req.get("lines") or ()) if search(s) is not None]})

sys.exit(main())
''' % MAX_FRAME


def _read_exact(f, n: int) -> Optional[bytes]:
    buf = b""
    while len(buf) < n:
        chunk = f.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class RegexWorker:
    """One worker child for one pattern. ``search_lines`` returns the matching indices, or None once the deadline
    passed, ``cancel`` was set or the child died (it is killed then; ``timed_out``/``cancelled`` say why).
    Use as a context manager; ``close`` ends the child."""

    def __init__(self, pattern: str, flags: int = 0, *, deadline: float, cancel: Optional[threading.Event] = None):
        self.deadline = float(deadline)
        self.cancel = cancel
        self.timed_out = False
        self.cancelled = False
        self.error: Optional[str] = None
        self._done = threading.Event()
        kw = {}
        if os.name == "nt":  # pragma: no cover - Windows only
            kw["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        self.proc = subprocess.Popen([sys.executable, "-I", "-c", WORKER_SOURCE], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True, **kw)
        self._watch = threading.Thread(target=self._watchdog, name="hive-regex-watchdog", daemon=True)
        self._watch.start()
        remaining = max(1, int(self.deadline - time.monotonic()) + 2)
        reply = self._exchange({"pattern": pattern, "flags": int(flags), "alarm_s": remaining})
        if reply is not None and "error" in reply:
            self.error = str(reply["error"])
            self.close()
        elif reply is None or not reply.get("ok"):
            self.close()

    @property
    def alive(self) -> bool:
        return self.error is None and not self._done.is_set()

    def _watchdog(self) -> None:
        while not self._done.is_set():
            left = self.deadline - time.monotonic()
            if left <= 0:
                self.timed_out = True
                break
            if self.cancel is not None and self.cancel.is_set():
                self.cancelled = True
                break
            self._done.wait(min(left, 0.2))
        self._kill()

    def _kill(self) -> None:
        try:
            if self.proc.poll() is None:
                self.proc.kill()
        except OSError:
            pass

    def _exchange(self, obj: dict) -> Optional[dict]:
        if self._done.is_set():
            return None
        body = json.dumps(obj, ensure_ascii=True).encode("ascii")
        try:
            self.proc.stdin.write(struct.pack(">I", len(body)) + body)
            self.proc.stdin.flush()
            head = _read_exact(self.proc.stdout, 4)
            if head is None:
                return None
            n = struct.unpack(">I", head)[0]
            if n > MAX_FRAME:
                return None
            data = _read_exact(self.proc.stdout, n)
            return None if data is None else json.loads(data.decode("ascii"))
        except (OSError, ValueError):
            return None

    def search_lines(self, lines: List[str]) -> Optional[List[int]]:
        """Indices of the lines ``pattern`` matches (``re.search``), or None (deadline, cancel, child died)."""
        if not self.alive:
            return None
        reply = self._exchange({"lines": list(lines)})
        if reply is None or not isinstance(reply.get("hits"), list):
            self.close()
            return None
        return [int(i) for i in reply["hits"]]

    def close(self) -> None:
        """End the child (the watchdog kills it; it is idle or past its deadline by now) and reap it."""
        self._done.set()
        self._kill()
        for f in (self.proc.stdin, self.proc.stdout):
            try:
                f.close()
            except OSError:
                pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover
            pass

    def __enter__(self) -> "RegexWorker":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["RegexWorker", "WORKER_SOURCE"]
