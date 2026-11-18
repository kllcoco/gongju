"""NetGuard (SSRF categories, proxy routes, C1–C4), guarded HTTP, open_stream, open_download, AuthSpec, tcp_check
(design §11.2 and §5.4 NetGuard, WP3).

WP3a: ``NetGuard`` (rules 1–6 incl. ``HOST:PORT`` deny entries, fake-ip, redirect allow, proxy routes and
``pin_ip``), ``guarded_request`` (per-hop vetting, pinned direct connections, proxy CONNECT/absolute-form, C1–C4),
``tcp_check``, ``AuthSpec``/``VettedTarget``/``HttpResult``/``auth_host_allowed``. Still Phase 0 until WP3b:
``open_stream`` (loopback-only stub: one hop with http.client to loopback http targets, else ``unsupported``) and
``open_download``.
Module rule: imports only stdlib, ``hive.util``, ``hive.errors`` and ``hive.netproxy`` at module level.
"""
from __future__ import annotations

import base64
import fnmatch
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import unquote, urljoin, urlsplit

from . import netproxy
from .errors import HiveError, bad_request, cancelled, denied, timeout, unavailable, unsupported
from .util import ip_category, is_local_name, is_loopback_host

log = logging.getLogger("hive.netutil")


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
    return unsupported(f"netutil.{what} not available yet (WP3b)")


# ---------------------------------------------------------------------------------------------------------------
# NetGuard (§5.4 NetGuard rules 1–6, §11.2)

PURPOSES = ("http", "download", "model", "health")
METADATA_NAMES = ("metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal")
CATEGORY_WORDS = ("loopback", "private", "public")
DNS_TIMEOUT = 5.0
MAX_REDIRECTS = 5
REDIRECT_CODES = (301, 302, 303, 307, 308)
FRAMING_HEADERS = ("host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "keep-alive",
                   "trailer")
CRED_HEADER_RE = re.compile(r"(?i)(pass|secret|token|key|auth|cred|cookie|bearer)")   # = audit.SECRET_KEY_RE
_SSRF_HINT = "this target is not allowed by the node's network policy (net.http_allow / net.download_allow)"


@dataclass(frozen=True)
class _Entry:
    kind: str                                  # "category" | "net" | "host"
    value: Any                                 # category word | ip_network | lowercase host glob
    port: Optional[int] = None


def _parse_entries(items: Sequence[str]) -> Tuple[_Entry, ...]:
    """Allow/deny list entries: categories, IPs, CIDRs, host globs, each optionally ``HOST:PORT`` /
    ``[v6]:PORT`` (v7)."""
    out: List[_Entry] = []
    for raw in items or ():
        e = str(raw).strip().lower()
        if not e:
            continue
        if e in CATEGORY_WORDS:
            out.append(_Entry("category", e))
            continue
        host, port = e, None
        if e.startswith("["):
            close = e.find("]")
            if close > 0:
                host, rest = e[1:close], e[close + 1:]
                if rest.startswith(":") and rest[1:].isdigit():
                    port = int(rest[1:])
        else:
            try:
                out.append(_Entry("net", ipaddress.ip_network(e, strict=False)))
                continue
            except ValueError:
                pass
            if e.count(":") == 1:
                h, p = e.rsplit(":", 1)
                if p.isdigit():
                    host, port = h, int(p)
        try:
            out.append(_Entry("net", ipaddress.ip_network(host, strict=False), port))
        except ValueError:
            out.append(_Entry("host", host.rstrip("."), port))
    return tuple(out)


def _addr(ip: str) -> Optional[Any]:
    try:
        return ipaddress.ip_address(_host_key(ip).split("%", 1)[0])
    except ValueError:
        return None


def _unwrapped(a: Any) -> Any:
    if isinstance(a, ipaddress.IPv6Address):
        if a.ipv4_mapped is not None:
            return a.ipv4_mapped
        if a in ipaddress.IPv6Network("64:ff9b::/96"):
            return ipaddress.IPv4Address(int(a) & 0xFFFFFFFF)
        if a.sixtofour is not None:
            return a.sixtofour
        if a.teredo is not None:
            return a.teredo[1]
    return a


