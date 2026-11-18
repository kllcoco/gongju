"""Process and listener listing: list_processes(), process_info(), listeners() (design §12.3, WP4).

Sources, in order: psutil (optional import) | Linux ``/proc`` | macOS ``ps``/``lsof`` | Windows Toolhelp32 + CIM /
``netstat``. Every external program runs through ``proctree.run_quick`` resolved on ``proctree.trusted_path()`` (or a
fixed system location), never through a shell. The text parsers are module functions so tests can feed fixtures.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import util

log = logging.getLogger("hive.procinfo")

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

_PROC = "/proc"            # test seam: a fixture tree with the same layout
CPU_SAMPLE_S = 0.2         # §12.3: two /proc samples over 200 ms
_user_cache: Dict[int, Optional[str]] = {}


# ---------------------------------------------------------------------------------------------------------------
# helpers

def _psutil() -> Any:
    """The psutil module when importable, else None (optional dependency, §12.1)."""
    try:
        import psutil  # type: ignore
        return psutil
    except Exception:  # noqa: BLE001 - ImportError or a broken install
        return None


def _tool(name: str, fixed: Iterable[str] = ()) -> Optional[str]:
    """Resolve an internal probe program on the trusted PATH, then fixed system locations (§9.2 v3)."""
    from . import proctree
    p = proctree.which(name, proctree.trusted_path())
    if p:
        return p
    for f in fixed:
        if os.path.isfile(f):
            return f
    return None


def _run(argv: List[str], timeout: float, env: Optional[Dict[str, str]] = None) -> Optional[Tuple[int, bytes]]:
    """run_quick wrapper: (code, stdout) or None when the program is missing/failed/timed out."""
    from . import proctree
    try:
        code, out, _err = proctree.run_quick(argv, timeout=timeout, env=env, max_output=32 << 20)
    except Exception as e:  # noqa: BLE001 - probes never raise
        log.debug("probe %s failed: %s", argv[0] if argv else "?", e)
        return None
    return code, out


def _c_env() -> Dict[str, str]:
    """Environment for ``ps``: C locale and UTC so ``lstart`` parses (§20.6)."""
    env = dict(os.environ)
    env.update({"LC_ALL": "C", "LANG": "C", "TZ": "UTC0"})
    return env


def _username(uid: Optional[int]) -> Optional[str]:
    if uid is None or IS_WINDOWS:
        return None
    if uid in _user_cache:
        return _user_cache[uid]
    name: Optional[str]
    try:
        import pwd
        name = pwd.getpwuid(uid).pw_name
    except (KeyError, ImportError, OSError):
        name = str(uid)
    _user_cache[uid] = name
    return name


def _powershell() -> Optional[str]:  # pragma: no cover - Windows only
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return _tool("powershell", [os.path.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")])


def _ps_json(script: str, timeout: float) -> Any:  # pragma: no cover - Windows only
    """Run a PowerShell one-liner that prints JSON; parsed value or None."""
    ps = _powershell()
    if not ps:
        return None
    r = _run([ps, "-NoProfile", "-NonInteractive", "-Command",
              "[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false);" + script], timeout)
    if r is None or r[0] != 0:
        return None
    try:
        return json.loads(util.decode_text(r[1]) or "null")
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------------
# Linux /proc

def _read(path: str, binary: bool = False) -> Any:
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    return data if binary else data.decode("utf-8", "replace")


def parse_proc_stat(raw: str) -> Optional[dict]:
    """Parse ``/proc/<pid>/stat``: {comm, state, ppid, ticks (utime+stime), starttime, rss_pages}."""
    if not raw:
        return None
    left = raw.find("(")
    right = raw.rfind(")")
    if left < 0 or right < left:
        return None
    rest = raw[right + 2:].split()
    try:
        return {"comm": raw[left + 1:right], "state": rest[0], "ppid": int(rest[1]),
                "ticks": int(rest[11]) + int(rest[12]), "starttime": int(rest[19]), "rss_pages": int(rest[21])}
    except (IndexError, ValueError):
        return None


def _status_uid(raw: Optional[str]) -> Optional[int]:
    for line in (raw or "").splitlines():
        if line.startswith("Uid:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _cmdline(raw: Optional[bytes]) -> Optional[List[str]]:
    if not raw:
        return None
    parts = raw.split(b"\0")
    if parts and parts[-1] == b"":
        parts = parts[:-1]
    return [util.decode_text(p) for p in parts] or None


def _proc_name(comm: str, cmd: Optional[List[str]]) -> str:
    """comm is cut at 15 chars by the kernel; prefer the argv[0] basename that extends it (like psutil)."""
    if cmd and len(comm) >= 15:
        base = os.path.basename(cmd[0])
        if base.startswith(comm):
            return base
    return comm


def _btime() -> Optional[float]:
    raw = _read(os.path.join(_PROC, "stat"))
    for line in (raw or "").splitlines():
        if line.startswith("btime "):
            try:
                return float(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _clk_tck() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100.0


def _page_size() -> int:
    try:
        return int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, ValueError, OSError):
        return 4096


def _has_proc() -> bool:
    return os.path.isdir(os.path.join(_PROC, "self"))


def _proc_pids() -> List[int]:
    try:
        return sorted(int(n) for n in os.listdir(_PROC) if n.isdigit())
    except OSError:
        return []


def _proc_row(pid: int, *, cmdline: bool, btime: Optional[float], tck: float, page: int) -> Optional[dict]:
    st = parse_proc_stat(_read(os.path.join(_PROC, str(pid), "stat")))
    if st is None:
        return None
    cmd = _cmdline(_read(os.path.join(_PROC, str(pid), "cmdline"), binary=True))
    uid = _status_uid(_read(os.path.join(_PROC, str(pid), "status")))
    ctime = (btime + st["starttime"] / tck) if btime is not None else None
    return {"pid": pid, "ppid": st["ppid"], "name": _proc_name(st["comm"], cmd), "uid": uid, "user": _username(uid),
            "cpu_pct": None, "mem_bytes": st["rss_pages"] * page, "create_time": ctime,
            "cmdline": cmd if cmdline else None, "_ticks": st["ticks"], "_state": st["state"]}


def _linux_list(cmdline: bool, sample: float = CPU_SAMPLE_S) -> List[dict]:
    btime, tck, page = _btime(), _clk_tck(), _page_size()
    first: Dict[int, int] = {}
    t0 = time.monotonic()
    for pid in _proc_pids():
        st = parse_proc_stat(_read(os.path.join(_PROC, str(pid), "stat")))
        if st is not None:
            first[pid] = st["ticks"]
    if sample > 0:
        time.sleep(sample)
    elapsed = max(1e-3, time.monotonic() - t0)
    rows = []
    for pid in _proc_pids():
        r = _proc_row(pid, cmdline=cmdline, btime=btime, tck=tck, page=page)
        if r is None:
            continue
        r.pop("_state", None)
        ticks = r.pop("_ticks")
        if pid in first:
            r["cpu_pct"] = round(max(0, ticks - first[pid]) / tck / elapsed * 100.0, 1)
        rows.append(r)
    return rows


def _linux_info(pid: int) -> Optional[dict]:
    r = _proc_row(int(pid), cmdline=True, btime=_btime(), tck=_clk_tck(), page=_page_size())
    if r is None or r.get("_state") in ("Z", "X"):
        return None
    return {k: r[k] for k in ("pid", "ppid", "name", "cmdline", "uid", "user", "create_time")}


# ---------------------------------------------------------------------------------------------------------------
# macOS ps

_LSTART_RE = re.compile(r"^(\w{3})\s+(\w{3})\s+(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})$")


def parse_lstart(text: str) -> Optional[float]:
    """``ps -o lstart`` under ``LC_ALL=C TZ=UTC0`` ("Mon Nov 18 03:23:47 2026") -> epoch seconds (UTC)."""
    m = _LSTART_RE.match(" ".join((text or "").split()))
    if not m:
        return None
    try:
        tm = time.strptime(" ".join(m.groups()[1:3]) + " " + ":".join(m.groups()[3:6]) + " " + m.group(7),
                           "%b %d %H:%M:%S %Y")
    except ValueError:
        return None
    return float(calendar.timegm(tm))


def parse_ps_rows(text: str) -> List[dict]:
    """Rows of ``ps -axo pid=,ppid=,user=,rss=,%cpu=,lstart=,comm=`` (rss in KiB, lstart = 5 words, comm = rest)."""
    rows = []
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 11:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
            rss = int(parts[3]) * 1024
            cpu = float(parts[4].replace(",", "."))
        except ValueError:
            continue
        start = parse_lstart(" ".join(parts[5:10]))
        comm = line.split(None, 10)[10].strip() if len(line.split(None, 10)) > 10 else parts[10]
        rows.append({"pid": pid, "ppid": ppid, "name": os.path.basename(comm) or comm, "user": parts[2],
                     "uid": None, "cpu_pct": cpu, "mem_bytes": rss, "create_time": start, "cmdline": None})
    return rows


def parse_ps_args(text: str) -> Dict[int, str]:
    """``ps -axo pid=,args=`` -> {pid: args}."""
    out: Dict[int, str] = {}
    for line in (text or "").splitlines():
        parts = line.strip().split(None, 1)
        if parts and parts[0].isdigit():
            out[int(parts[0])] = parts[1] if len(parts) > 1 else ""
    return out


def _ps() -> Optional[str]:
    return _tool("ps", ["/bin/ps", "/usr/bin/ps"])


def _ps_list(cmdline: bool) -> List[dict]:
    ps = _ps()
    if not ps:
        return []
    r = _run([ps, "-axo", "pid=,ppid=,user=,rss=,%cpu=,lstart=,comm="], 10, env=_c_env())
    if r is None or r[0] != 0:
        return []
    rows = parse_ps_rows(util.decode_text(r[1]))
    if cmdline:
        a = _run([ps, "-axo", "pid=,args="], 10, env=_c_env())
        args = parse_ps_args(util.decode_text(a[1])) if a and a[0] == 0 else {}
        for row in rows:
            text = args.get(row["pid"])
            row["cmdline"] = text.split() if text else None   # ps joins argv with spaces; best effort
    return rows


def _ps_info(pid: int) -> Optional[dict]:
    ps = _ps()
    if not ps:
        return None
    r = _run([ps, "-o", "pid=,ppid=,user=,rss=,%cpu=,lstart=,comm=", "-p", str(int(pid))], 5, env=_c_env())
    if r is None or r[0] != 0:
        return None
    rows = parse_ps_rows(util.decode_text(r[1]))
    if not rows:
        return None
    row = rows[0]
    u = _run([ps, "-o", "uid=", "-p", str(int(pid))], 5, env=_c_env())
    uid: Optional[int] = None
    if u and u[0] == 0:
        try:
            uid = int(util.decode_text(u[1]).strip())
        except ValueError:
            uid = None
    a = _run([ps, "-o", "args=", "-p", str(int(pid))], 5, env=_c_env())
    text = util.decode_text(a[1]).strip() if a and a[0] == 0 else ""
    return {"pid": row["pid"], "ppid": row["ppid"], "name": row["name"], "cmdline": text.split() if text else None,
            "uid": uid, "user": row["user"], "create_time": row["create_time"]}


# ---------------------------------------------------------------------------------------------------------------
# Windows Toolhelp32 + CIM

def _cim_processes(pid: Optional[int] = None) -> Dict[int, dict]:  # pragma: no cover - Windows only
    flt = f" -Filter \"ProcessId={int(pid)}\"" if pid is not None else ""
    data = _ps_json(f"Get-CimInstance Win32_Process{flt} | Select ProcessId,CommandLine,WorkingSetSize | "
                    "ConvertTo-Json -Compress", 10)
    if isinstance(data, dict):
        data = [data]
    out: Dict[int, dict] = {}
    for d in data or []:
        if isinstance(d, dict) and isinstance(d.get("ProcessId"), int):
            out[d["ProcessId"]] = d
    return out


def split_windows_cmdline(text: str) -> List[str]:
    """CommandLineToArgvW-like split (quotes, backslashes before quotes)."""
    args: List[str] = []
    cur: List[str] = []
    in_q = False
    have = False
    i = 0
    s = text or ""
    while i < len(s):
        c = s[i]
        if c == "\\":
            j = i
            while j < len(s) and s[j] == "\\":
                j += 1
            n = j - i
            if j < len(s) and s[j] == '"':
                cur.append("\\" * (n // 2))
                if n % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j
                have = True
                continue
            cur.append("\\" * n)
            i = j
            have = True
            continue
        if c == '"':
            in_q = not in_q
            have = True
        elif c in " \t" and not in_q:
            if have:
                args.append("".join(cur))
                cur, have = [], False
        else:
            cur.append(c)
            have = True
        i += 1
    if have:
        args.append("".join(cur))
    return args


def _win_list(cmdline: bool) -> List[dict]:  # pragma: no cover - Windows only
    from . import winapi
    snap = winapi.process_list()
    cim = _cim_processes()   # also gives WorkingSetSize
    rows = []
    for pid, ppid, name in snap:
        c = cim.get(pid) or {}
        ws = c.get("WorkingSetSize")
        cl = c.get("CommandLine")
        rows.append({"pid": pid, "ppid": ppid, "name": name, "user": None, "uid": None, "cpu_pct": None,
                     "mem_bytes": int(ws) if isinstance(ws, (int, float)) else None,
                     "create_time": util.process_create_time(pid) if pid > 4 else None,
                     "cmdline": split_windows_cmdline(cl) if (cmdline and isinstance(cl, str)) else None})
    return rows


def _win_info(pid: int) -> Optional[dict]:  # pragma: no cover - Windows only
    from . import winapi
    row = next(((p, pp, n) for p, pp, n in winapi.process_list() if p == int(pid)), None)
    if row is None:
        return None
    c = _cim_processes(int(pid)).get(int(pid)) or {}
    cl = c.get("CommandLine")
    return {"pid": row[0], "ppid": row[1], "name": row[2],
            "cmdline": split_windows_cmdline(cl) if isinstance(cl, str) else None, "uid": None, "user": None,
            "create_time": util.process_create_time(int(pid))}


# ---------------------------------------------------------------------------------------------------------------
# psutil

def _psutil_list(ps: Any, cmdline: bool) -> List[dict]:
    procs = []
    for p in ps.process_iter():
        try:
            p.cpu_percent(None)
            procs.append(p)
        except Exception:  # noqa: BLE001 - gone / access denied
            continue
    time.sleep(CPU_SAMPLE_S)
    rows = []
    for p in procs:
        try:
            with p.oneshot():
                row = _psutil_row(p, cmdline)
                try:
                    row["cpu_pct"] = round(float(p.cpu_percent(None)), 1)
                except Exception:  # noqa: BLE001
                    row["cpu_pct"] = None
            rows.append(row)
        except Exception:  # noqa: BLE001
            continue
    return rows


def _safe(fn: Any, default: Any = None) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - AccessDenied, ZombieProcess, NoSuchProcess
        return default


def _psutil_row(p: Any, cmdline: bool) -> dict:
    uids = _safe(p.uids)
    mem = _safe(p.memory_info)
    cmd = _safe(p.cmdline) if cmdline else None
    return {"pid": int(p.pid), "ppid": _safe(p.ppid), "name": _safe(p.name, "") or "", "user": _safe(p.username),
            "uid": int(uids.real) if uids is not None else None, "cpu_pct": None,
            "mem_bytes": int(mem.rss) if mem is not None else None, "create_time": _safe(p.create_time),
            "cmdline": list(cmd) if cmd else None}


# ---------------------------------------------------------------------------------------------------------------
# public API

def list_processes(*, cmdline: bool = True) -> Tuple[List[dict], str]:
    """(rows, method): rows ``{pid, ppid, name, user, uid, cpu_pct, mem_bytes, create_time, cmdline}``;
    method ``psutil|proc|ps|toolhelp`` (``unavailable`` when nothing works)."""
    ps = _psutil()
    if ps is not None:
        try:
            return _psutil_list(ps, cmdline), "psutil"
        except Exception as e:  # noqa: BLE001
            log.debug("psutil listing failed: %s", e)
    if IS_WINDOWS:  # pragma: no cover - Windows only
        rows = _win_list(cmdline)
        return rows, ("toolhelp" if rows else "unavailable")
    if _has_proc():
        return _linux_list(cmdline), "proc"
    rows = _ps_list(cmdline)
    return rows, ("ps" if rows else "unavailable")


def process_info(pid: int) -> Optional[dict]:
    """``{"pid","ppid","name","cmdline","uid","user","create_time"}`` or None when the process is gone (§12.3)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid < 0:
        return None
    ps = _psutil()
    if ps is not None:
        try:
            p = ps.Process(pid)
            if _safe(p.status) == "zombie":
                return None
            row = _psutil_row(p, True)
            return {k: row[k] for k in ("pid", "ppid", "name", "cmdline", "uid", "user", "create_time")}
        except Exception:  # noqa: BLE001 - NoSuchProcess etc.
            if not util.pid_alive(pid):
                return None
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return _win_info(pid)
    if _has_proc():
        return _linux_info(pid)
    return _ps_info(pid)


