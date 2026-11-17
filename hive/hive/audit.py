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
import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional

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


_SECRET_FLAG = r"--?[A-Za-z0-9_-]*(?:key|token|secret|passw(?:or)?d|passwd|auth|cred|cookie|bearer)[A-Za-z0-9_-]*"
_RULES = [
    # --api-key X / --token=X / --password X (also -p after mysql/psql below)
    (re.compile(r"(?i)(?<![\w-])(" + _SECRET_FLAG + r")(=|[ \t]+)(\"[^\"]*\"|'[^']*'|[^\s\"']+)"), r"\1\2***"),
    # mysql/psql -p SECRET / -pSECRET
    (re.compile(r"(?i)(\b(?:mysql|mysqldump|mysqladmin|mariadb|psql)\b[^\n;|&]*?\s-p)([ \t]*)([^\s-][^\s]*)"),
     r"\1\2***"),
    # URL userinfo (scheme://user:pass@host), incl. proxy URLs
    (re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@\"'<>]+@"), r"\1***@"),
    # Authorization / Proxy-Authorization header values
    (re.compile(r"(?i)\b((?:proxy-)?authorization)(\s*[:=]\s*)(?:\"[^\"\n]*\"|'[^'\n]*'|[^\n,;\"'}]+)"), r"\1\2***"),
    # Hive tokens
    (re.compile(r"\bhv_[A-Za-z0-9_-]{8,}"), "hv_***"),
    # NAME=value assignments of secret-looking names
    (re.compile(r"(?i)\b([A-Z0-9_]*(?:PASS|SECRET|TOKEN|KEY|AUTH|CRED)[A-Z0-9_]*)=(\S+)"), r"\1=***"),
    # well-known key prefixes
    (re.compile(r"\bhf_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "***"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"), "***"),
    (re.compile(r"\bgithub_pat_\w{20,}"), "***"),
    (re.compile(r"\bxox[abprs]-[\w-]+"), "***"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "***"),
]


def redact_text(s: str) -> str:
    """Text with tokens, keys, URL userinfo, known secret prefixes and node secret values masked."""
    if not isinstance(s, str) or not s:
        return s
    out = _apply_mask(s)
    for rx, repl in _RULES:
        out = rx.sub(repl, out)
    return _apply_mask(out)


def redact_env(env: Mapping[str, str]) -> Dict[str, str]:
    """Env with the values of secret-looking keys replaced by ``***`` (other values pass ``redact_text``)."""
    out: Dict[str, str] = {}
    for k, v in (env or {}).items():
        out[str(k)] = "***" if _SECRET_KEY.search(str(k)) else redact_text(str(v))
    return out


def _digest(value: Any) -> Dict[str, Any]:
    if isinstance(value, bytes):
        raw = value
        n = len(value)
    elif isinstance(value, str):
        raw = value.encode("utf-8", "surrogatepass")
        n = len(value)
    else:
        try:
            text = canonical_json(value)
        except (TypeError, ValueError):
            text = repr(value)
        raw = text.encode("utf-8", "surrogatepass")
        n = len(text)
    return {"len": n, "sha256": hashlib.sha256(raw).hexdigest()[:12]}


def _names_only(value: Any) -> List[str]:
    if isinstance(value, dict):
        return [str(k) for k in value]
    names: List[str] = []
    for item in value if isinstance(value, (list, tuple)) else [value]:
        s = str(item)
        for sep in ("=", ":"):
            if sep in s:
                s = s.split(sep, 1)[0]
                break
        names.append(s.strip())
    return names


def _short(s: str, limit: int) -> str:
    s = redact_text(s)
    if len(s) > limit:
        return s[:limit] + f"…[{len(s) - limit} chars]"
    return s


def _redact_argv(argv: List[Any]) -> List[str]:
    out: List[str] = []
    hide_next = False
    flag_re = re.compile(r"(?i)^" + _SECRET_FLAG + r"$")
    for item in argv:
        s = str(item)
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if flag_re.match(s):
            hide_next = True
            out.append(s)
            continue
        out.append(_short(s, _STR_MAX))
    return out


def _redact_value(key: str, value: Any) -> Any:
    lk = key.lower()
    if lk in _BODY_KEYS and value is not None:
        return _digest(value)
    if lk in ("env", "headers", "env_unset"):
        return _names_only(value) if value is not None else value
    if lk not in _NAME_KEYS and _SECRET_KEY.search(key) and not isinstance(value, (bool, int, float)) \
            and value is not None:
        return "***"
    if lk in ("command", "stop_command") and isinstance(value, str):
        return _short(value, _COMMAND_MAX)
    if lk in ("argv", "stop_argv") and isinstance(value, list):
        return _redact_argv(value)
    return _redact_any(value)


def _redact_any(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_any(v) for v in value]
    if isinstance(value, str):
        return _short(value, _STR_MAX)
    if isinstance(value, bytes):
        return _digest(value)
    return value


def redact_params(action: str, params: dict) -> dict:
    """Params with secret-named keys masked, bodies hashed, env/headers reduced to names, strings capped."""
    if not isinstance(params, dict):
        return {} if params is None else {"_": _redact_any(params)}
    return {str(k): _redact_value(str(k), v) for k, v in params.items()}


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
            "params": redact_params(action or "", params) if params is not None else None,
            "ok": bool(ok), "code": code, "ms": int(ms or 0),
            "extra": _redact_any(extra) if extra else None,
        }
        rec = _mask_deep(rec, self.mask)
        try:
            line = json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
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


__all__ = ["SECRET_KEY_RE", "KINDS", "AuditLog", "redact_params", "redact_env", "redact_text", "set_text_mask"]
