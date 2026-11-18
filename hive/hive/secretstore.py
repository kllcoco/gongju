"""Node secrets: SecretStore, host binding, masking (design §5.13, WP1; NOT secrets.py, which would shadow the stdlib).

Secrets live on the node in ``<data>/secrets.json`` (0600, hard-denied for every action), come from
``HIVE_SECRET_<NAME>`` env vars at start (imported into memory and DELETED from the environment, with
``HIVE_SECRET_HOSTS_<NAME>`` setting their hosts), or from the node's own env for the ``secrets.env_passthrough``
names. Agents reference them by name only; values never reach agents, audit, argv or config and are masked in results
(``«secret:NAME»``). Each secret is bound to hosts (``netutil.auth_host_allowed``, rule C1 for secrets) and has an
``exec`` flag (may be injected into children).
"""
from __future__ import annotations

import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, MutableMapping, Optional, Sequence, Tuple

from .errors import HiveError, bad_request, denied, not_found
from .util import FileLock, IS_WINDOWS, iso_utc, read_json, secure_file, write_json_atomic

if TYPE_CHECKING:  # pragma: no cover
    from .netutil import AuthSpec

log = logging.getLogger("hive.secretstore")

_HF = ("huggingface.co", "*.huggingface.co", "hf.co", "*.hf.co")
_GH = ("github.com", "api.github.com", "*.githubusercontent.com")
DEFAULT_SECRET_HOSTS: Dict[str, Tuple[str, ...]] = {
    "HF_TOKEN": _HF, "HUGGING_FACE_HUB_TOKEN": _HF,
    "MODELSCOPE_API_TOKEN": ("modelscope.cn", "*.modelscope.cn"),
    "OPENAI_API_KEY": ("api.openai.com",), "ANTHROPIC_API_KEY": ("api.anthropic.com",),
    "CIVITAI_API_TOKEN": ("civitai.com", "*.civitai.com"),
    "GITHUB_TOKEN": _GH, "GH_TOKEN": _GH,
}
OTHER_SECRET_HOSTS: Tuple[str, ...] = ("loopback",)   # any other name defaults to loopback only

NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}\Z")
MAX_VALUE_CHARS = 16384
MAX_SECRETS = 64
MIN_MASK_CHARS = 8
ENV_PREFIX = "HIVE_SECRET_"
ENV_HOSTS_PREFIX = "HIVE_SECRET_HOSTS_"


@dataclass(frozen=True)
class Secret:
    """One node secret; ``repr`` never shows the value."""
    name: str
    value: str = field(repr=False)
    hosts: Tuple[str, ...]
    exec_ok: bool
    set_at: str
    source: str                       # "store" | "env-import" | "passthrough"

    def __repr__(self) -> str:
        return (f"Secret(name={self.name!r}, hosts={self.hosts!r}, exec_ok={self.exec_ok!r}, "
                f"set_at={self.set_at!r}, source={self.source!r})")


def default_hosts(name: str) -> Tuple[str, ...]:
    """Default host binding of a secret name (§5.13)."""
    return DEFAULT_SECRET_HOSTS.get(name, OTHER_SECRET_HOSTS)


def parse_hosts(name: str, spec: Optional[Sequence[str]]) -> Tuple[str, ...]:
    """``--hosts`` syntax: None -> defaults; ``["+h"]`` entries add to the defaults; a plain list replaces them;
    ``"*"`` = any host (https only)."""
    if spec is None:
        return default_hosts(name)
    items: List[str] = []
    for raw in spec:
        for part in str(raw).split(","):
            p = part.strip()
            if p:
                items.append(p)
    if not items:
        raise bad_request("empty host list", hint="e.g. --hosts huggingface.co,*.huggingface.co or --hosts +hf-mirror.com")
    add = any(p.startswith("+") for p in items)
    base: List[str] = list(default_hosts(name)) if add else []
    for p in items:
        h = p[1:].strip() if p.startswith("+") else p
        h = h.lower().rstrip(".")
        if not h or any(c.isspace() for c in h) or "/" in h:
            raise bad_request(f"invalid host entry {p!r}", hint="host globs like *.example.com, or loopback/private")
        if h not in base:
            base.append(h)
    return tuple(base)


