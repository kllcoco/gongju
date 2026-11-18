"""HttpTransport: HTTP(S) client, TLS pinning, keep-alive, proxy, connect-only retry (design §15.1, §5.9, §6.5, D12, WP5a).

One keep-alive ``http.client`` connection per thread. Every connection resolves the target once (getaddrinfo, ≤ 5 s),
decides direct vs proxy with ``netproxy.proxy_for`` on the resolved addresses (§11.4), refuses to send a token over
plain ``http://`` unless the target is loopback/private/tailnet (rule C1), tunnels through a proxy with CONNECT only
(C1: the proxy never sees the token), and verifies TLS end-to-end inside the tunnel (C3): system CAs, ``ca_file``, or
a ``tls_pin`` checked before any request byte is written (§5.9). A request is retried automatically (≤ 2 times) only
while no request byte was written (D12); later failures surface as ``unavailable`` reason ``lost`` / ``timeout`` with
``details.maybe_executed`` set per §15.1.
"""
from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import logging
import os
import re
import select
import socket
import ssl
import threading
import time
import weakref
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from . import __version__, catalog
from .errors import HiveError, bad_request, denied, internal, timeout as timeout_error, unavailable
from .util import escape_terminal, ip_category, is_local_name, is_loopback_host

log = logging.getLogger("hive.transport")

MAX_CALL_SECONDS = 3700.0       # limits.max_call_seconds (§6.4)
CALL_MARGIN = 25.0              # hub call_timeout_slack 15 + 10 s margin (§15.1)
UNKNOWN_ACTION_DEADLINE = 30.0  # §15.1: unknown action -> 30
CONNECT_RETRIES = 2             # D12 / §6.5: <= 2 automatic retries, only before any request byte
CONNECT_TIMEOUT = 15.0          # TCP connect + proxy CONNECT + TLS handshake (capped by the socket timeout)
RESOLVE_TIMEOUT = 5.0           # §11.4 proxy_for: getaddrinfo once per connection, <= 5 s
IDLE_REUSE_MAX = 20.0           # servers close idle keep-alive connections after 30 s (§6.1, PHASE0 note 60)
MAX_RESPONSE_BYTES = 128 << 20  # a response larger than this is refused (memory bound)
_RETRY_REASONS = ("connect", "dns", "proxy")
_PIN_RE = re.compile(r"^[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"^[\x21-\x7e]{1,4096}\Z")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}\Z")
# request-target characters: printable ASCII without space (http.client's _contains_disallowed_url_pchar_re plus
# non-ASCII, which http.client cannot encode); '#' is never part of a request-target
_PATH_RE = re.compile(r"^[\x21-\x7e]*\Z")
READ_CHUNK = 1 << 20            # bodies of unknown length are read in 1 MiB steps (never read(MAX_RESPONSE_BYTES))

_warned_lock = threading.Lock()
_warned_hosts: set = set()


def normalize_pin(pin: str) -> str:
    """``"sha256:AB:CD…"`` (colons ignored, prefix optional, any case) -> 64 lowercase hex chars; bad_request otherwise."""
    s = str(pin or "").strip().lower()
    if s.startswith("sha256:"):
        s = s[len("sha256:"):]
    s = s.replace(":", "")
    if not _PIN_RE.match(s):
        raise bad_request("invalid tls_pin", hint='expected "sha256:<64 hex chars of the DER certificate hash>" '
                                                  "(hive tls-pin CERT.pem prints it)")
    return s


def cert_pin(der: bytes) -> str:
    """The §5.9 pin of a DER certificate: ``"sha256:<hex>"``."""
    return "sha256:" + hashlib.sha256(der).hexdigest()


def _is_ip(host: str) -> bool:
    try:
        ip_category(host)
        return True
    except HiveError:
        return False


def _category(ip: str) -> str:
    try:
        return ip_category(ip)
    except HiveError:
        return "reserved"


