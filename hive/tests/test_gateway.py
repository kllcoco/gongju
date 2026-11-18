"""WP5b model gateway tests (§8.7, D48; §21.2 relay/hub row "Gateway")."""
from __future__ import annotations

import http.client
import json
import threading
import time
import unittest
from pathlib import Path

from hive import gateway, util
from hive.errors import HiveError
from hive.transport import HttpTransport
from tests import helpers
from tests.wp5util import FAST_RELAY, FakeRelay, call_async, hub_token_entry, ok_env


def req(url: str, method: str, path: str, *, token=None, body=None, headers=None, stream=False):
    """Raw HTTP request; returns (status, headers dict, body bytes | list of chunks)."""
    from urllib.parse import urlsplit
    u = urlsplit(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=30)
    h = dict(headers or {})
    if token:
        h["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    if stream:
        chunks = []
        while True:
            c = r.read1(65536)
            if not c:
                break
            chunks.append((time.monotonic(), c))
        conn.close()
        return r.status, hdrs, chunks
    out = r.read()
    conn.close()
    return r.status, hdrs, out


def raw_status(url: str, method: str, path: str, token: str, length: int) -> int:
    """Send only the head of a request announcing ``length`` body bytes; return the HTTP status."""
    import socket
    from urllib.parse import urlsplit
    u = urlsplit(url)
    s = socket.create_connection((u.hostname, u.port), timeout=10)
    try:
        s.sendall((f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\n"
                   f"Content-Type: application/json\r\nContent-Length: {length}\r\n\r\n").encode())
        data = s.recv(4096)
    finally:
        s.close()
    return int(data.split(b" ", 2)[1])


def slow_sse(stub, path="/v1/chat/completions", n=3, gap=0.3):
    def handler(req):
        def gen():
            for i in range(n):
                time.sleep(gap)
                yield ("data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": f"w{i}"}}]}) +
                       "\n\n").encode()
            yield b"data: [DONE]\n\n"
        return 200, {"Content-Type": "text/event-stream"}, gen()
    stub.add_handler("POST", path, handler)


class _GwCase(unittest.TestCase):
    def start_stub(self):
        stub = helpers.StubHTTPServer().openai(models=("qwen-7b",), reply="hello there")
        stub.start()
        self.addCleanup(stub.stop)
        return stub

    def start_node(self, stub, *, extra_tokens=(), **kw):
        models_tok = util.new_token()
        toks = [{"name": "chatbox", "sha256": util.hash_token(models_tok), "kind": "models", "models": ["stub*"]}]
        toks += list(extra_tokens)
        gw = dict({"enabled": True}, **kw.pop("gateway", {}))
        url, agent, rt = helpers.start_node_server(
            start_managers=False, auth={"tokens": toks}, gateway=gw,
            model_endpoints=[{"name": "stub", "base_url": stub.url + "/v1", "api_key_env": None},
                             {"name": "stubhidden", "base_url": stub.url + "/v1", "api_key_env": None},
                             {"name": "other", "base_url": stub.url + "/v1", "api_key_env": None}], **kw)
        self.addCleanup(rt.test_stop)
        return url, agent, rt, models_tok


