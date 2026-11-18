"""Action handlers: system.info (incl. `which`), proc.list, proc.kill (design §9.2–§9.3, WP4).

Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
Every internal probe resolves programs on ``proctree.trusted_path()`` (§9.2 v3); ``which`` looks names up on the
node PATH and runs ``--version`` only for known names found on the trusted PATH.
"""
from __future__ import annotations

import os
import signal
import time
from typing import Any, Dict, List, Optional

from .. import gpu, procinfo, proctree, sysinfo, util
from ..errors import HiveError, not_found
from . import ActionContext, handler

ALL_SECTIONS = ("os", "cpu", "memory", "disk", "gpu", "network", "software")
CMD_MAX = 300
FILTER_MAX = 4096                   # proc.list filter: name + this much of the redacted command line


# ---------------------------------------------------------------------------------------------------------------
# system.info

@handler("system.info")
def system_info(ctx: ActionContext, p: dict) -> dict:
    """Hardware/OS/software facts; only the requested sections (``which`` alone when sections is omitted)."""
    sections: List[str] = list(p.get("sections") or [])
    if "which" in ctx.given and "sections" not in ctx.given:
        sections = []
    if "all" in sections:
        sections = list(ALL_SECTIONS)
    refresh = bool(p.get("refresh"))
    if refresh:
        sysinfo.clear_cache()
        gpu.clear_cache()
    out: Dict[str, Any] = {}
    for sec in dict.fromkeys(sections):
        ctx.check()
        if sec == "os":
            out["os"] = sysinfo.os_info()
        elif sec == "cpu":
            out["cpu"] = sysinfo.cpu_info()
        elif sec == "memory":
            out["memory"] = sysinfo.memory()
        elif sec == "disk":
            out["disks"] = sysinfo.disks()
        elif sec == "gpu":
            res = gpu.detect(processes=True, timeout=max(1.0, min(8.0, ctx.remaining() - 2.0)))
            out["gpus"] = res.get("gpus") or []
            out["gpu_backend"] = res.get("backend")
            out["gpu_note"] = gpu.note(res)
        elif sec == "network":
            out["network"] = sysinfo.network()
        elif sec == "software":
            out["software"] = sysinfo.software(sysinfo.SOFTWARE, refresh=refresh)
    if p.get("which"):
        node_path = getattr(ctx.policy, "node_path", None) or os.environ.get("PATH", "")
        out["which"] = sysinfo.which(p["which"], node_path=node_path, versions=bool(p.get("versions", True)))
    return out


# ---------------------------------------------------------------------------------------------------------------
# proc.list

def _joined(cmd: Any) -> str:
    """The whole command line joined with spaces ("" for none)."""
    if not cmd:
        return ""
    return " ".join(str(c) for c in cmd) if isinstance(cmd, (list, tuple)) else str(cmd)


def _filter_text(cmd: Any) -> str:
    """The redacted head (FILTER_MAX chars) of the command line that ``filter`` is matched against."""
    from ..audit import redact_head
    return redact_head(_joined(cmd), FILTER_MAX)[0]


def _cmd_text(cmd: Any) -> Optional[str]:
    """Audit round 4 (DoS): only a bounded head of the command line is redacted (``audit.redact_head``), so a
    local user's process with a megabyte argv cannot stall proc.list."""
    if not cmd:
        return None
    from ..audit import redact_head
    text, cut = redact_head(_joined(cmd), CMD_MAX - 1)
    return text + "…" if cut else text


def _user_key(u: Optional[str]) -> str:
    s = str(u or "")
    if "\\" in s:
        s = s.rsplit("\\", 1)[1]
    return s.casefold()


def _managed(ctx: ActionContext) -> Dict[int, str]:
    try:
        return dict(ctx.rt.managed_pids() or {})
    except Exception:  # noqa: BLE001 - a manager may be unavailable
        return {}


