"""NetGuard (SSRF categories, proxy routes, C1–C4), guarded HTTP, open_stream, open_download, AuthSpec, tcp_check
(design §11.2 and §5.4 NetGuard, WP3).

Phase 0: ``VettedTarget``, ``AuthSpec``, ``HttpResult`` and ``auth_host_allowed`` (the §5.13 matcher) are REAL and
kept by WP3; ``open_stream`` is a loopback-only stub (one hop with http.client to loopback http targets, else
``unsupported``); ``NetGuard`` (no specified behaviour; WP1 tests use ``tests.helpers.FakeNetGuard``),
``guarded_request``, ``open_download`` and ``tcp_check`` are stubs.
Module rule: imports only stdlib, ``hive.util``, ``hive.errors`` and ``hive.netproxy`` at module level.
"""
from __future__ import annotations

import fnmatch
import http.client
import socket
import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import netproxy
from .errors import HiveError, bad_request, timeout, unavailable, unsupported
from .util import ip_category, is_loopback_host


@dataclass(frozen=True)
class VettedTarget:
    """Result of a URL check: where and how to connect (§11.2)."""
    scheme: str
    host: str
    port: int
    path: str
    ip: Optional[str]          # pinned IP for direct routes (and proxy routes with pin_ip); None when proxied+unresolvable
    route: str                 # "direct" | "proxy"
    proxy: Optional[str]       # proxy URL (with credentials; never logged) when route == "proxy"
    category: str              # category used for the decision ("public", "fakeip", "private", "loopback", …)
    pin_ip: bool = False       # v8: proxy route goes to `ip` (strict_dns, gated write requests, rule 5c)


@dataclass(frozen=True)
class AuthSpec:
    """A resolved secret for one request; ``repr`` hides the value."""
    name: str
    header: str
    value: str = field(repr=False)
    hosts: Tuple[str, ...]

    def __repr__(self) -> str:
        return f"AuthSpec(name={self.name!r}, header={self.header!r}, value='***', hosts={self.hosts!r})"


@dataclass
class HttpResult:
    """Result of guarded_request."""
    status: int
    reason: str
    url: str
    headers: Dict[str, str]
    body: bytes
    truncated: bool
    elapsed_s: float
    route: str


def _host_key(host: str) -> str:
    h = (host or "").strip()
    if h.startswith("[") and "]" in h:
        h = h[1:h.index("]")]
    return h.rstrip(".").lower()


def auth_host_allowed(hosts: Sequence[str], host: str, scheme: str) -> bool:
    """§5.13 matching (rule C1 for secrets): "loopback"/"private" entries match those IPs (any scheme); other
    entries are host globs and require https."""
    h = _host_key(host)
    if not h:
        return False
    sch = (scheme or "").lower()
    cat: Optional[str] = None
    try:
        cat = ip_category(h)
    except HiveError:
        cat = None
    for entry in hosts or ():
        e = str(entry).strip().lower()
        if not e:
            continue
        if e == "loopback":
            if is_loopback_host(h):
                return True
            continue
        if e == "private":
            if cat == "private":
                return True
            continue
        if sch == "https" and fnmatch.fnmatchcase(h, e.rstrip(".")):
            return True
    return False


def _na(what: str) -> HiveError:
    return unsupported(f"netutil.{what} not available yet (WP3)")


class NetGuard:
    """SSRF guard + proxy routing (WP3). Phase 0 stub: accepts the constructor arguments, check_url is unsupported."""

    def __init__(self, *, http_allow: Sequence[str], http_deny: Sequence[str], download_allow: Sequence[str],
                 allow_metadata_ip: bool, proxies: "netproxy.ProxySettings", own_ports: Sequence[int] = (),
                 download_redirect_allow: Sequence[str] = ()):
        self.http_allow = tuple(http_allow)
        self.http_deny = tuple(http_deny)
        self.download_allow = tuple(download_allow)
        self.allow_metadata_ip = bool(allow_metadata_ip)
        self.proxies = proxies
        self.own_ports = tuple(own_ports)
        self.download_redirect_allow = tuple(download_redirect_allow)

    def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> VettedTarget:
        """Rules 1–6 of §5.4 NetGuard (WP3)."""
        raise _na("NetGuard.check_url")

    def to_dict(self) -> dict:
        """Serializable form for 0600 specs (WP3)."""
        raise _na("NetGuard.to_dict")

    @classmethod
    def from_dict(cls, d: dict) -> "NetGuard":
        """Rebuild in the _download child (WP3)."""
        raise _na("NetGuard.from_dict")


