"""WP3b tests: net.http, net.ports (§9.8), model.forward (§9.9, gateway relay path) and the real open_stream
(§11.2: SSE chunks as they arrive, one hop, C1–C4 through the real CONNECT tunnel)."""
from __future__ import annotations

import base64
import dataclasses
import json
import socket
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hive import catalog, netproxy, netutil, schema
from hive.actions import HANDLERS, load_all
from hive.errors import HiveError
from hive.netutil import AuthSpec
from tests import helpers, wp1util, wp2util
from tests.fixtures.connect_proxy import ConnectProxy
from tests.test_download import guard, resolving

load_all()


class FakeServices:
    def __init__(self, defs: Dict[str, Tuple[str, str, Optional[AuthSpec]]]):
        self.defs = defs

    def endpoint_for(self, name: str) -> Tuple[str, str, Optional[AuthSpec]]:
        if name not in self.defs:
            raise helpers.not_found(f"model service '{name}' not found", hint="known model services: x")
        return self.defs[name]

    def endpoints(self) -> List[dict]:
        return []


class HandlerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = helpers.StubHTTPServer().start()
        self.srv._httpd.handle_error = lambda *a: None
        self.addCleanup(self.srv.stop)
        self.rt = helpers.FakeRuntime()
        self.addCleanup(self.rt.cleanup)
        self.pol = helpers.FakeEffectivePolicy(workdir=self.rt.workdir, data_dir=self.rt.data_dir)

    def call(self, action: str, params: dict, policy: Any = None, timeout: float = 30) -> dict:
        ctx = helpers.make_ctx(action, runtime=self.rt, policy=policy or self.pol, timeout=timeout)
        self.ctx = ctx
        out = HANDLERS[action](ctx, schema.validate(catalog.SPECS[action].params, params))
        schema.validate(catalog.SPECS[action].result, out, coerce=False, defaults=False, path="result")
        return out

    def err(self, action: str, params: dict, **kw: Any) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.call(action, params, **kw)
        return cm.exception


