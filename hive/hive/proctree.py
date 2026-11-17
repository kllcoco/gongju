"""Shells, argv building, shim/detached spawning (spawn modes), kill_tree, pid identity, descendants, trusted_path
(design §10.1, WP2).

``pid_alive``/``process_create_time`` are re-exported from util unchanged (v5). ``set_trusted_path``/``trusted_path``
hold the node's probe PATH (Policy.trusted_path) with a fail-safe default.
Module rule: imports only stdlib, ``hive.util``, ``hive.errors`` and ``hive.winapi`` at module level (the shim imports
this module).
"""
from __future__ import annotations

import base64
import logging
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import util, winapi
from .errors import HiveError, not_found, timeout as timeout_error, unsupported

log = logging.getLogger("hive.proctree")

pid_alive = util.pid_alive                       # v5: defined in util (P0, §4.2) and re-exported here unchanged
process_create_time = util.process_create_time

# fixed tool locations of §12.2 (always trusted for internal probes)
FIXED_TOOL_DIRS_POSIX = ("/usr/lib/wsl/lib", "/usr/bin")
FIXED_TOOL_DIRS_WINDOWS = (r"C:\Windows\System32", r"C:\Program Files\NVIDIA Corporation\NVSMI")
# system shell locations searched in addition to the trusted PATH (never a folder agents can write)
SHELL_DIRS_POSIX = ("/bin", "/usr/bin", "/usr/local/bin", "/opt/homebrew/bin", "/usr/sbin", "/sbin")

PS_PREFIX = ("$ProgressPreference='SilentlyContinue';[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
             "$OutputEncoding=[Console]::OutputEncoding;$global:LASTEXITCODE=0;\n")
PS_SUFFIX = "\nif(-not $?){if($global:LASTEXITCODE){exit $global:LASTEXITCODE};exit 1};exit $global:LASTEXITCODE"
PS_MAX_ENCODED = 30000
POSIX_SHELLS = ("bash", "sh", "zsh")
SHELL_NAMES = ("bash", "sh", "zsh", "pwsh", "powershell", "cmd")

_trusted_path: Optional[str] = None
_shell_cache: Dict[str, Any] = {"t": 0.0, "rows": None}
_shell_lock = threading.Lock()
_detached: List[subprocess.Popen] = []
_detached_lock = threading.Lock()


# ---------------------------------------------------------------------------------------------------------------
# trusted PATH (internal probes)

def set_trusted_path(path: str) -> None:
    """Set the PATH used for internal probes (NodeRuntime.start passes Policy.trusted_path)."""
    global _trusted_path
    _trusted_path = path


def _is_under(child: str, parent: str) -> bool:
    try:
        c = os.path.normcase(os.path.realpath(child))
        p = os.path.normcase(os.path.realpath(parent))
    except OSError:
        return False
    if not p:
        return False
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _is_root(p: str) -> bool:
    try:
        r = os.path.realpath(p)
    except OSError:
        r = p
    return os.path.dirname(r) == r


