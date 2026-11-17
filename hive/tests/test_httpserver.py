"""WP1 ApiServer tests (§21.2 httpserver row): transport limits, auth modes, proxied detection, Host/Origin rules,
route options and modes, multi-address bind, TLS handshake in the worker."""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from typing import Any, Dict
from unittest import mock

from hive import httpserver, util
from hive.auth import TokenStore
from hive.config import ListenConfig, TlsConfig
from hive.errors import HiveError, rate_limited
from hive.httpserver import ApiServer, Request, json_depth_ok
from hive.limits import AuthThrottle
from tests.wp1util import http, raw_request

T = {k: util.new_token() for k in ("owner", "agent", "hub", "models", "node")}


def store() -> TokenStore:
    return TokenStore([{"name": "owner", "sha256": util.hash_token(T["owner"]), "kind": "owner"},
                       {"name": "agent", "sha256": util.hash_token(T["agent"]), "kind": "agent"},
                       {"name": "hub-home", "sha256": util.hash_token(T["hub"]), "kind": "hub"},
                       {"name": "chat", "sha256": util.hash_token(T["models"]), "kind": "models", "models": ["q"]}],
                      kind_default="agent")


def relay_store() -> TokenStore:
    return TokenStore([{"name": "pod-a", "sha256": util.hash_token(T["node"]), "kind": "node"}], kind_default="node")


def echo(req: Request) -> Dict[str, Any]:
    i = req.identity
    return {"ip": req.client_ip, "proxied": req.proxied, "kind": i.kind if i else None, "via": i.via if i else None,
            "agent": i.agent if i else None, "exec_gated": i.exec_gated if i else None, "query": req.query,
            "source_ip": i.source_ip if i else None}


class Server:
    def __init__(self, role: str = "node", **listen: Any):
        listen.setdefault("port", 0)
        self.audit = mock.Mock()
        self.api = ApiServer(ListenConfig(**listen), role=role, tokens=store, relay_tokens=relay_store,
                             throttle=AuthThrottle(3, 60, 300), logger=logging.getLogger("hive.test.http"),
                             audit=self.audit, owner_check=lambda i: i.source_ip == "127.0.0.1")
        a = self.api
        a.route("GET", "/v1/echo", echo)
        a.route("GET", "/v1/open", echo, auth="none")
        a.route("POST", "/v1/post", lambda r: {"json": r.json()})
        a.route("GET", "/v1/owner", echo, auth="owner")
        a.route("POST", "/v1/relay/poll", lambda r: echo(r), auth="relay")
        a.route("GET", "/openai/{node}/{service}/{rest*}", echo, auth="models", error_style="openai", cors=True)
        a.route("POST", "/openai/{node}/{service}/{rest*}", lambda r: {"n": len(r.body)}, auth="models",
                error_style="openai", cors=True, max_body=100)
        a.route("OPTIONS", "/openai/{node}/{service}/{rest*}", lambda r: None, auth="none", cors=True)
        a.route("POST", "/v1/call", self._env, mode="envelope")
        a.route("GET", "/v1/raw", lambda r: (201, {"Content-Type": "text/plain"}, b"hi"), auth="none", mode="raw")
        a.route("GET", "/v1/stream", lambda r: (200, {"Content-Type": "text/event-stream"},
                                                iter([b"data: 1\n\n", b"data: 2\n\n"])), auth="none", mode="raw")
        a.route("GET", "/v1/boom", self._boom)
        a.route("GET", "/v1/approvals/{id}/x", echo)
        a.route("GET", "/v1/rl", self._rl)
        self.bound = a.start()
        self.port = self.bound[0][1]
        self.url = f"http://127.0.0.1:{self.port}"

    @staticmethod
    def _env(req: Request) -> dict:
        body = req.json() or {}
        if body.get("fail"):
            return {"ok": False, "error": rate_limited("busy", retry_after=2.5, reason="inflight").to_dict(),
                    "meta": {"request_id": "r_x", "api": 1}}
        return {"ok": True, "result": {"x": 1}, "meta": {"request_id": "r_given", "api": 1}}

    @staticmethod
    def _boom(req: Request) -> dict:
        raise ValueError("secret detail /etc/x")

    @staticmethod
    def _rl(req: Request) -> dict:
        raise rate_limited("slow down", retry_after=3, reason="inflight")

    def stop(self) -> None:
        self.api.stop()


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Server()
        self.addCleanup(self.s.stop)