def _port_ok(entry: _Entry, port: int) -> bool:
    return entry.port is None or entry.port == port


def _ip_matches(entries: Sequence[_Entry], ip: str, port: int) -> bool:
    a = _addr(ip)
    if a is None:
        return False
    cands = {a, _unwrapped(a)}
    for e in entries:
        if e.kind == "net" and _port_ok(e, port):
            for c in cands:
                if c.version == e.value.version and c in e.value:
                    return True
    return False


def _host_matches(entries: Sequence[_Entry], host: str, port: int) -> bool:
    return any(e.kind == "host" and _port_ok(e, port) and fnmatch.fnmatchcase(host, e.value) for e in entries)


def _categories(entries: Sequence[_Entry]) -> FrozenSet[str]:
    return frozenset(e.value for e in entries if e.kind == "category")


def _resolve(host: str, port: int, timeout: float = DNS_TIMEOUT) -> List[str]:
    """getaddrinfo in a worker thread bounded by ``timeout``; [] when the name does not resolve (or too slowly)."""
    result: List[List[str]] = []

    def run() -> None:
        try:
            infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except (OSError, UnicodeError):
            return
        ips: List[str] = []
        for info in infos:
            ip = str(info[4][0]).split("%", 1)[0]
            if ip not in ips:
                ips.append(ip)
        result.append(ips)

    t = threading.Thread(target=run, name="hive-dns", daemon=True)
    t.start()
    t.join(max(0.05, timeout))
    return result[0] if result else []


def _split_url(url: str) -> Tuple[str, str, int, str]:
    """(scheme, host, port, path+query) with rule 1 (http/https only, no userinfo)."""
    if not isinstance(url, str) or not url or len(url) > 8192 or any(c in url for c in "\r\n\x00"):
        raise bad_request("invalid URL")
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        raise bad_request(f"invalid URL: {url[:200]!r}")
    scheme = (parts.scheme or "").lower()
    if scheme not in ("http", "https"):
        raise bad_request(f"unsupported URL scheme in {url[:200]!r}", hint="use http:// or https://")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise bad_request("credentials in URLs are not allowed; use auth_env",
                          hint="put the token in a node secret and pass its name as auth_env")
    host = _host_key(parts.hostname or "")
    if not host:
        raise bad_request(f"URL without host: {url[:200]!r}")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    return scheme, host, port or (443 if scheme == "https" else 80), path