# ---------------------------------------------------------------------------------------------------------------
# listeners

def _hex_ip(h: str) -> str:
    import ipaddress
    import struct
    b = bytes.fromhex(h)
    if len(b) == 4:
        return str(ipaddress.IPv4Address(struct.unpack("<I", b)[0]))
    words = struct.unpack("<4I", b)
    addr = ipaddress.IPv6Address(struct.pack(">4I", *words))
    if addr.ipv4_mapped is not None:
        return str(addr.ipv4_mapped)
    return str(addr)


def parse_proc_net_tcp(text: str) -> List[Tuple[str, int, int]]:
    """``/proc/net/tcp`` or ``tcp6`` -> [(address, port, inode)] for LISTEN sockets (state 0A)."""
    out = []
    for line in (text or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) < 10 or parts[3].upper() != "0A":
            continue
        try:
            ip_hex, port_hex = parts[1].split(":")
            out.append((_hex_ip(ip_hex), int(port_hex, 16), int(parts[9])))
        except (ValueError, IndexError):
            continue
    return out


def _socket_inodes() -> Dict[int, Tuple[int, str]]:
    """{socket inode: (pid, name)} from /proc/*/fd (own processes, or all as root)."""
    out: Dict[int, Tuple[int, str]] = {}
    for pid in _proc_pids():
        fd_dir = os.path.join(_PROC, str(pid), "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        name: Optional[str] = None
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target.startswith("socket:["):
                try:
                    ino = int(target[8:-1])
                except ValueError:
                    continue
                if name is None:
                    st = parse_proc_stat(_read(os.path.join(_PROC, str(pid), "stat")))
                    name = st["comm"] if st else ""
                out.setdefault(ino, (pid, name))
    return out


def _linux_listeners() -> List[dict]:
    socks = []
    for fn, proto in (("tcp", "tcp"), ("tcp6", "tcp6")):
        txt = _read(os.path.join(_PROC, "net", fn))
        for addr, port, ino in parse_proc_net_tcp(txt or ""):
            socks.append((proto, addr, port, ino))
    if not socks:
        return []
    inodes = _socket_inodes()
    rows = []
    for proto, addr, port, ino in socks:
        owner = inodes.get(ino)
        rows.append({"proto": proto, "address": addr, "port": port, "pid": owner[0] if owner else None,
                     "name": owner[1] if owner else None})
    return _dedupe(rows)


def _split_hostport(s: str) -> Optional[Tuple[str, int]]:
    s = s.strip()
    if s.startswith("["):
        r = s.find("]")
        if r < 0 or s[r + 1:r + 2] != ":":
            return None
        host, port = s[1:r], s[r + 2:]
    else:
        if ":" not in s:
            return None
        host, port = s.rsplit(":", 1)
    if host == "*":
        host = "0.0.0.0"
    host = host.split("%", 1)[0]
    try:
        return host, int(port)
    except ValueError:
        return None


def parse_lsof(text: str) -> List[dict]:
    """``lsof -nP -iTCP -sTCP:LISTEN -F pcn`` -> rows {proto, address, port, pid, name}."""
    rows = []
    pid: Optional[int] = None
    name: Optional[str] = None
    for line in (text or "").splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(val)
            except ValueError:
                pid = None
            name = None
        elif tag == "c":
            name = val
        elif tag == "n":
            hp = _split_hostport(val.split("->", 1)[0])
            if hp is None:
                continue
            proto = "tcp6" if ":" in hp[0] else "tcp"
            rows.append({"proto": proto, "address": hp[0], "port": hp[1], "pid": pid, "name": name})
    return _dedupe(rows)


def parse_netstat(text: str, names: Optional[Dict[int, str]] = None) -> List[dict]:
    """``netstat -ano -p TCP`` / ``-p TCPv6`` -> LISTEN rows. The state word is localized, so a listening socket is
    recognised by its foreign address ``0.0.0.0:0`` / ``[::]:0`` (or the English LISTENING)."""
    rows = []
    names = names or {}
    for line in (text or "").splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() not in ("TCP", "TCPV6"):
            continue
        local, foreign = parts[1], parts[2]
        listening = foreign in ("0.0.0.0:0", "[::]:0", "*:*") or (len(parts) >= 5 and parts[3].upper() == "LISTENING")
        if not listening:
            continue
        hp = _split_hostport(local)
        if hp is None:
            continue
        try:
            pid: Optional[int] = int(parts[-1])
        except ValueError:
            pid = None
        rows.append({"proto": "tcp6" if ":" in hp[0] else "tcp", "address": hp[0], "port": hp[1], "pid": pid,
                     "name": names.get(pid) if pid is not None else None})
    return _dedupe(rows)


def _dedupe(rows: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for r in rows:
        k = (r["proto"], r["address"], r["port"], r.get("pid"))
        if k not in seen:
            seen.add(k)
            out.append(r)
    out.sort(key=lambda r: (r["port"], r["address"]))
    return out


def _psutil_listeners(ps: Any) -> List[dict]:
    rows = []
    for c in ps.net_connections(kind="tcp"):
        if getattr(c, "status", "") != "LISTEN" or not c.laddr:
            continue
        name = None
        if c.pid:
            name = _safe(lambda: ps.Process(c.pid).name())
        addr = str(c.laddr.ip).split("%", 1)[0]
        rows.append({"proto": "tcp6" if ":" in addr else "tcp", "address": addr, "port": int(c.laddr.port),
                     "pid": c.pid, "name": name})
    return _dedupe(rows)


def listeners() -> Tuple[List[dict], str]:
    """(listening TCP sockets ``{proto, address, port, pid, name}``, method ``psutil|proc|lsof|netstat``)."""
    ps = _psutil()
    if ps is not None:
        try:
            return _psutil_listeners(ps), "psutil"
        except Exception as e:  # noqa: BLE001 - AccessDenied on macOS without root
            log.debug("psutil net_connections failed: %s", e)
    if IS_WINDOWS:  # pragma: no cover - Windows only
        from . import winapi
        names = {p: n for p, _pp, n in winapi.process_list()}
        root = os.environ.get("SystemRoot", r"C:\Windows")
        ns = _tool("netstat", [os.path.join(root, "System32", "NETSTAT.EXE")])
        if not ns:
            return [], "unavailable"
        rows: List[dict] = []
        for proto in ("TCP", "TCPv6"):
            r = _run([ns, "-ano", "-p", proto], 10)
            if r is not None:
                rows += parse_netstat(util.decode_text(r[1]), names)
        return _dedupe(rows), "netstat"
    if os.path.exists(os.path.join(_PROC, "net", "tcp")):
        return _linux_listeners(), "proc"
    lsof = _tool("lsof", ["/usr/sbin/lsof", "/usr/bin/lsof"])
    if not lsof:
        return [], "unavailable"
    r = _run([lsof, "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"], 10, env=_c_env())
    if r is None:
        return [], "unavailable"
    return parse_lsof(util.decode_text(r[1])), "lsof"


__all__ = ["list_processes", "process_info", "listeners", "parse_proc_stat", "parse_proc_net_tcp", "parse_lsof",
           "parse_netstat", "parse_lstart", "parse_ps_rows", "parse_ps_args", "split_windows_cmdline"]
