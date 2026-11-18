"""WP3a tests: model.endpoints and model.chat against StubHTTPServer OpenAI stubs (design §9.9, §21.2)."""
from __future__ import annotations

import json
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

from hive import catalog, schema
from hive.actions import HANDLERS, load_all
from hive.actions import model as model_mod
from hive.errors import HiveError
from hive.netutil import AuthSpec, NetGuard
from tests import helpers, wp1util

load_all()


class FakeServices:
    """ServiceManager stand-in: endpoint_for + endpoints (§10.5 shapes)."""

    def __init__(self, defs: Dict[str, Tuple[str, str, Optional[AuthSpec]]], server: str = "vllm"):
        self.defs = defs
        self.server = server

    def endpoint_for(self, name: str) -> Tuple[str, str, Optional[AuthSpec]]:
        if name not in self.defs:
            raise helpers.not_found(f"model service '{name}' not found")
        return self.defs[name]

    def endpoints(self) -> List[dict]:
        return [{"name": n, "source": "service", "api": api, "server": self.server, "base_url": base,
                 "healthy": True, "models": None} for n, (base, api, _a) in sorted(self.defs.items())]


class ModelCase(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = helpers.StubHTTPServer().start()
        self.srv._httpd.handle_error = lambda *a: None
        self.addCleanup(self.srv.stop)
        self.rt = helpers.FakeRuntime()
        self.addCleanup(self.rt.cleanup)
        self.pol = helpers.FakeEffectivePolicy(workdir=self.rt.workdir, data_dir=self.rt.data_dir)

    def call(self, action: str, params: dict, policy: Any = None, ident: Any = None, timeout: float = 30) -> dict:
        ctx = helpers.make_ctx(action, runtime=self.rt, policy=policy or self.pol, identity=ident, timeout=timeout)
        out = HANDLERS[action](ctx, schema.validate(catalog.SPECS[action].params, params))
        schema.validate(catalog.SPECS[action].result, out, coerce=False, defaults=False, path="result")
        return out

    def err(self, action: str, params: dict, **kw: Any) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.call(action, params, **kw)
        return cm.exception


class TestChat(ModelCase):
    def test_chat_port(self):
        self.srv.openai(models=("qwen", "other"), reply="hello there friend")
        r = self.call("model.chat", {"port": self.srv.port, "prompt": "hi", "system": "be nice"})
        self.assertEqual((r["content"], r["model"], r["finish_reason"]), ("hello there friend", "qwen", "stop"))
        self.assertEqual(r["usage"], {"prompt_tokens": 3, "completion_tokens": 3})
        self.assertEqual(r["base_url"], f"http://127.0.0.1:{self.srv.port}/v1")
        self.assertIsNotNone(r["tokens_per_s"])
        sent = json.loads(self.srv.requests[-1]["body"])
        self.assertEqual(sent["messages"], [{"role": "system", "content": "be nice"},
                                            {"role": "user", "content": "hi"}])
        self.assertEqual((sent["stream"], sent["max_tokens"], sent["model"]), (False, 512, "qwen"))
        self.assertIsNone(self.srv.requests[-1]["headers"].get("Authorization"))
        r = self.call("model.chat", {"base_url": self.srv.url + "/v1/", "model": "other",
                                     "messages": [{"role": "user", "content": "x"}], "temperature": 0.2,
                                     "stop": ["\n"], "extra_json": '{"top_k": 5}', "max_chars": 100})
        sent = json.loads(self.srv.requests[-1]["body"])
        self.assertEqual((sent["model"], sent["temperature"], sent["stop"], sent["top_k"]), ("other", 0.2, ["\n"], 5))

    def test_reasoning_and_truncation(self):
        def chat(req: dict) -> Any:
            return 200, {}, {"model": "m", "choices": [{"message": {"content": "x" * 500,
                                                                    "reasoning_content": "think " * 1000},
                                                        "finish_reason": "length"}]}
        self.srv.add_handler("POST", "/v1/chat/completions", chat)
        r = self.call("model.chat", {"port": self.srv.port, "model": "m", "prompt": "p", "max_chars": 100})
        self.assertLessEqual(len(r["content"]), 160)
        self.assertLessEqual(len(r["reasoning"]), 2100)
        self.assertEqual((r["usage"], r["tokens_per_s"], r["finish_reason"]), (None, None, "length"))

    def test_bad_params(self):
        for params in ({"base_url": "http://127.0.0.1:1/v1?x=1", "prompt": "p"},
                       {"base_url": "http://u:p@127.0.0.1:1/v1", "prompt": "p"},
                       {"base_url": "ftp://x/v1", "prompt": "p"},
                       {"port": 1, "service": "a", "prompt": "p"},
                       {"port": 1, "base_url": "http://127.0.0.1:2/v1", "prompt": "p"},
                       {"port": 1, "prompt": "p", "extra_json": '{"model": "x"}'},
                       {"port": 1, "prompt": "p", "extra_json": '{"stream": true}'},
                       {"port": 1, "prompt": "p", "extra_json": '[1]'},
                       {"port": 1, "prompt": "p", "extra_json": '{bad'},
                       {"port": 1, "prompt": "p", "extra_json": "[" * 70 + "]" * 70}):
            with self.subTest(params=params):
                self.assertEqual(self.err("model.chat", params).code, "bad_request")
        e = self.err("model.chat", {"port": 1, "prompt": "p", "extra_json": '{"messages": []}'})
        self.assertIn("messages", e.message)

    def test_errors(self):
        port = helpers.free_port()
        e = self.err("model.chat", {"port": port, "model": "m", "prompt": "p"})
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "connect", False))
        self.assertEqual(e.hint, f"nothing listening on 127.0.0.1:{port}; check service_list / service_logs")
        self.srv.openai(models=("real-model",))
        self.srv.add("POST", "/v1/chat/completions", 404, {"error": {"message": "model not found"}})
        e = self.err("model.chat", {"port": self.srv.port, "model": "nope", "prompt": "p"})
        self.assertEqual((e.code, e.details["models"]), ("not_found", ["real-model"]))
        self.srv.add("POST", "/v1/chat/completions", 500, "boom")
        e = self.err("model.chat", {"port": self.srv.port, "model": "m", "prompt": "p"})
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"], e.details["status"]),
                         ("unavailable", "upstream", True, 500))
        self.assertIn("boom", e.details["body"])
        self.srv.add("POST", "/v1/chat/completions", 200, "not json")
        self.assertEqual(self.err("model.chat", {"port": self.srv.port, "model": "m", "prompt": "p"}).code,
                         "unavailable")

    def test_service_target_and_auth(self):
        self.srv.openai(models=("svc-model",))
        auth = AuthSpec(name="VLLM_KEY", header="Authorization", value="Bearer local-key-123", hosts=("loopback",))
        self.rt.services = FakeServices({"qwen": (self.srv.url + "/v1", "openai", auth)})
        r = self.call("model.chat", {"service": "qwen", "prompt": "hi"})
        self.assertEqual(r["model"], "svc-model")
        self.assertEqual(self.srv.requests[-1]["headers"].get("Authorization"), "Bearer local-key-123")
        self.assertEqual(self.err("model.chat", {"service": "missing", "prompt": "hi"}).code, "not_found")
        # model_endpoints fallback when no service manager is loaded
        self.rt.services = None
        self.pol.model_endpoints = [{"name": "lm", "base_url": self.srv.url + "/v1", "api_key_env": None}]
        self.assertEqual(self.call("model.chat", {"service": "lm", "prompt": "hi"})["model"], "svc-model")

    def test_auto_target(self):
        self.srv.openai(models=("auto-model",))
        with mock.patch.object(model_mod, "PROBE_PORTS", (self.srv.port,)):
            r = self.call("model.chat", {"prompt": "hi"})
            self.assertEqual(r["model"], "auto-model")
        with mock.patch.object(model_mod, "PROBE_PORTS", (helpers.free_port(),)):
            e = self.err("model.chat", {"prompt": "hi"})
            self.assertEqual(e.code, "bad_request")
            self.assertIn("service, port or base_url", e.hint)

    def test_gated_identity_loopback_write(self):
        """rule 5a for purpose model (v7): a gated identity reaches only known endpoints with POST."""
        self.srv.openai(models=("m",))
        with wp1util.FakeHome() as fh:
            eps = [{"name": "known", "base_url": self.srv.url + "/v1", "api_key_env": None}]
            pol = wp1util.build_policy(fh, guard=NetGuard, model_endpoints=eps)
            gated = pol.effective(wp1util.READNET, paused=False)
            r = self.call("model.chat", {"service": "known", "prompt": "hi", "model": "m"}, policy=gated,
                          ident=wp1util.READNET)
            self.assertEqual(r["content"], "hello from stub")
            other = helpers.StubHTTPServer().start()
            self.addCleanup(other.stop)
            other.openai(models=("m",))
            e = self.err("model.chat", {"port": other.port, "prompt": "hi", "model": "m"}, policy=gated,
                         ident=wp1util.READNET)
            self.assertEqual((e.code, e.details.get("rule")), ("policy_denied", "loopback_write"))
            op = pol.effective(wp1util.OPERATOR, paused=False)
            self.assertEqual(self.call("model.chat", {"port": other.port, "prompt": "hi", "model": "m"}, policy=op,
                                       ident=wp1util.OPERATOR)["model"], "m")


