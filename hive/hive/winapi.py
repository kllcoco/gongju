"""Windows ctypes helpers: Job Objects, spawn_mode, escape-once relaunch, resume_process, Toolhelp32,
GetProcessTimes, console ctrl, SetThreadExecutionState (design §10.1, §10.2, D40, D52, WP2).

Every function is safe to call on any OS: off Windows the probes return neutral values (``spawn_mode()`` -> "free",
``session_winlogon_ctime()`` -> None, ``process_list()`` -> []) and the actions raise ``unsupported``.
Imports only the stdlib, ``hive.errors`` and ``hive.util`` (the shim imports this module eagerly, §10.2).

Day-1 spike (D40) record: the measurement on Win10 22H2 / Win11 24H2 (IsProcessInJob and the breakaway flags of the
§20.1 task) must be run on real Windows machines; this module implements every mode (free/silent/breakaway/job) and
the WMI escape, so the outcome only decides which path is the normal one.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from . import util
from .errors import unavailable, unsupported

log = logging.getLogger("hive.winapi")

IS_WINDOWS = os.name == "nt"
SPAWN_MODES = ("free", "silent", "breakaway", "job", "escaped")

# process creation flags
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008

# misc constants
PROCESS_TERMINATE = 0x0001
PROCESS_SET_QUOTA = 0x0100
PROCESS_SUSPEND_RESUME = 0x0800
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x00000800
JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK = 0x00001000
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectExtendedLimitInformation = 9
CTRL_C_EVENT = 0
CTRL_BREAK_EVENT = 1
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
TH32CS_SNAPPROCESS = 0x00000002
ERROR_ACCESS_DENIED = 5
SW_HIDE = 0

_mode_lock = threading.Lock()
_mode: Optional[str] = None
_mode_info: Dict[str, object] = {}


# ---------------------------------------------------------------------------------------------------------------
# ctypes plumbing (Windows only)

_k32 = None
_ntdll = None


def _kernel32():  # pragma: no cover - Windows only
    global _k32
    if _k32 is None:
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.restype = wintypes.HANDLE
        k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.GetCurrentProcess.restype = wintypes.HANDLE
        k.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                                ctypes.POINTER(wintypes.DWORD)]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.GenerateConsoleCtrlEvent.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.GetConsoleProcessList.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD]
        k.GetConsoleProcessList.restype = wintypes.DWORD
        k.GetConsoleWindow.restype = wintypes.HWND
        k.SetThreadExecutionState.argtypes = [wintypes.DWORD]
        k.SetThreadExecutionState.restype = wintypes.DWORD
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        k.GetCurrentProcessId.restype = wintypes.DWORD
        _k32 = k
    return _k32


def _structs():  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IOC(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                                                    "OtherOperationCount", "ReadTransferCount",
                                                    "WriteTransferCount", "OtherTransferCount")]

    class EXT(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class PE32(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    return EXT, PE32


def _require_windows(what: str) -> None:
    if not IS_WINDOWS:
        raise unsupported(f"{what} is only available on Windows")


# ---------------------------------------------------------------------------------------------------------------
# process inventory

def process_list() -> List[Tuple[int, int, str]]:
    """Toolhelp32 snapshot: [(pid, ppid, exe name)]; [] off Windows or on failure."""
    if not IS_WINDOWS:
        return []
    try:  # pragma: no cover - Windows only
        import ctypes
        k = _kernel32()
        _EXT, PE32 = _structs()
        snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        out: List[Tuple[int, int, str]] = []
        try:
            e = PE32()
            e.dwSize = ctypes.sizeof(PE32)
            ok = k.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                out.append((int(e.th32ProcessID), int(e.th32ParentProcessID), str(e.szExeFile)))
                ok = k.Process32NextW(snap, ctypes.byref(e))
        finally:
            k.CloseHandle(snap)
        return out
    except Exception:  # noqa: BLE001
        return []


def session_id(pid: Optional[int] = None) -> Optional[int]:
    """ProcessIdToSessionId (this process when pid is None); None off Windows or on failure."""
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        k = _kernel32()
        sid = wintypes.DWORD(0)
        p = k.GetCurrentProcessId() if pid is None else int(pid)
        if not k.ProcessIdToSessionId(p, ctypes.byref(sid)):
            return None
        return int(sid.value)
    except Exception:  # noqa: BLE001
        return None


def process_create_time(pid: int) -> Optional[float]:
    """GetProcessTimes creation time (epoch seconds) via util (shared implementation)."""
    return util.process_create_time(pid)


def session_winlogon_ctime() -> Optional[float]:
    """Creation time of the winlogon.exe of this process's logon session (Windows boot_key, D52); None otherwise."""
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        mine = session_id(None)
        if mine is None:
            return None
        for pid, _ppid, name in process_list():
            if name.lower() == "winlogon.exe" and session_id(pid) == mine:
                ct = util.process_create_time(pid)
                if ct is not None:
                    return ct
        return None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------