@handler("proc.list")
def proc_list(ctx: ActionContext, p: dict) -> dict:
    """Processes on the node (filter, sort, limit); Hive-managed ones are marked."""
    flt = (p.get("filter") or "").casefold()
    want_cmd = bool(p.get("cmdline", True))
    rows, method = procinfo.list_processes(cmdline=want_cmd or bool(flt))
    ctx.check()
    pids = set(p.get("pids") or [])
    user = p.get("user")
    sel = []
    for r in rows:
        if pids and r.get("pid") not in pids:
            continue
        if user and _user_key(r.get("user")) != _user_key(user):
            continue
        if flt:
            # matched against redacted text like `cmd` shows (not cut to 300 chars): `total`/`shown` must not
            # become an oracle for secrets on a command line (§5.8). Round 4 (DoS): the first FILTER_MAX chars only
            hay = (str(r.get("name") or "") + " " + _filter_text(r.get("cmdline"))).casefold()
            if flt not in hay:
                continue
        sel.append(r)
    sort = p.get("sort") or "memory"
    if sort == "memory":
        sel.sort(key=lambda r: (r.get("mem_bytes") is None, -(r.get("mem_bytes") or 0), r.get("pid") or 0))
    elif sort == "cpu":
        sel.sort(key=lambda r: (r.get("cpu_pct") is None, -(r.get("cpu_pct") or 0), r.get("pid") or 0))
    elif sort == "pid":
        sel.sort(key=lambda r: r.get("pid") or 0)
    elif sort == "name":
        sel.sort(key=lambda r: (str(r.get("name") or "").casefold(), r.get("pid") or 0))
    elif sort == "started":
        sel.sort(key=lambda r: (r.get("create_time") is None, -(r.get("create_time") or 0)))
    limit = int(p.get("limit", 30))
    managed = _managed(ctx)
    out = []
    for r in sel[:limit]:
        ct = r.get("create_time")
        cpu = r.get("cpu_pct")
        out.append({"pid": int(r["pid"]), "ppid": r.get("ppid"), "name": str(r.get("name") or ""),
                    "user": r.get("user"), "cpu_pct": cpu, "mem_bytes": r.get("mem_bytes"),
                    "started": util.iso_utc(ct) if isinstance(ct, (int, float)) else None,
                    "cmd": _cmd_text(r.get("cmdline")) if want_cmd else None,
                    "managed": managed.get(int(r["pid"]))})
    return {"processes": out, "total": len(sel), "shown": len(out), "method": method}


# ---------------------------------------------------------------------------------------------------------------
# proc.kill

def _ppid_map() -> Optional[Dict[int, int]]:
    """{pid: ppid} from one snapshot on Windows (psutil | Toolhelp32), where ``process_info`` without psutil starts a
    PowerShell CIM query per pid; None elsewhere (``/proc`` and ``ps`` reads are cheap)."""
    if not util.IS_WINDOWS:
        return None
    ps = procinfo._psutil()  # pragma: no cover - Windows only below
    if ps is not None:
        try:
            return {int(p.info["pid"]): int(p.info["ppid"] or 0) for p in ps.process_iter(["pid", "ppid"])}
        except Exception:  # noqa: BLE001
            pass
    from .. import winapi
    rows = winapi.process_list()
    return {int(pid): int(ppid) for pid, ppid, _n in rows} if rows else None


def _ancestors(pid: int, pmap: Optional[Dict[int, int]] = None) -> List[int]:
    """``pid`` and its ancestors (bounded walk over ``pmap`` when given, else ``procinfo.process_info``)."""
    out = [pid]
    cur = pid
    for _ in range(64):
        if pmap is not None:
            pp = pmap.get(cur)
        else:
            info = procinfo.process_info(cur)
            pp = info.get("ppid") if info else None
        if not isinstance(pp, int) or pp <= 0 or pp in out:
            break
        out.append(pp)
        cur = pp
    return out


def protected_pids(ctx: ActionContext) -> Dict[int, str]:
    """{node pid and its ancestors: "hive node"} ∪ managed pids (jobs, services, WSL keepalive) (§5.4 kill guard)."""
    prot: Dict[int, str] = {}
    self_pid = int(getattr(ctx.rt, "self_pid", None) or os.getpid())
    for a in _ancestors(self_pid, _ppid_map()):
        prot[a] = "hive node"
    prot.update(_managed(ctx))
    try:
        prot.update(dict(ctx.rt.keepalive_pids() or {}))
    except Exception:  # noqa: BLE001
        pass
    return prot


