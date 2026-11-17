"""ApiServer: bounded ThreadingHTTPServer, TLS, transport checks, router, envelopes, multi-address bind
(design §6.1, §5.7, §5.9, T7, WP1).

Transport rules: ``max_connections`` semaphore (excess sockets get a static 503), TLS wrapped in the worker thread with
a 10 s handshake timeout, headers complete within 10 s (absolute), request line <= 8 KiB, <= 64 headers / 16 KiB,
authentication from the headers BEFORE the body is read (an unauthenticated body is never read), POST needs
Content-Length (chunked -> 411) and ``application/json`` (-> 415), body limit checked before reading (413), slow
bodies (< 64 KiB/s after 10 s) closed, <= 16 pre-auth connections per client IP, Origin/Host checks (T7) decided per
connection with ``getsockname()``, proxied-request detection (§5.7), ``X-Hive-Api``, route modes result/envelope/raw,
``{rest*}`` patterns, per-route ``max_body``/``error_style``/``cors``.
"""
from __future__ import annotations

import http.server
import ipaddress
import json
import logging
import math
import re
import socket
import socketserver
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from .errors import CODES, HiveError, bad_request, forbidden, internal, rate_limited
from .util import IS_WINDOWS, ip_category, is_loopback_host, new_id

if TYPE_CHECKING:  # pragma: no cover
    from .audit import AuditLog
    from .auth import Identity, TokenStore
    from .config import ListenConfig
    from .limits import AuthThrottle

log = logging.getLogger("hive.httpserver")

AUTH_MODES = ("none", "token", "owner", "relay", "models")
ROUTE_MODES = ("result", "envelope", "raw")
API_VERSION = 1
HEADER_DEADLINE = 10.0
IDLE_TIMEOUT = 30.0
MAX_REQUEST_LINE = 8192
MAX_HEADERS = 64
MAX_HEADER_BYTES = 16384
MAX_REQUESTS_PER_CONN = 100
MIN_BODY_RATE = 65536
PREAUTH_PER_IP = 16
MAX_JSON_DEPTH = 64
AGENT_HEADER_RE = re.compile(r"^[A-Za-z0-9._/-]{1,64}\Z")
CORS_HEADERS_RE = re.compile(r"^[A-Za-z0-9_, -]{0,1024}\Z")
FORWARD_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip", "cf-connecting-ip")
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
ID_PARAM_PATTERNS = {
    "id": r"^(ap|gr)_[A-Za-z0-9_-]{6,40}$",
    "job_id": r"^j_[A-Za-z0-9_-]{6,40}$",
    "service": r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,63}$",
    "name": r"^[a-z0-9][a-z0-9_.-]{0,63}$",
    "node": r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
}


class Headers(dict):
    """Case-insensitive header mapping (keys stored lowercase)."""

    def __getitem__(self, key: str) -> str:
        return dict.__getitem__(self, key.lower())

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        """Header value (case-insensitive)."""
        return dict.get(self, key.lower(), default)

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and dict.__contains__(self, key.lower())


def json_depth_ok(data: bytes, limit: int = MAX_JSON_DEPTH) -> bool:
    """Bracket-depth pre-scan (strings skipped); False when nesting exceeds ``limit``."""
    depth = 0
    in_str = False
    esc = False
    for b in data:
        if in_str:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                in_str = False
            continue
        if b == 0x22:
            in_str = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > limit:
                return False
        elif b in (0x5D, 0x7D):
            depth -= 1
    return True


@dataclass
class Request:
    """One HTTP request as seen by a route handler (path params are in ``query`` under their names)."""
    method: str
    path: str
    query: Dict[str, str]
    headers: Mapping[str, str]
    client_ip: str
    body: bytes
    identity: Optional["Identity"]
    proxied: bool = False
    local_ip: str = ""
    request_id: str = ""
    cancel: threading.Event = field(default_factory=threading.Event)

    def json(self) -> Any:
        """Body as JSON; bad_request on invalid JSON or nesting deeper than 64."""
        if not self.body:
            return None
        if not json_depth_ok(self.body):
            raise bad_request("JSON nested too deeply")
        try:
            return json.loads(self.body.decode("utf-8"))
        except RecursionError:
            raise bad_request("JSON nested too deeply")
        except (ValueError, UnicodeDecodeError) as e:
            raise bad_request(f"invalid JSON body: {e}")


@dataclass
class _Route:
    method: str
    pattern: str
    regex: "re.Pattern[str]"
    params: List[str]
    fn: Callable[[Request], Any]
    auth: str
    mode: str
    max_body: Optional[int]
    error_style: str
    cors: bool


class _Reject(Exception):
    def __init__(self, status: int, err: HiveError, *, close: bool = False, headers: Optional[Dict[str, str]] = None):
        super().__init__(err.message)
        self.status = status
        self.err = err
        self.close = close
        self.headers = headers or {}


class _Closed(Exception):
    pass


def _strip_port(host: str) -> str:
    h = (host or "").strip().lower()
    if h.startswith("["):
        return h[1:h.index("]")] if "]" in h else h
    if h.count(":") == 1:
        return h.split(":", 1)[0]
    return h.rstrip(".")


