"""Small stdlib helpers shared across modules (design §4.2, Phase 0).

util imports only the stdlib and ``hive.errors`` at import time; Work-Package modules (e.g. ``hive.winapi``)
are reached through function-level imports only (§4.2 ``boot_key``).
"""
from __future__ import annotations

import base64
import calendar
import datetime as _dt
import fnmatch
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import platform
import re
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .errors import HiveError, bad_request, internal

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"

log = logging.getLogger("hive.util")


# ---------------------------------------------------------------------------------------------------------------
# tokens, ids, hashes

def new_token(nbytes: int = 32, prefix: str = "hv_") -> str:
    """URL-safe random Bearer token, ``hv_`` + token_urlsafe(nbytes)."""
    return f"{prefix}{secrets.token_urlsafe(nbytes)}"


def new_id(prefix: str = "") -> str:
    """Short sortable-ish unique id: <prefix><unix-ms hex><random hex>."""
    return f"{prefix}{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def token_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Constant-time comparison; False if either side is empty."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def hash_token(token: str) -> str:
    """``"sha256:" + hexdigest`` of the token (stored instead of plaintext tokens)."""
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def match_token_hash(token: str, stored: Sequence[str]) -> int:
    """Index of the stored hash matching ``token`` or -1; compares EVERY entry with hmac.compare_digest."""
    h = hash_token(token).encode("utf-8")
    found = -1
    for i, s in enumerate(stored):
        cand = (s or "").encode("utf-8")
        if hmac.compare_digest(h, cand) and found < 0:
            found = i
    return found


def sha256_bytes(data: bytes) -> str:
    """Hex sha256 of bytes."""
    return hashlib.sha256(data).hexdigest()


def sha256_text(s: str) -> str:
    """Hex sha256 of the UTF-8 encoding of ``s``."""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# time

def now() -> float:
    """Wall clock seconds since the epoch."""
    return time.time()


def monotonic() -> float:
    """Monotonic seconds (internal deadlines)."""
    return time.monotonic()


def iso(ts: Optional[float] = None) -> str:
    """Local time without zone, ONLY for local display (render.py, cli.py banner); wire uses iso_utc()."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def iso_utc(ts: Optional[float] = None) -> str:
    """ISO 8601 UTC with milliseconds, e.g. ``2026-11-17T09:12:03.120Z`` (all wire, audit and state timestamps)."""
    t = time.time() if ts is None else float(ts)
    ms_total = int(round(t * 1000))
    secs, ms = divmod(ms_total, 1000)
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(secs)) + f".{ms:03d}Z"


_ISO_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,9}))?)?)?\s*(Z|z|[+-]\d{2}:?\d{2})?$")


def parse_iso(s: str) -> float:
    """Parse an ISO 8601 timestamp (``…Z``, offset, or naive = UTC; date-only = midnight UTC) to epoch seconds."""
    if not isinstance(s, str):
        raise bad_request(f"invalid timestamp: {s!r}")
    m = _ISO_RE.match(s.strip())
    if not m:
        raise bad_request(f"invalid timestamp: {s!r}", hint="use ISO 8601, e.g. 2026-11-17T09:12:03.120Z")
    y, mo, d, hh, mi, ss, frac, tz = m.groups()
    # calendar.timegm silently normalizes out-of-range fields ("25:61:61" -> the next day): range-check them
    if int(hh or 0) > 23 or int(mi or 0) > 59 or int(ss or 0) > 60:
        raise bad_request(f"invalid timestamp: {s!r} (time out of range)")
    if tz and tz not in ("Z", "z"):
        tzd = tz[1:].replace(":", "")
        if int(tzd[2:] or 0) > 59 or int(tzd[:2]) * 60 + int(tzd[2:] or 0) > 14 * 60:
            raise bad_request(f"invalid timestamp: {s!r} (UTC offset out of range)")
    try:
        base = calendar.timegm((int(y), int(mo), int(d), int(hh or 0), int(mi or 0), int(ss or 0), 0, 0, 0))
        _dt.date(int(y), int(mo), int(d))  # validates the date
    except (ValueError, OverflowError) as e:
        raise bad_request(f"invalid timestamp: {s!r} ({e})")
    t = float(base)
    if frac:
        t += float("0." + frac)
    if tz and tz not in ("Z", "z"):
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = int(digits[:2]) * 3600 + int(digits[2:]) * 60
        t -= sign * offset
    return t


# ---------------------------------------------------------------------------------------------------------------
# JSON and files

def json_dumps(obj: Any, pretty: bool = False) -> str:
    """JSON text (UTF-8, compact unless pretty)."""
    return json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None, default=str,
                      separators=None if pretty else (",", ":"))


def canonical_json(obj: Any) -> str:
    """Canonical JSON: sort_keys, separators(",",":"), ensure_ascii=False (hashes, approvals, catalog_hash)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read_json(path: "os.PathLike[str] | str", default: Any = None) -> Any:
    """Read a JSON file; missing -> ``default``; decode error -> HiveError bad_request "invalid JSON in <path>: …".

    ``utf-8-sig`` accepts plain UTF-8 and a leading UTF-8 BOM (Notepad "UTF-8 with BOM", PowerShell 5 ``Out-File``)."""
    p = Path(path)
    if not p.exists():
        return default
    try:
        with open(p, "r", encoding="utf-8-sig") as f:
            return json.load(f)
    except (ValueError, UnicodeDecodeError) as e:
        raise bad_request(f"invalid JSON in {p}: {e}")


