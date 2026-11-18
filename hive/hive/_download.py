"""``hive _download --spec PATH`` subprocess: file and pull modes, Range resume, hashes, progress.json/result.json
(design §11.3, WP3).

Eager-import rule (§10.2): imports only stdlib, hive.util, hive.errors, hive.netproxy, hive.netutil and hive.hfhub,
all at module top (never a WP1 module, never a function-level ``hive.*`` import). It runs under the shim with
cwd = its run dir, reads and deletes ``download.json`` (0600), rebuilds ``NetGuard``/``AuthSpec`` from it and fetches
every file with ``netutil.open_download`` (routing and C1–C4 on every hop). It writes ``progress.json`` (every 1 s)
and, on success, ``result.json`` in the run dir; the job log (stdout) gets a progress line every 5 s and, on
failure, the error as its last line (the JobManager shows it as the job's ``error``).

Exit codes: 0 done; 1 failed (network, HTTP, conflict); 2 invalid spec; 3 hash/size mismatch (part kept in file
mode); 4 not enough disk space; 5 server-chosen name refused by policy; 6 another pull holds the folder lock;
7 stopped (SIGTERM / CTRL_BREAK: current writes finished, manifest flushed).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

from . import errors, hfhub, netproxy, netutil, util  # noqa: F401  (eager imports, §10.2)
from .errors import HiveError

EXIT_OK, EXIT_FAIL, EXIT_SPEC, EXIT_HASH, EXIT_DISK, EXIT_NAME, EXIT_LOCK, EXIT_STOPPED = 0, 1, 2, 3, 4, 5, 6, 7
CHUNK = 1 << 20
PROGRESS_EVERY = 1.0
LOG_EVERY = 5.0
BACKOFF = (2, 4, 8, 16, 32)
GIB = 1 << 30
IS_WINDOWS = sys.platform == "win32"
CASEFOLD_DEFAULT = IS_WINDOWS or sys.platform == "darwin"
PART_SUFFIX = ".hive-part"
MANIFEST = ".hive-pull.json"


class Failure(Exception):
    """A download failure with its exit code (message = the job's error)."""

    def __init__(self, message: str, code: int = EXIT_FAIL):
        super().__init__(message)
        self.code = code


class Stopped(Exception):
    """The stop signal arrived."""


def _say(line: str) -> None:
    """One line into the job log (stdout)."""
    try:
        sys.stdout.write(util.escape_terminal(line)[:2000] + "\n")
        sys.stdout.flush()
    except (OSError, ValueError):
        pass


def _err_text(e: BaseException) -> str:
    if isinstance(e, HiveError):
        return e.message + (f" ({e.hint})" if e.hint else "")
    return str(e) or type(e).__name__


def _header(headers: Dict[str, str], name: str) -> Optional[str]:
    low = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == low:
            return str(v)
    return None


def _content_length(headers: Dict[str, str]) -> Optional[int]:
    v = _header(headers, "Content-Length")
    try:
        n = int(str(v).strip()) if v is not None else None
    except ValueError:
        return None
    return n if n is not None and n >= 0 else None


def _range_start(headers: Dict[str, str]) -> Optional[int]:
    m = re.match(r"^\s*bytes\s+(\d+)-(\d+)/(\d+|\*)\s*$", _header(headers, "Content-Range") or "")
    return int(m.group(1)) if m else None


def _retry_after(headers: Dict[str, str]) -> Optional[float]:
    v = _header(headers, "Retry-After")
    try:
        return max(0.0, min(60.0, float(str(v).strip()))) if v is not None else None
    except ValueError:
        return None


def _free_bytes(path: Path) -> Optional[int]:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return int(shutil.disk_usage(str(p)).free)
    except OSError:
        return None


def _open_part(path: Path, *, truncate: bool) -> int:
    flags = os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if truncate:
        flags |= os.O_TRUNC
    fd = os.open(str(path), flags, 0o600)
    if not truncate:
        os.lseek(fd, 0, os.SEEK_END)
    return fd


def _hash_file(path: Path, h: Any, stop: Optional[threading.Event] = None, limit: Optional[int] = None) -> int:
    """Feed a file (up to ``limit`` bytes) into a hash object; returns the bytes read."""
    n = 0
    with open(str(path), "rb") as f:
        while True:
            if stop is not None and stop.is_set():
                raise Stopped()
            want = CHUNK if limit is None else min(CHUNK, limit - n)
            if want <= 0:
                break
            b = f.read(want)
            if not b:
                break
            h.update(b)
            n += len(b)
    return n


def _fsync_close(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError:
        pass
    os.close(fd)


# ---------------------------------------------------------------------------------------------------------------
# progress

class Progress:
    """progress.json every 1 s (atomic) and a short stdout line every 5 s."""

    def __init__(self, total: int, *, files_total: Optional[int] = None):
        self.lock = threading.Lock()
        self.total = int(total)
        self.done = 0
        self.downloaded = 0
        self.files_total = files_total
        self.files_done = 0
        self.current: List[str] = []
        self.note = ""
        self._samples: List[Tuple[float, int]] = []
        self._stop = threading.Event()
        self._last_log = 0.0
        self._thread: Optional[threading.Thread] = None

    def add(self, n: int, downloaded: bool = True) -> None:
        with self.lock:
            self.done += n
            if downloaded:
                self.downloaded += n

    def snapshot(self) -> Dict[str, Any]:
        now = time.monotonic()
        with self.lock:
            self._samples.append((now, self.downloaded))
            self._samples = [s for s in self._samples if now - s[0] <= 10.0]
            t0, b0 = self._samples[0]
            rate = (self.downloaded - b0) / (now - t0) if now - t0 >= 0.5 else 0.0
            total = max(self.total, self.done)
            left = max(0, total - self.done)
            d: Dict[str, Any] = {"v": 1, "done": self.done, "total": total,
                                 "percent": round(100.0 * self.done / total, 1) if total else 100.0,
                                 "rate_bps": int(rate), "eta_s": int(left / rate) if rate > 0 else None}
            if self.files_total is not None:
                d.update(files_done=self.files_done, files_total=self.files_total, current=list(self.current[:4]))
                cur = self.current[0].rsplit("/", 1)[-1] if self.current else ""
                d["note"] = f"{self.files_done}/{self.files_total} files" + (f" · {cur}" if cur else "")
            else:
                d["note"] = self.note
            return d

    def write(self) -> Dict[str, Any]:
        d = self.snapshot()
        try:
            util.write_json_atomic("progress.json", d)
        except (OSError, HiveError):
            pass
        return d

    def _loop(self) -> None:
        while not self._stop.wait(PROGRESS_EVERY):
            d = self.write()
            now = time.monotonic()
            if now - self._last_log >= LOG_EVERY:
                self._last_log = now
                eta = f", eta {d['eta_s']}s" if d.get("eta_s") is not None else ""
                _say(f"progress {d['percent']}% {util.human_bytes(d['done'])}/{util.human_bytes(d['total'])} "
                     f"at {util.human_bytes(d['rate_bps'])}/s{eta}" + (f" · {d['note']}" if d.get("note") else ""))

    def start(self) -> "Progress":
        self._last_log = time.monotonic()
        self.write()
        self._thread = threading.Thread(target=self._loop, name="hive-progress", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(2)
        self.write()


# ---------------------------------------------------------------------------------------------------------------
# one transfer into a part file

def _fetch(url: str, part: Path, *, guard: Any, auth: Optional[netutil.AuthSpec], headers: Dict[str, str],
           resume: bool, max_size: Optional[int], hasher_factory: Callable[[], Any], progress: Progress,
           stop: threading.Event, on_headers: Optional[Callable[[int, int, Dict[str, str]], None]] = None
           ) -> Tuple[Any, int, Dict[str, str], str]:
    """Download ``url`` into ``part`` (Range resume from the part size when ``resume``); returns
    (hasher over the whole part, size, response headers, final URL). ``max_size``: abort at max_size + 1 bytes.
    ``on_headers(start, remaining_or_-1, headers)`` runs before the first byte is written (disk checks)."""
    start = 0
    if resume and part.exists() and not part.is_symlink():
        start = part.stat().st_size
        if max_size is not None and start > max_size:
            start = 0
    status, rh, final, chunks = netutil.open_download(url, guard=guard, headers=headers, auth=auth,
                                                      range_start=start, cancel=stop)
    try:
        if start > 0 and status == 416:
            total = re.match(r"^\s*bytes\s+\*/(\d+)\s*$", _header(rh, "Content-Range") or "")
            if total and int(total.group(1)) == start:      # the part is already complete
                h = hasher_factory()
                _hash_file(part, h, stop)
                progress.add(start, downloaded=False)
                return h, start, rh, final
            start = 0
            raise _Restart()
        if status == 206 and start > 0 and _range_start(rh) == start:
            h = hasher_factory()
            _hash_file(part, h, stop, limit=start)
            progress.add(start, downloaded=False)
            fd = _open_part(part, truncate=False)
            size = start
        elif status in (200, 206) and (status == 200 or _range_start(rh) in (0, None)):
            h = hasher_factory()
            fd = _open_part(part, truncate=True)
            size = 0
        else:
            raise _HttpStatus(status, rh)
        if on_headers is not None:
            cl = _content_length(rh)
            try:
                on_headers(size, cl if cl is not None else -1, rh)
            except BaseException:
                os.close(fd)
                raise
        try:
            for b in chunks:
                if stop.is_set():
                    raise Stopped()
                if max_size is not None and size + len(b) > max_size:
                    raise Failure(f"the server sent more than the listed {max_size} bytes", EXIT_HASH)
                os.write(fd, b)
                h.update(b)
                size += len(b)
                progress.add(len(b))
        finally:
            _fsync_close(fd)
        return h, size, rh, final
    finally:
        close = getattr(chunks, "close", None)
        if callable(close):
            close()


class _Restart(Exception):
    pass


class _HttpStatus(Exception):
    def __init__(self, status: int, headers: Dict[str, str]):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.headers = headers


def _transient(e: BaseException) -> bool:
    if isinstance(e, _HttpStatus):
        return e.status >= 500 or e.status in (408, 429)
    if isinstance(e, HiveError):
        return e.code in ("unavailable", "timeout") and e.details.get("reason") not in ("proxy_auth", "redirects")
    return isinstance(e, (OSError, _Restart))


def _sleep(seconds: float, stop: threading.Event) -> None:
    if stop.wait(max(0.0, seconds)):
        raise Stopped()


# ---------------------------------------------------------------------------------------------------------------
# file mode (net.download)

def _disposition_name(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    m = re.search(r"filename\*\s*=\s*([^']*)'[^']*'([^;]+)", value, re.IGNORECASE)
    if m:
        try:
            return unquote(m.group(2).strip().strip('"'), encoding=(m.group(1) or "utf-8").strip() or "utf-8",
                           errors="replace")
        except LookupError:
            return unquote(m.group(2).strip().strip('"'), errors="replace")
    m = re.search(r'filename\s*=\s*"([^"]*)"', value, re.IGNORECASE) or \
        re.search(r"filename\s*=\s*([^;]+)", value, re.IGNORECASE)
    return m.group(1).strip() if m else None


def choose_name(headers: Dict[str, str], url: str) -> str:
    """Server-chosen file name (§11.3): Content-Disposition (``filename*``, then ``filename``) or the last URL path
    segment; basename after splitting on ``/`` and ``\\``; ``util.safe_remote_name``; dotfiles and failures become
    ``download.bin``."""
    raw = _disposition_name(_header(headers, "Content-Disposition"))
    if not raw:
        try:
            raw = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
        except ValueError:
            raw = ""
    base = re.split(r"[/\\]", raw or "")[-1].strip()
    name = util.safe_remote_name(base) if base else None
    if not name or name.startswith(".") or "/" in name:
        return "download.bin"
    return name


def _name_allowed(directory: str, target: str, deny: List[str], casefold: bool) -> Optional[str]:
    """None when the server-chosen target may be written, else the reason."""
    if os.path.normcase(os.path.realpath(os.path.dirname(target))) != os.path.normcase(directory):
        return "name leaves the folder"
    if os.path.islink(target):
        return "target is a symbolic link"
    if util.match_deny(deny, util.path_key(target, casefold=casefold)):
        return "name denied by policy"
    return None


def _candidates(name: str) -> List[str]:
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    out = [name]
    for i in range(1, 1000):
        out.append(f"{stem}-{i}.{ext}" if ext else f"{stem}-{i}")
    return out


def _publish_exclusive(part: Path, directory: str, name: str, deny: List[str], casefold: bool) -> str:
    """Create the final file exclusively (O_CREAT|O_EXCL|O_NOFOLLOW; ``-1``, ``-2``… on a clash), then move the
    part onto the name this process created. Server-chosen names never overwrite."""
    for cand in _candidates(name):
        target = os.path.join(directory, cand)
        why = _name_allowed(directory, target, deny, casefold)
        if why is not None:
            if os.path.lexists(target):
                continue
            raise Failure(f"{why}: {cand}", EXIT_NAME)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(target, flags, 0o644)
        except FileExistsError:
            continue
        os.close(fd)
        util.replace_with_retry(part, target)
        return target
    raise Failure(f"no free file name for {name}", EXIT_FAIL)


def run_file(spec: Dict[str, Any], guard: Any, auth: Optional[netutil.AuthSpec], stop: threading.Event) -> int:
    """net.download: one URL into ``path`` (or a server-chosen name in ``dir``)."""
    url = str(spec["url"])
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    want_sha = (spec.get("sha256") or "").strip().lower() or None
    overwrite, resume = bool(spec.get("overwrite")), bool(spec.get("resume", True))
    deny = [str(x) for x in spec.get("deny") or ()]
    casefold = bool(spec.get("casefold", CASEFOLD_DEFAULT))
    path = spec.get("path")
    directory = spec.get("dir")
    if bool(path) == bool(directory):
        raise Failure("the spec needs exactly one of path and dir", EXIT_SPEC)
    if path:
        dest = Path(str(path))
        if spec.get("make_dirs", True):
            dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.parent.is_dir():
            raise Failure(f"folder {dest.parent} does not exist (make_dirs=false)")
        if dest.is_symlink() or (dest.exists() and not overwrite):
            raise Failure(f"{dest} already exists (use overwrite=true)")
        part = Path(str(dest) + PART_SUFFIX)
        if part.is_symlink():
            raise Failure(f"{part} is a symbolic link")
    else:
        directory = os.path.normcase(os.path.realpath(str(directory)))
        if not os.path.isdir(directory):
            raise Failure(f"folder {directory} does not exist")
        dest = None
        part = Path(directory) / (".hive-download-" + os.path.basename(os.getcwd())[-16:] + PART_SUFFIX)
        resume = resume and part.exists()
    progress = Progress(0)
    progress.note = url.rsplit("/", 1)[-1][:80]
    progress.start()
    try:
        attempt = 0
        while True:
            try:
                h, size, rh, final = _fetch_checked(url, part, guard=guard, auth=auth, headers=headers,
                                                     resume=resume, progress=progress, stop=stop)
                break
            except Stopped:
                raise
            except _HttpStatus as e:
                if attempt < 3 and _transient(e):
                    attempt += 1
                    _say(f"HTTP {e.status}; retrying in {BACKOFF[attempt - 1]} s")
                    _sleep(_retry_after(e.headers) or BACKOFF[attempt - 1], stop)
                    continue
                raise Failure(f"download failed: HTTP {e.status} from {urlsplit(url).hostname}")
            except Failure:
                raise
            except Exception as e:  # noqa: BLE001
                if attempt < 3 and _transient(e):
                    attempt += 1
                    resume = True
                    _say(f"{_err_text(e)}; retrying in {BACKOFF[attempt - 1]} s")
                    _sleep(BACKOFF[attempt - 1], stop)
                    continue
                raise Failure(f"download failed: {_err_text(e)}")
        digest = h.hexdigest()
        if want_sha and digest != want_sha:
            raise Failure(f"sha256 mismatch: expected {want_sha}, got {digest} (partial file kept: {part})", EXIT_HASH)
        if dest is not None:
            if dest.exists() and not overwrite:
                raise Failure(f"{dest} already exists (use overwrite=true)")
            util.replace_with_retry(part, dest)
            final_path = str(dest)
        else:
            final_path = _publish_exclusive(part, str(directory), choose_name(rh, url), deny, casefold)
        util.write_json_atomic("result.json", {"v": 1, "path": final_path, "size": size, "sha256": digest})
        _say(f"saved {final_path} ({util.human_bytes(size)}, sha256 {digest})")
        return EXIT_OK
    finally:
        progress.stop()


def _fetch_checked(url: str, part: Path, *, guard: Any, auth: Optional[netutil.AuthSpec], headers: Dict[str, str],
                   resume: bool, progress: Progress, stop: threading.Event) -> Tuple[Any, int, Dict[str, str], str]:
    """One attempt; the free-space check runs against Content-Length before the first byte is written."""
    with progress.lock:
        progress.done = 0
        progress.downloaded = 0

    def check(start: int, remaining: int, _rh: Dict[str, str]) -> None:
        if remaining < 0:
            return
        with progress.lock:
            progress.total = start + remaining
        free = _free_bytes(part.parent)
        if free is not None and free < remaining:
            raise Failure(f"not enough disk space: need {remaining} bytes, {free} free in {part.parent}", EXIT_DISK)

    try:
        return _fetch(url, part, guard=guard, auth=auth, headers=headers, resume=resume, max_size=None,
                      hasher_factory=hashlib.sha256, progress=progress, stop=stop, on_headers=check)
    except _Restart:
        if part.exists():
            part.unlink()
        return _fetch(url, part, guard=guard, auth=auth, headers=headers, resume=False, max_size=None,
                      hasher_factory=hashlib.sha256, progress=progress, stop=stop, on_headers=check)


# ---------------------------------------------------------------------------------------------------------------
# pull mode (model.pull)

def _git_sha1_factory(size: int) -> Callable[[], Any]:
    def make() -> Any:
        h = hashlib.sha1()  # noqa: S324 - git blob id, not a security hash (size is checked too)
        h.update(b"blob %d\0" % size)
        return h
    return make


def _expected_hash(f: Dict[str, Any]) -> Optional[str]:
    if f.get("sha256"):
        return "sha256:" + str(f["sha256"]).lower()
    if f.get("git_sha1"):
        return "sha1:" + str(f["git_sha1"]).lower()
    return None


def _hash_of(path: Path, f: Dict[str, Any], stop: threading.Event) -> Optional[str]:
    if f.get("sha256"):
        h = hashlib.sha256()
        _hash_file(path, h, stop)
        return "sha256:" + h.hexdigest()
    if f.get("git_sha1"):
        h = _git_sha1_factory(int(f["size"]))()
        _hash_file(path, h, stop)
        return "sha1:" + h.hexdigest()
    return None


class _Counted:
    """Progress proxy counting what one attempt reported (so a retry can take it back)."""

    def __init__(self, progress: Progress):
        self.p = progress
        self.done = 0
        self.downloaded = 0

    def add(self, n: int, downloaded: bool = True) -> None:
        self.done += n
        if downloaded:
            self.downloaded += n
        self.p.add(n, downloaded)


class Puller:
    """Pull mode: worker pool, per-file retries, manifest updates, stop handling."""

    def __init__(self, spec: Dict[str, Any], guard: Any, auth: Optional[netutil.AuthSpec], stop: threading.Event):
        self.spec = spec
        self.guard = guard
        self.auth = auth
        self.stop = stop
        self.target = Path(os.path.realpath(str(spec["target"])))
        self.files: List[Dict[str, Any]] = [dict(f) for f in spec.get("files") or []]
        self.verify = str(spec.get("verify") or "fast")
        self.overwrite = bool(spec.get("overwrite"))
        self.lock = threading.Lock()
        self.failures: List[Tuple[str, str, int]] = []
        self.skipped_unsafe: List[str] = list(spec.get("skipped_unsafe") or [])
        self.skipped_existing = 0
        self.downloaded_bytes = 0
        self.manifest_path = self.target / MANIFEST
        total = sum(int(f["size"]) for f in self.files)
        self.progress = Progress(total, files_total=len(self.files))
        self.manifest = self._load_manifest()

    # manifest
    def _load_manifest(self) -> Dict[str, Any]:
        try:
            m = util.read_json(self.manifest_path, None) if self.manifest_path.is_file() else None
        except HiveError:
            m = None
        sp = self.spec
        fresh = {"source": sp.get("source"), "repo": sp.get("repo"), "repo_type": sp.get("repo_type", "model"),
                 "revision": sp.get("revision"), "commit": sp.get("commit"), "endpoint": sp.get("endpoint"),
                 "files": {}, "completed_at": None}
        if not isinstance(m, dict) or m.get("source") != sp.get("source") or m.get("repo") != sp.get("repo") \
                or not isinstance(m.get("files"), dict):
            return fresh
        m.update({k: fresh[k] for k in ("repo_type", "revision", "commit", "endpoint")})
        m["completed_at"] = None
        return m

    def _save_manifest(self) -> None:
        with self.lock:
            data = json.loads(json.dumps(self.manifest))
        util.write_json_atomic(self.manifest_path, data)

    def _record(self, f: Dict[str, Any], digest: Optional[str]) -> None:
        with self.lock:
            self.manifest["files"][f["path"]] = {"size": int(f["size"]), "hash": digest, "verified_at": util.iso_utc()}
        self._save_manifest()

    # per file
    def _dest(self, rel: str) -> Optional[Path]:
        dest = self.target.joinpath(*rel.split("/"))
        parent = dest.parent
        parent.mkdir(parents=True, exist_ok=True)
        real_parent = os.path.realpath(str(parent))
        tgt = str(self.target)
        if os.path.commonpath([os.path.normcase(real_parent), os.path.normcase(tgt)]) != os.path.normcase(tgt):
            return None
        if dest.is_symlink() or Path(str(dest) + PART_SUFFIX).is_symlink():
            return None
        return Path(real_parent) / dest.name

    def _existing_ok(self, f: Dict[str, Any], dest: Path) -> Optional[bool]:
        """True: complete and verified; False: must be (re)downloaded; None: conflict (not ours, differs)."""
        if not dest.exists():
            return False
        rec = self.manifest["files"].get(f["path"])
        ours = isinstance(rec, dict)
        size = dest.stat().st_size
        want = _expected_hash(f)
        if size != int(f["size"]):
            return False if (ours or self.overwrite) else None
        if self.verify == "fast" and ours and want is not None and rec.get("hash") == want \
                and int(rec.get("size", -1)) == size:
            return True
        got = _hash_of(dest, f, self.stop)
        if want is None or got == want:
            self._record(f, got)
            return True
        return False if (ours or self.overwrite) else None

    def _download_one(self, f: Dict[str, Any], dest: Path) -> None:
        url = hfhub.file_url(self.spec.get("source", "huggingface"), self.spec["endpoint"], self.spec["repo"],
                             self.spec.get("repo_type", "model"), self.spec["commit"], f["path"])
        size = int(f["size"])
        part = Path(str(dest) + PART_SUFFIX)
        want = _expected_hash(f)
        factory = (lambda: hashlib.sha256()) if f.get("sha256") else _git_sha1_factory(size)
        if size > GIB:
            free = _free_bytes(self.target)
            have = part.stat().st_size if part.exists() else 0
            if free is not None and free < size - have:
                raise Failure(f"not enough disk space for {f['path']}: need {size - have} bytes, {free} free",
                              EXIT_DISK)
        hash_retries = 0
        net_retries = 0
        resume = True
        with self.lock:
            self.progress.current.append(f["path"])
        try:
            while True:
                if self.stop.is_set():
                    raise Stopped()
                before = _Counted(self.progress)
                try:
                    h, got, _rh, _final = _fetch(url, part, guard=self.guard, auth=self.auth, headers={},
                                                 resume=resume, max_size=size, hasher_factory=factory,
                                                 progress=before, stop=self.stop)  # type: ignore[arg-type]
                except (Stopped, Failure) as e:
                    if isinstance(e, Failure) and e.code == EXIT_HASH:     # oversize body: delete, one retry
                        self._rewind(before)
                        if part.exists():
                            part.unlink()
                        if hash_retries < 1:
                            hash_retries += 1
                            resume = False
                            continue
                    raise
                except _HttpStatus as e:
                    self._rewind(before)
                    if _transient(e) and net_retries < len(BACKOFF):
                        _sleep(_retry_after(e.headers) or BACKOFF[net_retries], self.stop)
                        net_retries += 1
                        continue
                    hint = " (gated or private: check the token)" if e.status in (401, 403) else ""
                    raise Failure(f"{f['path']}: HTTP {e.status}{hint}")
                except _Restart:
                    self._rewind(before)
                    if part.exists():
                        part.unlink()
                    resume = False
                    continue
                except Exception as e:  # noqa: BLE001
                    self._rewind(before)
                    if _transient(e) and net_retries < len(BACKOFF):
                        _say(f"{f['path']}: {_err_text(e)}; retrying in {BACKOFF[net_retries]} s")
                        _sleep(BACKOFF[net_retries], self.stop)
                        net_retries += 1
                        resume = True
                        continue
                    raise Failure(f"{f['path']}: {_err_text(e)}")
                if got < size:                                    # cut short: resume
                    if net_retries < len(BACKOFF):
                        net_retries += 1
                        self._rewind(before)
                        resume = True
                        _sleep(min(2.0, BACKOFF[0]), self.stop)
                        continue
                    raise Failure(f"{f['path']}: the server sent {got} of {size} bytes")
                digest = ("sha256:" if f.get("sha256") else "sha1:") + h.hexdigest()
                if want is not None and digest != want:
                    self._rewind(before)
                    if part.exists():
                        part.unlink()
                    if hash_retries < 1:
                        hash_retries += 1
                        resume = False
                        _say(f"{f['path']}: hash mismatch; downloading it again")
                        continue
                    raise Failure(f"{f['path']}: hash mismatch (expected {want}, got {digest})", EXIT_HASH)
                util.replace_with_retry(part, dest)
                with self.lock:
                    self.downloaded_bytes += size
                self._record(f, digest if want is not None else digest)
                return
        finally:
            with self.lock:
                if f["path"] in self.progress.current:
                    self.progress.current.remove(f["path"])

    def _rewind(self, counted: "_Counted") -> None:
        """Take back the progress one failed attempt reported."""
        with self.progress.lock:
            self.progress.done -= counted.done
            self.progress.downloaded = max(0, self.progress.downloaded - counted.downloaded)
        counted.done = counted.downloaded = 0

    def _task(self, f: Dict[str, Any]) -> None:
        if self.stop.is_set() or self._fatal():
            return
        try:
            dest = self._dest(f["path"])
            if dest is None:
                with self.lock:
                    self.skipped_unsafe.append(f["path"])
                    self.progress.files_done += 1
                return
            ok = self._existing_ok(f, dest)
            if ok is None:
                raise Failure(f"{f['path']} exists with other content (use overwrite=true)")
            if ok:
                with self.lock:
                    self.skipped_existing += 1
                self.progress.add(int(f["size"]), downloaded=False)
            else:
                self._download_one(f, dest)
            with self.lock:
                self.progress.files_done += 1
        except Stopped:
            return
        except Failure as e:
            with self.lock:
                self.failures.append((f["path"], str(e), e.code))
            _say(f"failed: {e}")
        except (OSError, HiveError) as e:
            with self.lock:
                self.failures.append((f["path"], f"{f['path']}: {_err_text(e)}", EXIT_FAIL))
            _say(f"failed: {f['path']}: {_err_text(e)}")

    def _fatal(self) -> bool:
        with self.lock:
            return any(code == EXIT_DISK for _p, _m, code in self.failures)

    def run(self) -> int:
        """Download every planned file; write result.json on success."""
        self.target.mkdir(parents=True, exist_ok=True)
        stale = sorted(p for p in self.manifest["files"] if p not in {f["path"] for f in self.files})
        self._save_manifest()
        self.progress.start()
        try:
            order = sorted(self.files, key=lambda f: int(f["size"]))
            workers = max(1, min(8, int(self.spec.get("max_parallel") or 4)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hive-pull") as pool:
                for _ in pool.map(self._task, order):
                    pass
        finally:
            self.progress.stop()
        if self.stop.is_set():
            self._save_manifest()
            _say("stopped; run the same model.pull again to resume")
            return EXIT_STOPPED
        if self.failures:
            self._save_manifest()
            codes = sorted({c for _p, _m, c in self.failures})
            code = EXIT_DISK if EXIT_DISK in codes else EXIT_HASH if EXIT_HASH in codes else EXIT_FAIL
            names = ", ".join(p for p, _m, _c in self.failures[:10])
            more = f" (+{len(self.failures) - 10} more)" if len(self.failures) > 10 else ""
            raise Failure(f"{len(self.failures)} file(s) failed: {names}{more}; first error: {self.failures[0][1]}",
                          code)
        with self.lock:
            self.manifest["completed_at"] = util.iso_utc()
        self._save_manifest()
        res = {"v": 1, "path": str(self.target), "repo": self.spec.get("repo"), "commit": self.spec.get("commit"),
               "files": len(self.files), "bytes": sum(int(f["size"]) for f in self.files),
               "downloaded_bytes": self.downloaded_bytes, "skipped_existing": self.skipped_existing,
               "skipped_invalid": list(self.spec.get("skipped_invalid") or []),
               "skipped_pickle": list(self.spec.get("skipped_pickle") or []),
               "skipped_unsafe": sorted(set(self.skipped_unsafe)), "stale": stale,
               "manifest": str(self.manifest_path)}
        util.write_json_atomic("result.json", res)
        _say(f"done: {len(self.files)} files, {util.human_bytes(self.downloaded_bytes)} downloaded, "
             f"{self.skipped_existing} already complete")
        return EXIT_OK


def run_pull(spec: Dict[str, Any], guard: Any, auth: Optional[netutil.AuthSpec], stop: threading.Event) -> int:
    """model.pull: acquire the folder lock, then the Puller."""
    for k in ("source", "endpoint", "repo", "commit", "target", "files", "lock_file"):
        if k not in spec:
            raise Failure(f"invalid pull spec: missing {k}", EXIT_SPEC)
    target = Path(str(spec["target"]))
    target.mkdir(parents=True, exist_ok=True)
    lock = util.FileLock(str(spec["lock_file"]))
    if not lock.acquire(note=os.path.basename(os.getcwd())):
        raise Failure("another pull into this folder is running", EXIT_LOCK)
    try:
        return Puller(spec, guard, auth, stop).run()
    finally:
        lock.release()
        try:
            os.unlink(str(spec["lock_file"]))
        except OSError:
            pass


# ---------------------------------------------------------------------------------------------------------------
# entry point

def _install_signals(stop: threading.Event) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(_sig: int, _frm: Any) -> None:
        stop.set()
    for name in ("SIGTERM", "SIGINT", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


def run(spec: Dict[str, Any], stop: Optional[threading.Event] = None) -> int:
    """Run one spec (file or pull mode) in this process; returns the exit code."""
    stop = stop or threading.Event()
    try:
        if not isinstance(spec, dict) or spec.get("mode") not in ("file", "pull"):
            raise Failure("invalid download spec (mode)", EXIT_SPEC)
        guard = netutil.NetGuard.from_dict(spec.get("net") or {})
        a = spec.get("auth")
        auth = None
        if isinstance(a, dict) and a.get("value"):
            auth = netutil.AuthSpec(name=str(a.get("name") or "secret"), header=str(a.get("header") or "Authorization"),
                                    value=str(a["value"]), hosts=tuple(str(h) for h in a.get("hosts") or ()))
        if spec["mode"] == "file":
            return run_file(spec, guard, auth, stop)
        return run_pull(spec, guard, auth, stop)
    except Stopped:
        _say("stopped")
        return EXIT_STOPPED
    except Failure as e:
        _say(f"error: {e}")
        return e.code
    except HiveError as e:
        _say(f"error: {_err_text(e)}")
        return EXIT_FAIL
    except OSError as e:
        _say(f"error: {e}")
        return EXIT_FAIL


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point of ``hive _download --spec PATH``."""
    util.prune_cwd_path()
    if sys.stdout is None or sys.stderr is None:  # pythonw guard (§13.1)
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    ap = argparse.ArgumentParser(prog="hive _download", add_help=False)
    ap.add_argument("--spec", required=True)
    try:
        args = ap.parse_args(argv if argv is not None else sys.argv[1:])
    except SystemExit:
        return EXIT_SPEC
    spec_path = os.path.abspath(args.spec)
    try:
        os.chdir(os.path.dirname(spec_path))     # cwd = the run dir (progress.json / result.json are relative)
    except OSError as e:
        _say(f"error: cannot enter the run dir: {e}")
        return EXIT_SPEC
    try:
        with open(spec_path, "rb") as f:
            spec = json.loads(f.read().decode("utf-8"))
    except (OSError, ValueError) as e:
        _say(f"error: cannot read the download spec: {e}")
        return EXIT_SPEC
    finally:
        try:
            os.unlink(spec_path)
        except OSError:
            pass
    stop = threading.Event()
    _install_signals(stop)
    return run(spec, stop)


__all__ = ["main", "run", "run_file", "run_pull", "choose_name", "Puller", "Progress"]
