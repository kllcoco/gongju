"""ServiceManager + supervisor + health + container rules + panic_offline (design §10.5, §9.6, WP2).

Persistence: definitions in ``<data>/state/services.json`` ``{"v":1,"services":{name: definition}}`` (0600; env
values stored, redacted on output; ``defined_by``), runtime in ``<data>/state/services/<name>/runtime.json``, shim run
dir ``<data>/state/services/<name>/run/`` (logs persist across restarts and rotate).
Policy is re-checked at every spawn: manual starts with the calling identity; autostart, supervisor restarts and
starts after adoption with the system identity ``node:autostart``. Nothing is spawned while paused or when the node
policy disables execution. Threads: one supervisor (1 s tick) and a 4-worker health pool (health checks, ready_pattern
matching, docker inspect/cleanup and system spawns). Lock rule (§16): the manager lock is never held across
subprocess, network or heavy file I/O; per-service op locks serialize spawn/stop transitions.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from . import presets, proctree, textlog, util
from .errors import HiveError, bad_request, cancelled, conflict, denied, not_found, unsupported
from .jobs import is_wsl_exe, keep_awake_warnings, read_log

if TYPE_CHECKING:  # pragma: no cover
    from .actions import ActionContext
    from .netutil import AuthSpec, NetGuard
    from .node import NodeRuntime

log = logging.getLogger("hive.services")

STATES = ("stopped", "starting", "running", "unhealthy", "stopping", "backoff", "failed")
ALIVE = ("starting", "running", "unhealthy", "stopping")
TICK = 1.0
NO_HEALTH_ALIVE_S = 3.0
SHORT_RUN_S = 60.0
MAX_BACKOFF_S = 300.0
STOP_EXTRA_S = 7.0
DOCKER_RECHECK_S = 15.0
DOCKER_WAIT_MAX_S = 600.0
CONTAINER_CACHE_S = 10.0
AUTOSTART_STAGGER_S = 2.0
STATUS_TAIL_UNHEALTHY = 40
PAUSED_ERROR = "stopped while paused (panic or crash); start it again with service_control"
REBOOT_ERROR = "not restarted after reboot (autostart=false); use service_control start, or set autostart=true"
# v8.1 (external review): a crash restart, autostart or re-hold found an approved file changed (the run state keeps the
# approved {path: fingerprint} of the last approval that bound files for this service)
APPROVED_CHANGED_ERROR = "approved file changed; approve a start again"
EXEC_DISABLED_ERROR = "exec disabled by node policy"
DOCKER_WAIT_ERROR = "waiting for the Docker daemon (start Docker Desktop)"
WSL_ERROR = ("services cannot be supervised across wsl.exe (no Linux pid, health, GPU attribution or adoption after "
             "restart)")
WSL_HINT = "run a Hive node inside WSL (guide('windows')); nodes_list shows it, e.g. desk-wsl"
SECRET_ENV_WARNING = "env {k} looks secret; prefer secret_env (values in env are stored in services.json)"
CODE_DIR_WARNING = "service code under {d} can be changed by file-writing agents; give the service its own folder"

# seams (tests patch these)
_mono = time.monotonic


def _wall() -> float:
    return time.time()


FUTURE_SLACK_S = 60.0


def _recent_restarts(stamps: Any, window: float) -> List[str]:
    """Restart timestamps inside the window. Phase 2 review: stamps later than now (+ 60 s slack), recorded while
    the clock ran ahead (RTC in local time, a Pi before its first NTP sync), are dropped instead of counting
    toward max_restarts for hours."""
    now = _wall()
    out: List[str] = []
    for t in stamps or []:
        try:
            ts = util.parse_iso(t)
        except (HiveError, TypeError, ValueError):
            continue
        if now - window <= ts <= now + FUTURE_SLACK_S:
            out.append(t)
    return out


def _identity_system() -> Any:
    from .auth import system_identity
    return system_identity("node:autostart")


def _secret_re() -> "re.Pattern[str]":
    try:
        from .audit import SECRET_KEY_RE
    except Exception:  # noqa: BLE001
        SECRET_KEY_RE = r"(?i)(pass|secret|token|key|auth|cred|cookie|bearer)"
    return re.compile(SECRET_KEY_RE)


def check_base_url(url: str, what: str) -> str:
    """``http(s)://host[:port][/path]`` without userinfo, query or fragment (endpoint / health_url, §9.6 v4)."""
    try:
        u = urlsplit(str(url))
        _port = u.port
    except ValueError:
        raise bad_request(f"{what} is not a valid URL: {url!r}")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise bad_request(f"{what} must be http(s)://host[:port][/path] (got {url!r})")
    if u.username or u.password or "@" in (u.netloc or ""):
        raise bad_request(f"{what} must not contain credentials", hint="use api_key_env for a key")
    if u.query or u.fragment or "?" in str(url) or "#" in str(url):
        raise bad_request(f"{what} must not contain a query or fragment (got {url!r})")
    return str(url)


# ---------------------------------------------------------------------------------------------------------------
# module helpers (no runtime needed)

def _docker_exe() -> Optional[str]:
    return proctree.which("docker.exe" if util.IS_WINDOWS else "docker", proctree.trusted_path()) \
        or proctree.which("docker", proctree.trusted_path())


def _docker(args: List[str], timeout: float) -> Tuple[int, str, str]:
    exe = _docker_exe()
    if exe is None:
        raise not_found("docker CLI not found on the node's trusted PATH")
    code, out, err = proctree.run_quick([exe] + list(args), timeout=timeout, max_output=1 << 20)
    return code, util.decode_text(out), util.decode_text(err)


def _daemon_down(err: str) -> bool:
    low = (err or "").lower()
    return any(x in low for x in ("cannot connect to the docker daemon", "failed to connect to the docker",
                                  "is the docker daemon running", "error during connect", "docker_engine",
                                  "dockerdesktoplinuxengine", "connect: no such file or directory",
                                  "connection refused"))


def _resolve_any(argv0: str) -> Optional[str]:
    return proctree.which(argv0, proctree.trusted_path()) or proctree.which(argv0, os.environ.get("PATH", ""))


def run_stop_command(definition: dict, *, timeout: float = 30.0) -> Optional[int]:
    """Run a definition's ``stop_command`` (argv, no shell) without a runtime; its exit code, or None on failure."""
    argv = list((definition or {}).get("stop_command") or [])
    if not argv:
        return None
    exe = argv[0] if os.path.isabs(str(argv[0])) else _resolve_any(str(argv[0]))
    if exe is None:
        return None
    try:
        code, _o, _e = proctree.run_quick([exe] + [str(a) for a in argv[1:]], timeout=timeout)
        return code
    except HiveError:
        return None


def _read_status(run: Path) -> Optional[dict]:
    p = run / "status.json"
    try:
        d = util.read_json(p, None) if p.exists() else None
    except HiveError:
        return None
    return d if isinstance(d, dict) else None


def _shim_alive(run: Path) -> Tuple[bool, Optional[dict]]:
    st = _read_status(run)
    if not st or not st.get("shim_pid"):
        return False, st
    if (run / "exit.json").exists():
        return False, st
    return util.pid_alive(int(st["shim_pid"]), st.get("shim_ctime")), st


def running_offline(data_dir: Path) -> Tuple[List[str], List[str]]:
    """(job ids, service names) whose shim is alive, read from the state files without a runtime (v8.1, external
    review round 2: uninstall warns about what keeps running)."""
    state = Path(data_dir) / "state"
    jobs: List[str] = []
    svcs: List[str] = []
    jobs_dir = state / "jobs"
    for d in sorted(jobs_dir.iterdir()) if jobs_dir.is_dir() else []:
        if _shim_alive(d)[0]:
            jobs.append(d.name)
    svc_dir = state / "services"
    for d in sorted(svc_dir.iterdir()) if svc_dir.is_dir() else []:
        if _shim_alive(d / "run")[0]:
            svcs.append(d.name)
    return jobs, svcs


def panic_offline(data_dir: Path, node_id: str, *, force_after: float = 10.0) -> dict:
    """Stop every job, service and labelled container without a runtime (§5.10): stop files for live shims, wait
    ``force_after``, force-kill survivors, run stop_commands of services whose shim is dead, ``docker stop`` the
    containers labelled with this node id. ``{"jobs","services","containers","errors"}``."""
    data_dir = Path(data_dir)
    state = data_dir / "state"
    out: Dict[str, List[Any]] = {"jobs": [], "services": [], "containers": [], "errors": []}
    targets: List[Tuple[Path, dict]] = []
    grace = max(1.0, min(float(force_after), 10.0))
    jobs_dir = state / "jobs"
    for d in sorted(jobs_dir.iterdir()) if jobs_dir.is_dir() else []:
        alive, st = _shim_alive(d)
        if alive and st is not None:
            try:
                proctree.request_stop(d, grace)
                out["jobs"].append(d.name)
                targets.append((d, st))
            except OSError as e:
                out["errors"].append(f"job {d.name}: {e}")
    defs: Dict[str, dict] = {}
    try:
        raw = util.read_json(state / "services.json", {}) or {}
        defs = dict((raw.get("services") or {})) if isinstance(raw, dict) else {}
    except HiveError as e:
        out["errors"].append(f"services.json: {e.message}")
    svc_dir = state / "services"
    dead_with_stop: List[str] = []
    for d in sorted(svc_dir.iterdir()) if svc_dir.is_dir() else []:
        run = d / "run"
        alive, st = _shim_alive(run)
        rt_path = d / "runtime.json"
        try:
            rtd = util.read_json(rt_path, {}) if rt_path.exists() else {}
        except HiveError:
            rtd = {}
        if alive and st is not None:
            try:
                proctree.request_stop(run, grace)
                out["services"].append(d.name)
                targets.append((run, st))
            except OSError as e:
                out["errors"].append(f"service {d.name}: {e}")
        elif isinstance(rtd, dict) and rtd.get("state") in ("starting", "running", "unhealthy", "stopping",
                                                             "backoff") and (defs.get(d.name) or {}).get("stop_command"):
            dead_with_stop.append(d.name)
        if isinstance(rtd, dict) and rtd:
            rtd["desired"] = "stopped" if rtd.get("desired") != "removed" else "removed"
            rtd["last_error"] = PAUSED_ERROR
            try:
                util.write_json_atomic(rt_path, rtd, private=True)
            except OSError:
                pass
    end = time.monotonic() + max(0.0, float(force_after))
    while time.monotonic() < end:
        if not any(util.pid_alive(int(st["shim_pid"]), st.get("shim_ctime")) for _r, st in targets):
            break
        time.sleep(0.1)
    for run, st in targets:
        if util.pid_alive(int(st["shim_pid"]), st.get("shim_ctime")):
            try:
                proctree.kill_tree(int(st["shim_pid"]), force=True, create_time=st.get("shim_ctime"))
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"kill {run}: {e}")
        cp = st.get("child_pid")
        if cp and util.pid_alive(int(cp), st.get("child_ctime")):
            try:
                proctree.kill_tree(int(cp), force=True, create_time=st.get("child_ctime"))
            except Exception as e:  # noqa: BLE001
                out["errors"].append(f"kill {cp}: {e}")
    threads = []
    for name in dead_with_stop:
        def run_one(n: str = name) -> None:
            code = run_stop_command(defs[n], timeout=30.0)
            if code is None:
                out["errors"].append(f"service {n}: stop_command failed")
        t = threading.Thread(target=run_one, daemon=True)
        t.start()
        threads.append(t)
        if name not in out["services"]:
            out["services"].append(name)
    for t in threads:
        t.join(35)
    if node_id and _docker_exe() is not None:
        try:
            code, o, e = _docker(["ps", "-q", "--filter", f"label=ai.hive.node={node_id}"], 15)
            ids = [x.strip() for x in o.split() if x.strip()]
            if code != 0 and _daemon_down(e):
                pass   # no daemon -> no containers to stop
            elif code != 0:
                out["errors"].append(f"docker ps: {e.strip()[:200]}")
            elif ids:
                code, _o, e = _docker(["stop", "-t", "10"] + ids, 60)
                out["containers"] = ids
                if code != 0:
                    out["errors"].append(f"docker stop: {e.strip()[:200]}")
        except HiveError as e:
            out["errors"].append(f"docker: {e.message}")
    return out