def _is_tailnet(host: str, ips: Sequence[str] = ()) -> bool:
    """MagicDNS names and 100.64/10 / fd7a:115c:a1e0::/48 addresses (the §5.9 warning exception)."""
    h = host.lower().rstrip(".")
    if h.endswith(".ts.net"):
        return True
    for ip in ([host] if _is_ip(host) else []) + list(ips):
        try:
            a = ipaddress.ip_address(ip.strip("[]").split("%", 1)[0])
        except ValueError:
            continue
        if a in ipaddress.ip_network("100.64.0.0/10") or a in ipaddress.ip_network("fd7a:115c:a1e0::/48"):
            return True
    return False


def plaintext_ok(host: str, ips: Optional[Sequence[str]]) -> bool:
    """Rule C1: may a credential go over plain http:// to this target (loopback or private/tailnet only)?

    An IP literal is judged by its category; a name by its locally resolved addresses (all must be loopback/private;
    a fake-ip answer means the traffic leaves through a proxy TUN, so it does not count). Link-local addresses
    (fe80::/10, 169.254/16 without the metadata address) are ignored when the name also resolves to a loopback/private
    address: mDNS hosts publish both, and link-local traffic cannot leave the link. An unresolvable name is accepted
    only when it is a local name (no dot, .lan, .local, …) or a tailnet ``*.ts.net`` name.
    """
    if is_loopback_host(host):
        return True
    if _is_ip(host):
        return _category(host) in ("loopback", "private")
    if ips:
        cats = [_category(ip) for ip in ips]
        routable = [c for c in cats if c != "linklocal"]
        return bool(routable) and all(c in ("loopback", "private") for c in routable)
    return is_local_name(host) or host.lower().rstrip(".").endswith(".ts.net")


def resolve_host(host: str, port: int, timeout: float = RESOLVE_TIMEOUT) -> Optional[List[str]]:
    """Resolve once (getaddrinfo in a helper thread, ≤ ``timeout`` s); unique addresses in order, None on failure."""
    if _is_ip(host):
        return [host.strip("[]")]
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["r"] = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
        except (OSError, UnicodeError) as e:
            box["e"] = e

    t = threading.Thread(target=run, name="hive-resolve", daemon=True)
    t.start()
    t.join(timeout)
    infos = box.get("r")
    if not infos:
        return None
    out: List[str] = []
    for info in infos:
        ip = str(info[4][0])
        if ip not in out:
            out.append(ip)
    return out or None


class _Headers(dict):
    """Response headers with case-insensitive ``get`` / ``[]`` / ``in`` (keys stored lowercase)."""

    def __init__(self, items: Sequence[Tuple[str, str]] = ()):
        super().__init__()
        for k, v in items:
            k = k.lower()
            self[k] = f"{self[k]}, {v}" if dict.__contains__(self, k) else v

    def __getitem__(self, k: str) -> str:
        return dict.__getitem__(self, k.lower())

    def __contains__(self, k: object) -> bool:
        return dict.__contains__(self, k.lower() if isinstance(k, str) else k)

    def get(self, k: str, default: Any = None) -> Any:
        return dict.get(self, k.lower(), default)


class _Conn(http.client.HTTPConnection):
    """An HTTPConnection over a socket that HttpTransport connected itself (never reconnects on its own)."""

    def __init__(self, host: str, port: int, sock: socket.socket, default_port: int):
        super().__init__(host, port)
        self.default_port = default_port   # Host header without the port when it is the scheme default
        self.sock = sock
        self.auto_open = 0                 # no hidden reconnect: HttpTransport decides about every connection
        self.hive_last_used = time.monotonic()

    def connect(self) -> None:             # pragma: no cover - auto_open=0 means http.client never calls it
        raise http.client.NotConnected("connection closed")