class TestNetHttp(HandlerCase):
    def test_text_json_and_headers(self):
        self.srv.add("GET", "/j", 200, {"a": 1}, {"X-Thing": "v"})
        r = self.call("net.http", {"url": self.srv.url + "/j"})
        self.assertEqual((r["status"], json.loads(r["body"]), r["route"], r["truncated"]), (200, {"a": 1}, "direct",
                                                                                           False))
        self.assertEqual(r["headers"]["X-Thing"], "v")
        self.assertEqual(r["bytes"], len(r["body"]))
        self.assertEqual(self.ctx.audit_extra["route"], "direct")
        self.assertEqual(self.ctx.audit_extra["dns"], "local")

    def test_post_body_and_status_is_a_result(self):
        self.srv.add_handler("POST", "/echo", lambda req: (418, {"Content-Type": "text/plain"},
                                                           req["body"] + b"|" + req["headers"]["Content-Type"].encode()))
        r = self.call("net.http", {"url": self.srv.url + "/echo", "method": "POST", "body": "hello",
                                   "content_type": "text/x-test", "headers": ["X-A: 1"]})
        self.assertEqual((r["status"], r["body"]), (418, "hello|text/x-test"))
        self.assertEqual(self.srv.requests[-1]["headers"]["X-A"], "1")
        r = self.call("net.http", {"url": self.srv.url + "/echo", "method": "POST",
                                   "body": base64.b64encode(b"\x00\x01").decode(), "body_base64": True,
                                   "headers": ["Content-Type: application/octet-stream"]})
        self.assertEqual(self.srv.requests[-1]["body"], b"\x00\x01")
        e = self.err("net.http", {"url": self.srv.url + "/echo", "method": "POST", "body": "!!", "body_base64": True})
        self.assertEqual(e.code, "bad_request")

    def test_binary_body_and_truncation(self):
        blob = bytes(range(256)) * 10
        self.srv.add("GET", "/b", 200, blob, {"Content-Type": "application/octet-stream"})
        r = self.call("net.http", {"url": self.srv.url + "/b", "max_bytes": 100})
        self.assertNotIn("body", r)
        self.assertEqual(base64.b64decode(r["body_base64"]), blob[:100])
        self.assertTrue(r["truncated"])
        self.assertEqual(r["bytes"], 100)
        self.srv.add("GET", "/many", 200, "x", {f"X-H{i}": str(i) for i in range(80)})
        r = self.call("net.http", {"url": self.srv.url + "/many"})
        self.assertLessEqual(len(r["headers"]), 50)

    def test_refusals(self):
        e = self.err("net.http", {"url": "http://169.254.169.254/latest"})
        self.assertEqual((e.code, e.details.get("rule")), ("policy_denied", "ssrf"))
        e = self.err("net.http", {"url": self.srv.url, "headers": ["Host: evil.com"]})
        self.assertEqual(e.code, "bad_request")
        e = self.err("net.http", {"url": self.srv.url, "headers": ["Content-Type: a"], "content_type": "b"})
        self.assertEqual(e.code, "bad_request")
        e = self.err("net.http", {"url": "http://user:pw@127.0.0.1/"})
        self.assertEqual(e.code, "bad_request")
        e = self.err("net.http", {"url": f"http://127.0.0.1:{helpers.free_port()}/"})
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "connect", False))

    def test_secrets(self):
        self.srv.add("GET", "/s", 200, "ok")
        with wp1util.FakeHome() as fh:
            from hive.secretstore import SecretStore
            st = SecretStore(fh.data / "secrets.json", passthrough=(), env={})
            st.set("LOCAL_KEY", "localsecret123", hosts=["loopback"])
            st.set("GITHUB_TOKEN", "ghp_abcdefghijklmnop")
            pol = wp1util.build_policy(fh, guard=netutil.NetGuard, secrets=st)
            op = pol.effective(wp1util.OPERATOR, paused=False)
            rn = pol.effective(wp1util.READNET, paused=False)
            r = self.call("net.http", {"url": self.srv.url + "/s", "auth_env": "LOCAL_KEY"}, policy=op)
            self.assertEqual(r["status"], 200)
            self.assertEqual(self.srv.requests[-1]["headers"]["Authorization"], "Bearer localsecret123")
            e = self.err("net.http", {"url": self.srv.url + "/s", "auth_env": "GITHUB_TOKEN"}, policy=op)
            self.assertEqual((e.code, e.details["rule"]), ("policy_denied", "secret_host"))
            e = self.err("net.http", {"url": self.srv.url + "/s", "auth_env": "LOCAL_KEY", "verify_tls": False},
                         policy=op)
            self.assertEqual((e.code, e.details["rule"]), ("policy_denied", "unverified_tls"))
            # rule 5b: a {read,net} identity may not send a secret with POST; GET is fine
            e = self.err("net.http", {"url": self.srv.url + "/s", "auth_env": "GITHUB_TOKEN", "method": "POST"},
                         policy=rn)
            self.assertEqual((e.code, e.details["rule"]), ("policy_denied", "secret_write"))
            self.assertEqual(self.call("net.http", {"url": self.srv.url + "/s", "auth_env": "LOCAL_KEY"},
                                       policy=rn)["status"], 200)
            e = self.err("net.http", {"url": self.srv.url + "/s", "auth_env": "NOT_SET"}, policy=op)
            self.assertEqual(e.code, "not_found")


class TestNetPorts(HandlerCase):
    def test_ports_loopback(self):
        rows = [{"proto": "tcp", "address": "127.0.0.1", "port": self.srv.port, "pid": 4242, "name": "stub"}]
        with mock.patch("hive.procinfo.listeners", return_value=(rows, "proc")):
            closed = helpers.free_port()
            r = self.call("net.ports", {"ports": [self.srv.port, closed], "timeout": 0.5})
            self.assertEqual(r["method"], "proc")
            a, b = r["ports"]
            self.assertEqual((a["port"], a["open"], a["pid"], a["process"], a["address"]),
                             (self.srv.port, True, 4242, "stub", "127.0.0.1"))
            self.assertIsInstance(a["latency_ms"], float)
            self.assertEqual((b["open"], b["latency_ms"], b["pid"]), (False, None, None))
            r = self.call("net.ports", {})
            self.assertEqual(r["listeners"][0]["process"], "stub")
        r = self.call("net.ports", {})
        self.assertIn("listeners", r)
        self.assertIn("method", r)
        self.assertNotIn("host", catalog.SPECS["net.ports"].params["properties"])     # loopback only


