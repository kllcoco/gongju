"""Shared test helpers with REAL fakes (design §21.1, Phase 0, lead-owned).

``make_ctx()`` uses ``FakeRuntime``/``FakeEffectivePolicy`` unless ``runtime=``/``policy=`` are given; WP1 tests patch
``FakeNetGuard`` in for ``hive.netutil.NetGuard`` (``patch("hive.netutil.NetGuard", FakeNetGuard)``) before a Policy
is built. ``make_runtime``/``start_node_server`` use the real WP1 NodeRuntime/ApiServer; ``start_hub`` needs WP5 and
raises ``unittest.SkipTest`` while the hub is a Phase 0 stub.
"""
from __future__ import annotations

import contextlib
import copy
import http.server
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
import weakref
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union
from urllib.parse import urlsplit

from hive import catalog, netproxy, util
from hive.actions import ActionContext
from hive.auth import Identity
from hive.errors import HiveError, bad_request, denied, not_found
from hive.netutil import AuthSpec, VettedTarget
from hive.policy import Limits

ALL_CLASSES = ("read", "write", "exec", "net", "persist")


# ---------------------------------------------------------------------------------------------------------------
# environment

@contextlib.contextmanager
def tmp_home() -> Iterator[Path]:
    """Temporary HIVE_HOME (restored afterwards); the path has no `.hive` component."""
    d = Path(tempfile.mkdtemp(prefix="hivetest-")).resolve()
    old = os.environ.get("HIVE_HOME")
    os.environ["HIVE_HOME"] = str(d)
    try:
        yield d
    finally:
        if old is None:
            os.environ.pop("HIVE_HOME", None)
        else:
            os.environ["HIVE_HOME"] = old
        shutil.rmtree(d, ignore_errors=True)


def py_cmd(code: str) -> List[str]:
    """``[sys.executable, "-c", code]`` (no shell differences)."""
    return [sys.executable, "-c", code]


def wait_until(fn: Callable[[], bool], timeout: float = 10, interval: float = 0.05) -> None:
    """Poll ``fn`` until truthy; AssertionError after ``timeout``."""
    end = time.monotonic() + timeout
    while True:
        if fn():
            return
        if time.monotonic() >= end:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(interval)


