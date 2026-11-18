"""AuditLog (daily JSONL) and redaction helpers redact_params/redact_env/redact_text, SECRET_KEY_RE
(design §5.8, WP1; v1 redact.py merged here).

- Files ``<data>/audit/audit-YYYYMMDD.jsonl`` (UTC day), appended under a lock and flushed; retention pruning at start
  and daily. Record: ``{"ts","id","kind","node","hub","identity","agent","ip","via","action","rid","params","ok","code",
  "ms","extra"}``. Params are redacted with ``redact_params``; every string passes the node secret mask.
- ``required`` audit fails closed (``internal``) when a record cannot be written; otherwise the failure is logged.
- ``redact_text`` masks CLI secrets (``--api-key X``, ``--token=X``, ``mysql -p X``), URL userinfo (proxy URLs too),
  ``Authorization:`` values, ``hv_…`` tokens, ``NAME=value`` assignments of secret-looking names and well-known key
  prefixes (``hf_``, ``sk-``, ``ghp_``…, ``AKIA…``), and applies the node secret-value mask (set by NodeRuntime).
"""
from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Tuple

from .errors import internal
from .util import canonical_json, iso_utc, new_id, secure_mkdir

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity

log = logging.getLogger("hive.audit")

SECRET_KEY_RE = r"(?i)(pass|secret|token|key|auth|cred|cookie|bearer)"
_SECRET_KEY = re.compile(SECRET_KEY_RE)

KINDS = ("call", "auth_fail", "approval", "relay", "lifecycle", "warning", "gateway")

# keys whose values are bodies: replaced by {"len", "sha256": first 12}
_BODY_KEYS = frozenset({"content", "data", "body", "stdin", "messages", "prompt", "input", "extra_json",
                        "body_base64", "new_text", "old_text"})
# secret-looking key names whose values are NAMES of secrets or plain numbers, never secret values
_NAME_KEYS = frozenset({"secret_env", "auth_env", "api_key_env", "max_tokens", "token_sha256", "requester_key",
                        "identity_key"})
_STR_MAX = 500
_COMMAND_MAX = 4096

# ---------------------------------------------------------------------------------------------------------------
# redaction

_MASK_LOCK = threading.Lock()
_TEXT_MASK: Optional[Callable[[str], str]] = None


def set_text_mask(fn: Optional[Callable[[str], str]]) -> None:
    """Register the node secret-value mask (``SecretStore.mask``) used by ``redact_text`` (None clears it)."""
    global _TEXT_MASK
    with _MASK_LOCK:
        _TEXT_MASK = fn


def _apply_mask(s: str, mask: Optional[Callable[[str], str]] = None) -> str:
    fn = mask or _TEXT_MASK
    if fn is None:
        return s
    try:
        return fn(s)
    except Exception:  # noqa: BLE001 - masking must never break logging
        return s


_SECRET_WORDS = r"(?:key|token|secret|passw(?:or)?d|passwd|auth|cred|cookie|bearer)"
# Audit round 4 (DoS): every rule is linear in the input. Python 3.9 has no atomic groups, so a whole flag word or
# NAME word is consumed atomically with the lookahead + backreference idiom ``(?=(X*))\1`` after a lazy lookahead
# proved it contains a secret word; nothing can backtrack into the word (the old ``X*KEY X*`` forms were quadratic).
_FLAG_RE = re.compile(r"(?i)(?<![\w-])(?=--?[A-Za-z0-9_-]*?" + _SECRET_WORDS + r")(?=(--?[A-Za-z0-9_-]*))\1"
                      r"(=|[ \t]+)(\"[^\"]*\"|'[^']*'|[^\s\"']+)")
_FLAG_WORD_RE = re.compile(r"(?i)(?<![\w-])(?=--?[A-Za-z0-9_-]*?" + _SECRET_WORDS + r")(?=(--?[A-Za-z0-9_-]*))\1\n?\Z")
_MYSQL_WORD = re.compile(r"(?i)\b(?:mysql|mysqldump|mysqladmin|mariadb|psql)\b")
_MYSQL_P = re.compile(r"(?i)\s-p")
_MYSQL_VALUE = re.compile(r"[ \t]*([^\s-]\S*)")
_SEGMENT_END = re.compile(r"[\n;|&]")


