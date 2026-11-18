"""OS/CPU/memory/disk/network/software probes (psutil optional), capabilities, summary_line, live_status
(design §12.1, WP4).

psutil is used when importable; otherwise Linux reads ``/proc`` and cgroup files, macOS runs ``sysctl``/``vm_stat``/
``df``/``sw_vers`` and Windows uses ctypes (GlobalMemoryStatusEx, GetSystemTimes, GetDiskFreeSpaceExW, …). Every
program is resolved on ``proctree.trusted_path()`` (or a fixed system location) and run with ``proctree.run_quick``.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import platform
import re
import socket
import sys
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from . import util

log = logging.getLogger("hive.sysinfo")

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

# test seams
_PROC = "/proc"
_CGROUP = "/sys/fs/cgroup"
_OS_RELEASE = "/etc/os-release"
_AUTODL = "/root/autodl-tmp"

STATIC_TTL = 60.0
SOFTWARE_TTL = 300.0
CAPS_TTL = 60.0
LIVE_TTL = 5.0
SOFTWARE = ("python", "python3", "pip", "git", "docker", "nvidia-smi", "nvcc", "conda", "uv", "ollama", "node", "curl",
            "wget", "ffmpeg", "wsl")
# §9.2 v5: `which` probes --version only for these names (a read token must never launch arbitrary programs)
VERSION_NAMES = frozenset(SOFTWARE) | {"llama-server", "vllm", "sglang"}
_VERSION_FLAG = {"ffmpeg": "-version"}

DROP_FS = {"proc", "sysfs", "cgroup", "cgroup2", "devtmpfs", "tmpfs", "squashfs", "devpts", "mqueue", "debugfs",
           "tracefs", "securityfs", "pstore", "bpf", "autofs", "configfs", "fusectl", "hugetlbfs", "binfmt_misc",
           "nsfs", "rpc_pipefs", "ramfs", "efivarfs", "selinuxfs", "fuse.gvfsd-fuse", "fuse.portal", "overlayfs-meta",
           "nfsd", "cpuset", "none"}
WSL_DROP_MOUNTS = ("/usr/lib/wsl/drivers", "/usr/lib/wsl/lib", "/mnt/wsl", "/init", "/mnt/wslg")

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fn: Callable[[], Any], refresh: bool = False) -> Any:
    now = time.monotonic()
    if not refresh:
        with _cache_lock:
            hit = _cache.get(key)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
    val = fn()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


def clear_cache() -> None:
    """Forget every cached probe (tests, system.info refresh)."""
    with _cache_lock:
        _cache.clear()


def _psutil() -> Any:
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # noqa: BLE001
        return None


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _tool(name: str, fixed: Iterable[str] = ()) -> Optional[str]:
    from . import proctree
    p = proctree.which(name, proctree.trusted_path())
    if p:
        return p
    for f in fixed:
        if os.path.isfile(f):
            return f
    return None


def _run(argv: List[str], timeout: float = 5.0, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    """stdout of a probe (exit 0), else None."""
    from . import proctree
    try:
        code, out, _err = proctree.run_quick(argv, timeout=timeout, env=env)
    except Exception:  # noqa: BLE001
        return None
    return util.decode_text(out) if code == 0 else None


def _sysctl(*names: str) -> Optional[List[str]]:  # pragma: no cover - macOS only
    exe = _tool("sysctl", ["/usr/sbin/sysctl", "/sbin/sysctl"])
    if not exe:
        return None
    out = _run([exe, "-n"] + list(names))
    if out is None:
        return None
    return out.splitlines()


# ---------------------------------------------------------------------------------------------------------------
# Windows ctypes helpers

def _win_memory() -> Optional[dict]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    swap_total = max(0, m.ullTotalPageFile - m.ullTotalPhys)
    swap_used = max(0, (m.ullTotalPageFile - m.ullAvailPageFile) - (m.ullTotalPhys - m.ullAvailPhys))
    return {"total": int(m.ullTotalPhys), "available": int(m.ullAvailPhys), "swap_total": int(swap_total),
            "swap_used": int(min(swap_used, swap_total))}


def _win_times() -> Optional[Tuple[int, int]]:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None

    def v(ft: Any) -> int:
        return (ft.dwHighDateTime << 32) | ft.dwLowDateTime
    return v(idle), v(kernel) + v(user)        # kernel time includes idle time


def _win_disks() -> List[dict]:  # pragma: no cover - Windows only
    import ctypes
    k = ctypes.windll.kernel32
    buf = ctypes.create_unicode_buffer(1024)
    n = k.GetLogicalDriveStringsW(len(buf), buf)
    drives = [d for d in buf[:n].split("\x00") if d]
    out = []
    for d in drives:
        if k.GetDriveTypeW(ctypes.c_wchar_p(d)) not in (2, 3):     # removable, fixed
            continue
        free, total = ctypes.c_ulonglong(0), ctypes.c_ulonglong(0)
        if not k.GetDiskFreeSpaceExW(ctypes.c_wchar_p(d), ctypes.byref(free), ctypes.byref(total), None):
            continue
        fsname = ctypes.create_unicode_buffer(64)
        k.GetVolumeInformationW(ctypes.c_wchar_p(d), None, 0, None, None, None, fsname, len(fsname))
        if total.value:
            out.append(_disk_row(d, fsname.value or "unknown", total.value, free.value))
    return out


def _win_admin() -> bool:  # pragma: no cover - Windows only
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------------------
# os

def _linux_distro() -> Optional[str]:
    txt = _read(_OS_RELEASE) or _read("/usr/lib/os-release")
    if not txt:
        return None
    vals = {}
    for line in txt.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals.get("PRETTY_NAME") or (" ".join(x for x in (vals.get("NAME"), vals.get("VERSION_ID")) if x) or None)


def _timezone() -> Optional[str]:
    tz = os.environ.get("TZ")
    if tz and not tz.startswith(":") and "/" in tz:
        return tz
    if not IS_WINDOWS:
        try:
            link = os.readlink("/etc/localtime")
            m = re.search(r"zoneinfo/(.+)$", link)
            if m:
                return m.group(1)
        except OSError:
            pass
        t = (_read("/etc/timezone") or "").strip()
        if t:
            return t
    try:
        name = time.strftime("%Z")
        return name or None
    except (ValueError, OSError):
        return None


def _release() -> str:
    rel = platform.release()
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            build = int(platform.version().split(".")[-1])
            if rel == "10" and build >= 22000:
                rel = "11"
        except (ValueError, IndexError):
            pass
    return rel


def cloud_info() -> dict:
    """``{provider: "runpod"|"vast"|"autodl"|None, pod_id}`` from the environment and well-known paths."""
    env = os.environ
    if env.get("RUNPOD_POD_ID") or env.get("RUNPOD_GPU_COUNT"):
        return {"provider": "runpod", "pod_id": env.get("RUNPOD_POD_ID") or None}
    if env.get("VAST_CONTAINERLABEL"):
        return {"provider": "vast", "pod_id": env.get("VAST_CONTAINERLABEL")}
    if os.path.isdir(_AUTODL):
        return {"provider": "autodl", "pod_id": env.get("AutoDLContainerUUID") or None}
    return {"provider": None, "pod_id": None}


def os_info() -> dict:
    """OS section (§9.2): system, release, version, machine, hostname, distro, python, boot_time, uptime_s, timezone,
    cloud, wsl (+ wsl_distros on Windows)."""
    def static() -> dict:
        distro: Optional[str] = None
        if sys.platform.startswith("linux"):
            distro = _linux_distro()
        elif IS_MAC:  # pragma: no cover - macOS only
            exe = _tool("sw_vers", ["/usr/bin/sw_vers"])
            v = _run([exe, "-productVersion"]) if exe else None
            distro = f"macOS {v.strip()}" if v else None
        elif IS_WINDOWS:  # pragma: no cover - Windows only
            try:
                ed = platform.win32_edition()
                distro = f"Windows {_release()} {ed}".strip() if ed else None
            except Exception:  # noqa: BLE001
                distro = None
        wsl_part = None
        try:
            from . import wsl
            if wsl.is_wsl():
                wi = wsl.wsl_info() or {}
                wsl_part = {"distro": wi.get("distro"), "version": 2, "networking": wi.get("networking"),
                            "systemd": bool(wi.get("systemd"))}
        except Exception:  # noqa: BLE001
            wsl_part = None
        return {"system": platform.system(), "release": _release(), "version": platform.version(),
                "machine": platform.machine(), "hostname": socket.gethostname(), "distro": distro,
                "python": platform.python_version(), "timezone": _timezone(), "cloud": cloud_info(), "wsl": wsl_part}
    out = dict(_cached("os", STATIC_TTL, static))
    up = uptime()
    out["uptime_s"] = round(up, 1) if up is not None else None
    out["boot_time"] = util.iso_utc(time.time() - up) if up is not None else None
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            from . import wsl
            out["wsl_distros"] = wsl.list_distros()
        except Exception:  # noqa: BLE001
            out["wsl_distros"] = []
    order = ["system", "release", "version", "machine", "hostname", "distro", "python", "boot_time", "uptime_s",
             "timezone", "cloud", "wsl", "wsl_distros"]
    return {k: out[k] for k in order if k in out}


def uptime() -> Optional[float]:
    """Seconds since boot."""
    ps = _psutil()
    if ps is not None:
        try:
            return max(0.0, time.time() - float(ps.boot_time()))
        except Exception:  # noqa: BLE001
            pass
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            import ctypes
            f = ctypes.windll.kernel32.GetTickCount64
            f.restype = ctypes.c_ulonglong
            return f() / 1000.0
        except Exception:  # noqa: BLE001
            return None
    txt = _read(os.path.join(_PROC, "uptime"))
    if txt:
        try:
            return float(txt.split()[0])
        except (ValueError, IndexError):
            pass
    if IS_MAC:  # pragma: no cover - macOS only
        v = _sysctl("kern.boottime")
        m = re.search(r"sec\s*=\s*(\d+)", (v or [""])[0])
        if m:
            return max(0.0, time.time() - float(m.group(1)))
    return None


# ---------------------------------------------------------------------------------------------------------------
# cpu

def _cpuinfo() -> Tuple[Optional[str], Optional[int]]:
    txt = _read(os.path.join(_PROC, "cpuinfo")) or ""
    model = None
    cores = set()
    phys = core = None
    for line in txt.splitlines() + [""]:
        if not line.strip():
            if phys is not None or core is not None:
                cores.add((phys, core))
            phys = core = None
            continue
        if ":" not in line:
            continue
        k, v = (x.strip() for x in line.split(":", 1))
        if k in ("model name", "Model", "Hardware", "cpu model", "Processor") and model is None and v:
            model = v
        elif k == "physical id":
            phys = v
        elif k == "core id":
            core = v
    return model, (len(cores) or None)


def cpu_info() -> dict:
    """CPU section: model, logical, physical, load_avg, percent."""
    def static() -> dict:
        model: Optional[str] = None
        physical: Optional[int] = None
        ps = _psutil()
        if ps is not None:
            try:
                physical = ps.cpu_count(logical=False)
            except Exception:  # noqa: BLE001
                physical = None
        if sys.platform.startswith("linux"):
            model, phys2 = _cpuinfo()
            physical = physical or phys2
        elif IS_MAC:  # pragma: no cover - macOS only
            v = _sysctl("machdep.cpu.brand_string", "hw.physicalcpu")
            if v and len(v) >= 2:
                model = v[0].strip() or None
                try:
                    physical = physical or int(v[1])
                except ValueError:
                    pass
        elif IS_WINDOWS:  # pragma: no cover - Windows only
            try:
                import winreg
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                    r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                    model = str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
            except OSError:
                model = platform.processor() or None
        return {"model": model, "logical": os.cpu_count() or 1, "physical": physical}
    out = dict(_cached("cpu", STATIC_TTL, static))
    out["load_avg"] = load_avg()
    out["percent"] = cpu_percent()
    return out


def load_avg() -> Optional[List[float]]:
    """1/5/15-minute load average (None on Windows)."""
    if IS_WINDOWS:
        return None
    try:
        return [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        return None


def _proc_stat_times() -> Optional[Tuple[int, int]]:
    """(idle, total) jiffies from /proc/stat."""
    txt = _read(os.path.join(_PROC, "stat"))
    if not txt:
        return None
    for line in txt.splitlines():
        if line.startswith("cpu "):
            vals = [int(x) for x in line.split()[1:]]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals[:8]) if len(vals) >= 8 else sum(vals)
            return idle, total
    return None


def _times() -> Optional[Tuple[int, int]]:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            return _win_times()
        except Exception:  # noqa: BLE001
            return None
    return _proc_stat_times()


def _pct(a: Tuple[int, int], b: Tuple[int, int]) -> Optional[float]:
    didle, dtotal = b[0] - a[0], b[1] - a[1]
    if dtotal <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - didle / dtotal) * 100.0)), 1)


def cpu_percent(sample_ms: int = 300) -> Optional[float]:
    """CPU percent over a short sample (psutil | /proc/stat | GetSystemTimes | ps)."""
    ps = _psutil()
    if ps is not None:
        try:
            return round(float(ps.cpu_percent(interval=max(0.05, sample_ms / 1000.0))), 1)
        except Exception:  # noqa: BLE001
            pass
    a = _times()
    if a is not None:
        time.sleep(max(0.05, sample_ms / 1000.0))
        b = _times()
        if b is not None:
            return _pct(a, b)
    if IS_MAC:  # pragma: no cover - macOS only
        exe = _tool("ps", ["/bin/ps"])
        out = _run([exe, "-A", "-o", "%cpu="], env=dict(os.environ, LC_ALL="C")) if exe else None
        if out:
            try:
                total = sum(float(x.replace(",", ".")) for x in out.split())
                return round(min(100.0, total / (os.cpu_count() or 1)), 1)
            except ValueError:
                return None
    return None


_last_times: Dict[str, Any] = {"t": None}


def _cpu_since_last() -> Optional[float]:
    """CPU percent since the previous call (no sleep; the first call samples 100 ms)."""
    cur = _times()
    prev = _last_times.get("t")
    _last_times["t"] = cur
    if cur is None:
        return cpu_percent(100) if _psutil() is not None else None
    if prev is None:
        return cpu_percent(100)
    return _pct(prev, cur)


# ---------------------------------------------------------------------------------------------------------------
# memory

def _meminfo() -> Dict[str, int]:
    out = {}
    for line in (_read(os.path.join(_PROC, "meminfo")) or "").splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1]) * (1024 if len(parts) > 2 and parts[2].lower() == "kb" else 1)
            except ValueError:
                pass
    return out


def container_limit(total: Optional[int] = None) -> Optional[int]:
    """cgroup v2 ``memory.max`` / v1 ``memory.limit_in_bytes`` in bytes (None when unlimited or unknown)."""
    for p in (os.path.join(_CGROUP, "memory.max"), os.path.join(_CGROUP, "memory", "memory.limit_in_bytes")):
        v = (_read(p) or "").strip()
        if not v or v == "max":
            continue
        try:
            n = int(v)
        except ValueError:
            continue
        if n >= (1 << 60) or (total and n >= total):
            return None
        return n
    return None


def memory() -> dict:
    """Memory section: total, available, used_pct, swap_total, swap_used, container_limit (bytes)."""
    total = avail = 0
    swap_total: Optional[int] = None
    swap_used: Optional[int] = None
    ps = _psutil()
    done = False
    if ps is not None:
        try:
            vm = ps.virtual_memory()
            total, avail = int(vm.total), int(vm.available)
            sw = ps.swap_memory()
            swap_total, swap_used = int(sw.total), int(sw.used)
            done = True
        except Exception:  # noqa: BLE001
            done = False
    if not done and IS_WINDOWS:  # pragma: no cover - Windows only
        m = _win_memory() or {}
        total, avail = m.get("total", 0), m.get("available", 0)
        swap_total, swap_used = m.get("swap_total"), m.get("swap_used")
        done = bool(total)
    if not done and sys.platform.startswith("linux"):
        mi = _meminfo()
        total = mi.get("MemTotal", 0)
        avail = mi.get("MemAvailable", mi.get("MemFree", 0) + mi.get("Cached", 0) + mi.get("Buffers", 0))
        if "SwapTotal" in mi:
            swap_total = mi["SwapTotal"]
            swap_used = max(0, mi["SwapTotal"] - mi.get("SwapFree", 0))
        done = bool(total)
    if not done and IS_MAC:  # pragma: no cover - macOS only
        v = _sysctl("hw.memsize")
        try:
            total = int((v or ["0"])[0])
        except ValueError:
            total = 0
        exe = _tool("vm_stat", ["/usr/bin/vm_stat"])
        out = _run([exe]) if exe else None
        avail = parse_vm_stat(out or "") if out else 0
    limit = container_limit(total) if sys.platform.startswith("linux") else None
    used_pct = round((1 - avail / total) * 100.0, 1) if total else 0.0
    return {"total": int(total), "available": int(avail), "used_pct": max(0.0, used_pct), "swap_total": swap_total,
            "swap_used": swap_used, "container_limit": limit}


def parse_vm_stat(text: str) -> int:
    """macOS ``vm_stat`` -> available bytes (free + inactive + speculative + purgeable pages)."""
    m = re.search(r"page size of (\d+) bytes", text)
    page = int(m.group(1)) if m else 4096
    vals = {}
    for line in text.splitlines():
        mm = re.match(r'^"?([^":]+)"?:\s+(\d+)\.?$', line.strip())
        if mm:
            vals[mm.group(1).strip()] = int(mm.group(2))
    pages = sum(vals.get(k, 0) for k in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable"))
    return pages * page


# ---------------------------------------------------------------------------------------------------------------
# disks

def _disk_row(mount: str, fs: str, total: int, free: int) -> dict:
    used_pct = round((total - free) / total * 100.0, 1) if total else 0.0
    return {"mount": mount, "fs": fs, "total": int(total), "free": int(free), "used_pct": max(0.0, used_pct)}


def _unescape_mount(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mounts(text: str, *, wsl: bool = False) -> List[Tuple[str, str]]:
    """``/proc/mounts`` -> [(mount, fs)] without pseudo file systems (overlay kept for pods; WSL helper mounts
    dropped)."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        dev, mnt, fs = parts[0], _unescape_mount(parts[1]), parts[2]
        if fs in DROP_FS or fs.startswith("fuse.") and fs not in ("fuse.sshfs", "fuse.rclone", "fuse.s3fs"):
            continue
        if mnt.startswith(("/proc", "/sys", "/dev", "/run/user", "/snap/")) or mnt in ("/run", "/dev/shm"):
            continue
        if wsl and any(mnt == d or mnt.startswith(d + "/") for d in WSL_DROP_MOUNTS):
            continue
        if mnt in seen:
            continue
        if dev == "none" and fs not in ("overlay", "9p", "drvfs", "virtiofs"):
            continue
        seen.add(mnt)
        out.append((mnt, fs))
    return out


