"""A small HTTP proxy for tests (design §21.1, WP3 fixture): CONNECT tunnels and absolute-form requests, optional
Basic auth, a request log, and a ``routes`` map that sends a requested ``host:port`` (name or IP) to a local
address, so names like ``public.test`` reach a ``StubHTTPServer`` on 127.0.0.1 without real DNS."""
from __future__ import annotations

import base64
import select
import socket
import threading
from typing import Dict, List, Optional, Tuple


class ConnectProxy:
    """``with ConnectProxy(routes={"public.test:80": ("127.0.0.1", 8123)}) as px: ...`` -> ``px.url``.

    ``log`` gets one dict per request: ``{"kind": "connect"|"absolute", "line", "headers", "target"}`` (headers of
    the proxy request only: a tunnelled request is never visible to the proxy)."""

    def __init__(self, routes: Optional[Dict[str, Tuple[str, int]]] = None, *, auth: Optional[str] = None,
                 refuse_connect: Optional[int] = None):
        self.routes = {k.lower(): v for k, v in (routes or {}).items()}
        self.auth = auth                          # "user:pass" -> 407 without matching Proxy-Authorization
        self.refuse_connect = refuse_connect      # status to answer every CONNECT with (e.g. 403)
        self.log: List[dict] = []
        self._sock: Optional[socket.socket] = None
        self._stop = threading.Event()

    # lifecycle
    def start(self) -> "ConnectProxy":
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(16)
        s.settimeout(0.2)
        self._sock = s
        threading.Thread(target=self._serve, name="connect-proxy", daemon=True).start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()

    def __enter__(self) -> "ConnectProxy":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._sock is not None
        return self._sock.getsockname()[1]

    @property
    def url(self) -> str:
        return f"http://{self.auth + '@' if self.auth else ''}127.0.0.1:{self.port}"

    # serving
    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                c, _ = self._sock.accept()  # type: ignore[union-attr]
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _route(self, hostport: str) -> Tuple[str, int]:
        hp = hostport.lower()
        if hp in self.routes:
            return self.routes[hp]
        host, _, port = hp.rpartition(":")
        return host.strip("[]"), int(port)

    def _read_head(self, c: socket.socket) -> Tuple[bytes, bytes]:
        data = b""
        while b"\r\n\r\n" not in data:
            b = c.recv(65536)
            if not b:
                break
            data += b
            if len(data) > 65536:
                break
        head, _, rest = data.partition(b"\r\n\r\n")
        return head, rest

    def _handle(self, c: socket.socket) -> None:
        c.settimeout(10)
        try:
            head, rest = self._read_head(c)
            if not head:
                return
            lines = head.decode("latin-1").split("\r\n")
            line = lines[0]
            headers = {}
            for h in lines[1:]:
                k, _, v = h.partition(":")
                headers[k.strip().lower()] = v.strip()
            method, target, _ver = (line.split(" ", 2) + ["", ""])[:3]
            kind = "connect" if method.upper() == "CONNECT" else "absolute"
            self.log.append({"kind": kind, "line": line, "headers": dict(headers), "target": target})
            if self.auth is not None:
                want = "Basic " + base64.b64encode(self.auth.encode()).decode()
                if headers.get("proxy-authorization") != want:
                    c.sendall(b"HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n")
                    return
            if kind == "connect":
                if self.refuse_connect:
                    c.sendall(f"HTTP/1.1 {self.refuse_connect} Refused\r\nContent-Length: 0\r\n\r\n".encode())
                    return
                try:
                    up = socket.create_connection(self._route(target), timeout=5)
                except OSError:
                    c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                    return
                c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                if rest:
                    up.sendall(rest)
                self._pipe(c, up)
                return
            # absolute-form: http://host[:port]/path
            if not target.lower().startswith("http://"):
                c.sendall(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
                return
            hostport, _, path = target[7:].partition("/")
            if ":" not in hostport.rsplit("]", 1)[-1]:
                hostport += ":80"
            try:
                up = socket.create_connection(self._route(hostport), timeout=5)
            except OSError:
                c.sendall(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n")
                return
            fwd = [f"{method} /{path} HTTP/1.1"] + [h for h in lines[1:]
                                                    if not h.lower().startswith(("proxy-", "connection"))]
            fwd.append("Connection: close")
            up.sendall(("\r\n".join(fwd) + "\r\n\r\n").encode("latin-1") + rest)
            self._pipe(c, up)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def _pipe(self, a: socket.socket, b: socket.socket) -> None:
        socks = [a, b]
        try:
            while True:
                r, _, _ = select.select(socks, [], [], 10)
                if not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (b if s is a else a).sendall(data)
        except OSError:
            return
        finally:
            b.close()


def simple_open_tunnel(proxy_url: str, target_host: str, target_port: int, *, timeout: float) -> socket.socket:
    """Minimal CONNECT client used by tests to stand in for netproxy.open_tunnel until WP3b (200 -> socket)."""
    from urllib.parse import urlsplit
    p = urlsplit(proxy_url)
    s = socket.create_connection((p.hostname, p.port or 80), timeout=timeout)
    hp = f"[{target_host}]:{target_port}" if ":" in target_host else f"{target_host}:{target_port}"
    s.sendall(f"CONNECT {hp} HTTP/1.1\r\nHost: {hp}\r\n\r\n".encode())
    data = b""
    while b"\r\n\r\n" not in data:
        b = s.recv(1)
        if not b:
            break
        data += b
    if not data.startswith(b"HTTP/1.1 200") and not data.startswith(b"HTTP/1.0 200"):
        s.close()
        from hive.errors import unavailable
        raise unavailable("proxy refused CONNECT", reason="proxy", maybe_executed=False)
    return s


__all__ = ["ConnectProxy", "simple_open_tunnel"]