def _http_status(url: str, timeout: float, guard: "NetGuard") -> Optional[int]:
    """GET ``url`` for a health check (purpose "health", never proxied); the HTTP status or None on failure."""
    from . import netutil
    try:
        r = netutil.guarded_request("GET", url, guard=guard, purpose="health", timeout=timeout, max_bytes=4096,
                                    follow_redirects=False)
        return int(r.status)
    except HiveError as e:
        if e.code != "unsupported":
            return None
    try:   # WP3 not merged yet: the P0 loopback-only open_stream
        status, _h, chunks = netutil.open_stream("GET", url, guard=guard, purpose="health",
                                                 connect_timeout=timeout, idle_timeout=timeout)
        try:
            for _b in chunks:
                break
        except HiveError:
            pass
        close = getattr(chunks, "close", None)
        if callable(close):
            close()
        return int(status)
    except HiveError:
        return None


def _tcp_check(port: int, timeout: float) -> Tuple[bool, Optional[float], Optional[str]]:
    from . import netutil
    try:
        return netutil.tcp_check("127.0.0.1", int(port), timeout)
    except HiveError as e:
        if e.code != "unsupported":
            return False, None, e.message
    t0 = time.monotonic()
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:   # loopback only (the service's own port, never proxied); WP3's tcp_check replaces this
        s.settimeout(timeout)
        s.connect(("127.0.0.1", int(port)))
        return True, round((time.monotonic() - t0) * 1000, 1), None
    except OSError as e:
        return False, None, str(e)
    finally:
        s.close()


def _port_listener(port: int) -> Tuple[bool, Optional[int], Optional[str]]:
    """(in use, pid, name) for 127.0.0.1/any listeners on ``port`` (procinfo.listeners, else a loopback connect)."""
    try:
        from . import procinfo
        rows, _method = procinfo.listeners()
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows or []:
        try:
            if int(r.get("port")) == int(port):
                return True, r.get("pid"), r.get("name") or r.get("process")
        except (TypeError, ValueError):
            continue
    ok, _lat, _err = _tcp_check(port, 0.5)
    return ok, None, None


# ---------------------------------------------------------------------------------------------------------------
# manager

