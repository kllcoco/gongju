"""M2 model gateway end to end (§8.7; the §21.2 WP5 "Gateway" list, run through real processes on loopback).

Machine "home" (one HIVE_HOME): hub ``gw-hub`` (gateway ``max_body_bytes`` 4096, ``max_inflight_per_token`` 1) and
node ``desk`` (direct link, alias ``box``, a ``model_endpoints`` entry ``ollama`` pointing at a stub the test runs
itself, i.e. not a managed service). Machine "pod" (another HIVE_HOME): node ``pod`` joined as a relay node. A third
relay node ``ghost`` is configured on the hub but never joins (offline). A stub OpenAI server is defined as service
``llm`` on desk and pod by an operator hub agent.

Covers: GET models; chat non-stream and stream over the direct link (SSE chunks arrive incrementally, the app's
Authorization never reaches the model server, CORS header on the response); relay link with stream:true →
synthesized chunks + [DONE] as text/event-stream (stream_options removed for a vLLM-style stub, include_usage chunk,
tool_calls with call indexes + reasoning_content, n=2 → indexes 0 and 1, completions as text_completion, an upstream
400 returned as JSON); the ``ollama`` model_endpoints name; embeddings; the /v1-less form; OpenAI-shaped errors (401,
403 outside the globs, 403 for an alias glob that does not match the canonical name, 404 unknown service without
naming other services or nodes, 404 unknown route, 413, 429 inflight, 503 offline); CORS preflight 204 without a
token; agent token → 403; models token on /v1/call → 403; the node's own /openai/self/… with a node models token and
with the hub token; ``X-Hive-Agent: gw/<token>`` in the node audit; hub audit rows with model/usage and no prompt
text; ``hive gateway url`` on the hub machine.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from hive import dist, proctree, util
from hive import client as hclient

from tests import helpers
from tests.integration import m2util
from tests.integration.m2util import IS_WINDOWS, ROOT, STUB_OPENAI, Supervisor, http, http_json, run_cli, wait_for


@unittest.skipIf(IS_WINDOWS, "POSIX E2E (the Windows variant runs in the lead's Windows CI)")
class TestE2EGateway(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="hivegw-")).resolve()
        cls.home, cls.user, cls.work = cls.base / "home", cls.base / "user", cls.base / "work"
        cls.pod_home, cls.pod_user = cls.base / "pod", cls.base / "pod-user"
        for d in (cls.home, cls.user, cls.work, cls.pod_home, cls.pod_user):
            d.mkdir()
        pypath = os.pathsep.join([str(ROOT)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
        cls.env = m2util.base_env(HIVE_HOME=str(cls.home), HOME=str(cls.user), PYTHONPATH=pypath)
        cls.pod_env = m2util.base_env(HIVE_HOME=str(cls.pod_home), HOME=str(cls.pod_user), PYTHONPATH=pypath)
        cls.sups: List[Supervisor] = []
        cls.pids: List[int] = []
        cls.ollama: Optional[subprocess.Popen] = None
        try:
            cls._setup()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def ok(cls, r: Any, what: str) -> str:
        if r.returncode != 0:
            raise AssertionError(f"{what} failed ({r.returncode}): {r.stdout}{r.stderr}")
        return r.stdout

    @classmethod
    def cli(cls, *argv: str, env: Optional[Dict[str, str]] = None) -> Any:
        return run_cli(argv, env=env or cls.env)

    @staticmethod
    def token_of(out: str) -> str:
        return re.search(r"^(hv_[A-Za-z0-9_-]+)$", out, re.M).group(1)

    @classmethod
    def _setup(cls) -> None:
        cls.hub_port, cls.node_port, cls.ollama_port = helpers.free_port(), helpers.free_port(), helpers.free_port()
        cls.hub_url, cls.node_url = f"http://127.0.0.1:{cls.hub_port}", f"http://127.0.0.1:{cls.node_port}"
        cls.stub = cls.work / "stub_openai.py"
        cls.stub.write_text(STUB_OPENAI, encoding="utf-8")
        cls.ollama = subprocess.Popen([sys.executable, str(cls.stub), str(cls.ollama_port), "ollama-model"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        cls.ok(cls.cli("init", "hub", "--yes", "--name", "gw-hub", "--listen", f"127.0.0.1:{cls.hub_port}"),
               "init hub")
        cls.ok(cls.cli("init", "node", "--yes", "--name", "desk", "--listen", f"127.0.0.1:{cls.node_port}",
                       "--root", str(cls.work), "--proxy", "off", "--mirror", "none"), "init node")
        out = cls.ok(cls.cli("hub", "add-node", "desk", "--direct", cls.node_url, "--generate"), "add-node direct")
        line = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("hive node token add")]
        cls.ok(cls.cli(*line[0].split()[1:]), "node token add --kind hub")
        out = cls.ok(cls.cli("hub", "add-node", "pod", "--relay", "--host", "10.255.0.1"), "add-node relay")
        cls.join = dist.decode_join(re.search(r"HIVE_JOIN='(hvj1\.[A-Za-z0-9_-]+=*)'", out).group(1))
        cls.join["u"] = cls.hub_url
        cls.ok(cls.cli("hub", "add-node", "ghost", "--relay", "--host", "10.255.0.1"), "add-node ghost")
        cls.op = cls.token_of(cls.ok(cls.cli("hub", "add-agent", "op", "--role", "operator", "--nodes", "*"),
                                     "add-agent"))
        cls.full = cls.token_of(cls.ok(cls.cli("gateway", "token", "add", "full", "--models", "*/*", "--hub"),
                                       "gateway token full"))
        cls.narrow = cls.token_of(cls.ok(cls.cli("gateway", "token", "add", "narrow", "--models", "desk/llm",
                                                 "--hub"), "gateway token narrow"))
        cls.boxonly = cls.token_of(cls.ok(cls.cli("gateway", "token", "add", "boxonly", "--models", "box/*",
                                                  "--hub"), "gateway token boxonly"))
        cls.node_models = cls.token_of(cls.ok(cls.cli("gateway", "token", "add", "app", "--models", "llm",
                                                      "--node"), "gateway token --node"))

        def hub_cfg(d: dict) -> None:
            d.setdefault("gateway", {}).update({"max_body_bytes": 4096, "max_inflight_per_token": 1})
            for n in d["nodes"]:
                if n["name"] == "desk":
                    n["aliases"] = ["box"]
        m2util.edit_json(cls.home / "hub" / "config.json", hub_cfg)
        m2util.edit_json(cls.home / "node" / "config.json", lambda d: d.__setitem__(
            "model_endpoints", [{"name": "ollama", "base_url": f"http://127.0.0.1:{cls.ollama_port}/v1"}]))
        m2util.allow_root(cls.home / "hub" / "config.json")
        m2util.allow_root(cls.home / "node" / "config.json")
        cls.hub_token = [n for n in json.loads((cls.home / "hub" / "config.json").read_text(encoding="utf-8"))["nodes"]
                         if n["name"] == "desk"][0]["token"]
        cls.start("hub", ["hub", "run"], cls.env)
        cls.start("desk", ["node", "run"], cls.env)
        wait_for(lambda: http_json("GET", cls.hub_url + "/v1/health", timeout=2)[1]["result"]["role"] == "hub", 60,
                 what="hub health")
        cls.ok(cls.cli("join", "--yes", "--no-pod", env=dict(cls.pod_env, HIVE_JOIN=dist.encode_join(cls.join))),
               "join")
        m2util.allow_root(cls.pod_home / "node" / "config.json")
        cls.start("pod", ["node", "run"], cls.pod_env)
        c = hclient.connect(cls.hub_url, token=cls.op)
        try:
            wait_for(lambda: all({n["name"]: n for n in c.nodes()}[x]["online"] for x in ("desk", "pod")), 60,
                     what="desk and pod online")
            for node, model in (("desk", "desk-model"), ("pod", "pod-model")):
                port = helpers.free_port()
                script = (cls.work if node == "desk" else cls.pod_user) / "stub_openai.py"
                script.write_text(STUB_OPENAI, encoding="utf-8")
                st = c.call("service.define", {
                    "name": "llm", "argv": [sys.executable, str(script), str(port), model], "port": port,
                    "endpoint": f"http://127.0.0.1:{port}/v1", "health": "http",
                    "health_url": f"http://127.0.0.1:{port}/health", "health_interval": 2, "wait": 30}, node=node)
                assert st["state"] == "running" and st["health"] == "healthy", st
                cls.pids += [p for p in (st["resources"]["pids"] or [])]
        finally:
            c.close()

    @classmethod
    def start(cls, name: str, argv: List[str], env: Dict[str, str]) -> None:
        cls.sups.append(Supervisor([sys.executable, "-m", "hive"] + argv, env=env, cwd=str(ROOT),
                                   log=cls.base / f"{name}.log", name=name).start())

    @classmethod
    def tearDownClass(cls) -> None:
        for sup in getattr(cls, "sups", []):
            if sup.running:
                sup.kill()
        for pid in getattr(cls, "pids", []):
            if pid and util.pid_alive(pid):
                try:
                    proctree.kill_tree(pid, force=True, grace=1)
                except Exception:  # noqa: BLE001
                    pass
        if getattr(cls, "ollama", None) is not None:
            cls.ollama.kill()
            cls.ollama.wait(10)
        if os.environ.get("HIVE_TEST_KEEP"):
            print("kept", cls.base)
        else:
            shutil.rmtree(str(cls.base), ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------------
    def gw(self, node: str, service: str = "llm", path: str = "chat/completions") -> str:
        return f"{self.hub_url}/openai/{node}/{service}/v1/{path}"

    def chat(self, content: str, **extra: Any) -> dict:
        return dict({"messages": [{"role": "user", "content": content}]}, **extra)

    def assert_openai_error(self, st: int, body: Any, status: int) -> dict:
        self.assertEqual(st, status, body)
        self.assertIsInstance(body, dict, body)
        self.assertEqual(set(body), {"error"}, body)
        err = body["error"]
        self.assertTrue({"message", "type", "code"} <= set(err), err)
        return err

    # -----------------------------------------------------------------------------------------------------------
    def test_direct_passthrough(self) -> None:
        st, body = http_json("GET", self.gw("desk", path="models"), token=self.full)
        self.assertEqual((st, body["data"][0]["id"]), (200, "desk-model"))
        r = http("POST", self.gw("desk"), token=self.full, body=self.chat("hello desk"))
        self.assertEqual(r.status, 200)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")
        reply = json.loads(r.read())
        r.close()
        self.assertEqual(reply["choices"][0]["message"]["content"], "pong: hello desk")   # no "[auth:"
        # stream: the node's SSE chunks pass through as they arrive
        r = http("POST", self.gw("desk"), token=self.full, body=self.chat("a b c", stream=True))
        self.assertTrue(r.headers.get("Content-Type", "").startswith("text/event-stream"))
        lines = m2util.sse_lines(r)
        r.close()
        data = [json.loads(ln[6:]) for _, ln in lines if ln.startswith("data: {")]
        self.assertEqual("".join(d["choices"][0]["delta"]["content"] for d in data), "pong: a b c")
        self.assertEqual(lines[-1][1], "data: [DONE]")
        self.assertGreater(lines[-1][0] - lines[0][0], 1.0, lines)
        # an upstream 400 is passed through unchanged
        st, body = http_json("POST", self.gw("desk"), token=self.full, body=self.chat("fail400"))
        self.assertEqual((st, body["error"]["code"]), (400, "stub_400"))
        # the /v1-less form and embeddings
        st, body = http_json("GET", f"{self.hub_url}/openai/desk/llm/models", token=self.full)
        self.assertEqual(st, 200, body)
        st, body = http_json("POST", self.gw("desk", path="embeddings"), token=self.full,
                             body={"model": "desk-model", "input": "x"})
        self.assertEqual((st, body["data"][0]["embedding"]), (200, [0.1, 0.2]))
        # the alias resolves to desk for a */* token
        st, body = http_json("GET", self.gw("box", path="models"), token=self.full)
        self.assertEqual((st, body["data"][0]["id"]), (200, "desk-model"))

    def test_relay_synthesis(self) -> None:
        # non-stream through model.forward
        st, body = http_json("POST", self.gw("pod"), token=self.full, body=self.chat("hello pod"))
        self.assertEqual((st, body["choices"][0]["message"]["content"]), (200, "pong: hello pod"))
        # stream + stream_options against a vLLM-style stub: rewritten, synthesized, include_usage chunk
        r = http("POST", self.gw("pod"), token=self.full,
                 body=self.chat("relay me", stream=True, stream_options={"include_usage": True}))
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers.get("Content-Type", "").startswith("text/event-stream"))
        chunks = [ln for _, ln in m2util.sse_lines(r)]
        r.close()
        self.assertEqual(chunks[-1], "data: [DONE]")
        first, usage = json.loads(chunks[0][6:]), json.loads(chunks[1][6:])
        self.assertEqual(first["object"], "chat.completion.chunk")
        self.assertEqual(first["choices"][0]["delta"]["content"], "pong: relay me")
        self.assertEqual(first["choices"][0]["finish_reason"], "stop")
        self.assertEqual((usage["choices"], usage["usage"]["total_tokens"]), ([], 5))
        # tool_calls (each with its index) and reasoning_content stay in the delta
        r = http("POST", self.gw("pod"), token=self.full, body=self.chat("tools", stream=True))
        chunks = [ln for _, ln in m2util.sse_lines(r)]
        r.close()
        delta = json.loads(chunks[0][6:])["choices"][0]["delta"]
        self.assertEqual([tc["index"] for tc in delta["tool_calls"]], [0, 1])
        self.assertEqual(delta["tool_calls"][1]["function"]["name"], "f1")
        self.assertEqual(delta["reasoning_content"], "thinking")
        self.assertEqual(json.loads(chunks[0][6:])["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(len(chunks), 2)                                # no usage chunk unless asked
        # n=2 -> two chunks with indexes 0 and 1
        r = http("POST", self.gw("pod"), token=self.full, body=self.chat("two", stream=True, n=2))
        chunks = [json.loads(ln[6:]) for _, ln in m2util.sse_lines(r) if ln.startswith("data: {")]
        r.close()
        self.assertEqual([ch["choices"][0]["index"] for ch in chunks], [0, 1])
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "pong: two #1")
        # completions -> text_completion chunks
        r = http("POST", self.gw("pod", path="completions"), token=self.full,
                 body={"model": "pod-model", "prompt": "complete me", "stream": True})
        chunks = [ln for _, ln in m2util.sse_lines(r)]
        r.close()
        c0 = json.loads(chunks[0][6:])
        self.assertEqual((c0["object"], c0["choices"][0]["text"]), ("text_completion", "pong: complete me"))
        # an upstream 400 on a stream request comes back as JSON, not SSE
        r = http("POST", self.gw("pod"), token=self.full, body=self.chat("fail400", stream=True))
        self.assertEqual(r.status, 400)
        self.assertTrue(r.headers.get("Content-Type", "").startswith("application/json"), r.headers)
        self.assertEqual(json.loads(r.read())["error"]["code"], "stub_400")
        r.close()
        st, body = http_json("POST", self.gw("pod", path="embeddings"), token=self.full,
                             body={"model": "pod-model", "input": "x"})
        self.assertEqual((st, body["data"][0]["embedding"]), (200, [0.1, 0.2]))

    def test_model_endpoints_name(self) -> None:
        st, body = http_json("POST", self.gw("desk", "ollama"), token=self.full, body=self.chat("hi ollama"))
        self.assertEqual(st, 200, body)
        self.assertEqual((body["model"], body["choices"][0]["message"]["content"]), ("ollama-model", "pong: hi ollama"))

    def test_errors(self) -> None:
        # 401: no token / unknown token
        st, body = http_json("POST", self.gw("desk"), body=self.chat("x"))
        self.assert_openai_error(st, body, 401)
        st, body = http_json("POST", self.gw("desk"), token="hv_" + "A" * 43, body=self.chat("x"))
        self.assert_openai_error(st, body, 401)
        # 403: outside the token's globs; an alias glob never matches the canonical name
        st, body = http_json("POST", self.gw("pod"), token=self.narrow, body=self.chat("x"))
        self.assert_openai_error(st, body, 403)
        st, body = http_json("GET", self.gw("box", path="models"), token=self.boxonly)
        self.assert_openai_error(st, body, 403)
        st, body = http_json("GET", self.gw("desk", path="models"), token=self.boxonly)
        self.assert_openai_error(st, body, 403)
        # 404: unknown service (direct and relay link)
        for node in ("desk", "pod"):
            st, body = http_json("POST", self.gw(node, "nope"), token=self.full, body=self.chat("x"))
            err = self.assert_openai_error(st, body, 404)
            self.assertNotIn("ollama", err["message"].replace("ollama-model", ""), err)
        # v8 (T18): a models token for desk/llm asking for desk/other or an unknown node: 403 (§8.7 step 1, the
        # pair is outside its globs) or 404, never naming another service or node
        for path in ("/openai/desk/other/v1/models", "/openai/pod/llm/v1/models", "/openai/pod-x/llm/v1/models",
                     "/openai/ghost/llm/v1/models"):
            st, body = http_json("GET", self.hub_url + path, token=self.narrow)
            self.assertIn(st, (403, 404), (path, body))
            err = self.assert_openai_error(st, body, st)
            for other in ("ollama", "ghost", "box", "nope"):
                if other not in path:
                    self.assertNotIn(other, err["message"], (path, err))
            if "pod" not in path:
                self.assertNotIn("pod", err["message"], (path, err))
        st, body = http_json("GET", self.hub_url + "/openai/pod-x/llm/v1/models", token=self.narrow)
        self.assertEqual(st, 404, body)
        # unknown route below /openai/
        st, body = http_json("POST", self.gw("desk", path="images/generations"), token=self.full, body={})
        self.assert_openai_error(st, body, 404)
        # 413: body over the hub's max_body_bytes (4096)
        st, body = http_json("POST", self.gw("desk"), token=self.full, body=self.chat("x" * 5000))
        self.assert_openai_error(st, body, 413)
        # 503: a configured relay node that never joined
        st, body = http_json("POST", self.gw("ghost"), token=self.full, body=self.chat("x"))
        self.assert_openai_error(st, body, 503)
        # 429: max_inflight_per_token 1 -> a second concurrent request of the same token
        slow: Dict[str, Any] = {}
        t = threading.Thread(target=lambda: slow.update(r=http_json("POST", self.gw("desk"), token=self.full,
                                                                    body=self.chat("slow one"))))
        t.start()
        time.sleep(1.0)
        r = http("POST", self.gw("desk"), token=self.full, body=self.chat("x"))
        st, body = r.status if hasattr(r, "status") else r.code, json.loads(r.read())
        retry = r.headers.get("Retry-After")
        r.close()
        t.join(30)
        self.assert_openai_error(st, body, 429)
        self.assertTrue(retry, "Retry-After missing")
        self.assertEqual(slow["r"][0], 200, slow)
        # another token has its own cap
        st, body = http_json("POST", self.gw("desk"), token=self.narrow, body=self.chat("y"))
        self.assertEqual(st, 200, body)

    def test_cors_and_tokens(self) -> None:
        r = http("OPTIONS", self.gw("desk"), headers={"Origin": "https://chat.example", "Access-Control-Request-Method":
                                                      "POST", "Access-Control-Request-Headers":
                                                      "authorization, content-type, x-stainless-os"})
        self.assertEqual(r.status, 204)
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")
        self.assertIn("x-stainless-os", r.headers.get("Access-Control-Allow-Headers", ""))
        self.assertIsNone(r.headers.get("Access-Control-Allow-Credentials"))
        r.close()
        st, body = http_json("POST", self.gw("desk"), body=self.chat("x"),
                             headers={"Origin": "https://chat.example"})
        self.assert_openai_error(st, body, 401)
        # a browser app with a models token: the Origin check is skipped on /openai/*
        st, body = http_json("GET", self.gw("desk", path="models"), token=self.full,
                             headers={"Origin": "https://chat.example"})
        self.assertEqual(st, 200, body)
        # agent and owner tokens cannot use the gateway; models tokens cannot use /v1/*
        owner = (self.home / "tokens" / "hub-owner.token").read_text(encoding="utf-8").strip()
        for tok in (self.op, owner):
            st, body = http_json("GET", self.gw("desk", path="models"), token=tok)
            self.assert_openai_error(st, body, 403)
        st, body = http_json("POST", self.hub_url + "/v1/call", token=self.full,
                             body={"action": "model.forward", "node": "desk",
                                   "params": {"service": "llm", "path": "models"}})
        self.assertEqual(st, 403, body)
        # the node's own gateway: a node models token (globs "llm") and the hub token
        node_gw = f"{self.node_url}/openai/self/llm/v1/chat/completions"
        st, body = http_json("POST", node_gw, token=self.node_models, body=self.chat("node direct"))
        self.assertEqual((st, body["choices"][0]["message"]["content"]), (200, "pong: node direct"))
        st, body = http_json("POST", f"{self.node_url}/openai/self/ollama/v1/chat/completions",
                             token=self.node_models, body=self.chat("x"))
        self.assert_openai_error(st, body, 403)
        st, body = http_json("POST", node_gw, token=self.hub_token, body=self.chat("via hub token"))
        self.assertEqual(st, 200, body)
        st, body = http_json("POST", f"{self.node_url}/openai/desk/llm/v1/chat/completions", token=self.node_models,
                             body=self.chat("own name"))
        self.assertEqual(st, 200, body)
        st, body = http_json("POST", self.node_url + "/v1/call", token=self.node_models,
                             body={"action": "node.info", "params": {}})
        self.assertEqual(st, 403, body)

    def test_zz_audit_and_url(self) -> None:
        http_json("POST", self.gw("desk"), token=self.full, body=self.chat("audit-secret-prompt"))
        http_json("POST", self.gw("pod"), token=self.full, body=self.chat("audit-secret-prompt"))

        def rows(d: Path) -> List[dict]:
            out: List[dict] = []
            for f in sorted(d.glob("audit-*.jsonl")):
                out += [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
            return out
        hub_rows = wait_for(lambda: [r for r in rows(self.home / "hub" / "audit") if r.get("kind") == "gateway"],
                            10, what="hub gateway audit")
        text = json.dumps(hub_rows)
        self.assertNotIn("audit-secret-prompt", text)
        self.assertNotIn("pong:", text)
        ok_rows = [r for r in hub_rows if (r.get("extra") or {}).get("usage")]
        self.assertTrue(ok_rows, hub_rows[-2:])
        self.assertTrue(any((r.get("extra") or {}).get("model") for r in hub_rows), hub_rows[-2:])
        self.assertTrue({"desk", "pod"} <= {r.get("node") for r in ok_rows}, ok_rows)   # direct link too
        self.assertNotIn("box", {r.get("node") for r in hub_rows})                       # canonical names
        node_rows = [r for r in rows(self.home / "node" / "audit") if r.get("kind") == "gateway"]
        self.assertEqual({r.get("node") for r in node_rows}, {"desk"})                   # not "self"
        self.assertTrue(any(r.get("agent") == "gw/full" for r in node_rows),
                        [(r.get("identity"), r.get("agent")) for r in node_rows][-5:])
        self.assertNotIn("audit-secret-prompt", json.dumps(node_rows))
        r = self.cli("gateway", "url", "desk", "llm")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"http://127.0.0.1:{self.hub_port}/openai/desk/llm/v1", r.stdout)
        self.assertIn(f"http://host.docker.internal:{self.hub_port}/openai/desk/llm/v1", r.stdout)
        self.assertIn(f"/openai/desk/llm\n", r.stdout + "\n")


if __name__ == "__main__":
    unittest.main()