class NetGuard:
    """SSRF guard + proxy routing (§5.4 NetGuard rules 1–6). Built by Policy (WP1); rebuilt in the _download child
    from ``to_dict()``."""

    def __init__(self, *, http_allow: Sequence[str], http_deny: Sequence[str], download_allow: Sequence[str],
                 allow_metadata_ip: bool, proxies: "netproxy.ProxySettings", own_ports: Sequence[int] = (),
                 download_redirect_allow: Sequence[str] = ()):
        self.http_allow = tuple(str(x) for x in http_allow or ())
        self.http_deny = tuple(str(x) for x in http_deny or ())
        self.download_allow = tuple(str(x) for x in download_allow or ())
        self.allow_metadata_ip = bool(allow_metadata_ip)
        self.proxies = proxies
        self.own_ports = tuple(int(p) for p in own_ports or () if p)
        self.download_redirect_allow = tuple(str(x) for x in download_redirect_allow or ())
        self._allow = _parse_entries(self.http_allow)
        self._deny = _parse_entries(self.http_deny)
        self._dl_allow = _parse_entries(self.download_allow)
        self._dl_redirect = _parse_entries(self.download_redirect_allow)

    # ------------------------------------------------------------------------------------------- helpers
    def _deny_check(self, host: str, port: int, ips: Sequence[str]) -> None:
        hit = _host_matches(self._deny, host, port) or any(_ip_matches(self._deny, ip, port) for ip in ips)
        if hit:
            raise denied(f"{host}:{port} is denied by node policy (net.http_deny)", rule="http_deny", host=host,
                         hint="the owner blocked this address; ask the owner")

    def _ip_allowed(self, ip: str, cat: str, host: str, port: int, allow: Sequence[_Entry], *, literal: bool,
                    local_ok: bool) -> None:
        if cat in ("metadata", "linklocal"):
            if self.allow_metadata_ip:
                return
            raise denied(f"{host} ({ip}) is a cloud metadata / link-local address", rule="ssrf", category=cat,
                         host=host, hint="cloud metadata endpoints are never reachable from actions")
        if cat == "reserved" or (cat == "fakeip" and literal):
            raise denied(f"{host} ({ip}) is a reserved address", rule="ssrf", category="reserved", host=host,
                         hint=_SSRF_HINT)
        eff = "public" if cat == "fakeip" else cat
        if local_ok and eff == "loopback":
            return
        if eff in _categories(allow) or _ip_matches(allow, ip, port):
            return
        if not literal and _host_matches(allow, host, port):
            return
        allowed = ", ".join(str(e.value) + (f":{e.port}" if e.port else "") for e in allow) or "nothing"
        raise denied(f"{host} ({eff} address {ip}) is not allowed by node policy", rule="net_allow", category=eff,
                     host=host, hint=f"allowed targets on this node: {allowed}; ask the owner to extend "
                                     "net.http_allow / net.download_allow")

    def _own_port(self, purpose: str, port: int, cats: Sequence[str]) -> None:
        if purpose == "http" and port in self.own_ports and any(c == "loopback" for c in cats):
            raise denied(f"port {port} is this node's own API", rule="own_port",
                         hint="Hive's own listen ports are not reachable through net.http")

    def _proxy(self, purpose: str, scheme: str, host: str, ips: Sequence[str]) -> Optional[str]:
        if purpose == "health" or self.proxies is None:
            return None
        try:
            return netproxy.proxy_for(self.proxies, scheme, host, ips=list(ips))
        except HiveError:
            raise
        except Exception:  # noqa: BLE001 - a broken proxy config must not crash the guard
            log.warning("proxy lookup failed for %s", host, exc_info=True)
            return None

    # ------------------------------------------------------------------------------------------- check
    def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> VettedTarget:
        """Rules 1–6 of §5.4 NetGuard; purpose http|download|model|health; ``redirect=True`` on hops >= 2 (v5);
        ``method`` is accepted and ignored here (EffectivePolicy uses it for rule 5a)."""
        if purpose not in PURPOSES:
            raise bad_request(f"unknown network purpose {purpose!r}")
        scheme, host, port, path = _split_url(url)
        allow = self._dl_allow if purpose == "download" else self._allow
        if purpose == "download" and redirect and self._dl_redirect:
            allow = allow + self._dl_redirect
        local_ok = purpose in ("model", "health")
        if host in METADATA_NAMES and not self.allow_metadata_ip:
            raise denied(f"{host} is a cloud metadata name", rule="ssrf", category="metadata", host=host,
                         hint="cloud metadata endpoints are never reachable from actions")
        if _addr(host) is not None:                                   # rule 3: IP literal
            cat = ip_category(host)
            self._deny_check(host, port, [host])
            self._ip_allowed(host, cat, host, port, allow, literal=True, local_ok=local_ok)
            self._own_port(purpose, port, [cat])
            proxy = self._proxy(purpose, scheme, host, [host]) if cat == "public" else None
            return VettedTarget(scheme=scheme, host=host, port=port, path=path, ip=host,
                                route="proxy" if proxy else "direct", proxy=proxy, category=cat)
        self._deny_check(host, port, [])
        ips = _resolve(host, port, DNS_TIMEOUT)                        # rule 4
        if ips:
            cats = [ip_category(ip) for ip in ips]
            self._deny_check(host, port, ips)
            fake = [c == "fakeip" for c in cats]
            if any(fake) and not all(fake):
                raise denied(f"{host} resolves to a mix of fake-ip and other addresses", rule="ssrf",
                             category="fakeip", host=host, hint=_SSRF_HINT)
            for ip, c in zip(ips, cats):
                self._ip_allowed(ip, c, host, port, allow, literal=False, local_ok=local_ok)
            self._own_port(purpose, port, cats)
            category = "fakeip" if all(fake) else cats[0]
            proxy = None
            if all(c in ("public", "fakeip") for c in cats) and not is_local_name(host):
                proxy = self._proxy(purpose, scheme, host, ips)
            if proxy:
                pin = bool(getattr(self.proxies, "strict_dns", False)) and category != "fakeip"
                return VettedTarget(scheme=scheme, host=host, port=port, path=path, ip=ips[0], route="proxy",
                                    proxy=proxy, category=category, pin_ip=pin)
            return VettedTarget(scheme=scheme, host=host, port=port, path=path, ip=ips[0], route="direct",
                                proxy=None, category=category)
        # rule 4b: not resolvable locally -> only through a proxy, never for local names
        proxy = None if is_local_name(host) else self._proxy(purpose, scheme, host, [])
        if proxy and ("public" in _categories(allow) or _host_matches(allow, host, port)):
            return VettedTarget(scheme=scheme, host=host, port=port, path=path, ip=None, route="proxy",
                                proxy=proxy, category="public")
        raise unavailable(f"DNS lookup failed for {host}", reason="dns", maybe_executed=False, host=host,
                          hint="check the host name; on networks with blocked DNS configure network.proxy")

    def to_dict(self) -> dict:
        """Serializable form for 0600 specs (proxy credentials included)."""
        px = self.proxies.to_dict() if self.proxies is not None else None
        return {"v": 1, "http_allow": list(self.http_allow), "http_deny": list(self.http_deny),
                "download_allow": list(self.download_allow),
                "download_redirect_allow": list(self.download_redirect_allow),
                "allow_metadata_ip": self.allow_metadata_ip, "own_ports": list(self.own_ports), "proxies": px}

    @classmethod
    def from_dict(cls, d: dict) -> "NetGuard":
        """Rebuild (the _download child)."""
        if not isinstance(d, dict):
            raise bad_request("invalid NetGuard spec")
        px = d.get("proxies")
        if isinstance(px, dict):
            proxies = netproxy.ProxySettings(http=px.get("http"), https=px.get("https"),
                                             no_proxy=tuple(px.get("no_proxy") or ()),
                                             source=str(px.get("source") or "none"),
                                             strict_dns=bool(px.get("strict_dns")), ca_file=px.get("ca_file"))
        else:
            proxies = netproxy.ProxySettings(None, None, netproxy.ALWAYS_NO_PROXY, "none")
        return cls(http_allow=d.get("http_allow") or (), http_deny=d.get("http_deny") or (),
                   download_allow=d.get("download_allow") or (), allow_metadata_ip=bool(d.get("allow_metadata_ip")),
                   proxies=proxies, own_ports=d.get("own_ports") or (),
                   download_redirect_allow=d.get("download_redirect_allow") or ())