class TestNodeGateway(_GwCase):
    def setUp(self):
        self.stub = self.start_stub()
        self.url, self.agent, self.rt, self.mtok = self.start_node(self.stub)

    def test_models_and_chat(self):
        st, h, body = req(self.url, "GET", "/openai/self/stub/v1/models", token=self.mtok)
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["data"][0]["id"], "qwen-7b")
        self.assertEqual(h.get("access-control-allow-origin"), "*")
        # the /v1-less form works too
        st, h, body = req(self.url, "POST", "/openai/self/stub/chat/completions", token=self.mtok,
                           body={"model": "qwen-7b", "messages": [{"role": "user", "content": "secret prompt"}]})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["choices"][0]["message"]["content"], "hello there")
        # the client's Authorization is not forwarded upstream
        last = self.stub.requests[-1]
        self.assertNotIn("Authorization", last["headers"])
        audit = "".join(p.read_text() for p in (Path(self.rt.data_dir) / "audit").glob("*.jsonl"))
        rec = [json.loads(x) for x in audit.splitlines() if '"kind":"gateway"' in x][-1]
        self.assertEqual(rec["extra"]["model"], "qwen-7b")
        self.assertEqual(rec["extra"]["usage"], {"prompt_tokens": 3, "completion_tokens": 2})
        self.assertEqual(rec["extra"]["status"], 200)
        self.assertNotIn("secret prompt", audit)

    def test_stream_incremental(self):
        slow_sse(self.stub)
        st, h, chunks = req(self.url, "POST", "/openai/self/stub/v1/chat/completions", token=self.mtok,
                             body={"model": "q", "stream": True, "messages": []}, stream=True)
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/event-stream"))
        self.assertGreaterEqual(len(chunks), 2)
        self.assertGreater(chunks[-1][0] - chunks[0][0], 0.2)       # arrived incrementally
        self.assertIn(b"[DONE]", b"".join(c for _t, c in chunks))

    def test_errors_openai_shaped(self):
        # unauthenticated POST -> 401 openai-shaped
        st, _h, body = req(self.url, "POST", "/openai/self/stub/v1/chat/completions", body={})
        self.assertEqual(st, 401)
        self.assertEqual(json.loads(body)["error"]["type"], "authentication_error")
        # agent token -> 403
        st, _h, body = req(self.url, "GET", "/openai/self/stub/v1/models", token=self.agent)
        self.assertEqual(st, 403)
        self.assertIn("gateway token", json.loads(body)["error"]["message"])
        # service outside the globs -> 403
        st, _h, body = req(self.url, "GET", "/openai/self/other/v1/models", token=self.mtok)
        self.assertEqual(st, 403)
        # unknown service -> 404 naming only glob-matching services
        st, _h, body = req(self.url, "GET", "/openai/self/stubx/v1/models", token=self.mtok)
        self.assertEqual(st, 404)
        msg = json.loads(body)["error"]["message"]
        self.assertIn("stub", msg)
        self.assertNotIn("other", msg)
        # other node name -> 404
        st, _h, _b = req(self.url, "GET", "/openai/elsewhere/stub/v1/models", token=self.mtok)
        self.assertEqual(st, 404)
        # unknown route under /openai
        st, _h, _b = req(self.url, "GET", "/openai/self/stub/v1/files", token=self.mtok)
        self.assertEqual(st, 404)
        # body must be a JSON object; too large -> 413
        st, _h, _b = req(self.url, "POST", "/openai/self/stub/v1/chat/completions", token=self.mtok, body=b"[1]")
        self.assertEqual(st, 400)
        self.assertEqual(raw_status(self.url, "POST", "/openai/self/stub/v1/chat/completions", self.mtok,
                                    8388608 + 10), 413)
        # models token on /v1/call -> 403
        st, _h, _b = req(self.url, "POST", "/v1/call", token=self.mtok, body={"action": "node.info"})
        self.assertEqual(st, 403)

    def test_cors_preflight(self):
        st, h, body = req(self.url, "OPTIONS", "/openai/self/stub/v1/chat/completions",
                           headers={"Origin": "https://chat.example", "Access-Control-Request-Method": "POST",
                                    "Access-Control-Request-Headers": "authorization, x-stainless-os"})
        self.assertEqual((st, body), (204, b""))
        self.assertEqual(h["access-control-allow-origin"], "*")
        self.assertEqual(h["access-control-allow-headers"], "authorization, x-stainless-os")
        self.assertNotIn("access-control-allow-credentials", h)

    def test_inflight_cap(self):
        slow_sse(self.stub, n=4, gap=0.4)
        boxes = [call_async(req, self.url, "POST", "/openai/self/stub/v1/chat/completions", token=self.mtok,
                            body={"stream": True}) for _ in range(4)]
        time.sleep(0.5)
        st, h, _b = req(self.url, "POST", "/openai/self/stub/v1/chat/completions", token=self.mtok,
                         body={"stream": True})
        self.assertEqual(st, 429)
        self.assertIn("retry-after", h)
        for b in boxes:
            b["thread"].join(15)

    def test_masks_non_2xx_body(self):
        helpers.set_secret(self.rt, "MY_API_KEY", "supersecretvalue123", hosts=["loopback"])
        self.stub.add("POST", "/v1/embeddings", 400, {"error": {"message": "bad key supersecretvalue123"}})
        st, _h, body = req(self.url, "POST", "/openai/self/stub/v1/embeddings", token=self.mtok,
                            body={"input": "x"})
        self.assertEqual(st, 400)
        self.assertNotIn(b"supersecretvalue123", body)

    def test_hub_token_passthrough_and_paused(self):
        tok = util.new_token()
        url, _agent, rt, _m = self.start_node(self.stub, extra_tokens=[hub_token_entry(tok)])
        st, _h, body = req(url, "GET", "/openai/self/stub/v1/models", token=tok,
                            headers={"X-Hive-Agent": "gw/chatbox"})
        self.assertEqual(st, 200)
        audit = "".join(p.read_text() for p in (Path(rt.data_dir) / "audit").glob("*.jsonl"))
        self.assertIn('"agent":"gw/chatbox"', audit)
        (Path(rt.data_dir) / "PAUSED").write_text("{}")
        time.sleep(1.1)
        st, _h, _b = req(url, "POST", "/openai/self/stub/v1/chat/completions", token=tok, body={})
        self.assertEqual(st, 403)

    def test_hub_token_without_net(self):
        tok = util.new_token()
        url, _agent, rt, _m = self.start_node(self.stub, extra_tokens=[hub_token_entry(tok, role="files")])
        st, _h, body = req(url, "GET", "/openai/self/stub/v1/models", token=tok)
        self.assertEqual(st, 403)


