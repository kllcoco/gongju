"""NodeRuntime (the single call pipeline / PEP), node routes, control.json, pause/resume/panic, run_node(),
supervise() (design §13, §5.10, D40, D55, WP1).

Every ingress (direct HTTP, hub calls, relay tasks) ends in ``NodeRuntime.call`` (§13.2, order binding): request id ->
catalog lookup -> ``check_action`` -> raw ``given`` + schema validation -> approval (direct agent tokens) ->
concurrency gate -> bound files verified and held -> deadline + handler -> result size -> secret masking -> audit ->
envelope. Managers (WP2/WP3) and the WSL keepalive (WP4) are late imports; anything unavailable becomes an
``_Unavailable`` stub. Exit codes (D55): 0 after a graceful stop, 75 after a requested restart.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import logging.handlers
import os
import platform
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

from . import __version__, catalog, schema
from .auth import Identity, TokenStore
from .errors import (HiveError, bad_request, conflict, internal, not_found, timeout as timeout_error, unavailable,
                     unsupported, too_large)
from .util import (FileLock, IS_WINDOWS, glob_match, iso_utc, new_id, pid_alive, process_create_time, secure_mkdir,
                   write_json_atomic)

if TYPE_CHECKING:  # pragma: no cover
    from .config import NodeConfig
    from .httpserver import ApiServer, Request

log = logging.getLogger("hive.node")

EXIT_STOP = 0       # D55: graceful stop
EXIT_RESTART = 75   # D55: requested restart
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}\Z")
GATE_WAIT = 5.0
GPU_CACHE_S = 60.0


class _Unavailable:
    """Stand-in for a manager that could not be loaded: every attribute raises ``unsupported``."""

    def __init__(self, what: str, reason: str = ""):
        self._what = what
        self._reason = reason

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise unsupported(f"{self._what} not available", hint=self._reason or None)

    def __bool__(self) -> bool:
        return False


def _platform_name() -> str:
    if IS_WINDOWS:
        return "windows"
    return "darwin" if sys.platform == "darwin" else "linux"


def _is_wsl() -> bool:
    try:
        from . import wsl
        return bool(wsl.is_wsl())
    except Exception:  # noqa: BLE001 - WP4 not merged: own check
        try:
            return "microsoft" in platform.release().lower() and not IS_WINDOWS
        except Exception:  # noqa: BLE001
            return False


def _json_size(obj: Any) -> int:
    return len(json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


class NodeRuntime:
    """The node runtime (§13.1)."""

    def __init__(self, config: "NodeConfig", *, config_path: Optional[Path] = None, start_managers: bool = True):
        self.config = config
        self.config_path = Path(config_path) if config_path else getattr(config, "_path", None)
        self.start_managers = start_managers
        self.name = config.name
        self.node_id = config.node_id or ""
        self.data_dir = Path(config.data_dir or ".").resolve()
        self.state_dir = self.data_dir / "state"
        self.workdir = Path(os.path.expanduser(config.workdir))
        self.started_at = time.time()
        self.self_pid = os.getpid()
        self.shutdown_event = threading.Event()
        self.exit_code = EXIT_STOP
        self.jobs: Any = _Unavailable("job manager")
        self.services: Any = _Unavailable("service manager")
        self.parts: Any = _Unavailable("part tracker")
        self.keepalive: Any = None
        self.policy: Any = None
        self.audit: Any = None
        self.approvals: Any = None
        self.secrets: Any = None
        self.holds: Any = None
        self.gate: Any = None
        self.tokens: TokenStore = TokenStore([e.to_dict() for e in config.auth.tokens], kind_default="agent")
        self.warnings: List[str] = []
        self.server: Any = None
        self.relay: Any = None
        self.last_call_at: Optional[float] = None
        self._lock = threading.Lock()
        self._inflight: Dict[str, Tuple[threading.Event, Identity]] = {}
        self._periodic: List[Dict[str, Any]] = []
        self._sched: Optional[threading.Thread] = None
        self._lockfile: Optional[FileLock] = None
        self._paused_cache: Tuple[float, bool] = (0.0, False)
        self._was_paused = False
        self._gpu_cache: Tuple[float, Optional[dict]] = (0.0, None)
        self._started = False
        self._spawn_mode: Optional[str] = None

    # ------------------------------------------------------------------------------------------- start / stop
    def start(self) -> None:
        """Start the runtime (lock, checks, secrets, policy, audit, approvals, handlers, managers, scheduler)."""
        from . import actions, config as cfgmod
        from .approvals import ApprovalStore, FileHolds
        from .audit import AuditLog, set_text_mask
        from .limits import ConcurrencyGate
        from .policy import Policy
        from .secretstore import SecretStore
        cfg = self.config
        for d in (self.data_dir, self.state_dir, self.data_dir / "audit", self.data_dir / "logs",
                  self.data_dir / "run"):
            secure_mkdir(d, required=(d == self.data_dir))
        lock = FileLock(self.data_dir / "node.lock")
        if not lock.acquire(note="node"):
            holder = lock.holder_pid()
            raise conflict(f"another node is running (pid {holder})" if holder else "another node is running",
                           hint="stop it first: hive node stop")
        self._lockfile = lock
        try:
            self.warnings += cfgmod.startup_safety_checks(cfg, "node")
            self.secrets = SecretStore(self.data_dir / "secrets.json", passthrough=cfg.secrets.env_passthrough)
            self.secrets.node_name = self.name
            set_text_mask(self.secrets.mask)
            hub_cfg = client_cfg = None
            paths = cfgmod.default_paths()
            try:
                if paths["hub_config"].exists():
                    hub_cfg = cfgmod.load_hub_config(str(paths["hub_config"]))
            except HiveError as e:
                log.warning("hub config not readable for protected paths: %s", e.message)
            try:
                client_cfg = cfgmod.load_client_config()
            except HiveError as e:
                log.warning("client config not readable for protected paths: %s", e.message)
            protected = cfgmod.protected_paths(cfg, hub_cfg, client_cfg)
            own_ports = [p for p in (cfg.listen.port, cfg.listen.proxy_port) if p] if cfg.listen.enabled else []
            if hub_cfg is not None:  # §5.4 rule 5a: the hub's own ports are denied to net.http too
                own_ports += [p for p in (hub_cfg.listen.port, hub_cfg.listen.proxy_port) if p and p not in own_ports]
            self.policy = Policy(cfg.policy, workdir=Path(cfg.workdir), data_dir=self.data_dir,
                                 hive_home=paths["home"], protected=protected, network=cfg.network,
                                 models=cfg.models, secrets=self.secrets, gateway=cfg.gateway, is_wsl=_is_wsl(),
                                 model_endpoints=cfg.model_endpoints, own_ports=own_ports)
            self.warnings += self.policy.warnings
            self.workdir = Path(self.policy._workdir)
            try:
                from . import proctree
                proctree.set_trusted_path(self.policy.trusted_path)
            except Exception as e:  # noqa: BLE001
                log.warning("trusted PATH not set: %s", e)
            if cfg.models.dir:
                key = self.policy._norm(cfg.models.dir)
                if not self.policy._in_rw_root(key):
                    self.warnings.append(f"models.dir {cfg.models.dir} is not inside a writable root; model.pull "
                                         "without path will fail")
            self.audit = AuditLog(self.data_dir / "audit", enabled=bool(cfg.audit.get("enabled", True)),
                                  retention_days=int(cfg.audit.get("retention_days", 30)),
                                  required=bool(cfg.audit.get("required", False)), mask=self.secrets.mask)
            appr = cfg.policy.approvals or {}
            self.approvals = ApprovalStore(self.data_dir / "approvals.json", ttl=int(appr.get("ttl", 600)),
                                           max_pending=int(appr.get("max_pending", 50)), where=f"node:{self.name}",
                                           decide_from=appr.get("decide_from") or ("loopback",),
                                           max_grant_minutes=int(appr.get("max_grant_minutes", 240)))
            if (self.data_dir / "PAUSED").exists():
                self.approvals.revoke(None)
                self._was_paused = True
            self.holds = FileHolds()
            self.gate = ConcurrencyGate(self.policy.limits.max_concurrent_calls)
            actions.load_all()
            if self.start_managers:
                self._start_managers()
            if IS_WINDOWS and cfg.wsl.keepalive:  # pragma: no cover - Windows only
                try:
                    from . import wsl
                    self.keepalive = wsl.Keepalive(self.state_dir)
                    self.keepalive.start(list(cfg.wsl.keepalive))
                    self.add_periodic(self.keepalive.check, 10, "wsl-keepalive")
                except Exception as e:  # noqa: BLE001
                    self.keepalive = None
                    self.warnings.append(f"WSL keepalive unavailable: {e}")
            self.add_periodic(self._check_control, 1, "control.json")
            self.add_periodic(self._watch_paused, 1, "PAUSED")
            self.add_periodic(self.audit.prune, 86400, "audit-prune")
            self.add_periodic(lambda: self._mgr_call(self.jobs, "prune"), 3600, "job-prune")
            self.add_periodic(lambda: self._mgr_call(self.parts, "prune"), 3600, "part-prune")
            self._sched = threading.Thread(target=self._scheduler, name="node-scheduler", daemon=True)
            self._sched.start()
            self._started = True
            self.audit.record(kind="lifecycle", identity=None, ip="", via="", node=self.name, action=None,
                              params=None, ok=True, code=None, ms=0, request_id="",
                              extra={"event": "start", "version": __version__, "pid": self.self_pid,
                                     "config_sha256": self._config_sha(), "policy": self.policy.summary()})
            for w in self.warnings:
                log.warning("%s", w)
        except BaseException:
            self._release_lock()
            raise

    def _config_sha(self) -> Optional[str]:
        import hashlib
        try:
            if self.config_path and Path(self.config_path).exists():
                return "sha256:" + hashlib.sha256(Path(self.config_path).read_bytes()).hexdigest()
        except OSError:
            pass
        return None

    def _start_managers(self) -> None:
        for attr, mod, cls, what in (("jobs", "jobs", "JobManager", "job manager"),
                                     ("services", "services", "ServiceManager", "service manager")):
            try:
                m = __import__(f"hive.{mod}", fromlist=[cls])
                inst = getattr(m, cls)(self)
                inst.start()
                setattr(self, attr, inst)
            except Exception as e:  # noqa: BLE001 - ImportError or a not-yet-merged WP: degrade
                log.error("%s not available: %s", what, e)
                setattr(self, attr, _Unavailable(what, str(e)))
        try:
            from .fsops import PartTracker
            self.parts = PartTracker(self.state_dir / "parts.json")
        except Exception as e:  # noqa: BLE001
            self.parts = _Unavailable("part tracker", str(e))

    @staticmethod
    def _mgr_call(mgr: Any, name: str) -> None:
        if isinstance(mgr, _Unavailable) or mgr is None:
            return
        fn = getattr(mgr, name, None)
        if callable(fn):
            try:
                fn()
            except HiveError as e:
                if e.code != "unsupported":
                    log.warning("%s failed: %s", name, e.message)

    def stop(self) -> None:
        """Cancel in-flight calls, wait <= 10 s, shut managers down (services keep running), release the lock."""
        if not self._started:
            self._release_lock()
            return
        self.shutdown_event.set()
        with self._lock:
            events = [ev for ev, _i in self._inflight.values()]
        for ev in events:
            ev.set()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with self._lock:
                if not self._inflight:
                    break
            time.sleep(0.05)
        for mgr in (self.services, self.jobs):
            if not isinstance(mgr, _Unavailable):
                try:
                    mgr.shutdown()
                except Exception as e:  # noqa: BLE001
                    log.warning("manager shutdown failed: %s", e)
        try:
            self.audit.record(kind="lifecycle", identity=None, ip="", via="", node=self.name, action=None,
                              params=None, ok=True, code=None, ms=0, request_id="",
                              extra={"event": "stop", "exit_code": self.exit_code})
        except Exception:  # noqa: BLE001
            pass
        self._started = False
        self._release_lock()

    def _release_lock(self) -> None:
        if self._lockfile is not None:
            self._lockfile.release()
            self._lockfile = None

    def request_stop(self, code: int = EXIT_STOP) -> None:
        """Ask the daemon to shut down gracefully with ``code`` (0 stop, 75 restart)."""
        self.exit_code = code
        self.shutdown_event.set()

    # ------------------------------------------------------------------------------------------- scheduler
    def add_periodic(self, fn: Callable[[], None], interval_s: float, name: str) -> None:
        """Register a periodic task (run by the single scheduler thread)."""
        with self._lock:
            self._periodic.append({"fn": fn, "interval": float(interval_s), "name": name,
                                   "next": time.monotonic() + (0 if interval_s <= 1 else float(interval_s))})

    def _scheduler(self) -> None:
        while not self.shutdown_event.wait(0.2):
            now = time.monotonic()
            with self._lock:
                due = [t for t in self._periodic if t["next"] <= now]
                for t in due:
                    t["next"] = now + t["interval"]
            for t in due:
                try:
                    t["fn"]()
                except Exception:  # noqa: BLE001
                    log.warning("periodic task %s failed", t["name"], exc_info=True)

    def _check_control(self) -> None:
        p = self.data_dir / "control.json"
        if not p.exists():
            return
        try:
            d = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            d = {}
        try:
            p.unlink()
        except OSError:
            pass
        cmd = d.get("cmd") if isinstance(d, dict) else None
        if cmd in ("stop", "restart"):
            log.info("control.json: %s requested", cmd)
            self.audit.record(kind="lifecycle", identity=None, ip="", via="", node=self.name, action=None,
                              params=None, ok=True, code=None, ms=0, request_id="", extra={"event": f"{cmd} requested"})
            self.request_stop(EXIT_RESTART if cmd == "restart" else EXIT_STOP)

    def _watch_paused(self) -> None:
        paused = (self.data_dir / "PAUSED").exists()
        self._paused_cache = (time.monotonic(), paused)
        if paused and not self._was_paused:
            n = self.approvals.revoke(None)
            self.audit.record(kind="lifecycle", identity=None, ip="", via="", node=self.name, action=None,
                              params=None, ok=True, code=None, ms=0, request_id="",
                              extra={"event": "paused", "grants_revoked": n})
        elif not paused and self._was_paused:
            self.audit.record(kind="lifecycle", identity=None, ip="", via="", node=self.name, action=None,
                              params=None, ok=True, code=None, ms=0, request_id="", extra={"event": "resumed"})
        self._was_paused = paused

    def is_paused(self) -> bool:
        """PAUSED file present (1 s cache)."""
        ts, val = self._paused_cache
        now = time.monotonic()
        if now - ts < 1.0:
            return val
        val = (self.data_dir / "PAUSED").exists()
        self._paused_cache = (now, val)
        return val

    # ------------------------------------------------------------------------------------------- policy
    def _services_view(self) -> Optional[Callable[[], dict]]:
        sm = self.services
        if isinstance(sm, _Unavailable) or sm is None:
            return None
        fn = getattr(sm, "policy_view", None)
        return fn if callable(fn) else None

    def effective(self, identity: Identity) -> Any:
        """The EffectivePolicy of ``identity`` on this node right now."""
        return self.policy.effective(identity, paused=self.is_paused(), approvals=self.approvals, node=self.name,
                                     services=self._services_view(), holds=self.holds)

    # ------------------------------------------------------------------------------------------- pipeline
    def call(self, identity: Identity, action: str, params: Optional[dict] = None, *,
             timeout: Optional[float] = None, request_id: Optional[str] = None, approval_id: Optional[str] = None,
             cancel: Optional[threading.Event] = None, via: str = "direct", ip: str = "", progress: Any = None,
             bound_files: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """The single PEP (§13.2); returns the full envelope and never raises."""
        t0 = time.monotonic()
        rid = request_id if isinstance(request_id, str) and REQUEST_ID_RE.match(request_id) else None
        ctx_via = identity.via if identity.via in ("hub", "relay") else via
        meta: Dict[str, Any] = {"request_id": rid or new_id("r_"), "node": self.name, "action": action,
                                "duration_ms": 0, "via": ctx_via, "api": 1}
        rid = meta["request_id"]
        audit_params: Optional[dict] = params if isinstance(params, dict) else None
        extra: Dict[str, Any] = {}
        hold: Optional[str] = None
        ctx = None
        gate_held = False
        ok = False
        err: Optional[HiveError] = None
        result: Any = None
        ev = cancel or threading.Event()
        try:
            if request_id is not None and not (isinstance(request_id, str) and REQUEST_ID_RE.match(request_id)):
                raise bad_request("invalid request_id", hint="use ^[A-Za-z0-9_.:-]{1,80}$")
            if params is not None and not isinstance(params, dict):
                raise bad_request("params must be an object")
            if bound_files is not None:
                if identity.kind not in ("hub", "relay"):
                    raise bad_request("bound_files is only accepted from a hub")
                if not isinstance(bound_files, dict) or not all(
                        isinstance(k, str) and (v is None or isinstance(v, str)) for k, v in bound_files.items()):
                    raise bad_request("bound_files must map paths to fingerprints or null")
            # 3. spec
            spec = catalog.get(action)
            if spec.scope == "hub":
                raise bad_request(f"hub action {action} sent to a node", hint="call it on the hub")
            if _platform_name() not in spec.platforms:
                raise unsupported(f"{action} is not available on {_platform_name()}")
            from .actions import HANDLERS
            fn = HANDLERS.get(action)
            if fn is None:
                raise unsupported(f"{action} is not available on node '{self.name}' (handler missing)",
                                  hint="this node's hive build does not implement it yet")
            # 4. authorization
            eff = self.effective(identity)
            eff.check_action(spec)
            # 5. given + validation
            given = frozenset(k for k, v in (params or {}).items() if v is not None)
            params = schema.validate(spec.params, params or {})
            audit_params = params
            # 7. approval (direct agent tokens only)
            files: Dict[str, Optional[str]] = {}
            if eff.needs_approval(spec):
                review = self._review(action, params, eff, given)
                files = self.approvals.require(identity, self.name, action, params, review, approval_id)
                extra["approval"] = approval_id
            elif eff.grant_id:
                extra["grant"] = eff.grant_id
            # 8. concurrency gate
            if not self.gate.acquire(GATE_WAIT):
                raise unavailable("node is busy (too many concurrent calls)", reason="busy", maybe_executed=False,
                                  hint="retry in a few seconds")
            gate_held = True
            # 8a. bound files: verify and hold in one step
            to_hold = dict(files or {})
            if bound_files:
                to_hold.update(bound_files)
            if to_hold:
                hold = self.holds.acquire(to_hold, policy=eff)
            # 9. deadline + handler
            from .actions import ActionContext
            deadline = catalog.deadline(spec, params, timeout, max_seconds=self.policy.limits.max_call_seconds)
            ctx = ActionContext(rt=self, identity=identity, policy=eff, spec=spec, request_id=rid,
                                deadline=time.monotonic() + deadline, cancel=ev, via=ctx_via, hold=hold, given=given,
                                progress=progress or (lambda done, total, msg: None))
            with self._lock:
                self._inflight[rid] = (ev, identity)
            try:
                result = fn(ctx, params)
            except HiveError:
                raise
            except (TimeoutError, socket.timeout):
                raise timeout_error(f"{action} timed out", maybe_executed=spec.mutating)
            except Exception as e:  # noqa: BLE001 - mapped once, here (§13.2 step 9)
                log.error("handler %s failed", action, exc_info=True)
                from .audit import redact_text
                raise internal(redact_text(self.secrets.mask(f"{type(e).__name__}: {e}"))[:500])
            # 10. result checks
            if not isinstance(result, dict):
                raise internal(f"{action} returned {type(result).__name__}, not an object")
            try:
                size = _json_size(result)
            except (TypeError, ValueError) as e:
                raise internal(f"{action} returned a result that is not JSON: {e}")
            if size > self.policy.limits.max_result_bytes:
                raise too_large(f"result of {action} is larger than {self.policy.limits.max_result_bytes} bytes",
                                hint="ask for less (paging, max_bytes, tail)")
            # 10a. masking
            if self.secrets is not None and self.secrets.has_values:
                result, masked = self._mask(result)
                if masked:
                    result["masked"] = True
                    meta["masked"] = True
            ok = True
            if ctx is not None:
                extra.update(ctx.audit_extra or {})
        except HiveError as e:
            err = e
        except Exception as e:  # noqa: BLE001 - defensive: call() never raises
            log.error("pipeline failure for %s", action, exc_info=True)
            err = internal(f"{type(e).__name__}")
        finally:
            with self._lock:
                self._inflight.pop(rid, None)
            if ctx is not None and ctx.hold:
                self.holds.release(ctx.hold)
            elif ctx is None and hold:
                self.holds.release(hold)
            if gate_held:
                self.gate.release()
        if err is not None and self.secrets is not None and self.secrets.has_values:
            err, err_masked = self._mask_error(err)
            if err_masked:
                meta["masked"] = True
        ms = int((time.monotonic() - t0) * 1000)
        meta["duration_ms"] = ms
        if identity.kind in ("agent", "hub", "relay"):
            self.last_call_at = time.time()
        try:
            self.audit.record(kind="call", identity=identity, ip=ip, via=ctx_via, node=self.name, action=action,
                              params=audit_params, ok=ok, code=None if ok else err.code if err else None, ms=ms,
                              request_id=rid, extra=extra or None)
        except HiveError as e:
            if ok:
                ok, err = False, e
        if ok:
            return {"ok": True, "result": result, "meta": meta}
        return {"ok": False, "error": err.to_dict() if err else internal("unknown").to_dict(), "meta": meta}

    def _review(self, action: str, params: dict, eff: Any, given: frozenset) -> dict:
        try:
            from . import presets
            caps = {}
            try:
                from . import sysinfo
                caps = sysinfo.capabilities() or {}
            except Exception:  # noqa: BLE001
                caps = {}
            ctx = presets.PresetCtx(os=_platform_name(), node_id=self.node_id, policy=eff,
                                    # WP2 CONTRACT-REQUEST: only exec-allowed secrets can be auto-added (vllm HF_TOKEN)
                                    secret_names=[n["name"] for n in self.secrets.names() if n.get("exec", True)],
                                    docker=bool(caps.get("docker")), mask=self.secrets.mask)
            service = None
            if action in ("service.start", "service.restart") and not isinstance(self.services, _Unavailable):
                try:
                    service = (self.services.status(params.get("name")) or {}).get("definition")
                except Exception:  # noqa: BLE001
                    service = None
            return presets.approval_review(action, params, ctx=ctx, given=given, service=service)
        except HiveError as e:
            if e.code in ("too_large", "conflict"):
                raise
        except Exception:  # noqa: BLE001
            log.warning("approval review unavailable; showing redacted params", exc_info=True)
        from .audit import redact_params
        from .util import escape_terminal
        lines = [f"action: {action}"] + [f"{k}: {json.dumps(v, ensure_ascii=False)}"
                                         for k, v in redact_params(action, params).items()]
        return {"action": action, "lines": [escape_terminal(x) for x in lines], "changes_what_runs": [], "files": []}

    def _mask_value(self, key: Optional[str], v: Any, flag: List[bool]) -> Any:
        if isinstance(v, str):
            if key in ("data", "body_base64"):
                try:
                    raw = base64.b64decode(v, validate=True)
                except (binascii.Error, ValueError):
                    raw = None
                if raw is not None and raw:
                    text = raw.decode("utf-8", "surrogateescape")
                    m = self.secrets.mask(text)
                    if m != text:
                        flag[0] = True
                        return base64.b64encode(m.encode("utf-8", "surrogateescape")).decode("ascii")
            m = self.secrets.mask(v)
            if m != v:
                flag[0] = True
            return m
        if isinstance(v, dict):
            return {k: self._mask_value(k, x, flag) for k, x in v.items()}
        if isinstance(v, list):
            return [self._mask_value(None, x, flag) for x in v]
        return v

    def _mask(self, result: dict) -> Tuple[dict, bool]:
        flag = [False]
        out = self._mask_value(None, result, flag)
        return out, flag[0]

    def _mask_error(self, err: HiveError) -> Tuple[HiveError, bool]:
        """(masked error, anything replaced) for step 10a; the flag sets ``meta.masked``."""
        flag = [False]
        msg = self._mask_value(None, err.message, flag)
        hint = self._mask_value(None, err.hint, flag) if err.hint else err.hint
        details = self._mask_value(None, err.details, flag)
        if not flag[0]:
            return err, False
        return HiveError(err.code, msg, details=details, hint=hint, status=getattr(err, "status", None)), True

    def cancel(self, request_id: str, identity: Identity) -> bool:
        """Cancel an in-flight call (own requests only; owner: any)."""
        with self._lock:
            item = self._inflight.get(request_id)
        if item is None:
            return False
        ev, owner = item
        if identity.kind != "owner" and (owner.name != identity.name or owner.token_sha256 != identity.token_sha256):
            return False
        ev.set()
        return True

    # ------------------------------------------------------------------------------------------- views
    def implemented(self) -> List[str]:
        """Names of the node-scope actions this node has handlers for (platform-filtered)."""
        from .actions import HANDLERS
        plat = _platform_name()
        return [n for n, s in catalog.SPECS.items() if s.scope == "node" and n in HANDLERS and plat in s.platforms]

    def actions_for(self, identity: Identity) -> List[dict]:
        """/v1/actions rows: public_dict + {"approval"} for the actions this identity may call."""
        eff = self.effective(identity)
        out = []
        for name in self.implemented():
            spec = catalog.SPECS[name]
            try:
                eff.check_action(spec)
            except HiveError:
                continue
            row = catalog.public_dict(spec)
            row["approval"] = bool(identity.kind == "agent" and identity.via == "direct"
                                   and glob_match(name, identity.require_approval))
            out.append(row)
        return out

    def info(self, identity: Identity) -> dict:
        """node.info result for ``identity``."""
        from .actions.node import node_info_result
        return node_info_result(self, identity, self.effective(identity))

    def gpu_info(self) -> Optional[dict]:
        """gpu.detect(processes=False), cached 60 s (None when unavailable)."""
        ts, val = self._gpu_cache
        if time.monotonic() - ts < GPU_CACHE_S and ts:
            return val
        try:
            from . import gpu
            val = gpu.detect(processes=False)
        except Exception:  # noqa: BLE001
            val = None
        self._gpu_cache = (time.monotonic(), val)
        return val

    def summary_row(self) -> dict:
        """The node's GET /v1/nodes NodeSummary row (§9.10)."""
        g = self.gpu_info() or {}
        gpus = [{"name": x.get("name"), "memory_total_mb": x.get("memory_total_mb")} for x in g.get("gpus") or []
                if isinstance(x, dict)]
        try:
            from . import sysinfo
            status = sysinfo.live_status() or {}
            summary = sysinfo.summary_line() or None
        except Exception:  # noqa: BLE001
            status, summary = {}, None
        is_wsl = _is_wsl()
        osname = platform.system()
        if is_wsl:
            label = "Linux (WSL2)"
        elif IS_WINDOWS:
            label = "Windows 11" if sys.getwindowsversion().build >= 22000 else "Windows 10"  # type: ignore[attr-defined]
        elif osname == "Darwin":
            label = "macOS"
        else:
            label = "Linux"
        idle = (time.time() - self.last_call_at) if self.last_call_at else None
        return {"name": self.name, "online": True, "mode": "self", "os": osname, "os_label": label,
                "arch": platform.machine(), "version": __version__, "gpus": gpus,
                "gpus_summary": summary if summary else None, "tags": list(self.config.tags),
                "description": self.config.description, "wsl": is_wsl,
                "wsl_distros": self._wsl_distros(), "secrets": [n["name"] for n in self.secrets.names()] if self.secrets else [],
                "last_seen": iso_utc(), "last_call_at": iso_utc(self.last_call_at) if self.last_call_at else None,
                "idle_s": round(idle, 1) if idle is not None else None,
                "inflight": self.gate.inflight if self.gate else 0, "queued": self.gate.queued if self.gate else 0,
                "identity_changed": False, "outdated": False, "preset": self.policy.preset if self.policy else None,
                "auto_update": bool(self.config.relay.auto_update),
                "max_timeout": self.policy.limits.exec_max_timeout if self.policy else None, "status": status}

    def _wsl_distros(self) -> List[str]:
        """§9.10 ``wsl_distros`` = node.info.wsl.distros or [] (same data as node.info; cached 30 s on Windows)."""
        if not IS_WINDOWS:
            return []
        now = time.monotonic()  # pragma: no cover - Windows only
        cached = getattr(self, "_wsl_distros_cache", None)
        if cached is not None and now - cached[0] < 30.0:
            return list(cached[1])
        try:
            from .actions.node import _wsl_block
            names = [str(n) for n in ((_wsl_block(self) or {}).get("distros") or []) if n]
        except Exception:  # noqa: BLE001
            names = []
        self._wsl_distros_cache = (now, names)
        return list(names)

    def keepalive_pids(self) -> Dict[int, str]:
        """{pid: "wsl-keepalive:<distro>"} ({} when none)."""
        if self.keepalive is None:
            return {}
        try:
            return dict(self.keepalive.pids())
        except Exception:  # noqa: BLE001
            return {}

    def managed_pids(self) -> Dict[int, str]:
        """jobs ∪ services ∪ keepalive pids."""
        out: Dict[int, str] = {}
        for mgr in (self.jobs, self.services):
            if isinstance(mgr, _Unavailable) or mgr is None:
                continue
            try:
                out.update(mgr.managed_pids() or {})
            except Exception:  # noqa: BLE001
                pass
        out.update(self.keepalive_pids())
        return out