# job objects / spawn mode

def is_in_job() -> Optional[bool]:
    """IsProcessInJob(GetCurrentProcess(), NULL); None off Windows or on failure."""
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        k = _kernel32()
        res = wintypes.BOOL(False)
        if not k.IsProcessInJob(k.GetCurrentProcess(), None, ctypes.byref(res)):
            return None
        return bool(res.value)
    except Exception:  # noqa: BLE001
        return None


def _job_limit_flags(job: Optional[int] = None) -> Optional[int]:  # pragma: no cover - Windows only
    import ctypes
    k = _kernel32()
    EXT, _PE32 = _structs()
    info = EXT()
    if not k.QueryInformationJobObject(job, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                       ctypes.sizeof(info), None):
        return None
    return int(info.BasicLimitInformation.LimitFlags)


def _detect_mode() -> str:
    if not IS_WINDOWS:
        return "free"
    in_job = is_in_job()  # pragma: no cover - Windows only
    _mode_info["in_job"] = in_job
    if in_job is False:
        return "free"
    if in_job is None:
        return "breakaway"   # API failure -> try breakaway (§10.1)
    try:
        flags = _job_limit_flags(None)
    except Exception:  # noqa: BLE001
        flags = None
    if flags is None:
        return "breakaway"
    if flags & JOB_OBJECT_LIMIT_SILENT_BREAKAWAY_OK:
        return "silent"
    if flags & JOB_OBJECT_LIMIT_BREAKAWAY_OK:
        return "breakaway"
    return "job"


def spawn_mode() -> str:
    """free | silent | breakaway | job, computed once per process (D40); "free" off Windows."""
    global _mode
    with _mode_lock:
        if _mode is None:
            try:
                _mode = _detect_mode()
            except Exception:  # noqa: BLE001
                _mode = "breakaway"
        return _mode


def set_spawn_mode(mode: str) -> None:
    """Override the detected mode (an ERROR_ACCESS_DENIED breakaway spawn downgrades to "job"; tests)."""
    global _mode
    if mode not in SPAWN_MODES:
        raise ValueError(mode)
    with _mode_lock:
        _mode = mode


def spawn_flags(mode: Optional[str] = None) -> int:
    """creationflags for detached Hive helpers (shims, _download, keepalive): new group, no window, breakaway."""
    flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    if (mode or spawn_mode()) == "breakaway":
        flags |= CREATE_BREAKAWAY_FROM_JOB
    return flags


def platform_info() -> Optional[dict]:
    """``{"spawn_mode","session_id","in_job"}`` for node.info (None off Windows)."""
    if not IS_WINDOWS:
        return None
    return {"spawn_mode": spawn_mode(), "session_id": session_id(None), "in_job": bool(is_in_job())}


