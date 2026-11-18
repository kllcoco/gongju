"""WSL detection, distro listing, Windows-side keepalive (design §12.1, §12.4 and §20.7, WP4).

Inside WSL2 (a Linux node): ``is_wsl()`` and ``wsl_info()``. On Windows: ``list_distros()`` (``wsl.exe -l -v``) and
``Keepalive``, which keeps one ``wsl.exe -d <distro> --exec /bin/sleep infinity`` (or the entry's ``command``) per
configured distro running, spawned through ``proctree.spawn_detached`` so it is outside the Scheduled Task's job and
survives node restarts (adopted from ``state/wsl-keepalive-<distro>.json`` at the next start).
"""
from __future__ import annotations

import logging
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import util

log = logging.getLogger("hive.wsl")

IS_WINDOWS = os.name == "nt"

# test seams (fixture files with the same content)
_INTEROP = "/proc/sys/fs/binfmt_misc/WSLInterop"
_OSRELEASE = "/proc/sys/kernel/osrelease"
_PID1_COMM = "/proc/1/comm"

LIST_TTL = 60.0
BACKOFF_MIN = 10.0
BACKOFF_MAX = 300.0
STABLE_AFTER = 300.0       # a keepalive that ran this long resets its backoff

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()

# English and common localized state words of `wsl.exe -l -v` (the header and states follow the UI language)
_STATES = {"running": "Running", "stopped": "Stopped", "installing": "Installing", "converting": "Converting",
           "uninstalling": "Uninstalling", "正在运行": "Running", "已停止": "Stopped", "執行中": "Running",
           "已停止運作": "Stopped", "wird ausgeführt": "Running", "beendet": "Stopped"}


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def is_wsl() -> bool:
    """Running inside WSL2: ``/proc/sys/fs/binfmt_misc/WSLInterop`` exists or ``microsoft`` in the kernel release."""
    if IS_WINDOWS or sys.platform == "darwin":
        return False
    if os.path.exists(_INTEROP) or os.path.exists(_INTEROP + "-late"):
        return True
    rel = _read(_OSRELEASE)
    return bool(rel and "microsoft" in rel.lower())


def interop_enabled() -> bool:
    """Inside WSL: Windows interop is on (``WSLInterop`` registered)."""
    return os.path.exists(_INTEROP) or os.path.exists(_INTEROP + "-late")


def _wslinfo() -> Optional[str]:
    from . import proctree
    p = proctree.which("wslinfo", proctree.trusted_path())
    if p:
        return p
    for f in ("/usr/bin/wslinfo", "/bin/wslinfo"):
        if os.path.exists(f):
            return f
    return None


def networking_mode() -> Optional[str]:
    """``wslinfo --networking-mode`` -> "nat" | "mirrored" | None (older WSL has no wslinfo)."""
    exe = _wslinfo()
    if not exe:
        return None
    from . import proctree
    try:
        code, out, _err = proctree.run_quick([exe, "--networking-mode"], timeout=3)
    except Exception:  # noqa: BLE001
        return None
    if code != 0:
        return None
    word = util.decode_text(out).strip().lower()
    return word if word in ("nat", "mirrored") else None


def systemd_running() -> bool:
    """PID 1 is systemd."""
    comm = (_read(_PID1_COMM) or "").strip()
    return comm == "systemd"


def wsl_info() -> Optional[dict]:
    """``{distro, networking, systemd}`` inside WSL (cached 60 s), else None."""
    if not is_wsl():
        return None

    def build() -> dict:
        return {"distro": os.environ.get("WSL_DISTRO_NAME") or None, "networking": networking_mode(),
                "systemd": systemd_running()}
    return dict(_cached("wsl_info", LIST_TTL, build))


def _cached(key: str, ttl: float, fn: Callable[[], Any]) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    """Forget cached distro lists / WSL info (tests)."""
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------------------------------------------
# wsl.exe output

def decode_output(data: bytes) -> str:
    """wsl.exe writes UTF-16LE unless ``WSL_UTF8=1`` is honoured (newer versions); handle both, strip BOM/NULs."""
    if not data:
        return ""
    if data.startswith(b"\xff\xfe") or (len(data) >= 2 and data[1:2] == b"\x00" and data[0:1] != b"\x00"):
        text = data.decode("utf-16-le", "replace")
    else:
        text = util.decode_text(data)
    return text.replace("\ufeff", "").replace("\x00", "")