# ---------------------------------------------------------------------------------------------------------------
# HTTP routes (§13.3)

def _identity_dict(ident: Identity) -> dict:
    return {"name": ident.name, "kind": ident.kind, "classes": sorted(ident.classes), "read_only": ident.read_only,
            "nodes": list(ident.nodes), "approver": ident.approver, "via": ident.via, "agent": ident.agent}


_CALL_KEYS = ("action", "params", "node", "timeout", "request_id", "approval_id", "bound_files")


def build_server(rt: NodeRuntime) -> "ApiServer":
    """The node's ApiServer with every node route registered (not started)."""
    from .httpserver import ApiServer
    from .limits import AuthThrottle
    cfg = rt.config
    th = cfg.auth.throttle or {}
    server = ApiServer(cfg.listen, role="node", tokens=lambda: rt.tokens,
                       throttle=AuthThrottle(int(th.get("max_failures", 10)), float(th.get("window", 60)),
                                             float(th.get("block", 300))),
                       logger=logging.getLogger("hive.httpserver"), audit=rt.audit,
                       owner_check=rt.approvals.may_decide)
    server.node_name = rt.name

    def own_node(node: Any) -> None:
        if node in (None, "", "self") or (isinstance(node, str) and node.lower() in (rt.name, rt.node_id.lower())):
            return
        raise not_found(f"this is node '{rt.name}', not {str(node)[:64]!r}", hint="omit node or use 'self'")

    def health(req: "Request") -> dict:
        return {"role": "node", "api": 1}

    def whoami(req: "Request") -> dict:
        ident = req.identity
        approvals = bool(ident.kind == "agent" and ident.via == "direct" and ident.require_approval)
        return {"identity": _identity_dict(ident),
                "server": {"role": "node", "name": rt.name, "version": __version__, "api": 1},
                "approvals": approvals}

    def actions(req: "Request") -> dict:
        own_node(req.query.get("node"))
        return {"node": rt.name, "catalog_hash": catalog.catalog_hash(), "implemented": rt.implemented(),
                "actions": rt.actions_for(req.identity)}

    def nodes(req: "Request") -> dict:
        return {"nodes": [rt.summary_row()]}

    def call(req: "Request") -> dict:
        body = req.json()
        if not isinstance(body, dict):
            raise bad_request("body must be a JSON object", hint='{"action": "...", "params": {...}}')
        unknown = [k for k in body if k not in _CALL_KEYS]
        if unknown:
            raise bad_request(f"unknown field(s) in call body: {', '.join(unknown[:5])}",
                              hint="fields: " + ", ".join(_CALL_KEYS))
        action = body.get("action")
        if not isinstance(action, str) or not action:
            raise bad_request("missing action")
        own_node(body.get("node"))
        timeout = body.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
            raise bad_request("timeout must be a number of seconds")
        if body.get("bound_files") is not None and req.identity.kind != "hub":
            raise bad_request("bound_files is only accepted from a hub")
        approval_id = body.get("approval_id")
        if approval_id is not None and not isinstance(approval_id, str):
            raise bad_request("approval_id must be a string")
        return rt.call(req.identity, action, body.get("params"), timeout=timeout, request_id=body.get("request_id"),
                       approval_id=approval_id, cancel=req.cancel, via=req.identity.via, ip=req.client_ip,
                       bound_files=body.get("bound_files"))

    def call_envelope(req: "Request") -> dict:
        try:
            return call(req)
        except HiveError as e:
            return {"ok": False, "error": e.to_dict(),
                    "meta": {"request_id": req.request_id, "node": rt.name, "action": None, "duration_ms": 0,
                             "via": req.identity.via if req.identity else "direct", "api": 1}}

    def cancel(req: "Request") -> dict:
        body = req.json() or {}
        rid = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(rid, str) or not REQUEST_ID_RE.match(rid):
            raise bad_request("request_id required")
        return {"cancelled": rt.cancel(rid, req.identity)}

    def approvals_list(req: "Request") -> dict:
        inc = str(req.query.get("include_done", "")).lower() in ("1", "true", "yes")
        return {"approvals": rt.approvals.list(include_done=inc)}

    def _decide(req: "Request", approve: bool) -> dict:
        body = req.json() or {}
        if not isinstance(body, dict):
            raise bad_request("body must be an object")
        extra = [k for k in body if k not in ("note", "grant_minutes")]
        if extra:
            raise bad_request(f"unknown field(s): {', '.join(extra)}")
        gm = body.get("grant_minutes")
        rec = rt.approvals.decide(req.query["id"], approve, req.identity, note=str(body.get("note") or ""),
                                  grant_minutes=gm)
        rt.audit.record(kind="approval", identity=req.identity, ip=req.client_ip, via="direct", node=rt.name,
                        action=rec.get("action"), params=None, ok=True, code=None, ms=0, request_id=req.request_id,
                        extra={"approval": rec["id"], "decision": rec["status"], "requester": rec.get("requester"),
                               "grant": (rec.get("grant") or {}).get("id")})
        return rec

    def grants(req: "Request") -> dict:
        return {"grants": rt.approvals.grants()}

    def revoke(req: "Request") -> dict:
        body = req.json() or {}
        if not isinstance(body, dict):
            raise bad_request("body must be an object")
        if body.get("all"):
            n = rt.approvals.revoke(None)
        elif isinstance(body.get("id"), str):
            n = rt.approvals.revoke(body["id"])
        else:
            raise bad_request("give id or all:true")
        rt.audit.record(kind="approval", identity=req.identity, ip=req.client_ip, via="direct", node=rt.name,
                        action=None, params=None, ok=True, code=None, ms=0, request_id=req.request_id,
                        extra={"revoked": n})
        return {"revoked": n}

    server.route("GET", "/v1/health", health, auth="none")
    server.route("GET", "/v1/whoami", whoami)
    server.route("GET", "/v1/actions", actions)
    server.route("GET", "/v1/nodes", nodes)
    server.route("POST", "/v1/call", call_envelope, mode="envelope")
    server.route("POST", "/v1/cancel", cancel)
    server.route("GET", "/v1/approvals", approvals_list, auth="owner")
    server.route("POST", "/v1/approvals/{id}/approve", lambda r: _decide(r, True), auth="owner")
    server.route("POST", "/v1/approvals/{id}/deny", lambda r: _decide(r, False), auth="owner")
    server.route("GET", "/v1/approvals/grants", grants, auth="owner")
    server.route("POST", "/v1/approvals/grants/revoke", revoke, auth="owner")
    try:
        from . import tools
        tools.register_agent_routes(server, lambda ident, req: tools.RuntimeTarget(rt, ident, req.client_ip),
                                    role="node")
    except Exception as e:  # noqa: BLE001 - optional (WP6)
        log.warning("agent tool routes not registered: %s", e)
    if cfg.gateway.enabled:
        try:
            from . import gateway
            gateway.register_gateway_routes(server, role="node", backend=gateway.NodeGatewayBackend(rt),
                                            cfg=cfg.gateway)
        except Exception as e:  # noqa: BLE001 - optional (WP5)
            log.warning("model gateway routes not registered: %s", e)
    return server