class TestBasics(Base):
    def test_health_headers_no_version_leak(self):
        st, h, body = http(self.s.url + "/v1/open")
        self.assertEqual(st, 200)
        self.assertEqual(h["Server"], "hive")
        self.assertEqual((h["Cache-Control"], h["X-Content-Type-Options"], h["X-Hive-Api"]),
                         ("no-store", "nosniff", "1"))
        self.assertTrue(h["X-Request-Id"].startswith("r_"))
        self.assertEqual(body["meta"]["api"], 1)
        self.assertNotIn("Access-Control-Allow-Origin", h)

    def test_auth_modes(self):
        u = self.s.url
        self.assertEqual(http(u + "/v1/echo")[0], 401)
        st, h, b = http(u + "/v1/echo", token=util.new_token())
        self.assertEqual((st, h.get("WWW-Authenticate"), b["error"]["code"]), (401, "Bearer", "unauthorized"))
        self.assertEqual(http(u + "/v1/echo", token=T["agent"])[2]["result"]["kind"], "agent")
        self.assertEqual(http(u + "/v1/echo", token=T["hub"])[2]["result"]["via"], "hub")
        self.assertEqual(http(u + "/v1/echo", token=T["models"])[0], 403)
        self.assertEqual(http(u + "/v1/echo", token=T["node"])[0], 401)       # relay tokens are another store
        self.assertEqual(http(u + "/v1/owner", token=T["agent"])[0], 403)
        self.assertEqual(http(u + "/v1/owner", token=T["owner"])[0], 200)
        self.assertEqual(http(u + "/v1/relay/poll", method="POST", body={}, token=T["node"])[0], 200)
        self.assertEqual(http(u + "/v1/relay/poll", method="POST", body={}, token=T["agent"])[0], 401)
        self.assertEqual(http(u + "/openai/self/q/v1/models", token=T["models"])[0], 200)
        self.assertEqual(http(u + "/openai/self/q/v1/models", token=T["hub"])[0], 200)       # node: hub passthrough
        st, _, b = http(u + "/openai/self/q/v1/models", token=T["agent"])
        self.assertEqual((st, b["error"]["code"]), (403, "forbidden"))
        self.assertIn("gateway token", b["error"]["message"])
        self.assertEqual(self.s.audit.record.call_args_list[0].kwargs["kind"], "auth_fail")
        for c in self.s.audit.record.call_args_list:
            self.assertNotIn(T["agent"], repr(c))

    def test_hub_headers(self):
        u = self.s.url + "/v1/echo"
        r = http(u, token=T["hub"], headers={"X-Hive-Agent": "gw/chatbox", "X-Hive-Exec-Gated": "1"})[2]["result"]
        self.assertEqual((r["agent"], r["exec_gated"], r["via"]), ("gw/chatbox", True, "hub"))
        r = http(u, token=T["hub"], headers={"X-Hive-Agent": "gw:x"})[2]["result"]
        self.assertEqual(r["agent"], "")
        r = http(u, token=T["agent"], headers={"X-Hive-Agent": "evil", "X-Hive-Exec-Gated": "1"})[2]["result"]
        self.assertEqual((r["agent"], r["exec_gated"], r["via"]), ("", False, "direct"))

    def test_throttle(self):
        u = self.s.url + "/v1/echo"
        for _ in range(3):
            http(u, token=util.new_token())
        st, h, b = http(u, token=util.new_token())
        self.assertEqual((st, b["error"]["details"]["reason"]), (429, "auth_throttle"))
        self.assertTrue(int(h["Retry-After"]) > 200)
        self.assertEqual(http(u, token=T["agent"])[0], 200)                  # a valid token still proceeds

    def test_transport_limits(self):
        p = self.s.port
        hdr = f"Host: 127.0.0.1:{p}\r\nAuthorization: Bearer {T['agent']}\r\n"
        out = raw_request(p, f"POST /v1/post HTTP/1.1\r\n{hdr}Content-Type: application/json\r\n\r\n".encode())
        self.assertIn(b" 411 ", out.split(b"\r\n")[0])
        self.assertIn(b'"bad_request"', out)
        out = raw_request(p, (f"POST /v1/post HTTP/1.1\r\n{hdr}Content-Type: application/json\r\n"
                              "Transfer-Encoding: chunked\r\n\r\n").encode())
        self.assertIn(b" 411 ", out.split(b"\r\n")[0])
        out = raw_request(p, (f"POST /v1/post HTTP/1.1\r\n{hdr}Content-Type: application/json\r\nContent-Length: 2"
                              "\r\nContent-Length: 2\r\n\r\n{}").encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        out = raw_request(p, f"POST /v1/post HTTP/1.1\r\n{hdr}Content-Type: text/plain\r\nContent-Length: 2\r\n\r\n{{}}"
                          .encode())
        self.assertIn(b" 415 ", out.split(b"\r\n")[0])
        out = raw_request(p, (f"POST /v1/post HTTP/1.1\r\n{hdr}Content-Type: application/json\r\n"
                              f"Content-Length: {60 << 20}\r\n\r\n").encode())
        self.assertIn(b" 413 ", out.split(b"\r\n")[0])
        out = raw_request(p, f"GET /v1/open HTTP/1.1\r\nHost: x\r\nX-{'a' * 17000}: 1\r\n\r\n".encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        many = "".join(f"X-H{i}: 1\r\n" for i in range(70))
        out = raw_request(p, f"GET /v1/open HTTP/1.1\r\nHost: 127.0.0.1\r\n{many}\r\n".encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])

    def test_unauthenticated_body_not_read(self):
        p = self.s.port
        t0 = time.monotonic()
        out = raw_request(p, (f"POST /v1/post HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\n"
                              f"Content-Length: 1000000\r\n\r\n").encode())
        self.assertIn(b" 401 ", out.split(b"\r\n")[0])
        self.assertLess(time.monotonic() - t0, 3)

    def test_json_depth(self):
        self.assertFalse(json_depth_ok(b"[" * 65 + b"]" * 65))
        self.assertTrue(json_depth_ok(b'{"a":"[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[[["}'))
        st, _, b = http(self.s.url + "/v1/post", method="POST", body=b"[" * 70 + b"]" * 70, token=T["agent"])
        self.assertEqual((st, b["error"]["message"]), (400, "JSON nested too deeply"))
        st, _, b = http(self.s.url + "/v1/post", method="POST", body=b"{bad", token=T["agent"])
        self.assertEqual(st, 400)

    def test_origin_host_and_api(self):
        u = self.s.url
        st, _, b = http(u + "/v1/echo", token=T["agent"], headers={"Origin": "https://evil.example"})
        self.assertEqual((st, b["error"]["message"]), (403, "browser origins not allowed"))
        st, h, _ = http(u + "/openai/self/q/v1/models", token=T["models"], headers={"Origin": "https://app"})
        self.assertEqual((st, h["Access-Control-Allow-Origin"]), (200, "*"))
        st, _, b = http(u + "/v1/echo", token=T["agent"], headers={"Host": "evil.example:7701"})
        self.assertEqual((st, b["error"]["code"]), (421, "bad_request"))
        self.assertEqual(http(u + "/v1/echo", token=T["agent"], headers={"Host": "localhost:9"})[0], 200)
        self.assertEqual(http(u + "/openai/self/q/v1/models", token=T["models"],
                              headers={"Host": "host.docker.internal:7701"})[0], 200)
        self.assertEqual(http(u + "/v1/echo", token=T["agent"], headers={"Host": "host.docker.internal"})[0], 421)
        st, _, b = http(u + "/v1/echo", token=T["agent"], headers={"X-Hive-Api": "2"})
        self.assertEqual((st, b["error"]["message"]), (400, "api mismatch"))

    def test_route_options_and_modes(self):
        u = self.s.url
        self.assertEqual(http(u + "/v1/post", token=T["agent"])[0], 405)
        r = http(u + "/openai/desk/qwen/v1/models", token=T["models"])[2]["result"]
        self.assertEqual((r["query"]["node"], r["query"]["service"], r["query"]["rest"]), ("desk", "qwen", "v1/models"))
        st, _, b = http(u + "/openai/self/q/v1/chat/completions", method="POST", body={"x": "y" * 200},
                        token=T["models"])
        self.assertEqual((st, b["error"]["type"], b["error"]["code"]), (413, "invalid_request_error", "too_large"))
        st, _, b = http(u + "/openai/self/q/v1/chat/completions", method="POST", body={})
        self.assertEqual((st, b["error"]["type"]), (401, "authentication_error"))
        st, h, b = http(u + "/openai/self/q/v1/chat/completions", method="OPTIONS",
                        headers={"Access-Control-Request-Headers": "authorization, x-stainless-os",
                                 "Origin": "https://app"})
        self.assertEqual((st, h["Access-Control-Allow-Headers"], h["Access-Control-Allow-Methods"],
                          h["Access-Control-Max-Age"]), (204, "authorization, x-stainless-os", "GET, POST, OPTIONS",
                                                         "600"))
        st, h, _ = http(u + "/openai/self/q/v1/chat/completions", method="OPTIONS",
                        headers={"Access-Control-Request-Headers": "bad\x01"})
        self.assertEqual(h["Access-Control-Allow-Headers"], "Authorization, Content-Type")
        self.assertEqual(http(u + "/openai/nope", token=T["models"])[2]["error"]["type"], "not_found_error")
        # envelope: no double wrap, status from the error code, Retry-After
        st, h, b = http(u + "/v1/call", method="POST", body={}, token=T["agent"])
        self.assertEqual((st, b, h["X-Request-Id"]), (200, {"ok": True, "result": {"x": 1},
                                                            "meta": {"request_id": "r_given", "api": 1}}, "r_given"))
        st, h, b = http(u + "/v1/call", method="POST", body={"fail": 1}, token=T["agent"])
        self.assertEqual((st, h["Retry-After"], b["error"]["code"]), (429, "3", "rate_limited"))
        st, h, b = http(u + "/v1/rl", token=T["agent"])
        self.assertEqual((st, h["Retry-After"]), (429, "3"))
        st, h, b = http(u + "/v1/raw")
        self.assertEqual((st, h["Content-Type"], b), (201, "text/plain", b"hi"))
        st, h, b = http(u + "/v1/stream")
        self.assertEqual((st, b), (200, b"data: 1\n\ndata: 2\n\n"))
        self.assertNotIn("Content-Length", h)
        st, _, b = http(u + "/v1/boom", token=T["agent"])
        self.assertEqual((st, b["error"]["code"], b["error"]["message"]), (500, "internal", "internal error"))
        self.assertEqual(http(u + "/v1/approvals/ap_bad!/x", token=T["agent"])[0], 400)
        self.assertEqual(http(u + "/v1/approvals/ap_abcdefgh/x", token=T["agent"])[0], 200)
        self.assertEqual(http(u + "/v1/nothing")[0], 404)

    def test_keep_alive(self):
        p = self.s.port
        req = f"GET /v1/open HTTP/1.1\r\nHost: 127.0.0.1:{p}\r\n\r\n".encode()
        s = socket.create_connection(("127.0.0.1", p), timeout=5)
        try:
            for _ in range(3):
                s.sendall(req)
                data = b""
                while b"}}" not in data:
                    data += s.recv(4096)
                self.assertIn(b"200 OK", data)
        finally:
            s.close()


class TestSlowClients(unittest.TestCase):
    def test_header_deadline_and_slow_body_and_preauth_cap(self):
        with mock.patch.object(httpserver, "HEADER_DEADLINE", 0.5), mock.patch.object(httpserver, "MIN_BODY_RATE",
                                                                                         10 ** 9), \
                mock.patch.object(httpserver, "PREAUTH_PER_IP", 3):
            s = Server()
            try:
                p = s.port
                c = socket.create_connection(("127.0.0.1", p), timeout=5)
                c.sendall(b"GET /v1/open HTTP/1.1\r\n")
                t0 = time.monotonic()
                self.assertEqual(c.recv(100), b"")                 # closed after the header deadline
                self.assertLess(time.monotonic() - t0, 3)
                c.close()
                c = socket.create_connection(("127.0.0.1", p), timeout=5)
                c.sendall((f"POST /v1/post HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {T['agent']}\r\n"
                           "Content-Type: application/json\r\nContent-Length: 100\r\n\r\n{").encode())
                self.assertEqual(c.recv(100), b"")                 # body too slow -> closed
                c.close()
                idle = [socket.create_connection(("127.0.0.1", p), timeout=5) for _ in range(3)]
                time.sleep(0.2)
                extra = socket.create_connection(("127.0.0.1", p), timeout=5)
                self.assertIn(b"503", extra.recv(200))
                for c in idle + [extra]:
                    c.close()
            finally:
                s.stop()

    def test_max_connections(self):
        s = Server(max_connections=1)
        try:
            hold = socket.create_connection(("127.0.0.1", s.port), timeout=5)
            time.sleep(0.2)
            out = raw_request(s.port, b"GET /v1/open HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertIn(b" 503 ", out.split(b"\r\n")[0])
            hold.close()
        finally:
            s.stop()


class TestProxied(unittest.TestCase):
    def test_proxy_port_and_forwarded_on_main(self):
        s = Server(proxy_port=util.__dict__.get("_unused", 0) or _free_port(), allowed_hosts=["hub.example.ts.net"])
        try:
            proxy_port = s.bound[-1][1]
            st, _, b = http(f"http://127.0.0.1:{proxy_port}/v1/echo", token=T["owner"],
                            headers={"Host": "hub.example.ts.net", "X-Forwarded-For": "6.6.6.6, 5.6.7.8"})
            r = b["result"]
            self.assertEqual((st, r["ip"], r["proxied"]), (200, "5.6.7.8", True))
            self.assertEqual(r["source_ip"], "proxy:5.6.7.8")
            self.assertEqual(http(f"http://127.0.0.1:{proxy_port}/v1/owner", token=T["owner"],
                                  headers={"Host": "hub.example.ts.net"})[0], 403)   # never loopback
            self.assertEqual(http(f"http://127.0.0.1:{proxy_port}/v1/echo", token=T["owner"])[0], 421)
            with self.assertLogs("hive.test.http", level="WARNING") as logs:
                self.assertEqual(http(s.url + "/v1/echo", token=T["agent"],
                                      headers={"X-Forwarded-For": "1.2.3.4"})[0], 421)   # proxied: allowed_hosts
                r = http(s.url + "/v1/echo", token=T["agent"], headers={"X-Forwarded-For": "1.2.3.4",
                                                                       "Host": "hub.example.ts.net"})[2]["result"]
                r2 = http(s.url + "/v1/echo", token=T["agent"], headers={"X-Real-IP": "1.2.3.4",
                                                                        "Host": "hub.example.ts.net"})[2]["result"]
            self.assertEqual((r["ip"], r["proxied"], r2["proxied"]), ("proxy:127.0.0.1", True, True))
            self.assertEqual(sum("forwarded request on the main port" in m for m in logs.output), 1)
            r = http(s.url + "/v1/echo", token=T["agent"], headers={"Host": "hub.example.ts.net"})[2]["result"]
            self.assertTrue(r["proxied"])                  # allowed non-loopback Host on a loopback socket
            r = http(s.url + "/v1/echo", token=T["agent"])[2]["result"]
            self.assertFalse(r["proxied"])
        finally:
            s.stop()

    def test_host_rules_unit(self):
        s = Server(allowed_hosts=["home-hub"])
        try:
            api = s.api
            api.tailnet_ip, api.tailnet_names = "100.101.1.5", ["home-hub.tail1.ts.net", "home-hub"]
            route = api._routes[0]
            srv = mock.Mock(kind="tailnet")
            H = httpserver.Headers
            for host in ("100.101.1.5:7701", "home-hub.tail1.ts.net", "home-hub"):
                api._check_host(H({"host": host}), "100.101.1.5", srv, route, False)
                self.assertEqual(api._client("100.101.1.9", "100.101.1.5", srv, H({"host": host}), route),
                                 ("100.101.1.9", False))
            with self.assertRaises(httpserver._Reject):
                api._check_host(H({"host": "evil"}), "100.101.1.5", srv, route, False)
        finally:
            s.stop()


def _free_port() -> int:
    from tests.helpers import free_port
    return free_port()


class TestBinds(unittest.TestCase):
    def test_tailscale_bind(self):
        with mock.patch.object(httpserver, "tailscale_info", return_value=("127.0.0.2", ["n.ts.net", "n"], "")):
            s = Server(host="tailscale")
        try:
            self.assertEqual([h for h, _ in s.bound], ["127.0.0.2", "127.0.0.1"])
            self.assertEqual(len({p for _, p in s.bound}), 1)
            self.assertEqual(http(f"http://127.0.0.2:{s.port}/v1/open", headers={"Host": "localhost"})[0], 200)
        finally:
            s.stop()

    def test_tailscale_missing(self):
        with mock.patch.object(httpserver, "_run", return_value=(127, "")), \
                mock.patch("shutil.which", return_value=None), mock.patch("os.path.exists", return_value=False):
            with self.assertRaises(HiveError) as cm:
                httpserver.tailscale_info()
        self.assertIn("tailscale up", cm.exception.hint)

    def test_wsl_interop_off_single_tailnet_address(self):
        with mock.patch("shutil.which", return_value=None), mock.patch("os.path.exists", return_value=False), \
                mock.patch.object(httpserver, "_running_in_wsl", return_value=True), \
                mock.patch.object(httpserver, "_local_ipv4s", return_value=["172.20.1.2", "100.101.1.7"]):
            ip, names, note = httpserver.tailscale_info()
        self.assertEqual(ip, "100.101.1.7")
        self.assertIn("interop off", note)

    def test_wsl_mirrored_via_windows_cli(self):
        def run(argv, timeout=5.0):
            return (0, "100.101.1.8\n") if argv[1:] == ["ip", "-4"] else (0, '{"Self":{"DNSName":"desk.t.ts.net."}}')
        with mock.patch("shutil.which", return_value=None), mock.patch("os.path.exists", return_value=True), \
                mock.patch.object(httpserver, "_run", side_effect=run), \
                mock.patch.object(httpserver, "_local_ipv4s", return_value=["100.101.1.8"]):
            self.assertEqual(httpserver.tailscale_info(), ("100.101.1.8", ["desk.t.ts.net", "desk"], ""))

    def test_plaintext_and_no_token_refusals(self):
        for listen in ({"host": "0.0.0.0"},):
            with self.assertRaises(HiveError) as cm:
                Server(**listen)
            self.assertEqual(cm.exception.code, "forbidden")

    def test_wildcard_and_specific_ip(self):
        s = Server(host="0.0.0.0", allow_insecure_remote=True)
        try:
            self.assertEqual(len(s.bound), 1)
            self.assertEqual(http(s.url + "/v1/echo", token=T["agent"])[0], 200)
            self.assertEqual(http(s.url + "/v1/echo", token=T["agent"], headers={"Host": "evil"})[0], 421)
        finally:
            s.stop()
        ip = _local_ip()
        if not ip:
            self.skipTest("no non-loopback IPv4 address")
        s = Server(host=ip, allow_insecure_remote=True)
        try:
            self.assertEqual([h for h, _ in s.bound], [ip, "127.0.0.1"])
            self.assertEqual(http(f"http://127.0.0.1:{s.port}/v1/echo", token=T["agent"])[0], 200)
            self.assertEqual(http(f"http://{ip}:{s.port}/v1/echo", token=T["agent"])[0], 200)
        finally:
            s.stop()


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
            u.connect(("192.0.2.1", 9))
            ip = u.getsockname()[0]
        return "" if ip.startswith("127.") else ip
    except OSError:
        return ""


class TestTls(unittest.TestCase):
    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_tls_stall_does_not_block_accept(self):
        d = tempfile.mkdtemp(prefix="hivetls-")
        self.addCleanup(shutil.rmtree, d, True)
        cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
        r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                            "-days", "1", "-subj", "/CN=localhost"], capture_output=True, timeout=60)
        if r.returncode != 0:
            self.skipTest("openssl failed")
        s = Server(tls=TlsConfig(cert=cert, key=key))
        try:
            stall = socket.create_connection(("127.0.0.1", s.port), timeout=5)    # never sends a ClientHello
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.load_verify_locations(cert)
            t0 = time.monotonic()
            with socket.create_connection(("127.0.0.1", s.port), timeout=5) as raw:
                with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
                    tls.sendall(f"GET /v1/open HTTP/1.1\r\nHost: 127.0.0.1:{s.port}\r\nConnection: close\r\n\r\n"
                                .encode())
                    self.assertIn(b"200 OK", tls.recv(4096))
            self.assertLess(time.monotonic() - t0, 3)
            stall.close()
        finally:
            s.stop()


if __name__ == "__main__":
    unittest.main()