class HttpTransport:
    """Client transport to a node or hub (§15.1)."""

    def __init__(self, base_url: str, token: Optional[str] = None, *, ca_file: Optional[str] = None,
                 tls_pin: Optional[str] = None, timeout: float = 120.0, proxy: str = "auto"):
        parts = self._parse_url(base_url)
        self.url = parts["url"]
        self.scheme: str = parts["scheme"]
        self.host: str = parts["host"]
        self.port: int = parts["port"]
        self._prefix: str = parts["prefix"]
        if token is not None and not (isinstance(token, str) and _TOKEN_RE.match(token)):
            raise bad_request("invalid token (empty or contains whitespace/control characters)")
        self._token = token or None
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            raise bad_request("timeout must be a number of seconds")
        if not self.timeout > 0:
            raise bad_request("timeout must be > 0")
        self.ca_file = os.path.expanduser(ca_file) if ca_file else None
        self._pin = normalize_pin(tls_pin) if tls_pin else None
        if self._pin and self.scheme != "https":
            raise bad_request("tls_pin needs an https:// URL", hint=f"use https:// in {self.url}")
        self.tls_pin = ("sha256:" + self._pin) if self._pin else None
        self.proxy = self._check_proxy(proxy)
        # hint added to connect-phase failures (connect("local") sets "start it: hive node run …", §15.2)
        self.offline_hint: Optional[str] = None
        self._ssl: Optional[ssl.SSLContext] = self._ssl_context() if self.scheme == "https" else None
        self._local = threading.local()
        self._lock = threading.Lock()
        self._conns: "weakref.WeakSet[_Conn]" = weakref.WeakSet()
        self._warn_plaintext()

    # ------------------------------------------------------------------ setup

    @staticmethod
    def _parse_url(base_url: str) -> Dict[str, Any]:
        if not isinstance(base_url, str) or not base_url.strip():
            raise bad_request("missing URL", hint="e.g. http://127.0.0.1:7701 or https://hub.example.com:7700")
        raw = base_url.strip()
        try:
            u = urlsplit(raw)
            port = u.port
        except ValueError as e:
            raise bad_request(f"invalid URL {raw!r}: {e}")
        scheme = (u.scheme or "").lower()
        if scheme not in ("http", "https"):
            raise bad_request(f"unsupported URL scheme in {raw!r}", hint="use http:// or https://")
        if u.username is not None or u.password is not None or "@" in u.netloc:
            raise bad_request("credentials in the URL are not allowed", hint="pass the token with token=/token_file")
        if u.query or u.fragment:
            raise bad_request(f"URL {raw!r} must not contain a query or fragment")
        host = u.hostname or ""
        if not host:
            raise bad_request(f"URL {raw!r} has no host")
        port = port or (443 if scheme == "https" else 80)
        if not _PATH_RE.match(u.path):
            raise bad_request(f"invalid URL {raw!r}: the path contains spaces, control or non-ASCII characters",
                              hint="percent-encode the path (e.g. %20 for a space)")
        prefix = u.path.rstrip("/")
        shown = f"[{host}]" if ":" in host else host
        return {"scheme": scheme, "host": host, "port": port, "prefix": prefix,
                "url": f"{scheme}://{shown}:{port}{prefix}"}

    @staticmethod
    def _check_proxy(proxy: Optional[str]) -> str:
        """Cheap early syntax check (§11.4: only ``http://[user:pass@]host:port[/]``); the URL itself is resolved
        through ``netproxy.load_proxy_settings(explicit=…)`` on every new connection (``wsl-host``, 60 s cache)."""
        if proxy is not None and not isinstance(proxy, str):
            raise bad_request("proxy must be 'auto', 'off' or an http:// URL")
        p = (proxy or "auto").strip()
        if p.lower() in ("auto", "off"):
            return p.lower()
        if not p.lower().startswith("http://"):
            raise bad_request(f"unsupported proxy {_redact(p)!r}",
                              hint="only HTTP proxies are supported; use the client's HTTP/mixed port, "
                                   "e.g. http://127.0.0.1:7890 (socks/PAC/https proxies are not)")
        try:
            u = urlsplit(p)
            port = u.port
        except ValueError as e:
            raise bad_request(f"invalid proxy URL {_redact(p)!r}: {e}")
        if not u.hostname or port is None or u.path not in ("", "/") or u.query or u.fragment or "#" in p:
            raise bad_request(f"invalid proxy URL {_redact(p)!r}",
                              hint="expected http://[user:pass@]host:port, e.g. http://127.0.0.1:7890")
        return p

    def _ssl_context(self) -> ssl.SSLContext:
        if self._pin:
            return _pin_context()
        try:
            ctx = ssl.create_default_context(cafile=self.ca_file) if self.ca_file else ssl.create_default_context()
        except (OSError, ssl.SSLError) as e:
            raise bad_request(f"cannot load ca_file {self.ca_file!r}: {e}", hint="give a PEM file with the CA cert")
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def _warn_plaintext(self) -> None:
        if self.scheme != "http" or is_loopback_host(self.host) or _is_tailnet(self.host):
            return
        with _warned_lock:
            if self.host in _warned_hosts:
                return
            _warned_hosts.add(self.host)
        log.warning("plaintext http:// to %s: traffic is not encrypted (use https://, Tailscale or a LAN only)",
                    self.host)

    def __repr__(self) -> str:
        return f"HttpTransport({self.url!r})"

    # ------------------------------------------------------------------ public

    @property
    def has_token(self) -> bool:
        """Whether a token is configured (the value is never exposed)."""
        return self._token is not None

    def call_socket_timeout(self, body: Optional[dict], timeout: Optional[float] = None) -> Tuple[float, bool]:
        """(socket timeout, mutating) of a ``POST /v1/call`` body (§15.1: ``catalog.deadline`` + 25, min the default)."""
        base = float(timeout) if timeout is not None else self.timeout
        b = body if isinstance(body, dict) else {}
        spec = catalog.SPECS.get(b.get("action")) if isinstance(b.get("action"), str) else None
        if spec is not None:
            dl = catalog.deadline(spec, b.get("params") if isinstance(b.get("params"), dict) else None,
                                  b.get("timeout"), max_seconds=MAX_CALL_SECONDS)
            mutating = spec.mutating
        else:
            t = b.get("timeout")
            try:
                t = float(t) if t is not None and not isinstance(t, bool) else UNKNOWN_ACTION_DEADLINE
            except (TypeError, ValueError):
                t = UNKNOWN_ACTION_DEADLINE
            if t != t or t in (float("inf"), float("-inf")):
                t = UNKNOWN_ACTION_DEADLINE
            dl = max(1.0, min(MAX_CALL_SECONDS, t))
            mutating = True     # an action this client does not know: fail safe (maybe executed)
        return max(base, dl + CALL_MARGIN), mutating

    def request(self, method: str, path: str, body: Optional[dict] = None, *, timeout: Optional[float] = None,
                headers: Optional[dict] = None, raw: bool = False) -> Any:
        """Envelope result (or (status, headers, bytes) when raw)."""
        method = str(method or "").upper()
        if not re.match(r"^[A-Z]{3,7}\Z", method):
            raise bad_request(f"invalid HTTP method {method!r}")
        if not isinstance(path, str) or not path.startswith("/") or not _PATH_RE.match(path) or "#" in path:
            raise bad_request(f"invalid request path {path!r}",
                              hint="percent-encode spaces, control and non-ASCII characters")
        payload: Optional[bytes] = None
        if body is not None or method in ("POST", "PUT", "PATCH"):
            try:
                payload = json.dumps({} if body is None else body, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")
            except (TypeError, ValueError) as e:
                raise bad_request(f"request body is not JSON-serializable: {e}")
        if timeout is not None:
            try:
                timeout = float(timeout)
            except (TypeError, ValueError):
                raise bad_request("timeout must be a number of seconds")
        sock_timeout = timeout if timeout is not None else self.timeout
        mutating = False
        if method == "POST" and path.split("?", 1)[0] == "/v1/call":
            sock_timeout, mutating = self.call_socket_timeout(body, timeout)
        hdrs = self._headers(headers, payload is not None)
        status, rheaders, data = self._roundtrip(method, self._prefix + path, payload, hdrs, sock_timeout,
                                                 mutating)
        if raw:
            return status, rheaders, data
        return self._parse(status, rheaders, data)

    def open_stream(self, method: str, path: str, body: Optional[bytes] = None, *, headers: Optional[dict] = None,
                    timeout: Optional[float] = None, cancel: Optional[threading.Event] = None
                    ) -> Tuple[int, Dict[str, str], Iterator[bytes]]:
        """One request on a fresh connection whose response body is yielded in chunks as it arrives (WP5b: the hub's
        gateway passthrough, §8.7). Connect-phase failures are retried like ``request`` (D12); ``cancel`` (or closing
        the iterator) closes the connection. Errors after the request was sent: ``unavailable`` reason ``lost`` /
        ``timeout``, both ``maybe_executed=True``."""
        method = str(method or "").upper()
        if not re.match(r"^[A-Z]{3,7}\Z", method):
            raise bad_request(f"invalid HTTP method {method!r}")
        if not isinstance(path, str) or not path.startswith("/") or not _PATH_RE.match(path) or "#" in path:
            raise bad_request(f"invalid request path {path!r}")
        sock_timeout = float(timeout) if timeout is not None else self.timeout
        hdrs = self._headers(headers, body is not None)
        failures = 0
        while True:
            try:
                conn = self._open(sock_timeout)
                break
            except HiveError as e:
                if (e.code == "unavailable" and e.details.get("reason") in _RETRY_REASONS
                        and failures < CONNECT_RETRIES):
                    failures += 1
                    time.sleep(0.2 * failures)
                    continue
                raise
        try:
            conn.sock.settimeout(sock_timeout)
            conn.request(method, self._prefix + path, body=body, headers=hdrs)
            resp = conn.getresponse()
        except socket.timeout:
            conn.close()
            raise timeout_error(f"no response from {self.url} within {sock_timeout:.0f} s", maybe_executed=True)
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise unavailable(f"connection to {self.url} lost: {_errtext(e)}", reason="lost", maybe_executed=True)
        rheaders = {k: v for k, v in resp.getheaders()}

        def chunks() -> Iterator[bytes]:
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        return
                    try:
                        b = resp.read1(READ_CHUNK) if hasattr(resp, "read1") else resp.read(READ_CHUNK)
                    except socket.timeout:
                        raise timeout_error(f"idle timeout reading from {self.url}", maybe_executed=True)
                    except (OSError, http.client.HTTPException, ValueError) as e:
                        raise unavailable(f"connection to {self.url} lost: {_errtext(e)}", reason="lost",
                                          maybe_executed=True)
                    if not b:
                        return
                    yield b
            finally:
                conn.close()
        return resp.status, rheaders, chunks()

    def close(self) -> None:
        """Close every keep-alive connection (the transport stays usable; new requests reconnect)."""
        with self._lock:
            conns = list(self._conns)
            self._conns = weakref.WeakSet()
        for c in conns:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        self._local = threading.local()

    # ------------------------------------------------------------------ request helpers

    def _headers(self, extra: Optional[dict], has_body: bool) -> Dict[str, str]:
        h: Dict[str, str] = {"Accept": "application/json", "X-Hive-Api": "1", "User-Agent": f"hive/{__version__}"}
        if has_body:
            h["Content-Type"] = "application/json"
        for k, v in (extra or {}).items():
            k, v = str(k), str(v)
            if not _HEADER_NAME_RE.match(k) or any(c in v for c in "\r\n\x00"):
                raise bad_request(f"invalid header {k!r}")
            if k.lower() in ("authorization", "host", "content-length", "transfer-encoding", "connection"):
                raise bad_request(f"header {k!r} is set by the transport")
            h[k] = v
        if self._token:
            h["Authorization"] = "Bearer " + self._token
        return h

    def _parse(self, status: int, headers: "_Headers", data: bytes) -> Any:
        api = headers.get("x-hive-api")
        if api is not None and api.strip() != "1":
            raise bad_request("api mismatch", hint=f"{self.url} speaks Hive API {escape_terminal(api[:16])}; "
                                                   "upgrade the older side", api=api[:16])
        try:
            env = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            snippet = escape_terminal(data[:200].decode("utf-8", "replace"))
            raise internal(f"non-JSON response from {self.url} (HTTP {status})",
                           hint="is this URL a Hive node or hub (and not a web page or proxy error)?",
                           status=status, snippet=snippet)
        if not isinstance(env, dict) or not isinstance(env.get("ok"), bool):
            raise internal(f"unexpected response from {self.url} (HTTP {status}): not a Hive envelope",
                           status=status)
        if env["ok"]:
            return env.get("result")
        err = HiveError.from_dict(env.get("error"))
        if err.code == "unauthorized" and not err.hint:
            err.hint = "check the token (client.json token_file, HIVE_TOKEN or --token-file)"
        raise err

    def _roundtrip(self, method: str, path: str, payload: Optional[bytes], hdrs: Dict[str, str],
                   sock_timeout: float, mutating: bool) -> Tuple[int, "_Headers", bytes]:
        connect_failures = 0
        get_retried = False
        idempotent = method in ("GET", "HEAD")
        while True:
            conn = self._take_conn()
            if conn is None:
                try:
                    conn = self._open(sock_timeout)
                except HiveError as e:
                    if (e.code == "unavailable" and e.details.get("reason") in _RETRY_REASONS
                            and connect_failures < CONNECT_RETRIES):
                        connect_failures += 1
                        time.sleep(0.2 * connect_failures)
                        continue
                    if self.offline_hint and e.code == "unavailable" and e.details.get("reason") in _RETRY_REASONS:
                        e.hint = self.offline_hint
                    raise
            too_big = False
            data = b""
            try:
                conn.sock.settimeout(sock_timeout)
                conn.request(method, path, body=payload, headers=hdrs)
                resp = conn.getresponse()
                if resp.length is not None and resp.length > MAX_RESPONSE_BYTES:
                    too_big = True           # refused from Content-Length, before reading the body
                else:
                    data = _read_body(resp)
            except socket.timeout:
                self._discard(conn)
                raise timeout_error(f"no response from {self.url} within {sock_timeout:.0f} s",
                                    maybe_executed=mutating,
                                    hint="the server may still be working on it" if mutating else None)
            except (OSError, http.client.HTTPException) as e:
                self._discard(conn)
                if idempotent and not get_retried:
                    get_retried = True       # §6.5: idempotent GETs reconnect once
                    continue
                raise unavailable(f"connection to {self.url} lost: {_errtext(e)}", reason="lost",
                                  maybe_executed=mutating,
                                  hint=("the call may have run; check before repeating it" if mutating
                                        else "the request can be repeated"))
            if too_big or len(data) > MAX_RESPONSE_BYTES:
                self._discard(conn)
                raise internal(f"response from {self.url} exceeds {MAX_RESPONSE_BYTES} bytes")
            headers = _Headers(resp.getheaders())
            if resp.will_close or conn.sock is None:
                self._discard(conn)
            else:
                conn.hive_last_used = time.monotonic()
                self._local.conn = conn
            return resp.status, headers, data

    def _take_conn(self) -> Optional[_Conn]:
        conn: Optional[_Conn] = getattr(self._local, "conn", None)
        if conn is None:
            return None
        self._local.conn = None
        if conn.sock is None or time.monotonic() - conn.hive_last_used > IDLE_REUSE_MAX or _dropped(conn.sock):
            self._discard(conn)
            return None
        return conn

    def _discard(self, conn: _Conn) -> None:
        if getattr(self._local, "conn", None) is conn:
            self._local.conn = None
        try:
            conn.close()
        except Exception:  # noqa: BLE001 - best effort
            pass

    # ------------------------------------------------------------------ connection establishment

    def proxy_route(self) -> Tuple[Optional[str], Optional[List[str]]]:
        """(proxy URL or None for direct, locally resolved addresses or None) for a new connection (§11.4, §14.4)."""
        if self.proxy == "off":
            return None, resolve_host(self.host, self.port)
        from . import netproxy  # function-level (§11.4: WP5 imports netproxy at function level)
        if self.proxy != "auto":
            # an explicit per-connection proxy URL forces the proxy for every target, also private ones (§14.4), so
            # proxy_for/bypass are not consulted; the URL still goes through §11.4 (strict syntax, v4 `wsl-host`
            # substitution, 60 s cache) and the substituted URL is the one used
            settings = netproxy.load_proxy_settings(None, os.environ, explicit=self.proxy)
            url = settings.https if self.scheme == "https" else settings.http
            # the P0 netproxy stub returns no proxy for anything: keep the (syntax-checked) explicit URL then
            return (url or self.proxy), resolve_host(self.host, self.port)
        hive_proxy = os.environ.get("HIVE_PROXY") or None
        net = {"proxy": hive_proxy, "no_proxy": [], "use_system_proxy": True, "ca_file": None}
        settings = netproxy.load_proxy_settings(net, os.environ)
        if not (settings.https if self.scheme == "https" else settings.http):
            return None, resolve_host(self.host, self.port)
        ips = resolve_host(self.host, self.port)
        return netproxy.proxy_for(settings, self.scheme, self.host, ips or ()), ips

    def _open(self, sock_timeout: float) -> _Conn:
        ctimeout = max(0.1, min(CONNECT_TIMEOUT, sock_timeout))
        proxy, ips = self.proxy_route()
        if proxy is None and not ips:
            # a failed lookup on a direct route is a (retried) connect-phase failure, not a C1 refusal (§6.5)
            raise unavailable(f"cannot resolve {self.host}", reason="dns", maybe_executed=False,
                              hint="check the host name (DNS / MagicDNS) in the URL")
        if self.scheme == "http" and self._token and not plaintext_ok(self.host, ips):
            raise denied(f"refusing to send the token over plain http:// to {self.host}",
                         hint="use https:// (with tls_pin or ca_file), a Tailscale/LAN address, or loopback",
                         rule="plaintext_credentials")
        if proxy is None:
            sock = self._connect_direct(ips, ctimeout)
        else:
            sock = self._connect_proxy(proxy, ctimeout)
        if self.scheme == "https":
            sock = self._tls(sock, ctimeout)
        conn = _Conn(self.host, self.port, sock, 443 if self.scheme == "https" else 80)
        with self._lock:
            self._conns.add(conn)
        return conn

    def _connect_direct(self, ips: Optional[List[str]], ctimeout: float) -> socket.socket:
        if not ips:
            raise unavailable(f"cannot resolve {self.host}", reason="dns", maybe_executed=False,
                              hint="check the host name (DNS / MagicDNS) in the URL")
        last: Optional[BaseException] = None
        for ip in ips:
            try:
                # connect to the address vetted above (rule C1 / proxy decision used the same resolution)
                return socket.create_connection((ip, self.port), timeout=ctimeout)
            except (OSError, socket.timeout) as e:
                last = e
        raise unavailable(f"cannot connect to {self.url}: {_errtext(last)}", reason="connect", maybe_executed=False,
                          hint="is the node/hub running and reachable at this address?")

    def _connect_proxy(self, proxy: str, ctimeout: float) -> socket.socket:
        from . import netproxy  # function-level (§11.4)
        try:
            # always CONNECT (also for http:// targets): the request carries a token (rule C1, never absolute-form)
            return netproxy.open_tunnel(proxy, self.host, self.port, timeout=ctimeout)
        except HiveError:
            raise
        except (OSError, socket.timeout) as e:
            raise unavailable(f"proxy {netproxy.redact_proxy(proxy)} failed: {_errtext(e)}", reason="proxy",
                              maybe_executed=False, hint="is the proxy (Clash/v2rayN) running? check the proxy "
                                                         "setting of this connection")

    def _tls(self, sock: socket.socket, ctimeout: float) -> socket.socket:
        assert self._ssl is not None
        sock.settimeout(ctimeout)
        try:
            ssock = self._ssl.wrap_socket(sock, server_hostname=self.host.strip("[]"))
        except ssl.SSLCertVerificationError as e:
            sock.close()
            raise unavailable(f"TLS certificate of {self.host} is not trusted: {getattr(e, 'verify_message', e)}",
                              reason="connect", maybe_executed=False,
                              hint="for a self-signed certificate pass tls_pin (hive tls-pin CERT.pem) or ca_file")
        except (OSError, socket.timeout, ssl.SSLError) as e:
            sock.close()
            raise unavailable(f"TLS handshake with {self.url} failed: {_errtext(e)}", reason="connect",
                              maybe_executed=False)
        if self._pin:
            # §5.9: compare the pin BEFORE any request byte is written
            der = ssock.getpeercert(binary_form=True) or b""
            got = hashlib.sha256(der).hexdigest()
            if got != self._pin:
                ssock.close()
                raise unavailable(f"TLS pin mismatch for {self.url}", reason="tls_pin", maybe_executed=False,
                                  hint="the server certificate changed or a proxy intercepts TLS; verify it "
                                       "and update tls_pin (hive tls-pin CERT.pem)",
                                  expected="sha256:" + self._pin, got="sha256:" + got)
        return ssock


def _pin_context() -> ssl.SSLContext:
    """TLS context for pinned connections: chain/hostname checks are replaced by the §5.9 pin compare."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE     # §22.3 rule 11 (a): the pin-verification path; the pin is checked in _tls
    return ctx


def _read_body(resp: http.client.HTTPResponse) -> bytes:
    """The response body without a huge ``read(amt)`` (Python < 3.12 allocates a zeroed buffer of ``amt`` bytes).

    A known length (already checked against MAX_RESPONSE_BYTES) is read at once; otherwise (chunked, close-delimited)
    in READ_CHUNK steps until EOF or until MAX_RESPONSE_BYTES is passed (the caller refuses the response then).
    """
    if resp.length is not None:
        return resp.read()
    buf = bytearray()
    while len(buf) <= MAX_RESPONSE_BYTES:
        chunk = resp.read(READ_CHUNK)
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def _dropped(sock: Any) -> bool:
    """True when an idle keep-alive socket was closed by the peer (readable = EOF or unexpected bytes)."""
    try:
        if isinstance(sock, ssl.SSLSocket) and sock.pending():
            return True
        r, _, _ = select.select([sock], [], [], 0)
        return bool(r)
    except (OSError, ValueError):
        return True


def _errtext(e: Optional[BaseException]) -> str:
    if e is None:
        return "unknown error"
    s = str(e) or type(e).__name__
    return escape_terminal(s[:300])


def _redact(url: str) -> str:
    from . import netproxy
    try:
        return netproxy.redact_proxy(url)
    except Exception:  # noqa: BLE001
        return "***"


__all__ = ["HttpTransport", "normalize_pin", "cert_pin", "plaintext_ok", "resolve_host", "MAX_CALL_SECONDS",
           "CALL_MARGIN"]
