"""NVIDIA/Apple/Windows GPU detection (AMD names only) (design §12.2, WP4).

Every external call goes through ``proctree.run_quick`` with a timeout, and programs are resolved only on
``proctree.trusted_path()`` plus the fixed tool locations of §12.2 (a fake ``nvidia-smi`` an agent wrote into an rw
root is never executed, §9.2 v3). Static results (CUDA version, Apple/Windows/lspci inventories) are cached 60 s
(system_profiler 5 min); live metrics are never cached.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import util

log = logging.getLogger("hive.gpu")

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

WSL_SMI = "/usr/lib/wsl/lib/nvidia-smi"
FIXED_SMI = [r"C:\Windows\System32\nvidia-smi.exe", r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe",
             "/usr/bin/nvidia-smi"]
QUERY_GPU = ("index,name,uuid,memory.total,memory.used,memory.free,utilization.gpu,temperature.gpu,power.draw,"
             "power.limit,driver_version")
QUERY_APPS = "pid,process_name,gpu_uuid,used_memory"
STATIC_TTL = 60.0
APPLE_TTL = 300.0
WSL_NOTE = "per-process GPU usage is not available in WSL"
AMD_NOTE = "AMD metrics not supported in this version"
CONTAINER_NOTE = "per-process GPU usage is not visible inside this container (PID namespace)"
NO_SMI_NOTE = "nvidia-smi not found"
WIN_RAM_NOTE = "memory from Win32_VideoController is capped at 4 GiB"

_cache: Dict[str, Tuple[float, Any]] = {}
_cache_lock = threading.Lock()


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
    """Forget cached static GPU data (tests, system.info refresh)."""
    with _cache_lock:
        _cache.clear()


def _is_wsl() -> bool:
    try:
        from . import wsl
        return wsl.is_wsl()
    except Exception:  # noqa: BLE001
        return False


def find_tool(name: str) -> Optional[str]:
    """Locate a GPU tool through the trusted path and fixed locations (nvidia-smi: WSL path first, §12.2)."""
    from . import proctree
    if name == "nvidia-smi":
        if not IS_WINDOWS and _is_wsl() and os.path.isfile(WSL_SMI) and os.access(WSL_SMI, os.X_OK):
            return WSL_SMI
        p = proctree.which(name, proctree.trusted_path())
        if p:
            return p
        for f in FIXED_SMI:
            if (f.endswith(".exe")) != IS_WINDOWS:
                continue
            if os.path.isfile(f) and (IS_WINDOWS or os.access(f, os.X_OK)):
                return f
        return None
    return proctree.which(name, proctree.trusted_path())


def _run(argv: List[str], timeout: float) -> Tuple[Optional[int], str, str]:
    """(code|None, stdout, stderr) via run_quick; None when the program could not run or timed out."""
    from . import proctree
    try:
        code, out, err = proctree.run_quick(argv, timeout=timeout)
    except Exception as e:  # noqa: BLE001
        return None, "", str(getattr(e, "message", e))
    return code, util.decode_text(out), util.decode_text(err)


def _num(v: Any) -> Optional[float]:
    s = str(v or "").strip()
    if not s or s.startswith("[") or s.upper() in ("N/A", "NA"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _int_or_none(v: Optional[float]) -> Optional[int]:
    return int(round(v)) if v is not None else None


def _text(v: Any) -> Optional[str]:
    s = str(v or "").strip()
    if not s or s.startswith("[") or s.upper() == "N/A":
        return None
    return s


def _csv_rows(text: str) -> List[List[str]]:
    rows = []
    for row in csv.reader(io.StringIO(text), skipinitialspace=True):
        if row and any(c.strip() for c in row):
            rows.append([c.strip() for c in row])
    return rows


def parse_query_gpu(text: str) -> List[dict]:
    """``nvidia-smi --query-gpu=… --format=csv,noheader,nounits`` -> GPU rows (``[N/A]``/``[Not Supported]`` -> null)."""
    gpus = []
    for r in _csv_rows(text):
        if len(r) < 11:
            continue
        idx = _num(r[0])
        total = _num(r[3])
        power = _num(r[8])
        gpus.append({"index": int(idx) if idx is not None else len(gpus), "vendor": "nvidia", "name": r[1],
                     "uuid": _text(r[2]), "memory_total_mb": _int_or_none(total) or 0,
                     "memory_used_mb": _int_or_none(_num(r[4])), "utilization_pct": _num(r[6]),
                     "temperature_c": _num(r[7]), "power_w": round(power, 1) if power is not None else None,
                     "driver": _text(r[10]), "cuda": None, "unified_memory": False, "processes": []})
    return gpus


def parse_compute_apps(text: str) -> List[dict]:
    """``--query-compute-apps=pid,process_name,gpu_uuid,used_memory`` -> [{pid, name, gpu_uuid, memory_mb}]."""
    out = []
    for r in _csv_rows(text):
        if len(r) < 4:
            continue
        pid = _num(r[0])
        if pid is None:
            continue
        out.append({"pid": int(pid), "name": _text(r[1]), "gpu_uuid": _text(r[2]),
                    "memory_mb": _int_or_none(_num(r[3]))})
    return out


_CUDA_RE = re.compile(r"CUDA Version:\s*([\d.]+)")


def parse_cuda_version(text: str) -> Optional[str]:
    """CUDA version from the plain ``nvidia-smi`` header."""
    m = _CUDA_RE.search(text or "")
    return m.group(1) if m else None


def _in_container() -> bool:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    if os.environ.get("RUNPOD_POD_ID") or os.environ.get("VAST_CONTAINERLABEL") or os.environ.get("container"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as f:
            data = f.read()
        return any(k in data for k in ("docker", "kubepods", "containerd", "lxc"))
    except OSError:
        return False


def _nvidia(smi: str, processes: bool, timeout: float) -> dict:
    res: Dict[str, Any] = {"backend": "nvidia", "gpus": [], "processes": [], "errors": [], "driver": None,
                           "cuda": None}
    code, out, err = _run([smi, "--query-gpu=" + QUERY_GPU, "--format=csv,noheader,nounits"], timeout)
    if code != 0:
        msg = (out.strip() or err.strip() or "nvidia-smi failed").splitlines()[0][:300]
        res["errors"].append(f"nvidia-smi: {msg}")
        return res
    gpus = parse_query_gpu(out)
    cuda = _cached("cuda:" + smi, STATIC_TTL, lambda: parse_cuda_version(_run([smi], timeout)[1]))
    for g in gpus:
        g["cuda"] = cuda
    res["gpus"] = gpus
    res["driver"] = next((g["driver"] for g in gpus if g.get("driver")), None)
    res["cuda"] = cuda
    if not processes:
        return res
    if _is_wsl():
        res["errors"].append(WSL_NOTE)
        return res
    code, out, _err = _run([smi, "--query-compute-apps=" + QUERY_APPS, "--format=csv,noheader,nounits"], timeout)
    if code != 0:
        res["errors"].append("nvidia-smi: per-process query failed")
        return res
    apps = parse_compute_apps(out)
    res["processes"] = apps
    by_uuid = {g.get("uuid"): g for g in gpus}
    for a in apps:
        g = by_uuid.get(a.get("gpu_uuid"))
        if g is None and len(gpus) == 1:
            g = gpus[0]
        if g is not None:
            g["processes"].append({"pid": a["pid"], "name": a["name"], "memory_mb": a["memory_mb"]})
    if not apps and _in_container() and any((g.get("memory_used_mb") or 0) > 0 for g in gpus):
        res["errors"].append(CONTAINER_NOTE)
    return res


def _vendor(text: str) -> str:
    t = (text or "").lower()
    if "nvidia" in t:
        return "nvidia"
    if "advanced micro devices" in t or "amd" in t.split() or t.startswith("amd") or "ati " in t or "radeon" in t:
        return "amd"
    if "intel" in t:
        return "intel"
    if "apple" in t:
        return "apple"
    if "microsoft" in t:
        return "microsoft"
    return (t.split() or ["unknown"])[0]


def parse_lspci(text: str) -> List[dict]:
    """``lspci -mm`` -> display controllers [{vendor, name}] (names only)."""
    out = []
    for line in (text or "").splitlines():
        fields = re.findall(r'"([^"]*)"', line)
        if len(fields) < 3:
            continue
        cls = fields[0].lower()
        if not any(k in cls for k in ("vga", "3d controller", "display controller")):
            continue
        out.append({"vendor": _vendor(fields[1]), "name": f"{fields[1]} {fields[2]}".strip()})
    return out


def _names_only(rows: List[dict], start: int = 0) -> List[dict]:
    gpus = []
    for i, r in enumerate(rows):
        gpus.append({"index": start + i, "vendor": r["vendor"], "name": r["name"], "uuid": None,
                     "memory_total_mb": int(r.get("memory_total_mb") or 0), "memory_used_mb": None,
                     "utilization_pct": None, "temperature_c": None, "power_w": None,
                     "driver": r.get("driver"), "cuda": None, "unified_memory": False, "processes": []})
    return gpus


def _lspci(timeout: float) -> List[dict]:
    from . import proctree
    exe = proctree.which("lspci", proctree.trusted_path()) or next(
        (f for f in ("/usr/bin/lspci", "/sbin/lspci", "/usr/sbin/lspci") if os.path.isfile(f)), None)
    if not exe:
        return []
    code, out, _err = _run([exe, "-mm"], timeout)
    return parse_lspci(out) if code == 0 else []


def parse_win_video(data: Any) -> List[dict]:
    """``Get-CimInstance Win32_VideoController | Select Name,AdapterRAM,DriverVersion | ConvertTo-Json`` rows."""
    if isinstance(data, dict):
        data = [data]
    rows = []
    for d in data or []:
        if not isinstance(d, dict) or not d.get("Name"):
            continue
        ram = d.get("AdapterRAM")
        mb = int(ram) // (1 << 20) if isinstance(ram, (int, float)) and ram > 0 else 0
        rows.append({"vendor": _vendor(str(d["Name"])), "name": str(d["Name"]), "memory_total_mb": mb,
                     "driver": d.get("DriverVersion") or None})
    return rows


def _windows_cim(timeout: float) -> List[dict]:  # pragma: no cover - Windows only
    from . import proctree
    root = os.environ.get("SystemRoot", r"C:\Windows")
    ps = proctree.which("powershell", proctree.trusted_path()) or os.path.join(
        root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    code, out, _err = _run([ps, "-NoProfile", "-NonInteractive", "-Command",
                            "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);"
                            "Get-CimInstance Win32_VideoController | Select Name,AdapterRAM,DriverVersion | "
                            "ConvertTo-Json -Compress"], timeout)
    if code != 0:
        return []
    try:
        return parse_win_video(json.loads(out or "null"))
    except ValueError:
        return []


def _memsize_mb() -> int:
    try:
        from . import sysinfo
        return int(sysinfo.memory().get("total") or 0) // (1 << 20)
    except Exception:  # noqa: BLE001
        return 0


def parse_system_profiler(data: Any, memsize_mb: int = 0) -> List[dict]:
    """``system_profiler SPDisplaysDataType -json`` -> GPU rows (Apple Silicon: unified memory = hw.memsize)."""
    items = (data or {}).get("SPDisplaysDataType") if isinstance(data, dict) else None
    out = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            continue
        name = str(it.get("sppci_model") or it.get("_name") or "GPU")
        vend_raw = str(it.get("spdisplays_vendor") or it.get("sppci_vendor") or name)
        vendor = "apple" if ("apple" in vend_raw.lower() or name.lower().startswith("apple")) else _vendor(vend_raw)
        unified = vendor == "apple"
        mem = memsize_mb if unified else 0
        if not unified:
            vram = str(it.get("spdisplays_vram") or it.get("sppci_vram") or it.get("spdisplays_vram_shared") or "")
            m = re.match(r"\s*(\d+)\s*(GB|MB)", vram, re.I)
            if m:
                mem = int(m.group(1)) * (1024 if m.group(2).upper() == "GB" else 1)
        row = _names_only([{"vendor": vendor, "name": name, "memory_total_mb": mem}], i)[0]
        row["unified_memory"] = unified
        out.append(row)
    return out


def _apple(timeout: float) -> List[dict]:  # pragma: no cover - macOS only
    from . import proctree
    exe = proctree.which("system_profiler", proctree.trusted_path()) or "/usr/sbin/system_profiler"

    def build() -> List[dict]:
        code, out, _err = _run([exe, "SPDisplaysDataType", "-json"], max(timeout, 15))
        if code != 0:
            return []
        try:
            return parse_system_profiler(json.loads(out or "null"), _memsize_mb())
        except ValueError:
            return []
    return [dict(r, processes=[]) for r in _cached("apple", APPLE_TTL, build)]


def detect(processes: bool = True, timeout: float = 8.0) -> dict:
    """``{"backend","gpus":[…],"processes":[…],"errors":[…],"driver","cuda"}`` (§12.2; never raises)."""
    try:
        return _detect(processes, timeout)
    except Exception as e:  # noqa: BLE001 - a probe must never break node.info/system.info
        log.debug("gpu detection failed", exc_info=True)
        return {"backend": None, "gpus": [], "processes": [], "errors": [f"gpu detection failed: {e}"],
                "driver": None, "cuda": None}


def _detect(processes: bool, timeout: float) -> dict:
    empty: Dict[str, Any] = {"backend": None, "gpus": [], "processes": [], "errors": [], "driver": None, "cuda": None}
    smi = None if IS_MAC else find_tool("nvidia-smi")
    if smi:
        res = _nvidia(smi, processes, timeout)
        if res["gpus"]:
            return res
        empty["errors"] = list(res["errors"])
    if IS_MAC:  # pragma: no cover - macOS only
        gpus = _apple(timeout)
        return dict(empty, backend="apple" if gpus else None, gpus=gpus)
    if IS_WINDOWS:  # pragma: no cover - Windows only
        rows = _cached("wincim", STATIC_TTL, lambda: _windows_cim(timeout))
        gpus = _names_only(rows)
        errs = list(empty["errors"])
        if not smi:
            errs.append(NO_SMI_NOTE)
        if any(r.get("memory_total_mb") for r in rows):
            errs.append(WIN_RAM_NOTE)
        if any(g["vendor"] == "amd" for g in gpus):
            errs.append(AMD_NOTE)
        return dict(empty, backend="cim" if gpus else None, gpus=gpus, errors=errs)
    rows = _lspci_cached(timeout)
    gpus = _names_only(rows)
    errs = list(empty["errors"])
    if not smi:
        errs.append(NO_SMI_NOTE)
    if any(g["vendor"] == "amd" for g in gpus):
        errs.append(AMD_NOTE)
    return dict(empty, backend="lspci" if gpus else None, gpus=gpus, errors=errs)


def _lspci_cached(timeout: float) -> List[dict]:
    return [dict(r) for r in _cached("lspci", STATIC_TTL, lambda: _lspci(timeout))]


def process_memory() -> Dict[int, int]:
    """pid -> MiB (nvidia compute apps); ``{}`` on WSL, without nvidia-smi or on failure."""
    if IS_MAC or _is_wsl():
        return {}
    smi = find_tool("nvidia-smi")
    if not smi:
        return {}
    code, out, _err = _run([smi, "--query-compute-apps=" + QUERY_APPS, "--format=csv,noheader,nounits"], 8.0)
    if code != 0:
        return {}
    out_map: Dict[int, int] = {}
    for a in parse_compute_apps(out):
        if a.get("memory_mb") is not None:
            out_map[a["pid"]] = out_map.get(a["pid"], 0) + int(a["memory_mb"])
    return out_map


def note(result: dict) -> Optional[str]:
    """``gpu_note`` for system.info: the detection errors joined, or null."""
    errs = [e for e in result.get("errors") or [] if e]
    return "; ".join(dict.fromkeys(errs)) if errs else None


def summary(gpus: List[dict]) -> str:
    """"1× NVIDIA RTX 4090 24GB" (grouped by name; Apple: "Apple M2 Pro (unified 32GB)"); "" without GPUs."""
    groups: Dict[Tuple[str, int, bool], int] = {}
    for g in gpus or []:
        key = (str(g.get("name") or "GPU"), int(g.get("memory_total_mb") or 0), bool(g.get("unified_memory")))
        groups[key] = groups.get(key, 0) + 1
    parts = []
    for (name, mb, unified), n in groups.items():
        gb = f"{round(mb / 1024)}GB" if mb else ""
        if unified:
            parts.append(f"{name} (unified {gb})" if gb else name)
        else:
            parts.append(f"{n}× {name}" + (f" {gb}" if gb else ""))
    return ", ".join(parts)


__all__ = ["detect", "process_memory", "find_tool", "parse_query_gpu", "parse_compute_apps", "parse_cuda_version",
           "parse_lspci", "parse_win_video", "parse_system_profiler", "note", "summary", "clear_cache",
           "WSL_NOTE", "AMD_NOTE", "NO_SMI_NOTE"]
