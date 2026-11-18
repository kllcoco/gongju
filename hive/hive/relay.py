"""RelayAgent: the node side of the long-poll relay (design §7.5, §7.1–§7.3, D11, D54, WP5).

The node dials out: ``POST /v1/relay/register`` (with its ``instance_key``, D54), then ``/v1/relay/poll`` in a loop
(one poll thread) and ``/v1/relay/result`` for every finished task. Tasks run in a ``ThreadPoolExecutor(max_parallel)``
through ``NodeRuntime.call`` as ``relay_identity(runtime.config)`` (+ the informational ``agent`` and the
narrowing-only ``exec_gated`` / ``bound_files``), so the node stays the single policy enforcement point (D3).

Transport (§7.5, D34): ``relay.proxy`` ``"auto"`` follows the node ``network`` rules (loopback/private/tailnet hubs
direct), ``"off"`` direct, an explicit URL is used for every hub address (Tailscale userspace pods, §20.4 C). TLS and the
pin are checked end-to-end inside the tunnel (C3, ``transport.HttpTransport``); a token never travels over plain
``http://`` except to a loopback hub, or a private/tailnet hub with ``relay.allow_insecure`` (§5.9).
"""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import random
import secrets as _secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from . import __version__, catalog
from .errors import HiveError, bad_request, denied, too_large
from .transport import HttpTransport, plaintext_ok, resolve_host
from .util import iso_utc, is_loopback_host, secure_file

if TYPE_CHECKING:  # pragma: no cover
    from .config import RelayConfig
    from .node import NodeRuntime

log = logging.getLogger("hive.relay")

TASK_LRU = 4096
MAX_SLOTS = 16                  # §7.2: at most 16 tasks per poll
STATUS_EVERY = 30.0             # §7.2: status every 30 s
STATUS_MAX = 4096               # §7.2: status <= 4 KiB
INFO_MAX = 2 << 20              # §7.1: info + actions <= 2 MiB
RESULT_RETRIES = (1.0, 3.0, 9.0)
BACKOFF_MAX = 60.0
BACKOFF_CONFLICT = 60.0         # 409 on register: "online from another instance"
BACKOFF_AUTH = 300.0            # 401/403
DEFAULT_POLL_WAIT = 25.0
INSTANCE_KEY_FILE = "instance_key"


class NetworkTransport(HttpTransport):
    """HttpTransport whose ``auto`` proxy follows the node ``network`` block (§7.5) instead of the client rules."""

    def __init__(self, *args: Any, network: Optional[dict] = None, **kw: Any):
        super().__init__(*args, **kw)
        self._network = network

    def proxy_route(self) -> Tuple[Optional[str], Optional[List[str]]]:
        """(proxy or None, resolved IPs): explicit URL / off as HttpTransport; auto per the node network rules."""
        if self.proxy != "auto" or self._network is None:
            return super().proxy_route()
        from . import netproxy  # function-level (§11.4)
        settings = netproxy.load_proxy_settings(dict(self._network), os.environ)
        ips = resolve_host(self.host, self.port)
        if not (settings.https if self.scheme == "https" else settings.http):
            return None, ips
        return netproxy.proxy_for(settings, self.scheme, self.host, ips or ()), ips


def _network_dict(network: Any) -> Optional[dict]:
    if network is None:
        return None
    if isinstance(network, dict):
        return dict(network)
    if dataclasses.is_dataclass(network):
        d = dataclasses.asdict(network)
        return {k: d.get(k) for k in ("proxy", "no_proxy", "use_system_proxy", "ca_file", "strict_dns") if k in d}
    return None


def relay_token(cfg: Any) -> Optional[str]:
    """The relay token from ``relay.token`` or the first line of ``relay.token_file`` (never logged)."""
    tok = getattr(cfg, "token", None)
    if tok:
        return str(tok).strip()
    tf = getattr(cfg, "token_file", None)
    if tf:
        try:
            with open(os.path.expanduser(str(tf)), encoding="utf-8") as f:
                return (f.readline() or "").strip() or None
        except OSError as e:
            raise bad_request(f"cannot read relay.token_file: {e.strerror or e}")
    return None