class JobObject:
    """A kill-on-close Job Object (the shim's container for the child tree)."""

    def __init__(self) -> None:
        _require_windows("Job Objects")
        import ctypes  # pragma: no cover - Windows only
        k = _kernel32()
        h = k.CreateJobObjectW(None, None)
        if not h:
            raise OSError(ctypes.get_last_error(), "CreateJobObjectW failed")
        EXT, _PE32 = _structs()
        info = EXT()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                         ctypes.sizeof(info)):
            err = ctypes.get_last_error()
            k.CloseHandle(h)
            raise OSError(err, "SetInformationJobObject failed")
        self.handle = h

    def assign(self, process_handle: int) -> bool:
        """AssignProcessToJobObject; False on failure (the caller continues without a job)."""
        try:  # pragma: no cover - Windows only
            return bool(_kernel32().AssignProcessToJobObject(self.handle, int(process_handle)))
        except Exception:  # noqa: BLE001
            return False

    def terminate(self, exit_code: int = 1) -> bool:
        """TerminateJobObject (hard stop of the whole tree)."""
        try:  # pragma: no cover - Windows only
            return bool(_kernel32().TerminateJobObject(self.handle, exit_code))
        except Exception:  # noqa: BLE001
            return False

    def close(self) -> None:
        """Close the handle (kills the tree: KILL_ON_JOB_CLOSE)."""
        if getattr(self, "handle", None):  # pragma: no cover - Windows only
            try:
                _kernel32().CloseHandle(self.handle)
            finally:
                self.handle = None


def resume_process(process_handle: int) -> bool:
    """ntdll.NtResumeProcess on a CREATE_SUSPENDED child; False when unavailable."""
    if not IS_WINDOWS:
        return False
    try:  # pragma: no cover - Windows only
        import ctypes
        global _ntdll
        if _ntdll is None:
            _ntdll = ctypes.WinDLL("ntdll")
        status = _ntdll.NtResumeProcess(ctypes.c_void_p(int(process_handle)))
        return status == 0
    except Exception:  # noqa: BLE001
        return False


def terminate_pid(pid: int, exit_code: int = 1) -> bool:
    """OpenProcess + TerminateProcess; False when the process is gone or access is denied."""
    if not IS_WINDOWS:
        return False
    try:  # pragma: no cover - Windows only
        k = _kernel32()
        h = k.OpenProcess(PROCESS_TERMINATE, False, int(pid))
        if not h:
            return False
        try:
            return bool(k.TerminateProcess(h, exit_code))
        finally:
            k.CloseHandle(h)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------------------
# console

def has_console() -> bool:
    """GetConsoleProcessList reports a console attached to this process (True off Windows)."""
    if not IS_WINDOWS:
        return True
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        buf = (wintypes.DWORD * 4)()
        return _kernel32().GetConsoleProcessList(buf, 4) > 0
    except Exception:  # noqa: BLE001
        return True


def alloc_hidden_console() -> bool:
    """AllocConsole() and hide its window (so CTRL_BREAK delivery works under pythonw, §10.2 step 1)."""
    if not IS_WINDOWS:
        return False
    try:  # pragma: no cover - Windows only
        import ctypes
        k = _kernel32()
        if not k.AllocConsole():
            return False
        hwnd = k.GetConsoleWindow()
        if hwnd:
            ctypes.WinDLL("user32").ShowWindow(hwnd, SW_HIDE)
        return True
    except Exception:  # noqa: BLE001
        return False


def send_ctrl_break(pid: int) -> bool:
    """GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, pid): reaches the process group ``pid`` on our console."""
    if not IS_WINDOWS:
        return False
    try:  # pragma: no cover - Windows only
        return bool(_kernel32().GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT, int(pid)))
    except Exception:  # noqa: BLE001
        return False


def keep_awake(on: bool = True) -> bool:
    """SetThreadExecutionState(ES_CONTINUOUS|ES_SYSTEM_REQUIRED) for the calling thread (False off Windows)."""
    if not IS_WINDOWS:
        return False
    try:  # pragma: no cover - Windows only
        flags = ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if on else 0)
        return _kernel32().SetThreadExecutionState(flags) != 0
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------------------
# escape-once relaunch through WMI (D40)

def _ps_quote(s: str) -> str:
    return "'" + str(s).replace("'", "''") + "'"