def replace_with_retry(src: "os.PathLike[str] | str", dst: "os.PathLike[str] | str", attempts: int = 5) -> None:
    """os.replace with a Windows PermissionError backoff of 0.1 -> 1.6 s (AV scanners, open handles)."""
    delay = 0.1
    for i in range(max(1, attempts)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if not IS_WINDOWS or i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 1.6)


def atomic_write_bytes(path: "os.PathLike[str] | str", data: bytes, private: bool = False) -> None:
    """Write bytes atomically (tmp file in the same dir + fsync + replace); ``private`` -> secure_file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if private:
            secure_file(tmp)
        replace_with_retry(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path: "os.PathLike[str] | str", obj: Any, private: bool = False) -> None:
    """Write JSON atomically (tmp file + fsync + replace). ``private`` -> 0600 on POSIX, owner-only ACL on Windows."""
    atomic_write_bytes(path, (json_dumps(obj, pretty=True) + "\n").encode("utf-8"), private=private)


def b64e(data: bytes) -> str:
    """Standard base64 text."""
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    """Decode standard base64 text."""
    return base64.b64decode(text.encode("ascii"), validate=False)


# ---------------------------------------------------------------------------------------------------------------
# truncation and sizes

def truncate_bytes(data: bytes, limit: int, head_ratio: float = 0.5) -> Tuple[bytes, bool]:
    """Keep the head and tail of ``data`` within ``limit`` bytes (plus a marker). Returns (data, truncated)."""
    if limit <= 0 or len(data) <= limit:
        return data, False
    marker = b"\n...[truncated %d bytes]...\n" % (len(data) - limit)
    head = int(limit * max(0.0, min(1.0, head_ratio)))
    tail = limit - head
    return data[:head] + marker + (data[-tail:] if tail > 0 else b""), True


def truncate_text(s: str, max_chars: int, head_ratio: float = 0.25) -> Tuple[str, int]:
    """Keep head and tail of ``s`` within about ``max_chars``, cutting at line boundaries; returns (text, omitted chars)."""
    if max_chars <= 0 or len(s) <= max_chars:
        return s, 0
    reserve = min(48, max_chars // 4)
    budget = max(1, max_chars - reserve)
    head_budget = int(budget * max(0.0, min(1.0, head_ratio)))
    tail_budget = budget - head_budget
    head = s[:head_budget]
    nl = head.rfind("\n")
    if nl >= 0:
        head = head[:nl + 1]
    tail = s[len(s) - tail_budget:] if tail_budget > 0 else ""
    nl = tail.find("\n")
    if 0 <= nl < len(tail) - 1:
        tail = tail[nl + 1:]
    omitted = len(s) - len(head) - len(tail)
    sep = "" if head.endswith("\n") or not head else "\n"
    return f"{head}{sep}…[{omitted} chars omitted]…\n{tail}", omitted


_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB", "EiB")


def human_bytes(n: int) -> str:
    """Humanized binary size, e.g. ``"23.9 GiB"``, ``"512 B"``."""
    try:
        v = float(n)
    except (TypeError, ValueError):
        return str(n)
    sign = "-" if v < 0 else ""
    v = abs(v)
    i = 0
    while v >= 1024 and i < len(_UNITS) - 1:
        v /= 1024.0
        i += 1
    if i == 0:
        return f"{sign}{int(v)} B"
    return f"{sign}{v:.1f} {_UNITS[i]}"


_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtpe]?)(i?)(b?)\s*$", re.IGNORECASE)


def parse_size(s: Union[str, int]) -> int:
    """``"64MiB"`` -> 67108864. Bare/``i`` suffixes (``k``, ``64m``, ``KiB``) are binary; ``KB``/``MB``/``GB`` decimal."""
    if isinstance(s, bool):
        raise bad_request(f"invalid size: {s!r}")
    if isinstance(s, int):
        if s < 0:
            raise bad_request(f"invalid size: {s!r}")
        return s
    if isinstance(s, float):
        if s < 0 or s != s:
            raise bad_request(f"invalid size: {s!r}")
        return int(s)
    m = _SIZE_RE.match(str(s))
    if not m:
        raise bad_request(f"invalid size: {s!r}", hint="e.g. 512, 64MiB, 16g, 2GB")
    num, unit, i, b = m.groups()
    unit = unit.lower()
    if not unit:
        if i:
            raise bad_request(f"invalid size: {s!r}")
        return int(float(num))
    power = "kmgtpe".index(unit) + 1
    base = 1000 if (b and not i) else 1024
    return int(float(num) * (base ** power))


# ---------------------------------------------------------------------------------------------------------------
# names, globs, hosts

def glob_match(name: str, patterns: Iterable[str]) -> bool:
    """True when ``name`` matches any pattern (fnmatchcase; ``"*"`` matches all)."""
    for p in patterns or ():
        if p == "*" or fnmatch.fnmatchcase(name, p):
            return True
    return False


def valid_name(s: str, maxlen: int = 64) -> bool:
    """``^[a-z0-9][a-z0-9_.-]{0,maxlen-1}$``: lowercase only (§4.2; names are canonical lowercase, so callers that
    accept user input lower-case it first; the case-insensitive *comparison* happens elsewhere)."""
    if not isinstance(s, str) or maxlen < 1:
        return False
    return re.match(r"^[a-z0-9][a-z0-9_.-]{0,%d}\Z" % (maxlen - 1), s) is not None


def _strip_host(host: str) -> str:
    h = (host or "").strip()
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    h = h.split("%", 1)[0] if ":" in h else h  # drop an IPv6 zone id
    return h.rstrip(".").lower()


def _parse_ip(host: str) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    try:
        return ipaddress.ip_address(_strip_host(host))
    except ValueError:
        return None


def is_loopback_host(host: str) -> bool:
    """``localhost``, 127/8 or ::1 (IPv4-mapped forms unwrapped)."""
    h = _strip_host(host)
    if h == "localhost":
        return True
    if _parse_ip(h) is None:
        return False
    return ip_category(h) == "loopback"


_V4 = ipaddress.IPv4Network
_V6 = ipaddress.IPv6Network
_METADATA = {ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("100.100.100.200"),
             ipaddress.ip_address("fd00:ec2::254")}
_NAT64 = _V6("64:ff9b::/96")
_CATS_V4: Tuple[Tuple[str, Tuple[ipaddress.IPv4Network, ...]], ...] = (
    ("loopback", (_V4("127.0.0.0/8"),)),
    ("linklocal", (_V4("169.254.0.0/16"),)),
    ("fakeip", (_V4("198.18.0.0/15"),)),
    ("private", (_V4("10.0.0.0/8"), _V4("172.16.0.0/12"), _V4("192.168.0.0/16"), _V4("100.64.0.0/10"))),
    ("reserved", (_V4("0.0.0.0/8"), _V4("224.0.0.0/4"), _V4("240.0.0.0/4"), _V4("192.0.2.0/24"),
                  _V4("198.51.100.0/24"), _V4("203.0.113.0/24"))),
)
_CATS_V6: Tuple[Tuple[str, Tuple[ipaddress.IPv6Network, ...]], ...] = (
    ("loopback", (_V6("::1/128"),)),
    ("linklocal", (_V6("fe80::/10"),)),
    ("private", (_V6("fc00::/7"),)),
    ("reserved", (_V6("::/128"), _V6("ff00::/8"))),
)


def _unwrap_v6(a: ipaddress.IPv6Address) -> Union[ipaddress.IPv4Address, ipaddress.IPv6Address]:
    if a.ipv4_mapped is not None:
        return a.ipv4_mapped
    if a in _NAT64:
        return ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
    if a.sixtofour is not None:
        return a.sixtofour
    if a.teredo is not None:
        return a.teredo[1]  # client IPv4 (stdlib already applies the XOR 0xffffffff)
    return a


def ip_category(ip: str) -> str:
    """Classify an IP literal: metadata|loopback|linklocal|fakeip|private|reserved|public (§4.2; wrappers unwrapped)."""
    a = _parse_ip(ip) if isinstance(ip, str) else None
    if a is None:
        raise bad_request(f"not an IP address: {ip!r}")
    if a in _METADATA:
        return "metadata"
    if isinstance(a, ipaddress.IPv6Address):
        a = _unwrap_v6(a)
        if a in _METADATA:
            return "metadata"
    table = _CATS_V4 if isinstance(a, ipaddress.IPv4Address) else _CATS_V6
    for cat, nets in table:
        for net in nets:
            if a in net:
                return cat
    return "public"


_LOCAL_SUFFIXES = (".local", ".lan", ".internal", ".home.arpa", ".localdomain")


def is_local_name(host: str) -> bool:
    """``localhost``, a name without a dot, or a .local/.lan/.internal/.home.arpa/.localdomain name (never proxied).

    IP literals are not names (False); ``.ts.net`` is NOT local (v6).
    """
    h = _strip_host(host)
    if not h or _parse_ip(h) is not None:
        return False
    if h == "localhost" or "." not in h:
        return True
    return h.endswith(_LOCAL_SUFFIXES)


_RESERVED_DEVICE = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}


def safe_remote_name(name: str, *, max_total: int = 1024) -> Optional[str]:
    """Validate a server-supplied relative "/"-separated file name; the normalized name or None (§4.2)."""
    if not isinstance(name, str) or not name or len(name) > max_total:
        return None
    if name.startswith("/") or "\\" in name or ":" in name:
        return None
    for ch in name:
        o = ord(ch)
        if o < 32 or o == 127 or 0x80 <= o <= 0x9F:
            return None
    parts = name.split("/")
    for seg in parts:
        if seg in ("", ".", ".."):
            return None
        if len(seg) > 255:
            return None
        if seg[-1] in (".", " "):
            return None
        if seg.split(".", 1)[0].strip().lower() in _RESERVED_DEVICE:
            return None
    return "/".join(parts)


# ---------------------------------------------------------------------------------------------------------------
# permissions

def _current_user_sid() -> Optional[str]:
    """Current user's SID string on Windows (ctypes, fallback ``whoami /user /fo csv /nh``); None elsewhere."""
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
            raise OSError("OpenProcessToken")
        try:
            size = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
            buf = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(token, 1, buf, size, ctypes.byref(size)):
                raise OSError("GetTokenInformation")
            psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]
            sid_str = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(ctypes.c_void_p(psid), ctypes.byref(sid_str)):
                raise OSError("ConvertSidToStringSidW")
            try:
                return sid_str.value
            finally:
                kernel32.LocalFree(sid_str)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # pragma: no cover
        pass
    try:  # pragma: no cover
        out = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"], capture_output=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.decode("utf-8", "replace")
        fields = [f.strip().strip('"') for f in out.strip().split(",")]
        if fields and fields[-1].startswith("S-1-"):
            return fields[-1]
    except Exception:
        pass
    return None