def _run(argv: List[str], timeout: float = 5.0) -> Tuple[int, str]:
    """Run a short probe (argv list, never a shell); (returncode, stdout)."""
    kw: Dict[str, Any] = {}
    if IS_WINDOWS:  # pragma: no cover
        kw["creationflags"] = 0x08000000
    try:
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=timeout,
                           stdin=subprocess.DEVNULL, **kw)
        return p.returncode, p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return 127, ""


def _local_ipv4s() -> List[str]:
    rc, out = _run(["ip", "-4", "-o", "addr", "show"])
    ips: List[str] = []
    if rc == 0:
        for m in re.finditer(r"\binet\s+(\d+\.\d+\.\d+\.\d+)/", out):
            ips.append(m.group(1))
    return ips


def _in_cgnat(ip: str) -> bool:
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def _running_in_wsl() -> bool:
    """Inside WSL2 (``wsl.is_wsl()``, or the kernel release while WP4's module is a stub)."""
    try:
        from . import wsl
        return bool(wsl.is_wsl())
    except Exception:  # noqa: BLE001 - WP4 not merged: own check
        try:
            import platform
            return not IS_WINDOWS and "microsoft" in platform.release().lower()
        except Exception:  # noqa: BLE001
            return False


def tailscale_info() -> Tuple[str, List[str], str]:
    """(tailnet IPv4, own names [MagicDNS name, short name], note) via ``tailscale``; WSL falls back to the Windows
    CLI through interop or the single local 100.64/10 address (§5.9). Raises HiveError when unavailable."""
    import shutil
    from .util import IS_WINDOWS as _win
    cmd = shutil.which("tailscale")
    note = ""
    win_cli = "/mnt/c/Program Files/Tailscale/tailscale.exe"
    via_windows = False
    if cmd is None and not _win:
        import os
        if os.path.exists(win_cli):
            cmd, via_windows = win_cli, True
    ip = ""
    if cmd:
        rc, out = _run([cmd, "ip", "-4"])
        if rc == 0:
            for line in out.split():
                if _in_cgnat(line.strip()):
                    ip = line.strip()
                    break
    if ip and via_windows and ip not in _local_ipv4s():
        raise HiveError("unavailable", f"tailnet address {ip} is not on a local interface (WSL NAT mode?)",
                        hint="in WSL NAT mode use relay mode (hub add-node --relay), or switch WSL to mirrored "
                             "networking", details={"reason": "connect", "maybe_executed": False})
    # §5.9 (v5/v8): the single-local-100.64/10-address fallback exists only for WSL with interop off (no Linux CLI,
    # and the Windows CLI cannot be run). Elsewhere carrier CGNAT and cloud-internal networks also use 100.64/10, so
    # a missing CLI or a failing `tailscale ip -4` is a startup error.
    if not ip and not _win and (cmd is None or via_windows) and _running_in_wsl():
        cands = [a for a in _local_ipv4s() if _in_cgnat(a)]
        if len(cands) == 1:
            ip = cands[0]
            note = f"tailscale.exe unreachable (interop off); using the only tailnet address {ip}"
    if not ip:
        raise HiveError("unavailable", "Tailscale is not available (tailscale ip -4 failed)",
                        hint="start Tailscale and log in (tailscale up), then start Hive again",
                        details={"reason": "connect", "maybe_executed": False})
    names: List[str] = []
    if cmd:
        rc, out = _run([cmd, "status", "--json"])
        if rc == 0:
            try:
                dns = (json.loads(out).get("Self") or {}).get("DNSName") or ""
            except ValueError:
                dns = ""
            dns = dns.rstrip(".").lower()
            if dns:
                names.append(dns)
                names.append(dns.split(".", 1)[0])
    return ip, names, note


class _BoundedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """ThreadingHTTPServer with a shared connection semaphore and no FQDN lookup at bind."""

    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = not IS_WINDOWS

    def __init__(self, addr: Tuple[str, int], api: "ApiServer", *, kind: str):
        fam = socket.AF_INET6 if ":" in addr[0] else socket.AF_INET
        self.address_family = fam
        self.api = api
        self.kind = kind          # "main" | "tailnet" | "proxy"
        super().__init__(addr, _ConnHandler, bind_and_activate=False)
        if fam == socket.AF_INET6:
            try:
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0 if addr[0] == "::" else 1)
            except (OSError, AttributeError):
                pass
        try:
            self.server_bind()
            self.server_activate()
        except BaseException:
            self.server_close()
            raise

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)

    def process_request(self, request: Any, client_address: Any) -> None:
        if not self.api._conn_sem.acquire(blocking=False):
            try:
                body = json.dumps({"ok": False, "error": {"code": "unavailable", "message": "server busy",
                                                          "details": {"reason": "busy", "maybe_executed": False}}})
                request.sendall(("HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                                 f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}").encode())
            except OSError:
                pass
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.api._conn_sem.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.api._conn_sem.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        log.debug("connection error from %s", client_address, exc_info=True)


class _ConnHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _BoundedServer = self.server  # type: ignore[assignment]
        server.api._serve_connection(self.request, self.client_address, server)