class TestHubGateway(_GwCase):
    def setUp(self):
        self.stub = self.start_stub()
        self.hub_tok = util.new_token()
        self.nurl, _a, self.nrt, _m = self.start_node(self.stub, extra_tokens=[hub_token_entry(self.hub_tok)])
        self.rtok = util.new_token()
        nodes = [{"name": "desk", "mode": "direct", "url": self.nurl, "token": self.hub_tok, "aliases": ["pc"]},
                 {"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(self.rtok)}]
        self.url, self.toks, self.hub = helpers.start_hub(
            nodes, relay=dict(FAST_RELAY),
            extra_tokens=[("models", {"name": "chatbox", "kind": "models", "models": ["desk/stub", "pod-a/*"]}),
                          ("aliasonly", {"name": "pcapp", "kind": "models", "models": ["pc/*"]})])
        self.addCleanup(self.hub.test_stop)
        self.mtok = self.toks["models"]

    def test_direct_passthrough(self):
        st, _h, body = req(self.url, "GET", "/openai/desk/stub/v1/models", token=self.mtok)
        self.assertEqual(st, 200, body)
        slow_sse(self.stub)
        st, h, chunks = req(self.url, "POST", "/openai/desk/stub/chat/completions", token=self.mtok,
                             body={"stream": True}, stream=True)
        self.assertEqual(st, 200)
        self.assertGreater(chunks[-1][0] - chunks[0][0], 0.2)
        audit = "".join(p.read_text() for p in (Path(self.nrt.data_dir) / "audit").glob("*.jsonl"))
        self.assertIn('"agent":"gw/chatbox"', audit)
        haudit = "".join(p.read_text() for p in (self.hub.data_dir / "audit").glob("*.jsonl"))
        self.assertIn('"kind":"gateway"', haudit)

    def test_globs_on_canonical_name_and_404s(self):
        # alias "pc" matches the alias-only glob but the canonical name "desk" does not -> 403
        st, _h, _b = req(self.url, "GET", "/openai/pc/stub/v1/models", token=self.toks["aliasonly"])
        self.assertEqual(st, 403)
        # pair outside the globs -> refused without naming other services
        st, _h, body = req(self.url, "GET", "/openai/desk/other/v1/models", token=self.mtok)
        self.assertIn(st, (403, 404))
        self.assertNotIn(b"stubhidden", body)
        # unknown node -> 404 without other names
        st, _h, body = req(self.url, "GET", "/openai/pod-x/qwen/v1/models", token=self.mtok)
        self.assertEqual(st, 404)
        self.assertNotIn(b"desk", body)
        # agent/owner tokens -> 403
        st, _h, _b = req(self.url, "GET", "/openai/desk/stub/v1/models", token=self.toks["agent"])
        self.assertEqual(st, 403)

    def test_relay_offline_503(self):
        st, _h, body = req(self.url, "GET", "/openai/pod-a/qwen/v1/models", token=self.mtok)
        self.assertEqual(st, 503)
        self.assertIn("offline", json.loads(body)["error"]["message"])

    def _relay_roundtrip(self, req_body, reply, *, path="chat/completions", status=200, truncated=False):
        fake = getattr(self, "_fake", None)
        if fake is None:
            fake = self._fake = FakeRelay(self.url, self.rtok, "pod-a")
            fake.register()
        box = call_async(req, self.url, "POST", f"/openai/pod-a/qwen/v1/{path}", token=self.mtok, body=req_body)
        t = fake.poll_until(1)[0]
        self.assertEqual(t["action"], "model.forward")
        self.assertEqual(t["agent"], "gw/chatbox")
        self.assertEqual(t["params"]["timeout"], 1800)
        fake.result(t["task_id"], ok_env({"status": status, "content_type": "application/json",
                                          "body": json.dumps(reply), "truncated": truncated, "elapsed_s": 0.1}))
        box["thread"].join(15)
        return t, box["result"]

    def test_relay_stream_synthesis(self):
        reply = {"id": "c1", "object": "chat.completion", "created": 1, "model": "qwen",
                 "choices": [{"index": 0, "message": {"role": "assistant", "content": "a", "reasoning_content": "r",
                                                      "tool_calls": [{"id": "x", "type": "function",
                                                                      "function": {"name": "f", "arguments": "{}"}}]},
                              "finish_reason": "tool_calls"},
                             {"index": 1, "message": {"role": "assistant", "content": "b"}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        t, (st, h, body) = self._relay_roundtrip({"model": "qwen", "stream": True, "n": 2,
                                                  "stream_options": {"include_usage": True}}, reply)
        sent = json.loads(t["params"]["body"])
        self.assertEqual(sent["stream"], False)
        self.assertNotIn("stream_options", sent)
        self.assertEqual(st, 200)
        self.assertTrue(h["content-type"].startswith("text/event-stream"))
        events = [ln[6:] for ln in body.decode().split("\n") if ln.startswith("data: ")]
        self.assertEqual(events[-1], "[DONE]")
        chunks = [json.loads(e) for e in events[:-1]]
        self.assertEqual([c["choices"][0]["index"] for c in chunks[:2]], [0, 1])
        d0 = chunks[0]["choices"][0]["delta"]
        self.assertEqual((d0["reasoning_content"], d0["tool_calls"][0]["index"]), ("r", 0))
        self.assertEqual(chunks[2]["choices"], [])
        self.assertEqual(chunks[2]["usage"], reply["usage"])

    def test_relay_non_stream_and_400(self):
        _t, (st, h, body) = self._relay_roundtrip({"model": "qwen"}, {"choices": []})
        self.assertEqual((st, json.loads(body)), (200, {"choices": []}))
        _t, (st, h, body) = self._relay_roundtrip({"stream": True}, {"error": {"message": "bad"}}, status=400)
        self.assertEqual(st, 400)
        self.assertTrue(h["content-type"].startswith("application/json"))

    def test_relay_truncated_502(self):
        for stream in (False, True):
            _t, (st, _h, body) = self._relay_roundtrip({"stream": stream}, {}, truncated=True)
            self.assertEqual(st, 502)
            self.assertEqual(json.loads(body)["error"]["code"], "relay_truncated")


class TestSse(unittest.TestCase):
    def test_completions_synthesis(self):
        out = gateway.synthesize_sse({"id": "x", "model": "m", "choices": [{"index": 0, "text": "hi",
                                                                             "finish_reason": "stop"}]},
                                     "completions", False).decode()
        ev = [ln[6:] for ln in out.split("\n") if ln.startswith("data: ")]
        self.assertEqual(json.loads(ev[0])["object"], "text_completion")
        self.assertEqual(json.loads(ev[0])["choices"][0]["text"], "hi")
        self.assertEqual(ev[-1], "[DONE]")

    def test_paths_and_rewrite(self):
        self.assertEqual(gateway.normalize_path("v1/chat/completions"), "chat/completions")
        self.assertEqual(gateway.normalize_path("models"), "models")
        self.assertIsNone(gateway.normalize_path("v1/files"))
        b, s, u = gateway.rewrite_stream({"stream": True, "stream_options": {"include_usage": True}, "x": 1})
        self.assertEqual((b, s, u), ({"stream": False, "x": 1}, True, True))


if __name__ == "__main__":
    unittest.main()


class TestRelayGatewayRealNode(_GwCase):
    def test_relay_model_forward_end_to_end(self):
        from hive.actions import HANDLERS, load_all
        load_all()
        if "model.forward" not in HANDLERS:
            self.skipTest("model.forward handler not merged yet (WP3b)")
        from tests.wp5util import start_relay_node
        stub = self.start_stub()
        rtok = util.new_token()
        url, toks, hub = helpers.start_hub(
            [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(rtok)}], relay=dict(FAST_RELAY),
            extra_tokens=[("models", {"name": "chatbox", "kind": "models", "models": ["pod-a/stub"]})])
        self.addCleanup(hub.test_stop)
        rt = start_relay_node(url, rtok, start_managers=True,
                              model_endpoints=[{"name": "stub", "base_url": stub.url + "/v1", "api_key_env": None}])
        self.addCleanup(rt.test_stop)
        helpers.wait_until(lambda: hub.links["pod-a"].online(), 15)
        st, _h, body = req(url, "GET", "/openai/pod-a/stub/v1/models", token=toks["models"])
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["data"][0]["id"], "qwen-7b")
        st, h, body = req(url, "POST", "/openai/pod-a/stub/v1/chat/completions", token=toks["models"],
                          body={"model": "qwen-7b", "stream": True, "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(st, 200, body)
        self.assertTrue(h["content-type"].startswith("text/event-stream"))
        self.assertIn(b"hello there", body)
        self.assertTrue(body.rstrip().endswith(b"data: [DONE]"))
        sent = json.loads(stub.requests[-1]["body"])
        self.assertIs(sent.get("stream"), False)