def check_hub_url(cfg: Any) -> None:
    """§5.9: ``hub_url`` is https, or http to a loopback host, or to a private/tailnet host with allow_insecure."""
    url = getattr(cfg, "hub_url", None)
    if not url:
        raise bad_request("relay.hub_url is not set", hint="hive join (join code) or hive init node --relay URL")
    from urllib.parse import urlsplit
    u = urlsplit(str(url))
    if u.scheme == "https":
        return
    host = u.hostname or ""
    if u.scheme != "http" or not host:
        raise bad_request(f"invalid relay.hub_url {url!r}", hint="use https://HUB or http://<tailnet/LAN IP>:7700")
    if is_loopback_host(host):
        return
    if getattr(cfg, "allow_insecure", False):
        ips = resolve_host(host, u.port or 80)
        if plaintext_ok(host, ips):
            return
    raise denied(f"refusing a plaintext relay to {host}",
                 hint="use an https:// hub URL (Funnel/Caddy), or a tailnet/LAN hub with relay.allow_insecure",
                 rule="plaintext_credentials")


def relay_transport(cfg: Any, *, network: Any = None, token: Optional[str] = None,
                    timeout: float = 60.0) -> HttpTransport:
    """The relay transport to ``cfg.hub_url`` (same proxy and TLS pin/CA rules as the RelayAgent; ``_update`` too)."""
    check_hub_url(cfg)
    return NetworkTransport(str(cfg.hub_url), token, ca_file=getattr(cfg, "ca_file", None),
                           tls_pin=getattr(cfg, "tls_pin", None), timeout=timeout,
                           proxy=getattr(cfg, "proxy", None) or "auto", network=_network_dict(network))


def load_instance_key(data_dir: Path) -> str:
    """``<data>/instance_key`` (``ik_`` + token_urlsafe(32), 0600), generated at the first start (D54)."""
    p = Path(data_dir) / INSTANCE_KEY_FILE
    try:
        key = p.read_text(encoding="utf-8").strip()
        if key.startswith("ik_") and len(key) >= 20:
            secure_file(p, required=True)
            return key
    except FileNotFoundError:
        pass
    key = "ik_" + _secrets.token_urlsafe(32)
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key.encode("ascii"))
    finally:
        os.close(fd)
    os.replace(str(tmp), str(p))
    secure_file(p, required=True)
    return key


class _LRU:
    def __init__(self, maxlen: int):
        self.maxlen = maxlen
        self._d: Dict[str, None] = {}
        self._lock = threading.Lock()

    def add(self, key: str) -> bool:
        """True when new."""
        with self._lock:
            if key in self._d:
                return False
            self._d[key] = None
            while len(self._d) > self.maxlen:
                self._d.pop(next(iter(self._d)))
            return True