class ApiServer:
    """HTTP server for node and hub (§6.1)."""

    def __init__(self, listen: "ListenConfig", *, role: str, tokens: Callable[[], "TokenStore"],
                 relay_tokens: Optional[Callable[[], "TokenStore"]] = None, throttle: "AuthThrottle",
                 logger: logging.Logger, audit: Optional["AuditLog"] = None,
                 owner_check: Optional[Callable[["Identity"], bool]] = None):
        self.listen = listen
        self.role = role
        self.tokens = tokens
        self.relay_tokens = relay_tokens
        self.throttle = throttle
        self.log = logger or log
        self.audit = audit
        self.owner_check = owner_check
        self.node_name = ""
        self._routes: List[_Route] = []
        self._servers: List[_BoundedServer] = []
        self._threads: List[threading.Thread] = []
        self._conn_sem = threading.BoundedSemaphore(max(1, int(getattr(listen, "max_connections", 64) or 64)))
        self._preauth: Dict[str, int] = {}
        self._lock = threading.Lock()
        self._warned: set = set()
        self._ssl: Optional[ssl.SSLContext] = None
        self.tailnet_ip: Optional[str] = None
        self.tailnet_names: List[str] = []
        self.bound: List[Tuple[str, int]] = []
        self.proxy_port: Optional[int] = getattr(listen, "proxy_port", None)
        self.trusted_proxies = [str(x) for x in getattr(listen, "trusted_proxies", None) or []]
        self.allowed_hosts = [_strip_port(h) for h in getattr(listen, "allowed_hosts", None) or []]
        self.allowed_origins = [str(o).rstrip("/").lower() for o in getattr(listen, "allowed_origins", None) or []]
        self.max_body = int(getattr(listen, "max_body_bytes", 50331648) or 50331648)

    # ------------------------------------------------------------------------------------------- routes
    def route(self, method: str, pattern: str, fn: Callable[[Request], Any], *, auth: str = "token",
              mode: str = "result", max_body: Optional[int] = None, error_style: str = "hive",
              cors: bool = False) -> None:
        """Register a route (``{name}`` path params, a final ``{name*}`` matches the rest)."""
        if auth not in AUTH_MODES:
            raise ValueError(f"unknown auth mode {auth!r}")
        if mode not in ROUTE_MODES:
            raise ValueError(f"unknown route mode {mode!r}")
        if error_style not in ("hive", "openai"):
            raise ValueError(f"unknown error style {error_style!r}")
        parts = pattern.strip("/").split("/")
        rx = ""
        params: List[str] = []
        for i, part in enumerate(parts):
            m = re.fullmatch(r"\{([A-Za-z_][A-Za-z0-9_]*)(\*?)\}", part)
            if m:
                params.append(m.group(1))
                if m.group(2):
                    if i != len(parts) - 1:
                        raise ValueError("a {name*} segment must be last")
                    rx += "/(?P<%s>.+)" % m.group(1)
                else:
                    rx += "/(?P<%s>[^/]+)" % m.group(1)
            else:
                rx += "/" + re.escape(part)
        self._routes.append(_Route(method=method.upper(), pattern=pattern, regex=re.compile(rx + r"\Z"),
                                   params=params, fn=fn, auth=auth, mode=mode, max_body=max_body,
                                   error_style=error_style, cors=bool(cors)))

    def _find(self, method: str, path: str) -> Tuple[Optional[_Route], Dict[str, str], bool]:
        path_match = False
        for r in self._routes:
            m = r.regex.match(path)
            if not m:
                continue
            path_match = True
            if r.method == method:
                from urllib.parse import unquote
                return r, {k: unquote(v) for k, v in m.groupdict().items()}, True
        return None, {}, path_match

    # ------------------------------------------------------------------------------------------- lifecycle
    def _bind_plan(self) -> List[Tuple[str, str]]:
        host = str(self.listen.host)
        plan: List[Tuple[str, str]] = []
        if host == "tailscale":
            ip, names, note = tailscale_info()
            self.tailnet_ip, self.tailnet_names = ip, names
            if note:
                self.log.warning("%s", note)
            plan = [(ip, "tailnet"), ("127.0.0.1", "main")]
        elif _in_cgnat(host):
            # §5.9: an explicit tailnet IP is accepted only if `tailscale ip` reports it (carrier CGNAT also uses
            # 100.64/10; on WSL with interop off: the only 100.64/10 address on a local interface)
            try:
                ip, names, note = tailscale_info()
            except HiveError as e:
                raise HiveError("unavailable", f"cannot verify that {host} is this machine's tailnet address: "
                                f"{e.message}", hint=e.hint, details={"reason": "connect", "maybe_executed": False})
            if ip != host:
                raise HiveError("unavailable", f"listen.host {host} is not this machine's tailnet address ({ip})",
                                hint="use listen.host 'tailscale', or the address `tailscale ip -4` prints",
                                details={"reason": "connect", "maybe_executed": False})
            if note:
                self.log.warning("%s", note)
            self.tailnet_ip, self.tailnet_names = ip, names
            plan = [(ip, "tailnet"), ("127.0.0.1", "main")]
        elif host in ("0.0.0.0", "::", ""):
            plan = [(host or "0.0.0.0", "main")]
        elif host == "localhost":
            plan = [("127.0.0.1", "main")]
        else:
            plan = [(host, "main")]
            try:
                if not is_loopback_host(host):
                    plan.append(("127.0.0.1", "main"))
            except Exception:  # noqa: BLE001
                pass
        return plan

    def _tls_context(self) -> Optional[ssl.SSLContext]:
        tls = getattr(self.listen, "tls", None)
        cert, key = (getattr(tls, "cert", None), getattr(tls, "key", None)) if tls else (None, None)
        if not cert or not key:
            return None
        import os
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.options |= ssl.OP_NO_COMPRESSION
        ctx.load_cert_chain(os.path.expanduser(cert), os.path.expanduser(key))
        return ctx

    def start(self) -> List[Tuple[str, int]]:
        """Start listening (one socket per bind address, plus 127.0.0.1:proxy_port); returns bound addresses."""
        plan = self._bind_plan()
        self._ssl = self._tls_context()
        main_host = plan[0][0]
        if not is_loopback_host(main_host) and main_host not in ("localhost",):
            store = self.tokens()
            if len(store) == 0 and (self.relay_tokens is None or len(self.relay_tokens()) == 0):
                raise forbidden(f"refusing to listen on {main_host} with no tokens configured")
            tailnet = plan[0][1] == "tailnet"  # "tailscale" or a verified explicit tailnet IP: WireGuard transport
            if self._ssl is None and not tailnet and not getattr(self.listen, "allow_insecure_remote", False):
                raise forbidden(f"refusing plaintext HTTP on {main_host}",
                                hint="use listen.host 'tailscale', TLS (listen.tls), or allow_insecure_remote")
        port = int(self.listen.port)
        bound: List[Tuple[str, int]] = []
        try:
            for host, kind in plan:
                srv = _BoundedServer((host, port), self, kind=kind)
                port = srv.server_address[1]
                self._servers.append(srv)
                bound.append((host, port))
            if self.proxy_port:
                srv = _BoundedServer(("127.0.0.1", int(self.proxy_port)), self, kind="proxy")
                self._servers.append(srv)
                bound.append(("127.0.0.1", srv.server_address[1]))
                self.proxy_port = srv.server_address[1]
        except OSError as e:
            for s in self._servers:
                s.server_close()
            self._servers = []
            raise HiveError("unavailable", f"cannot listen on {plan[0][0]}:{port}: {e}",
                            hint="another program uses the port, or the address is not on this machine",
                            details={"reason": "connect", "maybe_executed": False})
        for srv in self._servers:
            t = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.2},
                                 name=f"http-{srv.kind}-{srv.server_address[1]}", daemon=True)
            t.start()
            self._threads.append(t)
        self.bound = bound
        return bound

    def stop(self) -> None:
        """Stop the server (listening sockets closed; in-flight connections finish on their own)."""
        for srv in self._servers:
            try:
                srv.shutdown()
            except Exception:  # noqa: BLE001
                pass
            srv.server_close()
        self._servers = []

    # ------------------------------------------------------------------------------------------- connection
    def _preauth_add(self, ip: str, delta: int) -> int:
        with self._lock:
            n = self._preauth.get(ip, 0) + delta
            if n <= 0:
                self._preauth.pop(ip, None)
            else:
                self._preauth[ip] = n
            if len(self._preauth) > 10000:
                self._preauth.clear()
            return max(0, n)

    def _serve_connection(self, sock: socket.socket, addr: Any, server: _BoundedServer) -> None:
        peer = str(addr[0]) if isinstance(addr, tuple) else str(addr)
        if peer.startswith("::ffff:"):
            peer = peer[7:]
        try:
            local = sock.getsockname()
            local_ip = str(local[0])
            if local_ip.startswith("::ffff:"):
                local_ip = local_ip[7:]
        except OSError:
            local_ip = ""
        if self._preauth_add(peer, 1) > PREAUTH_PER_IP:
            self._preauth_add(peer, -1)
            self._static(sock, 503, "too many connections from your address")
            return
        authed = [False]
        try:
            if self._ssl is not None:
                sock.settimeout(10.0)
                try:
                    sock = self._ssl.wrap_socket(sock, server_side=True)
                except (OSError, ssl.SSLError):
                    return
            conn = _Conn(sock)
            for i in range(MAX_REQUESTS_PER_CONN):
                keep = self._one_request(conn, peer, local_ip, server, authed, last=(i == MAX_REQUESTS_PER_CONN - 1))
                if not keep:
                    break
        except _Closed:
            pass
        except Exception:  # noqa: BLE001
            self.log.debug("connection handler failed", exc_info=True)
        finally:
            if not authed[0]:
                self._preauth_add(peer, -1)
            try:
                sock.close()
            except OSError:
                pass

    @staticmethod
    def _static(sock: socket.socket, status: int, msg: str) -> None:
        body = json.dumps({"ok": False, "error": {"code": "unavailable", "message": msg,
                                                  "details": {"reason": "busy", "maybe_executed": False}}})
        try:
            sock.sendall((f"HTTP/1.1 {status} Service Unavailable\r\nContent-Type: application/json\r\n"
                          f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n{body}").encode())
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _warn_once(self, key: str, msg: str, *args: Any) -> bool:
        with self._lock:
            if key in self._warned:
                return False
            self._warned.add(key)
            if len(self._warned) > 10000:
                self._warned.clear()
        self.log.warning(msg, *args)
        return True

    # ------------------------------------------------------------------------------------------- one request
    def _one_request(self, conn: "_Conn", peer: str, local_ip: str, server: _BoundedServer, authed: List[bool],
                     last: bool) -> bool:
        t0 = time.monotonic()
        try:
            head = conn.read_head(idle=IDLE_TIMEOUT, deadline=HEADER_DEADLINE)
        except _Reject as rj:
            self._write_error(conn, rj.status, rj.err, None, "hive", close=True)
            return False
        if head is None:
            return False
        method, target, version, headers = head
        path, _, qs = target.partition("?")
        from urllib.parse import parse_qsl
        query = dict(parse_qsl(qs, keep_blank_values=True))
        rid = new_id("r_")
        route: Optional[_Route] = None
        style = "openai" if path.startswith("/openai/") else "hive"
        ident = None
        client_ip = peer
        proxied = False
        close = last or headers.get("connection", "").lower() == "close" or version == "HTTP/1.0"
        # a request whose body is never read must end the connection (the bytes would parse as the next request)
        unread_body = "transfer-encoding" in headers or headers.get("content-length", "0").strip() not in ("", "0")
        status = 500
        try:
            route, params, path_match = self._find(method, path)
            if route is not None:
                style = route.error_style
            elif path_match:
                raise _Reject(405, HiveError("bad_request", f"method {method} not allowed on {path}", status=405))
            else:
                from .errors import not_found
                raise _Reject(404, not_found(f"no route for {method} {path}"))
            # transport and proxy rules
            client_ip, proxied = self._client(peer, local_ip, server, headers, route)
            api = headers.get("x-hive-api")
            if api is not None and api.strip() != str(API_VERSION):
                raise _Reject(400, bad_request("api mismatch", hint=f"this server speaks API {API_VERSION}"))
            self._check_host(headers, local_ip, server, route, proxied)
            if not route.cors and headers.get("origin") is not None and \
                    headers.get("origin", "").rstrip("/").lower() not in self.allowed_origins:
                raise _Reject(403, forbidden("browser origins not allowed"))
            for k, v in params.items():
                pat = ID_PARAM_PATTERNS.get(k)
                if pat and not re.match(pat, v):
                    raise _Reject(400, bad_request(f"invalid {k} {v[:80]!r}"))
            # CORS preflight
            if method == "OPTIONS" and route.cors and route.auth == "none":
                # a preflight body is never read: end the connection so it cannot parse as a next request
                close = close or unread_body
                req_h = headers.get("access-control-request-headers", "")
                allow_h = req_h if req_h and CORS_HEADERS_RE.match(req_h) else "Authorization, Content-Type"
                self._write(conn, 204, {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": allow_h,
                                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                        "Access-Control-Max-Age": "600", "X-Request-Id": rid}, b"", close=close)
                status = 204
                return not close
            length = self._content_length(method, headers)
            if method == "POST":
                ctype = headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype != "application/json":
                    raise _Reject(415, HiveError("bad_request", "Content-Type must be application/json",
                                                 status=415), close=length > 0)
            ident = self._authenticate(route, headers, client_ip, proxied, length)
            if ident is not None and not authed[0]:
                authed[0] = True
                self._preauth_add(peer, -1)
            if ident is None and length > 0:
                # §6.1: an unauthenticated body is never read (auth "none" routes take no body); close the connection
                # so the unread bytes are not parsed as a next request
                raise _Reject(413, HiveError("too_large", "this route takes no request body", status=413), close=True)
            limit = route.max_body if route.max_body is not None else self.max_body
            if length > limit:
                raise _Reject(413, HiveError("too_large", f"request body larger than {limit} bytes",
                                             hint="send smaller chunks"), close=True)
            body = conn.read_body(length, start=time.monotonic()) if length > 0 else b""
            unread_body = False
            req = Request(method=method, path=path, query=dict(query, **params), headers=headers,
                          client_ip=client_ip, body=body, identity=ident, proxied=proxied, local_ip=local_ip,
                          request_id=rid)
            status = self._dispatch(conn, route, req, close)
            return not close and status != -1
        except _Reject as rj:
            status = rj.status
            done = close or rj.close or unread_body
            self._write_error(conn, rj.status, rj.err, rid, style, close=done, extra=rj.headers,
                              cors=bool(route and route.cors))
            return not done
        except _Closed:
            raise
        finally:
            ms = int((time.monotonic() - t0) * 1000)
            self.log.info("%s %s %s %dms ip=%s id=%s", method, path, status, ms, client_ip,
                          ident.display_name if ident is not None else "-")

    def _content_length(self, method: str, headers: "Headers") -> int:
        te = headers.get("transfer-encoding")
        cls = headers.get("content-length")
        if te is not None:
            raise _Reject(411, HiveError("bad_request", "chunked bodies are not supported; send Content-Length",
                                         status=411), close=True)
        if cls is None:
            if method == "POST":
                raise _Reject(411, HiveError("bad_request", "POST requires Content-Length", status=411), close=True)
            return 0
        if "," in cls or not re.fullmatch(r"\s*\d{1,15}\s*", cls):
            raise _Reject(400, bad_request("invalid or duplicate Content-Length"), close=True)
        return int(cls)

    def _client(self, peer: str, local_ip: str, server: _BoundedServer, headers: "Headers",
                route: _Route) -> Tuple[str, bool]:
        fwd = [h for h in FORWARD_HEADERS if h in headers] + [k for k in headers if k.startswith("tailscale-")]
        xff = headers.get("x-forwarded-for")
        rightmost = xff.split(",")[-1].strip() if xff else ""
        if server.kind == "proxy" or self._trusted_peer(peer):
            return (rightmost if rightmost and self._is_ip(rightmost) else f"proxy:{peer}"), True
        loopback_local = self._is_loopback_ip(local_ip)
        host = _strip_port(headers.get("host", ""))
        proxied = bool(fwd)
        if not proxied and loopback_local and host and host in self.allowed_hosts and host not in LOOPBACK_NAMES:
            proxied = True
        if proxied:
            self._warn_once(f"fwd:{peer}", "forwarded request on the main port from %s; point the proxy at "
                                           "listen.proxy_port", peer)
            return f"proxy:{peer}", True
        return peer, False

    def _trusted_peer(self, peer: str) -> bool:
        for t in self.trusted_proxies:
            try:
                if "/" in t:
                    if ipaddress.ip_address(peer) in ipaddress.ip_network(t, strict=False):
                        return True
                elif ipaddress.ip_address(peer) == ipaddress.ip_address(t):
                    return True
            except ValueError:
                continue
        return False

    @staticmethod
    def _is_ip(s: str) -> bool:
        try:
            ipaddress.ip_address(s.strip("[]"))
            return True
        except ValueError:
            return False

    @staticmethod
    def _is_loopback_ip(ip: str) -> bool:
        try:
            return ip_category(ip) == "loopback"
        except HiveError:
            return False

    def _check_host(self, headers: "Headers", local_ip: str, server: _BoundedServer, route: _Route,
                    proxied: bool) -> None:
        host = _strip_port(headers.get("host", ""))
        bad = _Reject(421, HiveError("bad_request", "unexpected Host", status=421,
                                     hint="add the name to listen.allowed_hosts if a proxy forwards it"))
        if proxied:
            if host not in self.allowed_hosts:
                raise bad
            return
        own = set()
        if self.tailnet_ip and local_ip == self.tailnet_ip:
            own = {self.tailnet_ip} | set(self.tailnet_names)
        if self._is_loopback_ip(local_ip):
            if host in LOOPBACK_NAMES or host in self.allowed_hosts:
                return
            if route.cors and host == "host.docker.internal":
                return
            raise bad
        if host in own:
            return
        if self.allowed_hosts and host not in self.allowed_hosts:
            raise bad

    def _authenticate(self, route: _Route, headers: "Headers", client_ip: str, proxied: bool,
                      length: int) -> Optional["Identity"]:
        if route.auth == "none":
            return None
        from .auth import parse_bearer
        token = parse_bearer(headers.get("authorization"))
        blocked = self.throttle.check(client_ip) if self.throttle is not None else None
        store = (self.relay_tokens() if (route.auth == "relay" and self.relay_tokens is not None) else self.tokens())
        source_ip = client_ip if not proxied or client_ip.startswith("proxy:") else "proxy:" + client_ip
        try:
            ident = store.authenticate(token, source_ip)
        except HiveError as e:
            if self.throttle is not None:
                self.throttle.fail(client_ip)
            if self.audit is not None:
                try:
                    self.audit.record(kind="auth_fail", identity=None, ip=client_ip, via="direct",
                                      node=self.node_name, action=None, params=None, ok=False, code="unauthorized",
                                      ms=0, request_id="", extra={"route": route.pattern, "reason": e.message})
                except HiveError:
                    pass
            if blocked is not None:
                raise _Reject(429, rate_limited("too many failed logins from your address", retry_after=blocked,
                                                reason="auth_throttle"),
                              close=length > 0, headers={"Retry-After": str(int(blocked) + 1)})
            raise _Reject(401, e, close=length > 0, headers={"WWW-Authenticate": "Bearer"})
        if self.throttle is not None:
            self.throttle.ok(client_ip)
        kind = ident.kind
        mode = route.auth
        if mode == "token":
            if kind not in ("owner", "agent", "hub"):
                hint = "gateway tokens only work on /openai/… routes" if kind == "models" else None
                raise _Reject(403, forbidden(f"a {kind} token cannot use this route", hint=hint), close=length > 0)
        elif mode == "owner":
            if kind != "owner" or (self.owner_check is not None and not self.owner_check(ident)):
                raise _Reject(403, forbidden("only the owner may do this, from this machine",
                                             hint="run the hive CLI on the machine itself (owner token, loopback)"),
                              close=length > 0)
        elif mode == "relay":
            if kind != "node":
                raise _Reject(403, forbidden("relay routes need a relay node token"), close=length > 0)
        elif mode == "models":
            ok = kind == "models" or (kind == "hub" and self.role == "node")
            if not ok:
                raise _Reject(403, forbidden("use a gateway token: hive gateway token add", ),
                              close=length > 0)
        return self._hub_headers(ident, headers)

    def _hub_headers(self, ident: "Identity", headers: "Headers") -> "Identity":
        import dataclasses
        agent = headers.get("x-hive-agent")
        gated = headers.get("x-hive-exec-gated")
        if ident.kind != "hub":
            if agent is not None or gated is not None:
                self._warn_once(f"hdr:{ident.name}", "token %s sent X-Hive-Agent/X-Hive-Exec-Gated; ignored (only hub "
                                "tokens may)", ident.name)
                self._audit_warning(ident, "hub-only headers ignored")
            return ident
        changes: Dict[str, Any] = {}
        if agent is not None:
            if AGENT_HEADER_RE.match(agent):
                changes["agent"] = agent
            elif self._warn_once(f"agent:{ident.name}", "invalid X-Hive-Agent from %s dropped", ident.name):
                self._audit_warning(ident, "invalid X-Hive-Agent dropped")
        if gated is not None and gated.strip() == "1":
            changes["exec_gated"] = True
        changes["via"] = "hub"
        return dataclasses.replace(ident, **changes)

    def _audit_warning(self, ident: "Identity", msg: str) -> None:
        if self.audit is None:
            return
        try:
            self.audit.record(kind="warning", identity=ident, ip=ident.source_ip, via=ident.via, node=self.node_name,
                              action=None, params=None, ok=True, code=None, ms=0, request_id="",
                              extra={"warning": msg})
        except HiveError:
            pass

    # ------------------------------------------------------------------------------------------- dispatch
    def _dispatch(self, conn: "_Conn", route: _Route, req: Request, close: bool) -> int:
        extra = {"Access-Control-Allow-Origin": "*"} if route.cors else {}
        try:
            out = route.fn(req)
        except HiveError as e:
            return self._write_error(conn, e.http_status, e, req.request_id, route.error_style, close=close,
                                     extra=self._retry_after(e, extra), cors=route.cors)
        except Exception as e:  # noqa: BLE001
            self.log.error("route %s failed: %s", route.pattern, type(e).__name__, exc_info=True)
            err = internal("internal error")
            return self._write_error(conn, 500, err, req.request_id, route.error_style, close=close, extra=extra,
                                     cors=route.cors)
        if route.mode == "result":
            env = {"ok": True, "result": out, "meta": {"request_id": req.request_id, "api": API_VERSION}}
            return self._write_json(conn, 200, env, req.request_id, close=close, extra=extra)
        if route.mode == "envelope":
            env = out if isinstance(out, dict) else {"ok": False, "error": internal("bad envelope").to_dict()}
            rid = str((env.get("meta") or {}).get("request_id") or req.request_id)
            status = 200
            hdrs = dict(extra)
            if not env.get("ok"):
                err = env.get("error") or {}
                status = CODES.get(str(err.get("code")), 500)
                if err.get("code") == "rate_limited":
                    ra = (err.get("details") or {}).get("retry_after")
                    if isinstance(ra, (int, float)) and not isinstance(ra, bool):
                        hdrs["Retry-After"] = str(max(1, int(math.ceil(ra))))
            return self._write_json(conn, status, env, rid, close=close, extra=hdrs)
        # raw
        try:
            status, headers, body = out
        except (TypeError, ValueError):
            err = internal("bad raw response")
            return self._write_error(conn, 500, err, req.request_id, route.error_style, close=close, extra=extra)
        hdrs = dict(extra)
        hdrs.update({str(k): str(v) for k, v in (headers or {}).items()})
        hdrs.setdefault("X-Request-Id", req.request_id)
        if isinstance(body, (bytes, bytearray)):
            self._write(conn, int(status), hdrs, bytes(body), close=close, default_ct=False)
            return int(status)
        self._write_stream(conn, int(status), hdrs, body, req.cancel)
        return -1

    @staticmethod
    def _retry_after(e: HiveError, extra: Dict[str, str]) -> Dict[str, str]:
        out = dict(extra)
        if e.code == "rate_limited":
            ra = e.details.get("retry_after")
            if isinstance(ra, (int, float)) and not isinstance(ra, bool):
                out["Retry-After"] = str(max(1, int(math.ceil(ra))))
        if e.code == "unauthorized":
            out["WWW-Authenticate"] = "Bearer"
        return out

    def _write_error(self, conn: "_Conn", status: int, err: HiveError, rid: Optional[str], style: str, *,
                     close: bool, extra: Optional[Dict[str, str]] = None, cors: bool = False) -> int:
        rid = rid or new_id("r_")
        hdrs = dict(extra or {})
        if cors:
            hdrs["Access-Control-Allow-Origin"] = "*"
        if style == "openai":
            typ = {"unauthorized": "authentication_error", "forbidden": "permission_error",
                   "not_found": "not_found_error", "rate_limited": "rate_limit_error",
                   "too_large": "invalid_request_error", "bad_request": "invalid_request_error"}.get(err.code,
                                                                                                    "server_error")
            body = {"error": {"message": err.message, "type": typ, "code": err.code}}
        else:
            body = {"ok": False, "error": err.to_dict(), "meta": {"request_id": rid, "api": API_VERSION}}
        return self._write_json(conn, status, body, rid, close=close, extra=hdrs)

    def _write_json(self, conn: "_Conn", status: int, obj: Any, rid: str, *, close: bool,
                    extra: Optional[Dict[str, str]] = None) -> int:
        try:
            data = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError):
            data = json.dumps({"ok": False, "error": {"code": "internal", "message": "unserializable result"}}
                              ).encode("utf-8")
            status = 500
        hdrs = {"Content-Type": "application/json; charset=utf-8", "X-Request-Id": rid}
        hdrs.update(extra or {})
        self._write(conn, status, hdrs, data, close=close)
        return status

    def _base_headers(self) -> List[Tuple[str, str]]:
        return [("Server", "hive"), ("Date", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())),
                ("Cache-Control", "no-store"), ("X-Content-Type-Options", "nosniff"), ("X-Hive-Api", str(API_VERSION))]

    def _write(self, conn: "_Conn", status: int, headers: Dict[str, str], body: bytes, *, close: bool,
               default_ct: bool = True) -> None:
        reason = http.server.BaseHTTPRequestHandler.responses.get(status, ("OK",))[0]
        lines = [f"HTTP/1.1 {status} {reason}"]
        hdrs = self._base_headers()
        if default_ct and not any(k.lower() == "content-type" for k in headers) and body:
            hdrs.append(("Content-Type", "application/json; charset=utf-8"))
        for k, v in headers.items():
            if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
                continue
            hdrs.append((k, v))
        hdrs.append(("Content-Length", str(len(body))))
        hdrs.append(("Connection", "close" if close else "keep-alive"))
        lines += [f"{k}: {v}" for k, v in hdrs]
        conn.send(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1", "replace") + body)

    def _write_stream(self, conn: "_Conn", status: int, headers: Dict[str, str], chunks: Iterable[bytes],
                      cancel: threading.Event) -> None:
        reason = http.server.BaseHTTPRequestHandler.responses.get(status, ("OK",))[0]
        hdrs = self._base_headers() + [(k, v) for k, v in headers.items() if "\n" not in k + v and "\r" not in k + v]
        hdrs.append(("Connection", "close"))
        head = "\r\n".join([f"HTTP/1.1 {status} {reason}"] + [f"{k}: {v}" for k, v in hdrs]) + "\r\n\r\n"
        try:
            conn.send(head.encode("latin-1", "replace"))
            for chunk in chunks:
                if chunk:
                    conn.send(bytes(chunk))
        except (_Closed, OSError):
            cancel.set()
        finally:
            close = getattr(chunks, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass


class _Conn:
    """Buffered socket reader with absolute header deadlines and a minimum body rate."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buf = b""

    def _recv(self, timeout: float) -> bytes:
        if timeout <= 0:
            raise socket.timeout("deadline")
        self.sock.settimeout(timeout)
        data = self.sock.recv(65536)
        return data

    def read_head(self, *, idle: float, deadline: float) -> Optional[Tuple[str, str, str, Headers]]:
        """Request line + headers, or None when the client closed an idle connection."""
        if not self.buf:
            try:
                data = self._recv(idle)
            except (socket.timeout, OSError):
                return None
            if not data:
                return None
            self.buf += data
        end_at = time.monotonic() + deadline
        limit = MAX_REQUEST_LINE + MAX_HEADER_BYTES + 4
        while b"\r\n\r\n" not in self.buf:
            if b"\r\n" not in self.buf and len(self.buf) > MAX_REQUEST_LINE:
                raise _Reject(400, bad_request("request line too long"), close=True)
            if len(self.buf) > limit:
                raise _Reject(400, bad_request("request headers too large"), close=True)
            try:
                data = self._recv(end_at - time.monotonic())
            except (socket.timeout, OSError):
                raise _Closed()
            if not data:
                raise _Closed()
            self.buf += data
        head, _, self.buf = self.buf.partition(b"\r\n\r\n")
        lines = head.split(b"\r\n")
        if len(lines[0]) > MAX_REQUEST_LINE:
            raise _Reject(400, bad_request("request line too long"), close=True)
        if len(head) - len(lines[0]) > MAX_HEADER_BYTES:
            raise _Reject(400, bad_request("request headers too large"), close=True)
        try:
            method, target, version = lines[0].decode("latin-1").split(" ")
        except ValueError:
            raise _Reject(400, bad_request("malformed request line"), close=True)
        if not version.startswith("HTTP/1.") or not re.fullmatch(r"[A-Z]{1,16}", method) or not target.startswith("/"):
            raise _Reject(400, bad_request("malformed request line"), close=True)
        headers = Headers()
        if len(lines) - 1 > MAX_HEADERS:
            raise _Reject(400, bad_request("too many headers"), close=True)
        for raw in lines[1:]:
            if not raw:
                continue
            if raw[:1] in (b" ", b"\t"):
                raise _Reject(400, bad_request("folded headers are not supported"), close=True)
            name, sep, value = raw.decode("latin-1").partition(":")
            if not sep or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
                raise _Reject(400, bad_request("malformed header"), close=True)
            key = name.lower()
            value = value.strip()
            if key in headers:
                if key in ("content-length", "host", "authorization", "transfer-encoding"):
                    raise _Reject(400, bad_request(f"duplicate {name} header"), close=True)
                dict.__setitem__(headers, key, headers.get(key) + ", " + value)
            else:
                dict.__setitem__(headers, key, value)
        return method, target, version, headers

    def read_body(self, length: int, *, start: float) -> bytes:
        """Exactly ``length`` bytes; after 10 s the body must keep >= 64 KiB/s, else the connection is closed."""
        out = bytearray(self.buf[:length])
        self.buf = self.buf[length:]
        while len(out) < length:
            allowed = HEADER_DEADLINE + length / MIN_BODY_RATE
            left = start + allowed - time.monotonic()
            try:
                data = self._recv(left)
            except (socket.timeout, OSError):
                raise _Closed()
            if not data:
                raise _Closed()
            need = length - len(out)
            out += data[:need]
            if len(data) > need:
                self.buf = data[need:]
        return bytes(out)

    def send(self, data: bytes) -> None:
        """sendall with a generous write timeout."""
        try:
            self.sock.settimeout(300)
            self.sock.sendall(data)
        except OSError:
            raise _Closed()


__all__ = ["AUTH_MODES", "ROUTE_MODES", "Request", "ApiServer", "Headers", "json_depth_ok", "tailscale_info"]