def parse_list_verbose(data: bytes) -> List[dict]:
    """``wsl.exe -l -v`` -> ``[{name, state, version}]`` (header line skipped; ``*`` marks the default)."""
    rows = []
    lines = [ln for ln in decode_output(data).splitlines() if ln.strip()]
    for ln in lines[1:]:
        s = ln.strip()
        if s.startswith("*"):
            s = s[1:].strip()
        parts = s.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        ver: Any = None
        rest = parts[1:]
        if rest and rest[-1].isdigit():
            ver = int(rest[-1])
            rest = rest[:-1]
        raw_state = " ".join(rest)
        state = _STATES.get(raw_state.lower(), _STATES.get(raw_state, raw_state or "Unknown"))
        rows.append({"name": name, "state": state, "version": ver})
    return rows


def parse_list_quiet(data: bytes) -> List[str]:
    """``wsl.exe -l -q`` -> distro names."""
    return [ln.strip() for ln in decode_output(data).splitlines() if ln.strip()]


def wsl_exe() -> Optional[str]:
    """Windows: path of wsl.exe (trusted PATH, then %SystemRoot%\\System32); None elsewhere."""
    if not IS_WINDOWS:
        return None
    from . import proctree  # pragma: no cover - Windows only
    p = proctree.which("wsl.exe", proctree.trusted_path())  # pragma: no cover
    if p:  # pragma: no cover
        return p
    f = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "wsl.exe")  # pragma: no cover
    return f if os.path.exists(f) else None  # pragma: no cover


def _run_wsl(args: List[str], timeout: float = 5.0, exe: Optional[str] = None) -> Optional[bytes]:
    exe = exe or wsl_exe()
    if not exe:
        return None
    from . import proctree
    env = dict(os.environ)
    env["WSL_UTF8"] = "1"
    try:
        code, out, _err = proctree.run_quick([exe] + list(args), timeout=timeout, env=env)
    except Exception:  # noqa: BLE001
        return None
    return out if code == 0 else None


def list_distros() -> List[dict]:
    """Windows: installed distros ``[{name, state, version}]`` (``wsl.exe -l -v``, WSL_UTF8=1, 5 s, cached 60 s);
    ``[]`` elsewhere or when WSL is not installed."""
    if not IS_WINDOWS:
        return []

    def build() -> List[dict]:  # pragma: no cover - Windows only
        out = _run_wsl(["-l", "-v"])
        return parse_list_verbose(out) if out else []
    return [dict(r) for r in _cached("list_distros", LIST_TTL, build)]


def has_distros() -> bool:
    """Windows: ``wsl.exe -l -q`` lists at least one distro (5 s, cached 60 s)."""
    if not IS_WINDOWS:
        return False

    def build() -> bool:  # pragma: no cover - Windows only
        out = _run_wsl(["-l", "-q"])
        return bool(out and parse_list_quiet(out))
    return bool(_cached("has_distros", LIST_TTL, build))


def version_line() -> Optional[str]:
    """Windows: first line of ``wsl.exe --version`` (None when unavailable)."""
    if not IS_WINDOWS:
        return None
    out = _run_wsl(["--version"])  # pragma: no cover - Windows only
    if not out:  # pragma: no cover - Windows only
        return None
    lines = [ln.strip() for ln in decode_output(out).splitlines() if ln.strip()]  # pragma: no cover
    return lines[0] if lines else None  # pragma: no cover


# ---------------------------------------------------------------------------------------------------------------
# keepalive (Windows node; testable anywhere with a fake wsl executable)