# ---------------------------------------------------------------------------------------------------------------
# kill switch (work whether or not the daemon runs)

def pause(data_dir: Path) -> None:
    """Write PAUSED (non-read calls denied within 1 s; the daemon revokes this node's grants)."""
    d = Path(data_dir)
    secure_mkdir(d)
    write_json_atomic(d / "PAUSED", {"at": iso_utc()}, private=True)


def resume(data_dir: Path, *, relay: bool = True) -> None:
    """Remove PAUSED; also remove relay.disabled unless ``relay=False``. Restarts nothing."""
    d = Path(data_dir)
    for name in ("PAUSED",) + (("relay.disabled",) if relay else ()):
        try:
            (d / name).unlink()
        except FileNotFoundError:
            pass


def panic(cfg: "NodeConfig", data_dir: Path, *, force_after: float = 10.0) -> dict:
    """PAUSED + relay.disabled + ``services.panic_offline`` (stop every job, service and labelled container)."""
    d = Path(data_dir)
    pause(d)
    write_json_atomic(d / "relay.disabled", {"at": iso_utc(), "by": "panic"}, private=True)
    try:
        from . import services
        out = services.panic_offline(d, cfg.node_id or "", force_after=force_after)
    except Exception as e:  # noqa: BLE001
        out = {"jobs": [], "services": [], "containers": [], "errors": [f"panic_offline failed: {e}"]}
    for k in ("jobs", "services", "containers", "errors"):
        out.setdefault(k, [])
    return out