class TestEndpoints(ModelCase):
    def test_port_and_service_listing(self):
        self.srv.add("GET", "/v1/models", 200, {"data": [{"id": "a", "owned_by": "vllm"}, {"id": "b"}]})
        r = self.call("model.endpoints", {"port": self.srv.port})
        self.assertEqual(r["models"], [{"id": "a", "owned_by": "vllm"}, {"id": "b", "owned_by": None}])
        row = r["endpoints"][0]
        self.assertEqual((row["source"], row["api"], row["server"], row["healthy"], row["models"]),
                         ("probe", "openai", "vllm", True, ["a", "b"]))
        self.rt.services = FakeServices({"qwen": (self.srv.url + "/v1", "openai", None)}, server="sglang")
        r = self.call("model.endpoints", {"service": "qwen"})
        self.assertEqual((r["endpoints"][0]["name"], r["endpoints"][0]["source"], r["endpoints"][0]["server"]),
                         ("qwen", "service", "sglang"))
        self.assertEqual(self.err("model.endpoints", {"service": "qwen", "port": 1}).code, "bad_request")
        port = helpers.free_port()
        e = self.err("model.endpoints", {"port": port})
        self.assertEqual((e.code, e.details["reason"]), ("unavailable", "connect"))

    def test_ollama_tags_fallback(self):
        self.srv.add("GET", "/api/tags", 200, {"models": [{"name": "llama3:8b"}, {"name": "qwen2:7b"}]})
        self.pol.model_endpoints = [{"name": "ollama", "base_url": self.srv.url + "/v1", "api_key_env": None}]
        self.rt.services = FakeServices({"ollama": (self.srv.url + "/v1", "ollama", None)}, server="ollama")
        r = self.call("model.endpoints", {"service": "ollama"})
        self.assertEqual([m["id"] for m in r["models"]], ["llama3:8b", "qwen2:7b"])

    def test_listing_dedupe_and_server_detection(self):
        self.srv.add("GET", "/v1/models", 200, {"data": [{"id": "x", "owned_by": "llamacpp"}]})
        probe2 = helpers.StubHTTPServer().start()
        self.addCleanup(probe2.stop)
        probe2.add("GET", "/v1/models", 200, {"data": [{"id": "y", "owned_by": "someone"}]})
        dead = helpers.free_port()
        self.pol.model_endpoints = [{"name": "cfg", "base_url": self.srv.url + "/v1", "api_key_env": None},
                                    {"name": "remote", "base_url": "https://api.example.com/v1", "api_key_env": None},
                                    {"name": "gone", "base_url": f"http://127.0.0.1:{dead}/v1", "api_key_env": None}]
        with mock.patch.object(model_mod, "PROBE_PORTS", (self.srv.port, probe2.port, helpers.free_port())):
            rows = self.call("model.endpoints", {})["endpoints"]
        by = {r["base_url"]: r for r in rows}
        self.assertEqual(len(rows), 4)                   # cfg (deduplicated with the probe), remote, gone, probe2
        cfg = by[self.srv.url + "/v1"]
        self.assertEqual((cfg["name"], cfg["source"], cfg["server"], cfg["healthy"], cfg["models"]),
                         ("cfg", "config", "llamacpp", True, ["x"]))
        remote = by["https://api.example.com/v1"]
        self.assertEqual((remote["healthy"], remote["models"]), (None, None))      # not probed (read risk)
        gone = by[f"http://127.0.0.1:{dead}/v1"]
        self.assertEqual((gone["healthy"], gone["server"]), (False, "unknown"))
        p2 = by[probe2.url + "/v1"]
        self.assertEqual((p2["source"], p2["server"], p2["name"]), ("probe", "openai", None))
        rows = self.call("model.endpoints", {"probe": False})["endpoints"]
        self.assertEqual({r["source"] for r in rows}, {"config"})
        self.assertEqual(self.call("model.endpoints", {"probe": False})["endpoints"][0]["healthy"], None)


if __name__ == "__main__":
    unittest.main()