def _icacls_argv(path: "os.PathLike[str] | str", sid: str, is_dir: bool) -> List[str]:
    """argv for an owner-only ACL: current user SID + SYSTEM (S-1-5-18), inheritance removed."""
    perm = "(OI)(CI)F" if is_dir else "F"
    return ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:{perm}", "/grant:r", f"*S-1-5-18:{perm}"]


def _run_icacls(argv: List[str]) -> bool:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.returncode == 0
    except Exception:
        return False


def _secure(path: "os.PathLike[str] | str", *, is_dir: bool, required: bool) -> bool:
    ok = False
    why = ""
    try:
        if IS_WINDOWS:
            sid = _current_user_sid()
            if not sid:
                why = "cannot determine the current user's SID"
            else:
                ok = _run_icacls(_icacls_argv(path, sid, is_dir))
                why = "" if ok else "icacls failed"
        else:
            os.chmod(path, 0o700 if is_dir else 0o600)
            ok = True
    except OSError as e:
        why = str(e)
    if not ok:
        if required:
            raise internal(f"cannot restrict permissions of {path}: {why}",
                           hint="check the folder's owner and permissions (hive doctor)")
        log.warning("cannot restrict permissions of %s: %s", path, why)
    return ok


def secure_mkdir(path: "os.PathLike[str] | str", *, required: bool = False) -> bool:
    """mkdir -p; chmod 700 on POSIX; owner+SYSTEM ACL ((OI)(CI)) on Windows. Returns success."""
    try:
        Path(path).mkdir(parents=True, exist_ok=True)
    except OSError as e:
        if required:
            raise internal(f"cannot create {path}: {e}")
        log.warning("cannot create %s: %s", path, e)
        return False
    return _secure(path, is_dir=True, required=required)