def _redact_mysql(s: str) -> str:
    """mysql/psql ``-p SECRET`` / ``-pSECRET``: the first ``-p`` with a value after each client name within its
    command segment (up to newline ; | &). Linear: each region is scanned once (a segment without ``-p`` is skipped
    whole, it holds none for any later client name either)."""
    w = _MYSQL_WORD.search(s)
    if w is None:
        return s
    out: List[str] = []
    pos = 0
    seg_end = -1
    e = None
    while w is not None:
        if w.end() > seg_end:          # the segment end is searched once per segment, not once per client name
            e = _SEGMENT_END.search(s, w.end())
            seg_end = e.start() if e is not None else len(s)
        # the whitespace before -p may be the newline that ends the segment ("mysql …\n-pSECRET")
        p_end = seg_end + 3 if e is not None and s[seg_end] == "\n" else seg_end
        nxt = seg_end
        at = w.end()
        while True:
            m = _MYSQL_P.search(s, at, p_end)
            if m is None:
                break
            v = _MYSQL_VALUE.match(s, m.end())
            if v is not None:
                out.append(s[pos:v.start(1)])
                out.append("***")
                pos = nxt = v.end()
                break
            at = m.end()
        w = _MYSQL_WORD.search(s, nxt)
    out.append(s[pos:])
    return "".join(out)