class TestModelForward(HandlerCase):
    def setUp(self) -> None:
        super().setUp()
        self.srv.openai(models=("qwen",), reply="hi there")
        self.srv.add_handler("POST", "/v1/completions", lambda req: (200, {}, {"choices": [{"text": "x"}],
                                                                              "echo": json.loads(req["body"])}))
        self.srv.add_handler("POST", "/v1/embeddings", lambda req: (200, {}, {"data": [{"embedding": [0.1]}],
                                                                             "usage": {"prompt_tokens": 2}}))
        self.key = AuthSpec(name="SVC_KEY", header="Authorization", value="Bearer svc-secret-1",
                            hosts=("loopback",))
        self.rt.services = FakeServices({"qwen": (self.srv.url + "/v1", "openai", self.key)})

    def test_paths(self):
        r = self.call("model.forward", {"service": "qwen", "path": "models"})
        self.assertEqual((r["status"], json.loads(r["body"])["data"][0]["id"]), (200, "qwen"))
        self.assertEqual(r["content_type"], "application/json")
        self.assertEqual(self.srv.requests[-1]["method"], "GET")
        body = {"model": "qwen", "messages": [{"role": "user", "content": "hi"}], "stream": True,
                "stream_options": {"include_usage": True}}
        r = self.call("model.forward", {"service": "qwen", "path": "chat/completions", "body": json.dumps(body)})
        sent = json.loads(self.srv.requests[-1]["body"])
        self.assertIs(sent["stream"], False)                   # rewritten
        self.assertNotIn("stream_options", sent)               # removed (v5)
        self.assertEqual(json.loads(r["body"])["choices"][0]["message"]["content"], "hi there")
        self.assertEqual(self.ctx.audit_extra["model"], "qwen")
        self.assertIs(self.ctx.audit_extra["stream"], True)
        self.assertEqual(self.ctx.audit_extra["usage"]["completion_tokens"], 2)
        self.assertEqual(self.srv.requests[-1]["headers"]["Authorization"], "Bearer svc-secret-1")
        self.assertEqual(self.srv.requests[-1]["headers"]["Content-Type"], "application/json")
        r = self.call("model.forward", {"service": "qwen", "path": "completions", "body": '{"prompt":"a"}'})
        self.assertEqual(json.loads(r["body"])["echo"], {"prompt": "a"})
        r = self.call("model.forward", {"service": "qwen", "path": "embeddings", "body": '{"input":"a"}'})
        self.assertEqual(r["status"], 200)

    def test_upstream_errors_pass_through(self):
        self.srv.add("POST", "/v1/chat/completions", 400, {"error": {"message": "bad model"}})
        r = self.call("model.forward", {"service": "qwen", "path": "chat/completions", "body": "{}"})
        self.assertEqual((r["status"], r["truncated"]), (400, False))
        self.assertIn("bad model", r["body"])

    def test_api_key_only_to_service_endpoint(self):
        # an endpoint on another host than the key's binding never receives the key
        other = AuthSpec(name="HF_TOKEN", header="Authorization", value="Bearer hf_x1234567", hosts=("huggingface.co",))
        self.rt.services = FakeServices({"evil": (self.srv.url + "/v1", "openai", other)})
        e = self.err("model.forward", {"service": "evil", "path": "models"})
        self.assertEqual((e.code, e.details.get("rule")), ("policy_denied", "secret_host"))
        self.assertFalse(any("hf_x1234567" in json.dumps(q["headers"]) for q in self.srv.requests))

    def test_limits_and_errors(self):
        e = self.err("model.forward", {"service": "nope", "path": "models"})
        self.assertEqual(e.code, "not_found")
        self.assertIn("define the service with a preset or set endpoint", e.hint)
        e = self.err("model.forward", {"service": "qwen", "path": "chat/completions"})
        self.assertEqual(e.code, "bad_request")
        e = self.err("model.forward", {"service": "qwen", "path": "chat/completions", "body": "[1]"})
        self.assertEqual(e.code, "bad_request")
        lim = self.pol.limits
        self.pol.limits = dataclasses.replace(lim, gateway_max_body_bytes=10)
        e = self.err("model.forward", {"service": "qwen", "path": "chat/completions",
                                       "body": json.dumps({"x": "y" * 50})})
        self.assertEqual(e.code, "too_large")
        big = "z" * 200000
        self.srv.add("GET", "/v1/models", 200, big)
        self.pol.limits = dataclasses.replace(lim, gateway_max_response_bytes=100000)
        r = self.call("model.forward", {"service": "qwen", "path": "models"})
        self.assertTrue(r["truncated"])
        self.assertEqual(len(r["body"]), 100000)
        # min(gateway_max_response_bytes, max_result_bytes - 64 KiB)
        self.pol.limits = dataclasses.replace(lim, max_result_bytes=65536 + 5000)
        r = self.call("model.forward", {"service": "qwen", "path": "models"})
        self.assertEqual((r["truncated"], len(r["body"])), (True, 5000))
        self.pol.limits = lim
        self.rt.services = FakeServices({"down": (f"http://127.0.0.1:{helpers.free_port()}/v1", "openai", None)})
        e = self.err("model.forward", {"service": "down", "path": "models"})
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "connect", False))
        self.assertIn("check service_status", e.hint)

    def test_model_endpoints_name_and_ollama(self):
        self.rt.services = None
        self.pol.model_endpoints = [{"name": "ollama", "base_url": self.srv.url + "/v1", "api_key_env": None}]
        r = self.call("model.forward", {"service": "ollama", "path": "models"})
        self.assertEqual(r["status"], 200)