def secure_file(path: "os.PathLike[str] | str", *, required: bool = False) -> bool:
    """chmod 600 on POSIX; owner+SYSTEM ACL via icacls on Windows. ``required`` failures raise internal."""
    return _secure(path, is_dir=False, required=required)


# ---------------------------------------------------------------------------------------------------------------
# processes

def _ticks_per_second() -> float:
    try:
        return float(os.sysconf("SC_CLK_TCK"))
    except (AttributeError, ValueError, OSError):
        return 100.0


def _linux_btime() -> Optional[float]:
    try:
        with open("/proc/stat", "r", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except OSError:
        pass
    return None


def _linux_stat_fields(pid: int) -> Optional[List[str]]:
    try:
        with open(f"/proc/{int(pid)}/stat", "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return None
    r = raw.rfind(")")
    if r < 0:
        return None
    return raw[r + 2:].split()  # fields from #3 (state) on


def process_create_time(pid: int) -> Optional[float]:
    """Process creation time (epoch seconds) or None when gone/unreadable (psutil | /proc | ps | GetProcessTimes)."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid < 0:
        return None
    try:
        import psutil  # type: ignore  # optional extra (hive-agent[full])
        try:
            return float(psutil.Process(pid).create_time())
        except Exception:
            return None
    except ImportError:
        pass
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                ft = (c.dwHighDateTime << 32) | c.dwLowDateTime
                return ft / 1e7 - 11644473600.0
            finally:
                k32.CloseHandle(h)
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        fields = _linux_stat_fields(pid)
        btime = _linux_btime()
        if not fields or btime is None or len(fields) < 20:
            return None
        try:
            return btime + float(fields[19]) / _ticks_per_second()  # field 22 = starttime
        except ValueError:
            return None
    try:  # macOS and other POSIX
        env = dict(os.environ, LC_ALL="C", TZ="UTC0")
        r = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, timeout=5, env=env)
        s = r.stdout.decode("ascii", "replace").strip()
        if r.returncode != 0 or not s:
            return None
        return float(calendar.timegm(time.strptime(s.strip(), "%a %b %d %H:%M:%S %Y")))
    except Exception:
        return None


def _posix_is_zombie(pid: int) -> bool:
    """Non-Linux POSIX (macOS, BSD): psutil status when installed, else ``ps -o stat= -p PID`` (LC_ALL=C); a state
    starting with ``Z`` is a zombie. Unreadable -> False (alive is the safe answer, as for an unreadable ctime)."""
    try:
        import psutil  # type: ignore  # optional extra (hive-agent[full])
        try:
            return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
        except Exception:
            return False
    except ImportError:
        pass
    try:
        env = dict(os.environ, LC_ALL="C")
        r = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, timeout=5, env=env)
        return r.returncode == 0 and r.stdout.decode("ascii", "replace").strip().startswith("Z")
    except Exception:
        return False


def pid_alive(pid: int, create_time: Optional[float] = None, tol: float = 2.0) -> bool:
    """Process exists (zombies count as dead: Linux /proc state, macOS/BSD psutil or ``ps -o stat=``; Windows
    GetExitCodeProcess), and when ``create_time`` is given its creation time is within ``tol``."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            h = k32.OpenProcess(0x1000, False, pid)
            if not h:
                return False
            try:
                code = wintypes.DWORD()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
                    return False
            finally:
                k32.CloseHandle(h)
        except Exception:
            return False
    else:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass
        except OSError:
            return False
        if sys.platform.startswith("linux"):
            fields = _linux_stat_fields(pid)
            if fields and fields[0] in ("Z", "X"):
                return False
        elif _posix_is_zombie(pid):   # macOS/BSD: kill(pid, 0) succeeds for an unreaped zombie
            return False
    if create_time is not None:
        ct = process_create_time(pid)
        if ct is not None and abs(ct - float(create_time)) > tol:
            return False
    return True


# ---------------------------------------------------------------------------------------------------------------
# self invocation (D56)

def _package_dir() -> Path:
    import hive
    return Path(hive.__file__).resolve().parent


def _zipapp_path() -> Optional[Path]:
    """Path of the running ``hive.pyz`` when the package is imported from a zipapp, else None."""
    import hive
    p = Path(hive.__file__).parent.parent
    try:
        if p.is_file():
            return p
    except OSError:
        pass
    return None


def console_python() -> str:
    """Windows: pythonw.exe -> sibling python.exe (when present); else sys.executable."""
    exe = sys.executable
    if IS_WINDOWS and os.path.basename(exe).lower() == "pythonw.exe":
        cand = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(cand):
            return cand
    return exe


def _windowless_python(exe: str) -> str:
    if IS_WINDOWS and os.path.basename(exe).lower() == "python.exe":
        cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(cand):
            return cand
    return exe


def self_command() -> List[str]:
    """``[python, "-m", "hive"]`` (``-P`` on Python >= 3.11) or ``[python, "/path/hive.pyz"]`` from a zipapp."""
    pyz = _zipapp_path()
    if pyz is not None:
        return [sys.executable, str(pyz)]
    if sys.version_info >= (3, 11):
        return [sys.executable, "-P", "-m", "hive"]
    return [sys.executable, "-m", "hive"]


def _norm_dir(p: str) -> str:
    try:
        r = os.path.realpath(p or os.getcwd())
    except OSError:
        r = os.path.abspath(p or ".")
    return os.path.normcase(r)


def prune_cwd_path() -> None:
    """Drop ``""`` and the cwd from sys.path unless the imported hive package lives directly in the cwd (D56)."""
    try:
        cwd = _norm_dir(os.getcwd())
    except OSError:
        return
    try:
        pkg_parent = _norm_dir(str(_package_dir().parent))
    except Exception:
        pkg_parent = None
    if pkg_parent == cwd:
        return
    keep = []
    for entry in sys.path:
        if entry == "" or entry == ".":
            continue
        try:
            if _norm_dir(entry) == cwd:
                continue
        except Exception:
            pass
        keep.append(entry)
    sys.path[:] = keep


def _site_dirs() -> List[str]:
    dirs: List[str] = []
    try:
        import site
        dirs.extend(site.getsitepackages())
        dirs.append(site.getusersitepackages())
    except Exception:
        pass
    return [_norm_dir(d) for d in dirs if d]


def self_env() -> Dict[str, str]:
    """``{"PYTHONPATH": <checkout>[+existing]}`` when hive runs from a source checkout (not pyz/site), else {}."""
    if _zipapp_path() is not None:
        return {}
    parent = str(_package_dir().parent)
    np = _norm_dir(parent)
    for d in _site_dirs():
        if np == d or np.startswith(d.rstrip(os.sep) + os.sep):
            return {}
    existing = os.environ.get("PYTHONPATH", "")
    return {"PYTHONPATH": parent + (os.pathsep + existing if existing else "")}


def launcher_argv(kind: str, config_path: Path, *, windowless: bool = False, supervise: bool = False) -> List[str]:
    """self_command() (pythonw when windowless) + [kind, "run", "--config", <abs config>] [+ "--supervise"]."""
    argv = self_command()
    if windowless:
        argv[0] = _windowless_python(argv[0])
    argv += [kind, "run", "--config", str(Path(config_path).resolve())]
    if supervise:
        argv.append("--supervise")
    return argv


# ---------------------------------------------------------------------------------------------------------------
# boot detection (D52)

def _tick_count_s() -> float:
    """Seconds since boot: GetTickCount64 on Windows, /proc/uptime on Linux, else monotonic()."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            import ctypes
            f = ctypes.windll.kernel32.GetTickCount64
            f.restype = ctypes.c_ulonglong
            return f() / 1000.0
        except Exception:
            pass
    try:
        with open("/proc/uptime", "r", encoding="ascii") as fh:
            return float(fh.read().split()[0])
    except (OSError, ValueError, IndexError):
        return time.monotonic()


def _tick_key() -> str:
    return "t:" + f"{time.time() - _tick_count_s():.1f}"


def boot_key() -> str:
    """Identifies this boot (Linux boot_id:pid1 ctime, macOS kern.boottime, Windows "w:" winlogon ctime, else "t:")."""
    if IS_WINDOWS:
        try:
            from . import winapi  # function-level: util never imports a WP module at import time
            ct = winapi.session_winlogon_ctime()
        except ImportError:
            ct = None
        except Exception:
            ct = None
        if ct is not None:
            return "w:" + str(int(ct))
        return _tick_key()
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/sys/kernel/random/boot_id", "r", encoding="ascii") as f:
                bid = f.read().strip()
            ct = process_create_time(1)
            if bid:
                return bid + ":" + (str(int(ct)) if ct is not None else "?")
        except OSError:
            pass
        return _tick_key()
    if IS_MAC:
        try:
            out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, timeout=5).stdout.decode()
            m = re.search(r"sec\s*=\s*(\d+)", out)
            if m:
                return m.group(1)
        except Exception:
            pass
    return _tick_key()


def same_boot(a: Optional[str], b: Optional[str]) -> bool:
    """Equal keys, or two "t:" keys within 30 s of each other; None/empty -> False."""
    if not a or not b:
        return False
    if a == b:
        return True
    if a.startswith("t:") and b.startswith("t:"):
        try:
            return abs(float(a[2:]) - float(b[2:])) < 30.0
        except ValueError:
            return False
    return False


# ---------------------------------------------------------------------------------------------------------------
# paths and display

def path_key(p: str, *, casefold: bool) -> str:
    """Normalized comparison key: "/" separators, no trailing "/", casefolded when ``casefold``."""
    k = str(p).replace("\\", "/")
    while len(k) > 1 and k.endswith("/"):
        k = k[:-1]
    return k.casefold() if casefold else k


def match_deny(patterns: Sequence[str], key: str) -> bool:
    """fnmatchcase against normalized patterns; ``P/**`` also matches P itself and everything below it."""
    for pat in patterns:
        if fnmatch.fnmatchcase(key, pat):
            return True
        if pat.endswith("/**"):
            base = pat[:-3]
            if key == base or key.startswith(base + "/") or fnmatch.fnmatchcase(key, base):
                return True
    return False


_BIDI = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x206A)) | set(range(0x200B, 0x2010)) | {0xFEFF}


def escape_terminal(s: str) -> str:
    """C0/C1 controls (except \\n, \\t) -> ``\\xNN``; bidi/zero-width marks -> ``\\u{XXXX}``. Idempotent."""
    out = []
    for ch in str(s):
        o = ord(ch)
        if ch in ("\n", "\t"):
            out.append(ch)
        elif o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
            out.append("\\x%02x" % o)
        elif o in _BIDI:
            out.append("\\u{%04X}" % o)
        else:
            out.append(ch)
    return "".join(out)


def kv_list(value: Any) -> List[str]:
    """dict or list -> ``["K=V", ...]`` (None -> [])."""
    if value is None:
        return []
    if isinstance(value, dict):
        return [f"{k}={v}" for k, v in value.items()]
    if isinstance(value, (list, tuple)):
        return [str(x) for x in value]
    return [str(value)]


def parse_kv(items: Iterable[str], sep: str = "=") -> Dict[str, str]:
    """``["K=V", ...]`` -> dict (split at the first ``sep``; ``":"`` values are stripped); bad item -> bad_request."""
    out: Dict[str, str] = {}
    for it in items or ():
        s = str(it)
        if sep not in s:
            raise bad_request(f"expected 'KEY{sep}VALUE', got {s!r}")
        k, v = s.split(sep, 1)
        k = k.strip()
        if not k:
            raise bad_request(f"empty key in {s!r}")
        out[k] = v.strip() if sep == ":" else v
    return out


# ---------------------------------------------------------------------------------------------------------------
# misc (existing)

def decode_text(data: bytes) -> str:
    """Decode process output / file bytes leniently (UTF-8, then local codepage)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WINDOWS:
        for enc in ("mbcs", "gbk"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
    return data.decode("utf-8", errors="replace")


def default_data_dir(name: str) -> Path:
    """``$HIVE_HOME/<name>`` or ``~/.hive/<name>``."""
    base = os.environ.get("HIVE_HOME")
    if base:
        return Path(base).expanduser() / name
    return Path.home() / ".hive" / name


def platform_summary() -> dict:
    """OS / machine / python summary."""
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }


def clamp(value: Any, lo: float, hi: float, default: float) -> float:
    """Float clamp with a default for invalid/NaN input (display helpers only; never for policy limits)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


# ---------------------------------------------------------------------------------------------------------------
# single-instance lock

class FileLock:
    """Single-instance lock: holder JSON ``{"pid","ctime","note"}`` at offset 0; POSIX flock, Windows byte 4096."""

    _WIN_OFFSET = 4096

    def __init__(self, path: "os.PathLike[str] | str"):
        self.path = Path(path)
        self._fd: Optional[int] = None

    def acquire(self, note: str = "") -> bool:
        """Non-blocking acquire; True when this object holds the lock afterwards."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if IS_WINDOWS:  # pragma: no cover - Windows only
                import msvcrt
                os.lseek(fd, self._WIN_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        try:
            pid = os.getpid()
            data = json.dumps({"pid": pid, "ctime": process_create_time(pid), "note": str(note)}).encode("utf-8")
            os.lseek(fd, 0, os.SEEK_SET)
            if not IS_WINDOWS:
                os.ftruncate(fd, 0)
            os.write(fd, data + b"\n" if IS_WINDOWS else data)
            if IS_WINDOWS:  # pad the JSON area so stale bytes of a longer note cannot follow it
                os.write(fd, b" " * max(0, 1024 - len(data) - 1))
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd
        return True

    def release(self) -> None:
        """Release the lock (clears the holder JSON first)."""
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            if IS_WINDOWS:  # pragma: no cover
                os.write(fd, b" " * 1024)
            else:
                os.ftruncate(fd, 0)
        except OSError:
            pass
        try:
            if IS_WINDOWS:  # pragma: no cover
                import msvcrt
                os.lseek(fd, self._WIN_OFFSET, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(fd)

    def holder(self) -> Optional[dict]:
        """``{"pid","ctime","note"}`` when the recorded pid is alive (ctime-checked), else None. Reads without locking."""
        try:
            with open(self.path, "rb") as f:
                raw = f.read(4096)
        except OSError:
            return None
        text = raw.decode("utf-8", "replace").strip().strip("\x00").strip()
        if not text:
            return None
        try:
            d = json.loads(text.split("\n", 1)[0])
        except ValueError:
            return None
        if not isinstance(d, dict) or not isinstance(d.get("pid"), int):
            return None
        ct = d.get("ctime")
        if not pid_alive(d["pid"], ct if isinstance(ct, (int, float)) else None):
            return None
        return {"pid": d["pid"], "ctime": ct, "note": d.get("note", "")}

    def holder_pid(self) -> Optional[int]:
        """``holder()["pid"]`` or None."""
        h = self.holder()
        return h["pid"] if h else None

    def __enter__(self) -> "FileLock":
        if not self.acquire():
            raise HiveError("conflict", f"lock {self.path} is held by another process")
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()


# ---------------------------------------------------------------------------------------------------------------
# streams

class ClosingIterator:
    """Iterator over ``source`` whose ``close()`` closes the source and runs ``on_close`` exactly once, also when
    iteration never started (WP5b review: ``close()`` on a generator that never ran skips its ``finally``, so a
    client that disconnects before the first chunk would leak sockets, inflight slots and audit records).
    Exhaustion or an error raised by the source also closes it."""

    def __init__(self, source: Iterable[bytes], on_close: Optional[Any] = None):
        import threading
        self._source = source
        self._it = iter(source)
        self._on_close = on_close
        self._lock = threading.Lock()
        self._closed = False

    def __iter__(self) -> "ClosingIterator":
        return self

    def __next__(self) -> Any:
        if self._closed:
            raise StopIteration
        try:
            return next(self._it)
        except BaseException:
            self.close()
            raise

    @property
    def closed(self) -> bool:
        """True once ``close()`` ran."""
        return self._closed

    def close(self) -> None:
        """Close the source (errors ignored), then run ``on_close`` once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        try:
            close = getattr(self._source, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001 - best effort
                    pass
        finally:
            if self._on_close is not None:
                self._on_close()
