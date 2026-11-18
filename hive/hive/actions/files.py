"""Action handlers: fs.list, fs.stat, fs.read, fs.read_chunk, fs.write, fs.write_chunk, fs.edit, fs.search,
fs.mkdir, fs.move, fs.copy, fs.delete (design §9.7, WP3).

Every path goes through ``ctx.policy.resolve_path`` (PathGuard, §5.4) before any file is opened; handlers open the
returned real path. Handlers register with ``@handler(name)`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

import base64
import binascii
import errno
import fnmatch
import hashlib
import os
import re
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .. import fsops, util
from ..errors import HiveError, bad_request, conflict, denied, not_found, timeout, too_large, unsupported
from ..fsops import os_errors
from . import ActionContext, handler

PART_SUFFIX = ".hive-part"
PLACEHOLDER_RE = re.compile(r"«secret:([A-Za-z_][A-Za-z0-9_]{0,127})»")
SEARCH_SKIP = (".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "site-packages")
LIST_CAP = 100_000
COPY_CAP = 100_000
REGEX_LINE_CAP = 2048
TEXT_CAP = 500
DEADLINE_MARGIN = 1.0
O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
O_BINARY = getattr(os, "O_BINARY", 0)
NEW_FILE_MODE = 0o644
WINERROR_PRIVILEGE_NOT_HELD = 1314     # os.symlink without SeCreateSymbolicLinkPrivilege / Developer Mode


# ---------------------------------------------------------------------------------------------------------------
# helpers

def _deadline(ctx: ActionContext) -> float:
    return time.monotonic() + max(0.5, ctx.remaining() - DEADLINE_MARGIN)


def _lp(p: Any) -> str:
    """Native path string for an OS call; Windows long paths get the ``\\\\?\\`` prefix (§5.4 step 10)."""
    return fsops.long_path(Path(str(p)))


def _edge_masker(ctx: ActionContext) -> Optional[fsops.EdgeMask]:
    """``mask(kept, rest)`` hiding a secret value cut by the end of ``kept`` (``SecretStore.mask_edges``, §5.13 v8);
    None when the node knows no secret values."""
    sec = getattr(ctx.rt, "secrets", None)
    if sec is None or not getattr(sec, "has_values", False) or not hasattr(sec, "mask_edges"):
        return None
    n = max(0, int(getattr(sec, "max_value_bytes", 0) or 0) - 1)
    if n <= 0:
        return None

    def mask(kept: str, rest: str) -> Tuple[str, bool]:
        data = kept.encode("utf-8", "replace")
        after = rest[:n].encode("utf-8", "replace")[:n]
        try:
            new, masked = sec.mask_edges(data, b"", after)
        except Exception:  # noqa: BLE001 - the pipeline still masks whole values (step 10a)
            return kept, False
        return (new.decode("utf-8", "replace"), True) if masked else (kept, False)
    return mask


def _iso(ts: Optional[float]) -> Optional[str]:
    return util.iso_utc(ts) if ts is not None else None


def _hash_file(ctx: Optional[ActionContext], path: Path) -> str:
    h = hashlib.sha256()
    with fsops.open_regular(path) as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            if ctx is not None:
                ctx.check()
    return h.hexdigest()


def _norm_version(v: str) -> str:
    v = str(v).strip().lower()
    return v[7:] if v.startswith("sha256:") else v


def _check_placeholders(ctx: ActionContext, data: Any) -> None:
    """v8 masked round trip: content holding «secret:NAME» for a NAME this node's SecretStore knows -> conflict."""
    text = data.decode("utf-8", "ignore") if isinstance(data, (bytes, bytearray)) else str(data)
    names = set(PLACEHOLDER_RE.findall(text))
    if not names:
        return
    try:
        known = {str(n.get("name")) for n in ctx.rt.secrets.names()}
    except Exception:  # noqa: BLE001 - no secret store: nothing can be a placeholder of it
        known = set()
    hits = sorted(names & known)
    if hits:
        raise conflict("content contains masked secret placeholders; change only the lines you need with fs_edit",
                       hint="content contains masked secret placeholders; change only the lines you need with "
                            "fs_edit", secrets=hits)


