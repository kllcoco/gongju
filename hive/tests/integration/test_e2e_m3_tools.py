"""M3 (§22.1 step 6): every tool of the ``full`` profile end to end, and the binding counts.

One real node daemon; ``hive mcp --local --profile full`` (agent token ``this-pc``) calls each of the 31 tools at least
once against it (stub web server, stub OpenAI service, ``hfstub`` for the pull dry run); a process started by the
test is killed with ``proc_kill``. Then the counts of §9 / §17 / §18 are verified end to end:

* 38 actions (37 node-scope on the node's ``GET /v1/actions``, + ``hub.nodes``);
* tool profiles 10 / 28 / 31 with exactly the §17.2 names, through ``tools/list`` of three MCP sessions;
* 4 export formats through the node's ``GET /v1/tools?format=…&profile=…`` (shape per §17.7, same names);
* the 3 MCP protocol versions (``title`` only from 2025-06-18).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from typing import List

from hive import catalog, mcp as hmcp, schema, tools

from tests import helpers
from tests.fixtures.hfstub import HFStub, Repo
from tests.integration import m2util
from tests.integration.m3util import IS_WINDOWS, McpSession, NodeDaemon, http_json, job_id_of, wait_for
from tests.test_mcp import McpProc
from tests.test_mcp_http import init_msg, post as mcp_post

MINIMAL = ["nodes_list", "sys_info", "shell_run", "job_start", "job_status", "fs_list", "fs_read", "fs_write",
           "fs_edit", "guide"]
STANDARD = MINIMAL + ["job_logs", "job_stop", "job_list", "fs_search", "fs_op", "fs_transfer", "proc_list", "proc_kill",
                      "service_list", "service_define", "service_control", "service_status", "service_logs",
                      "model_chat", "model_pull", "http_request", "net_download", "port_check"]
FULL = STANDARD + ["model_list", "list_actions", "call_action"]
PY = sys.executable


class TestE2EM3Tools(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = None
        cls.web = helpers.StubHTTPServer()
        cls.web.add("GET", "/hello", 200, "hello m3")
        cls.web.add_file("/blob.bin", b"B" * 5000)
        cls.web.start()
        cls.hf = HFStub({"org/tools": Repo({"config.json": b"{}", "model.safetensors": b"w" * 3000})}).start()
        cls.extra: List[subprocess.Popen] = []
        try:
            def cfg(raw: dict) -> None:
                raw.setdefault("models", {}).update(hf_endpoint=cls.hf.url, dir="~/models")
            def tokens(node: NodeDaemon) -> None:
                cls.hub_tok = node.add_token("hub-m3", "--kind", "hub")
            cls.node = NodeDaemon("m3tools", init_args=("--proxy", "off", "--mirror", "none"), cfg_edit=cfg,
                                  before_run=tokens)
            cls.node.start()
            cls.mcp = cls.node.mcp("--profile", "full")
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.node is not None:
            cls.node.stop()
        for p in cls.extra:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        cls.web.stop()
        cls.hf.stop()

    def tool(self, name: str, args: dict, *, error: bool = False) -> str:
        text, is_err = self.mcp.tool(name, args)
        self.assertEqual(is_err, error, f"{name}: {text}")
        self.used.add(name)
        return text

    # -------------------------------------------------------------------------------------------------------------
    def test_every_full_tool(self) -> None:
        self.used = set()
        n, w = self.node, self.node.work
        port = helpers.free_port()

        self.assertIn("m3tools", self.tool("nodes_list", {}))
        text = self.tool("sys_info", {"sections": ["os", "software"], "which": ["python3" if not IS_WINDOWS
                                                                                else "python"]})
        self.assertIn("python", text.lower())
        self.assertIn("m3-hello", self.tool("shell_run", {"command": "echo m3-hello"}))

        # jobs
        text = self.tool("job_start", {"argv": [PY, "-c", "print('job-out-1'); print('job-out-2')"], "name": "short"})
        jid = job_id_of(text)
        self.assertIn("exit 0", self.tool("job_status", {"job_id": jid, "wait": 30}))
        self.assertIn("job-out-2", self.tool("job_logs", {"job_id": jid}))
        long_id = job_id_of(self.tool("job_start", {"argv": [PY, "-c", "import time; time.sleep(300)"],
                                                     "name": "sleeper"}))
        self.assertIn(long_id, self.tool("job_list", {"state": "running"}))
        self.tool("job_stop", {"job_id": long_id})
        self.assertIn("killed", self.tool("job_status", {"job_id": long_id, "wait": 20}))

        # files
        f = w / "notes" / "a.txt"
        self.tool("fs_write", {"path": str(f), "content": "alpha\nbeta\ngamma\n", "make_dirs": True})
        self.assertIn("beta", self.tool("fs_read", {"path": str(f)}))
        self.tool("fs_edit", {"path": str(f), "old_text": "beta", "new_text": "BETA"})
        self.assertEqual(f.read_text(encoding="utf-8"), "alpha\nBETA\ngamma\n")
        self.assertIn("a.txt", self.tool("fs_list", {"path": str(w / "notes")}))
        self.assertIn("BETA", self.tool("fs_search", {"pattern": "BETA", "path": str(w)}))
        self.tool("fs_op", {"op": "mkdir", "path": str(w / "d2")})
        self.tool("fs_op", {"op": "copy", "path": str(f), "dst": str(w / "d2" / "b.txt")})
        self.tool("fs_op", {"op": "move", "path": str(w / "d2" / "b.txt"), "dst": str(w / "d2" / "c.txt")})
        self.assertIn("c.txt", self.tool("fs_op", {"op": "stat", "path": str(w / "d2" / "c.txt"), "sha256": True}))
        self.tool("fs_op", {"op": "delete", "path": str(w / "d2"), "recursive": True})
        self.assertFalse((w / "d2").exists())
        local = n.user / "Hive" / "local-src.bin"            # client.json local_files.roots default: ~/Hive
        local.parent.mkdir(exist_ok=True)
        local.write_bytes(os.urandom(100_000))
        text = self.tool("fs_transfer", {"src": f"local:{local}", "dst": f"m3tools:{w / 'up.bin'}"})
        wait_for(lambda: (w / "up.bin").exists() and (w / "up.bin").read_bytes() == local.read_bytes(), 30,
                 what=f"fs_transfer ({text})")

        # processes
        victim = subprocess.Popen([PY, "-c", "import time; time.sleep(300)"])
        self.extra.append(victim)
        self.assertIn(str(victim.pid), self.tool("proc_list", {"pids": [victim.pid], "cmdline": True}))
        self.tool("proc_kill", {"pid": victim.pid})
        self.assertIsNotNone(victim.wait(15))

        # services + models
        script = w / "bin" / "stub_openai.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text(m2util.STUB_OPENAI, encoding="utf-8")
        self.tool("service_define", {"name": "llm", "port": port, "endpoint": f"http://127.0.0.1:{port}/v1",
                                     "options": ["argv=" + json.dumps([PY, str(script), str(port), "m3-model"]),
                                                 "endpoint_kind=openai", "health=http",
                                                 f"health_url=http://127.0.0.1:{port}/health", "health_interval=2"],
                                     "wait": 60})
        self.assertIn("llm", self.tool("service_list", {}))
        self.assertIn("healthy", self.tool("service_status", {"name": "llm", "wait": 30}))
        self.assertIn("stub listening", self.tool("service_logs", {"name": "llm"}))
        self.assertIn("m3-model", self.tool("model_list", {"service": "llm"}))
        self.assertIn("pong: hi m3", self.tool("model_chat", {"service": "llm", "prompt": "hi m3"}))
        self.tool("service_control", {"op": "restart", "name": "llm", "wait": 60})
        self.assertIn(str(port), self.tool("port_check", {"ports": [port]}))
        text = self.tool("call_action", {"action": "model.forward",
                                         "params_json": json.dumps({"service": "llm", "path": "models"})})
        self.assertIn("m3-model", text)
        self.assertIn("m3tools", self.tool("call_action", {"action": "node.info", "params_json": "{}"}))
        self.tool("service_control", {"op": "stop", "name": "llm", "wait": 30})
        self.tool("service_control", {"op": "remove", "name": "llm", "wait": 30})

        # network
        self.assertIn("hello m3", self.tool("http_request", {"url": f"http://127.0.0.1:{self.web.port}/hello"}))
        jid = job_id_of(self.tool("net_download", {"url": f"http://127.0.0.1:{self.web.port}/blob.bin",
                                                    "path": str(w / "dl" / "blob.bin"), "make_dirs": True}))
        self.tool("job_status", {"job_id": jid, "wait": 30})
        wait_for(lambda: (w / "dl" / "blob.bin").exists(), 20, what="download")
        self.assertEqual((w / "dl" / "blob.bin").read_bytes(), b"B" * 5000)
        self.assertIn("org/tools", self.tool("model_pull", {"repo": "org/tools", "dry_run": True}))

        # docs and discovery
        self.assertIn("preset", self.tool("guide", {"topic": "presets"}).lower())
        self.assertTrue(self.tool("guide", {}))
        text = self.tool("list_actions", {})
        for a in ("shell.run", "model.pull", "model.forward", "fs.write_chunk"):
            self.assertIn(a, text)

        self.assertEqual(sorted(self.used), sorted(FULL), set(FULL) - self.used)

    # -------------------------------------------------------------------------------------------------------------
    def test_counts(self) -> None:
        # 38 actions: 37 node-scope (the node's /v1/actions) + hub.nodes
        self.assertEqual(len(catalog.SPECS), 38)
        self.assertEqual(sorted(a for a, s in catalog.SPECS.items() if s.scope == "hub"), ["hub.nodes"])
        st, res = http_json("GET", self.node.url + "/v1/actions", token=self.node.agent)
        self.assertEqual(st, 200, res)
        rows = res["result"]["actions"] if isinstance(res["result"], dict) else res["result"]
        names = sorted(r["name"] for r in rows)
        self.assertEqual(len(names), 37, names)
        self.assertEqual(names, sorted(a for a, s in catalog.SPECS.items() if s.scope == "node"))

        # tool profiles 10/28/31 through MCP tools/list
        self.assertEqual(tools.PROFILES, ("minimal", "standard", "full"))
        for prof, want in (("minimal", MINIMAL), ("standard", STANDARD), ("full", FULL)):
            s = self.node.mcp("--profile", prof)
            got = [t["name"] for t in s.tools()]
            self.assertEqual(sorted(got), sorted(want), prof)
            self.assertEqual(len(got), {"minimal": 10, "standard": 28, "full": 31}[prof])
            s.close()
        # the default profile is standard; a prefix renames every tool
        s = self.node.mcp("--prefix", "hv_")
        got = [t["name"] for t in s.tools()]
        self.assertEqual(sorted(got), sorted("hv_" + x for x in STANDARD))
        s.close()

        # the hub's Streamable HTTP /mcp, per profile (?profile=), with this node as a direct link
        with helpers.tmp_home():
            hub_url, toks, hub = helpers.start_hub([{"name": "m3tools", "mode": "direct", "url": self.node.url,
                                                     "token": self.hub_tok}])
            try:
                helpers.wait_until(lambda: hub.links["m3tools"].summary().get("online"), 20)
                for prof, want in (("minimal", MINIMAL), ("standard", STANDARD), ("full", FULL)):
                    st, h, body = mcp_post(hub_url, toks["agent"], init_msg("2025-06-18"),
                                           path=f"/mcp?profile={prof}")
                    self.assertEqual(st, 200, body)
                    sid = h["mcp-session-id"]
                    st, _h, body = mcp_post(hub_url, toks["agent"], {"jsonrpc": "2.0", "id": 2,
                                                                     "method": "tools/list"}, sid=sid)
                    self.assertEqual(st, 200, body)
                    self.assertEqual(sorted(t["name"] for t in body["result"]["tools"]), sorted(want), prof)
            finally:
                hub.test_stop()

        # 4 export formats x 3 profiles through GET /v1/tools
        self.assertEqual(tuple(schema.EXPORT_FORMATS), ("mcp", "anthropic", "openai", "openai-responses"))
        for fmt in schema.EXPORT_FORMATS:
            for prof, want in (("minimal", MINIMAL), ("standard", STANDARD), ("full", FULL)):
                st, res = http_json("GET", f"{self.node.url}/v1/tools?format={fmt}&profile={prof}",
                                    token=self.node.agent)
                self.assertEqual(st, 200, res)
                exported = res["result"]["tools"]
                if fmt == "mcp":
                    got = [t["name"] for t in exported]
                    for t in exported:
                        self.assertEqual(set(t) - {"title"}, {"name", "description", "inputSchema", "annotations"})
                elif fmt == "anthropic":
                    got = [t["name"] for t in exported]
                    for t in exported:
                        self.assertEqual(set(t), {"name", "description", "input_schema"})
                elif fmt == "openai":
                    got = [t["function"]["name"] for t in exported]
                    for t in exported:
                        self.assertEqual((t["type"], set(t["function"])),
                                         ("function", {"name", "description", "parameters"}))
                else:
                    got = [t["name"] for t in exported]
                    for t in exported:
                        self.assertEqual(set(t), {"type", "name", "description", "parameters"})
                self.assertEqual(sorted(got), sorted(want), (fmt, prof))
                self.assertTrue(res["result"]["instructions"])
        st, res = http_json("GET", f"{self.node.url}/v1/tools?format=gemini", token=self.node.agent)
        self.assertEqual(st, 400, res)

        # 3 MCP protocol versions; title only from 2025-06-18
        self.assertEqual(len(hmcp.SUPPORTED_VERSIONS), 3)
        for ver in hmcp.SUPPORTED_VERSIONS:
            s = McpSession.__new__(McpSession)
            s.p = McpProc(["--local", "--profile", "full"], env=self.node.env)
            s.mid = 0
            init = s.request("initialize", {"protocolVersion": ver, "capabilities": {},
                                            "clientInfo": {"name": "m3", "version": "1"}})
            self.assertEqual(init["protocolVersion"], ver)
            s.p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            listed = s.tools()
            self.assertEqual(len(listed), 31)
            has_title = all("title" in t for t in listed)
            self.assertEqual(has_title, ver >= "2025-06-18", ver)
            s.close()
        # an unknown version is answered with the newest supported one
        s = McpSession.__new__(McpSession)
        s.p = McpProc(["--local"], env=self.node.env)
        s.mid = 0
        init = s.request("initialize", {"protocolVersion": "2099-01-01", "capabilities": {},
                                        "clientInfo": {"name": "m3", "version": "1"}})
        self.assertIn(init["protocolVersion"], hmcp.SUPPORTED_VERSIONS)
        s.close()


if __name__ == "__main__":
    unittest.main()