def _safe_name(distro: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", distro)[:80] or "distro"


class Keepalive:
    """Windows-side WSL keepalive processes (spawned with proctree.spawn_detached, §20.7 step 6).

    ``state_dir`` holds ``wsl-keepalive-<distro>.json`` (pid + ctime) for adoption; default: the node data dir's
    ``state`` folder. ``wsl_exe`` and ``clock`` are test seams.
    """

    def __init__(self, state_dir: Optional[Path] = None, *, wsl_exe: Optional[str] = None,
                 clock: Callable[[], float] = time.monotonic):
        if state_dir is None:
            from .config import default_paths
            state_dir = default_paths()["node_data"] / "state"
        self.state_dir = Path(state_dir)
        self._exe = wsl_exe
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: Dict[str, dict] = {}

    # -- helpers
    def _state_path(self, distro: str) -> Path:
        return self.state_dir / f"wsl-keepalive-{_safe_name(distro)}.json"

    def _argv(self, entry: dict) -> List[str]:
        exe = self._exe or wsl_exe()
        if not exe:
            raise RuntimeError("wsl.exe not found")
        cmd = entry.get("command")
        tail = [str(c) for c in cmd] if isinstance(cmd, list) and cmd else ["/bin/sleep", "infinity"]
        return [exe, "-d", str(entry["distro"]), "--exec"] + tail

    def _spawn(self, distro: str) -> None:
        st = self._entries[distro]
        from . import proctree
        env = dict(os.environ)
        env["WSL_UTF8"] = "1"
        st["attempts"] += 1
        st["last_try"] = self._clock()
        try:
            argv = self._argv(st["entry"])
            pid, ctime = proctree.spawn_detached(argv, env=env, cwd=self.state_dir.parent)
        except Exception as e:  # noqa: BLE001 - recorded, retried with backoff
            st["error"] = str(e)[:300]
            st["pid"], st["ctime"] = None, None
            log.warning("WSL keepalive for %s could not start: %s", distro, e)
            return
        st.update(pid=pid, ctime=ctime, started=self._clock(), error=None)
        try:
            util.secure_mkdir(self.state_dir)
            util.write_json_atomic(self._state_path(distro), {"distro": distro, "pid": pid, "ctime": ctime,
                                                              "argv": argv, "started_at": util.iso_utc()},
                                   private=True)
        except OSError as e:
            log.warning("cannot record WSL keepalive state: %s", e)
        log.info("WSL keepalive for %s started (pid %s)", distro, pid)

    def _alive(self, st: dict) -> bool:
        pid = st.get("pid")
        return bool(pid) and util.pid_alive(int(pid), st.get("ctime"))

    # -- API
    def start(self, entries: List[dict]) -> None:
        """Start or adopt one keepalive per entry ``{"distro", "command"?}``."""
        with self._lock:
            for e in entries or []:
                if not isinstance(e, dict) or not e.get("distro"):
                    continue
                distro = str(e["distro"])
                st = {"entry": dict(e), "pid": None, "ctime": None, "started": None, "backoff": BACKOFF_MIN,
                      "next_try": 0.0, "attempts": 0, "last_try": None, "error": None, "adopted": False}
                self._entries[distro] = st
                rec = util.read_json(self._state_path(distro), default=None)
                if isinstance(rec, dict) and isinstance(rec.get("pid"), int) and \
                        util.pid_alive(rec["pid"], rec.get("ctime")):
                    st.update(pid=rec["pid"], ctime=rec.get("ctime"), started=self._clock(), adopted=True)
                    log.info("WSL keepalive for %s adopted (pid %s)", distro, rec["pid"])
                    continue
                self._spawn(distro)
                if st.get("pid") is None:
                    self._defer(st, self._clock())

    def _defer(self, st: dict, now: float) -> None:
        st["next_try"] = now + st["backoff"]
        st["backoff"] = min(BACKOFF_MAX, st["backoff"] * 2)

    def check(self) -> None:
        """Restart dead keepalives with backoff 10 -> 300 s (called every 10 s by the node scheduler)."""
        with self._lock:
            now = self._clock()
            for distro, st in self._entries.items():
                if self._alive(st):
                    if st.get("started") is not None and now - st["started"] >= STABLE_AFTER:
                        st["backoff"] = BACKOFF_MIN
                    continue
                if st.get("pid") is not None:
                    log.warning("WSL keepalive for %s (pid %s) ended; restarting in %.0f s", distro, st["pid"],
                                st["backoff"])
                    st["pid"], st["ctime"] = None, None
                    self._defer(st, now)
                    continue
                if now < st.get("next_try", 0.0):
                    continue
                self._spawn(distro)
                if st.get("pid") is None:
                    self._defer(st, now)

    def status(self) -> List[dict]:
        """``[{"distro","pid","running"}]``."""
        with self._lock:
            return [{"distro": d, "pid": st.get("pid"), "running": self._alive(st)}
                    for d, st in sorted(self._entries.items())]

    def pids(self) -> Dict[int, str]:
        """``{pid: "wsl-keepalive:<distro>"}`` of live keepalives (protected from proc.kill, §5.4)."""
        with self._lock:
            return {int(st["pid"]): f"wsl-keepalive:{d}" for d, st in self._entries.items() if self._alive(st)}


__all__ = ["is_wsl", "wsl_info", "list_distros", "Keepalive", "interop_enabled", "networking_mode",
           "systemd_running", "decode_output", "parse_list_verbose", "parse_list_quiet", "has_distros",
           "version_line", "wsl_exe", "clear_cache"]