def free_port() -> int:
    """A free TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def fake_tool_on_path(name: str, script: str) -> Iterator[Path]:
    """Put an executable ``name`` on PATH (POSIX script, or ``name.cmd`` + .py on Windows); yields its directory.

    ``script`` is Python code unless it starts with ``#!`` (then it is written verbatim, POSIX only).
    """
    d = Path(tempfile.mkdtemp(prefix="hivetool-")).resolve()
    old_path = os.environ.get("PATH", "")
    try:
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            (d / f"{name}.py").write_text(script, encoding="utf-8")
            (d / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', encoding="utf-8")
        else:
            body = script if script.startswith("#!") else f"#!{sys.executable}\n{script}"
            p = d / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)
        os.environ["PATH"] = str(d) + os.pathsep + old_path
        yield d
    finally:
        os.environ["PATH"] = old_path
        shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------------------------------------------------------
# identities, configs, runtimes

def identity(classes: Iterable[str] = ALL_CLASSES, **kw: Any) -> Identity:
    """Test Identity (default: agent "test-agent" with all classes, via direct)."""
    kw.setdefault("name", "test-agent")
    kw.setdefault("kind", "agent")
    return Identity(classes=frozenset(classes), **kw)


def _deep_merge(base: dict, over: Mapping[str, Any]) -> dict:
    for k, v in over.items():
        if isinstance(v, Mapping) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def make_node_config(preset: str = "standard", **overrides: Any) -> dict:
    """A raw node config dict (§14.1 shape) for tests: listen on 127.0.0.1:0, temp-friendly; overrides deep-merge."""
    cfg: Dict[str, Any] = {
        "version": 1,
        "name": "test-node",
        "node_id": "n_test0000000000",
        "description": "test node",
        "tags": [],
        "consent": {"by": "test@host", "at": util.iso_utc(), "text": "test consent"},
        "allow_root": True,
        "workdir": "~",
        "data_dir": None,
        "listen": {"enabled": True, "host": "127.0.0.1", "port": 0, "proxy_port": None,
                   "tls": {"cert": None, "key": None}, "allow_insecure_remote": False, "allowed_hosts": [],
                   "allowed_origins": [], "trusted_proxies": [], "max_connections": 64,
                   "max_body_bytes": 50331648},
        "relay": {"enabled": False, "hub_url": None, "token": None, "token_file": None, "tls_pin": None,
                  "ca_file": None, "allow_insecure": False, "max_parallel": 8, "proxy": "auto",
                  "role": "operator", "deny_actions": [], "auto_update": False},
        "auth": {"tokens": [], "throttle": {"max_failures": 10, "window": 60, "block": 300}},
        "gateway": {"enabled": False, "cors": True, "max_body_bytes": 8388608, "max_response_bytes": 67108864,
                    "timeout": 1800, "idle_timeout": 300, "max_inflight_per_token": 4},
        "network": {"proxy": "off", "no_proxy": [], "use_system_proxy": False, "proxy_children": True,
                    "strict_dns": False, "ca_file": None},
        "models": {"dir": None, "hf_endpoint": None, "modelscope_endpoint": "https://www.modelscope.cn",
                   "max_concurrent_pulls": 2},
        "secrets": {"env_passthrough": []},
        "wsl": {"keepalive": []},
        # only the preset: the loader fills the rest from the preset (§14.2), so make_runtime("readonly") is read-only
        "policy": {"preset": preset},
        "model_endpoints": [],
        "audit": {"enabled": True, "retention_days": 30, "required": False},
        "log": {"level": "info", "file": "logs/node.log", "max_mb": 10, "keep": 5},
    }
    return _deep_merge(cfg, overrides)


def _wp_missing(e: BaseException) -> bool:
    return isinstance(e, HiveError) and e.code == "unsupported"


def make_runtime(preset: str = "standard", *, start_managers: bool = True, **policy_overrides: Any) -> Any:
    """A started real NodeRuntime on a temp HIVE_HOME (SkipTest until WP1 merges)."""
    from hive import config, node
    home = Path(os.environ.get("HIVE_HOME") or tempfile.mkdtemp(prefix="hivetest-")).resolve()
    raw = make_node_config(preset, data_dir=str(home / "node"), policy=policy_overrides)
    path = home / "node" / "config.json"
    util.write_json_atomic(path, raw, private=True)
    try:
        cfg = config.load_node_config(str(path))
        rt = node.NodeRuntime(cfg, config_path=path, start_managers=start_managers)
        rt.start()
    except HiveError as e:
        if _wp_missing(e):
            raise unittest.SkipTest(f"NodeRuntime not available yet (WP1): {e.message}")
        raise
    return rt


def start_node_server(**cfg: Any) -> Tuple[str, str, Any]:
    """(url, agent token, runtime) on 127.0.0.1:0 with a real NodeRuntime + ApiServer (WP1 ``node.build_server``).

    ``preset=`` picks the policy preset, ``start_managers=`` is passed to NodeRuntime, ``agent_entry=`` overrides
    fields of the agent token entry (e.g. ``require_approval``); other keyword arguments deep-merge into the
    ``make_node_config`` dict. The owner token is ``rt.test_owner_token``; ``rt.test_stop()`` stops server and
    runtime.
    """
    from hive import config, node
    preset = cfg.pop("preset", "standard")
    start_managers = cfg.pop("start_managers", False)
    agent_entry = cfg.pop("agent_entry", {})
    home = Path(os.environ.get("HIVE_HOME") or tempfile.mkdtemp(prefix="hivetest-")).resolve()
    agent, owner = util.new_token(), util.new_token()
    entry = {"name": "test-agent", "sha256": util.hash_token(agent), "kind": "agent", "role": "operator"}
    entry.update(agent_entry)
    tokens = [{"name": "owner", "sha256": util.hash_token(owner), "kind": "owner"}, entry]
    raw = make_node_config(preset, data_dir=str(home / "node"), **cfg)
    raw["auth"]["tokens"] = tokens + list(raw["auth"].get("tokens") or [])
    path = home / "node" / "config.json"
    util.write_json_atomic(path, raw, private=True)
    try:
        conf = config.load_node_config(str(path))
        rt = node.NodeRuntime(conf, config_path=path, start_managers=start_managers)
        rt.start()
    except HiveError as e:
        if _wp_missing(e):
            raise unittest.SkipTest(f"node server not available yet (WP1): {e.message}")
        raise
    server = node.build_server(rt)
    bound = server.start()
    rt.server = server
    rt.test_owner_token = owner

    def stop() -> None:
        server.stop()
        rt.stop()
    rt.test_stop = stop
    return f"http://127.0.0.1:{bound[0][1]}", agent, rt


def start_hub(nodes: Sequence[Any] = (), **cfg: Any) -> Tuple[str, Dict[str, str], Any]:
    """(url, tokens, hub): a real Hub + its ApiServer on 127.0.0.1:0 (WP5b ``hub.build_server``).

    ``nodes`` are raw hub ``nodes[]`` entries (dicts, §14.3). Tokens: ``"owner"`` (kind owner) and ``"agent"`` (agent
    ``test-agent``, operator; ``agent_entry=`` overrides fields such as ``require_approval``/``role``/``nodes``);
    ``extra_tokens=[(key, entry_without_sha256), …]`` adds more (the plaintext is returned under ``key``). Other
    keyword arguments deep-merge into the hub config. ``hub.test_stop()`` stops server and hub.
    """
    from hive import config, hub as hubmod
    agent_entry = cfg.pop("agent_entry", {})
    extra = cfg.pop("extra_tokens", [])
    home = Path(tempfile.mkdtemp(prefix="hivehub-")).resolve()
    owner, agent = util.new_token(), util.new_token()
    toks = {"owner": owner, "agent": agent}
    entries = [{"name": "owner", "sha256": util.hash_token(owner), "kind": "owner"},
               dict({"name": "test-agent", "sha256": util.hash_token(agent), "kind": "agent", "role": "operator"},
                    **agent_entry)]
    for key, entry in extra:
        t = util.new_token()
        toks[key] = t
        entries.append(dict(entry, sha256=util.hash_token(t)))
    raw: Dict[str, Any] = {
        "version": 1, "name": "test-hub", "consent": {"by": "test@host", "at": util.iso_utc(), "text": "test"},
        "allow_root": True, "data_dir": str(home / "hub"),
        "listen": {"host": "127.0.0.1", "port": 0}, "network": {"proxy": "off", "use_system_proxy": False},
        "tokens": entries, "nodes": [dict(n) for n in nodes],
    }
    raw = _deep_merge(raw, cfg)
    path = home / "hub" / "config.json"
    util.write_json_atomic(path, raw, private=True)
    conf = config.load_hub_config(str(path))
    h = hubmod.Hub(conf, config_path=path)
    h.start()
    server = hubmod.build_server(h)
    bound = server.start()
    h.server = server

    def stop() -> None:
        server.stop()
        h.stop()
        shutil.rmtree(str(home), ignore_errors=True)
    h.test_stop = stop
    return f"http://127.0.0.1:{bound[0][1]}", toks, h


# ---------------------------------------------------------------------------------------------------------------
# fakes

class FakeSecrets:
    """Empty SecretStore stand-in; ``set`` stores values so masking/resolution can be tested."""

    def __init__(self) -> None:
        self._s: Dict[str, Dict[str, Any]] = {}

    def names(self) -> List[dict]:
        return [{"name": n, "hosts": list(d["hosts"]), "exec": d["exec"], "source": "store", "set_at": d["set_at"]}
                for n, d in sorted(self._s.items())]

    def set(self, name: str, value: str, *, hosts: Optional[Sequence[str]] = None, exec_ok: bool = True) -> None:
        from hive.secretstore import DEFAULT_SECRET_HOSTS, OTHER_SECRET_HOSTS
        self._s[name] = {"value": value, "hosts": tuple(hosts) if hosts is not None else
                         DEFAULT_SECRET_HOSTS.get(name, OTHER_SECRET_HOSTS), "exec": exec_ok,
                         "set_at": util.iso_utc()}

    def unset(self, name: str) -> bool:
        return self._s.pop(name, None) is not None

    def _get(self, name: str) -> Dict[str, Any]:
        if name not in self._s:
            raise not_found(f"secret '{name}' is not set on node 'test-node'",
                            hint=f"ask the owner to run on that machine: hive node secret set {name}", secret=name)
        return self._s[name]

    def resolve(self, name: str, *, purpose: str, host: Optional[str] = None, scheme: str = "https") -> str:
        from hive.netutil import auth_host_allowed
        d = self._get(name)
        if purpose == "exec" and not d["exec"]:
            raise denied(f"secret '{name}' may not be passed to processes", rule="secret_env")
        if purpose == "auth" and host is not None and not auth_host_allowed(d["hosts"], host, scheme):
            raise denied(f"secret '{name}' may not be sent to {host}", rule="secret_host")
        return d["value"]

    def resolve_auth(self, name: str) -> AuthSpec:
        d = self._get(name)
        return AuthSpec(name=name, header="Authorization", value="Bearer " + d["value"], hosts=tuple(d["hosts"]))

    @property
    def has_values(self) -> bool:
        return bool(self._s)

    def mask(self, text: str) -> str:
        for n, d in sorted(self._s.items(), key=lambda kv: -len(kv[1]["value"])):
            if len(d["value"]) >= 8:
                text = text.replace(d["value"], f"«secret:{n}»")
        return text

    def mask_edges(self, data: bytes, before: bytes, after: bytes) -> Tuple[bytes, bool]:
        new = self.mask(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
        return new, new != data


class _NoAudit:
    def record(self, **kw: Any) -> None:
        return None

    def tail(self, *a: Any, **kw: Any) -> List[dict]:
        return []

    def prune(self) -> None:
        return None


class FakeRuntime:
    """Minimal NodeRuntime stand-in: name, node_id, dirs, no-op audit, empty secrets; jobs/services/parts settable."""

    def __init__(self, *, base: Optional[Path] = None, name: str = "test-node", node_id: str = "n_test0000000000",
                 workdir: Optional[Path] = None):
        self._owned = base is None
        self.base = Path(base) if base is not None else Path(tempfile.mkdtemp(prefix="hiveft-")).resolve()
        # an owned temp dir is removed when the runtime is garbage-collected (e.g. the make_ctx() context goes away)
        # or at interpreter exit, so handler tests that never call cleanup() leave no hiveft-* dirs behind
        self._finalizer = weakref.finalize(self, shutil.rmtree, str(self.base), True) if self._owned else None
        self.name = name
        self.node_id = node_id
        self.data_dir = self.base / "data"
        self.state_dir = self.data_dir / "state"
        self.workdir = Path(workdir) if workdir is not None else self.base / "work"
        for d in (self.data_dir, self.state_dir, self.workdir):
            d.mkdir(parents=True, exist_ok=True)
        self.audit = _NoAudit()
        self.secrets = FakeSecrets()
        self.jobs: Any = None
        self.services: Any = None
        self.parts: Any = None
        self.policy: Any = None
        self.holds: Any = None
        self.started_at = time.time()
        self.self_pid = os.getpid()

    def managed_pids(self) -> Dict[int, str]:
        return {}

    def keepalive_pids(self) -> Dict[int, str]:
        return {}

    def cleanup(self) -> None:
        """Remove the owned temp dir now (idempotent; otherwise it goes when the runtime is collected)."""
        if self._finalizer is not None:
            self._finalizer()


def _parse_url(url: str) -> Tuple[str, str, int, str]:
    try:
        p = urlsplit(url)
        port = p.port
    except ValueError:
        raise bad_request(f"invalid URL: {url!r}")
    scheme = (p.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise bad_request(f"unsupported URL scheme in {url!r}", hint="use http:// or https://")
    if p.username or p.password:
        raise bad_request("credentials in URLs are not allowed; use auth_env")
    host = (p.hostname or "").lower()
    if not host:
        raise bad_request(f"URL without host: {url!r}")
    path = p.path or "/"
    if p.query:
        path += "?" + p.query
    return scheme, host, port or (443 if scheme == "https" else 80), path


def _is_ip(host: str) -> bool:
    try:
        util.ip_category(host)
        return True
    except HiveError:
        return False


class FakeNetGuard:
    """Specified stand-in for netutil.NetGuard (§21.1 v7): classifies, refuses metadata/linklocal/reserved (rule ssrf),
    allows the rest with route direct; records calls; to_dict/from_dict round trip."""

    DEFAULT_IP = "93.184.216.34"

    def __init__(self, resolve: Optional[Dict[str, List[str]]] = None, **netguard_kwargs: Any):
        self.kwargs = dict(netguard_kwargs)
        self.resolve = {k.lower(): list(v) for k, v in (resolve or {}).items()}
        self.calls: List[Tuple[str, str, str, bool]] = []

    def _ip_for(self, host: str) -> str:
        if host in self.resolve and self.resolve[host]:
            return self.resolve[host][0]
        if host == "localhost":
            return "127.0.0.1"
        return self.DEFAULT_IP

    def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> VettedTarget:
        self.calls.append((url, purpose, method, redirect))
        scheme, host, port, path = _parse_url(url)
        ip = host if _is_ip(host) else self._ip_for(host)
        cat = util.ip_category(ip)
        if cat in ("metadata", "linklocal", "reserved"):
            raise denied(f"target {host} ({ip}) is not allowed ({cat})", rule="ssrf", category=cat)
        return VettedTarget(scheme=scheme, host=host, port=port, path=path, ip=ip, route="direct", proxy=None,
                            category=cat)

    def to_dict(self) -> dict:
        return {"kwargs": copy.deepcopy(self.kwargs), "resolve": copy.deepcopy(self.resolve)}

    @classmethod
    def from_dict(cls, d: dict) -> "FakeNetGuard":
        return cls(resolve=d.get("resolve") or {}, **(d.get("kwargs") or {}))


class FakeEffectivePolicy:
    """Full EffectivePolicy surface with permissive behaviour (§21.1): resolve_path = realpath + hard denies only;
    checks are no-ops; child_env = os.environ − HIVE_* + overrides; check_url allows loopback+public (direct)."""

    def __init__(self, identity: Optional[Identity] = None, *, workdir: Optional[Path] = None,
                 data_dir: Optional[Path] = None, limits: Optional[Limits] = None,
                 resolve: Optional[Dict[str, List[str]]] = None):
        self.identity = identity or globals()["identity"]()
        self.limits = limits or Limits()
        self.exec_enabled = True
        self.exec_mode = "open"
        self.shells = ("auto", "bash", "sh", "zsh", "powershell", "pwsh", "cmd")
        self.model_endpoints: List[dict] = []
        self.node_path = os.environ.get("PATH", "")
        self._workdir = Path(workdir).resolve() if workdir is not None else Path.cwd()
        self._data_dir = Path(data_dir).resolve() if data_dir is not None else None
        self._resolve = {k.lower(): list(v) for k, v in (resolve or {}).items()}
        self.url_calls: List[Tuple[str, str, str, bool]] = []

    # authorization
    def check_action(self, spec: Any) -> None:
        return None

    def needs_approval(self, spec: Any) -> bool:
        return False

    # filesystem
    def _hard_denied(self, real: Path) -> bool:
        parts = [p.lower() for p in real.parts]
        if ".hive" in parts:
            return True
        home = os.environ.get("HIVE_HOME")
        bases = [Path(home).resolve()] if home else []
        if self._data_dir is not None:
            bases.append(self._data_dir)
        for b in bases:
            if real == b or b in real.parents:
                return True
        return False

    def resolve_path(self, path: str, *, write: bool = False, must_exist: bool = False,
                     subtree: bool = False, nofollow_last: bool = False) -> Path:
        if not isinstance(path, str) or not path or "\x00" in path or len(path) > 4096:
            raise bad_request(f"invalid path: {path!r}")
        p = Path(os.path.expanduser(path))
        if not p.is_absolute():
            p = self._workdir / p
        if nofollow_last and p.name not in ("", ".", ".."):
            real = Path(os.path.realpath(str(p.parent))) / p.name
        else:
            real = Path(os.path.realpath(str(p)))
        if self._hard_denied(real):
            raise denied(f"path '{real}' is internal to Hive", rule="hive_internal",
                         hint="Hive's own data cannot be read or changed through actions")
        if must_exist and not real.exists():
            raise not_found(f"path '{real}' does not exist")
        return real

    def denied_under(self, path: Path, *, write: bool = False, rebase: Optional[Path] = None) -> List[str]:
        return []

    def path_key(self, path: Union[str, Path], *, follow: bool = True) -> str:
        s = os.path.abspath(str(path))
        return os.path.normcase(os.path.realpath(s) if follow else os.path.normpath(s)).replace("\\", "/")

    def roots(self) -> List[dict]:
        return [{"path": "*", "mode": "rw"}]

    @property
    def workdir(self) -> Path:
        return self._workdir

    @property
    def write_exec_paths_denied(self) -> bool:
        return False

    # exec
    def check_shell(self, shell: str) -> None:
        return None

    def check_command(self, command: Union[str, List[str]], shell: str) -> None:
        return None

    def check_env(self, env: Mapping[str, str]) -> None:
        return None

    def check_cwd(self, cwd: Optional[str]) -> Path:
        if cwd is None:
            return self._workdir
        p = self.resolve_path(cwd, must_exist=True)
        if not p.is_dir():
            raise bad_request(f"cwd '{p}' is not a directory")
        return p

    def child_env(self, overrides: Dict[str, str], unset: Iterable[str] = (), gpus: Optional[str] = None,
                  secret_env: Sequence[str] = ()) -> Dict[str, str]:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HIVE_")}
        for k in unset:
            env.pop(k, None)
        if gpus is not None:
            if gpus == "all":
                for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"):
                    env.pop(k, None)
            else:
                v = "" if gpus == "none" else gpus
                for k in ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES"):
                    env[k] = v
        env.update(overrides or {})
        return env

    # net
    @property
    def net_guard(self) -> "FakeEffectivePolicy":
        return self

    def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> VettedTarget:
        self.url_calls.append((url, purpose, method, redirect))
        scheme, host, port, path = _parse_url(url)
        if _is_ip(host):
            ip = host
        elif host in self._resolve and self._resolve[host]:
            ip = self._resolve[host][0]
        elif host == "localhost":
            ip = "127.0.0.1"
        else:
            ip = FakeNetGuard.DEFAULT_IP
        cat = util.ip_category(ip)
        if cat not in ("loopback", "public"):
            raise denied(f"target {host} ({ip}) is not allowed ({cat})", rule="ssrf", category=cat)
        return VettedTarget(scheme=scheme, host=host, port=port, path=path, ip=ip, route="direct", proxy=None,
                            category=cat)

    def auth_spec(self, name: str) -> AuthSpec:
        raise not_found(f"secret '{name}' is not set on node 'test-node'",
                        hint=f"ask the owner to run on that machine: hive node secret set {name}", secret=name)

    @property
    def proxies(self) -> netproxy.ProxySettings:
        return netproxy.load_proxy_settings({"proxy": "off"})   # WP3b: the real netproxy reads env; fakes use none

    # process
    def check_kill(self, pid: int, info: dict, *, protected: Mapping[int, str]) -> None:
        return None

    def check_timeout(self, value: Optional[float], default: float, maximum: float) -> float:
        if value is None:
            return float(default)
        if value > maximum:
            raise bad_request(f"timeout {value} s is above the node limit",
                              hint=f"max timeout on this node is {maximum:g} s")
        return float(value)

    def summary(self) -> dict:
        return {"preset": "test", "read_only": False, "classes": sorted(self.identity.classes),
                "denied_actions": [], "approval_actions": [], "fs_roots": self.roots(),
                "exec": {"enabled": True, "mode": "open", "shells": list(self.shells),
                         "default_timeout": self.limits.exec_default_timeout,
                         "max_timeout": self.limits.exec_max_timeout},
                "net": {"http_allow": ["loopback", "public"], "download_allow": ["loopback", "public"]},
                "io_limits": {"max_read_bytes": self.limits.max_read_bytes,
                              "max_write_bytes": self.limits.max_write_bytes,
                              "max_chunk_bytes": self.limits.max_chunk_bytes,
                              "max_output_bytes": self.limits.max_output_bytes,
                              "max_response_bytes": self.limits.max_response_bytes},
                "limits": {"max_concurrent_calls": self.limits.max_concurrent_calls,
                           "max_call_seconds": self.limits.max_call_seconds,
                           "max_result_bytes": self.limits.max_result_bytes},
                "note": "test policy"}


def set_secret(runtime: Any, name: str, value: str, **kw: Any) -> None:
    """SecretStore.set (or FakeSecrets.set) for tests."""
    runtime.secrets.set(name, value, **kw)


def make_ctx(action: str, *, runtime: Any = None, identity: Optional[Identity] = None, policy: Any = None,
             timeout: float = 30) -> ActionContext:
    """ActionContext for calling a handler directly (FakeRuntime / FakeEffectivePolicy by default)."""
    spec = catalog.get(action)
    rt = runtime if runtime is not None else FakeRuntime()
    ident = identity if identity is not None else globals()["identity"]()
    pol = policy if policy is not None else FakeEffectivePolicy(ident, workdir=getattr(rt, "workdir", None),
                                                                data_dir=getattr(rt, "data_dir", None))
    return ActionContext(rt=rt, identity=ident, policy=pol, spec=spec, request_id=util.new_id("r_test"),
                         deadline=time.monotonic() + float(timeout), cancel=threading.Event(), via="direct")


# ---------------------------------------------------------------------------------------------------------------
# StubHTTPServer

Response = Tuple[int, Dict[str, str], Any]   # body: bytes | str | dict/list (JSON) | Iterable[bytes] (streamed)


def _parse_range(header: str, size: int) -> Union[None, str, Tuple[int, int]]:
    """RFC 9110 single byte range: ``bytes=a-b``, ``bytes=a-``, ``bytes=-n``. Returns (start, end) inclusive,
    ``"unsatisfiable"`` (-> 416), or None (malformed / multi-range -> ignored, full 200 response)."""
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    first, sep, last = spec.strip().partition("-")
    if not sep:
        return None
    first, last = first.strip(), last.strip()
    try:
        if first == "":                       # suffix range: the last n bytes
            n = int(last)
            if n <= 0:
                return "unsatisfiable"
            return (max(0, size - n), size - 1) if size > 0 else "unsatisfiable"
        start = int(first)
        end = int(last) if last else size - 1
    except ValueError:
        return None
    if start < 0 or (last and end < start):
        return None
    if start >= size:
        return "unsatisfiable"
    return start, min(end, size - 1)


class _StubHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stub"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        return None

    def _handle(self) -> None:
        stub: "StubHTTPServer" = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else b""
        path = self.path.split("?", 1)[0]
        req = {"method": self.command, "path": path, "full_path": self.path, "headers": dict(self.headers.items()),
               "body": body}
        stub.requests.append(req)
        route = stub.routes.get((self.command, path)) or stub.routes.get(("*", path))
        if route is None:
            resp: Response = (404, {"Content-Type": "application/json"}, {"error": "not found", "path": path})
        else:
            delay, target = route
            if delay:
                time.sleep(delay)
            resp = target(req) if callable(target) else target
        self._send(*resp, range_header=self.headers.get("Range"))

    def _send(self, status: int, headers: Dict[str, str], body: Any, range_header: Optional[str] = None) -> None:
        headers = dict(headers or {})
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
            headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            body = body.encode("utf-8")
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        if body is None:
            body = b""
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            if range_header and status == 200 and headers.pop("X-Stub-Range", None) == "1":
                rng = _parse_range(range_header, len(data))
                if rng == "unsatisfiable":
                    status, data = 416, b""
                    headers = {"Content-Range": f"bytes */{len(body)}"}
                elif rng is not None:
                    start, end = rng
                    headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                    data, status = data[start:end + 1], 206
            headers.pop("X-Stub-Range", None)
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)
            return
        # iterable of chunks: stream with Connection: close
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        for chunk in body:
            self.wfile.write(chunk if isinstance(chunk, bytes) else str(chunk).encode("utf-8"))
            self.wfile.flush()

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle


class StubHTTPServer:
    """Tiny threaded HTTP server with scripted routes (health, OpenAI stub incl. SSE, Range) on 127.0.0.1:0."""

    def __init__(self) -> None:
        self.routes: Dict[Tuple[str, str], Tuple[float, Any]] = {}
        self.requests: List[dict] = []
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # route scripting
    def add(self, method: str, path: str, status: int = 200, body: Any = b"",
            headers: Optional[Dict[str, str]] = None, delay: float = 0.0) -> "StubHTTPServer":
        """Static response (method "*" matches any)."""
        self.routes[(method.upper(), path)] = (delay, (status, dict(headers or {}), body))
        return self

    def add_handler(self, method: str, path: str, fn: Callable[[dict], Response], delay: float = 0.0
                    ) -> "StubHTTPServer":
        """Dynamic response: ``fn(request_dict) -> (status, headers, body)``."""
        self.routes[(method.upper(), path)] = (delay, fn)
        return self

    def add_file(self, path: str, data: bytes, headers: Optional[Dict[str, str]] = None) -> "StubHTTPServer":
        """GET/HEAD a file with Range support: ``bytes=a-b``, ``bytes=a-``, ``bytes=-n`` -> 206 with Content-Range;
        a start at or past the end -> 416 with ``Content-Range: bytes */len``; malformed/multi-range -> full 200."""
        h = dict(headers or {}, **{"X-Stub-Range": "1", "Content-Type": "application/octet-stream"})
        self.routes[("GET", path)] = (0.0, lambda req: (200, dict(h), data))
        self.routes[("HEAD", path)] = (0.0, lambda req: (200, dict(h), data))
        return self

    def add_sse(self, path: str, events: Sequence[Any], method: str = "POST") -> "StubHTTPServer":
        """Server-sent events, one chunk per event (dict -> ``data: <json>``), then ``data: [DONE]``."""
        def gen() -> Iterator[bytes]:
            for ev in events:
                payload = json.dumps(ev) if not isinstance(ev, str) else ev
                yield f"data: {payload}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"
        self.routes[(method.upper(), path)] = (0.0, lambda req: (200, {"Content-Type": "text/event-stream"}, gen()))
        return self

    def openai(self, models: Sequence[str] = ("stub-model",), reply: str = "hello from stub",
               base: str = "/v1") -> "StubHTTPServer":
        """Scripted OpenAI-compatible server: /health, {base}/models, {base}/chat/completions (stream or not)."""
        self.add("GET", "/health", 200, {"status": "ok"})
        self.add("GET", f"{base}/models",
                 200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "stub"} for m in models]})

        def chat(req: dict) -> Response:
            try:
                body = json.loads(req["body"] or b"{}")
            except ValueError:
                return 400, {}, {"error": {"message": "bad json"}}
            model = body.get("model") or models[0]
            if body.get("stream"):
                chunks = [{"id": "c1", "object": "chat.completion.chunk", "model": model,
                           "choices": [{"index": 0, "delta": {"content": w}, "finish_reason": None}]}
                          for w in reply.split(" ")]

                def gen() -> Iterator[bytes]:
                    for c in chunks:
                        yield f"data: {json.dumps(c)}\n\n".encode("utf-8")
                    yield b"data: [DONE]\n\n"
                return 200, {"Content-Type": "text/event-stream"}, gen()
            return 200, {}, {"id": "c1", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": reply},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": len(reply.split())}}
        self.add_handler("POST", f"{base}/chat/completions", chat)
        return self

    # lifecycle
    def start(self) -> "StubHTTPServer":
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
        httpd.daemon_threads = True
        httpd.stub = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="stub-http", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    @property
    def port(self) -> int:
        assert self._httpd is not None, "server not started"
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "StubHTTPServer":
        return self.start() if self._httpd is None else self

    def __exit__(self, *exc: Any) -> None:
        self.stop()


__all__ = ["tmp_home", "make_node_config", "make_runtime", "start_node_server", "start_hub", "identity", "make_ctx",
           "py_cmd", "wait_until", "free_port", "StubHTTPServer", "fake_tool_on_path", "set_secret",
           "FakeEffectivePolicy", "FakeNetGuard", "FakeRuntime", "FakeSecrets", "ALL_CLASSES"]