class TestOpenStreamReal(unittest.TestCase):
    def test_sse_chunks_arrive_incrementally(self):
        gate = threading.Event()

        def slow(req: dict) -> Any:
            def gen() -> Any:
                yield b"data: one\n\n"
                gate.wait(5)
                yield b"data: two\n\n"
            return 200, {"Content-Type": "text/event-stream"}, gen()
        with helpers.StubHTTPServer() as srv:
            srv.add_handler("POST", "/v1/chat/completions", slow)
            status, headers, chunks = netutil.open_stream("POST", srv.url + "/v1/chat/completions", guard=guard(),
                                                          purpose="model", body=b"{}")
            self.assertEqual(status, 200)
            self.assertEqual(headers.get("Content-Type"), "text/event-stream")
            first = next(chunks)
            self.assertIn(b"one", first)           # before the server sent the rest
            gate.set()
            rest = b"".join(chunks)
            self.assertIn(b"two", rest)

    def test_https_through_real_tunnel_and_credentialed_http(self):
        with helpers.StubHTTPServer() as srv:
            srv.add("GET", "/v1/models", 200, {"data": []})
            with ConnectProxy(routes={"10.1.2.3:80": ("127.0.0.1", srv.port)}) as px:
                auth = AuthSpec(name="K", header="Authorization", value="Bearer topsecret5", hosts=("private",))
                target = netutil.VettedTarget("http", "10.1.2.3", 80, "/v1/models", "10.1.2.3", "proxy", px.url,
                                              "private")

                class G:
                    def check_url(self, url: str, **kw: Any) -> Any:
                        return target
                status, _h, chunks = netutil.open_stream("GET", "http://10.1.2.3/v1/models", guard=G(),
                                                         purpose="model", auth=auth)
                self.assertEqual((status, json.loads(b"".join(chunks))), (200, {"data": []}))
                self.assertEqual(px.log[-1]["kind"], "connect")          # real netproxy.open_tunnel
                self.assertNotIn("topsecret5", json.dumps(px.log))
                self.assertEqual(srv.requests[-1]["headers"]["Authorization"], "Bearer topsecret5")
                # a secret not bound to the host is silently dropped (C2)
                other = AuthSpec(name="K2", header="Authorization", value="Bearer nope12345", hosts=("loopback",))
                _s, _h, chunks = netutil.open_stream("GET", "http://10.1.2.3/v1/models", guard=G(), purpose="model",
                                                     auth=other)
                b"".join(chunks)
                self.assertNotIn("Authorization", srv.requests[-1]["headers"])

    def test_policy_and_proxy_routes(self):
        with self.assertRaises(HiveError) as cm:
            netutil.open_stream("GET", "http://169.254.169.254/x", guard=guard(), purpose="model")
        self.assertEqual(cm.exception.details.get("rule"), "ssrf")
        with helpers.StubHTTPServer() as srv:
            srv.add("GET", "/v1/models", 200, {"ok": 1})
            with ConnectProxy(routes={f"api.example.com:{srv.port}": ("127.0.0.1", srv.port)}) as px, \
                    resolving({"api.example.com": ["93.184.216.34"]}):
                ps = netproxy.ProxySettings(px.url, px.url, netproxy.ALWAYS_NO_PROXY, "config")
                status, _h, chunks = netutil.open_stream("GET", f"http://api.example.com:{srv.port}/v1/models",
                                                         guard=guard(proxies=ps), purpose="model")
                self.assertEqual(json.loads(b"".join(chunks)), {"ok": 1})
                self.assertEqual(px.log[-1]["kind"], "absolute")


class TestTransportThroughRealProxy(unittest.TestCase):
    """WP5a note 72: the client proxy route re-run against the real netproxy (explicit proxy URL -> CONNECT)."""

    def test_explicit_proxy_connect(self):
        from hive import transport
        with helpers.StubHTTPServer() as srv:
            srv.add("GET", "/v1/whoami", 200, {"ok": True, "result": {"kind": "node"}}, {"X-Hive-Api": "1"})
            with ConnectProxy() as px:
                t = transport.HttpTransport(srv.url, proxy=px.url)
                proxy, _ips = t.proxy_route()
                self.assertEqual(proxy, px.url)


if __name__ == "__main__":
    unittest.main()
