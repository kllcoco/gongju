"""Chunked copy engine, LocalFS/NodeFS endpoints, TransferManager (runs in the tool-host process)
(design §15.3, §8.5, D9, WP5).

``copy()`` moves one file in chunks (default 4 MiB) between two endpoints. A destination writes into
``<path>.hive-part`` and only a ``final`` chunk whose whole-file sha256 matches is moved into place (fs.write_chunk
semantics), so a copy is resumable without client state: before the first chunk the engine probes the part size
(``dst.stat(dp + ".hive-part")``) and resumes from it, obtaining the source sha256 up front (v6). Read offsets advance
by the requested length and a short or long chunk is a ``conflict`` (v7). A ``fs.read_chunk`` result flagged
``masked`` (the source contains a node secret value) stops the copy before anything altered is written (v7).
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .errors import HiveError, bad_request, conflict, denied, not_found, rate_limited
from .util import iso_utc, new_id

log = logging.getLogger("hive.transfer")

DEFAULT_CHUNK = 4 << 20
MAX_CHUNK = 8 << 20
MAX_RUNNING = 4
KEEP_S = 3600.0
KEEP_N = 50
PART_SUFFIX = ".hive-part"
SHA_RATE = 100 << 20            # NodeFS sha256 timeout: 120 s + size / (100 MiB/s)
MASKED_MSG = "the file contains a secret value of node {node} and cannot be copied through Hive; copy it on the " \
             "node itself"


class FsEndpoint(Protocol):
    """One side of a copy."""

    def stat(self, path: str) -> dict:
        """fs.stat-like dict."""
        ...

    def read_chunk(self, path: str, offset: int, length: int) -> Tuple[bytes, bool]:
        """(data, eof)."""
        ...

    def write_chunk(self, path: str, offset: int, data: bytes, *, final: bool, sha256: Optional[str] = None,
                    total_size: Optional[int] = None, overwrite: bool = False) -> dict:
        """fs.write_chunk-like."""
        ...


# ---------------------------------------------------------------------------------------------------------------
# LocalFS

class LocalFS:
    """Local files: ``roots=None`` unconfined (CLI push/pull, user code); ``roots=(…)`` confined to those folders
    (client-side Toolkit ``local:``); ``roots=()`` refuses everything (server-hosted Toolkits)."""

    label = "local"

    def __init__(self, roots: Optional[Sequence[str]]):
        if roots is None:
            self.roots: Optional[List[str]] = None
        else:
            self.roots = [os.path.realpath(os.path.expanduser(str(r))) for r in roots]

    def _resolve(self, path: str) -> str:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise bad_request("invalid local path")
        p = os.path.expanduser(path)
        if self.roots is not None and not os.path.isabs(p) and self.roots:
            p = os.path.join(self.roots[0], p)
        p = os.path.abspath(p)
        parent, name = os.path.split(p)
        real = os.path.join(os.path.realpath(parent), name) if name else os.path.realpath(p)
        if self.roots is None:
            return real
        if not self.roots:
            raise denied("local: paths are not available here", rule="local_roots",
                         hint="this tool runs on a server; copy between nodes instead")
        full = os.path.realpath(real)
        for r in self.roots:
            try:
                if os.path.commonpath([os.path.normcase(full), os.path.normcase(r)]) == os.path.normcase(r):
                    return real
            except ValueError:
                continue
        raise denied(f"local path {path!r} is outside the local roots", rule="local_roots",
                     hint="allowed local folders: " + ", ".join(self.roots) +
                          " (client.json local_files.roots)")

    def stat(self, path: str, *, sha256: bool = False) -> dict:
        """{"path","exists","type","size","mtime"[,"sha256"]}."""
        p = self._resolve(path)
        try:
            st = os.stat(p)
        except FileNotFoundError:
            return {"path": p, "exists": False}
        except OSError as e:
            raise HiveError("internal", f"cannot stat {p}: {e.strerror or e}")
        import stat as _st
        typ = "file" if _st.S_ISREG(st.st_mode) else "dir" if _st.S_ISDIR(st.st_mode) else "other"
        out = {"path": p, "exists": True, "type": typ, "size": st.st_size, "mtime": iso_utc(st.st_mtime)}
        if sha256 and typ == "file":
            out["sha256"] = self.sha256(path)
        return out

    def sha256(self, path: str, size: Optional[int] = None) -> str:
        """Hex sha256 of a local file."""
        p = self._resolve(path)
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    def read_chunk(self, path: str, offset: int, length: int) -> Tuple[bytes, bool]:
        """(data, eof)."""
        p = self._resolve(path)
        try:
            with open(p, "rb") as f:
                size = os.fstat(f.fileno()).st_size
                f.seek(offset)
                data = f.read(length)
        except FileNotFoundError:
            raise not_found(f"{p} does not exist")
        except OSError as e:
            raise HiveError("internal", f"cannot read {p}: {e.strerror or e}")
        return data, offset + len(data) >= size

    def write_chunk(self, path: str, offset: int, data: bytes, *, final: bool, sha256: Optional[str] = None,
                    total_size: Optional[int] = None, overwrite: bool = False) -> dict:
        """Write into ``<path>.hive-part``; ``final`` verifies the sha256 and moves it into place."""
        p = self._resolve(path)
        part = p + PART_SUFFIX
        if offset == 0:
            if os.path.exists(p) and not overwrite:
                raise conflict(f"{p} exists", hint="pass overwrite=true to replace it")
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            os.close(fd)
        try:
            cur = os.path.getsize(part)
        except OSError:
            cur = -1
        if cur != offset:
            raise conflict(f"offset {offset} does not match the part size {max(cur, 0)}", part_size=max(cur, 0))
        with open(part, "r+b") as f:
            f.seek(offset)
            f.write(data)
            f.flush()
            if final:
                os.fsync(f.fileno())
        size = offset + len(data)
        out: Dict[str, Any] = {"path": p, "part_bytes": size, "done": False}
        if final:
            h = hashlib.sha256()
            with open(part, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            digest = h.hexdigest()
            if sha256 and digest != str(sha256).split(":", 1)[-1].lower():
                try:
                    os.unlink(part)
                except OSError:
                    pass
                raise conflict("sha256 mismatch: source changed; run again", hint="source changed; run again")
            from .util import replace_with_retry
            replace_with_retry(Path(part), Path(p))
            out.update(done=True, sha256=digest)
        return out


# ---------------------------------------------------------------------------------------------------------------
# NodeFS

class NodeFS:
    """``fs.stat`` / ``fs.read_chunk`` / ``fs.write_chunk`` on one node through a Target (``node=None``: the direct
    node / the client's default)."""

    def __init__(self, target: Any, node: Optional[str] = None):
        if target is None:
            raise bad_request("NodeFS needs a target")
        self.target = target
        self.node = node if node not in ("", None) else None
        self.label = self.node or "node"

    def _call(self, action: str, params: dict, timeout: Optional[float] = None) -> dict:
        return self.target.call(action, params, node=self.node, timeout=timeout)

    def stat(self, path: str, *, sha256: bool = False, timeout: Optional[float] = None) -> dict:
        """fs.stat."""
        p: Dict[str, Any] = {"path": path}
        if sha256:
            p["sha256"] = True
        return self._call("fs.stat", p, timeout=timeout)

    def sha256(self, path: str, size: Optional[int] = None) -> str:
        """fs.stat(sha256=true) with ``timeout = 120 + size / (100 MiB/s)``."""
        t = 120.0 + float(size or 0) / SHA_RATE
        r = self.stat(path, sha256=True, timeout=t)
        h = r.get("sha256")
        if not isinstance(h, str) or not h:
            raise HiveError("internal", f"no sha256 for {path} on {self.label}")
        return h.split(":", 1)[-1].lower()

    def read_chunk(self, path: str, offset: int, length: int) -> Tuple[bytes, bool]:
        """fs.read_chunk; a ``masked`` result raises conflict before any byte is returned (v7)."""
        r = self._call("fs.read_chunk", {"path": path, "offset": int(offset), "length": int(length)})
        if r.get("masked"):
            raise conflict(MASKED_MSG.format(node=self.label), masked=True)
        try:
            data = base64.b64decode(r.get("data") or "", validate=True)
        except (ValueError, TypeError):
            raise HiveError("internal", f"invalid chunk data from {self.label}")
        return data, bool(r.get("eof"))

    def write_chunk(self, path: str, offset: int, data: bytes, *, final: bool, sha256: Optional[str] = None,
                    total_size: Optional[int] = None, overwrite: bool = False) -> dict:
        """fs.write_chunk."""
        p: Dict[str, Any] = {"path": path, "offset": int(offset), "data": base64.b64encode(data).decode("ascii"),
                             "final": bool(final), "overwrite": bool(overwrite)}
        if sha256:
            p["sha256"] = sha256
        if total_size is not None and offset == 0:
            p["total_size"] = int(total_size)
        return self._call("fs.write_chunk", p)


# ---------------------------------------------------------------------------------------------------------------
# copy engine

def _src_sha(ep: Any, path: str, size: int) -> str:
    fn = getattr(ep, "sha256", None)
    if callable(fn):
        return fn(path, size)
    r = ep.stat(path)
    h = r.get("sha256")
    if not h:
        raise HiveError("internal", "source sha256 not available")
    return str(h).split(":", 1)[-1].lower()


def _part_size(dst: Any, dp: str) -> int:
    try:
        st = dst.stat(dp + PART_SUFFIX)
    except HiveError as e:
        if e.code in ("not_found", "policy_denied", "forbidden", "bad_request"):
            return 0
        raise
    if not st.get("exists") or st.get("type") not in (None, "file"):
        return 0
    try:
        return int(st.get("size") or 0)
    except (TypeError, ValueError):
        return 0


def copy(src: FsEndpoint, sp: str, dst: FsEndpoint, dp: str, *, chunk: int = DEFAULT_CHUNK, overwrite: bool = False,
         progress: Optional[Callable[[int, int], None]] = None, cancel: Optional[threading.Event] = None) -> dict:
    """Copy one file; ``{"bytes","sha256","duration_s","resumed_from"}`` (§15.3)."""
    t0 = time.monotonic()
    chunk = max(1, min(MAX_CHUNK, int(chunk)))
    st = src.stat(sp)
    if not st.get("exists"):
        raise not_found(f"source {sp} does not exist")
    if st.get("type") not in (None, "file"):
        raise bad_request(f"source {sp} is not a regular file", hint="copy files one by one (folders: fs_op copy "
                                                                      "on the same node)")
    size = int(st.get("size") or 0)
    dst_st = dst.stat(dp)
    if dst_st.get("exists"):
        if dst_st.get("type") not in (None, "file"):
            raise conflict(f"destination {dp} is a folder", hint="give the full destination file path")
        if not overwrite:
            raise conflict(f"destination {dp} exists", hint="pass overwrite=true to replace it")
    part = _part_size(dst, dp)
    start = part if 0 < part <= size else 0
    resumed_from = start
    hasher: Optional[Any] = None
    src_sha: Optional[str] = None
    if start > 0:
        src_sha = _src_sha(src, sp, size)
    else:
        hasher = hashlib.sha256()
    offset = start
    retried = False
    result: Dict[str, Any] = {}
    while True:
        if cancel is not None and cancel.is_set():
            raise HiveError("cancelled", "transfer cancelled", hint="run it again to resume")
        length = min(chunk, size - offset)
        if length > 0:
            data, _eof = src.read_chunk(sp, offset, length)
            if len(data) != length:
                raise conflict("source changed size while copying; run again", hint="source changed; run again")
        else:
            data = b""
        final = offset + length >= size
        if hasher is not None:
            hasher.update(data)
        sha = None
        if final:
            sha = src_sha if src_sha is not None else hasher.hexdigest()  # type: ignore[union-attr]
        try:
            result = dst.write_chunk(dp, offset, data, final=final, sha256=sha,
                                     total_size=size if offset == 0 else None, overwrite=overwrite)
        except HiveError as e:
            ps = (e.details or {}).get("part_size")
            if e.code == "conflict" and isinstance(ps, int) and not retried and 0 <= ps <= size and ps != offset:
                retried = True                     # another writer moved the part: resume from it once
                if ps == 0:
                    offset, hasher, src_sha = 0, hashlib.sha256(), None
                else:
                    src_sha = src_sha or _src_sha(src, sp, size)
                    offset, hasher = ps, None
                continue
            raise
        offset += length
        if progress is not None:
            try:
                progress(offset, size)
            except Exception:  # noqa: BLE001
                pass
        if final:
            break
    digest = (result.get("sha256") if isinstance(result, dict) else None) or \
        (src_sha if src_sha is not None else hasher.hexdigest() if hasher is not None else None)
    return {"bytes": size, "sha256": str(digest).split(":", 1)[-1] if digest else None,
            "duration_s": round(time.monotonic() - t0, 3), "resumed_from": resumed_from}


# ---------------------------------------------------------------------------------------------------------------
# TransferManager

class _Rec:
    def __init__(self, tid: str, src: str, dst: str):
        self.id = tid
        self.src = src
        self.dst = dst
        self.state = "running"
        self.done = 0
        self.total: Optional[int] = None
        self.sha: Optional[str] = None
        self.error: Optional[str] = None
        self.started = time.monotonic()
        self.ended: Optional[float] = None
        self.cancel = threading.Event()
        self.finished = threading.Event()

    def view(self) -> dict:
        el = (self.ended or time.monotonic()) - self.started
        rate = self.done / el if el > 0 and self.done else None
        eta = None
        if self.state == "running" and rate and self.total is not None:
            eta = round(max(0.0, (self.total - self.done) / rate), 1)
        return {"transfer_id": self.id, "state": self.state, "src": self.src, "dst": self.dst,
                "bytes_done": self.done, "bytes_total": self.total, "rate_bps": round(rate, 1) if rate else None,
                "eta_s": eta, "sha256": self.sha, "error": self.error}


class TransferManager:
    """Background transfers in the tool-host process (max 4 running; records kept 1 h / 50 entries)."""

    def __init__(self, client: Any, *, local_roots: Optional[Sequence[str]] = None):
        self.client = client
        self.local_roots = local_roots
        self._lock = threading.Lock()
        self._recs: Dict[str, _Rec] = {}

    def _endpoint(self, spec: str) -> Tuple[Any, str]:
        from .client import split_location
        where, path = split_location(spec)
        if where == "local":
            return LocalFS(self.local_roots), path
        target = getattr(self.client, "target", self.client)
        node = None if where == "self" else where
        return NodeFS(target, node), path

    def _prune(self) -> None:
        now = time.monotonic()
        done = [r for r in self._recs.values() if r.state != "running"]
        for r in done:
            if r.ended is not None and now - r.ended > KEEP_S:
                self._recs.pop(r.id, None)
        while len(self._recs) > KEEP_N:
            olds = sorted((r for r in self._recs.values() if r.state != "running"), key=lambda r: r.ended or 0)
            if not olds:
                break
            self._recs.pop(olds[0].id, None)

    def start(self, src: str, dst: str, **kw: Any) -> str:
        """Start a copy in a background thread; returns ``tx_…``."""
        overwrite = bool(kw.pop("overwrite", False))
        chunk = int(kw.pop("chunk", DEFAULT_CHUNK))
        if kw:
            raise bad_request(f"unknown transfer option(s): {', '.join(sorted(kw))}")
        s_ep, s_path = self._endpoint(src)
        d_ep, d_path = self._endpoint(dst)
        with self._lock:
            self._prune()
            running = sum(1 for r in self._recs.values() if r.state == "running")
            if running >= MAX_RUNNING:
                raise rate_limited(f"{running} transfers are running (max {MAX_RUNNING})", retry_after=10,
                                   reason="inflight", hint="wait for one to finish (fs_transfer transfer_id=…)")
            rec = _Rec("tx_" + new_id(), src, dst)
            self._recs[rec.id] = rec

        def prog(done: int, total: int) -> None:
            rec.done, rec.total = done, total

        def run() -> None:
            try:
                st = s_ep.stat(s_path)
                if st.get("exists") and isinstance(st.get("size"), int):
                    rec.total = st["size"]
                r = copy(s_ep, s_path, d_ep, d_path, chunk=chunk, overwrite=overwrite, progress=prog,
                         cancel=rec.cancel)
                rec.sha = r.get("sha256")
                rec.done = r.get("bytes") or rec.done
                rec.total = r.get("bytes") if rec.total is None else rec.total
                rec.state = "done"
            except HiveError as e:
                rec.state = "cancelled" if (e.code == "cancelled" or rec.cancel.is_set()) else "failed"
                rec.error = e.message + (f" ({e.hint})" if e.hint and e.code != "cancelled" else "")
            except Exception as e:  # noqa: BLE001
                log.error("transfer %s failed", rec.id, exc_info=True)
                rec.state = "failed"
                rec.error = f"{type(e).__name__}: {e}"[:500]
            finally:
                rec.ended = time.monotonic()
                rec.finished.set()
        threading.Thread(target=run, name=f"transfer-{rec.id}", daemon=True).start()
        return rec.id

    def _get(self, transfer_id: str) -> _Rec:
        with self._lock:
            rec = self._recs.get(transfer_id)
        if rec is None:
            raise not_found(f"transfer {transfer_id!r} not found", hint="transfers are kept for 1 hour")
        return rec

    def status(self, transfer_id: str, wait: float = 0) -> dict:
        """Transfer state (waits up to ``wait`` seconds for it to finish)."""
        rec = self._get(transfer_id)
        if wait and wait > 0 and rec.state == "running":
            rec.finished.wait(float(wait))
        return rec.view()

    def cancel(self, transfer_id: str) -> bool:
        """Cancel a running transfer (its part file stays for a later resume)."""
        rec = self._get(transfer_id)
        if rec.state != "running":
            return False
        rec.cancel.set()
        return True


__all__ = ["FsEndpoint", "LocalFS", "NodeFS", "copy", "TransferManager", "PART_SUFFIX"]