def _statvfs(path: str, timeout: float = 2.0) -> Optional[Tuple[int, int]]:
    """(total, free) bytes; a hanging network mount gives up after ``timeout``."""
    res: Dict[str, Any] = {}

    def run() -> None:
        try:
            st = os.statvfs(path)
            res["v"] = (st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize)
        except OSError:
            res["v"] = None
    t = threading.Thread(target=run, name="hive-statvfs", daemon=True)
    t.start()
    t.join(timeout)
    return res.get("v")


def parse_df(text: str) -> List[Tuple[str, int, int, str]]:
    """``df -kP`` -> [(mount, total bytes, free bytes, device)]."""
    rows = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 6:
            continue
        try:
            total, avail = int(parts[1]) * 1024, int(parts[3]) * 1024
        except ValueError:
            continue
        mnt = line.split(None, 5)[5] if len(line.split(None, 5)) > 5 else parts[-1]
        rows.append((mnt, total, avail, parts[0]))
    return rows


def disks() -> List[dict]:
    """Disks section: ``[{mount, fs, total, free, used_pct}]``."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        ps = _psutil()
        if ps is None:
            return _win_disks()
    if IS_MAC:  # pragma: no cover - macOS only
        return _mac_disks()
    from . import wsl
    in_wsl = wsl.is_wsl()
    mounts = parse_mounts(_read(os.path.join(_PROC, "mounts")) or "", wsl=in_wsl)
    out = []
    seen_dev = set()
    for mnt, fs in mounts:
        v = _statvfs(mnt)
        if not v or not v[0]:
            continue
        key = (fs, v[0], v[1]) if fs in ("overlay",) else None
        if key and key in seen_dev:
            continue
        if key:
            seen_dev.add(key)
        out.append(_disk_row(mnt, fs, v[0], v[1]))
    return out


def _mac_disks() -> List[dict]:  # pragma: no cover - macOS only
    df = _tool("df", ["/bin/df"])
    out = _run([df, "-kP"], env=dict(os.environ, LC_ALL="C")) if df else None
    types: Dict[str, str] = {}
    mnt_exe = _tool("mount", ["/sbin/mount"])
    mt = _run([mnt_exe]) if mnt_exe else None
    for line in (mt or "").splitlines():
        m = re.match(r"^\S+ on (.+) \(([^,)]+)", line)
        if m:
            types[m.group(1)] = m.group(2)
    rows = []
    for mnt, total, free, dev in parse_df(out or ""):
        if not dev.startswith("/dev/") or not total:
            continue
        if mnt.startswith("/System/Volumes/") and mnt != "/System/Volumes/Data":
            continue
        rows.append(_disk_row(mnt, types.get(mnt, "unknown"), total, free))
    return rows


# ---------------------------------------------------------------------------------------------------------------
# network

def _linux_addresses() -> List[str]:
    out: List[str] = []
    txt = _read(os.path.join(_PROC, "net", "fib_trie")) or ""
    prev = None
    for line in txt.splitlines():
        s = line.strip()
        if s.startswith("/32 host LOCAL") and prev:
            if prev not in out:
                out.append(prev)
        m = re.match(r"^\|--\s+(\d+\.\d+\.\d+\.\d+)$", s)
        prev = m.group(1) if m else (prev if s.startswith("/") else None)
    for line in (_read(os.path.join(_PROC, "net", "if_inet6")) or "").splitlines():
        parts = line.split()
        if parts and len(parts[0]) == 32:
            import ipaddress
            a = str(ipaddress.IPv6Address(bytes.fromhex(parts[0])))
            if a not in out:
                out.append(a)
    return out


def _ifconfig_addresses() -> List[str]:  # pragma: no cover - macOS only
    exe = _tool("ifconfig", ["/sbin/ifconfig"])
    out = _run([exe]) if exe else None
    addrs = []
    for m in re.finditer(r"\binet6?\s+([0-9a-fA-F:.%]+)", out or ""):
        a = m.group(1).split("%", 1)[0]
        if a not in addrs:
            addrs.append(a)
    return addrs


def addresses() -> List[str]:
    """Local IP addresses (no loopback, no link-local)."""
    raw: List[str] = []
    ps = _psutil()
    if ps is not None:
        try:
            for addrs in ps.net_if_addrs().values():
                for a in addrs:
                    if a.family in (socket.AF_INET, socket.AF_INET6):
                        raw.append(str(a.address))
        except Exception:  # noqa: BLE001
            raw = []
    if not raw:
        if sys.platform.startswith("linux"):
            raw = _linux_addresses()
        elif IS_MAC:  # pragma: no cover - macOS only
            raw = _ifconfig_addresses()
    if not raw:
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None):
                raw.append(str(info[4][0]))
        except OSError:
            pass
    out: List[str] = []
    import ipaddress
    for a in raw:
        a = a.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            continue
        if a not in out:
            out.append(a)
    return out


def _primary_ip() -> Optional[str]:
    """The source address of the default route (UDP connect: no packet is sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("0.") else None
    except OSError:
        return None
    finally:
        s.close()


def tailscale_ip(addrs: Optional[List[str]] = None) -> Optional[str]:
    """An address in 100.64.0.0/10 or fd7a:115c:a1e0::/48 (Tailscale), else None."""
    import ipaddress
    cgnat = ipaddress.ip_network("100.64.0.0/10")
    ts6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
    v6 = None
    for a in addrs if addrs is not None else addresses():
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            continue
        if ip.version == 4 and ip in cgnat:
            return a
        if ip.version == 6 and ip in ts6 and v6 is None:
            v6 = a
    return v6


def network() -> dict:
    """Network section: primary_ip, addresses, tailscale_ip."""
    addrs = addresses()
    prim = _primary_ip()
    if prim and prim not in addrs:
        addrs.insert(0, prim)
    return {"primary_ip": prim or (addrs[0] if addrs else None), "addresses": addrs, "tailscale_ip": tailscale_ip(addrs)}


# ---------------------------------------------------------------------------------------------------------------
# privileges

def is_admin() -> bool:
    """Running as root/Administrator."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return _win_admin()
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


# ---------------------------------------------------------------------------------------------------------------
# software / which

def version_line(name: str, path: str, timeout: float = 3.0) -> Optional[str]:
    """First meaningful line of ``<path> --version`` (``-version`` for ffmpeg; nvcc: the "release" line)."""
    if name == "wsl" and IS_WINDOWS:  # pragma: no cover - Windows only
        from . import wsl
        return wsl.version_line()
    from . import proctree
    try:
        code, out, err = proctree.run_quick([path, _VERSION_FLAG.get(name, "--version")], timeout=timeout,
                                            max_output=65536)
    except Exception:  # noqa: BLE001
        return None
    text = util.decode_text(out).strip() or util.decode_text(err).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    if name == "nvcc":
        rel = [ln for ln in lines if "release" in ln.lower()]
        if rel:
            return rel[0][:200]
    return lines[0][:200]


def _probe(name: str, search_path: str) -> Optional[str]:
    from . import proctree
    exe_name = "wsl.exe" if (name == "wsl" and IS_WINDOWS) else name
    if name == "wsl" and not IS_WINDOWS:
        return None
    if name == "nvidia-smi":
        from . import gpu
        path = gpu.find_tool("nvidia-smi")
    else:
        path = proctree.which(exe_name, search_path)
    if not path:
        return None
    v = version_line(name, path)
    return f"{v or 'unknown version'} @ {path}"


def software(names: Iterable[str], refresh: bool = False) -> Dict[str, Optional[str]]:
    """``{name: "version-line @ path" | None}`` (trusted PATH only; ``--version`` 3 s each, in parallel, 5 min cache)."""
    from . import proctree
    names = list(dict.fromkeys(names))
    tp = proctree.trusted_path()
    out: Dict[str, Optional[str]] = {}
    todo = []
    now = time.monotonic()
    with _cache_lock:
        for n in names:
            hit = _cache.get("sw:" + n)
            if hit is not None and not refresh and now - hit[0] < SOFTWARE_TTL and hit[1][0] == tp:
                out[n] = hit[1][1]
            else:
                todo.append(n)
    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(todo))) as ex:
            futs = {n: ex.submit(_probe, n, tp) for n in todo}
            for n, f in futs.items():
                try:
                    out[n] = f.result(timeout=10)
                except Exception:  # noqa: BLE001
                    out[n] = None
                with _cache_lock:
                    _cache["sw:" + n] = (time.monotonic(), (tp, out[n]))
    return {n: out.get(n) for n in names}


def which(names: Iterable[str], *, node_path: str, versions: bool = True) -> dict:
    """``{"found": {name: {path, version|None, note?}}, "missing": [...]}`` on the node PATH; ``--version`` only for
    VERSION_NAMES and only when the hit lies on the trusted PATH (§9.2 v3/v5)."""
    from . import proctree
    tp = proctree.trusted_path()
    tdirs = {os.path.normcase(os.path.realpath(d)) for d in tp.split(os.pathsep) if d}
    found: Dict[str, dict] = {}
    missing: List[str] = []
    jobs: Dict[str, str] = {}
    for n in dict.fromkeys(names):
        exe = "wsl.exe" if (n == "wsl" and IS_WINDOWS) else n
        p = proctree.which(exe, node_path)
        if not p:
            missing.append(n)
            continue
        found[n] = {"path": p, "version": None}
        trusted = os.path.normcase(os.path.realpath(os.path.dirname(p))) in tdirs
        if not trusted:
            found[n]["note"] = "in a writable root; not executed"
        elif versions and n in VERSION_NAMES:
            jobs[n] = p
    if jobs:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            futs = {n: ex.submit(version_line, n, p) for n, p in jobs.items()}
            for n, f in futs.items():
                try:
                    found[n]["version"] = f.result(timeout=10)
                except Exception:  # noqa: BLE001
                    found[n]["version"] = None
    return {"found": found, "missing": missing}


# ---------------------------------------------------------------------------------------------------------------
# capabilities / summary / live status

def _docker_caps() -> Tuple[bool, Optional[bool]]:
    from . import proctree
    exe = proctree.which("docker", proctree.trusted_path())
    if not exe:
        return False, (False if sys.platform.startswith("linux") else None)
    try:
        code, out, _err = proctree.run_quick([exe, "version", "--format", "{{.Server.Version}}"], timeout=5)
    except Exception:  # noqa: BLE001
        code, out = 1, b""
    ok = code == 0 and bool(util.decode_text(out).strip())
    if not sys.platform.startswith("linux"):
        return ok, None
    gpu = os.path.exists("/etc/cdi/nvidia.yaml")
    if ok and not gpu:
        try:
            code, out, _err = proctree.run_quick([exe, "info", "--format", "{{json .Runtimes}}"], timeout=5)
            gpu = code == 0 and "nvidia" in util.decode_text(out).lower()
        except Exception:  # noqa: BLE001
            gpu = False
    return ok, bool(gpu)


def capabilities(refresh: bool = False) -> dict:
    """``{"psutil","nvidia_smi","docker","docker_gpu","wsl"}`` (cached 60 s)."""
    def build() -> dict:
        from . import gpu, wsl
        docker, docker_gpu = _docker_caps()
        try:
            has_wsl = wsl.has_distros() if IS_WINDOWS else False
        except Exception:  # noqa: BLE001
            has_wsl = False
        return {"psutil": _psutil() is not None, "nvidia_smi": bool(gpu.find_tool("nvidia-smi")) if not IS_MAC
                else False, "docker": docker, "docker_gpu": docker_gpu, "wsl": bool(has_wsl)}
    return dict(_cached("caps", CAPS_TTL, build, refresh=refresh))


def summary_line() -> str:
    """One-line GPU summary, e.g. "1× NVIDIA RTX 4090 24GB" ("" without GPUs; static, cached 60 s)."""
    def build() -> str:
        from . import gpu
        return gpu.summary(gpu.detect(processes=False).get("gpus") or [])
    return str(_cached("summary", STATIC_TTL, build))


def live_status() -> dict:
    """<= 4 KiB live status for relay/NodeSummary: ``{cpu_pct, mem_pct, gpus:[{util, mem_used_mb}]}`` (5 s cache)."""
    def build() -> dict:
        from . import gpu
        try:
            mem = memory()
            mem_pct = mem.get("used_pct")
        except Exception:  # noqa: BLE001
            mem_pct = None
        gpus = []
        if not IS_MAC:
            for g in (gpu.detect(processes=False, timeout=5).get("gpus") or [])[:16]:
                gpus.append({"util": g.get("utilization_pct"), "mem_used_mb": g.get("memory_used_mb")})
        return {"cpu_pct": _cpu_since_last(), "mem_pct": mem_pct, "gpus": gpus}
    return dict(_cached("live", LIVE_TTL, build))


__all__ = ["os_info", "cpu_info", "memory", "disks", "cpu_percent", "load_avg", "addresses", "uptime", "is_admin",
           "cloud_info", "software", "capabilities", "summary_line", "live_status", "network", "which",
           "tailscale_ip", "container_limit", "parse_mounts", "parse_df", "parse_vm_stat", "version_line",
           "clear_cache", "SOFTWARE", "VERSION_NAMES"]