# ---------------------------------------------------------------------------------------------------------------
# guarded_request (§11.2)

class _Conn(http.client.HTTPConnection):
    """HTTPConnection whose socket comes from ``dial`` (pinned IP, proxy, CONNECT tunnel), optionally TLS-wrapped
    with SNI = the original host name; records whether any request byte was written."""

    def __init__(self, host: str, port: int, *, timeout: float, dial: Callable[[], socket.socket],
                 tls: Optional[ssl.SSLContext], sni: str):
        super().__init__(host, port, timeout=timeout)
        self._dial = dial
        self._tls = tls
        self._sni = sni
        self.sent = False

    def connect(self) -> None:
        sock = self._dial()
        try:
            sock.settimeout(self.timeout)
            if self._tls is not None:
                sock = self._tls.wrap_socket(sock, server_hostname=self._sni)
        except BaseException:
            sock.close()
            raise
        self.sock = sock

    def send(self, data: Any) -> None:
        self.sent = True
        super().send(data)


def _tls_context(verify: bool, ca_file: Optional[str]) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if verify:
        if ca_file:
            try:
                ctx.load_verify_locations(cafile=ca_file)
            except (OSError, ssl.SSLError) as e:
                log.warning("cannot load network.ca_file %s: %s", ca_file, e)
        return ctx
    # explicit verify_tls=False branch: never carries credentials (C4, checked by the caller)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _host_header(host: str, port: int, scheme: str) -> str:
    h = f"[{host}]" if ":" in host else host
    default = 443 if scheme == "https" else 80
    return h if port == default else f"{h}:{port}"