class ServiceManager:
    """Managed services (§10.5)."""

    def __init__(self, rt: "NodeRuntime"):
        self.rt = rt
        base = Path(getattr(rt, "state_dir", None) or Path(rt.data_dir) / "state") if rt is not None else Path(".")
        self.defs_path = base / "services.json"
        self.dir = base / "services"
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._defs: Dict[str, dict] = {}
        self._rs: Dict[str, Dict[str, Any]] = {}
        self._ops: Dict[str, threading.Lock] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._autostart_thread: Optional[threading.Thread] = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._busy: set = set()
        self._deleting: set = set()
        self._view: Optional[dict] = None
        self._view_mtime: Optional[float] = None
        self._container_cache: Dict[str, Tuple[float, Optional[dict]]] = {}
        self._top_cache: Dict[str, Tuple[float, List[int]]] = {}
        self._cpu_procs: Dict[str, Dict[int, Any]] = {}
        self._started = False

    # ------------------------------------------------------------------------------------------- helpers
    def _op(self, name: str) -> threading.Lock:
        with self._lock:
            lk = self._ops.get(name)
            if lk is None:
                lk = self._ops[name] = threading.Lock()
            return lk

    def _run_dir(self, name: str) -> Path:
        return self.dir / name / "run"

    def _policy(self) -> Any:
        return getattr(self.rt, "policy", None)

    def _limits(self, eff: Any = None) -> Any:
        lim = getattr(eff, "limits", None)
        if lim is None:
            lim = getattr(self._policy(), "limits", None)
        return lim

    def _paused(self) -> bool:
        fn = getattr(self.rt, "is_paused", None)
        try:
            return bool(fn()) if callable(fn) else (Path(self.rt.data_dir) / "PAUSED").exists()
        except Exception:  # noqa: BLE001
            return False

    def _exec_allowed(self) -> Optional[str]:
        pol = self._policy()
        if pol is None:
            return None
        if not getattr(pol, "exec_enabled", True):
            return EXEC_DISABLED_ERROR
        if getattr(pol, "read_only", False):
            return "node policy is read-only"
        return None

    def _system_eff(self) -> Any:
        fn = getattr(self.rt, "effective", None)
        if callable(fn):
            return fn(_identity_system())
        pol = self._policy()
        if pol is not None and hasattr(pol, "effective"):
            return pol.effective(_identity_system(), paused=self._paused())
        raise unsupported("no node policy for system starts")

    def _save_defs(self, *, without: Optional[str] = None) -> None:
        with self._lock:
            skip = set(self._deleting) | ({without} if without else set())
            data = {"v": 1, "services": {k: dict(v) for k, v in self._defs.items() if k not in skip}}
        util.write_json_atomic(self.defs_path, data, private=True)
        self._view = None

    def _save_rs(self, name: str) -> None:
        with self._lock:
            rs = self._rs.get(name)
            if rs is None:
                return
            data = {k: v for k, v in rs.items() if not k.startswith("_")}
        try:
            util.secure_mkdir(self.dir / name)
            util.write_json_atomic(self.dir / name / "runtime.json", data, private=True)
        except OSError as e:
            log.warning("cannot write runtime state of %s: %s", name, e)

    @staticmethod
    def _new_rs() -> Dict[str, Any]:
        return {"v": 1, "desired": "stopped", "state": "stopped", "health": "none", "shim_pid": None,
                "shim_ctime": None, "child_pid": None, "child_ctime": None, "started_at": None, "boot_key": None,
                "restarts": [], "last_exit_code": None, "last_error": None, "next_restart_at": None,
                "held_paths": [], "adopted": False, "approved_files": {}}

    def _set(self, name: str, **kw: Any) -> None:
        with self._lock:
            rs = self._rs.get(name)
            if rs is None:
                return
            rs.update(kw)
            self._cond.notify_all()
        self._save_rs(name)

    # ------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Load definitions and runtime state, adopt live shims, apply the boot rule, autostart (staggered 2 s in
        name order), start the supervisor and the health pool."""
        util.secure_mkdir(self.dir)
        try:
            raw = util.read_json(self.defs_path, {}) if self.defs_path.exists() else {}
        except (HiveError, OSError, ValueError) as e:
            # Phase 2 review: an unreadable services.json (power loss, AV, a hand edit) never disables service
            # management: keep it as services.json.corrupt-<ts>, start with no definitions and warn
            bad = self.defs_path.with_name(f"services.json.corrupt-{time.strftime('%Y%m%d%H%M%S')}")
            msg = e.message if isinstance(e, HiveError) else str(e)
            try:
                util.replace_with_retry(self.defs_path, bad)
            except OSError:
                bad = self.defs_path
            text = f"services.json was unreadable ({msg[:200]}); kept as {bad.name}, starting with no services"
            log.warning("%s", text)
            ws = getattr(self.rt, "warnings", None)
            if isinstance(ws, list):
                ws.append(text)
            raw = {}
        defs = (raw.get("services") or {}) if isinstance(raw, dict) else {}
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="svc-health")
        to_start: List[str] = []
        boot = util.boot_key()
        allow_autostart = bool(getattr(self._limits(), "allow_autostart", True))
        paused = self._paused()
        for name in sorted(defs):
            d = defs[name]
            if not isinstance(d, dict):
                continue
            rpath = self.dir / name / "runtime.json"
            try:
                rs0 = util.read_json(rpath, None) if rpath.exists() else None
            except HiveError:
                rs0 = None
            rs = self._new_rs()
            if isinstance(rs0, dict):
                rs.update(rs0)
                if isinstance(rs.get("restarts"), list):    # drop future stamps (Phase 2 review)
                    rs["restarts"] = _recent_restarts(rs["restarts"], float("inf"))
            rs.update(_gen=0, _health={"last_check": None, "latency_ms": None, "consecutive_failures": 0,
                                       "last_error": None})
            with self._lock:
                self._defs[name] = d
                self._rs[name] = rs
            run = self._run_dir(name)
            alive = bool(rs.get("shim_pid")) and util.pid_alive(int(rs["shim_pid"]), rs.get("shim_ctime")) \
                and not (run / "exit.json").exists()
            if alive:
                prev = rs.get("state")
                started_mono = _mono()
                if prev == "starting" and rs.get("started_at"):
                    try:   # keep the rest of start_period (a model still loading is not judged as running)
                        started_mono -= max(0.0, _wall() - util.parse_iso(str(rs["started_at"])))
                    except HiveError:
                        pass
                rs.update(state=prev if prev in ("stopping", "starting") else "running",
                          health="unknown" if self._health_kind(d) != "none" else "none", adopted=True,
                          _started_mono=started_mono, _next_health=_mono(),
                          _ready_offset=int(rs.get("log_offset") or 0))
                if prev == "stopping":
                    rs["_stop_mono"] = _mono()
                if rs.get("desired") == "stopped" and rs.get("state") != "stopping":
                    rs["desired"] = "running"
                self._rehold(name, rs)
                self._save_rs(name)
                continue
            ex = self._read_exit(name)
            if ex is not None and rs.get("state") in ALIVE:
                rs["last_exit_code"] = ex.get("exit_code")
            if ex is None and rs.get("shim_pid"):
                self._kill_orphan(name)     # dead shim, no exit.json: never leave its child running beside a new one
            if rs.get("desired") == "removed":
                self._delete(name, delete_logs=bool(rs.get("delete_logs")))
                continue
            rs.update(shim_pid=None, shim_ctime=None, child_pid=None, child_ctime=None, adopted=False,
                      next_restart_at=None)
            if paused:
                if rs.get("desired") == "running":
                    rs.update(desired="stopped", last_error=PAUSED_ERROR)
                rs.update(state="stopped", health="none")
            elif util.same_boot(rs.get("boot_key"), boot) and rs.get("desired") == "running":
                rs.update(state="stopped", health="none")
                to_start.append(name)
            elif d.get("autostart") and allow_autostart:
                rs.update(state="stopped", health="none", desired="running")
                to_start.append(name)
            else:
                if rs.get("desired") == "running" or rs.get("state") in ALIVE + ("backoff",):
                    rs["last_error"] = REBOOT_ERROR
                rs.update(desired="stopped", state="stopped", health="none")
            self._save_rs(name)
        self._stop.clear()
        self._thread = threading.Thread(target=self._supervisor, name="svc-supervisor", daemon=True)
        self._thread.start()
        self._started = True
        if to_start:
            self._autostart_thread = threading.Thread(target=self._autostart, args=(to_start,), name="svc-autostart",
                                                      daemon=True)
            self._autostart_thread.start()

    def _autostart(self, names: List[str]) -> None:
        for i, name in enumerate(names):
            if i and self._stop.wait(AUTOSTART_STAGGER_S):
                return
            try:
                self._system_spawn(name, count_restart=False)
            except Exception:  # noqa: BLE001
                log.warning("autostart of %s failed", name, exc_info=True)

    def shutdown(self) -> None:
        """Stop the supervisor; services keep running unless ``services.stop_on_exit`` (then the shim stop
        sequence runs, so stop_command runs too)."""
        cfg = getattr(getattr(getattr(self.rt, "config", None), "policy", None), "services", None) or {}
        if cfg.get("stop_on_exit"):
            waits = []
            for name in list(self._rs):
                if self._alive(name):
                    d = self._defs.get(name) or {}
                    try:
                        proctree.request_stop(self._run_dir(name), float(d.get("stop_timeout", 15)))
                        waits.append(float(d.get("stop_timeout", 15)) + STOP_EXTRA_S)
                    except OSError:
                        pass
            end = time.monotonic() + min(30.0, max(waits or [0]))
            while time.monotonic() < end and any(self._alive(n) for n in list(self._rs)):
                time.sleep(0.2)
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(3)
        self._thread = None
        at = getattr(self, "_autostart_thread", None)
        if at is not None:       # Phase 2 review: a staggered autostart never spawns after the shutdown
            at.join(5)
            self._autostart_thread = None
        with self._lock:
            proctree.adopt_popens([rs.get("_popen") for rs in self._rs.values() if rs.get("_popen") is not None])
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        self._started = False

    # ------------------------------------------------------------------------------------------- holds
    def _rehold(self, name: str, rs: Dict[str, Any]) -> None:
        """Adoption after a node restart: hold the approved files again, checked against the STORED approved
        fingerprints (v8.1); a file changed while the node was down -> no hold, ``last_error`` says so, and the next
        respawn refuses to run it (the running process is left alone)."""
        paths = rs.get("held_paths") or []
        holds = getattr(self.rt, "holds", None)
        approved = rs.get("approved_files")
        if not isinstance(approved, dict):
            approved = {}
        if paths and not approved:
            # a 1.0.0 run state (no approved_files): only the current content is known; keep it from now on
            from .approvals import file_fingerprint
            approved = {}
            for p in paths:
                try:
                    approved[p] = file_fingerprint(p)
                except OSError:
                    approved[p] = "unreadable"
            rs["approved_files"] = approved
            self._view = None
        if not paths or holds is None:
            return
        try:
            hid = holds.acquire({p: approved.get(p) for p in paths}, policy=self._system_eff())
            rs["_hold"] = hid
        except HiveError as e:
            rs["held_paths"] = []
            if e.code == "conflict":
                rs["last_error"] = APPROVED_CHANGED_ERROR + " (changed while the node was down: " + \
                    ", ".join(str(x) for x in (e.details.get("changed") or [])[:4]) + ")"
            log.warning("service %s: could not hold its approved files again: %s", name, e.message)
        except Exception as e:  # noqa: BLE001
            rs["held_paths"] = []
            log.warning("service %s: could not hold its approved files again: %s", name, e)

    def _hold_approved(self, name: str) -> Tuple[bool, Optional[str]]:
        """(ok, hold id) for a system spawn (crash restart, autostart, same-boot restart): the stored approved
        fingerprints are verified and held in one step (v8.1); no approved files -> (True, None)."""
        with self._lock:
            approved = dict((self._rs.get(name) or {}).get("approved_files") or {})
        if not approved:
            return True, None
        holds = getattr(self.rt, "holds", None)
        try:
            eff = self._system_eff()
        except HiveError:
            return False, None
        if holds is not None:
            try:
                return True, holds.acquire(approved, policy=eff)
            except HiveError:
                return False, None
        from .approvals import file_fingerprint
        for p, want in approved.items():
            try:
                cur = file_fingerprint(str(eff.resolve_path(p)),
                                       prefer_stat=bool(want and str(want).startswith("stat:")))
            except Exception:  # noqa: BLE001
                return False, None
            if cur != want:
                return False, None
        return True, None

    def _set_approved(self, name: str, ctx: Optional["ActionContext"]) -> None:
        """A manual start/define with the calling identity (v8.1): the approved fingerprints become those of the
        call's hold (an approval that bound files), merged over the earlier ones re-fingerprinted now (an ungated
        or owner start re-approves what it runs; gated identities cannot change those paths meanwhile, they are
        in the service code patterns)."""
        from .approvals import file_fingerprint
        holds = getattr(self.rt, "holds", None)
        hold = getattr(ctx, "hold", None) if ctx is not None else None
        new: Dict[str, Optional[str]] = {}
        with self._lock:
            old = dict((self._rs.get(name) or {}).get("approved_files") or {})
        for p, want in old.items():
            try:
                new[p] = file_fingerprint(p, prefer_stat=bool(want and str(want).startswith("stat:")))
            except OSError:
                new[p] = "unreadable"
        if hold and holds is not None:
            try:
                new.update(holds.fingerprints(hold))
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            rs = self._rs.get(name)
            if rs is not None:
                rs["approved_files"] = new
            self._view = None

    def _release_hold(self, name: str) -> None:
        with self._lock:
            rs = self._rs.get(name) or {}
            hid = rs.pop("_hold", None)
            rs["held_paths"] = []
        holds = getattr(self.rt, "holds", None)
        if hid and holds is not None:
            try:
                holds.release(hid)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------------------------------- process state
    def _read_exit(self, name: str) -> Optional[dict]:
        p = self._run_dir(name) / "exit.json"
        if not p.exists():
            return None
        try:
            d = util.read_json(p, None)
        except HiveError:
            return None
        return d if isinstance(d, dict) else None

    def _kill_orphan(self, name: str) -> None:
        """The shim of ``name`` is gone without exit.json (killed, OOM): kill its recorded child (own session, so it
        survived the shim) before any restart, so the service never runs twice (identity-checked by create time)."""
        with self._lock:
            rs = self._rs.get(name) or {}
            cands = [(rs.get("child_pid"), rs.get("child_ctime"))]
        p = self._run_dir(name) / "ready.json"
        try:
            r = util.read_json(p, None) if p.exists() else None
        except HiveError:
            r = None
        if isinstance(r, dict):
            cands.append((r.get("child_pid"), r.get("child_ctime")))
        seen = set()
        for pid, ct in cands:
            if not pid or ct is None or int(pid) in seen:
                continue
            seen.add(int(pid))
            try:
                if util.pid_alive(int(pid), ct):
                    proctree.kill_tree(int(pid), force=True, create_time=ct)
            except Exception:  # noqa: BLE001
                log.warning("service %s: could not kill the orphaned child %s", name, pid, exc_info=True)

    def _alive(self, name: str) -> bool:
        with self._lock:
            rs = self._rs.get(name)
            if rs is None or not rs.get("shim_pid"):
                return False
            popen = rs.get("_popen")
            pid, ct = int(rs["shim_pid"]), rs.get("shim_ctime")
        if popen is not None:
            return popen.poll() is None
        if (self._run_dir(name) / "exit.json").exists():
            return False
        return util.pid_alive(pid, ct)

    def _health_kind(self, d: dict) -> str:
        h = d.get("health") or "auto"
        if h == "auto":
            if d.get("health_url") or d.get("endpoint"):
                return "http"
            if d.get("port"):
                return "tcp"
            return "none"
        return h

    def _health_url(self, d: dict) -> Optional[str]:
        if d.get("health_url"):
            return str(d["health_url"])
        if d.get("endpoint"):
            return str(d["endpoint"]).rstrip("/") + "/models"
        return None

    # ------------------------------------------------------------------------------------------- building
    def _build(self, d: dict, eff: Any) -> Tuple[Any, Optional[List[str]], Optional[List[str]], Path, Dict[str, str],
                                                 Dict[str, bytes]]:
        """Policy checks + build of (argv|cmdline, stop_argv, post_exit_argv, cwd, env, files) for one spawn."""
        cwd = eff.check_cwd(d.get("cwd"))
        env_over = {str(k): str(v) for k, v in (d.get("env") or {}).items()}
        eff.check_env(env_over)
        files: Dict[str, bytes] = {}
        if d.get("argv"):
            argv = [str(a) for a in d["argv"]]
            if is_wsl_exe(argv[0]):
                raise bad_request(WSL_ERROR, hint=WSL_HINT)
            eff.check_command(list(argv), "none")
            built: Any = proctree.build_argv(argv, eff.node_path, cwd=cwd)
        else:
            command = str(d.get("command") or "")
            if not command:
                raise bad_request("service has neither command nor argv")
            from .jobs import _first_word
            if is_wsl_exe(_first_word(command)):
                raise bad_request(WSL_ERROR, hint=WSL_HINT)
            shell = proctree.resolve_shell(d.get("shell") or "auto")
            eff.check_shell(shell)
            eff.check_command(command, shell)
            plan = proctree.build_shell_argv(command, shell, login=bool(d.get("login")))
            built, files = plan.argv, dict(plan.files)
        stop_argv = None
        if d.get("stop_command"):
            sc = [str(a) for a in d["stop_command"]]
            eff.check_command(list(sc), "none")
            stop_argv = proctree.build_argv(sc, eff.node_path, cwd=cwd)
        post = None
        if d.get("post_exit"):
            try:
                post = proctree.build_argv([str(a) for a in d["post_exit"]], eff.node_path, cwd=cwd)
            except HiveError:
                post = [str(a) for a in d["post_exit"]]
        for u in (d.get("health_url"), d.get("endpoint")):
            if u:
                try:
                    eff.check_url(str(u), purpose="health")
                except HiveError as e:
                    # WP3a CONTRACT-REQUEST: a DNS failure (unavailable) is not a policy decision; the health check
                    # reports it later, the definition/start is not refused for it
                    if e.code not in ("unsupported", "unavailable"):
                        raise
        for v in d.get("volumes") or []:
            real, _c, _m = presets.docker_volume(v.get("spec", ""), eff)
            if real != v.get("host"):
                raise denied(f"volume host {v.get('spec')} now resolves to {real}; define the service again",
                             rule="volume_changed")
        env = eff.child_env(env_over, gpus=d.get("gpus"), secret_env=d.get("secret_env") or ())
        return built, stop_argv, post, Path(cwd), env, files

    # ------------------------------------------------------------------------------------------- spawning
    def _fail(self, name: str, msg: str, *, hint: Optional[str] = None) -> None:
        self._set(name, state="failed", health="none", last_error=msg + (f" ({hint})" if hint else ""),
                  next_restart_at=None)

    def _container_precheck(self, name: str, d: dict) -> str:
        """Container rule 1: 'go' | 'wait' | 'failed' (state updated)."""
        c = d.get("container")
        if not c:
            return "go"
        node_id = getattr(self.rt, "node_id", "") or ""
        try:
            code, out, err = _docker(["inspect", "--format", "{{json .Config.Labels}}", c], 15)
        except HiveError as e:
            if e.code == "not_found":
                self._fail(name, "docker not found", hint="install Docker or remove this service")
                return "failed"
            code, out, err = 1, "", e.message
        if code == 0:
            try:
                labels = json.loads(out.strip() or "null") or {}
            except ValueError:
                labels = {}
            if labels.get("ai.hive.node") == node_id and labels.get("ai.hive.service") == name:
                try:
                    _docker(["rm", "-f", c], 30)
                except HiveError:
                    pass
                return "go"
            self._fail(name, f"container name {c} is used by a container Hive did not create; remove or rename it")
            return "failed"
        low = err.lower()
        if "no such object" in low or "no such container" in low or "not found" in low:
            return "go"
        if "permission denied" in low:
            self._fail(name, "permission denied on the Docker socket",
                       hint="add the user to the docker group (root-equivalent) or use rootless Docker")
            return "failed"
        if _daemon_down(err) or not err.strip():
            return "wait"
        self._fail(name, "docker inspect failed: " + err.strip()[:300])
        return "failed"

    def _spawn(self, name: str, *, eff: Any, ctx: Optional["ActionContext"] = None, count_restart: bool = False,
               manual: bool = False, hold: Optional[str] = None) -> None:
        """Pre-spawn checks and the shim spawn (caller holds the service op lock)."""
        with self._lock:
            d = dict(self._defs.get(name) or {})
            rs = self._rs.get(name)
        if not d or rs is None:
            raise not_found(f"service '{name}' not found")
        msg = self._exec_allowed()
        if msg is None and self._paused():
            msg = "node is paused; nothing is started while paused"
        if msg is not None:
            self._fail(name, msg)
            if manual:
                raise denied(msg, rule="paused" if "paused" in msg else "exec_disabled")
            return
        try:
            built, stop_argv, post, cwd, env, files = self._build(d, eff)
        except HiveError as e:
            self._fail(name, e.message)
            if manual:
                raise
            return
        port = d.get("port")
        if port and not d.get("force_port"):
            in_use, pid, pname = _port_listener(int(port))
            if in_use:
                who = f" by pid {pid}" + (f" ({pname})" if pname else "") if pid else ""
                hint = "stop that program or pick another port"
                if util.IS_WINDOWS and int(port) == 11434:  # pragma: no cover - Windows only
                    hint += ("; the Ollama tray app serves 11434 after logon; use it directly (model_chat "
                             "service='ollama'; gateway name ollama) or quit it")
                self._fail(name, f"port {port} in use{who}", hint=hint)
                if manual:
                    raise conflict(f"port {port} in use{who}", hint=hint, port=port)
                return
        if d.get("container"):
            res = self._container_precheck(name, d)
            if res == "failed":
                if manual:
                    raise conflict(str((self._rs.get(name) or {}).get("last_error")))
                return
            if res == "wait":
                with self._lock:
                    since = rs.get("_docker_wait_since") or _mono()
                    rs.update(_docker_wait_since=since, _docker_next=_mono() + DOCKER_RECHECK_S)
                if _mono() - since >= DOCKER_WAIT_MAX_S:
                    self._fail(name, DOCKER_WAIT_ERROR)
                    with self._lock:
                        rs.pop("_docker_wait_since", None)
                    return
                self._set(name, state="starting", health="starting", desired="running",
                          last_error=DOCKER_WAIT_ERROR)
                return
        run = self._run_dir(name)
        util.secure_mkdir(self.dir / name)
        util.secure_mkdir(run)
        for fn in ("exit.json", "ready.json", "status.json", "stop", "spec.json"):
            try:
                (run / fn).unlink()
            except OSError:
                pass
        for fname, data in files.items():
            util.atomic_write_bytes(run / fname, data, private=True)
        if isinstance(built, list) and files:
            # only the element Hive generated (the PowerShell -File path) refers to the run dir; the definition's
            # own argv/command text is never rewritten after CommandGuard and approval saw it
            marks = {"{run_dir}" + sep + f for f in files for sep in ("/", "\\", os.sep)}
            built = [os.path.join(str(run), str(a)[len("{run_dir}") + 1:]) if str(a) in marks else str(a)
                     for a in built]
        spec = {"v": 1, "argv": built if isinstance(built, list) else None,
                "cmdline": built if isinstance(built, str) else None, "cwd": str(cwd), "env": env, "stdin_file": None,
                "merge_stderr": True, "logs": {"output": "output.log"},
                "log_max_bytes": int(d.get("log_max_mb") or 64) << 20, "log_keep": int(d.get("log_keep", 2)),
                "timeout": 0, "timeout_grace": float(d.get("stop_timeout", 15)), "keep_awake": bool(d.get("keep_awake")),
                "stop_argv": stop_argv, "post_exit_argv": post,
                "files": {"status": "status.json", "exit": "exit.json", "stop": "stop", "ready": "ready.json"}}
        log_offset = textlog.log_size(run / "output.log")
        try:
            h = proctree.spawn_shim(spec, run)
        except HiveError as e:
            self._fail(name, e.message)
            if manual:
                raise
            return
        if ctx is not None:
            self._set_approved(name, ctx)
            hold = getattr(ctx, "hold", None)
        held: List[str] = []
        holds = getattr(self.rt, "holds", None)
        if hold and holds is not None:
            self._release_hold(name)
            try:
                held = holds.paths(hold)
            except Exception:  # noqa: BLE001
                held = []
            if ctx is not None:
                ctx.hold = None
        now_iso = util.iso_utc()
        kind = self._health_kind(d)
        with self._lock:
            restarts = list(rs.get("restarts") or [])
            if count_restart:
                restarts.append(now_iso)
            rs.update(state="starting", health="starting" if kind != "none" else "none", desired="running",
                      shim_pid=h.shim_pid, shim_ctime=h.shim_ctime, child_pid=None, child_ctime=None,
                      started_at=now_iso, boot_key=util.boot_key(), restarts=restarts, next_restart_at=None,
                      adopted=False, last_error=None, held_paths=held,
                      _popen=h.popen, _gen=int(rs.get("_gen", 0)) + 1, _started_mono=_mono(),
                      _next_health=_mono(), _ready_offset=log_offset, log_offset=log_offset,
                      _health={"last_check": None, "latency_ms": None, "consecutive_failures": 0,
                               "last_error": None})
            rs.pop("_docker_wait_since", None)
            rs.pop("_respawn", None)
            if hold:
                rs["_hold"] = hold
            self._cond.notify_all()
            late = self._stop.is_set()
        if late:     # the manager shut down during this spawn: its Popen is reaped by proctree (Phase 2 review)
            proctree.adopt_popens([h.popen])
        self._save_rs(name)
        end = time.monotonic() + 2.0
        while time.monotonic() < end and not (run / "ready.json").exists() and not (run / "exit.json").exists():
            time.sleep(0.01)
        self._read_ready(name)

    def _read_ready(self, name: str) -> None:
        p = self._run_dir(name) / "ready.json"
        if not p.exists():
            return
        try:
            r = util.read_json(p, None)
        except HiveError:
            return
        if isinstance(r, dict) and r.get("child_pid"):
            with self._lock:
                rs = self._rs.get(name)
                if rs is not None and rs.get("child_pid") != r["child_pid"]:
                    rs.update(child_pid=int(r["child_pid"]), child_ctime=r.get("child_ctime"))
                else:
                    return
            self._save_rs(name)

    def _system_spawn(self, name: str, *, count_restart: bool) -> None:
        if self._stop.is_set():
            return          # the manager is shutting down: nothing new is spawned
        with self._op(name):
            if self._stop.is_set():
                return
            with self._lock:
                rs = self._rs.get(name)
                if name not in self._defs or rs is None or rs.get("desired") != "running" or rs.get("_manual"):
                    return
            if self._alive(name):
                return
            try:
                eff = self._system_eff()
            except HiveError as e:
                self._fail(name, e.message)
                return
            ok, hid = self._hold_approved(name)
            if not ok:      # v8.1: never run a bound file the owner did not approve (crash restart, autostart)
                self._fail(name, APPROVED_CHANGED_ERROR)
                log.warning("service %s: %s", name, APPROVED_CHANGED_ERROR)
                return
            try:
                self._spawn(name, eff=eff, count_restart=count_restart, hold=hid)
            finally:
                with self._lock:
                    taken = bool(hid) and (self._rs.get(name) or {}).get("_hold") == hid
                holds = getattr(self.rt, "holds", None)
                if hid and not taken and holds is not None:
                    holds.release(hid)

    # ------------------------------------------------------------------------------------------- supervisor
    def _supervisor(self) -> None:
        while not self._stop.wait(TICK):
            for name in list(self._rs):
                lk = self._op(name)
                if not lk.acquire(blocking=False):
                    continue
                try:
                    self._tick(name)
                except Exception:  # noqa: BLE001
                    log.warning("supervisor tick for %s failed", name, exc_info=True)
                finally:
                    lk.release()

    def _submit(self, key: str, fn: Any, *args: Any, **kwargs: Any) -> None:
        pool = self._pool
        if pool is None:
            return
        with self._lock:
            if key in self._busy:
                return
            self._busy.add(key)

        def run() -> None:
            try:
                fn(*args, **kwargs)
            except Exception:  # noqa: BLE001
                log.warning("service task %s failed", key, exc_info=True)
            finally:
                with self._lock:
                    self._busy.discard(key)
        try:
            pool.submit(run)
        except RuntimeError:
            with self._lock:
                self._busy.discard(key)

    def _tick(self, name: str) -> None:
        with self._lock:
            rs = self._rs.get(name)
            d = self._defs.get(name)
            if rs is None or d is None:
                return
            state = rs.get("state")
            desired = rs.get("desired")
        now = _mono()
        if rs.get("_docker_wait_since") and not rs.get("shim_pid"):
            if desired == "running" and now >= float(rs.get("_docker_next") or 0):
                self._submit("spawn:" + name, self._system_spawn, name, count_restart=False)
            return
        if state in ALIVE and rs.get("shim_pid"):
            if not self._alive(name):
                self._on_exit(name)
                return
            self._read_ready(name)
            if state == "stopping":
                if now - float(rs.get("_stop_mono") or now) >= float(d.get("stop_timeout", 15)) + STOP_EXTRA_S:
                    self._force_kill(name)
                return
            kind = self._health_kind(d)
            if state == "starting":
                if kind == "none" and not d.get("ready_pattern"):
                    if now - float(rs.get("_started_mono") or now) >= NO_HEALTH_ALIVE_S:
                        self._set(name, state="running", health="none")
                    return
                if d.get("ready_pattern"):
                    self._submit("ready:" + name, self._check_ready, name, int(rs.get("_gen", 0)))
                if kind != "none" and now >= float(rs.get("_next_health") or 0):
                    with self._lock:
                        rs["_next_health"] = now + float(d.get("health_interval", 15))
                    self._submit("health:" + name, self._check_health, name, int(rs.get("_gen", 0)))
                if now - float(rs.get("_started_mono") or now) >= float(d.get("start_period", 120)):
                    self._set(name, state="unhealthy", health="unhealthy",
                              last_error=f"not healthy after start_period {float(d.get('start_period', 120)):g} s")
                    if d.get("restart_on_unhealthy"):
                        self._restart_unhealthy(name)
                return
            if state in ("running", "unhealthy") and kind != "none" and now >= float(rs.get("_next_health") or 0):
                with self._lock:
                    rs["_next_health"] = now + float(d.get("health_interval", 15))
                self._submit("health:" + name, self._check_health, name, int(rs.get("_gen", 0)))
            return
        if state == "backoff" and desired == "running":
            nra = rs.get("_next_restart_mono")
            if nra is not None and now >= float(nra):
                self._submit("spawn:" + name, self._system_spawn, name, count_restart=True)
            return
        if state in ("stopped",) and desired == "running" and not rs.get("_manual"):
            self._submit("spawn:" + name, self._system_spawn, name, count_restart=False)

    def _force_kill(self, name: str) -> None:
        with self._lock:
            rs = dict(self._rs.get(name) or {})
        if rs.get("shim_pid"):
            proctree.kill_tree(int(rs["shim_pid"]), force=True, create_time=rs.get("shim_ctime"))
        if rs.get("child_pid"):
            proctree.kill_tree(int(rs["child_pid"]), force=True, create_time=rs.get("child_ctime"))
        if not self._alive(name):
            self._on_exit(name)

    def _restart_unhealthy(self, name: str) -> None:
        with self._lock:
            rs = self._rs.get(name)
            d = self._defs.get(name) or {}
            if rs is None:
                return
            rs["_respawn"] = True
            rs["_stop_mono"] = _mono()
        self._set(name, state="stopping")
        try:
            proctree.request_stop(self._run_dir(name), float(d.get("stop_timeout", 15)))
        except OSError:
            pass

    def _on_exit(self, name: str) -> None:
        """The shim of ``name`` is gone: record the exit and apply desired state / restart policy (§10.5)."""
        ex = self._read_exit(name)
        with self._lock:
            rs = self._rs.get(name)
            d = self._defs.get(name) or {}
            if rs is None:
                return
            popen = rs.pop("_popen", None)
            run_s = _mono() - float(rs.get("_started_mono") or _mono())
            desired = rs.get("desired")
            state = rs.get("state")
            respawn = rs.pop("_respawn", False)
            manual = rs.get("_manual")
        if popen is not None:
            try:
                popen.poll()
            except Exception:  # noqa: BLE001
                pass
        exit_code = ex.get("exit_code") if ex else None
        if ex is None:
            self._kill_orphan(name)     # the shim died without exit.json: its child (own session) may still run
        self._release_hold(name)
        base = dict(shim_pid=None, shim_ctime=None, child_pid=None, child_ctime=None, last_exit_code=exit_code,
                    adopted=False)
        if ex and ex.get("error") and exit_code is None:
            base["last_error"] = str(ex["error"])[:500]
        if desired == "removed":
            self._delete(name, delete_logs=bool((self._rs.get(name) or {}).get("delete_logs")))
            return
        if manual or desired == "stopped" or (state == "stopping" and not respawn):
            if desired == "running" and not manual:
                desired = "stopped"     # nobody asked to start it again: the supervisor must not respawn it
            self._set(name, state="stopped", health="none", desired=desired, **base)
            return
        if self._paused():
            base["last_error"] = PAUSED_ERROR
            self._set(name, state="stopped", desired="stopped", health="none", **base)
            return
        policy = d.get("restart") or "on-failure"
        if respawn or policy == "always" or (policy == "on-failure" and exit_code != 0):
            with self._lock:
                short = run_s < SHORT_RUN_S
                k = int(rs.get("_short_runs", 0)) if short else 0
                rs["_short_runs"] = k + 1 if short else 0
                window = float(d.get("restart_window", 600))
                recent = _recent_restarts(rs.get("restarts"), window)
                rs["restarts"] = recent
            if len(recent) >= int(d.get("max_restarts", 5)):
                base["last_error"] = (f"restarted {len(recent)} times within {window:g} s; giving up (last exit "
                                      f"code {exit_code})")
                self._set(name, state="failed", health="none", next_restart_at=None, **base)
                return
            delay = 0.0 if respawn else min(float(d.get("restart_delay", 3)) * (2 ** k), MAX_BACKOFF_S)
            with self._lock:
                rs["_next_restart_mono"] = _mono() + delay
            if base.get("last_error") is None:
                base["last_error"] = f"exited with code {exit_code}" if not respawn else "restarted (unhealthy)"
            self._set(name, state="backoff", health="none", next_restart_at=util.iso_utc(_wall() + delay), **base)
            return
        # restart=no, or on-failure with exit code 0: the service stays stopped (desired follows, so the supervisor's
        # "stopped + desired running" start, meant for boot/autostart starts, does not respawn it)
        self._set(name, state="stopped", desired="stopped", health="none", **base)

    def _check_health(self, name: str, gen: int) -> None:
        with self._lock:
            d = dict(self._defs.get(name) or {})
        kind = self._health_kind(d)
        t0 = time.monotonic()
        ok = False
        err: Optional[str] = None
        latency: Optional[float] = None
        timeout = float(d.get("health_timeout", 5))
        if kind == "http":
            url = self._health_url(d)
            pol = self._policy()
            guard = getattr(pol, "net_guard", None)
            status = _http_status(url, timeout, guard) if url else None
            lo, hi = (list(d.get("health_expect") or [200, 399]) + [399])[:2]
            ok = status is not None and int(lo) <= int(status) <= int(hi)
            latency = round((time.monotonic() - t0) * 1000, 1)
            if not ok:
                err = f"GET {url} -> {status if status is not None else 'no answer'}"
        elif kind == "tcp":
            ok, latency, e = _tcp_check(int(d.get("port") or 0), timeout)
            if not ok:
                err = f"port {d.get('port')} closed: {e}"
        else:
            return
        self._apply_health(name, gen, ok, latency, err)

    def _apply_health(self, name: str, gen: int, ok: bool, latency: Optional[float], err: Optional[str]) -> None:
        restart = False
        with self._lock:
            rs = self._rs.get(name)
            d = self._defs.get(name) or {}
            if rs is None or int(rs.get("_gen", 0)) != gen or rs.get("state") not in ("starting", "running",
                                                                                      "unhealthy"):
                return
            h = rs.setdefault("_health", {})
            h["last_check"] = util.iso_utc()
            h["latency_ms"] = latency
            if ok:
                h["consecutive_failures"] = 0
                h["last_error"] = None
                new = {"state": "running", "health": "healthy"}
                if rs.get("state") == "unhealthy" or rs.get("state") == "starting":
                    new["last_error"] = None
            else:
                h["consecutive_failures"] = int(h.get("consecutive_failures", 0)) + 1
                h["last_error"] = err
                new = {}
                if rs.get("state") == "running" and h["consecutive_failures"] >= int(d.get("unhealthy_threshold", 3)):
                    new = {"state": "unhealthy", "health": "unhealthy", "last_error": f"health check failing: {err}"}
                    restart = bool(d.get("restart_on_unhealthy"))
            rs.update(new)
            self._cond.notify_all()
        self._save_rs(name)
        if restart:
            self._restart_unhealthy(name)

    def _check_ready(self, name: str, gen: int) -> None:
        with self._lock:
            d = dict(self._defs.get(name) or {})
            rs = self._rs.get(name) or {}
            off = int(rs.get("_ready_offset") or 0)
        pat = d.get("ready_pattern")
        if not pat:
            return
        try:
            from .fsops import check_regex
            rx = check_regex(str(pat))
        except HiveError:
            return
        r = textlog.read_range(self._run_dir(name) / "output.log", off, 1 << 20, None)
        text = util.decode_text(r["data"])
        with self._lock:
            if rs.get("_gen") == gen:
                rs["_ready_offset"] = max(off, r["next_offset"] - 256)
        if rx.search(text):
            self._apply_health(name, gen, True, None, None)

    # ------------------------------------------------------------------------------------------- deletion
    def _delete(self, name: str, *, delete_logs: bool) -> None:
        self._release_hold(name)
        if delete_logs:
            shutil.rmtree(self.dir / name, ignore_errors=True)
        else:
            try:
                (self.dir / name / "runtime.json").unlink()
            except OSError:
                pass
        with self._lock:
            self._deleting.add(name)
        try:
            self._save_defs(without=name)      # the file first, so it never lists a service the map dropped
            with self._lock:
                self._defs.pop(name, None)
                self._rs.pop(name, None)
                self._view = None
                self._cond.notify_all()
        finally:
            with self._lock:
                self._deleting.discard(name)

    # ------------------------------------------------------------------------------------------- views
    def _get(self, name: str) -> Tuple[dict, Dict[str, Any]]:
        with self._lock:
            d = self._defs.get(name)
            rs = self._rs.get(name)
            names = sorted(self._defs)
        if d is None or rs is None:
            raise not_found(f"service '{name}' not found",
                            hint=("defined services: " + ", ".join(names[:20])) if names else
                            "no services are defined on this node (service_define)")
        return d, rs

    def _restarts_in_window(self, d: dict, rs: dict) -> int:
        return len(_recent_restarts(rs.get("restarts"), float(d.get("restart_window", 600))))

    def _summary(self, name: str) -> dict:
        d, rs = self._get(name)
        with self._lock:
            r = dict(rs)
        uptime = None
        if r.get("state") in ALIVE and r.get("started_at"):
            try:
                uptime = round(max(0.0, _wall() - util.parse_iso(r["started_at"])), 1)
            except HiveError:
                uptime = None
        return {"name": name, "state": r.get("state"), "desired": r.get("desired"), "health": r.get("health") or "none",
                "pid": r.get("child_pid"), "uptime_s": uptime, "restarts": self._restarts_in_window(d, r),
                "port": d.get("port"), "endpoint": d.get("endpoint"), "autostart": bool(d.get("autostart")),
                "last_exit_code": r.get("last_exit_code"), "last_error": r.get("last_error"),
                "adopted": bool(r.get("adopted")), "description": d.get("description")}

    @staticmethod
    def redacted(d: dict) -> dict:
        """A definition for output: env values redacted to keys, secret_env names only, internal keys dropped."""
        out = {k: v for k, v in d.items() if k not in ("volumes",) and not k.startswith("_")}
        out["env"] = sorted((d.get("env") or {}).keys())
        out["secret_env"] = list(d.get("secret_env") or [])
        return out

    def _container_pids(self, c: str) -> List[int]:
        """Host pids of the processes in container ``c`` (``docker top <c> -eo pid``, cached 10 s); Linux only
        (Docker Desktop pids belong to its VM), [] on any failure."""
        if util.IS_WINDOWS or util.IS_MAC:
            return []
        cached = self._top_cache.get(c)
        if cached and time.monotonic() - cached[0] < CONTAINER_CACHE_S:
            return list(cached[1])
        pids: List[int] = []
        try:
            code, out, _e = _docker(["top", c, "-eo", "pid"], 10)
            if code == 0:
                for line in out.splitlines()[1:]:
                    w = line.strip().split()
                    if w and w[0].isdigit():
                        pids.append(int(w[0]))
        except HiveError:
            pids = []
        self._top_cache[c] = (time.monotonic(), pids)
        return list(pids)

    def _resources(self, name: str, rs: dict, d: Optional[dict] = None) -> dict:
        pids: List[int] = []
        for k in ("shim_pid", "child_pid"):
            if rs.get(k):
                pids.append(int(rs[k]))
        if rs.get("child_pid") and rs.get("state") in ALIVE:
            try:
                pids += [p for p in proctree.descendants(int(rs["child_pid"])) if p not in pids]
            except Exception:  # noqa: BLE001
                pass
        rss: Optional[int] = None
        cpu: Optional[float] = None
        try:
            import psutil  # type: ignore
            rss = 0
            # cpu_percent(interval=None) is meaningful only from the second call on the same Process object: keep
            # them per service between calls; a pid seen for the first time contributes no sample (all new -> null)
            with self._lock:
                prev = dict(self._cpu_procs.get(name) or {})
            keep: Dict[int, Any] = {}
            total, sampled = 0.0, False
            for p in pids:
                try:
                    pr = prev.get(p)
                    if pr is not None and not pr.is_running():
                        pr = None
                    fresh = pr is None
                    if fresh:
                        pr = psutil.Process(p)
                    rss += int(pr.memory_info().rss)
                    c = float(pr.cpu_percent(interval=None))
                    keep[p] = pr
                    if not fresh:
                        total += c
                        sampled = True
                except Exception:  # noqa: BLE001
                    continue
            with self._lock:
                if keep:
                    self._cpu_procs[name] = keep
                else:
                    self._cpu_procs.pop(name, None)
            cpu = round(total, 1) if sampled else None
        except Exception:  # noqa: BLE001
            if os.path.isdir("/proc/self"):
                rss = 0
                page = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096
                for p in pids:
                    try:
                        with open(f"/proc/{p}/statm") as f:
                            rss += int(f.read().split()[1]) * page
                    except (OSError, ValueError, IndexError):
                        continue
        gpu_mb: Optional[int] = None
        gpu_pids = list(pids)
        if (d or {}).get("container") and rs.get("state") in ALIVE:
            # §9.6: best-effort for containers: nvidia compute-app pids matched against `docker top <c> -eo pid`
            gpu_pids += [p for p in self._container_pids(str(d["container"])) if p not in gpu_pids]
        if gpu_pids and not os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
            try:
                from . import gpu
                pm = gpu.process_memory() or {}
                hit = [pm[p] for p in gpu_pids if p in pm]
                gpu_mb = int(sum(hit)) if hit else (0 if pm else None)
            except Exception:  # noqa: BLE001
                gpu_mb = None
        return {"pids": pids if rs.get("state") in ALIVE else [], "rss_total": rss if rs.get("state") in ALIVE
                else None, "cpu_pct": cpu if rs.get("state") in ALIVE else None, "gpu_memory_mb": gpu_mb}

    def _container_info(self, d: dict) -> Optional[dict]:
        c = d.get("container")
        if not c:
            return None
        cached = self._container_cache.get(c)
        if cached and time.monotonic() - cached[0] < CONTAINER_CACHE_S:
            return cached[1]
        info: Dict[str, Any] = {"name": c, "id": None, "state": None, "image": None}
        try:
            code, out, _e = _docker(["inspect", "--format", "{{json .}}", c], 10)
            if code == 0:
                j = json.loads(out.strip() or "{}")
                info.update(id=(j.get("Id") or "")[:12] or None, state=(j.get("State") or {}).get("Status"),
                            image=(j.get("Config") or {}).get("Image"))
        except (HiveError, ValueError):
            pass
        self._container_cache[c] = (time.monotonic(), info)
        return info

    def _status_now(self, name: str, tail_lines: int) -> dict:
        d, rs = self._get(name)
        with self._lock:
            r = dict(rs)
            h = dict(r.get("_health") or {})
        out = self._summary(name)
        healthy = r.get("state") == "running" and r.get("health") in ("healthy", "none")
        n = int(tail_lines) if healthy else max(int(tail_lines), STATUS_TAIL_UNHEALTHY)
        tail = ""
        if n > 0:
            tail = textlog.clean_terminal_text(util.decode_text(
                textlog.tail_lines(self._run_dir(name) / "output.log", n)))
        out.update({"definition": self.redacted(d), "defined_by": d.get("defined_by"),
                    "next_restart_at": r.get("next_restart_at"), "started_at": r.get("started_at"),
                    "health_detail": {"last_check": h.get("last_check"), "latency_ms": h.get("latency_ms"),
                                      "consecutive_failures": int(h.get("consecutive_failures") or 0),
                                      "last_error": h.get("last_error")},
                    "resources": self._resources(name, r, d), "container": self._container_info(d), "tail": tail})
        return out

    def _settled(self, name: str) -> bool:
        with self._lock:
            rs = self._rs.get(name)
            if rs is None:
                return True
            st, hl = rs.get("state"), rs.get("health")
        if st == "running" and hl in ("healthy", "none"):
            return True
        if st == "running" and hl == "unknown" and rs.get("adopted"):
            return False
        return st in ("failed", "stopped", "backoff")

    def _wait_until(self, name: str, wait: float, pred: Any, cancel: Optional[threading.Event] = None) -> None:
        end = time.monotonic() + max(0.0, float(wait))
        while time.monotonic() < end:
            if pred():
                return
            if cancel is not None and cancel.is_set():
                return
            with self._lock:
                self._cond.wait(min(0.1, max(0.0, end - time.monotonic())))

    def status(self, name: str, tail_lines: int = 30, wait: float = 0,
               cancel: Optional[threading.Event] = None) -> dict:
        """ServiceStatus; ``wait`` blocks until running+healthy/none, failed, stopped, backoff or the wait elapses
        (or ``cancel`` is set; the caller then reports ``cancelled``)."""
        self._get(name)
        if wait and wait > 0:
            self._wait_until(name, wait, lambda: self._settled(name), cancel)
        return self._status_now(name, tail_lines)

    def list(self) -> dict:
        """``{"services": [ServiceSummary]}`` in name order (rows pending removal show desired "removed")."""
        with self._lock:
            names = sorted(self._defs)
        rows = []
        for n in names:
            try:
                rows.append(self._summary(n))
            except HiveError:
                continue
        return {"services": rows}

    def logs(self, name: str, **job_logs_params: Any) -> dict:
        """service.logs: job.logs params + a short ``status``."""
        self._get(name)
        r = read_log(self._run_dir(name), {"output": "output.log"},
                     lambda: not self._alive(name), getattr(self.rt, "secrets", None), merged_hint="service",
                     **job_logs_params)
        s = self._summary(name)
        r["state"] = s["state"]
        r["status"] = {"state": s["state"], "health": s["health"], "pid": s["pid"], "restarts": s["restarts"],
                       "uptime_s": s["uptime_s"]}
        return r

    # ------------------------------------------------------------------------------------------- control
    def _wait_started(self, name: str, wait: float, cancel: Optional[threading.Event]) -> None:
        if wait <= 0:
            return
        t0 = time.monotonic()

        def pred() -> bool:
            with self._lock:
                rs = self._rs.get(name) or {}
                st, hl = rs.get("state"), rs.get("health")
            if st == "running" and hl in ("healthy", "none"):
                return True
            if st in ("failed", "backoff", "stopped"):
                return True
            d = self._defs.get(name) or {}
            if st == "running" and self._health_kind(d) == "none" and time.monotonic() - t0 >= NO_HEALTH_ALIVE_S:
                return True
            return False
        self._wait_until(name, wait, pred, cancel)

    def _result_status(self, name: str) -> dict:
        s = self._status_now(name, 0)
        healthy = s["state"] == "running" and s["health"] in ("healthy", "none")
        if not healthy:
            s["tail"] = textlog.clean_terminal_text(util.decode_text(
                textlog.tail_lines(self._run_dir(name) / "output.log", STATUS_TAIL_UNHEALTHY)))
        return s

    def start_service(self, ctx: "ActionContext", name: str, wait: float) -> dict:
        """service.start: from stopped/failed reset counters and spawn (calling identity); while already
        starting/running/unhealthy/backoff only wait."""
        self._get(name)
        with self._op(name):
            with self._lock:
                rs = self._rs[name]
                if rs.get("desired") == "removed":
                    raise conflict("service is being removed; retry in a few seconds")
                state = rs.get("state")
                prev_desired = rs.get("desired")
                rs["desired"] = "running"
                rs.pop("_manual", None)
            alive = self._alive(name)
            if alive and state == "stopping" and not rs.get("_respawn"):
                # a stop is in progress: let it finish, then spawn here with the calling identity (never leave the
                # start to the supervisor's system identity)
                alive = self._finish_stop_for_start(ctx, name, prev_desired)
                state = (self._rs.get(name) or {}).get("state")
            elif not alive and state in ALIVE and rs.get("shim_pid"):
                with self._lock:
                    rs["_manual"] = True    # the shim exited before the supervisor noticed: record it first
                try:
                    self._on_exit(name)
                finally:
                    with self._lock:
                        rs.pop("_manual", None)
                state = (self._rs.get(name) or {}).get("state")
            if (alive and state in ALIVE) or state == "backoff" or rs.get("_docker_wait_since"):
                self._save_rs(name)     # already starting/running/unhealthy/backoff (or stopping): only wait
            else:
                with self._lock:
                    rs.update(restarts=[], _short_runs=0)
                self._spawn(name, eff=ctx.policy, ctx=ctx, manual=True)
        self._wait_started(name, min(float(wait or 0), max(0.0, ctx.remaining() - 1.0)), ctx.cancel)
        return self._result_status(name)

    def _finish_stop_for_start(self, ctx: "ActionContext", name: str, prev_desired: Any) -> bool:
        """service.start during ``stopping`` (caller holds the op lock): wait for the stop sequence to end (force
        after stop_timeout + 7, as the supervisor would), bounded by the call's remaining time. Returns False once
        the shim is gone (state stopped, desired running, ready for a manual spawn); still alive -> the start is
        dropped (desired restored) and ``conflict`` (``cancelled`` when the call was cancelled)."""
        with self._lock:
            rs = self._rs[name]
            d = self._defs.get(name) or {}
            rs["_manual"] = True        # _on_exit keeps desired running and the supervisor spawns nothing
            stop_end = float(rs.get("_stop_mono") or _mono()) + float(d.get("stop_timeout", 15)) + STOP_EXTRA_S
        call_end = time.monotonic() + max(0.0, ctx.remaining() - 2.0)
        cancel = getattr(ctx, "cancel", None)
        try:
            while self._alive(name) and time.monotonic() < call_end and not (cancel is not None and cancel.is_set()):
                if _mono() >= stop_end:
                    self._force_kill(name)
                    end = min(call_end, time.monotonic() + 3.0)
                    while time.monotonic() < end and self._alive(name):
                        time.sleep(0.05)
                    break
                time.sleep(0.05)
            if not self._alive(name):
                with self._lock:
                    st = rs.get("state")
                if st in ALIVE:
                    self._on_exit(name)
                return False
        finally:
            with self._lock:
                rs.pop("_manual", None)
        with self._lock:
            rs["desired"] = prev_desired if prev_desired in ("stopped", "running") else "stopped"
        self._save_rs(name)
        if cancel is not None and cancel.is_set():
            raise cancelled("cancelled by the caller")
        raise conflict(f"service '{name}' is still stopping; start it again once it has stopped",
                       hint="service_status shows its state")

    def _stop_now(self, name: str, *, desired: str, manual_flag: bool) -> None:
        with self._lock:
            rs = self._rs[name]
            d = self._defs.get(name) or {}
            rs["desired"] = desired
            if manual_flag:
                rs["_manual"] = True
            state = rs.get("state")
        alive = self._alive(name)
        if alive:
            if state != "stopping":
                with self._lock:
                    rs["_stop_mono"] = _mono()
                proctree.request_stop(self._run_dir(name), float(d.get("stop_timeout", 15)))
                self._set(name, state="stopping")
        else:
            with self._lock:
                rs.pop("_docker_wait_since", None)
            if state in ALIVE or state == "backoff":
                self._on_exit(name)
            elif desired == "removed":
                self._delete(name, delete_logs=bool(rs.get("delete_logs")))
            else:
                self._set(name, state="stopped" if state != "failed" else "failed", next_restart_at=None)

    def stop_service(self, ctx: "ActionContext", name: str, wait: float) -> dict:
        """service.stop: desired stopped, shim stop sequence (stop_command, signal, force after stop_timeout+7)."""
        self._get(name)
        with self._op(name):
            self._stop_now(name, desired="stopped", manual_flag=False)
        w = min(float(wait or 0), max(0.0, ctx.remaining() - 1.0))
        self._wait_until(name, w, lambda: (self._rs.get(name) or {}).get("state") in ("stopped", "failed"),
                         ctx.cancel)
        return self._result_status(name)

    def _stop_for_restart(self, ctx: Optional["ActionContext"], name: str) -> None:
        d = self._defs.get(name) or {}
        self._stop_now(name, desired="running", manual_flag=True)
        limit = float(d.get("stop_timeout", 15)) + STOP_EXTRA_S
        if ctx is not None:
            limit = min(limit, max(1.0, ctx.remaining() - 2.0))
        end = time.monotonic() + limit
        while time.monotonic() < end and self._alive(name):
            time.sleep(0.05)
        if self._alive(name):
            self._force_kill(name)
            end = time.monotonic() + 3
            while time.monotonic() < end and self._alive(name):
                time.sleep(0.05)
        with self._lock:
            rs = self._rs.get(name) or {}
            st = rs.get("state")
        if st in ALIVE and not self._alive(name):
            self._on_exit(name)

    def restart(self, ctx: "ActionContext", name: str, wait: float) -> dict:
        """service.restart: always respawns (stop sequence first when running), with the calling identity."""
        self._get(name)
        with self._op(name):
            with self._lock:
                if self._rs[name].get("desired") == "removed":
                    raise conflict("service is being removed; retry in a few seconds")
            self._stop_for_restart(ctx, name)
            if self._alive(name):
                with self._lock:
                    self._rs[name].pop("_manual", None)
                raise conflict(f"service '{name}' did not stop in time; try again",
                               hint="service_status shows its state")
            with self._lock:
                rs = self._rs[name]
                rs.pop("_manual", None)
                rs["desired"] = "running"
            self._spawn(name, eff=ctx.policy, ctx=ctx, manual=True)
        self._wait_started(name, min(float(wait or 0), max(0.0, ctx.remaining() - 1.0)), ctx.cancel)
        return self._result_status(name)

    def remove(self, ctx: "ActionContext", name: str, delete_logs: bool, wait: float) -> dict:
        """service.remove: desired removed + stop sequence; deleted at once when stopped within ``wait``, else the
        supervisor deletes it when the shim exits (pending)."""
        self._get(name)
        with self._op(name):
            with self._lock:
                self._rs[name]["delete_logs"] = bool(delete_logs)
            self._view = None
            self._stop_now(name, desired="removed", manual_flag=False)
        w = min(float(wait or 0), max(0.0, ctx.remaining() - 1.0))
        self._wait_until(name, w, lambda: name not in self._defs, ctx.cancel)
        with self._lock:
            gone = name not in self._defs
        if gone:
            return {"removed": name, "pending": False}
        return {"removed": name, "pending": True, "state": "stopping"}

    # ------------------------------------------------------------------------------------------- define
    def _preset_ctx(self, eff: Any) -> "presets.PresetCtx":
        names: List[str] = []
        sec = getattr(self.rt, "secrets", None)
        try:
            names = [n["name"] for n in (sec.names() if sec is not None else []) if n.get("exec", True)]
        except Exception:  # noqa: BLE001
            names = []
        try:
            from . import sysinfo
            caps = sysinfo.capabilities() or {}
        except Exception:  # noqa: BLE001
            caps = {}
        # WP4's capabilities() decides; without it (P0 stub: {}) a docker CLI on the trusted PATH counts
        docker = bool(caps["docker"]) if "docker" in caps else _docker_exe() is not None
        return presets.PresetCtx(os=_platform(), node_id=getattr(self.rt, "node_id", "") or "", policy=eff,
                                 secret_names=names, docker=docker,
                                 mask=getattr(sec, "mask", None) or presets._identity_mask)

    @staticmethod
    def _code_cwd(d: dict, eff: Any) -> Optional[str]:
        """The cwd a spawn of ``d`` uses (EffectivePolicy.check_cwd: relative -> workdir, ``~`` and the fixed
        variables expanded; none -> workdir); a cwd that does not resolve now is still expanded the same way."""
        cwd = d.get("cwd")
        if eff is not None:
            try:
                return str(eff.check_cwd(cwd))
            except HiveError:
                pass
            except Exception:  # noqa: BLE001
                pass
            if cwd:
                expand = getattr(eff, "_expand", None)
                try:
                    if callable(expand):
                        return os.path.realpath(expand(str(cwd)))
                except Exception:  # noqa: BLE001
                    pass
                wd = getattr(eff, "workdir", None)
                try:
                    base = str(wd() if callable(wd) else wd) if wd is not None else None
                except Exception:  # noqa: BLE001
                    base = None
                if base:
                    return os.path.realpath(os.path.join(base, os.path.expanduser(str(cwd))))
        if cwd:
            return os.path.abspath(os.path.expanduser(os.path.expandvars(str(cwd))))
        return None

    def _code_dirs(self, d: dict, node_path: str, eff: Any = None) -> List[str]:
        """Folders holding a service's code, resolved the way a spawn resolves them (§5.4 step 7): argv[0] (or the
        first command word) through build_argv with the spawn cwd (so ``tools/serve.sh`` counts), its venv prefix,
        and the cwd (the workdir when none is given). ``eff`` resolves the cwd (None: expanded as given)."""
        out: List[str] = []
        cwd = self._code_cwd(d, eff)
        first: Optional[str] = None
        if d.get("argv"):
            first = str(d["argv"][0])
        elif d.get("command"):
            from .jobs import _first_word
            first = _first_word(str(d["command"]))
        if first:
            exe: Optional[str]
            try:
                exe = proctree.build_argv([first], node_path, cwd=cwd)[0]
            except HiveError:
                exe = None
            except Exception:  # noqa: BLE001
                exe = None
            if exe is None and (os.sep in first or (os.altsep and os.altsep in first)):
                # not there (yet): the folder it would run from still counts
                exe = first if os.path.isabs(first) else os.path.join(cwd or os.getcwd(), first)
            if exe:
                is_python = os.path.basename(exe).lower().startswith("python")
                # a venv python is a symlink to the base interpreter: keep its own path so the venv prefix counts
                exe = os.path.abspath(exe) if is_python else os.path.realpath(exe)
                parent = os.path.dirname(exe)
                out.append(parent)
                if os.path.basename(parent) in ("bin", "Scripts") and is_python:
                    out.append(os.path.dirname(parent))
        if cwd:
            out.append(cwd)
        uniq: List[str] = []
        for x in out:
            if x not in uniq:
                uniq.append(x)
        return uniq

    def _validate(self, d: dict, eff: Any) -> List[str]:
        warnings: List[str] = []
        if d.get("argv"):
            if is_wsl_exe(str(d["argv"][0])):
                raise bad_request(WSL_ERROR, hint=self._wsl_hint())
        elif d.get("command"):
            from .jobs import _first_word
            if is_wsl_exe(_first_word(str(d["command"]))):
                raise bad_request(WSL_ERROR, hint=self._wsl_hint())
        else:
            raise bad_request("give exactly one of command, argv or preset")
        for key in ("endpoint", "health_url"):
            if d.get(key):
                check_base_url(d[key], key)
        if d.get("ready_pattern"):
            from .fsops import check_regex
            check_regex(str(d["ready_pattern"]))
        kind = self._health_kind(d)
        if kind == "http" and not self._health_url(d):
            raise bad_request("health=http needs health_url or endpoint")
        if kind == "tcp" and not d.get("port"):
            raise bad_request("health=tcp needs port")
        expect = list(d.get("health_expect") or [200, 399])
        if len(expect) != 2 or int(expect[0]) > int(expect[1]):
            raise bad_request("health_expect must be [low, high] (an inclusive range)")
        built, _stop, _post, _cwd, _env, _files = self._build(d, eff)
        sec = getattr(self.rt, "secrets", None)
        akey = d.get("api_key_env")
        if akey and akey not in (d.get("env") or {}):
            known = []
            try:
                known = [n["name"] for n in sec.names()] if sec is not None else []
            except Exception:  # noqa: BLE001
                known = []
            if akey not in known:
                raise not_found(f"api_key_env {akey!r} is neither a node secret nor a key of this service's env",
                                hint=f"ask the owner to run on that machine: hive node secret set {akey}")
        rx = _secret_re()
        for k in sorted(d.get("env") or {}):
            if rx.search(k):
                warnings.append(SECRET_ENV_WARNING.format(k=k))
        warnings += keep_awake_warnings(bool(d.get("keep_awake")))
        home = os.path.abspath(os.path.expanduser("~"))
        roots = []
        try:
            roots = [os.path.abspath(os.path.expanduser(str(r.get("path")))) for r in eff.roots()
                     if r.get("path") and r.get("path") != "*"]
        except Exception:  # noqa: BLE001
            roots = []
        implicit = None if d.get("cwd") else self._code_cwd(d, eff)   # the default workdir: no warning of its own
        for cd in self._code_dirs(d, eff.node_path, eff):
            if implicit is not None and cd == implicit:
                continue
            ncd = os.path.normcase(cd.rstrip("/\\") or cd)
            bases = [os.path.normcase(x.rstrip("/\\") or x) for x in [home] + roots]
            if any(ncd == b or b.startswith(ncd.rstrip(os.sep) + os.sep) for b in bases):
                warnings.append(CODE_DIR_WARNING.format(d=cd))
        return warnings

    def _wsl_hint(self) -> str:
        return WSL_HINT

    def define(self, ctx: "ActionContext", d: dict) -> dict:
        """service.define: expand the preset, validate (CommandGuard on the built argv and stop_command, cwd, env,
        URLs, ready_pattern), store, and (start=true) start or restart it."""
        params = dict(d)
        name = str(params["name"])
        with self._lock:
            existing = self._defs.get(name)
            rs = self._rs.get(name)
            count = len(self._defs)
        if rs is not None and rs.get("desired") == "removed":
            raise conflict("service is being removed; retry in a few seconds")
        if existing is not None and not params.get("replace", True):
            raise conflict(f"service '{name}' already exists", hint="pass replace=true to update it")
        lim = self._limits(ctx.policy)
        if existing is None and count >= int(getattr(lim, "services_max", 32) or 32):
            raise bad_request(f"this node allows at most {getattr(lim, 'services_max', 32)} services",
                              hint="remove one first (service_control remove)")
        definition = presets.expand_define(params, ctx=self._preset_ctx(ctx.policy), given=ctx.given)
        definition = {k: v for k, v in definition.items() if v is not None}
        warnings = self._validate(definition, ctx.policy)
        definition["defined_by"] = getattr(ctx.identity, "display_name", None) or ctx.identity.name
        definition["defined_at"] = util.iso_utc()
        ignore = ("defined_by", "defined_at")
        changed: List[str] = []
        if existing is not None:
            changed = sorted(k for k in set(existing) | set(definition)
                             if k not in ignore and existing.get(k) != definition.get(k))
        created = existing is None
        holds = getattr(self.rt, "holds", None)
        approved: Dict[str, Optional[str]] = {}
        if ctx.hold and holds is not None:
            try:
                approved = holds.fingerprints(ctx.hold)
            except Exception:  # noqa: BLE001
                approved = {}
        with self._op(name):
            with self._lock:
                self._defs[name] = definition
                if name not in self._rs:
                    nrs = self._new_rs()
                    nrs.update(_gen=0, _health={"last_check": None, "latency_ms": None, "consecutive_failures": 0,
                                                "last_error": None})
                    self._rs[name] = nrs
                # v8.1: a new definition replaces the approved fingerprints with those its own approval bound
                # (none for an ungated define); autostart and crash restarts check them
                self._rs[name]["approved_files"] = approved
                self._view = None
            self._save_defs()
            self._save_rs(name)
        restarted = False
        if params.get("start", True):
            alive = self._alive(name)
            if alive and changed:
                self.restart(ctx, name, 0)
                restarted = True
            elif alive or (self._rs.get(name) or {}).get("state") in ("backoff",) and not changed:
                with self._lock:
                    self._rs[name]["desired"] = "running"
                self._save_rs(name)
            else:
                with self._op(name):
                    with self._lock:
                        self._rs[name].update(restarts=[], _short_runs=0, desired="running")
                        self._rs[name].pop("_manual", None)
                    try:
                        self._spawn(name, eff=ctx.policy, ctx=ctx, manual=True)
                    except HiveError as e:
                        # v8.1 (external review round 2): a NEW definition the node refused to start because its
                        # port is taken is not kept (it would sit as "failed" and the next define says "updated")
                        if created and e.code == "conflict" and e.details.get("port"):
                            self._delete(name, delete_logs=True)
                            raise conflict(e.message + "; the service was not saved", hint=e.hint,
                                           port=e.details.get("port"))
                        raise
            self._wait_started(name, min(float(params.get("wait") or 0), max(0.0, ctx.remaining() - 1.0)),
                               ctx.cancel)
        out = self._result_status(name)
        out.update({"created": created, "changed": changed, "restarted": restarted,
                    "definition": self.redacted(definition), "warnings": warnings})
        return out

    # ------------------------------------------------------------------------------------------- model endpoints
    def _auth_for(self, d: dict) -> Optional["AuthSpec"]:
        key = d.get("api_key_env")
        if not key:
            return None
        env = d.get("env") or {}
        if key in env:
            from .netutil import AuthSpec
            host = urlsplit(str(d.get("endpoint") or "")).hostname or ""
            hosts = ("loopback",) if util.is_loopback_host(host) else (host,)
            return AuthSpec(name=str(key), header="Authorization", value="Bearer " + str(env[key]), hosts=hosts)
        return self.rt.secrets.resolve_auth(str(key))

    def endpoint_for(self, name: str) -> Tuple[str, str, Optional["AuthSpec"]]:
        """(base_url, api "openai"|"ollama", auth) for model.* and the gateway: a managed service of that name first,
        else the node's ``model_endpoints`` entry of that name; neither -> not_found listing both."""
        with self._lock:
            d = dict(self._defs.get(name) or {})
            managed = sorted(n for n, x in self._defs.items() if x.get("endpoint"))
        if d.get("endpoint"):
            return str(d["endpoint"]), str(d.get("endpoint_kind") or "openai"), self._auth_for(d)
        eps = list(getattr(self._policy(), "model_endpoints", None) or [])
        for e in eps:
            if e.get("name") == name and e.get("base_url"):
                auth = None
                if e.get("api_key_env"):
                    auth = self.rt.secrets.resolve_auth(str(e["api_key_env"]))
                return str(e["base_url"]), "openai", auth
        known = managed + sorted(str(e.get("name")) for e in eps if e.get("name"))
        reason = " has no endpoint" if d else " not found"
        raise not_found(f"model service '{name}'{reason}",
                        hint=("known model services: " + ", ".join(known)) if known else
                        "define one with service_define (e.g. preset=vllm) or ask the owner to add model_endpoints")

    def endpoints(self) -> List[dict]:
        """Managed services with an endpoint, as model.endpoints rows (source "service")."""
        out = []
        with self._lock:
            items = [(n, dict(d), dict(self._rs.get(n) or {})) for n, d in sorted(self._defs.items())]
        for n, d, rs in items:
            if not d.get("endpoint"):
                continue
            health = rs.get("health")
            healthy = True if health == "healthy" else False if health == "unhealthy" else None
            if rs.get("state") not in ALIVE:
                healthy = False
            out.append({"name": n, "source": "service", "api": d.get("endpoint_kind") or "openai",
                        "server": _server_of(d), "base_url": d["endpoint"], "healthy": healthy, "models": None})
        return out

    def policy_view(self) -> dict:
        """``{"endpoints": [...], "code_dirs": [...], "code_files": [...]}`` of the defined services (cached; rebuilt
        when services.json changes or a define/remove/approved start happens). v8.1: ``code_files`` are the files the
        services' approvals bound (run state ``approved_files``), not writable by gated identities."""
        try:
            mt = self.defs_path.stat().st_mtime
        except OSError:
            mt = None
        view = self._view
        if view is not None and mt == self._view_mtime:
            return view
        pol = self._policy()
        node_path = getattr(pol, "node_path", None) or os.environ.get("PATH", "")
        try:
            eff = self._system_eff()     # resolves cwd like a spawn does (relative -> workdir, ~ and variables)
        except Exception:  # noqa: BLE001
            eff = None
        with self._lock:
            defs = [dict(d) for d in self._defs.values()]
            files: List[str] = []
            for n in self._defs:
                for f in (self._rs.get(n) or {}).get("approved_files") or {}:
                    if f not in files:
                        files.append(str(f))
        eps: List[str] = []
        dirs: List[str] = []
        for d in defs:
            if d.get("endpoint") and d["endpoint"] not in eps:
                eps.append(str(d["endpoint"]))
            for cd in self._code_dirs(d, node_path, eff):
                if cd not in dirs:
                    dirs.append(cd)
        view = {"endpoints": eps, "code_dirs": dirs, "code_files": files}
        self._view, self._view_mtime = view, mt
        return view

    def managed_pids(self) -> Dict[int, str]:
        """shim/child pid -> "service:<name>" for proc.list and the kill guard."""
        out: Dict[int, str] = {}
        with self._lock:
            for n, rs in self._rs.items():
                if rs.get("state") not in ALIVE:
                    continue
                for k in ("shim_pid", "child_pid"):
                    if rs.get(k):
                        out[int(rs[k])] = f"service:{n}"
        return out

    def runtime(self, name: str) -> Dict[str, Any]:
        """A copy of the runtime record (tests, doctor)."""
        _d, rs = self._get(name)
        with self._lock:
            return {k: v for k, v in rs.items() if not k.startswith("_")}


def _server_of(d: dict) -> str:
    """model.endpoints ``server`` from a definition (§9.9: the preset or ``endpoint_kind`` first); "unknown" leaves
    it to WP3's probe (``owned_by`` of /v1/models, else "openai")."""
    if d.get("preset") in ("vllm", "sglang", "ollama", "llamacpp"):
        return str(d["preset"])
    if d.get("endpoint_kind") == "ollama":
        return "ollama"
    return "unknown"


def _platform() -> str:
    if util.IS_WINDOWS:
        return "windows"
    if util.IS_MAC:
        return "darwin"
    return "linux"


__all__ = ["ServiceManager", "run_stop_command", "panic_offline", "check_base_url"]