def _b64(data: str, what: str = "data") -> bytes:
    try:
        return base64.b64decode(data.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise bad_request(f"{what} is not valid base64")


def _link_at(ctx: ActionContext, path: str, *, write: bool = False) -> Optional[Path]:
    """The path itself when its last component is a symbolic link, else None (note 78). The link's own location
    is vetted by PathGuard without following it (``nofollow_last``: roots, fs.deny, deny_write and the hard denies
    apply to the link path) and its target must pass PathGuard as well; either refusal is raised, never a silent
    fall back to the target. For a path that is not a link the location check equals the caller's normal check."""
    s = path.rstrip("/\\") if util.IS_WINDOWS else path.rstrip("/")
    base = os.path.basename(s)
    if not base or base in (".", "..", "~") or base.startswith("~") and s == base:
        return None
    lex = ctx.policy.resolve_path(path, write=write, nofollow_last=True)
    if not os.path.islink(_lp(lex)):
        return None
    ctx.policy.resolve_path(path, write=write)          # the target must pass PathGuard too
    return lex


def _parts(ctx: ActionContext) -> Any:
    tr = getattr(ctx.rt, "parts", None)
    return tr if tr is not None and hasattr(tr, "touch") else None


def _parts_call(ctx: ActionContext, method: str, part: Path) -> None:
    tr = _parts(ctx)
    if tr is None:
        return
    try:
        getattr(tr, method)(part)
    except Exception:  # noqa: BLE001 - bookkeeping only
        pass


def _pkey(ctx: ActionContext, p: Any, *, follow: bool = True) -> str:
    """PathGuard's comparison key (casefolded where PathGuard casefolds: Windows, macOS, WSL drive paths)."""
    fn = getattr(ctx.policy, "path_key", None)
    if fn is not None:
        return str(fn(p, follow=follow))
    s = os.path.abspath(str(p))
    return util.path_key(os.path.normcase(os.path.realpath(s) if follow else os.path.normpath(s)), casefold=False)


def _key_under(key: str, base: str) -> bool:
    return key == base or key.startswith(base.rstrip("/") + "/")


def _protected(ctx: ActionContext) -> List[Tuple[str, str]]:
    """(normalized key, label) of locations no delete/move may target (§9.7 fs.delete): each in its lexical form,
    with the parent resolved, and fully resolved (a symbolic link to a root is protected like the root)."""
    out: List[Tuple[str, str]] = []

    def add(p: Any, label: str) -> None:
        if not p:
            return
        try:
            a = os.path.abspath(os.path.expanduser(str(p)))
            forms = (_pkey(ctx, a, follow=False),
                     _pkey(ctx, os.path.join(os.path.realpath(os.path.dirname(a)), os.path.basename(a)),
                           follow=False),
                     _pkey(ctx, a, follow=True))
        except (OSError, ValueError):
            return
        for k in dict.fromkeys(forms):
            out.append((k, label))

    add(os.path.expanduser("~"), "the home folder")
    add(getattr(ctx.policy, "workdir", None), "the node workdir")
    add(getattr(ctx.rt, "data_dir", None), "the node data folder")
    try:
        for r in ctx.policy.roots():
            if r.get("path") not in (None, "*"):
                add(r["path"], "an fs root")
    except Exception:  # noqa: BLE001
        pass
    return out


def _refuse_protected(ctx: ActionContext, p: Path, what: str, *, follow: bool = True) -> None:
    """protected_path refusal; ``follow=False`` for a symbolic link acted on itself (its own path is compared)."""
    a = os.path.abspath(str(p))
    real = os.path.realpath(a) if follow else a
    if os.path.dirname(real) == real:
        raise denied(f"cannot {what} a filesystem or drive root", rule="protected_path",
                     hint="pick a folder below the root")
    key = _pkey(ctx, a, follow=follow)
    for k, label in _protected(ctx):
        if _key_under(k, key):
            raise denied(f"cannot {what} '{p}': it is (or contains) {label}", rule="protected_path",
                         hint="pick a subfolder; roots, home, workdir and Hive's data folder are protected")


def _inside(ctx: ActionContext, child: Path, parent: Path) -> bool:
    return _key_under(_pkey(ctx, child), _pkey(ctx, parent))


def _exists_conflict(path: Path) -> HiveError:
    return conflict(f"'{path}' already exists", hint="use mode=overwrite to replace it")


def _publish_exclusive(tmp: str, path: Path, data: bytes, bits: int) -> None:
    """Put the finished temp file at ``path`` only if nothing is there (mode=create): a hard link fails with
    FileExistsError when the name exists (also a dangling link); filesystems without hard links fall back to an
    O_CREAT|O_EXCL|O_NOFOLLOW write of the bytes."""
    try:
        os.link(tmp, _lp(path))
        return
    except FileExistsError:
        raise _exists_conflict(path)
    except (OSError, NotImplementedError, AttributeError):
        pass
    try:
        fd = os.open(_lp(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_NOFOLLOW | O_BINARY, bits)
    except FileExistsError:
        raise _exists_conflict(path)
    try:
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes, *, mode_bits: Optional[int] = None, exclusive: bool = False) -> None:
    """Temp file in the same folder + ``replace_with_retry``; keeps the original mode bits (or ``mode_bits``).
    ``exclusive`` (fs.write mode=create): never replaces an existing file (conflict)."""
    keep: Optional[int] = None
    try:
        keep = stat.S_IMODE(os.stat(_lp(path)).st_mode)
    except FileNotFoundError:
        keep = None
    fd, tmp = tempfile.mkstemp(prefix="." + path.name[:60] + ".", suffix=".hive-tmp", dir=_lp(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        bits = mode_bits if mode_bits is not None else keep if keep is not None else NEW_FILE_MODE
        if not util.IS_WINDOWS:
            os.chmod(tmp, bits)
        if exclusive:
            _publish_exclusive(tmp, path, data, bits)
            os.unlink(tmp)
        else:
            util.replace_with_retry(tmp, _lp(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _parse_file_mode(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    return int(v, 8)


def _make_parent(path: Path, make_dirs: bool) -> None:
    parent = path.parent
    if os.path.isdir(_lp(parent)):
        return
    if not make_dirs:
        raise not_found(f"folder '{parent}' does not exist", hint="set make_dirs=true or create it with fs_mkdir")
    os.makedirs(_lp(parent), exist_ok=True)


# ---------------------------------------------------------------------------------------------------------------
# fs.list / fs.stat

def _entry_row(e: os.DirEntry, root: Path) -> Dict[str, Any]:
    rel = os.path.relpath(e.path, str(root)).replace(os.sep, "/")
    size: Optional[int] = None
    mtime: Optional[float] = None
    try:
        st = e.stat(follow_symlinks=False)
        mtime = st.st_mtime
    except OSError:
        st = None
    if e.is_symlink():
        kind = "link"
    elif e.is_dir(follow_symlinks=False):
        kind, rel = "dir", rel + "/"
    elif e.is_file(follow_symlinks=False):
        kind = "file"
        size = st.st_size if st is not None else None
    else:
        kind = "other"
    return {"path": rel, "type": kind, "size": size, "mtime": _iso(mtime), "_m": mtime or 0.0, "_s": size or -1}


@handler("fs.list")
def fs_list(ctx: ActionContext, p: dict) -> dict:
    """Directory listing (depth 1..5, glob filter, paging, sort)."""
    root = ctx.policy.resolve_path(p.get("path") or ".", must_exist=True)
    with os_errors(root):
        if not os.path.isdir(_lp(root)):
            raise bad_request(f"'{root}' is not a folder", hint="use fs_stat for a single file")
    pattern = p.get("pattern")
    deadline = _deadline(ctx)
    rows: List[Dict[str, Any]] = []
    capped = False
    walk_state: Dict[str, Any] = {}
    for e in fsops.walk(root, depth=int(p.get("depth", 1)), include_hidden=bool(p.get("include_hidden")),
                        guard=ctx.policy, deadline=deadline, state=walk_state):
        if pattern and not fnmatch.fnmatch(e.name, pattern):
            continue
        if len(rows) >= LIST_CAP:
            capped = True
            break
        rows.append(_entry_row(e, root))
    if walk_state.get("truncated") or time.monotonic() > deadline:
        capped = True
    sort = p.get("sort") or "name"
    if sort == "mtime":
        rows.sort(key=lambda r: (-r["_m"], r["path"]))
    elif sort == "size":
        rows.sort(key=lambda r: (-r["_s"], r["path"]))
    else:
        rows.sort(key=lambda r: r["path"])
    offset, limit = int(p.get("offset", 0)), int(p.get("limit", 200))
    page = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows[offset:offset + limit]]
    total = len(rows)
    return {"path": str(root), "entries": page, "total": total, "total_capped": capped,
            "next_offset": offset + limit if offset + limit < total else None}


@handler("fs.stat")
def fs_stat(ctx: ActionContext, p: dict) -> dict:
    """Metadata of a path; a missing path is ``exists:false``, not an error."""
    raw = p["path"]
    real = ctx.policy.resolve_path(raw)
    link = _link_at(ctx, raw)
    shown = link if link is not None else real
    out: Dict[str, Any] = {"path": str(shown)}
    with os_errors(shown):
        try:
            st = os.stat(_lp(real))
        except FileNotFoundError:
            if link is None:
                return {"path": str(real), "exists": False}
            st = os.lstat(_lp(link))
        out["exists"] = True
        if link is not None:
            out["type"] = "link"
            out["link_target"] = os.readlink(_lp(link))
        elif stat.S_ISDIR(st.st_mode):
            out["type"] = "dir"
        elif stat.S_ISREG(st.st_mode):
            out["type"] = "file"
        else:
            out["type"] = "other"
        out["size"] = int(st.st_size)
        out["mtime"] = util.iso_utc(st.st_mtime)
        out["ctime"] = util.iso_utc(st.st_ctime)
        out["mode"] = oct(stat.S_IMODE(st.st_mode))
        out["readable"] = os.access(_lp(real), os.R_OK)
        out["writable"] = os.access(_lp(real), os.W_OK)
        if p.get("sha256") and stat.S_ISREG(st.st_mode) and os.path.exists(_lp(real)):
            out["sha256"] = _hash_file(ctx, real)
    return out


# ---------------------------------------------------------------------------------------------------------------
# reading

@handler("fs.read")
def fs_read(ctx: ActionContext, p: dict) -> dict:
    """Text read with line paging."""
    lim = ctx.policy.limits
    max_bytes = int(p.get("max_bytes", 65536))
    if max_bytes > lim.max_read_bytes:
        raise bad_request(f"max_bytes {max_bytes} is above the node limit",
                          hint=f"max read size on this node is {lim.max_read_bytes} bytes")
    path = ctx.policy.resolve_path(p["path"], must_exist=True)
    r = fsops.read_lines(path, int(p.get("start_line", 1)), int(p.get("max_lines", 500)), max_bytes,
                         line_numbers=bool(p.get("line_numbers")), check=ctx.check, mask=_edge_masker(ctx))
    out: Dict[str, Any] = {"path": str(path)}
    out.update(r)
    ctx.audit_extra["bytes"] = r["size"]
    return out


@handler("fs.read_chunk")
def fs_read_chunk(ctx: ActionContext, p: dict) -> dict:
    """Binary range read (base64); secret values cut by the range edges are masked (``mask_edges``, §5.13)."""
    lim = ctx.policy.limits
    offset, length = int(p.get("offset", 0)), int(p.get("length", 4194304))
    if length > lim.max_read_bytes:
        raise bad_request(f"length {length} is above the node limit",
                          hint=f"max read size on this node is {lim.max_read_bytes} bytes")
    path = ctx.policy.resolve_path(p["path"], must_exist=True)
    with os_errors(path):
        if os.path.isdir(_lp(path)):
            raise bad_request(f"'{path}' is a folder")
        with fsops.open_regular(path) as f:      # never blocks on a FIFO/device (non-regular -> bad_request)
            st = os.fstat(f.fileno())
            size = st.st_size
            f.seek(offset)
            data = f.read(length)
            masked = False
            sec = getattr(ctx.rt, "secrets", None)
            if data and sec is not None and getattr(sec, "has_values", False):
                n = max(0, int(getattr(sec, "max_value_bytes", 0) or 0) - 1)
                before = after = b""
                if n:
                    lo = max(0, offset - n)
                    f.seek(lo)
                    before = f.read(offset - lo)
                    f.seek(offset + len(data))
                    after = f.read(n)
                try:
                    new, masked = sec.mask_edges(data, before, after)
                except Exception:  # noqa: BLE001 - the pipeline still masks whole values (step 10a)
                    new, masked = data, False
                raw_len = len(data)
                data = new
            else:
                raw_len = len(data)
    out: Dict[str, Any] = {"data": base64.b64encode(data).decode("ascii"), "offset": offset, "length": raw_len,
                           "size": size, "eof": offset + raw_len >= size, "mtime": util.iso_utc(st.st_mtime)}
    if masked:
        out["masked"] = True
    ctx.audit_extra["bytes"] = raw_len
    return out


# ---------------------------------------------------------------------------------------------------------------
# writing

@handler("fs.write")
def fs_write(ctx: ActionContext, p: dict) -> dict:
    """Write a whole file (overwrite/create via temp + replace, or append)."""
    lim = ctx.policy.limits
    enc = p.get("encoding") or "utf-8"
    content = p["content"]
    try:
        raw = _b64(content, "content") if enc == "base64" else content.encode("utf-8")
    except UnicodeEncodeError:
        raise bad_request("content is not valid Unicode text (lone surrogate)", hint="use encoding=base64 for bytes")
    if len(raw) > lim.max_write_bytes:
        raise too_large(f"content is {len(raw)} bytes", hint=f"max write size on this node is {lim.max_write_bytes} "
                                                             "bytes; use fs_transfer for large files")
    _check_placeholders(ctx, raw)
    path = ctx.policy.resolve_path(p["path"], write=True)
    mode = p.get("mode") or "overwrite"
    file_mode = _parse_file_mode(p.get("file_mode"))
    with os_errors(path):
        exists = os.path.lexists(_lp(path))
        if exists and os.path.isdir(_lp(path)):
            raise bad_request(f"'{path}' is a folder")
        if mode == "create" and exists:
            raise _exists_conflict(path)
        if p.get("if_version"):
            if not exists:
                raise conflict(f"'{path}' does not exist (if_version was given)",
                               hint="file changed since you read it; re-read")
            if _hash_file(ctx, path) != _norm_version(p["if_version"]):
                raise conflict(f"'{path}' changed since it was read (version mismatch)",
                               hint="file changed since you read it; re-read")
        _make_parent(path, bool(p.get("make_dirs", True)))
        if mode == "append":
            fd = os.open(fsops.long_path(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT | O_NOFOLLOW | O_BINARY,
                         file_mode if file_mode is not None else NEW_FILE_MODE)
            try:
                os.write(fd, raw)
                os.fsync(fd)
            finally:
                os.close(fd)
            if file_mode is not None and not util.IS_WINDOWS:
                os.chmod(_lp(path), file_mode)
            version = _hash_file(ctx, path)
        else:
            _atomic_write(path, raw, mode_bits=file_mode, exclusive=mode == "create")
            version = hashlib.sha256(raw).hexdigest()
    ctx.audit_extra.update(bytes=len(raw), path=str(path))
    return {"path": str(path), "bytes": len(raw), "version": version, "created": not exists}


def _free_bytes(path: Path) -> int:
    p = path
    while not os.path.exists(_lp(p)) and p != p.parent:
        p = p.parent
    return shutil.disk_usage(_lp(p)).free


@handler("fs.write_chunk")
def fs_write_chunk(ctx: ActionContext, p: dict) -> dict:
    """Stateless resumable upload chunk into ``<path>.hive-part`` (offset must equal the part size)."""
    lim = ctx.policy.limits
    data = _b64(p["data"])
    if len(data) > lim.max_chunk_bytes:
        raise too_large(f"chunk is {len(data)} bytes", hint=f"max chunk size on this node is {lim.max_chunk_bytes} "
                                                            "bytes")
    _check_placeholders(ctx, data)
    path = ctx.policy.resolve_path(p["path"], write=True)
    part = ctx.policy.resolve_path(str(path) + PART_SUFFIX, write=True)
    offset = int(p["offset"])
    overwrite = bool(p.get("overwrite"))
    with os_errors(path):
        if os.path.isdir(_lp(path)):
            raise bad_request(f"'{path}' is a folder")
        if offset == 0:
            if os.path.lexists(_lp(path)) and not overwrite:
                raise conflict(f"'{path}' already exists", hint="set overwrite=true to replace it")
            _make_parent(path, bool(p.get("make_dirs", True)))
            total = p.get("total_size")
            if total is not None:
                free = _free_bytes(path.parent)
                need = int(int(total) * 1.01) + (64 << 20)
                if free < need:
                    raise too_large(f"not enough disk space for {util.human_bytes(int(total))}",
                                    hint="free space on the node or pick another folder", need=need, free=free,
                                    path=str(path.parent))
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | O_NOFOLLOW | O_BINARY
        else:
            try:
                cur = os.lstat(_lp(part)).st_size
            except FileNotFoundError:
                cur = 0
            if cur != offset:
                raise conflict(f"offset {offset} does not match the part size {cur}", part_size=cur,
                               hint=f"resume from offset {cur} (or start again at 0)")
            flags = os.O_WRONLY | O_NOFOLLOW | O_BINARY
        fd = os.open(_lp(part), flags, 0o600)
        try:
            os.lseek(fd, offset, os.SEEK_SET)
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            if p.get("final"):
                os.fsync(fd)
            part_bytes = os.fstat(fd).st_size
        finally:
            os.close(fd)
        if not util.IS_WINDOWS:
            os.chmod(_lp(part), 0o600)
        _parts_call(ctx, "touch", part)
        out: Dict[str, Any] = {"path": str(path), "part_bytes": part_bytes, "done": False}
        if p.get("final"):
            digest = _hash_file(ctx, part)
            want = p.get("sha256")
            if want and _norm_version(want) != digest:
                try:
                    os.unlink(_lp(part))
                finally:
                    _parts_call(ctx, "done", part)
                raise conflict("sha256 mismatch; the upload was discarded", expected=_norm_version(want),
                               actual=digest, hint="upload the file again from offset 0")
            if os.path.lexists(_lp(path)) and not overwrite:
                raise conflict(f"'{path}' already exists", hint="set overwrite=true to replace it")
            if not util.IS_WINDOWS:   # overwrite keeps the replaced file's mode bits (note 78), new files 0644
                bits = NEW_FILE_MODE
                try:
                    st_old = os.stat(_lp(path))
                    if stat.S_ISREG(st_old.st_mode):
                        bits = stat.S_IMODE(st_old.st_mode)
                except FileNotFoundError:
                    pass
                os.chmod(_lp(part), bits)
            util.replace_with_retry(_lp(part), _lp(path))
            _parts_call(ctx, "done", part)
            out.update(done=True, sha256=digest, path=str(path))
    ctx.audit_extra.update(bytes=len(data), path=str(path))
    return out


@handler("fs.edit")
def fs_edit(ctx: ActionContext, p: dict) -> dict:
    """Exact text replacement keeping encoding, BOM and line endings."""
    lim = ctx.policy.limits
    _check_placeholders(ctx, p["new_text"])
    path = ctx.policy.resolve_path(p["path"], write=True, must_exist=True)
    with os_errors(path):
        if os.path.isdir(_lp(path)):
            raise bad_request(f"'{path}' is a folder")
        size = os.path.getsize(_lp(path))
        if size > lim.max_write_bytes:
            raise too_large(f"file is {size} bytes", hint=f"fs_edit works on files up to {lim.max_write_bytes} bytes")
        with fsops.open_regular(path) as f:
            data = f.read(lim.max_write_bytes + 1)
        if len(data) > lim.max_write_bytes:
            raise too_large("file grew while it was read", hint=f"fs_edit works on files up to "
                                                                f"{lim.max_write_bytes} bytes")
        encoding, binary = fsops.detect_encoding(data[:fsops.HEAD_BYTES])
        if binary:
            raise unsupported(f"'{path}' looks like a binary file", hint="binary files cannot be edited as text")
        if p.get("if_version") and hashlib.sha256(data).hexdigest() != _norm_version(p["if_version"]):
            raise conflict(f"'{path}' changed since it was read (version mismatch)",
                           hint="file changed since you read it; re-read")
        text, codec, errors = fsops.decode_bytes(data, encoding)
        new_text, count, lines = fsops.apply_edit(text, p["old_text"], p["new_text"], bool(p.get("replace_all")))
        out_bytes = fsops.encode_text(new_text, encoding, codec, errors)
        if len(out_bytes) > lim.max_write_bytes:
            raise too_large(f"the edited file would be {len(out_bytes)} bytes",
                            hint=f"max write size on this node is {lim.max_write_bytes} bytes")
        _atomic_write(path, out_bytes)
    _old, new_norm = fsops.normalize_newlines(text, p["old_text"], p["new_text"])
    snip = fsops.snippet(new_text, lines[0] if lines else 1, len(fsops.split_lines(new_norm)) or 1)
    ctx.audit_extra.update(path=str(path), replacements=count)
    return {"path": str(path), "replacements": count, "version": hashlib.sha256(out_bytes).hexdigest(),
            "snippet": snip}


# ---------------------------------------------------------------------------------------------------------------
# search

def _cut(s: str, n: int = TEXT_CAP, mask: Optional[fsops.EdgeMask] = None) -> str:
    """Match text cut to ``n`` chars; a secret value cut by the edge is masked (step 10a masks whole values)."""
    s = fsops.display_text(s)
    if len(s) <= n:
        return s
    kept = s[:n]
    i = kept.rfind("«")
    if i >= 0 and s.startswith("«secret:", i) and "»" not in kept[i:]:
        j = s.find("»", n, n + 80)
        if j >= 0:     # a mask marker (fs.search matches on masked text) is never cut in half
            kept = s[:j + 1]
            n = len(kept)
    if mask is not None:
        kept = mask(kept, s[n:])[0]
    return kept + (f"…[+{len(s) - n} chars]" if len(s) > n else "")


def _search_files(ctx: ActionContext, root: Path, p: dict, deadline: float) -> Iterable[os.DirEntry]:
    glob = p.get("glob")
    for e in fsops.walk(root, depth=4096, include_hidden=bool(p.get("include_hidden")), guard=ctx.policy,
                        deadline=deadline, skip_dirs=SEARCH_SKIP):
        try:
            if e.is_symlink() or not e.is_file(follow_symlinks=False):
                continue
        except OSError:
            continue
        if glob:
            subject = os.path.relpath(e.path, str(root)).replace(os.sep, "/") if "/" in glob else e.name
            if not fnmatch.fnmatch(subject, glob):
                continue
        yield e


@handler("fs.search")
def fs_search(ctx: ActionContext, p: dict) -> dict:
    """Content (or file-name) search below a folder; partial results on timeout."""
    t0 = time.monotonic()
    pattern = p["pattern"]
    literal, icase = bool(p.get("literal")), bool(p.get("ignore_case"))
    names_only = bool(p.get("names_only"))
    rx = None
    if not names_only and not literal:
        fsops.check_regex(pattern)
        rx = re.compile(pattern, re.IGNORECASE if icase else 0)
    needle = pattern.lower() if icase else pattern
    root = ctx.policy.resolve_path(p.get("path") or ".", must_exist=True)
    deadline = _deadline(ctx)
    max_results = int(p.get("max_results", 100))
    ctx_n = int(p.get("context", 0))
    max_file = int(float(p.get("max_file_mb", 10)) * (1 << 20))
    matches: List[Dict[str, Any]] = []
    scanned = matched_files = 0
    truncated, reason = False, None
    mask = _edge_masker(ctx)
    sec = getattr(ctx.rt, "secrets", None)
    whole_mask = sec.mask if sec is not None and getattr(sec, "has_values", False) and callable(
        getattr(sec, "mask", None)) else None
    if os.path.isfile(_lp(root)):
        files: Iterable[Any] = [root]
    else:
        files = _search_files(ctx, root, p, deadline)
    for e in files:
        if time.monotonic() > deadline or ctx.cancel.is_set():
            truncated, reason = True, "timeout"
            break
        fpath = Path(e.path) if hasattr(e, "path") else Path(e)
        name = fpath.name
        scanned += 1
        if names_only:
            ok = fnmatch.fnmatchcase(name.lower(), pattern.lower()) if icase else fnmatch.fnmatchcase(name, pattern)
            if ok:
                matched_files += 1
                matches.append({"file": str(fpath), "line": None, "text": None, "before": [], "after": []})
                if len(matches) >= max_results:
                    truncated, reason = True, "max_results"
                    break
            continue
        try:
            size = os.path.getsize(_lp(fpath))
            if size > max_file:
                continue
            with fsops.open_regular(fpath) as f:
                data = f.read(max_file + 1)
        except (OSError, HiveError):
            continue
        enc, binary = fsops.detect_encoding(data[:fsops.HEAD_BYTES])
        if binary:
            continue
        text = fsops.decode_bytes(data, enc)[0]
        if whole_mask is not None:
            # Phase 2 review: match against the masked text, so the match predicate is no oracle over secret
            # bytes (§5.1: a secret value never shows in a result, not even one bit of it at a time)
            text = whole_mask(text)
        lines = [fsops._strip_eol(x) for x in fsops.split_lines(text)]
        hit_here = False
        for i, line in enumerate(lines):
            if i % 2000 == 0 and time.monotonic() > deadline:
                truncated, reason = True, "timeout"
                break
            cut = False
            if rx is not None:
                subject = line[:REGEX_LINE_CAP]
                cut = len(line) > REGEX_LINE_CAP
                found = rx.search(subject) is not None
            else:
                found = needle in (line.lower() if icase else line)
            if not found:
                continue
            hit_here = True
            row: Dict[str, Any] = {"file": str(fpath), "line": i + 1, "text": _cut(line, mask=mask),
                                   "before": [_cut(x, mask=mask) for x in lines[max(0, i - ctx_n):i]] if ctx_n else [],
                                   "after": [_cut(x, mask=mask) for x in lines[i + 1:i + 1 + ctx_n]] if ctx_n else []}
            if cut:
                row["cut"] = True
            matches.append(row)
            if len(matches) >= max_results:
                truncated, reason = True, "max_results"
                break
        if hit_here:
            matched_files += 1
        if truncated:
            break
    if not truncated and time.monotonic() > deadline:
        truncated, reason = True, "timeout"
    return {"matches": matches, "files_scanned": scanned, "files_matched": matched_files, "truncated": truncated,
            "reason": reason, "elapsed_s": round(time.monotonic() - t0, 3)}


# ---------------------------------------------------------------------------------------------------------------
# mkdir / move / copy / delete

@handler("fs.mkdir")
def fs_mkdir(ctx: ActionContext, p: dict) -> dict:
    """Create a folder (with parents by default)."""
    path = ctx.policy.resolve_path(p["path"], write=True)
    with os_errors(path):
        if os.path.lexists(_lp(path)):
            if os.path.isdir(_lp(path)):
                return {"path": str(path), "created": False}
            raise conflict(f"'{path}' exists and is not a folder")
        if p.get("parents", True):
            os.makedirs(_lp(path), exist_ok=True)
        else:
            os.mkdir(_lp(path))
    return {"path": str(path), "created": True}


def _dest_check(ctx: ActionContext, src: Path, dst: Path) -> None:
    bad = ctx.policy.denied_under(src, write=True, rebase=dst)
    if bad:
        raise denied(f"the destination '{dst}' would place entries on protected paths ({bad[0]}"
                     f"{', …' if len(bad) > 1 else ''})", rule="contains_denied", entries=[str(b) for b in bad[:10]],
                     hint="choose another destination; writing there could make code run later or touch secrets")


def _guard_target(ctx: ActionContext, target: str, done: Dict[str, int]) -> None:
    """Re-check one destination entry right before it is created (D53, §5.4 steps 7–8): the pre-check cannot see
    entries added to the source later or a symbolic link already inside the destination. The entry's own location
    is checked (``nofollow_last``: parents resolved, so a linked parent folder is caught)."""
    try:
        ctx.policy.resolve_path(target, write=True, nofollow_last=True)
    except HiveError as e:
        if e.code != "policy_denied":
            raise
        raise denied(f"the copy stopped at '{target}': {e.message}", rule="contains_denied", entries=[target],
                     denied_rule=e.details.get("rule"), copied_files=done.get("files", 0),
                     hint="choose another destination; writing there could make code run later or touch secrets "
                          "(entries copied before this one stay in the destination)")


def _copy_file(src: str, dst: str) -> int:
    d = os.path.dirname(dst)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(dst)[:60] + ".", suffix=".hive-tmp", dir=_lp(d))
    os.close(fd)
    try:
        shutil.copyfile(_lp(src), tmp)
        try:
            shutil.copymode(_lp(src), tmp)
        except OSError:
            pass
        util.replace_with_retry(tmp, _lp(dst))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return os.path.getsize(_lp(dst))


def _copy_link(src: str, dst: str) -> bool:
    """Recreate a symbolic link (links stay links); False when Windows refuses to create one (no
    SeCreateSymbolicLinkPrivilege / Developer Mode): the caller skips and reports it."""
    to = os.readlink(_lp(src))
    try:
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            os.symlink(to, _lp(dst), target_is_directory=os.path.isdir(_lp(src)))
        else:
            os.symlink(to, _lp(dst))
        return True
    except OSError as e:
        if util.IS_WINDOWS and getattr(e, "winerror", None) == WINERROR_PRIVILEGE_NOT_HELD:  # pragma: no cover
            return False
        raise


def _copy_tree(ctx: ActionContext, src: Path, dst: Path, overwrite: bool) -> Dict[str, int]:
    """Copy the tree at ``src`` into the existing folder ``dst``: denied source entries are skipped (walk), every
    destination entry is re-checked before it is written (``_guard_target``), links stay links. Raises ``timeout``
    (maybe_executed) when the deadline cut the walk short: a partial copy is never reported as complete."""
    done = {"files": 0, "bytes": 0, "dirs": 0, "links": 0, "skipped_links": 0}
    state: Dict[str, Any] = {}
    for e in fsops.walk(src, depth=4096, include_hidden=True, guard=ctx.policy, deadline=_deadline(ctx),
                        state=state):
        ctx.check()
        rel = os.path.relpath(e.path, str(src))
        target = os.path.join(str(dst), rel)
        if e.is_symlink():
            if os.path.lexists(_lp(target)):
                if not overwrite or (os.path.isdir(_lp(target)) and not os.path.islink(_lp(target))):
                    continue
                _guard_target(ctx, target, done)
                os.unlink(_lp(target))
            else:
                _guard_target(ctx, target, done)
            if _copy_link(e.path, target):
                done["files"] += 1
                done["links"] += 1
            else:
                done["skipped_links"] += 1
        elif e.is_dir(follow_symlinks=False):
            _guard_target(ctx, target, done)
            os.makedirs(_lp(target), exist_ok=True)
            done["dirs"] += 1
        elif e.is_file(follow_symlinks=False):
            if os.path.lexists(_lp(target)) and not overwrite:
                raise conflict(f"'{target}' already exists", hint="set overwrite=true to replace files")
            _guard_target(ctx, target, done)
            done["bytes"] += _copy_file(e.path, target)
            done["files"] += 1
        if done["files"] > COPY_CAP:
            raise too_large(f"more than {COPY_CAP} files", hint="copy a smaller folder")
    if state.get("truncated"):
        raise timeout(f"copy incomplete: {done['files']} files copied before the deadline", maybe_executed=True,
                      files=done["files"], bytes=done["bytes"],
                      hint=f"copy incomplete: {done['files']} files copied; re-run with overwrite=true and a longer "
                           "timeout, or copy a smaller folder")
    ctx.check()
    return done


def _move_across(ctx: ActionContext, src: Path, dst: Path, *, is_link: bool) -> None:
    """fs.move between filesystems/drives (EXDEV): guarded copy, then delete the source (never ``shutil.move``,
    which would write the destination tree unchecked). A folder is deleted only when every entry was copied."""
    if is_link:
        if os.path.lexists(_lp(dst)):
            os.unlink(_lp(dst))
        if not _copy_link(str(src), str(dst)):  # pragma: no cover - Windows only
            raise unsupported("this node cannot create symbolic links (Windows: Developer Mode or admin rights)")
        os.unlink(_lp(src))
    elif not os.path.isdir(_lp(src)):
        _copy_file(str(src), str(dst))
        os.unlink(_lp(src))
    else:
        if os.path.lexists(_lp(dst)):
            raise conflict(f"'{dst}' already exists", hint="move to a new path (folders are not merged)")
        files, dirs = _count_tree(src, COPY_CAP)
        os.makedirs(_lp(dst))
        done = _copy_tree(ctx, src, dst, overwrite=False)
        if done["files"] + done["skipped_links"] != files or done["dirs"] != dirs:
            raise conflict(f"moved only part of '{src}' to '{dst}' ({done['files']} of {files} files); the source "
                           "was kept", hint="check both folders; delete the partial copy or the source yourself",
                           copied_files=done["files"])
        if sys.version_info >= (3, 12):
            shutil.rmtree(_lp(src), onexc=_rm_onerror)   # type: ignore[call-arg]
        else:  # pragma: no cover - Python < 3.12
            shutil.rmtree(_lp(src), onerror=_rm_onerror)


@handler("fs.move")
def fs_move(ctx: ActionContext, p: dict) -> dict:
    """Move/rename a file or folder (subtree and destination checks, §5.4 step 8)."""
    link = _link_at(ctx, p["src"], write=True)
    if link is not None:
        src = link
        _refuse_protected(ctx, src, "move", follow=False)
    else:
        src = ctx.policy.resolve_path(p["src"], write=True, must_exist=True, subtree=True)
        _refuse_protected(ctx, src, "move")
    dst = ctx.policy.resolve_path(p["dst"], write=True)
    _dest_check(ctx, src, dst)
    with os_errors(src):
        if _pkey(ctx, src, follow=False) == _pkey(ctx, dst, follow=False):
            raise bad_request("source and destination are the same")
        if link is None and os.path.isdir(_lp(src)) and _inside(ctx, dst, src):
            raise bad_request("cannot move a folder into itself")
        if os.path.lexists(_lp(dst)):
            if not p.get("overwrite"):
                raise conflict(f"'{dst}' already exists", hint="set overwrite=true to replace a file")
            if os.path.isdir(_lp(dst)) and not os.path.islink(_lp(dst)):
                raise conflict(f"'{dst}' is an existing folder", hint="move into a new path below it instead")
        if not os.path.isdir(_lp(dst.parent)):
            raise not_found(f"folder '{dst.parent}' does not exist", hint="create it with fs_mkdir first")
        try:
            os.replace(_lp(src), _lp(dst))
        except OSError as e:
            if getattr(e, "errno", None) != errno.EXDEV:   # other filesystem / drive -> guarded copy + delete
                raise
            _move_across(ctx, src, dst, is_link=link is not None)
    ctx.audit_extra.update(path=str(dst))
    return {"src": str(src), "dst": str(dst)}


@handler("fs.copy")
def fs_copy(ctx: ActionContext, p: dict) -> dict:
    """Copy a file, or a folder with recursive=true (denied entries are never copied; links stay links)."""
    recursive = bool(p.get("recursive"))
    src = ctx.policy.resolve_path(p["src"], must_exist=True, subtree=recursive)
    dst = ctx.policy.resolve_path(p["dst"], write=True)
    overwrite = bool(p.get("overwrite"))
    _dest_check(ctx, src, dst)            # fast pre-check; every entry is checked again when it is written
    files = nbytes = 0
    skipped_links = 0
    with os_errors(src):
        if _pkey(ctx, src) == _pkey(ctx, dst):
            raise bad_request("source and destination are the same")
        if not os.path.isdir(_lp(dst.parent)):
            raise not_found(f"folder '{dst.parent}' does not exist", hint="create it with fs_mkdir first")
        if os.path.isdir(_lp(src)):
            if not recursive:
                raise bad_request(f"'{src}' is a folder", hint="set recursive=true to copy folders")
            if _inside(ctx, dst, src):
                raise bad_request("cannot copy a folder into itself")
            if os.path.lexists(_lp(dst)) and not overwrite:
                raise conflict(f"'{dst}' already exists", hint="set overwrite=true to merge into it")
            if os.path.lexists(_lp(dst)) and not os.path.isdir(_lp(dst)):
                raise conflict(f"'{dst}' exists and is not a folder")
            os.makedirs(_lp(dst), exist_ok=True)
            done = _copy_tree(ctx, src, dst, overwrite)
            files, nbytes, skipped_links = done["files"], done["bytes"], done["skipped_links"]
        else:
            if os.path.lexists(_lp(dst)):
                if os.path.isdir(_lp(dst)):
                    raise conflict(f"'{dst}' is a folder", hint="give the full destination file path")
                if not overwrite:
                    raise conflict(f"'{dst}' already exists", hint="set overwrite=true to replace it")
            nbytes = _copy_file(str(src), str(dst))
            files = 1
    ctx.audit_extra.update(path=str(dst), bytes=nbytes)
    out: Dict[str, Any] = {"src": str(src), "dst": str(dst), "files": files, "bytes": nbytes}
    if skipped_links:
        out["skipped_links"] = skipped_links
    return out


def _count_tree(path: Path, cap: int) -> Tuple[int, int]:
    files = dirs = 0
    stack = [_lp(path)]
    while stack:
        d = stack.pop()
        with os.scandir(d) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    dirs += 1
                    stack.append(e.path)
                else:
                    files += 1
                if files + dirs > cap:
                    raise too_large(f"more than {cap} entries to delete",
                                    hint="delete smaller subfolders (node limit max_delete_entries)")
    return files, dirs


def _rm_onerror(func: Any, path: str, exc: Any) -> None:
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | (stat.S_IEXEC if os.path.isdir(path) else 0))
        func(path)
    except OSError:
        raise


@handler("fs.delete")
def fs_delete(ctx: ActionContext, p: dict) -> dict:
    """Delete a file, an empty folder, or (recursive) a tree; roots, home, workdir and data dir are refused."""
    raw = p["path"]
    recursive = bool(p.get("recursive"))
    link = _link_at(ctx, raw, write=True)       # vets the link's own path and its target (note 78)
    if link is not None:
        _refuse_protected(ctx, link, "delete", follow=False)
        with os_errors(link):
            os.unlink(_lp(link))
        ctx.audit_extra.update(path=str(link), deleted=1)
        return {"path": str(link), "deleted_files": 1, "deleted_dirs": 0}
    path = ctx.policy.resolve_path(raw, write=True, subtree=recursive)
    if not os.path.lexists(_lp(path)):
        if p.get("missing_ok"):
            return {"path": str(path), "deleted_files": 0, "deleted_dirs": 0}
        raise not_found(f"path '{path}' does not exist")
    _refuse_protected(ctx, path, "delete")
    with os_errors(path):
        if os.path.isdir(_lp(path)):
            if not recursive:
                with os.scandir(_lp(path)) as it:
                    nonempty = any(True for _ in it)
                if nonempty:
                    raise conflict(f"folder '{path}' is not empty", hint="set recursive=true to delete it with its "
                                                                         "contents")
                os.rmdir(_lp(path))
                files, dirs = 0, 1
            else:
                files, dirs = _count_tree(path, int(ctx.policy.limits.max_delete_entries))
                if sys.version_info >= (3, 12):
                    shutil.rmtree(_lp(path), onexc=_rm_onerror)   # type: ignore[call-arg]
                else:  # pragma: no cover - Python < 3.12
                    shutil.rmtree(_lp(path), onerror=_rm_onerror)
                dirs += 1
        else:
            os.unlink(_lp(path))
            files, dirs = 1, 0
    ctx.audit_extra.update(path=str(path), deleted=files + dirs)
    return {"path": str(path), "deleted_files": files, "deleted_dirs": dirs}


__all__ = ["fs_list", "fs_stat", "fs_read", "fs_read_chunk", "fs_write", "fs_write_chunk", "fs_edit", "fs_search",
           "fs_mkdir", "fs_move", "fs_copy", "fs_delete"]