def relaunch_script(env_file: str, cmdline: str, cwd: str) -> str:
    """The PowerShell text of the WMI relaunch: reads the env file (and deletes it), then Win32_Process.Create."""
    return "\n".join([
        "$ErrorActionPreference='Stop'",
        f"$f={_ps_quote(env_file)}",
        "try { $e = Get-Content -Raw -Encoding UTF8 -LiteralPath $f | ConvertFrom-Json } "
        "finally { Remove-Item -Force -LiteralPath $f -ErrorAction SilentlyContinue }",
        "$vars = @(); foreach ($p in $e.PSObject.Properties) { $vars += ($p.Name + '=' + [string]$p.Value) }",
        "$si = New-CimInstance -ClassName Win32_ProcessStartup -ClientOnly -Property "
        "@{ShowWindow=[uint16]0; CreateFlags=[uint32]0x08000200; EnvironmentVariables=[string[]]$vars}",
        f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments "
        f"@{{CommandLine={_ps_quote(cmdline)}; CurrentDirectory={_ps_quote(cwd)}; ProcessStartupInformation=$si}}",
        "Write-Output ('HIVE-RELAUNCH ' + $r.ReturnValue + ' ' + $r.ProcessId)",
    ])


def _data_dir_from_argv(argv: List[str]) -> Path:
    for i, a in enumerate(argv):
        if a == "--config" and i + 1 < len(argv):
            return Path(argv[i + 1]).resolve().parent
    return Path(os.getcwd())


def _run_powershell(script: str, timeout: float) -> Tuple[int, str]:  # pragma: no cover - Windows only
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                       "powershell.exe")
    if not os.path.exists(exe):
        exe = "powershell.exe"
    p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", enc], stdin=subprocess.DEVNULL,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    return p.returncode, (p.stdout or b"").decode("utf-8", "replace") + (p.stderr or b"").decode("utf-8", "replace")


def relaunch_outside_job(argv: List[str], env: Dict[str, str], *,
                         data_dir: Optional[Path] = None) -> Tuple[int, Optional[float]]:
    """Start ``argv`` once through WMI ``Win32_Process.Create`` (outside the task's Job Object) with the FULL ``env``
    passed via a 0600 ``<data>/run/relaunch-env.json`` (never argv; deleted by the script); returns (pid, ctime).

    ``data_dir`` defaults to the directory of the ``--config`` file named in ``argv`` (§14: data_dir null = the config
    dir), else the cwd; it is also the new process's CurrentDirectory.
    """
    _require_windows("WMI relaunch")
    ddir = Path(data_dir) if data_dir is not None else _data_dir_from_argv(argv)  # pragma: no cover
    run = ddir / "run"
    util.secure_mkdir(run)
    env_file = run / "relaunch-env.json"
    util.write_json_atomic(env_file, {str(k): str(v) for k, v in env.items()}, private=True)
    util.secure_file(env_file, required=True)
    cmdline = subprocess.list2cmdline([str(a) for a in argv])
    try:
        code, out = _run_powershell(relaunch_script(str(env_file), cmdline, str(ddir)), 20.0)
    except subprocess.TimeoutExpired:
        raise unavailable("WMI relaunch timed out", reason="upstream", maybe_executed=True)
    finally:
        try:
            env_file.unlink()
        except OSError:
            pass
    for line in out.splitlines():
        if line.startswith("HIVE-RELAUNCH "):
            parts = line.split()
            if len(parts) >= 3 and parts[1] == "0" and parts[2].isdigit():
                pid = int(parts[2])
                ctime = None
                for _ in range(20):
                    ctime = util.process_create_time(pid)
                    if ctime is not None:
                        break
                    time.sleep(0.05)
                return pid, ctime
            raise unavailable(f"Win32_Process.Create returned {parts[1:]}", reason="upstream", maybe_executed=False)
    log.error("WMI relaunch failed (exit %s): %s", code, out[-500:])
    raise unavailable("WMI relaunch failed", reason="upstream", maybe_executed=False,
                      hint="run `hive doctor`; the documented fallback is a plain start inside the task")


__all__ = ["SPAWN_MODES", "session_winlogon_ctime", "spawn_mode", "set_spawn_mode", "spawn_flags",
           "relaunch_outside_job", "relaunch_script", "process_list", "session_id", "is_in_job", "platform_info",
           "JobObject", "resume_process", "terminate_pid", "has_console", "alloc_hidden_console", "send_ctrl_break",
           "keep_awake", "process_create_time"]