_RULES = [
    # --api-key X / --token=X / --password X (also -p after mysql/psql, _redact_mysql)
    (_FLAG_RE, r"\1\2***"),
    # URL userinfo (scheme://user:pass@host), incl. proxy URLs. Round 4: matched from "://" on (the old
    # ``scheme*://`` start was quadratic on long letter runs); whatever precedes "://" is kept as it is
    (re.compile(r"(://)[^/\s@\"'<>]+@"), r"\1***@"),
    # Authorization / Proxy-Authorization header values
    (re.compile(r"(?i)\b((?:proxy-)?authorization)(\s*[:=]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\n,;\"'}]+)"), r"\1\2***"),
    # Hive tokens
    (re.compile(r"\bhv_[A-Za-z0-9_-]{8,}"), "hv_***"),
    # NAME=value assignments of secret-looking names (whole NAME word consumed atomically)
    (re.compile(r"(?i)\b(?=[A-Z0-9_]*?(?:PASS|SECRET|TOKEN|KEY|AUTH|CRED))(?=([A-Z0-9_]*))\1=(\S+)"), r"\1=***"),
    # well-known key prefixes
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bgithub_pat_\w{20,}"), "***"),
    (re.compile(r"\bxox[abprs]-[\w-]+"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
]

# a node secret value is at most secretstore.MAX_VALUE_CHARS long: masking this far past a cut masks a value that
# straddles it whole (never half)
_MASK_SPAN = 16384


def redact_text(s: str) -> str:
    """Text with tokens, keys, URL userinfo, known secret prefixes and node secret values masked (linear time)."""
    if not isinstance(s, str) or not s:
        return s
    out = _apply_mask(s)
    out = _FLAG_RE.sub(r"\1\2***", out)
    out = _redact_mysql(out)
    for rx, repl in _RULES[1:]:
        out = rx.sub(repl, out)
    return _apply_mask(out)


def redact_head(s: str, limit: int) -> Tuple[str, bool]:
    """``(redacted text of at most limit chars, cut)``. Audit round 4 (DoS): only a bounded window of ``s`` is
    ever redacted (2*limit + 64 chars; the secret-value mask looks 16 KiB further so a value across the cut is
    masked whole), and the word cut by the window is dropped, so a token is never shown half-redacted."""
    limit = max(0, int(limit))
    window = 2 * limit + 64
    if not isinstance(s, str):
        s = str(s)
    if len(s) <= window:
        out = redact_text(s)
        return (out[:limit], True) if len(out) > limit else (out, False)
    head = _apply_mask(s[:window + _MASK_SPAN])[:window]
    i = len(head) - 1
    while i >= 0 and not head[i].isspace():
        i -= 1
    out = redact_text(head[:i + 1] if i >= 0 else "")
    return out[:limit], True


def redact_env(env: Mapping[str, str]) -> Dict[str, str]:
    """Env with the values of secret-looking keys replaced by ``***`` (other values pass ``redact_text``)."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        out[str(k)] = "***" if _SECRET_KEY.search(str(k)) else redact_text(str(v))
    return out


# Verification round 4 (DoS): params containers were copied whole (twice) into one JSONL line - a 48 MiB body of
# empty lists wrote a 50 MB audit record. Audit records now go through a bounded walk (``_Budget``): at most
# _CONTAINER_MAX items/keys per container (_ARGV_MAX argv words), _NODES_MAX values and _DEPTH_MAX levels in all,
# the rest summarized ("…[+N items]"), and the serialized record is capped at _RECORD_MAX bytes.
_CONTAINER_MAX = 32
_ARGV_MAX = 128
_NODES_MAX = 2000
_DEPTH_MAX = 24
_DIGEST_NODES_MAX = 10000
_RECORD_MAX = 64 << 10
_MORE = "…"


class _Budget:
    __slots__ = ("left",)

    def __init__(self, nodes: int = _NODES_MAX):
        self.left = nodes

    def take(self, depth: int) -> bool:
        """Spend one value; False when the walk is over budget or too deep (summarize instead)."""
        self.left -= 1
        return self.left >= 0 and depth <= _DEPTH_MAX


def _summary(value: Any) -> Any:
    """A value the bounded walk does not descend into: its type and size only."""
    if isinstance(value, dict):
        return {_MORE: f"[{len(value)} keys]"}
    if isinstance(value, (list, tuple)):
        return [f"{_MORE}[{len(value)} items]"]
    if isinstance(value, str):
        return f"{_MORE}[{len(value)} chars]"
    if isinstance(value, bytes):
        return {"len": len(value)}
    return value


def _nodes_at_most(value: Any, limit: int) -> bool:
    """True when ``value`` holds at most ``limit`` values (stops counting early)."""
    stack, seen = [value], 0
    while stack:
        v = stack.pop()
        seen += 1
        if seen > limit:
            return False
        if isinstance(v, (dict, list, tuple)):
            if len(v) > limit - seen:
                return False
            stack.extend(v.values() if isinstance(v, dict) else v)
    return True


def _digest(value: Any, budget: Optional[_Budget] = None) -> Dict[str, Any]:
    if isinstance(value, bytes):
        raw = value
        n = len(value)
    elif isinstance(value, str):
        raw = value.encode("utf-8", "surrogatepass")
        n = len(value)
    else:
        if budget is not None and not _nodes_at_most(value, _DIGEST_NODES_MAX):
            return {"items": len(value) if isinstance(value, (dict, list, tuple)) else 1, "sha256": None}
        try:
            text = canonical_json(value)
        except (TypeError, ValueError):
            text = repr(value)
        raw = text.encode("utf-8", "surrogatepass")
        n = len(text)
    return {"len": n, "sha256": hashlib.sha256(raw).hexdigest()[:12]}


def _names_only(value: Any, budget: Optional[_Budget] = None) -> List[str]:
    if isinstance(value, dict):
        keys = list(value) if budget is None else list(itertools.islice(value, _CONTAINER_MAX))
        out = [str(k)[:_STR_MAX] if budget is not None else str(k) for k in keys]
        if len(value) > len(keys):
            out.append(f"{_MORE}[+{len(value) - len(keys)} names]")
        return out
    names: List[str] = []
    items = value if isinstance(value, (list, tuple)) else [value]
    shown = items if budget is None else items[:_CONTAINER_MAX]
    for item in shown:
        s = str(item) if budget is None or not isinstance(item, (dict, list, tuple)) else type(item).__name__
        if budget is not None:
            s = s[:_STR_MAX]
        for sep in ("=", ":"):
            if sep in s:
                s = s.split(sep, 1)[0]
                break
        names.append(s.strip())
    if len(items) > len(shown):
        names.append(f"{_MORE}[+{len(items) - len(shown)} names]")
    return names


def _short(s: str, limit: int) -> str:
    out, cut = redact_head(s, limit)
    if cut:
        return out + f"…[{max(1, len(s) - len(out))} chars]"
    return out


def _redact_argv(argv: List[Any], budget: Optional[_Budget] = None, depth: int = 0) -> List[str]:
    out: List[str] = []
    hide_next = False
    shown = argv if budget is None else argv[:_ARGV_MAX]
    for item in shown:
        if budget is not None and not budget.take(depth + 1):
            out.append(f"{_MORE}[+{len(argv) - len(out)} items]")
            return out
        s = str(item) if budget is None or not isinstance(item, (dict, list, tuple)) else repr(_summary(item))
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if len(s) <= 256 and _FLAG_WORD_RE.match(s):
            hide_next = True
            out.append(s)
            continue
        out.append(_short(s, _STR_MAX))
    if len(argv) > len(shown):
        out.append(f"{_MORE}[+{len(argv) - len(shown)} items]")
    return out


def _redact_value(key: str, value: Any, budget: Optional[_Budget] = None, depth: int = 0) -> Any:
    lk = key.lower()
    if lk in _BODY_KEYS and value is not None:
        return _digest(value, budget)
    if lk in ("env", "headers", "env_unset"):
        return _names_only(value, budget) if value is not None else value
    if lk not in _NAME_KEYS and _SECRET_KEY.search(key) and not isinstance(value, (bool, int, float)) \
            and value is not None:
        return "***"
    if lk in ("command", "stop_command") and isinstance(value, str):
        return _short(value, _COMMAND_MAX)
    if lk in ("argv", "stop_argv") and isinstance(value, list):
        return _redact_argv(value, budget, depth)
    return _redact_any(value, budget, depth)


def _key(k: Any, budget: Optional[_Budget]) -> str:
    s = str(k)
    return s if budget is None or len(s) <= 128 else _short(s, 128)


def _redact_any(value: Any, budget: Optional[_Budget] = None, depth: int = 0) -> Any:
    if budget is not None and not budget.take(depth):
        return _summary(value)
    if isinstance(value, dict):
        if budget is None:
            return {str(k): _redact_value(str(k), v) for k, v in value.items()}
        out = {}
        for k, v in itertools.islice(value.items(), _CONTAINER_MAX):
            out[_key(k, budget)] = _redact_value(str(k), v, budget, depth + 1)
        if len(value) > _CONTAINER_MAX:
            out[_MORE] = f"[+{len(value) - _CONTAINER_MAX} keys]"
        return out
    if isinstance(value, (list, tuple)):
        if budget is None:
            return [_redact_any(v) for v in value]
        out_l = [_redact_any(v, budget, depth + 1) for v in value[:_CONTAINER_MAX]]
        if len(value) > _CONTAINER_MAX:
            out_l.append(f"{_MORE}[+{len(value) - _CONTAINER_MAX} items]")
        return out_l
    if isinstance(value, str):
        return _short(value, _STR_MAX)
    if isinstance(value, bytes):
        return _digest(value)
    return value


def redact_params(action: str, params: dict, *, bounded: bool = False) -> dict:
    """Params with secret-named keys masked, bodies hashed, env/headers reduced to names, strings capped.
    ``bounded`` (audit records): containers, total values and depth are capped too (see ``_Budget``)."""
    budget = _Budget() if bounded else None
    if not isinstance(params, dict):
        return {} if params is None else {"_": _redact_any(params, budget)}
    if budget is None:
        return {str(k): _redact_value(str(k), v) for k, v in params.items()}
    out = {}
    for k, v in itertools.islice(params.items(), _CONTAINER_MAX):
        out[_key(k, budget)] = _redact_value(str(k), v, budget, 1)
    if len(params) > _CONTAINER_MAX:
        out[_MORE] = f"[+{len(params) - _CONTAINER_MAX} keys]"
    return out


def _mask_deep(value: Any, mask: Optional[Callable[[str], str]]) -> Any:
    if isinstance(value, str):
        return _apply_mask(value, mask)
    if isinstance(value, dict):
        return {k: _mask_deep(v, mask) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_deep(v, mask) for v in value]
    return value


# ---------------------------------------------------------------------------------------------------------------
# AuditLog

_FILE_RE = re.compile(r"^audit-(\d{8})\.jsonl$")


class AuditLog:
    """Daily JSONL audit files with retention (§5.8)."""

    def __init__(self, dir: Path, *, enabled: bool = True, retention_days: int = 30, required: bool = False,
                 mask: Optional[Callable[[str], str]] = None):
        self.dir = Path(dir)
        self.enabled = bool(enabled)
        self.retention_days = int(retention_days)
        self.required = bool(required)
        self.mask = mask
        self.hub_name: Optional[str] = None      # set by the hub so records carry "hub"
        self._lock = threading.Lock()
        self._last_prune = 0.0
        if self.enabled:
            try:
                secure_mkdir(self.dir)
            except OSError as e:
                if self.required:
                    raise internal(f"audit directory {self.dir} is not writable: {e}")
                log.error("audit directory %s is not writable: %s", self.dir, e)

    def _path_for(self, ts: float) -> Path:
        return self.dir / ("audit-" + time.strftime("%Y%m%d", time.gmtime(ts)) + ".jsonl")

    def record(self, *, kind: str, identity: Optional["Identity"], ip: str, via: str, node: str,
               action: Optional[str], params: Optional[dict], ok: bool, code: Optional[str],
               ms: int, request_id: str, extra: Optional[dict] = None) -> None:
        """Append one record (redacted and masked); fails closed with ``internal`` when ``required``."""
        if not self.enabled:
            return
        now = time.time()
        rec = {
            "ts": iso_utc(now), "id": new_id("a_"), "kind": kind, "node": node or None, "hub": self.hub_name,
            "identity": identity.name if identity is not None else None,
            "agent": (identity.agent or None) if identity is not None else None,
            "ip": ip or None, "via": via or None, "action": action, "rid": request_id or None,
            "params": redact_params(action or "", params, bounded=True) if params is not None else None,
            "ok": bool(ok), "code": code, "ms": int(ms or 0),
            "extra": _redact_any(extra, _Budget()) if extra else None,
        }
        rec = _mask_deep(rec, self.mask)
        try:
            line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            for field in ("params", "extra"):        # verification round 4: one record is at most _RECORD_MAX
                if len(line) <= _RECORD_MAX:
                    break
                if isinstance(rec.get(field), dict):
                    keys = [str(k)[:64] for k in itertools.islice(rec[field], _CONTAINER_MAX)]
                    rec[field] = {_MORE: f"[record cut: {len(line)} bytes]", "keys": keys}
                    line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
            if len(line) > _RECORD_MAX:
                line = json.dumps({"ts": rec["ts"], "id": rec["id"], "kind": rec["kind"],
                                   "action": str(rec["action"])[:200] if rec["action"] is not None else None,
                                   "ok": rec["ok"], "code": rec["code"],
                                   "extra": {_MORE: f"[record cut: {len(line)} bytes]"}}, ensure_ascii=False) + "\n"
        except (TypeError, ValueError) as e:
            line = json.dumps({"ts": rec["ts"], "kind": "warning", "extra": {"error": f"unserializable record: {e}"}}
                              ) + "\n"
        path = self._path_for(now)
        try:
            with self._lock:
                fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                try:
                    os.write(fd, line.encode("utf-8", "surrogatepass"))
                finally:
                    os.close(fd)
        except OSError as e:
            if self.required:
                raise internal("audit log could not be written; the call was refused (audit.required)",
                               hint="check disk space and permissions of the audit folder")
            log.error("audit write failed: %s", e)
            return
        if now - self._last_prune > 86400:
            self._last_prune = now
            try:
                self.prune()
            except Exception as e:  # noqa: BLE001
                log.warning("audit prune failed: %s", e)

    def _files(self) -> List[Path]:
        try:
            names = [p for p in self.dir.iterdir() if _FILE_RE.match(p.name)]
        except OSError:
            return []
        return sorted(names, key=lambda p: p.name)

    def tail(self, n: int = 50, *, since: Optional[str] = None, action: Optional[str] = None) -> List[dict]:
        """The last ``n`` records (oldest first), optionally newer than ``since`` and for one action (glob)."""
        import fnmatch
        from .util import parse_iso
        since_ts = parse_iso(since) if since else None
        out: List[dict] = []
        for p in reversed(self._files()):
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if since_ts is not None:
                    try:
                        if parse_iso(rec.get("ts", "")) < since_ts:
                            return list(reversed(out))
                    except Exception:  # noqa: BLE001
                        continue
                if action and not fnmatch.fnmatchcase(str(rec.get("action") or ""), action):
                    continue
                out.append(rec)
                if len(out) >= n:
                    return list(reversed(out))
        return list(reversed(out))

    def prune(self) -> None:
        """Delete daily files older than ``retention_days``."""
        if self.retention_days <= 0:
            return
        cutoff = time.strftime("%Y%m%d", time.gmtime(time.time() - self.retention_days * 86400))
        for p in self._files():
            m = _FILE_RE.match(p.name)
            if m and m.group(1) < cutoff:
                try:
                    p.unlink()
                except OSError as e:
                    log.warning("could not prune %s: %s", p, e)


__all__ = ["SECRET_KEY_RE", "KINDS", "AuditLog", "redact_params", "redact_env", "redact_text", "redact_head",
           "set_text_mask"]