def _default_trusted_path() -> str:
    excluded = []
    try:
        excluded.append(str(Path.home()))
    except (RuntimeError, KeyError):
        pass
    try:
        excluded.append(os.getcwd())
    except OSError:
        pass
    excluded.append(tempfile.gettempdir())
    # a filesystem root as home/cwd/temp (cwd "/" in containers, "C:\\", HOME=/) would exclude every PATH entry;
    # such a base is skipped (it is not "a writable folder the agent chose", PHASE0_NOTES)
    excluded = [e for e in excluded if e and not _is_root(e)]
    out: List[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        if any(_is_under(d, e) for e in excluded):
            continue
        if d not in out:
            out.append(d)
    fixed = FIXED_TOOL_DIRS_WINDOWS if util.IS_WINDOWS else FIXED_TOOL_DIRS_POSIX
    for d in fixed:
        if d not in out:
            out.append(d)
    return os.pathsep.join(out)


def trusted_path() -> str:
    """The value set by set_trusted_path(), else the fail-safe default (PATH minus home/cwd/temp + fixed tool dirs)."""
    if _trusted_path is not None:
        return _trusted_path
    return _default_trusted_path()


# ---------------------------------------------------------------------------------------------------------------
# executable resolution

def _pathext() -> List[str]:
    exts = [e.lower() for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
    return exts or [".com", ".exe", ".bat", ".cmd"]


def which(name: str, path: str) -> Optional[str]:
    """Resolve ``name`` against ``path`` only (never the cwd, also on Windows); absolute names are checked directly."""
    if not name:
        return None
    if os.path.isabs(name):
        cands = [name]
        if util.IS_WINDOWS and not os.path.splitext(name)[1]:  # pragma: no cover - Windows only
            cands = [name + e for e in _pathext()] + [name]
        for c in cands:
            if os.path.isfile(c) and (util.IS_WINDOWS or os.access(c, os.X_OK)):
                return c
        return None
    if os.sep in name or (os.altsep and os.altsep in name):
        return None   # relative paths with a separator are resolved by the caller against the command cwd
    if not util.IS_WINDOWS:
        return shutil.which(name, path=path)
    exts = _pathext()  # pragma: no cover - Windows only
    has_ext = os.path.splitext(name)[1].lower() in exts
    for d in (path or "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d or not os.path.isabs(d):
            continue
        for cand in ([name] if has_ext else []) + [name + e for e in exts]:
            p = os.path.join(d, cand)
            if os.path.isfile(p):
                return p
    return None


def _service_manager_hint() -> str:
    if os.environ.get("INVOCATION_ID") or os.environ.get("XPC_SERVICE_NAME") or os.environ.get("HIVE_WATCHDOG"):
        return ("PATH was captured when the service was installed; re-run `hive service install node` after "
                "installing new tools, or add the folder to exec.path_prepend")
    return "install it on the node or add its folder to policy.exec.path_prepend; system_info(which=[…]) shows the PATH"


def build_argv(argv: List[str], node_path: str, *, cwd: Optional[Union[str, Path]] = None) -> List[str]:
    """Resolve argv[0] with the node PATH only (never the agent env, never the cwd on Windows); returns a copy with
    the absolute path as argv[0]. A relative argv[0] with a separator (``./run.sh``) resolves against ``cwd``."""
    if not argv:
        raise not_found("empty argv")
    first = str(argv[0])
    resolved: Optional[str]
    if not os.path.isabs(first) and (os.sep in first or (os.altsep and os.altsep in first)):
        base = Path(cwd) if cwd is not None else Path.cwd()
        resolved = which(str((base / first).resolve()), node_path)
    else:
        resolved = which(first, node_path)
    if resolved is None:
        raise not_found(f"program '{first}' was not found on the node PATH", hint=_service_manager_hint(),
                        program=first)
    return [resolved] + [str(a) for a in argv[1:]]


# ---------------------------------------------------------------------------------------------------------------
# shells

def _shell_search_path() -> str:
    parts = [p for p in trusted_path().split(os.pathsep) if p]
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        root = os.environ.get("SystemRoot", r"C:\Windows")
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        extra = [os.path.join(pf, "PowerShell", "7"), os.path.join(root, "System32", "WindowsPowerShell", "v1.0"),
                 os.path.join(root, "System32")]
    else:
        extra = list(SHELL_DIRS_POSIX)
    for d in extra:
        if d not in parts:
            parts.append(d)
    return os.pathsep.join(parts)


def _git_bash() -> Optional[str]:  # pragma: no cover - Windows only
    for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432"), r"C:\Program Files"):
        if base:
            p = os.path.join(base, "Git", "bin", "bash.exe")
            if os.path.isfile(p):
                return p
    return None


def shell_path(name: str) -> Optional[str]:
    """Absolute path of a named shell (bash/sh/zsh/pwsh/powershell/cmd) or None when it is not installed."""
    search = _shell_search_path()
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        if name == "cmd":
            comspec = os.environ.get("ComSpec")
            if comspec and os.path.isfile(comspec) and os.path.basename(comspec).lower() == "cmd.exe":
                return comspec
            return which("cmd.exe", search)
        if name == "bash":
            return _git_bash() or which("bash.exe", search)
        if name == "powershell":
            return which("powershell.exe", search)
        if name == "pwsh":
            return which("pwsh.exe", search)
        return which(name + ".exe", search)
    if name in ("cmd", "powershell"):
        return None
    return which(name, search)


def default_shell() -> str:
    """POSIX: bash if found else sh; Windows: pwsh if found else powershell."""
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        return "pwsh" if shell_path("pwsh") else "powershell"
    return "bash" if shell_path("bash") else "sh"


def _shell_version(name: str, path: str) -> Optional[str]:
    if name not in ("bash", "zsh", "pwsh"):
        return None
    try:
        code, out, _err = run_quick([path, "--version"], timeout=3, max_output=4096)
    except HiveError:
        return None
    if code != 0:
        return None
    line = util.decode_text(out).strip().splitlines()
    return line[0].strip()[:120] if line else None


def available_shells() -> List[dict]:
    """``[{"name","path","version"|None}]`` of the installed shells (cached 5 min)."""
    with _shell_lock:
        if _shell_cache["rows"] is not None and time.monotonic() - _shell_cache["t"] < 300:
            return [dict(r) for r in _shell_cache["rows"]]
    rows: List[dict] = []
    for name in SHELL_NAMES:
        p = shell_path(name)
        if p:
            rows.append({"name": name, "path": p, "version": _shell_version(name, p)})
    with _shell_lock:
        _shell_cache.update(t=time.monotonic(), rows=rows)
    return [dict(r) for r in rows]


@dataclass
class ShellPlan:
    """Result of build_shell_argv. ``files`` (WP2 addition): run-dir files the argv refers to as ``{run_dir}/…``
    (the PowerShell ``-File`` fallback for long scripts); JobManager.start_job writes them (extra_files)."""
    argv: Union[List[str], str]
    shell: str
    env_extra: Dict[str, str]
    files: Dict[str, bytes] = field(default_factory=dict)


def resolve_shell(shell: str = "auto") -> str:
    """``auto`` -> default_shell(); other names unchanged."""
    return default_shell() if shell in (None, "", "auto") else shell


def powershell_encoded(command: str) -> str:
    """base64(utf16le(PREFIX + command + SUFFIX)) for ``-EncodedCommand``."""
    return base64.b64encode((PS_PREFIX + command + PS_SUFFIX).encode("utf-16-le")).decode("ascii")


def build_shell_argv(command: str, shell: str = "auto", login: bool = False) -> ShellPlan:
    """Shell argv per the §10.1 table; ``unsupported`` when the shell is not installed on this node."""
    name = resolve_shell(shell)
    if name not in SHELL_NAMES:
        raise unsupported(f"unknown shell '{name}'")
    path = shell_path(name)
    if path is None:
        hint = "available shells: " + ", ".join(r["name"] for r in available_shells())
        if name == "cmd" or name == "powershell":
            hint = f"{name} exists only on Windows nodes; " + hint
        raise unsupported(f"shell '{name}' is not installed on this node", hint=hint)
    if name in POSIX_SHELLS:
        flag = "-lc" if login and name in ("bash", "zsh") else "-c"
        return ShellPlan(argv=[path, flag, command], shell=name, env_extra={})
    if name in ("pwsh", "powershell"):
        base = [path, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass"]
        enc = powershell_encoded(command)
        if len(enc) <= PS_MAX_ENCODED:
            return ShellPlan(argv=base + ["-EncodedCommand", enc], shell=name, env_extra={})
        script = ("\ufeff" + PS_PREFIX + command + PS_SUFFIX).encode("utf-8")
        return ShellPlan(argv=base + ["-File", os.path.join("{run_dir}", "command.ps1")], shell=name, env_extra={},
                         files={"command.ps1": script})
    # cmd: a command-line STRING passed to CreateProcess as-is (no list2cmdline)
    return ShellPlan(argv=f'"{path}" /d /s /c "chcp 65001>nul & {command}"', shell="cmd", env_extra={})


# ---------------------------------------------------------------------------------------------------------------
# spawning

@dataclass
class ShimHandle:
    """A started shim."""
    shim_pid: int
    shim_ctime: Optional[float]
    popen: Optional[subprocess.Popen]
    run_dir: Path


def _helper_argv(extra: List[str]) -> List[str]:
    cmd = list(util.self_command())
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        cmd[0] = util.console_python()
    return cmd + extra


def _popen_detached(argv: Union[List[str], str], *, env: Dict[str, str], cwd: Optional[str]) -> subprocess.Popen:
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                          "close_fds": True, "env": env, "cwd": cwd}
    if not util.IS_WINDOWS:
        kw["start_new_session"] = True
        return subprocess.Popen(argv, **kw)
    mode = winapi.spawn_mode()  # pragma: no cover - Windows only
    kw["creationflags"] = winapi.spawn_flags(mode)
    try:
        return subprocess.Popen(argv, **kw)
    except OSError as e:
        if getattr(e, "winerror", None) == winapi.ERROR_ACCESS_DENIED and mode == "breakaway":
            log.warning("breakaway spawn refused (access denied); treating the spawn mode as 'job'")
            winapi.set_spawn_mode("job")
            kw["creationflags"] = winapi.spawn_flags("job")
            return subprocess.Popen(argv, **kw)
        raise


def _ctime_of(pid: int) -> Optional[float]:
    for _ in range(20):
        ct = util.process_create_time(pid)
        if ct is not None:
            return ct
        time.sleep(0.01)
    return None


def spawn_shim(spec: dict, run_dir: Path) -> ShimHandle:
    """Write ``spec.json`` (0600) into ``run_dir`` and start ``hive _shim --spec`` detached (cwd = run dir; env = node
    env + util.self_env())."""
    run_dir = Path(run_dir)
    util.secure_mkdir(run_dir)
    spec_path = run_dir / "spec.json"
    util.write_json_atomic(spec_path, dict(spec, v=spec.get("v", 1)), private=True)
    env = dict(os.environ)
    env.update(util.self_env())
    try:
        p = _popen_detached(_helper_argv(["_shim", "--spec", str(spec_path)]), env=env, cwd=str(run_dir))
    except OSError as e:
        try:
            spec_path.unlink()
        except OSError:
            pass
        raise HiveError("internal", f"could not start the process wrapper: {e}")
    return ShimHandle(shim_pid=p.pid, shim_ctime=_ctime_of(p.pid), popen=p, run_dir=run_dir)


def reap_detached() -> None:
    """Poll detached children (spawn_detached, adopt_popens) so none stays a zombie."""
    with _detached_lock:
        _detached[:] = [p for p in _detached if p.poll() is None]


_reap_detached = reap_detached


def adopt_popens(popens: List[Any]) -> None:
    """Keep Popen objects of still-running helpers (shims of a stopped manager) for later reaping."""
    with _detached_lock:
        for p in popens:
            if p is not None and p.poll() is None and p not in _detached:
                _detached.append(p)


def spawn_detached(argv: List[str], *, env: Dict[str, str], cwd: Optional[Path] = None) -> Tuple[int, Optional[float]]:
    """Detached spawn with the shim spawn flags but no shim (WSL keepalive); returns (pid, ctime)."""
    _reap_detached()
    p = _popen_detached([str(a) for a in argv], env=dict(env), cwd=str(cwd) if cwd is not None else None)
    with _detached_lock:
        _detached.append(p)   # polled later so a POSIX child never stays a zombie
    return p.pid, _ctime_of(p.pid)


def request_stop(run_dir: Path, grace: float, force: bool = False) -> None:
    """Write ``<run_dir>/stop`` ``{"v":1,"grace","force"}``; the shim runs its stop sequence."""
    util.write_json_atomic(Path(run_dir) / "stop", {"v": 1, "grace": float(grace), "force": bool(force)}, private=True)


# ---------------------------------------------------------------------------------------------------------------
# process trees

def _psutil():
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # noqa: BLE001 - optional dependency
        return None


def _linux_ppid_map() -> Dict[int, int]:
    out: Dict[int, int] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return out
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(f"/proc/{n}/stat", "rb") as f:
                raw = f.read().decode("utf-8", "replace")
            rest = raw[raw.rindex(")") + 2:].split()
            out[int(n)] = int(rest[1])
        except (OSError, ValueError, IndexError):
            continue
    return out


def _ps_ppid_map() -> Dict[int, int]:
    out: Dict[int, int] = {}
    try:
        code, data, _e = run_quick(["ps", "-A", "-o", "pid=,ppid="], timeout=5, env=dict(os.environ, LC_ALL="C"))
    except HiveError:
        return out
    for line in util.decode_text(data).splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            out[int(parts[0])] = int(parts[1])
    return out


def _ppid_map() -> Dict[int, int]:
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        return {pid: ppid for pid, ppid, _n in winapi.process_list()}
    if sys.platform.startswith("linux") and os.path.isdir("/proc/self"):
        return _linux_ppid_map()
    return _ps_ppid_map()


def descendants(pid: int) -> List[int]:
    """Descendant pids of ``pid`` (psutil | Linux /proc ppid walk | macOS ps | Windows Toolhelp32), parents first."""
    ps = _psutil()
    if ps is not None:
        try:
            return [c.pid for c in ps.Process(int(pid)).children(recursive=True)]
        except Exception:  # noqa: BLE001
            pass
    pmap = _ppid_map()
    children: Dict[int, List[int]] = {}
    for c, pp in pmap.items():
        if c != pp:
            children.setdefault(pp, []).append(c)
    out: List[int] = []
    seen = {int(pid)}
    queue = [int(pid)]
    while queue:
        cur = queue.pop(0)
        for c in sorted(children.get(cur, [])):
            if c not in seen:
                seen.add(c)
                out.append(c)
                queue.append(c)
    return out


def _alive_all(ids: Dict[int, Optional[float]]) -> Dict[int, Optional[float]]:
    return {p: ct for p, ct in ids.items() if util.pid_alive(p, ct)}


def _signal(p: int, sig: int) -> None:
    try:
        os.kill(p, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def kill_tree(pid: int, *, grace: float = 5.0, force: bool = False,
              create_time: Optional[float] = None) -> Dict[str, Any]:
    """Stop ``pid`` and its descendants: graceful signal (POSIX SIGTERM to the group / taskkill on Windows), wait
    ``grace``, then hard kill; identity-checked with create times. ``{"killed","still_alive","method"}``."""
    pid = int(pid)
    if pid <= 0 or not util.pid_alive(pid, create_time):
        return {"killed": [], "still_alive": [], "method": "taskkill" if util.IS_WINDOWS else "signal"}
    snap: Dict[int, Optional[float]] = {pid: create_time if create_time is not None else util.process_create_time(pid)}
    for d in descendants(pid):
        snap[d] = util.process_create_time(d)
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        return _kill_tree_windows(pid, snap, grace, force)
    method = "signal"
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = -1
    group = pgid == pid

    def send(sig: int) -> None:
        if group:
            try:
                os.killpg(pid, sig)
            except OSError:
                pass
        elif util.pid_alive(pid, snap[pid]):
            _signal(pid, sig)
        for p, ct in snap.items():
            if p == pid:
                continue
            if util.pid_alive(p, ct):
                _signal(p, sig)

    if group:
        method = "killpg"
    if not force:
        send(signal.SIGTERM)
        end = time.monotonic() + max(0.0, float(grace))
        while time.monotonic() < end and _alive_all(snap):
            time.sleep(0.1)
    if _alive_all(snap):
        send(signal.SIGKILL)
        end = time.monotonic() + 3.0
        while time.monotonic() < end and _alive_all(snap):
            time.sleep(0.05)
    alive = _alive_all(snap)
    return {"killed": sorted(p for p in snap if p not in alive), "still_alive": sorted(alive), "method": method}


def _taskkill(pid: int, force: bool) -> None:  # pragma: no cover - Windows only
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    argv = [exe if os.path.exists(exe) else "taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    try:
        subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15, creationflags=winapi.CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _kill_tree_windows(pid: int, snap: Dict[int, Optional[float]], grace: float,
                       force: bool) -> Dict[str, Any]:  # pragma: no cover - Windows only
    method = "taskkill/F" if force else "taskkill"
    if not force:
        _taskkill(pid, False)
        end = time.monotonic() + max(0.0, float(grace))
        while time.monotonic() < end and _alive_all(snap):
            time.sleep(0.1)
    if _alive_all(snap):
        if util.pid_alive(pid, snap[pid]):
            _taskkill(pid, True)
            method = "taskkill/F"
        time.sleep(0.2)
        for p, ct in _alive_all(snap).items():
            winapi.terminate_pid(p)
    end = time.monotonic() + 3.0
    while time.monotonic() < end and _alive_all(snap):
        time.sleep(0.05)
    alive = _alive_all(snap)
    return {"killed": sorted(p for p in snap if p not in alive), "still_alive": sorted(alive), "method": method}


# ---------------------------------------------------------------------------------------------------------------
# quick probes

def _reader(stream: Any, buf: bytearray, cap: int) -> None:
    try:
        while True:
            b = stream.read(65536)
            if not b:
                break
            room = cap - len(buf)
            if room > 0:
                buf += b[:room]
    except (OSError, ValueError):
        pass


def run_quick(argv: List[str], *, timeout: float, cwd: Any = None, env: Any = None,
              max_output: int = 1 << 20) -> Tuple[int, bytes, bytes]:
    """Direct subprocess for INTERNAL short probes only: (returncode, stdout, stderr), each capped at ``max_output``.
    Kills the tree and raises ``timeout`` (maybe_executed False) on timeout; a missing program -> ``not_found``."""
    kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                          "cwd": str(cwd) if cwd is not None else None, "env": env, "close_fds": True}
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        kw["creationflags"] = winapi.CREATE_NO_WINDOW | winapi.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    try:
        p = subprocess.Popen([str(a) for a in argv], **kw)
    except FileNotFoundError:
        raise not_found(f"program '{argv[0] if argv else ''}' not found")
    except OSError as e:
        raise HiveError("internal", f"cannot run {os.path.basename(str(argv[0]))}: {e}")
    out, err = bytearray(), bytearray()
    ts = [threading.Thread(target=_reader, args=(p.stdout, out, max_output), daemon=True),
          threading.Thread(target=_reader, args=(p.stderr, err, max_output), daemon=True)]
    for t in ts:
        t.start()
    try:
        code = p.wait(timeout=max(0.1, float(timeout)))
    except subprocess.TimeoutExpired:
        try:
            kill_tree(p.pid, force=True)
        finally:
            try:
                p.kill()
            except OSError:
                pass
            p.wait()
        for t in ts:
            t.join(1)
        _close_pipes(p)
        raise timeout_error(f"{os.path.basename(str(argv[0]))} did not finish within {timeout:g} s",
                            maybe_executed=False)
    for t in ts:
        t.join(5)
    _close_pipes(p)
    return code, bytes(out), bytes(err)


def _close_pipes(p: subprocess.Popen) -> None:
    for s in (p.stdout, p.stderr):
        try:
            if s is not None:
                s.close()
        except (OSError, AttributeError):
            pass


__all__ = ["pid_alive", "process_create_time", "set_trusted_path", "trusted_path", "default_shell",
           "available_shells", "ShellPlan", "build_shell_argv", "build_argv", "ShimHandle", "spawn_shim",
           "spawn_detached", "request_stop", "kill_tree", "descendants", "run_quick", "which", "shell_path",
           "resolve_shell", "powershell_encoded", "reap_detached", "adopt_popens"]
