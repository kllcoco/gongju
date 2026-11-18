"""HTTP proxy settings, bypass matching, CONNECT tunnel, child proxy env (design §11.4, WP3).

Sources, in order: ``network.proxy`` (``"off"`` = none, a URL = config), env ``HTTPS_PROXY``/``HTTP_PROXY``/
``ALL_PROXY`` (http:// only), then the OS system proxy when ``network.use_system_proxy`` (Windows: HKCU Internet
Settings read with ``winreg``, v8; elsewhere ``urllib.request.getproxies()``). Only ``http://[user:pass@]host:port``
proxies are accepted; socks/https proxies and PAC are ignored with one WARNING line. ``wsl-host`` (v4) is replaced
by the WSL default gateway (``/proc/net/route``) at every load / 60 s refresh. Results are cached 60 s.

Proxy credentials are never logged, audited or returned: ``describe()`` and ``redact_proxy()`` hide them.
Imports only stdlib, ``hive.util`` and ``hive.errors`` (the ``_download`` child imports it).
"""
from __future__ import annotations

import base64
import fnmatch
import ipaddress
import json
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urlsplit, urlunsplit

from .errors import bad_request, unavailable
from .util import ip_category, is_local_name

log = logging.getLogger("hive.netproxy")

ALWAYS_NO_PROXY: Tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
# §10.1 step 3: NO_PROXY handed to children (they match by name and cannot resolve first)
CHILD_NO_PROXY: Tuple[str, ...] = ("localhost", "127.0.0.1", "::1", ".local", ".lan", ".ts.net", "10.0.0.0/8",
                                   "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
CACHE_TTL = 60.0
MAX_CONNECT_HEADER = 16384
WSL_HOST = "wsl-host"
ONLY_HTTP_WARNING = "only HTTP proxies are supported; use the client's HTTP/mixed port, e.g. 7890"
_ROUTE_FILE = "/proc/net/route"
_IS_WINDOWS = sys.platform == "win32"

_lock = threading.Lock()
_cache: Dict[str, Tuple[float, "ProxySettings"]] = {}
_warned: set = set()


def _now() -> float:
    return time.monotonic()


def _warn_once(msg: str) -> None:
    with _lock:
        if msg in _warned:
            return
        _warned.add(msg)
    log.warning("%s", msg)


def redact_proxy(url: str) -> str:
    """Proxy URL with userinfo replaced by ``***`` (never log credentials)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, "***@" + host, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class ProxySettings:
    """Effective proxy settings (§11.4)."""
    http: Optional[str]
    https: Optional[str]
    no_proxy: Tuple[str, ...]
    source: str                      # "config" | "env" | "system" | "none"
    strict_dns: bool = False
    ca_file: Optional[str] = None

    def describe(self) -> str:
        """Credentials redacted, e.g. ``http://***@127.0.0.1:7890 (env)``; ``none`` when no proxy."""
        url = self.https or self.http
        if not url:
            return "none"
        if self.http and self.https and self.http != self.https:
            return f"http: {redact_proxy(self.http)}, https: {redact_proxy(self.https)} ({self.source})"
        return f"{redact_proxy(url)} ({self.source})"

    def to_dict(self) -> dict:
        """For _download specs (credentials included; the spec file is 0600)."""
        return {"http": self.http, "https": self.https, "no_proxy": list(self.no_proxy), "source": self.source,
                "strict_dns": self.strict_dns, "ca_file": self.ca_file}

    def __repr__(self) -> str:
        return (f"ProxySettings(http={redact_proxy(self.http or '') or None!r}, "
                f"https={redact_proxy(self.https or '') or None!r}, no_proxy={self.no_proxy!r}, "
                f"source={self.source!r}, strict_dns={self.strict_dns!r}, ca_file={self.ca_file!r})")


NONE = ProxySettings(None, None, ALWAYS_NO_PROXY, "none")


# ---------------------------------------------------------------------------------------------------------------
# URL validation, wsl-host

def _is_wsl() -> bool:
    if not sys.platform.startswith("linux"):
        return False
    if os.environ.get("WSL_DISTRO_NAME") or os.environ.get("WSL_INTEROP"):
        return True
    try:
        with open("/proc/sys/kernel/osrelease", "r", encoding="ascii", errors="replace") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


def _wsl_gateway() -> Optional[str]:
    """Default gateway from /proc/net/route (= the Windows host in WSL NAT mode)."""
    try:
        with open(_ROUTE_FILE, "r", encoding="ascii", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    for line in lines[1:]:
        cols = line.split()
        if len(cols) < 3 or cols[1] != "00000000":
            continue
        try:
            raw = int(cols[2], 16)
        except ValueError:
            continue
        if raw == 0:
            continue
        return str(ipaddress.IPv4Address(raw.to_bytes(4, "little")))
    return None


def _clean_url(raw: Any, what: str, *, scheme_default: str = "") -> Optional[str]:
    """Validated ``http://[user:pass@]host:port`` or None (ignored with one WARNING for other schemes).

    ``wsl-host`` is replaced by the WSL gateway (outside WSL: ``bad_request``)."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    if "://" not in s and scheme_default:
        s = scheme_default + "://" + s
    try:
        p = urlsplit(s)
        port = p.port
    except ValueError:
        _warn_once(f"ignoring invalid proxy URL in {what}: {redact_proxy(s)}")
        return None
    scheme = (p.scheme or "").lower()
    if scheme != "http":
        _warn_once(f"ignoring the {scheme or 'scheme-less'} proxy in {what} ({redact_proxy(s)}): {ONLY_HTTP_WARNING}")
        return None
    host = (p.hostname or "").lower()
    if not host or p.query or p.fragment or (p.path not in ("", "/")):
        _warn_once(f"ignoring invalid proxy URL in {what}: {redact_proxy(s)} (expected http://host:port)")
        return None
    if any(c in s for c in "\r\n\x00 \t"):
        _warn_once(f"ignoring invalid proxy URL in {what}")
        return None
    if host == WSL_HOST:
        if not _is_wsl():
            raise bad_request(f"{what} uses '{WSL_HOST}', which only works inside WSL",
                              hint="use the proxy's real address, e.g. http://127.0.0.1:7890")
        gw = _wsl_gateway()
        if not gw:
            _warn_once(f"{what}: cannot find the Windows host address for '{WSL_HOST}' (no default route); "
                       "no proxy for now")
            return None
        host = gw
    userinfo = ""
    if p.username is not None:
        userinfo = p.netloc.rsplit("@", 1)[0] + "@"
    hp = f"[{host}]" if ":" in host else host
    return f"http://{userinfo}{hp}:{port or 80}"


def _split_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        items = value.replace(";", ",").split(",")
    else:
        items = [str(x) for x in value]
    return [x.strip() for x in items if x and x.strip()]


# ---------------------------------------------------------------------------------------------------------------
# system proxy

def _registry_values() -> Tuple[int, str, str]:
    """(ProxyEnable, ProxyServer, ProxyOverride) of HKCU Internet Settings (Windows only)."""
    import winreg  # function-level: Windows only
    key = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
    vals: Dict[str, Any] = {}
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
        for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
            try:
                vals[name] = winreg.QueryValueEx(k, name)[0]
            except OSError:
                vals[name] = None
    try:
        enable = int(vals.get("ProxyEnable") or 0)
    except (TypeError, ValueError):
        enable = 0
    return enable, str(vals.get("ProxyServer") or ""), str(vals.get("ProxyOverride") or "")


def parse_registry(enable: int, server: str, override: str) -> Tuple[Optional[str], Optional[str], List[str]]:
    """(http, https, no_proxy) from the Windows Internet Settings values (v8 rules): a bare ``host:port`` is
    ``http://host:port`` for both schemes; ``http=…;https=…`` entries are per scheme (scheme-less = http://);
    ``socks=`` entries are ignored with a warning; ``<local>`` in ProxyOverride = names without a dot."""
    if not enable or not server.strip():
        return None, None, []
    http_url: Optional[str] = None
    https_url: Optional[str] = None
    what = "the Windows system proxy"
    if "=" not in server:
        url = _clean_url(server.split(";")[0], what, scheme_default="http")
        http_url = https_url = url
    else:
        for part in server.split(";"):
            if "=" not in part:
                continue
            k, _, v = part.partition("=")
            k = k.strip().lower()
            if k == "socks":
                _warn_once(f"ignoring the socks entry of {what}: {ONLY_HTTP_WARNING}")
                continue
            if k not in ("http", "https"):
                continue
            url = _clean_url(v, what, scheme_default="http")
            if k == "http":
                http_url = url
            else:
                https_url = url
    no_proxy = [x.strip() for x in (override or "").split(";") if x.strip()]
    return http_url, https_url, no_proxy


def _system_proxies() -> Tuple[Optional[str], Optional[str], List[str]]:
    if _IS_WINDOWS:
        try:
            return parse_registry(*_registry_values())
        except Exception as e:  # noqa: BLE001 - no registry access: no system proxy
            log.debug("reading the Windows proxy settings failed: %s", e)
            return None, None, []
    import urllib.request  # function-level: rarely needed
    try:
        px = urllib.request.getproxies()
    except Exception as e:  # noqa: BLE001
        log.debug("getproxies failed: %s", e)
        return None, None, []
    what = "the system proxy"
    http_url = _clean_url(px.get("http"), what, scheme_default="http")
    https_url = _clean_url(px.get("https"), what, scheme_default="http")
    no = _split_list(px.get("no"))
    return http_url, https_url, no


# ---------------------------------------------------------------------------------------------------------------
# loading

_ENV_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY",
             "no_proxy")


def _env_get(env: Mapping[str, str], *names: str) -> Optional[str]:
    for n in names:
        v = env.get(n)
        if v is not None and str(v).strip():
            return str(v)
    return None


def _load(cfg: dict, env: Mapping[str, str], explicit: Optional[str]) -> ProxySettings:
    strict = bool(cfg.get("strict_dns"))
    ca_file = cfg.get("ca_file") or None
    no_proxy: List[str] = _split_list(cfg.get("no_proxy")) + _split_list(_env_get(env, "NO_PROXY", "no_proxy"))

    def build(http_url: Optional[str], https_url: Optional[str], source: str, extra: Sequence[str] = ()) -> ProxySettings:
        items: List[str] = []
        for x in list(no_proxy) + list(extra) + list(ALWAYS_NO_PROXY):
            if x not in items:
                items.append(x)
        if not http_url and not https_url:
            source = "none"
        return ProxySettings(http_url, https_url, tuple(items), source, strict, ca_file)

    if explicit is not None and str(explicit).strip().lower() not in ("", "auto"):
        ex = str(explicit).strip()
        if ex.lower() == "off":
            return build(None, None, "none")
        url = _clean_url(ex, "the connection proxy")
        if url is None:
            raise bad_request(f"invalid proxy URL {redact_proxy(ex)!r}",
                              hint="use http://[user:pass@]host:port (an HTTP or mixed port, e.g. 7890), or off")
        return build(url, url, "config")
    proxy = cfg.get("proxy")
    if isinstance(proxy, str) and proxy.strip().lower() == "off":
        return build(None, None, "none")
    if isinstance(proxy, str) and proxy.strip() and proxy.strip().lower() != "auto":
        url = _clean_url(proxy, "network.proxy")
        if url is not None:
            return build(url, url, "config")
    # env
    https_raw = _env_get(env, "HTTPS_PROXY", "https_proxy")
    http_raw = _env_get(env, "HTTP_PROXY", "http_proxy")
    all_raw = _env_get(env, "ALL_PROXY", "all_proxy")
    https_url = _clean_url(https_raw, "HTTPS_PROXY") if https_raw else None
    http_url = _clean_url(http_raw, "HTTP_PROXY") if http_raw else None
    if all_raw and (https_url is None or http_url is None):
        all_url = _clean_url(all_raw, "ALL_PROXY")
        https_url = https_url or (all_url if not https_raw else None)
        http_url = http_url or (all_url if not http_raw else None)
    if https_url or http_url:
        return build(http_url, https_url, "env")
    if cfg.get("use_system_proxy", True):
        s_http, s_https, s_no = _system_proxies()
        if s_http or s_https:
            return build(s_http, s_https, "system", s_no)
    return build(None, None, "none")


def load_proxy_settings(network_cfg: Optional[dict], env: Mapping[str, str] = os.environ, *,
                        explicit: Optional[str] = None) -> ProxySettings:
    """Effective settings: explicit (relay.proxy / connection.proxy) > ``network.proxy`` > env > system proxy
    (cached 60 s, so system proxy toggles and the WSL host address are picked up)."""
    cfg = dict(network_cfg or {})
    env_part = {k: env.get(k) for k in _ENV_KEYS if env.get(k) is not None}
    try:
        key = json.dumps([cfg, env_part, explicit], sort_keys=True, default=str)
    except (TypeError, ValueError):
        key = repr((sorted(cfg.items(), key=str), sorted(env_part.items()), explicit))
    now = _now()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL:
            return hit[1]
    settings = _load(cfg, env, explicit)
    with _lock:
        if len(_cache) > 64:
            _cache.clear()
        _cache[key] = (now, settings)
    return settings


def clear_cache() -> None:
    """Forget cached settings (tests, config reload)."""
    with _lock:
        _cache.clear()


# ---------------------------------------------------------------------------------------------------------------
# bypass / proxy_for

def _strip_port(entry: str) -> str:
    e = entry.strip()
    if e.startswith("["):
        close = e.find("]")
        return e[1:close] if close > 0 else e
    if e.count(":") == 1:
        h, _, p = e.partition(":")
        if p.isdigit() or p == "*":
            return h
    return e


def _host_norm(host: str) -> str:
    h = (host or "").strip()
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    return h.split("%", 1)[0].rstrip(".").lower()


def _ip(s: str) -> Optional[Any]:
    try:
        return ipaddress.ip_address(s)
    except ValueError:
        return None


def bypass(settings: ProxySettings, host: str, ips: Sequence[str] = ()) -> bool:
    """True when the host must go direct: ``*`` = all; ``example.com`` / ``.example.com`` match the name and its
    subdomains (label boundary); globs; IP literals; CIDRs (also against ``ips``); ``<local>`` = names without a
    dot; case-insensitive; ``:port`` suffixes ignored."""
    h = _host_norm(host)
    if not h:
        return True
    addrs = [a for a in [_ip(h)] + [_ip(_host_norm(i)) for i in ips or ()] if a is not None]
    for raw in settings.no_proxy or ():
        e = _strip_port(str(raw)).lower().rstrip(".")
        if not e:
            continue
        if e == "*":
            return True
        if e == "<local>":
            if "." not in h and ":" not in h:
                return True
            continue
        if "/" in e:
            try:
                net = ipaddress.ip_network(e, strict=False)
            except ValueError:
                continue
            if any(a.version == net.version and a in net for a in addrs):
                return True
            continue
        ip_e = _ip(e)
        if ip_e is not None:
            if any(a == ip_e for a in addrs):
                return True
            continue
        if any(c in e for c in "*?["):
            if fnmatch.fnmatchcase(h, e) or (e.startswith("*.") and h == e[2:]):
                return True
            continue
        base = e.lstrip(".")
        if h == base or h.endswith("." + base):
            return True
    return False


def proxy_for(settings: ProxySettings, scheme: str, host: str, ips: Sequence[str] = ()) -> Optional[str]:
    """Proxy URL for a target or None (no proxy for the scheme, bypass, a local name, a non-public IP literal, or
    a locally resolved address that is not public/fake-ip)."""
    url = settings.https if (scheme or "").lower() == "https" else settings.http
    if not url:
        return None
    h = _host_norm(host)
    if not h or bypass(settings, h, ips):
        return None
    if _ip(h) is not None:
        try:
            if ip_category(h) != "public":
                return None
        except Exception:  # noqa: BLE001
            return None
    elif is_local_name(h):
        return None
    for ip in ips or ():
        try:
            cat = ip_category(_host_norm(ip))
        except Exception:  # noqa: BLE001
            return None
        if cat not in ("public", "fakeip"):
            return None
    return url


# ---------------------------------------------------------------------------------------------------------------
# CONNECT tunnel

def _proxy_parts(proxy_url: str) -> Tuple[str, int, Optional[str]]:
    try:
        p = urlsplit(proxy_url)
        port = p.port or 80
    except ValueError:
        raise bad_request("invalid proxy URL", hint="use http://[user:pass@]host:port")
    if (p.scheme or "").lower() != "http" or not p.hostname:
        raise bad_request(f"invalid proxy URL {redact_proxy(proxy_url)!r}", hint="use http://[user:pass@]host:port")
    auth = None
    if p.username is not None:
        raw = f"{unquote(p.username)}:{unquote(p.password or '')}".encode("utf-8")
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
    return p.hostname, port, auth


def _unreachable_hint(host: str, port: int) -> str:
    hp = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    return f"proxy {hp} is not reachable; is Clash/v2rayN running? (network.proxy)"


def _read_head(sock: socket.socket, deadline: float) -> bytes:
    """Status line + headers of the proxy's answer (<= 16 KiB) without consuming any tunnel byte."""
    buf = b""
    while True:
        left = deadline - _now()
        if left <= 0:
            raise socket.timeout("timed out")
        sock.settimeout(left)
        peek = sock.recv(4096, socket.MSG_PEEK)
        if not peek:
            raise ConnectionError("the proxy closed the connection")
        end = (buf + peek).find(b"\r\n\r\n")
        if end + 4 > MAX_CONNECT_HEADER or (end < 0 and len(buf) + len(peek) > MAX_CONNECT_HEADER):
            raise ValueError("oversized proxy response")
        if end >= 0:
            want = end + 4
            while len(buf) < want:
                b = sock.recv(want - len(buf))
                if not b:
                    raise ConnectionError("the proxy closed the connection")
                buf += b
            return buf
        buf += sock.recv(len(peek))


def open_tunnel(proxy_url: str, target_host: str, target_port: int, *, timeout: float) -> socket.socket:
    """CONNECT tunnel through an HTTP proxy (Proxy-Authorization from the URL's userinfo); returns the raw socket
    (the caller wraps TLS with SNI = the target host). Errors: 407 -> ``proxy_auth``, other statuses -> ``proxy``
    with ``details.status``, unreachable proxy -> ``proxy`` with the Clash hint. Never ``maybe_executed``."""
    p_host, p_port, auth = _proxy_parts(proxy_url)
    shown = redact_proxy(proxy_url)
    deadline = _now() + max(0.1, float(timeout))
    try:
        sock = socket.create_connection((p_host, p_port), timeout=max(0.1, deadline - _now()))
    except OSError as e:
        raise unavailable(f"proxy {p_host}:{p_port} is not reachable: {getattr(e, 'strerror', None) or e}",
                          reason="proxy", maybe_executed=False, proxy=shown, hint=_unreachable_hint(p_host, p_port))
    th = (target_host or "").strip()
    if th.startswith("[") and th.endswith("]"):
        th = th[1:-1]
    hp = f"[{th}]:{int(target_port)}" if ":" in th else f"{th}:{int(target_port)}"
    if any(c in hp for c in "\r\n\x00 "):
        sock.close()
        raise bad_request("invalid tunnel target")
    lines = [f"CONNECT {hp} HTTP/1.1", f"Host: {hp}", "User-Agent: hive"]
    if auth:
        lines.append(f"Proxy-Authorization: {auth}")
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        head = _read_head(sock, deadline)
    except socket.timeout:
        sock.close()
        raise unavailable(f"proxy {p_host}:{p_port} did not answer the CONNECT request in time", reason="proxy",
                          maybe_executed=False, proxy=shown, hint=_unreachable_hint(p_host, p_port))
    except ValueError:
        sock.close()
        raise unavailable(f"proxy {p_host}:{p_port} sent an oversized CONNECT response", reason="proxy",
                          maybe_executed=False, proxy=shown)
    except OSError as e:
        sock.close()
        raise unavailable(f"proxy {p_host}:{p_port} failed during CONNECT: {getattr(e, 'strerror', None) or e}",
                          reason="proxy", maybe_executed=False, proxy=shown, hint=_unreachable_hint(p_host, p_port))
    status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = status_line.split(" ", 2)
    try:
        status = int(parts[1]) if len(parts) > 1 and parts[0].upper().startswith("HTTP/") else 0
    except ValueError:
        status = 0
    if status == 200:
        sock.settimeout(None)
        return sock
    sock.close()
    if status == 407:
        raise unavailable("proxy requires credentials", reason="proxy_auth", maybe_executed=False, proxy=shown,
                          hint="put user:pass in network.proxy")
    if status == 0:
        raise unavailable(f"proxy {p_host}:{p_port} sent an invalid CONNECT response", reason="proxy",
                          maybe_executed=False, proxy=shown, hint="is network.proxy an HTTP (or mixed) proxy port?")
    raise unavailable(f"proxy refused CONNECT: {status}", reason="proxy", maybe_executed=False, status=status,
                      proxy=shown, target=hp)


# ---------------------------------------------------------------------------------------------------------------
# children

def child_proxy_env(settings: ProxySettings, extra_no_proxy: Sequence[str] = ()) -> Dict[str, str]:
    """HTTP(S)_PROXY / http(s)_proxy / NO_PROXY / no_proxy for children (§10.1 step 3); {} when no proxy."""
    if settings is None or not (settings.http or settings.https):
        return {}
    out: Dict[str, str] = {}
    if settings.http:
        out["HTTP_PROXY"] = out["http_proxy"] = settings.http
    if settings.https:
        out["HTTPS_PROXY"] = out["https_proxy"] = settings.https
    items: List[str] = []
    for x in list(CHILD_NO_PROXY) + [str(e).strip() for e in extra_no_proxy or ()]:
        if x and x not in items:
            items.append(x)
    out["NO_PROXY"] = out["no_proxy"] = ",".join(items)
    return out


__all__ = ["ProxySettings", "load_proxy_settings", "proxy_for", "bypass", "open_tunnel", "child_proxy_env",
           "redact_proxy", "parse_registry", "clear_cache", "ALWAYS_NO_PROXY", "CHILD_NO_PROXY"]