def _kill_one(pid: int, create_time: Optional[float], *, grace: float, force: bool) -> Dict[str, Any]:
    """Stop one process (no descendants): SIGTERM, wait ``grace``, SIGKILL (POSIX); taskkill / taskkill /F /
    TerminateProcess (Windows)."""
    if not util.pid_alive(pid, create_time):
        return {"killed": [], "still_alive": [], "method": "taskkill" if util.IS_WINDOWS else "signal"}
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        from .. import winapi
        method = "taskkill/F" if force else "taskkill"
        exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
        if not force:
            try:
                proctree.run_quick([exe, "/PID", str(pid)], timeout=15)
            except Exception:  # noqa: BLE001
                pass
            end = time.monotonic() + grace
            while time.monotonic() < end and util.pid_alive(pid, create_time):
                time.sleep(0.1)
        if util.pid_alive(pid, create_time):
            try:
                proctree.run_quick([exe, "/PID", str(pid), "/F"], timeout=15)
                method = "taskkill/F"
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.2)
            if util.pid_alive(pid, create_time) and winapi.terminate_pid(pid):
                method = "terminate"
    else:
        method = "signal"
        if not force:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
            end = time.monotonic() + grace
            while time.monotonic() < end and util.pid_alive(pid, create_time):
                time.sleep(0.1)
        if util.pid_alive(pid, create_time):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    end = time.monotonic() + 3.0
    while time.monotonic() < end and util.pid_alive(pid, create_time):
        time.sleep(0.05)
    alive = util.pid_alive(pid, create_time)
    return {"killed": [] if alive else [pid], "still_alive": [pid] if alive else [], "method": method}


def _group_members(pid: int) -> List[int]:
    """POSIX: the other members of ``pid``'s process group when ``pid`` leads one (``kill_tree`` signals the whole
    group then); [] on Windows or when it does not lead a group."""
    if util.IS_WINDOWS:
        return []
    try:
        if os.getpgid(pid) != pid:
            return []
    except OSError:
        return []
    if os.path.isdir("/proc/self"):
        pids = [int(n) for n in os.listdir("/proc") if n.isdigit()]
    else:
        pids = [r.get("pid") for r in procinfo.list_processes(cmdline=False)[0]]
    out = []
    for q in pids:
        if not isinstance(q, int) or q == pid:
            continue
        try:
            if os.getpgid(q) == pid:
                out.append(q)
        except OSError:
            continue
    return out


def _check_tree(ctx: ActionContext, pid: int, prot: Dict[int, str]) -> None:
    """The kill guard for every process a tree kill would signal (descendants + group members): the first refusal
    refuses the whole call, so a protected process cannot be killed through an unprotected parent (§5.4)."""
    targets = [d for d in dict.fromkeys(list(proctree.descendants(pid)) + _group_members(pid)) if d != pid]
    rows: Optional[Dict[int, dict]] = None
    if util.IS_WINDOWS and len(targets) > 1:  # pragma: no cover - Windows only
        # without psutil each process_info is a PowerShell CIM query: one listing serves every descendant
        rows = {int(r["pid"]): r for r in procinfo.list_processes(cmdline=True)[0] if isinstance(r.get("pid"), int)}
    for d in targets:
        ctx.check()
        dinfo = rows.get(d) if rows is not None else procinfo.process_info(d)
        if dinfo is None:        # exited meanwhile
            continue
        try:
            ctx.policy.check_kill(d, dinfo, protected=prot)
        except HiveError as e:
            details = dict(e.details or {})
            details.update(descendant=d)
            raise HiveError(e.code, f"process {pid} has a descendant that may not be stopped: {e.message}", details,
                            hint="use tree:false, or job_stop/service_control") from None


@handler("proc.kill")
def proc_kill(ctx: ActionContext, p: dict) -> dict:
    """Stop a process (and by default its descendants) after the kill guard (§5.4)."""
    pid = int(p["pid"])
    info = procinfo.process_info(pid)
    if info is None:
        raise not_found(f"process {pid} not found (already exited?)", hint="list processes with proc_list")
    prot = protected_pids(ctx)
    extra = ctx.policy.check_kill(pid, info, protected=prot)
    if isinstance(extra, dict):
        ctx.audit_extra.update(extra)
    ctx.audit_extra.update(pid=pid, name=info.get("name"))
    grace = float(p.get("grace", 5))
    grace = max(0.0, min(grace, ctx.remaining() - 5.0))
    force = bool(p.get("force"))
    ctime = info.get("create_time")
    if p.get("tree", True):
        _check_tree(ctx, pid, prot)
        res = proctree.kill_tree(pid, grace=grace, force=force, create_time=ctime)
    else:
        res = _kill_one(pid, ctime, grace=grace, force=force)
    out = {"killed": [int(x) for x in res.get("killed") or []],
           "still_alive": [int(x) for x in res.get("still_alive") or []], "method": str(res.get("method") or "")}
    ctx.audit_extra.update(killed=len(out["killed"]), still_alive=len(out["still_alive"]))
    return out


__all__ = ["system_info", "proc_list", "proc_kill", "protected_pids"]
