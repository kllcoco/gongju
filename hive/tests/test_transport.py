"""WP5a transport tests (§21.2 relay/hub/client row: transport parts; §15.1, §5.9, §6.5, D12, C1/C3)."""
from __future__ import annotations

import ast
import hashlib
import http.server
import json
import os
import select
import socket
import ssl
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hive import netproxy, transport
from hive.errors import HiveError
from hive.transport import HttpTransport, normalize_pin, plaintext_ok

from tests import helpers

# A self-signed EC certificate for 127.0.0.1 / localhost, valid until 2126 (test fixture only).
TEST_CERT = """-----BEGIN CERTIFICATE-----
MIIBwjCCAWigAwIBAgIUTh1JiWdFiZV8d3TqMmkGyImvKZ4wCgYIKoZIzj0EAwIw
FDESMBAGA1UEAwwJaGl2ZS10ZXN0MCAXDTI2MTExODAwMTIyNFoYDzIxMjYxMDI1
MDAxMjI0WjAUMRIwEAYDVQQDDAloaXZlLXRlc3QwWTATBgcqhkjOPQIBBggqhkjO
PQMBBwNCAASzIfI0GQ3a+zZAO+KZK8c7TekerxLqabv771sEoi7L56xm1RusvZY6
X013Ju7EYrB9YTSfvlaJlwB8gUZsotL2o4GVMIGSMB0GA1UdDgQWBBR2m2TSSk3B
MiL5n9WbCk+03pw67jAfBgNVHSMEGDAWgBR2m2TSSk3BMiL5n9WbCk+03pw67jAa
BgNVHREEEzARhwR/AAABgglsb2NhbGhvc3QwDwYDVR0TAQH/BAUwAwEB/zAOBgNV
HQ8BAf8EBAMCAoQwEwYDVR0lBAwwCgYIKwYBBQUHAwEwCgYIKoZIzj0EAwIDSAAw
RQIgS/1m8KiKlqChMXNlyJXPUqbqV2gONHPD6onk9DLyBrcCIQD8I+of9uhbdAuo
sqx3V7SotMqsM3aA4c568fZrGX48Uw==
-----END CERTIFICATE-----
"""
TEST_KEY = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgBuu1uKQsD/QxRJUV
EAckHoYj+rpe6FVFYy8tt5H2IF2hRANCAASzIfI0GQ3a+zZAO+KZK8c7TekerxLq
abv771sEoi7L56xm1RusvZY6X013Ju7EYrB9YTSfvlaJlwB8gUZsotL2
-----END PRIVATE KEY-----
"""
TEST_PIN = "sha256:" + hashlib.sha256(ssl.PEM_cert_to_DER_cert(TEST_CERT)).hexdigest()
WRONG_PIN = "sha256:" + "ab" * 32
TOKEN = "hv_test_token_value_123"


def ok_env(result: Any) -> Tuple[int, dict, Dict[str, str]]:
    """(status, body, headers) for StubHTTPServer.add."""
    return 200, {"ok": True, "result": result, "meta": {"api": 1}}, {"X-Hive-Api": "1"}


class CertFiles:
    """Writes the test cert/key to a temp dir."""

    def __init__(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="hivetls-"))
        self.cert = self.dir / "cert.pem"
        self.key = self.dir / "key.pem"
        self.cert.write_text(TEST_CERT)
        self.key.write_text(TEST_KEY)

    def cleanup(self) -> None:
        for p in (self.cert, self.key):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            self.dir.rmdir()
        except OSError:
            pass


class TlsStub(helpers.StubHTTPServer):
    """StubHTTPServer over TLS (handshake in the accept thread is fine for tests)."""

    def __init__(self, certs: CertFiles) -> None:
        super().__init__()
        self.certs = certs
        self.handshakes = 0

    def start(self) -> "TlsStub":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.certs.cert), str(self.certs.key))
        stub = self

        class Server(http.server.ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, request, client_address):  # quiet (pin-mismatch clients hang up)
                pass

            def get_request(self):  # type: ignore[override]
                sock, addr = self.socket.accept()
                sock.settimeout(5)
                try:
                    tls = ctx.wrap_socket(sock, server_side=True)
                except (OSError, ssl.SSLError):
                    sock.close()
                    raise
                stub.handshakes += 1
                return tls, addr

        httpd = Server(("127.0.0.1", 0), helpers._StubHandler)
        httpd.stub = self  # type: ignore[attr-defined]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"


class ConnectProxy:
    """Minimal CONNECT proxy recording request lines and the bytes it relays client -> target."""

    def __init__(self) -> None:
        self.lines: List[str] = []
        self.relayed = bytearray()
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while not self._stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c: socket.socket) -> None:
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                d = c.recv(4096)
                if not d:
                    return
                head += d
            line = head.split(b"\r\n", 1)[0].decode()
            self.lines.append(line)
            method, target = line.split()[:2]
            if method != "CONNECT":
                c.sendall(b"HTTP/1.1 405 no\r\nContent-Length: 0\r\n\r\n")
                return
            host, port = target.rsplit(":", 1)
            up = socket.create_connection((host, int(port)), timeout=5)
            c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
            socks = [c, up]
            while True:
                r, _, _ = select.select(socks, [], [], 10)
                if not r:
                    return
                for s in r:
                    d = s.recv(65536)
                    if not d:
                        return
                    if s is c:
                        self.relayed.extend(d)
                        up.sendall(d)
                    else:
                        c.sendall(d)
        except OSError:
            return
        finally:
            c.close()

    def close(self) -> None:
        self._stop = True
        self.sock.close()


def fake_open_tunnel(proxy_url: str, target_host: str, target_port: int, *, timeout: float) -> socket.socket:
    """Test stand-in for netproxy.open_tunnel (WP3b): CONNECT through the proxy, return the raw socket."""
    from urllib.parse import urlsplit
    u = urlsplit(proxy_url)
    s = socket.create_connection((u.hostname, u.port), timeout=timeout)
    s.sendall(f"CONNECT {target_host}:{target_port} HTTP/1.1\r\nHost: {target_host}:{target_port}\r\n\r\n".encode())
    head = b""
    while b"\r\n\r\n" not in head:
        d = s.recv(1)
        if not d:
            raise OSError("proxy closed")
        head += d
    if b" 200 " not in head.split(b"\r\n", 1)[0] + b" ":
        s.close()
        from hive.errors import unavailable
        raise unavailable("proxy refused CONNECT", reason="proxy", maybe_executed=False)
    return s


class RawServer:
    """TCP server whose per-connection behaviour is a function; counts connections and requests read."""

    def __init__(self, behaviour: Any) -> None:
        self.behaviour = behaviour
        self.connections = 0
        self.requests: List[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._run, args=(c,), daemon=True).start()

    def _run(self, c: socket.socket) -> None:
        try:
            self.behaviour(self, c)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass

    def read_request(self, c: socket.socket) -> bytes:
        c.settimeout(5)
        data = b""
        while b"\r\n\r\n" not in data:
            d = c.recv(65536)
            if not d:
                return data
            data += d
        head, rest = data.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n")[1:]:
            k, _, v = line.partition(b":")
            if k.strip().lower() == b"content-length":
                length = int(v.strip())
        while len(rest) < length:
            d = c.recv(65536)
            if not d:
                break
            rest += d
        self.requests.append(head + b"\r\n\r\n" + rest)
        return head + b"\r\n\r\n" + rest

    def close(self) -> None:
        self.sock.close()


def respond(c: socket.socket, obj: Any, extra: str = "") -> None:
    body = json.dumps(obj).encode()
    c.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n{extra}\r\n"
              .encode() + body)


class TestParsing(unittest.TestCase):
    def test_urls(self):
        t = HttpTransport("http://127.0.0.1:7701/")
        self.assertEqual((t.scheme, t.host, t.port, t.url), ("http", "127.0.0.1", 7701, "http://127.0.0.1:7701"))
        t = HttpTransport("https://hub.example.com/base/")
        self.assertEqual((t.port, t._prefix), (443, "/base"))
        t = HttpTransport("http://[::1]:7701")
        self.assertEqual((t.host, t.url), ("::1", "http://[::1]:7701"))
        for bad in ("ftp://x", "http://user:pw@host", "http://h/?q=1", "", "http://", "127.0.0.1:7701",
                    "http://h:99999", "http://h:7701/my hub", "http://h:7701/caf\u00e9", "http://h:7701/a\x7fb"):
            with self.subTest(bad=bad), self.assertRaises(HiveError) as cm:
                HttpTransport(bad)
            self.assertEqual(cm.exception.code, "bad_request")

    def test_pins(self):
        hexd = "AB" * 32
        self.assertEqual(normalize_pin("sha256:" + ":".join(hexd[i:i + 2] for i in range(0, 64, 2))), hexd.lower())
        self.assertEqual(normalize_pin(hexd), hexd.lower())
        for bad in ("sha256:abc", "md5:" + "ab" * 32, "", "zz" * 32):
            with self.assertRaises(HiveError):
                normalize_pin(bad)
        with self.assertRaises(HiveError) as cm:
            HttpTransport("http://127.0.0.1:1", tls_pin=TEST_PIN)
        self.assertIn("https", cm.exception.message)
        t = HttpTransport("https://127.0.0.1:1", tls_pin=TEST_PIN.upper().replace("SHA256", "sha256"))
        self.assertEqual(t.tls_pin, TEST_PIN)

    def test_token_and_options(self):
        for bad in ("hv_ a", "hv_x\r\nX: y", ""):
            with self.assertRaises(HiveError):
                HttpTransport("http://127.0.0.1:1", bad)
        for bad in ("socks5://127.0.0.1:1080", "https://127.0.0.1:8443", "pac", "http://127.0.0.1",
                    "http://127.0.0.1:7890/path", "http://127.0.0.1:7890/?x=1", "http://127.0.0.1:7890#f"):
            with self.subTest(proxy=bad), self.assertRaises(HiveError) as cm:
                HttpTransport("http://127.0.0.1:1", proxy=bad)
            self.assertEqual(cm.exception.code, "bad_request")
        self.assertEqual(HttpTransport("http://127.0.0.1:1", proxy="OFF").proxy, "off")
        for good in ("http://127.0.0.1:7890", "http://127.0.0.1:7890/", "http://u:p@wsl-host:7890"):
            self.assertEqual(HttpTransport("http://127.0.0.1:1", proxy=good).proxy, good)
        self.assertEqual(HttpTransport("http://127.0.0.1:1", proxy=None).proxy, "auto")
        with self.assertRaises(HiveError):
            HttpTransport("http://127.0.0.1:1", timeout=0)
        t = HttpTransport("http://127.0.0.1:1", TOKEN)
        self.assertNotIn(TOKEN, repr(t))
        self.assertTrue(t.has_token)
        with self.assertRaises(HiveError):
            t._headers({"Authorization": "Bearer x"}, False)
        with self.assertRaises(HiveError):
            t._headers({"X-Hive-Agent": "a\r\nb"}, False)
        h = t._headers({"X-Hive-Agent": "gw/app"}, True)
        self.assertEqual(h["Authorization"], "Bearer " + TOKEN)
        self.assertEqual((h["X-Hive-Api"], h["Content-Type"], h["X-Hive-Agent"]), ("1", "application/json", "gw/app"))

    def test_call_socket_timeouts(self):
        t = HttpTransport("http://127.0.0.1:1")
        # §21.2: model.chat without timeout -> 15 + 120 + 25 = 160
        self.assertEqual(t.call_socket_timeout({"action": "model.chat", "params": {"prompt": "hi"}}), (160.0, True))
        self.assertEqual(t.call_socket_timeout({"action": "shell.run", "params": {"command": "x", "timeout": 200}}),
                         (240.0, True))
        # body timeout wins; clamped to max_call_seconds + 25
        self.assertEqual(t.call_socket_timeout({"action": "shell.run", "params": {}, "timeout": 99999})[0], 3725.0)
        # the constructor timeout is the minimum
        self.assertEqual(t.call_socket_timeout({"action": "node.info", "params": {}})[0], 120.0)
        # unknown action -> 30 (+25), mutating assumed (fail safe)
        self.assertEqual(HttpTransport("http://127.0.0.1:1", timeout=5).call_socket_timeout({"action": "x.y"}),
                         (55.0, True))
        self.assertEqual(t.call_socket_timeout({"action": "fs.read", "params": {"path": "a"}})[1], False)

    def test_plaintext_rule(self):
        self.assertTrue(plaintext_ok("127.0.0.1", None))
        self.assertTrue(plaintext_ok("localhost", None))
        self.assertTrue(plaintext_ok("192.168.1.5", None))
        self.assertTrue(plaintext_ok("100.101.1.5", None))       # tailnet (CGNAT)
        self.assertFalse(plaintext_ok("93.184.216.34", None))
        self.assertFalse(plaintext_ok("198.18.0.7", None))        # fake-ip leaves through a proxy TUN
        self.assertTrue(plaintext_ok("home-hub.tail1234.ts.net", ["100.64.0.9"]))
        self.assertTrue(plaintext_ok("home-hub.tail1234.ts.net", None))
        self.assertTrue(plaintext_ok("nas", None))
        self.assertFalse(plaintext_ok("hub.example.com", ["93.184.216.34"]))
        self.assertFalse(plaintext_ok("hub.example.com", ["192.168.1.2", "93.184.216.34"]))
        self.assertFalse(plaintext_ok("hub.example.com", ["198.18.0.3"]))
        self.assertFalse(plaintext_ok("hub.example.com", None))
        # link-local addresses next to a private one (mDNS hosts publish fe80::) do not count; alone they do not pass
        self.assertTrue(plaintext_ok("desk.local", ["fe80::1", "192.168.1.5"]))
        self.assertTrue(plaintext_ok("mypc.local", ["169.254.7.7", "10.0.0.5"]))
        self.assertFalse(plaintext_ok("desk.local", ["fe80::1"]))
        self.assertFalse(plaintext_ok("hub.example.com", ["fe80::1", "93.184.216.34"]))
        self.assertFalse(plaintext_ok("hub.example.com", ["169.254.169.254", "192.168.1.5"]))   # metadata != link-local

    def test_cert_none_only_on_pin_path(self):
        # §22.3 rule 11 (a): CERT_NONE in transport.py only inside _pin_context (the pin-verification path)
        src = Path(transport.__file__).read_text(encoding="utf-8")
        fn = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef) and n.name == "_pin_context"][0]
        lines = [i + 1 for i, line in enumerate(src.splitlines()) if "CERT_NONE" in line]
        self.assertTrue(lines)
        for ln in lines:
            self.assertTrue(fn.lineno <= ln <= fn.end_lineno, ln)


class TestHttpAgainstNode(unittest.TestCase):
    """Real node server over loopback (WP1 ApiServer + NodeRuntime)."""

    @classmethod
    def setUpClass(cls):
        cls.url, cls.token, cls.rt = helpers.start_node_server()

    @classmethod
    def tearDownClass(cls):
        cls.rt.test_stop()

    def test_envelopes_and_keepalive(self):
        t = HttpTransport(self.url, self.token)
        try:
            who = t.request("GET", "/v1/whoami")
            self.assertEqual(who["server"]["role"], "node")
            self.assertEqual(who["identity"]["name"], "test-agent")
            conn1 = t._local.conn
            info = t.request("POST", "/v1/call", {"action": "node.info", "params": {}, "request_id": "r_t1"})
            self.assertEqual(info["name"], self.rt.name)
            self.assertIs(t._local.conn, conn1)           # keep-alive: same connection
            status, headers, body = t.request("GET", "/v1/health", raw=True)
            self.assertEqual(status, 200)
            self.assertEqual(headers["x-hive-api"], "1")
            self.assertEqual(headers.get("X-Hive-Api"), "1")
            self.assertTrue(json.loads(body)["ok"])
            with self.assertRaises(HiveError) as cm:
                t.request("POST", "/v1/call", {"action": "no.such", "params": {}})
            self.assertEqual(cm.exception.code, "not_found")
        finally:
            t.close()
        self.assertIsNone(getattr(t._local, "conn", None))

    def test_unauthorized(self):
        t = HttpTransport(self.url, "hv_wrong")
        with self.assertRaises(HiveError) as cm:
            t.request("GET", "/v1/whoami")
        self.assertEqual(cm.exception.code, "unauthorized")
        self.assertTrue(cm.exception.hint)
        t.close()

    def test_threads_get_own_connections(self):
        t = HttpTransport(self.url, self.token)
        conns: List[Any] = []
        lock = threading.Lock()

        def work():
            for _ in range(3):
                t.request("GET", "/v1/whoami")
            with lock:
                conns.append(t._local.conn)
        ths = [threading.Thread(target=work) for _ in range(4)]
        for th in ths:
            th.start()
        for th in ths:
            th.join(30)
        self.assertEqual(len(conns), 4)
        self.assertEqual(len({id(c) for c in conns}), 4)
        t.close()


class TestFailureModes(unittest.TestCase):
    def test_connect_refused_retries_then_unavailable(self):
        port = helpers.free_port()
        t = HttpTransport(f"http://127.0.0.1:{port}", TOKEN)
        real = socket.create_connection
        calls = []

        def counting(*a, **kw):
            calls.append(a)
            return real(*a, **kw)
        with mock.patch.object(transport.socket, "create_connection", counting):
            with self.assertRaises(HiveError) as cm:
                t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "x"}})
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "connect", False))
        self.assertEqual(len(calls), 3)    # 1 + 2 retries (D12)
        self.assertFalse(e.retryable is None)

    def test_retry_after_refused_connect_succeeds(self):
        srv = RawServer(lambda s, c: (s.read_request(c), respond(c, {"ok": True, "result": {"n": 1}})))
        real = socket.create_connection
        calls = []

        def flaky(addr, *a, **kw):
            calls.append(addr)
            if len(calls) == 1:
                raise ConnectionRefusedError(111, "Connection refused")
            return real(addr, *a, **kw)
        t = HttpTransport(srv.url, TOKEN)
        with mock.patch.object(transport.socket, "create_connection", flaky):
            self.assertEqual(t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "x"}}),
                             {"n": 1})
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(srv.requests), 1)
        t.close()
        srv.close()

    def test_dns_failure(self):
        t = HttpTransport("http://no-such-host.invalid:7701")
        with mock.patch.object(transport, "resolve_host", return_value=None):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual((cm.exception.details["reason"], cm.exception.details["maybe_executed"]), ("dns", False))

    def test_dns_failure_with_token_is_not_a_c1_refusal(self):
        # split-horizon name, temporary DNS failure: unavailable reason dns (retried), not policy_denied
        t = HttpTransport("http://nas.home.example.com:7701", TOKEN)
        with mock.patch.object(transport, "resolve_host", return_value=None) as rh, \
                mock.patch.object(transport.time, "sleep"):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "dns", False))
        self.assertTrue(e.retryable)
        self.assertEqual(rh.call_count, 3)          # 1 + 2 retries
        # a name that resolves to a public address is still refused by C1
        with mock.patch.object(transport, "resolve_host", return_value=["93.184.216.34"]):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual((cm.exception.code, cm.exception.details["rule"]), ("policy_denied", "plaintext_credentials"))

    def test_invalid_request_paths_send_nothing(self):
        t = HttpTransport("http://127.0.0.1:1", TOKEN)
        with mock.patch.object(t, "_open", side_effect=AssertionError("no connection expected")):
            for bad in ("/v1/a b", "/v1/caf\u00e9", "/v1/a\x00", "/v1/a\x7f", "v1/x", "/v1/x#f", "/v1/\r\nX: y"):
                with self.subTest(path=bad), self.assertRaises(HiveError) as cm:
                    t.request("GET", bad)
                self.assertEqual(cm.exception.code, "bad_request")

    def test_large_responses(self):
        # Content-Length above the limit: refused from the header, the body is never read
        def huge(s, c):
            s.read_request(c)
            c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                      + str(transport.MAX_RESPONSE_BYTES + 1).encode() + b"\r\n\r\n{")
            time.sleep(2)
        srv = RawServer(huge)
        t = HttpTransport(srv.url, TOKEN, timeout=10)
        t0 = time.monotonic()
        with mock.patch.object(transport.http.client.HTTPResponse, "read",
                               side_effect=AssertionError("body must not be read")):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/nodes")
        self.assertEqual(cm.exception.code, "internal")
        self.assertIn("exceeds", cm.exception.message)
        self.assertLess(time.monotonic() - t0, 1.5)
        srv.close()

        # chunked bodies are read in READ_CHUNK steps (never read(MAX_RESPONSE_BYTES + 1)) and capped
        def chunked(s, c):
            while s.read_request(c):
                body = json.dumps({"ok": True, "result": {"x": "y" * 3000}}).encode()
                parts = [body[i:i + 1000] for i in range(0, len(body), 1000)]
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                          + b"".join(b"%x\r\n%s\r\n" % (len(x), x) for x in parts) + b"0\r\n\r\n")
        srv = RawServer(chunked)
        t = HttpTransport(srv.url, TOKEN)
        amounts: List[Any] = []
        real_read = transport.http.client.HTTPResponse.read

        def spy(self, amt=None):
            amounts.append(amt)
            return real_read(self, amt)
        with mock.patch.object(transport.http.client.HTTPResponse, "read", spy):
            self.assertEqual(len(t.request("GET", "/v1/nodes")["x"]), 3000)
            self.assertTrue(amounts)
            self.assertTrue(all(a is not None and a <= transport.READ_CHUNK for a in amounts), amounts)
            with mock.patch.object(transport, "MAX_RESPONSE_BYTES", 1500), \
                    mock.patch.object(transport, "READ_CHUNK", 512):
                with self.assertRaises(HiveError) as cm:
                    t.request("GET", "/v1/nodes")
            self.assertIn("exceeds", cm.exception.message)
        t.close()
        srv.close()

    def test_lost_after_send_is_not_retried(self):
        # the server reads the whole request and closes without answering
        srv = RawServer(lambda s, c: s.read_request(c))
        t = HttpTransport(srv.url, TOKEN)
        with self.assertRaises(HiveError) as cm:
            t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "touch x"}})
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "lost", True))
        self.assertFalse(e.retryable)
        self.assertEqual(len(srv.requests), 1)      # no transport retry after request bytes were sent
        # a read action: maybe_executed false (the caller may retry it)
        with self.assertRaises(HiveError) as cm:
            t.request("POST", "/v1/call", {"action": "fs.read", "params": {"path": "a"}})
        self.assertEqual(cm.exception.details["maybe_executed"], False)
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(len(srv.requests), 2)
        # an idempotent GET reconnects once
        with self.assertRaises(HiveError):
            t.request("GET", "/v1/nodes")
        self.assertEqual(len(srv.requests), 4)
        srv.close()

    def test_reset_during_send_is_lost(self):
        def reset(s, c):
            c.recv(10)
            # SO_LINGER on with timeout 0: close() sends an RST (struct linger: two u_short on Windows, two int else)
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH" if os.name == "nt" else "ii", 1, 0))
            c.close()
        srv = RawServer(reset)
        t = HttpTransport(srv.url, TOKEN)
        big = {"action": "fs.write", "params": {"path": "a", "content": "x" * (4 << 20)}}
        with self.assertRaises(HiveError) as cm:
            t.request("POST", "/v1/call", big)
        self.assertEqual((cm.exception.details["reason"], cm.exception.details["maybe_executed"]), ("lost", True))
        self.assertEqual(srv.connections, 1)
        srv.close()

    def test_timeouts(self):
        def silent(s, c):
            s.read_request(c)
            time.sleep(3)
        srv = RawServer(silent)
        t = HttpTransport(srv.url, TOKEN, timeout=0.5)
        with self.assertRaises(HiveError) as cm:
            t.request("GET", "/v1/nodes")
        self.assertEqual((cm.exception.code, cm.exception.details["maybe_executed"]), ("timeout", False))
        with mock.patch.object(HttpTransport, "call_socket_timeout", return_value=(0.5, True)):
            with self.assertRaises(HiveError) as cm:
                t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "x"}})
        self.assertEqual((cm.exception.code, cm.exception.details["maybe_executed"]), ("timeout", True))
        self.assertEqual(len(srv.requests), 2)
        srv.close()

    def test_long_call_uses_call_deadline(self):
        # §21.2 "Client.call shell.run timeout=200 against a stub answering after 150 s" (scaled): the constructor
        # timeout (0.5 s) does not apply to /v1/call; deadline + 25 does
        with helpers.StubHTTPServer() as stub:
            stub.add("POST", "/v1/call", *ok_env({"exit_code": 0, "state": "exited"}), delay=1.5)
            t = HttpTransport(stub.url, TOKEN, timeout=0.5)
            r = t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "x", "timeout": 200}})
            self.assertEqual(r["exit_code"], 0)
            t.close()

    def test_bad_responses(self):
        with helpers.StubHTTPServer() as stub:
            stub.add("GET", "/v1/html", 502, "<html>bad gateway</html>")
            stub.add("GET", "/v1/notenv", 200, {"hello": 1})
            stub.add("GET", "/v1/api2", 200, {"ok": True, "result": {}}, headers={"X-Hive-Api": "2"})
            stub.add("GET", "/v1/err", 403, {"ok": False, "error": {"code": "policy_denied", "message": "no",
                                                                     "details": {"rule": "x"}}})
            stub.add("GET", "/v1/masked", *ok_env({"data": "x", "masked": True}))
            t = HttpTransport(stub.url, TOKEN)
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/html")
            self.assertEqual((cm.exception.code, cm.exception.details["status"]), ("internal", 502))
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/notenv")
            self.assertEqual(cm.exception.code, "internal")
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/api2")
            self.assertEqual((cm.exception.code, cm.exception.message), ("bad_request", "api mismatch"))
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/err")
            self.assertEqual((cm.exception.code, cm.exception.details), ("policy_denied", {"rule": "x"}))
            self.assertIs(t.request("GET", "/v1/masked")["masked"], True)   # only the result reaches callers
            # headers: token as Bearer, never in the URL
            req = stub.requests[0]
            self.assertEqual(req["headers"]["Authorization"], "Bearer " + TOKEN)
            self.assertNotIn(TOKEN, req["full_path"])
            t.close()

    def test_post_sends_json_body(self):
        with helpers.StubHTTPServer() as stub:
            stub.add("POST", "/v1/cancel", *ok_env({"cancelled": True}))
            t = HttpTransport(stub.url, TOKEN)
            self.assertEqual(t.request("POST", "/v1/cancel", {"request_id": "r_1"}), {"cancelled": True})
            self.assertEqual(t.request("POST", "/v1/cancel"), {"cancelled": True})
            self.assertEqual(json.loads(stub.requests[0]["body"]), {"request_id": "r_1"})
            self.assertEqual(stub.requests[1]["body"], b"{}")
            self.assertEqual(stub.requests[0]["headers"]["Content-Type"], "application/json")
            t.close()

    def test_stale_keepalive_reconnects(self):
        # the server answers keep-alive and closes the idle connection shortly after (like the 30 s idle close)
        def close_after(s, c):
            s.read_request(c)
            respond(c, {"ok": True, "result": {"n": s.connections}})
            time.sleep(0.1)
        srv = RawServer(close_after)
        t = HttpTransport(srv.url, TOKEN)
        self.assertEqual(t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "a"}}),
                         {"n": 1})
        time.sleep(0.4)
        self.assertEqual(t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "b"}}),
                         {"n": 2})
        self.assertEqual(len(srv.requests), 2)
        t.close()
        srv.close()

    def test_offline_hint(self):
        t = HttpTransport(f"http://127.0.0.1:{helpers.free_port()}", TOKEN)
        t.offline_hint = "start it: hive node run"
        with mock.patch.object(transport.time, "sleep"):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual(cm.exception.hint, "start it: hive node run")


class TestPlaintextCredentials(unittest.TestCase):
    def test_token_never_sent_in_plaintext_to_public(self):
        t = HttpTransport("http://93.184.216.34:7700", TOKEN)
        with mock.patch.object(transport.socket, "create_connection",
                               side_effect=AssertionError("must not connect")):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual((cm.exception.code, cm.exception.details.get("rule")),
                         ("policy_denied", "plaintext_credentials"))
        # a name resolving to a public address is refused too; to a private one allowed
        t = HttpTransport("http://hub.example.com:7700", TOKEN)
        with mock.patch.object(transport, "resolve_host", return_value=["93.184.216.34"]), \
                mock.patch.object(transport.socket, "create_connection",
                                  side_effect=AssertionError("must not connect")):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual(cm.exception.details.get("rule"), "plaintext_credentials")
        with mock.patch.object(transport, "resolve_host", return_value=["10.0.0.9"]), \
                mock.patch.object(transport.socket, "create_connection",
                                  side_effect=ConnectionRefusedError(111, "refused")) as cc, \
                mock.patch.object(transport.time, "sleep"):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual(cm.exception.details.get("reason"), "connect")
        # connects to the vetted address, not a re-resolved name
        self.assertEqual(cc.call_args[0][0], ("10.0.0.9", 7700))

    def test_no_token_public_http_allowed(self):
        t = HttpTransport("http://93.184.216.34:7700")
        with mock.patch.object(transport.socket, "create_connection",
                               side_effect=ConnectionRefusedError(111, "refused")), \
                mock.patch.object(transport.time, "sleep"):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/health")
        self.assertEqual(cm.exception.details.get("reason"), "connect")


class TestTls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certs = CertFiles()

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def stub(self) -> TlsStub:
        s = TlsStub(self.certs).start()
        s.add("GET", "/v1/whoami", *ok_env({"server": {"role": "node"}}))
        s.add("POST", "/v1/call", *ok_env({"ok": 1}))
        self.addCleanup(s.stop)
        return s

    def test_pin_pass(self):
        s = self.stub()
        t = HttpTransport(s.url, TOKEN, tls_pin=TEST_PIN)
        self.assertEqual(t.request("GET", "/v1/whoami"), {"server": {"role": "node"}})
        self.assertEqual(s.requests[0]["headers"]["Authorization"], "Bearer " + TOKEN)
        t.close()

    def test_pin_mismatch_sends_nothing(self):
        s = self.stub()
        t = HttpTransport(s.url, TOKEN, tls_pin=WRONG_PIN)
        with self.assertRaises(HiveError) as cm:
            t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "x"}})
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "tls_pin", False))
        self.assertFalse(e.retryable)
        self.assertEqual(e.details["got"], TEST_PIN)
        time.sleep(0.2)
        self.assertEqual(s.requests, [])
        self.assertEqual(s.handshakes, 1)          # a pin mismatch is never retried

    def test_ca_file_and_untrusted(self):
        s = self.stub()
        t = HttpTransport(s.url, TOKEN, ca_file=str(self.certs.cert))
        self.assertEqual(t.request("GET", "/v1/whoami")["server"]["role"], "node")
        t.close()
        t = HttpTransport(s.url, TOKEN)            # system CAs: the self-signed cert is not trusted
        with mock.patch.object(transport.time, "sleep"):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual((cm.exception.details["reason"], cm.exception.details["maybe_executed"]), ("connect", False))
        self.assertIn("tls_pin", cm.exception.hint)
        self.assertEqual(len(s.requests), 1)       # only the ca_file request arrived
        with self.assertRaises(HiveError):
            HttpTransport(s.url, ca_file=str(self.certs.dir / "missing.pem"))

    def test_explicit_proxy_tunnel_with_pin(self):
        # explicit connection proxy URL is used even for a loopback/private target; TLS and pin end-to-end (C3)
        s = self.stub()
        proxy = ConnectProxy()
        self.addCleanup(proxy.close)
        with mock.patch.object(netproxy, "open_tunnel", fake_open_tunnel):
            t = HttpTransport(s.url, TOKEN, tls_pin=TEST_PIN, proxy=proxy.url)
            self.assertEqual(t.request("GET", "/v1/whoami")["server"]["role"], "node")
            t.close()
            self.assertEqual(proxy.lines, [f"CONNECT 127.0.0.1:{s.port} HTTP/1.1"])
            self.assertNotIn(TOKEN.encode(), bytes(proxy.relayed))     # TLS inside the tunnel
            # wrong pin through the proxy: fails before the token is sent
            proxy.relayed.clear()
            t = HttpTransport(s.url, TOKEN, tls_pin=WRONG_PIN, proxy=proxy.url)
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
            self.assertEqual(cm.exception.details["reason"], "tls_pin")
            self.assertNotIn(TOKEN.encode(), bytes(proxy.relayed))
        self.assertEqual(len(s.requests), 1)

    def test_explicit_proxy_resolved_through_netproxy(self):
        # §15.1/§11.4: an explicit URL goes through load_proxy_settings(explicit=…) (v4 wsl-host substitution); the
        # substituted URL reaches open_tunnel, and proxy_for/bypass are not consulted (explicit forces the proxy)
        proxy = ConnectProxy()
        self.addCleanup(proxy.close)
        tunnels: List[str] = []

        def tunnel(proxy_url, host, port, *, timeout):
            tunnels.append(proxy_url)
            return fake_open_tunnel(proxy_url, host, port, timeout=timeout)
        settings = netproxy.ProxySettings(proxy.url, proxy.url, ("localhost",), "config")
        with helpers.StubHTTPServer() as stub, \
                mock.patch.object(netproxy, "load_proxy_settings", return_value=settings) as lps, \
                mock.patch.object(netproxy, "proxy_for", side_effect=AssertionError("not consulted")), \
                mock.patch.object(netproxy, "open_tunnel", tunnel):
            stub.add("GET", "/v1/whoami", *ok_env({"x": 3}))
            t = HttpTransport(stub.url, TOKEN, proxy="http://wsl-host:7890")
            self.assertEqual(t.request("GET", "/v1/whoami"), {"x": 3})
            t.close()
        args, kwargs = lps.call_args
        self.assertEqual(kwargs.get("explicit"), "http://wsl-host:7890")
        self.assertEqual(tunnels, [proxy.url])
        self.assertEqual(len(proxy.lines), 1)

    def test_http_through_proxy_uses_connect(self):
        proxy = ConnectProxy()
        self.addCleanup(proxy.close)
        with helpers.StubHTTPServer() as stub, mock.patch.object(netproxy, "open_tunnel", fake_open_tunnel):
            stub.add("GET", "/v1/whoami", *ok_env({"x": 1}))
            port = stub.port
            t = HttpTransport(stub.url, TOKEN, proxy=proxy.url)
            self.assertEqual(t.request("GET", "/v1/whoami"), {"x": 1})
            t.close()
        self.assertEqual(proxy.lines, [f"CONNECT 127.0.0.1:{port} HTTP/1.1"])   # never absolute-form


class TestAutoProxy(unittest.TestCase):
    def settings(self, url: str) -> netproxy.ProxySettings:
        return netproxy.ProxySettings(url, url, ("localhost",), "env")

    def test_auto_passes_resolved_ips_and_bypasses(self):
        seen = []

        def proxy_for(settings, scheme, host, ips=()):
            seen.append((scheme, host, list(ips)))
            return None
        with helpers.StubHTTPServer() as stub:
            stub.add("GET", "/v1/whoami", *ok_env({"x": 1}))
            with mock.patch.object(netproxy, "load_proxy_settings",
                                   return_value=self.settings("http://127.0.0.1:9")) as lps, \
                    mock.patch.object(netproxy, "proxy_for", proxy_for), \
                    mock.patch.dict(os.environ, {"HIVE_PROXY": "http://127.0.0.1:9"}):
                t = HttpTransport(stub.url.replace("127.0.0.1", "localhost"), TOKEN)
                self.assertEqual(t.request("GET", "/v1/whoami"), {"x": 1})
                t.close()
        self.assertEqual(seen[0][:2], ("http", "localhost"))
        self.assertIn("127.0.0.1", seen[0][2])
        # HIVE_PROXY replaces the proxy source like a configured proxy (bypass rules still apply), not explicit
        args, kwargs = lps.call_args
        self.assertEqual(args[0]["proxy"], "http://127.0.0.1:9")
        self.assertIsNone(kwargs.get("explicit"))

    def test_auto_through_proxy(self):
        proxy = ConnectProxy()
        self.addCleanup(proxy.close)
        with helpers.StubHTTPServer() as stub:
            stub.add("GET", "/v1/whoami", *ok_env({"x": 2}))
            with mock.patch.object(netproxy, "load_proxy_settings", return_value=self.settings(proxy.url)), \
                    mock.patch.object(netproxy, "proxy_for", lambda s, scheme, host, ips=(): proxy.url), \
                    mock.patch.object(netproxy, "open_tunnel", fake_open_tunnel):
                t = HttpTransport(stub.url, TOKEN)
                self.assertEqual(t.request("GET", "/v1/whoami"), {"x": 2})
                t.close()
        self.assertEqual(len(proxy.lines), 1)
        self.assertTrue(proxy.lines[0].startswith("CONNECT "))

    def test_proxy_off_and_no_proxy_configured(self):
        with mock.patch.object(netproxy, "proxy_for", side_effect=AssertionError("not consulted")):
            t = HttpTransport("http://127.0.0.1:1", proxy="off")
            self.assertEqual(t.proxy_route(), (None, ["127.0.0.1"]))
            t = HttpTransport("http://127.0.0.1:1")      # the netproxy settings have no proxy: direct
            with mock.patch.object(netproxy, "load_proxy_settings",
                                   return_value=netproxy.ProxySettings(None, None, (), "none")):
                self.assertEqual(t.proxy_route(), (None, ["127.0.0.1"]))

    def test_proxy_failure_is_connect_phase(self):
        def refuse(*a, **kw):
            from hive.errors import unavailable
            raise unavailable("proxy 127.0.0.1:7890 is not reachable", reason="proxy", maybe_executed=False)
        calls = []
        with mock.patch.object(netproxy, "open_tunnel", side_effect=lambda *a, **kw: (calls.append(1), refuse())), \
                mock.patch.object(transport.time, "sleep"):
            t = HttpTransport("http://127.0.0.1:1", TOKEN, proxy="http://127.0.0.1:7890")
            with self.assertRaises(HiveError) as cm:
                t.request("POST", "/v1/call", {"action": "shell.run", "params": {"command": "x"}})
        self.assertEqual((cm.exception.details["reason"], cm.exception.details["maybe_executed"]), ("proxy", False))
        self.assertEqual(len(calls), 3)
        auth = []
        with mock.patch.object(netproxy, "open_tunnel", side_effect=lambda *a, **kw: auth.append(1) or (_ for _ in ())
                               .throw(HiveError("unavailable", "auth", {"reason": "proxy_auth",
                                                                        "maybe_executed": False}))):
            with self.assertRaises(HiveError) as cm:
                t.request("GET", "/v1/whoami")
        self.assertEqual(cm.exception.details["reason"], "proxy_auth")
        self.assertEqual(len(auth), 1)    # proxy_auth is not retried


if __name__ == "__main__":
    unittest.main()