class RelayAgent:
    """Polls the hub for tasks and runs them as the relay identity (§7.5)."""

    def __init__(self, runtime: "NodeRuntime", cfg: "RelayConfig"):
        self.rt = runtime
        self.cfg = cfg
        self.token = relay_token(cfg)
        if not self.token:
            raise bad_request("relay.token is not set", hint="hive join (join code) or hive init node --relay URL "
                                                            "--relay-token-file F")
        self.hub_url = str(cfg.hub_url or "")
        self.max_parallel = max(1, int(getattr(cfg, "max_parallel", 8) or 8))
        self.transport = relay_transport(cfg, network=getattr(runtime.config, "network", None), token=self.token,
                                         timeout=60.0)
        self.instance_key = load_instance_key(Path(runtime.data_dir))
        self.session_id: Optional[str] = None
        self.poll_wait = DEFAULT_POLL_WAIT
        self.max_result_bytes = 33554432
        self.last_poll: Optional[float] = None
        self.failures = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._lock = threading.Lock()
        self._running: Dict[str, threading.Event] = {}
        self._seen = _LRU(TASK_LRU)
        self._last_status = 0.0
        self.restart_requested = False

    # ------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Start the poll thread and the task executor."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._pool = ThreadPoolExecutor(max_workers=self.max_parallel, thread_name_prefix="relay-task")
        self._thread = threading.Thread(target=self._loop, name="relay-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling; running tasks get their cancel events and the executor is shut down (no wait)."""
        self._stop.set()
        with self._lock:
            evs = list(self._running.values())
        for ev in evs:
            ev.set()
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001
            pass
        if self._pool is not None:
            self._pool.shutdown(wait=False)
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None

    @property
    def status(self) -> dict:
        """{"connected","session_id","last_poll","failures","hub_url"}."""
        lp = self.last_poll
        connected = bool(self.session_id and lp and time.time() - lp < self.poll_wait + 30)
        return {"connected": connected, "session_id": self.session_id,
                "last_poll": iso_utc(lp) if lp else None, "failures": self.failures, "hub_url": self.hub_url}

    # ------------------------------------------------------------------------------------------- loop
    def _disabled(self) -> bool:
        return (Path(self.rt.data_dir) / "relay.disabled").exists()

    def _sleep(self, seconds: float) -> None:
        self._stop.wait(max(0.0, seconds))

    def _backoff(self) -> float:
        base = min(BACKOFF_MAX, 2.0 ** max(0, self.failures - 1))
        return base * random.uniform(0.8, 1.2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._disabled():
                self.session_id = None
                self._sleep(1.0)
                continue
            try:
                if self.session_id is None:
                    self._register()
                self._poll_once()
                self.failures = 0
            except HiveError as e:
                if self._stop.is_set():
                    break
                self._on_error(e)
            except Exception:  # noqa: BLE001 - the poll thread must survive anything
                log.error("relay loop failure", exc_info=True)
                self.failures += 1
                self._sleep(self._backoff())

    def _on_error(self, e: HiveError) -> None:
        code = e.code
        if code == "conflict":
            if self.session_id is None:
                # 409 on register: the node is online from another instance (D54)
                log.error("relay register refused: %s", e.message)
                self.failures += 1
                self._sleep(BACKOFF_CONFLICT)
            else:
                log.info("relay session superseded; registering again")
                self.session_id = None
            return
        if code in ("unauthorized", "forbidden"):
            log.error("relay token rejected by %s: %s", self.hub_url, e.message)
            self.session_id = None
            self.failures += 1
            self._sleep(BACKOFF_AUTH)
            return
        self.failures += 1
        delay = self._backoff()
        if code == "unavailable" and (e.details or {}).get("reason") == "tls_pin":
            log.error("relay: %s", e.message)
        else:
            log.warning("relay: %s (retry in %.0f s)", e.message, delay)
        self._sleep(delay)

    # ------------------------------------------------------------------------------------------- register
    def _register_body(self) -> dict:
        from .auth import system_identity
        try:
            info = self.rt.info(system_identity())
        except Exception as e:  # noqa: BLE001
            log.warning("node.info for register failed: %s", e)
            info = {"name": self.rt.name, "node_id": self.rt.node_id}
        actions = self.rt.implemented()
        body = {"node": self.rt.name, "node_id": self.rt.node_id, "instance_key": self.instance_key, "api": 1,
                "version": __version__, "pid": os.getpid(), "info": info, "actions": actions,
                "catalog_hash": catalog.catalog_hash(), "max_parallel": self.max_parallel,
                "tags": list(getattr(self.rt.config, "tags", []) or [])}
        if len(json.dumps(info, default=str)) + len(json.dumps(actions)) > INFO_MAX:
            body["info"] = {"name": self.rt.name, "node_id": self.rt.node_id, "truncated": True}
        return body

    def _register(self) -> None:
        r = self.transport.request("POST", "/v1/relay/register", self._register_body(), timeout=60.0)
        if not isinstance(r, dict) or not isinstance(r.get("session_id"), str):
            raise HiveError("internal", "unexpected register answer from the hub")
        self.session_id = r["session_id"]
        try:
            self.poll_wait = max(1.0, min(120.0, float(r.get("poll_wait") or DEFAULT_POLL_WAIT)))
        except (TypeError, ValueError):
            self.poll_wait = DEFAULT_POLL_WAIT
        try:
            self.max_result_bytes = int(r.get("max_result_bytes") or self.max_result_bytes)
        except (TypeError, ValueError):
            pass
        self._last_status = 0.0
        log.info("relay registered with %s (session %s)", self.hub_url, self.session_id)

    # ------------------------------------------------------------------------------------------- poll
    def _status(self) -> Optional[dict]:
        now = time.monotonic()
        if now - self._last_status < STATUS_EVERY:
            return None
        self._last_status = now
        try:
            from . import sysinfo
            st = dict(sysinfo.live_status() or {})
        except Exception:  # noqa: BLE001
            st = {}
        try:
            if len(json.dumps(st, default=str)) > STATUS_MAX:
                st = {k: v for k, v in st.items() if not isinstance(v, (list, dict))}
        except (TypeError, ValueError):
            st = {}
        return st

    def _poll_once(self) -> None:
        with self._lock:
            running = list(self._running)
        free = max(0, min(MAX_SLOTS, self.max_parallel - len(running)))
        body: Dict[str, Any] = {"session_id": self.session_id, "running": running, "free_slots": free}
        st = self._status()
        if st is not None:
            body["status"] = st
        r = self.transport.request("POST", "/v1/relay/poll", body, timeout=self.poll_wait + 15)
        self.last_poll = time.time()
        if not isinstance(r, dict):
            raise HiveError("internal", "unexpected poll answer from the hub")
        for tid in r.get("cancel") or []:
            with self._lock:
                ev = self._running.get(str(tid))
            if ev is not None:
                ev.set()
        for task in r.get("tasks") or []:
            if isinstance(task, dict):
                self._accept(task)
        if r.get("refresh") == "restart":
            log.warning("relay: the hub asked this node to restart (hive hub restart-node)")
            self.restart_requested = True
            from .node import EXIT_RESTART
            self.rt.request_stop(EXIT_RESTART)
            self._stop.set()

    def _accept(self, task: dict) -> None:
        tid = task.get("task_id")
        if not isinstance(tid, str) or not tid:
            return
        if not self._seen.add(tid):
            return                           # duplicate: at most once
        try:
            ts = float(task.get("timeout_s") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return                           # §7.5: expired on arrival -> dropped
        ev = threading.Event()
        with self._lock:
            self._running[tid] = ev
        sid = self.session_id
        assert self._pool is not None
        try:
            self._pool.submit(self._run_task, task, ts, ev, sid)
        except RuntimeError:                 # executor shut down
            with self._lock:
                self._running.pop(tid, None)

    # ------------------------------------------------------------------------------------------- tasks
    def _identity(self, task: dict) -> Any:
        from .auth import relay_identity
        ident = relay_identity(self.rt.config)
        agent = task.get("agent")
        agent = agent if isinstance(agent, str) else ""
        from .httpserver import AGENT_HEADER_RE
        if agent and not AGENT_HEADER_RE.match(agent):
            agent = ""
        return dataclasses.replace(ident, agent=agent, exec_gated=bool(task.get("exec_gated")))

    def _run_task(self, task: dict, timeout_s: float, ev: threading.Event, sid: Optional[str]) -> None:
        tid = task["task_id"]
        try:
            ident = self._identity(task)
            action = str(task.get("action") or "")
            params = task.get("params") if isinstance(task.get("params"), dict) else {}
            bf = task.get("bound_files")
            env = self.rt.call(ident, action, params, timeout=timeout_s, request_id=task.get("request_id"),
                               cancel=ev, via="relay", ip="relay", bound_files=bf if isinstance(bf, dict) else None)
            env = self._hint_gateway(env, action, ident)
        except Exception as e:  # noqa: BLE001 - call() never raises; defensive
            log.error("relay task %s failed", tid, exc_info=True)
            env = {"ok": False, "error": HiveError("internal", type(e).__name__).to_dict(),
                   "meta": {"request_id": task.get("request_id"), "node": self.rt.name, "api": 1}}
        finally:
            with self._lock:
                self._running.pop(tid, None)
        self._post_result(sid, tid, env)

    def _hint_gateway(self, env: dict, action: str, ident: Any) -> dict:
        """v8 (§7.5): a narrowed relay role cannot serve the gateway; the forbidden error says so."""
        if action != "model.forward" or env.get("ok") or "net" in ident.classes:
            return env
        err = env.get("error") or {}
        if err.get("code") == "forbidden":
            role = getattr(self.rt.config.relay, "role", "operator")
            err = dict(err, hint=f"the relay role on this pod needs the net class for the gateway (role is {role})")
            env = dict(env, error=err)
        return env

    def _post_result(self, sid: Optional[str], tid: str, env: dict) -> None:
        try:
            size = len(json.dumps(env, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))
        except (TypeError, ValueError):
            size = self.max_result_bytes + 1
        if size > self.max_result_bytes:
            meta = env.get("meta") if isinstance(env.get("meta"), dict) else {}
            env = {"ok": False, "error": too_large(f"result larger than the relay limit ({self.max_result_bytes} "
                                                   "bytes)", hint="ask for less (paging, max_bytes, tail)").to_dict(),
                   "meta": meta}
        body = {"session_id": sid, "task_id": tid, "envelope": env}
        for i, delay in enumerate((0.0,) + RESULT_RETRIES):
            if delay:
                if self._stop.wait(delay):
                    break
            try:
                r = self.transport.request("POST", "/v1/relay/result", body, timeout=60.0)
                if isinstance(r, dict) and not r.get("accepted"):
                    log.info("relay result for %s not accepted (expired or superseded)", tid)
                return
            except HiveError as e:
                if e.code in ("unavailable", "timeout"):
                    continue
                log.warning("relay result for %s refused: %s", tid, e.message)
                return
        log.warning("relay result for %s could not be delivered", tid)


__all__ = ["RelayAgent", "NetworkTransport", "relay_transport", "relay_token", "check_hub_url", "load_instance_key"]