def _proxy_addr(proxy: str) -> Tuple[str, int, Optional[str]]:
    p = urlsplit(proxy)
    auth = None
    if p.username is not None:
        raw = f"{unquote(p.username)}:{unquote(p.password or '')}".encode("utf-8")
        auth = "Basic " + base64.b64encode(raw).decode("ascii")
    return p.hostname or "", p.port or 80, auth


def _check_headers(headers: Optional[Mapping[str, str]], auth: Optional[AuthSpec]) -> List[Tuple[str, str]]:
    out: List[Tuple[str, str]] = []
    for k, v in (headers or {}).items():
        name = str(k).strip()
        low = name.lower()
        if not name or any(c in name for c in " \t\r\n:") or any(c in str(v) for c in "\r\n\x00"):
            raise bad_request(f"invalid header {name[:64]!r}")
        if low in FRAMING_HEADERS or low.startswith("proxy-"):
            raise bad_request(f"header {name} is set by Hive", hint="remove it from headers")
        if low == "authorization" and auth is not None:
            raise bad_request("give either an Authorization header or auth_env, not both",
                              hint="auth_env sends the secret as 'Authorization: Bearer …'")
        out.append((name, str(v)))
    return out


def _is_cred_header(name: str) -> bool:
    low = name.lower()
    return low in ("authorization", "cookie") or bool(CRED_HEADER_RE.search(low))


def _secret_host_error(auth: AuthSpec, host: str) -> HiveError:
    hosts = ", ".join(auth.hosts) or "(no hosts)"
    return denied(f"secret '{auth.name}' may not be sent to {host}", rule="secret_host",
                  hint=f"secret {auth.name} may only be sent to {hosts}; the owner can widen it: hive node secret "
                       f"set {auth.name} --hosts +{host}")


def _auth_allowed(auth: AuthSpec, target: VettedTarget) -> bool:
    return auth_host_allowed(auth.hosts, target.host, target.scheme)


def guarded_request(method: str, url: str, *, guard: Any, purpose: str, headers: Optional[Dict[str, str]] = None,
                    body: Optional[bytes] = None, timeout: float = 30, max_bytes: int = 65536,
                    follow_redirects: bool = True, verify_tls: bool = True, auth: Optional[AuthSpec] = None,
                    cancel: Optional[threading.Event] = None) -> HttpResult:
    """Vetted, proxy-aware HTTP request (§11.2): every hop re-runs ``guard.check_url(url, purpose=, method=,
    redirect=hop>=2)``; direct routes connect to the vetted IP (Host/SNI = name); credentials per C1–C4."""
    method = str(method or "GET").upper()
    if not re.match(r"^[A-Z]{1,16}$", method):
        raise bad_request(f"invalid HTTP method {method!r}")
    user_headers = _check_headers(headers, auth)
    if auth is not None and not verify_tls:
        raise denied("verify_tls=false cannot be combined with a secret", rule="unverified_tls",
                     hint="drop verify_tls=false to use a secret")
    t0 = time.monotonic()
    deadline = t0 + max(0.1, float(timeout))
    max_bytes = max(0, int(max_bytes))
    ca_file = getattr(getattr(guard, "proxies", None), "ca_file", None)
    cur_url, cur_method, cur_body = url, method, body
    origin: Optional[Tuple[str, str, int]] = None
    sent_any = False
    hop = 0
    while True:
        hop += 1
        target = guard.check_url(cur_url, purpose=purpose, method=cur_method, redirect=hop >= 2)
        here = (target.scheme, target.host.lower(), target.port)
        if origin is None:
            origin = here
            if auth is not None:
                if target.scheme == "http" and target.category not in ("loopback", "private"):
                    raise denied(f"secret '{auth.name}' would be sent over plain http:// to {target.host}",
                                 rule="plaintext_credentials", hint="use https:// for requests that carry a secret")
                if not _auth_allowed(auth, target):
                    raise _secret_host_error(auth, target.host)
        hdrs: List[Tuple[str, str]] = [(k, v) for k, v in user_headers if here == origin or not _is_cred_header(k)]
        attach = (auth is not None and _auth_allowed(auth, target)
                  and ((target.scheme == "https" and verify_tls) or target.category in ("loopback", "private")))
        if attach:
            hdrs.append((auth.header, auth.value))   # type: ignore[union-attr]
        result = _one_hop(cur_method, target, hdrs, cur_body, deadline=deadline, max_bytes=max_bytes,
                          verify_tls=verify_tls, ca_file=ca_file, credentialed=attach, cancel=cancel,
                          sent_before=sent_any, follow=follow_redirects)
        sent_any = True
        status, reason, rheaders, data, truncated = result
        location = rheaders.get("location") if follow_redirects and status in REDIRECT_CODES else None
        if not location:
            return HttpResult(status=status, reason=reason, url=cur_url, headers=dict(rheaders.items()),
                              body=data, truncated=truncated, elapsed_s=round(time.monotonic() - t0, 3),
                              route=target.route)
        if hop > MAX_REDIRECTS:
            raise unavailable(f"too many redirects (more than {MAX_REDIRECTS})", reason="redirects",
                              maybe_executed=True, url=cur_url)
        cur_url = urljoin(cur_url, location.strip()).split("#", 1)[0]
        if status == 303 or (status in (301, 302) and cur_method not in ("GET", "HEAD")):
            cur_method = "GET" if cur_method != "HEAD" else "HEAD"
            cur_body = None