def _valid_name(name: str) -> bool:
    return isinstance(name, str) and bool(NAME_RE.match(name)) and not name.startswith("HIVE_")


def _check_value(value: str) -> None:
    if not isinstance(value, str) or not (1 <= len(value) <= MAX_VALUE_CHARS):
        raise bad_request(f"secret value must be 1..{MAX_VALUE_CHARS} characters")
    if any(c in value for c in ("\x00", "\r", "\n")):
        raise bad_request("secret value must not contain NUL, CR or LF")


class SecretStore:
    """Node secrets: store file + env imports + passthrough names; host binding, exec flag, masking (§5.13)."""

    def __init__(self, path: Path, *, passthrough: Sequence[str], env: MutableMapping[str, str] = os.environ):
        self.path = Path(path)
        self.passthrough = tuple(n for n in passthrough or () if _valid_name(n))
        self.node_name = "this node"
        self._env = env
        self._lock = threading.RLock()
        self._store: Dict[str, Secret] = {}
        self._imported: Dict[str, Secret] = {}
        self._pass_hosts: Dict[str, Tuple[str, ...]] = {}
        self._mtime: Optional[Tuple[float, int]] = None
        self._last_check = 0.0
        self._mask_key: Optional[Tuple[Tuple[str, str], ...]] = None
        self._mask_pairs: Tuple[Tuple[str, str], ...] = ()
        self._import_env()
        self._reload(force=True)

    # ---------------------------------------------------------------------------------------------- loading
    def _import_env(self) -> None:
        env = self._env
        hosts_spec: Dict[str, str] = {}
        started = iso_utc()
        for key in list(env.keys()):
            if key.upper().startswith(ENV_HOSTS_PREFIX):
                hosts_spec[key[len(ENV_HOSTS_PREFIX):]] = env.get(key, "")
                self._env_pop(key)
        for key in list(env.keys()):
            if not key.upper().startswith(ENV_PREFIX) or key.upper().startswith(ENV_HOSTS_PREFIX):
                continue
            value = env.get(key, "")
            self._env_pop(key)
            name = key[len(ENV_PREFIX):]
            if not _valid_name(name):
                log.warning("ignored env secret %s: invalid name (use ^[A-Z_][A-Z0-9_]*$, not HIVE_*)", key)
                continue
            try:
                _check_value(value)
            except HiveError as e:
                log.warning("ignored env secret %s: %s", name, e.message)
                continue
            try:
                hosts = parse_hosts(name, [hosts_spec[name]]) if name in hosts_spec else default_hosts(name)
            except HiveError as e:
                log.warning("HIVE_SECRET_HOSTS_%s ignored: %s", name, e.message)
                hosts = default_hosts(name)
            if len(self._imported) >= MAX_SECRETS:
                log.warning("ignored env secret %s: more than %d secrets", name, MAX_SECRETS)
                continue
            self._imported[name] = Secret(name=name, value=value, hosts=hosts, exec_ok=True, set_at=started,
                                          source="env-import")
        for name, spec in hosts_spec.items():
            if name in self.passthrough:
                try:
                    self._pass_hosts[name] = parse_hosts(name, [spec])
                except HiveError as e:
                    log.warning("HIVE_SECRET_HOSTS_%s ignored: %s", name, e.message)

    def _env_pop(self, key: str) -> None:
        try:
            del self._env[key]
        except (KeyError, TypeError):
            pass

    def _file_ok(self, st: os.stat_result) -> bool:
        if IS_WINDOWS:  # pragma: no cover - ACLs are checked by doctor (secure_file at write)
            return True
        mode = stat.S_IMODE(st.st_mode)
        if st.st_uid != os.getuid():
            log.error("%s is not owned by the node user; secrets NOT loaded (fix: chown, then chmod 600)", self.path)
            return False
        if mode & (stat.S_IWGRP | stat.S_IWOTH):
            log.error("%s is group/world-writable; secrets NOT loaded (fix: chmod 600)", self.path)
            return False
        if mode & 0o077:
            try:
                os.chmod(self.path, 0o600)
                log.warning("%s had mode %o; fixed to 600", self.path, mode)
            except OSError as e:
                log.warning("%s has mode %o and could not be fixed: %s", self.path, mode, e)
        return True

    def _reload(self, force: bool = False) -> None:
        """Re-read secrets.json when its mtime changed (one os.stat per call)."""
        try:
            st = os.stat(self.path)
            key: Optional[Tuple[float, int]] = (st.st_mtime, st.st_size)
        except OSError:
            st = None
            key = None
        with self._lock:
            if not force and key == self._mtime:
                return
            self._mtime = key
            store: Dict[str, Secret] = {}
            if st is not None and self._file_ok(st):
                try:
                    raw = read_json(self.path, default={}) or {}
                except HiveError as e:
                    log.error("secrets file unreadable: %s", e.message)
                    raw = {}
                for name, d in ((raw.get("secrets") or {}) if isinstance(raw, dict) else {}).items():
                    if not _valid_name(name) or not isinstance(d, dict) or not isinstance(d.get("value"), str):
                        continue
                    hosts = d.get("hosts")
                    store[name] = Secret(name=name, value=d["value"],
                                         hosts=tuple(hosts) if isinstance(hosts, list) else default_hosts(name),
                                         exec_ok=bool(d.get("exec", True)), set_at=str(d.get("set_at") or ""),
                                         source="store")
            self._store = store

    def _maybe_reload(self) -> None:
        now = time.monotonic()
        if now - self._last_check >= 1.0:
            self._last_check = now
            self._reload()

    # ---------------------------------------------------------------------------------------------- lookup
    def _passthrough(self, name: str) -> Optional[Secret]:
        if name not in self.passthrough:
            return None
        try:
            value = self._env.get(name)
        except Exception:  # noqa: BLE001
            value = None
        if not value:
            return None
        return Secret(name=name, value=value, hosts=self._pass_hosts.get(name, default_hosts(name)), exec_ok=True,
                      set_at="", source="passthrough")

    def _get(self, name: str) -> Optional[Secret]:
        with self._lock:
            s = self._store.get(name) or self._imported.get(name)
        return s or self._passthrough(name)

    def _all(self) -> List[Secret]:
        with self._lock:
            out: Dict[str, Secret] = dict(self._imported)
            out.update(self._store)
        for n in self.passthrough:
            if n not in out:
                p = self._passthrough(n)
                if p is not None:
                    out[n] = p
        return [out[k] for k in sorted(out)]

    def _missing(self, name: str) -> HiveError:
        return not_found(f"secret '{name}' is not set on node '{self.node_name}'",
                         hint=f"ask the owner to run on that machine: hive node secret set {name}", secret=name)

    def names(self) -> List[dict]:
        """``[{"name","hosts","exec","source","set_at"}]``; never values or lengths."""
        self._maybe_reload()
        return [{"name": s.name, "hosts": list(s.hosts), "exec": s.exec_ok, "source": s.source,
                 "set_at": s.set_at or None} for s in self._all()]

    def resolve(self, name: str, *, purpose: str, host: Optional[str] = None, scheme: str = "https") -> str:
        """Secret value for ``purpose`` "auth" (host-bound) or "exec" (needs the exec flag)."""
        if not _valid_name(name):
            raise self._missing(str(name))
        self._reload()
        s = self._get(name)
        if s is None:
            raise self._missing(name)
        if purpose == "exec":
            if not s.exec_ok:
                raise denied(f"secret '{name}' may not be passed to processes on this node", rule="secret_env",
                             hint=f"the owner set it with --no-exec; ask the owner (hive node secret set {name})",
                             secret=name)
        elif purpose == "auth":
            if host is not None:
                from .netutil import auth_host_allowed
                if not auth_host_allowed(s.hosts, host, scheme):
                    raise denied(f"secret '{name}' may not be sent to {host}", rule="secret_host",
                                 hint=self._host_hint(s), secret=name, hosts=list(s.hosts))
        else:
            raise bad_request(f"unknown secret purpose {purpose!r}")
        return s.value

    def _host_hint(self, s: Secret) -> str:
        bound = ", ".join(s.hosts) or "(none)"
        if s.source == "env-import":
            how = f"HIVE_SECRET_HOSTS_{s.name}=+<host>"
        else:
            how = f"hive node secret set {s.name} --hosts +<host>"
        return f"secret {s.name} may only be sent to {bound}; the owner can widen it: {how}"

    def resolve_auth(self, name: str) -> "AuthSpec":
        """AuthSpec(Authorization: Bearer <value>, the secret's hosts); per-hop host/TLS checks are the caller's."""
        from .netutil import AuthSpec
        value = self.resolve(name, purpose="auth")
        s = self._get(name)
        hosts = s.hosts if s is not None else ()
        return AuthSpec(name=name, header="Authorization", value="Bearer " + value, hosts=tuple(hosts))

    # ---------------------------------------------------------------------------------------------- masking
    def _maskable(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(sorted(((s.name, s.value) for s in self._all() if len(s.value) >= MIN_MASK_CHARS),
                            key=lambda nv: (-len(nv[1]), nv[0])))

    def _mask_state(self) -> Tuple[Tuple[str, str], ...]:
        """The maskable ``(name, value)`` pairs, longest value first, one entry per distinct value."""
        self._maybe_reload()
        key = self._maskable()
        with self._lock:
            if key != self._mask_key:
                self._mask_key = key
                seen: Dict[str, str] = {}
                for n, v in key:
                    seen.setdefault(v, n)
                self._mask_pairs = tuple((n, v) for v, n in seen.items())
            return self._mask_pairs

    def mask(self, text: str) -> str:
        """Replace every secret value (>= 8 chars) with ``«secret:NAME»``.

        Audit round 4 (DoS): occurrences are found with ``str.find`` per value (C speed) and overlapping
        occurrences are masked as one union, instead of one regex alternation of all values, which re-compared a
        long value at every position of a text repeating its prefix (a 1 MB text took ~50 s)."""
        if not isinstance(text, str) or not text:
            return text
        pairs = self._mask_state()
        if not pairs:
            return text
        spans: List[Tuple[int, int, str]] = []
        for name, value in pairs:
            n = len(value)
            start = text.find(value)
            while start >= 0:
                spans.append((start, -n, name))
                start = text.find(value, start + n)
        if not spans:
            return text
        spans.sort()
        out: List[str] = []
        pos = 0
        cur_s, cur_e, cur_n = spans[0][0], spans[0][0] - spans[0][1], spans[0][2]
        for st, neg, name in spans[1:]:
            if st < cur_e:
                cur_e = max(cur_e, st - neg)
                continue
            out.append(text[pos:cur_s])
            out.append(f"«secret:{cur_n}»")
            pos = cur_e
            cur_s, cur_e, cur_n = st, st - neg, name
        out.append(text[pos:cur_s])
        out.append(f"«secret:{cur_n}»")
        out.append(text[cur_e:])
        return "".join(out)

    def mask_edges(self, data: bytes, before: bytes, after: bytes) -> Tuple[bytes, bool]:
        """Mask secret occurrences that overlap ``data`` within ``before + data + after`` (range reads)."""
        self._mask_state()
        with self._lock:
            key = self._mask_key or ()
        if not key or not data:
            return data, False
        combined = before + data + after
        lo, hi = len(before), len(before) + len(data)
        spans: List[Tuple[int, int, str]] = []
        for name, value in key:
            vb = value.encode("utf-8")
            if len(vb) < MIN_MASK_CHARS:
                continue
            start = combined.find(vb)
            while start >= 0:
                end = start + len(vb)
                if end > lo and start < hi:
                    spans.append((max(start, lo) - lo, min(end, hi) - lo, name))
                start = combined.find(vb, start + 1)
        if not spans:
            return data, False
        spans.sort()
        merged: List[List] = []
        for s, e, n in spans:
            if merged and s <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], e)
            else:
                merged.append([s, e, n])
        out = bytearray()
        pos = 0
        for s, e, n in merged:
            out += data[pos:s]
            out += f"«secret:{n}»".encode("utf-8")
            pos = e
        out += data[pos:]
        return bytes(out), True

    @property
    def has_values(self) -> bool:
        """Any secret value is known (store, env import or passthrough present in env).

        Re-checks secrets.json first (one os.stat): NodeRuntime gates step-10a masking on this, so a secret the
        owner just set with the CLI must be masked from the very next call.
        """
        self._reload()
        return bool(self._all())

    @property
    def max_value_bytes(self) -> int:
        """Length in bytes of the longest maskable value (range readers read ``max_value_bytes - 1`` extra bytes)."""
        self._mask_state()
        with self._lock:
            key = self._mask_key or ()
        return max((len(v.encode("utf-8")) for _n, v in key), default=0)

    # ---------------------------------------------------------------------------------------------- CLI side
    def _lock_file(self) -> FileLock:
        return FileLock(str(self.path) + ".lock")

    def _locked(self, fn) -> None:  # type: ignore[no-untyped-def]
        lock = self._lock_file()
        deadline = time.monotonic() + 10
        while not lock.acquire(note="secret"):
            if time.monotonic() > deadline:
                raise HiveError("conflict", "secrets file is locked by another process; try again")
            time.sleep(0.05)
        try:
            fn()
        finally:
            lock.release()

    def _read_raw(self) -> dict:
        raw = read_json(self.path, default=None)
        if not isinstance(raw, dict):
            raw = {"version": 1, "secrets": {}}
        raw.setdefault("version", 1)
        if not isinstance(raw.get("secrets"), dict):
            raw["secrets"] = {}
        return raw

    def set(self, name: str, value: str, *, hosts: Optional[Sequence[str]] = None, exec_ok: bool = True) -> None:
        """Store a secret (CLI only; the value never goes on argv). ``hosts`` uses the ``--hosts`` syntax."""
        if not _valid_name(name):
            raise bad_request(f"invalid secret name {name!r}", hint="use ^[A-Z_][A-Z0-9_]*$ and not HIVE_*")
        _check_value(value)
        bound = parse_hosts(name, hosts)

        def apply() -> None:
            raw = self._read_raw()
            if name not in raw["secrets"] and len(raw["secrets"]) >= MAX_SECRETS:
                raise bad_request(f"at most {MAX_SECRETS} secrets per node")
            raw["secrets"][name] = {"value": value, "hosts": list(bound), "exec": bool(exec_ok),
                                    "set_at": iso_utc()}
            write_json_atomic(self.path, raw, private=True)
            secure_file(self.path, required=True)

        self._locked(apply)
        self._reload(force=True)

    def unset(self, name: str) -> bool:
        """Remove a stored secret; True when it existed."""
        found = [False]

        def apply() -> None:
            raw = self._read_raw()
            if name in raw["secrets"]:
                del raw["secrets"][name]
                found[0] = True
                write_json_atomic(self.path, raw, private=True)

        if self.path.exists():
            self._locked(apply)
        self._reload(force=True)
        return found[0]


__all__ = ["Secret", "DEFAULT_SECRET_HOSTS", "OTHER_SECRET_HOSTS", "SecretStore", "default_hosts", "parse_hosts"]