# ---------------------------------------------------------------------------------------------------------------
# run_node / supervise

def _stdio_guard() -> None:
    if sys.stdout is None or sys.stderr is None:
        devnull = open(os.devnull, "w", encoding="utf-8")
        if sys.stdout is None:
            sys.stdout = devnull
        if sys.stderr is None:
            sys.stderr = devnull


def setup_logging(data_dir: Path, log_cfg: dict, name: str = "node") -> None:
    """Rotating ``logs/<name>.log`` (+ stderr when it is a terminal-like stream)."""
    root = logging.getLogger("hive")
    level = getattr(logging, str(log_cfg.get("level", "info")).upper(), logging.INFO)
    root.setLevel(level)
    path = Path(log_cfg.get("file") or f"logs/{name}.log")
    if not path.is_absolute():
        path = Path(data_dir) / path
    secure_mkdir(path.parent)
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) and getattr(h, "baseFilename", "") == str(path)
               for h in root.handlers):
        fh = logging.handlers.RotatingFileHandler(str(path), maxBytes=int(log_cfg.get("max_mb", 10)) << 20,
                                                  backupCount=int(log_cfg.get("keep", 5)), encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    if sys.stderr is not None and not any(type(h) is logging.StreamHandler for h in root.handlers):
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        sh.setLevel(logging.WARNING)
        root.addHandler(sh)


def banner(rt: NodeRuntime, bound: List[Tuple[str, int]]) -> List[str]:
    """The §5.10 startup banner lines."""
    try:
        import getpass
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "?"
    cfg = rt.config
    if bound:
        scheme = "https" if cfg.listen.tls.cert else "http"
        from .config import _is_cgnat_literal
        tailnet = cfg.listen.host == "tailscale" or _is_cgnat_literal(cfg.listen.host)  # explicit IP: verified at bind
        tls = "TLS" if cfg.listen.tls.cert else ("tailscale (WireGuard)" if tailnet else "plaintext")
        listen = ", ".join(f"{scheme}://{h}:{p}" for h, p in bound) + f" ({tls}, {len(rt.tokens)} tokens)"
    else:
        listen = "off"
    relay = cfg.relay.hub_url if cfg.relay.enabled else "off"
    return [f"Hive node '{rt.name}' ACTIVE — remote agents may control this computer as user '{user}' "
            f"(preset: {rt.policy.preset}).",
            f"Listen: {listen}  Relay: {relay}  Paused: {'yes' if rt.is_paused() else 'no'}  "
            f"Audit: {rt.data_dir / 'audit'}"]


def run_node(config_path: Optional[str] = None, *, foreground: bool = True, relaunched: bool = False,
             supervise: bool = False) -> int:
    """Run the node daemon; exit 0 after stop, 75 after a requested restart, non-zero on failure."""
    _stdio_guard()
    escaped = os.environ.pop("HIVE_SPAWN_ESCAPED", None) == "1"
    from . import config as cfgmod
    from . import util
    try:
        cfg = cfgmod.load_node_config(config_path)
    except HiveError as e:
        logging.getLogger("hive").error("config: %s%s", e.message, f" ({e.hint})" if e.hint else "")
        return 2
    data_dir = Path(cfg.data_dir or ".")
    if IS_WINDOWS and not relaunched:  # pragma: no cover - Windows only
        try:
            from . import winapi
            mode = winapi.spawn_mode()
        except Exception:  # noqa: BLE001
            mode = "free"
        if supervise or mode == "job":
            cp = Path(config_path or getattr(cfg, "_path", None) or cfgmod.default_paths()["node_config"])
            argv = util.launcher_argv("node", cp, windowless=True)
            if supervise:
                return globals()["supervise"]("node", argv, data_dir)
            return _escape_once(argv, data_dir)      # hand-started `hive node run` inside a job (v5 path)
    setup_logging(data_dir, cfg.log, "node")
    try:
        os.chdir(str(data_dir))
    except OSError as e:
        log.warning("cannot chdir to the data dir %s: %s", data_dir, e)
    rt = NodeRuntime(cfg, config_path=Path(config_path) if config_path else None)
    if escaped:
        rt._spawn_mode = "escaped"
    try:
        rt.start()
    except HiveError as e:
        log.error("node failed to start: %s%s", e.message, f" ({e.hint})" if e.hint else "")
        return 1
    server = None
    relay = None
    try:
        bound: List[Tuple[str, int]] = []
        if cfg.listen.enabled:
            server = build_server(rt)
            bound = server.start()
            rt.server = server
        if cfg.relay.enabled:
            try:
                from .relay import RelayAgent
                relay = RelayAgent(rt, cfg.relay)
                relay.start()
                rt.relay = relay
            except HiveError as e:
                log.error("relay not started: %s", e.message)
                relay = None
        for line in banner(rt, bound):
            log.warning("%s", line)
        _install_signals(rt)
        while not rt.shutdown_event.wait(0.5):
            pass
    except HiveError as e:
        log.error("node failed: %s%s", e.message, f" ({e.hint})" if e.hint else "")
        rt.exit_code = 1
    finally:
        if server is not None:
            server.stop()
        if relay is not None:
            try:
                relay.stop()
            except Exception:  # noqa: BLE001
                pass
        rt.stop()
    return rt.exit_code


def _install_signals(rt: NodeRuntime) -> None:
    if threading.current_thread() is not threading.main_thread():
        return

    def handler(signum: int, frame: Any) -> None:
        rt.request_stop(EXIT_STOP)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass
    if IS_WINDOWS:  # pragma: no cover - Windows only: console close / logoff / shutdown events
        try:
            import ctypes
            from ctypes import wintypes
            proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

            def ctrl(event: int) -> bool:
                if event in (2, 5, 6):          # CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
                    rt.request_stop(EXIT_STOP)
                    rt.shutdown_event.wait(5)
                    return True
                return False
            _install_signals._ctrl = proto(ctrl)  # type: ignore[attr-defined]  # keep a reference alive
            ctypes.windll.kernel32.SetConsoleCtrlHandler(_install_signals._ctrl, True)  # type: ignore
        except Exception:  # noqa: BLE001
            log.debug("console control handler not installed", exc_info=True)


# supervise() seams (patched by tests)
_sleep = time.sleep
_monotonic = time.monotonic
BACKOFF = (5, 10, 20, 40, 60)
UPTIME_RESET_S = 600


def _spawn_daemon(argv: List[str], data_dir: Path, mode: str) -> Tuple[Optional[subprocess.Popen], int,
                                                                         Optional[float]]:
    if IS_WINDOWS and mode == "job":  # pragma: no cover - Windows only
        from . import util, winapi
        env = dict(os.environ)
        env.update(util.self_env())
        env["HIVE_SPAWN_ESCAPED"] = "1"
        pid, ctime = winapi.relaunch_outside_job(argv, env)
        return None, pid, ctime
    kw: Dict[str, Any] = {"cwd": str(data_dir), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
                          "stderr": subprocess.DEVNULL, "close_fds": True}
    if IS_WINDOWS:  # pragma: no cover
        flags = 0x00000200 | 0x08000000
        if mode == "breakaway":
            flags |= 0x01000000
        kw["creationflags"] = flags
    else:
        kw["start_new_session"] = True
    from . import util
    env = dict(os.environ)
    env.update(util.self_env())
    kw["env"] = env
    p = subprocess.Popen(argv, **kw)
    return p, p.pid, process_create_time(p.pid)


def _wait_exit(popen: Optional[subprocess.Popen], pid: int, ctime: Optional[float],
               stop_flag: threading.Event) -> Optional[int]:
    if popen is not None:
        while True:
            try:
                return popen.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if stop_flag.is_set():
                    return None
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return _win_wait(pid, stop_flag)
    while pid_alive(pid, ctime):
        if stop_flag.wait(0.5):
            return None
    return _exit_code_of(pid)


def _exit_code_of(pid: int) -> Optional[int]:
    """Exit code of a process that is not our child (Windows only; POSIX cannot know it -> None)."""
    return _WIN_HANDLES.pop(pid, (None, None))[1]


_WIN_HANDLES: Dict[int, Tuple[Any, Optional[int]]] = {}


def _win_wait(pid: int, stop_flag: threading.Event) -> Optional[int]:  # pragma: no cover - Windows only
    """Wait for a non-child process on Windows and return its exit code (None when stopped or unknown)."""
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    h = k32.OpenProcess(0x1000 | 0x00100000, False, int(pid))   # QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if not h:
        return None
    try:
        while True:
            r = k32.WaitForSingleObject(h, 500)
            if r == 0:
                code = wintypes.DWORD(0)
                if k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return int(code.value)
                return None
            if stop_flag.is_set():
                return None
    finally:
        k32.CloseHandle(h)


def _escape_once(argv: List[str], data_dir: Path) -> int:  # pragma: no cover - Windows only
    """Job mode without --supervise: relaunch once through WMI, wait, return the node's exit code (D40)."""
    _, pid, ctime = _spawn_daemon(list(argv) + ["--relaunched"], data_dir, "job")
    code = _win_wait(pid, threading.Event())
    return 1 if code is None else code


def supervise(kind: str, argv: List[str], data_dir: Path) -> int:
    """The launcher loop (Windows Task Action, D55): 0 -> return 0, 75 -> relaunch at once, other -> backoff."""
    data_dir = Path(data_dir)
    run = data_dir / "run"
    secure_mkdir(run)
    marker = run / "launcher.json"
    me = os.getpid()
    write_json_atomic(marker, {"pid": me, "ctime": process_create_time(me)}, private=True)
    stop_flag = threading.Event()
    if threading.current_thread() is threading.main_thread():
        def on_signal(signum: int, frame: Any) -> None:
            stop_flag.set()
        for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
            sig = getattr(signal, name, None)
            if sig is not None:
                try:
                    signal.signal(sig, on_signal)
                except (OSError, ValueError):
                    pass
    try:
        from . import winapi
        mode = winapi.spawn_mode()
    except Exception:  # noqa: BLE001
        mode = "free"
    attempt = 0
    first = True
    child_argv = list(argv) + ["--relaunched"]
    try:
        while True:
            popen: Optional[subprocess.Popen] = None
            holder = FileLock(data_dir / f"{kind}.lock").holder() if first else None
            first = False
            started = _monotonic()
            if holder:
                pid, ctime = int(holder["pid"]), holder.get("ctime")
                log.info("adopting running %s daemon pid %s", kind, pid)
            else:
                try:
                    popen, pid, ctime = _spawn_daemon(child_argv, data_dir, mode)
                except Exception as e:  # noqa: BLE001
                    log.error("could not start the %s daemon: %s", kind, e)
                    pid, ctime, popen = -1, None, None
            code = _wait_exit(popen, pid, ctime, stop_flag) if pid > 0 else 1
            if stop_flag.is_set():
                write_json_atomic(data_dir / "control.json", {"cmd": "stop", "at": iso_utc()}, private=True)
                end = _monotonic() + 30
                while pid > 0 and pid_alive(pid, ctime) and _monotonic() < end:
                    _sleep(0.2)
                return 0
            if code == EXIT_STOP:
                return 0
            if code == EXIT_RESTART:
                attempt = 0
                continue
            if _monotonic() - started >= UPTIME_RESET_S:
                attempt = 0
            delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
            attempt += 1
            log.warning("%s daemon exited with %s; restarting in %s s", kind, code, delay)
            _sleep(delay)
    finally:
        try:
            marker.unlink()
        except OSError:
            pass


__all__ = ["EXIT_STOP", "EXIT_RESTART", "NodeRuntime", "build_server", "run_node", "supervise", "pause", "resume",
           "panic", "banner", "setup_logging"]