class _Headers(dict):
    """Case-insensitive view of response headers (lower-case keys for lookup, original case kept for output)."""

    def __init__(self, pairs: Sequence[Tuple[str, str]]):
        super().__init__()
        self._lower: Dict[str, str] = {}
        for k, v in pairs:
            low = k.lower()
            if low in self._lower:
                orig = self._lower[low]
                super().__setitem__(orig, super().__getitem__(orig) + ", " + v)
            else:
                self._lower[low] = k
                super().__setitem__(k, v)

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        orig = self._lower.get(key.lower())
        return super().__getitem__(orig) if orig is not None else default


def _one_hop(method: str, target: VettedTarget, hdrs: List[Tuple[str, str]], body: Optional[bytes], *,
             deadline: float, max_bytes: int, verify_tls: bool, ca_file: Optional[str], credentialed: bool,
             cancel: Optional[threading.Event], sent_before: bool, follow: bool
             ) -> Tuple[int, str, "_Headers", bytes, bool]:
    host, port = target.host, target.port
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise timeout(f"request to {host} timed out", maybe_executed=sent_before)
    tls = _tls_context(verify_tls, ca_file) if target.scheme == "https" else None
    path = target.path
    proxy_auth: Optional[str] = None
    if target.route == "proxy" and target.proxy:
        connect_to = target.ip if (target.pin_ip and target.ip) else host
        if target.scheme == "https" or credentialed:
            proxy = target.proxy

            def dial() -> socket.socket:
                return netproxy.open_tunnel(proxy, connect_to, port, timeout=max(0.1, deadline - time.monotonic()))
        else:                                                         # absolute-form through an HTTP proxy
            p_host, p_port, proxy_auth = _proxy_addr(target.proxy)
            path = "http://" + _host_header(connect_to, port, "http") + path

            def dial() -> socket.socket:
                try:
                    return socket.create_connection((p_host, p_port), timeout=max(0.1, deadline - time.monotonic()))
                except OSError as e:
                    raise unavailable(f"proxy {p_host}:{p_port} is not reachable: {e}", reason="proxy",
                                      maybe_executed=False,
                                      hint=f"proxy {p_host}:{p_port} is not reachable; is Clash/v2rayN running? "
                                           "(network.proxy)")
    else:
        if not target.ip:
            raise unavailable(f"no address for {host}", reason="dns", maybe_executed=False)
        ip = target.ip

        def dial() -> socket.socket:
            return socket.create_connection((ip, port), timeout=max(0.1, deadline - time.monotonic()))

    conn = _Conn(host, port, timeout=max(0.1, remaining), dial=dial, tls=tls, sni=host)
    try:
        try:
            conn.connect()
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            conn.putheader("Host", _host_header(host, port, target.scheme))
            names = {k.lower() for k, _ in hdrs}
            if "accept-encoding" not in names:
                conn.putheader("Accept-Encoding", "identity")
            if "user-agent" not in names:
                conn.putheader("User-Agent", "hive")
            if proxy_auth:
                conn.putheader("Proxy-Authorization", proxy_auth)
            for k, v in hdrs:
                conn.putheader(k, v)
            if body is not None or method in ("POST", "PUT", "PATCH"):
                conn.putheader("Content-Length", str(len(body or b"")))
            conn.endheaders(body if body else None)
            resp = conn.getresponse()
        except HiveError:
            raise
        except ssl.SSLCertVerificationError as e:
            raise unavailable(f"TLS certificate of {host} could not be verified: {e.verify_message or e}",
                              reason="tls", maybe_executed=sent_before,
                              hint="the server's certificate is not trusted (network.ca_file adds a CA)")
        except socket.gaierror as e:
            raise unavailable(f"cannot resolve {host}: {e}", reason="dns", maybe_executed=sent_before or conn.sent)
        except socket.timeout:
            raise timeout(f"request to {host}:{port} timed out", maybe_executed=sent_before or conn.sent)
        except ConnectionRefusedError:
            raise unavailable(f"connection to {host}:{port} refused", reason="connect",
                              maybe_executed=sent_before or conn.sent, hint=f"nothing listening on {host}:{port}")
        except (OSError, http.client.HTTPException) as e:
            raise unavailable(f"connection to {host}:{port} failed: {e}", reason="connect",
                              maybe_executed=sent_before or conn.sent)
        if target.route == "proxy" and resp.status == 407 and proxy_auth is None and not credentialed \
                and target.scheme == "http":
            raise unavailable("proxy requires credentials", reason="proxy_auth", maybe_executed=False,
                              hint="put user:pass in network.proxy")
        status, reason = int(resp.status), str(resp.reason or "")
        rheaders = _Headers(resp.getheaders())
        if follow and status in REDIRECT_CODES and rheaders.get("location"):
            return status, reason, rheaders, b"", False
        chunks: List[bytes] = []
        got = 0
        truncated = False
        try:
            while True:
                if cancel is not None and cancel.is_set():
                    raise cancelled("cancelled by the caller")
                left = deadline - time.monotonic()
                if left <= 0:
                    raise socket.timeout()
                if conn.sock is not None:
                    conn.sock.settimeout(left)
                want = min(65536, max_bytes + 1 - got)
                if want <= 0:
                    truncated = True
                    break
                b = resp.read(want)
                if not b:
                    break
                chunks.append(b)
                got += len(b)
        except HiveError:
            raise
        except socket.timeout:
            raise timeout(f"reading the response from {host}:{port} timed out", maybe_executed=True)
        except (OSError, http.client.HTTPException) as e:
            raise unavailable(f"connection to {host}:{port} failed while reading: {e}", reason="connect",
                              maybe_executed=True)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            data, truncated = data[:max_bytes], True
        return status, reason, rheaders, data, truncated
    finally:
        conn.close()


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
    """(open, latency_ms, error) of a plain TCP connect; never proxied (health checks, net.ports)."""
    t0 = time.monotonic()
    try:
        s = socket.create_connection((_host_key(host) or "127.0.0.1", int(port)), timeout=max(0.05, float(timeout)))
    except socket.timeout:
        return False, None, "timeout"
    except OSError as e:
        return False, None, (e.strerror or str(e))
    try:
        return True, round((time.monotonic() - t0) * 1000, 1), None
    finally:
        s.close()


__all__ = ["VettedTarget", "NetGuard", "AuthSpec", "auth_host_allowed", "HttpResult", "guarded_request",
           "open_stream", "open_download", "tcp_check"]