def guarded_request(method: str, url: str, *, guard: Any, purpose: str, headers: Optional[Dict[str, str]] = None,
                    body: Optional[bytes] = None, timeout: float = 30, max_bytes: int = 65536,
                    follow_redirects: bool = True, verify_tls: bool = True, auth: Optional[AuthSpec] = None,
                    cancel: Optional[threading.Event] = None) -> HttpResult:
    """Vetted, proxy-aware HTTP request with C1–C4 credential rules (WP3)."""
    raise _na("guarded_request")


def _vet(guard: Any, url: str, purpose: str, method: str) -> None:
    check = getattr(guard, "check_url", None)
    if check is None:
        return
    try:
        check(url, purpose=purpose, method=method, redirect=False)
    except HiveError as e:
        if e.code != "unsupported":   # the P0 NetGuard stub cannot vet; loopback-only below is the stub's limit
            raise


def open_stream(method: str, url: str, *, guard: Any, purpose: str, headers: Optional[Dict[str, str]] = None,
                body: Optional[bytes] = None, connect_timeout: float = 10, idle_timeout: float = 300,
                auth: Optional[AuthSpec] = None) -> Tuple[int, Dict[str, str], Iterator[bytes]]:
    """One hop, no redirects, body streamed in chunks (P0 stub: http:// loopback targets only)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise bad_request(f"invalid URL: {url!r}")
    if parts.scheme not in ("http", "https"):
        raise bad_request(f"unsupported URL scheme in {url!r}")
    if parts.username or parts.password:
        raise bad_request("credentials in URLs are not allowed; use auth_env")
    host = parts.hostname or ""
    if parts.scheme != "http" or not is_loopback_host(host):
        raise unsupported("open_stream to non-loopback or https targets is not available yet (WP3)")
    _vet(guard, url, purpose, method)
    try:
        port = parts.port or 80
    except ValueError:
        raise bad_request(f"invalid port in {url!r}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    hdrs = dict(headers or {})
    if auth is not None:
        if auth_host_allowed(auth.hosts, host, "http"):
            hdrs[auth.header] = auth.value
    conn = http.client.HTTPConnection(host, port, timeout=connect_timeout)
    # §11.2 error rules (as guarded_request): timeouts -> timeout; socket errors -> unavailable reason connect|dns;
    # maybe_executed False before any request byte was written (connect phase), else True
    try:
        conn.connect()
    except socket.gaierror as e:
        conn.close()
        raise unavailable(f"cannot resolve {host}: {e}", reason="dns", maybe_executed=False)
    except socket.timeout as e:
        conn.close()
        raise timeout(f"timeout connecting to {host}:{port}: {e}", maybe_executed=False)
    except OSError as e:
        conn.close()
        raise unavailable(f"cannot connect to {host}:{port}: {e}", reason="connect", maybe_executed=False,
                          hint=f"nothing listening on {host}:{port}")
    try:
        if conn.sock is not None:
            conn.sock.settimeout(idle_timeout)
        conn.request(method, path, body=body, headers=hdrs)
        resp = conn.getresponse()
    except socket.timeout as e:
        conn.close()
        raise timeout(f"timeout talking to {host}:{port}: {e}", maybe_executed=True)
    except (OSError, http.client.HTTPException) as e:
        conn.close()
        raise unavailable(f"connection to {host}:{port} failed: {e}", reason="connect", maybe_executed=True)
    status = resp.status
    rheaders = {k: v for k, v in resp.getheaders()}

    def chunks() -> Iterator[bytes]:
        try:
            while True:
                try:
                    b = resp.read1(65536) if hasattr(resp, "read1") else resp.read(65536)
                except socket.timeout as e:
                    raise timeout(f"idle timeout reading from {host}:{port}: {e}", maybe_executed=True)
                except (OSError, http.client.HTTPException) as e:
                    raise unavailable(f"connection to {host}:{port} failed: {e}", reason="connect",
                                      maybe_executed=True)
                if not b:
                    break
                yield b
        finally:
            conn.close()

    return status, rheaders, chunks()


def open_download(url: str, *, guard: Any, purpose: str = "download", headers: Optional[Dict[str, str]] = None,
                  auth: Optional[AuthSpec] = None, range_start: int = 0, connect_timeout: float = 10,
                  idle_timeout: float = 60, max_redirects: int = 5, cancel: Optional[threading.Event] = None
                  ) -> Tuple[int, Dict[str, str], str, Iterator[bytes]]:
    """The only HTTP entry point of _download (WP3)."""
    raise _na("open_download")


def tcp_check(host: str, port: int, timeout: float = 1.0) -> Tuple[bool, Optional[float], Optional[str]]:
    """(open, latency_ms, error); never proxied (WP3)."""
    raise _na("tcp_check")


__all__ = ["VettedTarget", "NetGuard", "AuthSpec", "auth_host_allowed", "HttpResult", "guarded_request",
           "open_stream", "open_download", "tcp_check"]
