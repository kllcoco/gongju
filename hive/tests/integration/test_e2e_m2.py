"""E2E 1 (M2, §21.2 Integration row, §22.1 step 5): a hub with a direct node and a relay node, through real
processes on loopback.

Machine "home" (one HIVE_HOME): ``hive init hub`` + ``hive init node`` (desk), ``hive hub add-node desk --direct
--generate`` with the printed ``hive node token add … --kind hub --sha256 …`` line run on the node,
``hive hub add-node pod-a --relay`` (join code), ``hive hub add-agent``, ``hive gateway token add``; ``hive hub run``
and ``hive node run`` under a restart-on-75 supervisor (the watchdog rule).

Machine "pod" (another HIVE_HOME and HOME): the §20.4 pod one-liner: ``GET /v1/dist/hive.pyz`` without a token, the
sha256 checked against ``X-Hive-Sha256`` (and a local deterministic ``dist.build_pyz``), ``python hive.pyz join
--pod`` with ``HIVE_JOIN`` (and ``HIVE_HF_ENDPOINT`` = the hfstub), ``python hive.pyz _update``, then ``python
hive.pyz node run`` in the watchdog loop. A join code never names a loopback hub (dist R5b-6), so the hub prints the
code for a placeholder LAN address and the test points the decoded code's ``u`` at the hub's loopback port.

Agents: the Python client and an MCP stdio subprocess (``hive mcp`` with ``HIVE_URL``/``HIVE_TOKEN_FILE`` = the hub
agent token), plus the hub's Streamable HTTP ``/mcp``. Flow: nodes_list → shell_run → job_start/job_status(wait) →
model_pull from the hfstub → service_define a stub model server on both nodes → service_status(wait) →
model_chat → gateway chat from an OpenAI-style HTTP client through both links (direct: SSE passthrough arriving
incrementally; relay: synthesized SSE) → fs_transfer node↔node → ``hive hub restart`` (control.json) with a relay
job in flight: the pod re-registers and the job is still there → ``hive hub restart-node pod-a`` (relay
``refresh:"restart"`` → exit 75 → relaunched, job and service adopted) → stop everything with exit 0.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from hive import dist, proctree, util
from hive.transport import HttpTransport

from tests import helpers
from tests.fixtures.hfstub import HFStub, Repo
from tests.integration import m2util
from tests.integration.m2util import IS_WINDOWS, ROOT, STUB_OPENAI, Supervisor, http, http_json, run_cli, wait_for
from tests.test_mcp import McpProc

PULL_FILES = {"config.json": b'{"model_type": "tiny"}', "model.safetensors": b"w" * 5000, "README.md": b"# tiny\n"}


@unittest.skipIf(IS_WINDOWS, "POSIX E2E (the Windows variant runs in the lead's Windows CI)")
class TestE2EM2(unittest.TestCase):
    """Hub (direct + relay) + join + pod one-liner + /mcp + gateway + hub/node restarts, real processes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="hivem2-")).resolve()
        cls.home = cls.base / "home-hive"            # HIVE_HOME of the hub + direct node machine
        cls.user = cls.base / "home-user"            # its HOME
        cls.work = cls.base / "work"                 # extra rw root of desk
        cls.pod_home = cls.base / "pod-hive"         # HIVE_HOME of the pod (cwd of hive.pyz)
        cls.pod_user = cls.base / "pod-user"         # the pod's HOME
        for d in (cls.home, cls.user, cls.work, cls.pod_home, cls.pod_user):
            d.mkdir()
        cls.sups: List[Supervisor] = []
        cls.service_pids: List[int] = []
        cls.hf = HFStub({"org/tiny": Repo(PULL_FILES)}).start()
        cls.env = m2util.base_env(HIVE_HOME=str(cls.home), HOME=str(cls.user),
                                  PYTHONPATH=str(ROOT) + (os.pathsep + os.environ["PYTHONPATH"]
                                                          if os.environ.get("PYTHONPATH") else ""))
        # the pod runs only the downloaded zipapp: no PYTHONPATH pointing at the checkout
        cls.pod_env = m2util.base_env(HIVE_HOME=str(cls.pod_home), HOME=str(cls.pod_user))
        cls.pod_env.pop("PYTHONPATH", None)
        try:
            cls._setup_home()
            cls._setup_pod()
        except BaseException:
            cls._teardown()
            raise

    # -----------------------------------------------------------------------------------------------------------
    @classmethod
    def cli(cls, *argv: str, timeout: float = 120) -> Any:
        return run_cli(argv, env=cls.env, timeout=timeout)

    @classmethod
    def pod_cli(cls, *argv: str, env: Optional[Dict[str, str]] = None, timeout: float = 120) -> Any:
        return run_cli(argv, env=env or cls.pod_env, cwd=str(cls.pod_home), timeout=timeout,
                       prefix=[sys.executable, str(cls.pod_home / "hive.pyz")])

    @classmethod
    def _ok(cls, r: Any, what: str) -> str:
        if r.returncode != 0:
            raise AssertionError(f"{what} failed ({r.returncode}): {r.stdout}{r.stderr}")
        return r.stdout

    @classmethod
    def _setup_home(cls) -> None:
        cls.hub_port, cls.node_port = helpers.free_port(), helpers.free_port()
        cls.hub_url = f"http://127.0.0.1:{cls.hub_port}"
        cls._ok(cls.cli("init", "hub", "--yes", "--name", "home-hub", "--listen", f"127.0.0.1:{cls.hub_port}"),
                "hive init hub")
        m2util.allow_root(cls.home / "hub" / "config.json")
        cls._ok(cls.cli("init", "node", "--yes", "--name", "desk", "--listen", f"127.0.0.1:{cls.node_port}",
                        "--root", str(cls.work), "--proxy", "off", "--mirror", "none"), "hive init node")
        m2util.allow_root(cls.home / "node" / "config.json")
        # direct link: the hub generates the token, only its hash crosses to the node
        out = cls._ok(cls.cli("hub", "add-node", "desk", "--direct", f"http://127.0.0.1:{cls.node_port}",
                              "--generate"), "hub add-node --direct")
        line = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("hive node token add")]
        assert line, out
        cls._ok(cls.cli(*line[0].split()[1:]), "node token add (kind hub)")
        # relay node: the join code (relay token) is printed once
        out = cls._ok(cls.cli("hub", "add-node", "pod-a", "--relay", "--host", "10.255.0.1", "--tags", "cloud"),
                      "hub add-node --relay")
        m = re.search(r"HIVE_JOIN='(hvj1\.[A-Za-z0-9_-]+=*)'", out)
        assert m, out
        code = dist.decode_join(m.group(1))
        assert code["u"] == f"http://10.255.0.1:{cls.hub_port}" and code["n"] == "pod-a", code
        code["u"] = cls.hub_url                       # the test's "LAN" is loopback
        cls.join_code = dist.encode_join(code)
        # agent and gateway tokens
        out = cls._ok(cls.cli("hub", "add-agent", "claude", "--role", "operator", "--nodes", "*"), "hub add-agent")
        cls.agent_token = re.search(r"^(hv_[A-Za-z0-9_-]+)$", out, re.M).group(1)
        cls.agent_token_file = cls.base / "claude.token"
        cls.agent_token_file.write_text(cls.agent_token + "\n", encoding="utf-8")
        out = cls._ok(cls.cli("gateway", "token", "add", "chatbox", "--models", "desk/*,pod-a/*"),
                      "gateway token add")
        cls.gw_token = re.search(r"^(hv_[A-Za-z0-9_-]+)$", out, re.M).group(1)
        cls.owner_token = (cls.home / "tokens" / "hub-owner.token").read_text(encoding="utf-8").strip()
        # daemons (restart-on-75 loops)
        # the node first: the hub probes direct links every 30 s (§8.3)
        cls.node_sup = Supervisor([sys.executable, "-m", "hive", "node", "run"], env=cls.env, cwd=str(ROOT),
                                  log=cls.base / "node.log", name="desk").start()
        cls.sups.append(cls.node_sup)
        wait_for(lambda: http_json("GET", f"http://127.0.0.1:{cls.node_port}/v1/health", timeout=2)[1]
                 ["result"]["role"] == "node", 60, what="node health")
        cls.hub_sup = Supervisor([sys.executable, "-m", "hive", "hub", "run"], env=cls.env, cwd=str(ROOT),
                                 log=cls.base / "hub.log", name="hub").start()
        cls.sups.append(cls.hub_sup)
        wait_for(lambda: http_json("GET", cls.hub_url + "/v1/health", timeout=2)[1]["result"]["role"] == "hub",
                 60, what="hub health")

    @classmethod
    def _setup_pod(cls) -> None:
        # the §20.4 pod one-liner: curl the zipapp (no token), check its sha256, join, run
        r = http("GET", cls.hub_url + "/v1/dist/hive.pyz")
        assert r.status == 200, r.status
        body = r.read()
        r.close()
        cls.pyz_sha = hashlib.sha256(body).hexdigest()
        assert r.headers.get("X-Hive-Sha256") == cls.pyz_sha, (r.headers.get("X-Hive-Sha256"), cls.pyz_sha)
        cls.local_pyz_sha = dist.pyz_sha256(dist.build_pyz())   # deterministic build: same bytes as the hub's
        (cls.pod_home / "hive.pyz").write_bytes(body)
        env = dict(cls.pod_env, HIVE_JOIN=cls.join_code, HIVE_HF_ENDPOINT=cls.hf.url)
        out = cls._ok(cls.pod_cli("join", "--yes", "--pod", env=env), "hive.pyz join")
        assert "joined" in out, out
        cls.pod_cfg = json.loads((cls.pod_home / "node" / "config.json").read_text(encoding="utf-8"))
        # the watchdog loop runs `_update` first: same sha -> nothing replaced
        upd = cls.pod_cli("_update", env=dict(cls.pod_env, HIVE_WATCHDOG="1"))
        assert upd.returncode == 0, upd.stdout + upd.stderr
        assert hashlib.sha256((cls.pod_home / "hive.pyz").read_bytes()).hexdigest() == cls.pyz_sha
        cls.pod_sup = Supervisor([sys.executable, str(cls.pod_home / "hive.pyz"), "node", "run"],
                                 env=dict(cls.pod_env, HIVE_WATCHDOG="1"), cwd=str(cls.pod_home),
                                 log=cls.base / "pod.log", name="pod-a").start()
        cls.sups.append(cls.pod_sup)
        cls.wait_online("pod-a")

    @classmethod
    def hub_nodes(cls) -> Dict[str, dict]:
        t = HttpTransport(cls.hub_url, cls.owner_token, timeout=10)
        try:
            res = t.request("GET", "/v1/nodes")
        finally:
            t.close()
        return {n["name"]: n for n in res["nodes"]}

    @classmethod
    def wait_online(cls, name: str, timeout: float = 120) -> dict:
        return wait_for(lambda: cls.hub_nodes().get(name, {}).get("online") and cls.hub_nodes()[name], timeout,
                        what=f"{name} online at the hub")

    @classmethod
    def tearDownClass(cls) -> None:
        cls._teardown()

    @classmethod
    def _teardown(cls) -> None:
        for sup in getattr(cls, "sups", []):
            if sup.running:
                sup.kill()
        for pid in getattr(cls, "service_pids", []):
            if util.pid_alive(pid):
                try:
                    proctree.kill_tree(pid, grace=1)
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
        hf = getattr(cls, "hf", None)
        if hf is not None:
            hf.stop()
        if os.environ.get("HIVE_TEST_KEEP"):
            print("kept", cls.base)
        else:
            shutil.rmtree(str(cls.base), ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------------
    def tool(self, p: McpProc, name: str, args: dict, *, expect_error: bool = False, timeout: float = 120) -> str:
        self._mid += 1
        p.send({"jsonrpc": "2.0", "id": self._mid, "method": "tools/call",
                "params": {"name": name, "arguments": args}})
        msg = p.recv(self._mid, timeout=timeout)
        self.assertIn("result", msg, msg)
        res = msg["result"]
        text = "\n".join(c.get("text", "") for c in res["content"])
        if os.environ.get("HIVE_TEST_SHOW"):
            print(f"\n>>> {name} {json.dumps(args)}\n{text}")
        self.assertEqual(bool(res.get("isError")), expect_error, f"{name}: {text}")
        return text

    def stub_args(self, node: str, port: int, script_dir: Path, model: str) -> dict:
        script = script_dir / "stub_openai.py"
        script.write_text(STUB_OPENAI, encoding="utf-8")
        return {"node": node, "name": "llm", "port": port, "endpoint": f"http://127.0.0.1:{port}/v1",
                "options": ["argv=" + json.dumps([sys.executable, str(script), str(port), model]),
                            "health=http", f"health_url=http://127.0.0.1:{port}/health", "health_interval=2"],
                "wait": 30}

    def mcp_http(self, token: str) -> Any:
        """A minimal Streamable HTTP MCP client for the hub's /mcp."""
        url = self.hub_url + "/mcp"
        st, hdr = None, None
        r = http("POST", url, token=token, headers={"Accept": "application/json, text/event-stream"},
                 body={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "e2e-m2-http", "version": "1"}}})
        self.assertEqual(r.status, 200)
        sid = r.headers["Mcp-Session-Id"]
        init = json.loads(r.read())
        r.close()
        self.assertEqual(init["result"]["serverInfo"]["name"], "hive")
        h = {"Mcp-Session-Id": sid, "MCP-Protocol-Version": "2025-06-18",
             "Accept": "application/json, text/event-stream"}
        self.assertEqual(m2util.http_status("POST", url, token=token, headers=h,
                                            body={"jsonrpc": "2.0", "method": "notifications/initialized"}), 202)
        counter = [1]

        def call(method: str, params: Optional[dict] = None) -> dict:
            counter[0] += 1
            st_, body = http_json("POST", url, token=token, headers=h,
                                  body={"jsonrpc": "2.0", "id": counter[0], "method": method, "params": params or {}},
                                  timeout=180)
            self.assertEqual(st_, 200, body)
            return body
        call.sid = sid  # type: ignore[attr-defined]
        del st, hdr
        return call

    # -----------------------------------------------------------------------------------------------------------
    def test_e2e_1(self) -> None:
        self._mid = 0
        from hive import client as hclient

        # --- pod one-liner results
        self.assertEqual(self.pyz_sha, self.local_pyz_sha)
        self.assertEqual(self.pod_cfg["name"], "pod-a")
        self.assertTrue(self.pod_cfg["relay"]["enabled"])
        self.assertEqual(self.pod_cfg["relay"]["hub_url"], self.hub_url)
        self.assertEqual(self.pod_cfg["consent"]["by"], "join")
        self.assertEqual(self.pod_cfg["models"]["hf_endpoint"], self.hf.url)
        self.assertFalse(self.pod_cfg["listen"]["enabled"])            # pod topology: no listener

        # --- hub status (owner CLI on the hub machine)
        r = self.cli("hub", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r"desk\s+online\s+direct")
        self.assertRegex(r.stdout, r"pod-a\s+online\s+relay")

        # --- Python client through the hub (agent token)
        c = hclient.connect(self.hub_url, token=self.agent_token)
        rows = {n["name"]: n for n in c.nodes()}
        self.assertEqual((rows["desk"]["mode"], rows["pod-a"]["mode"]), ("direct", "relay"))
        self.assertTrue(rows["desk"]["online"] and rows["pod-a"]["online"])
        self.assertEqual(c.whoami()["server"]["role"], "hub")
        self.assertEqual(c.node("desk").sh("echo client-desk").strip(), "client-desk")
        self.assertEqual(c.node("pod-a").sh("echo client-pod").strip(), "client-pod")
        jh = c.node("pod-a").start_job(f"{sys.executable} -c \"print('pod-job-ok')\"", name="client-job")
        st = jh.wait(60)
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0), st)
        self.assertIn("pod-job-ok", c.node("pod-a").call("job.logs", {"job_id": jh.job_id})["text"])

        # --- MCP stdio subprocess against the hub
        mcp_env = dict(self.env, HIVE_URL=self.hub_url, HIVE_TOKEN_FILE=str(self.agent_token_file))
        p = McpProc([], env=mcp_env)
        try:
            p.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "e2e-m2", "version": "1"}}})
            self.assertEqual(p.recv(0, timeout=60)["result"]["serverInfo"]["name"], "hive")
            p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            p.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            tools = {t["name"]: t for t in p.recv(1)["result"]["tools"]}
            for t in ("nodes_list", "shell_run", "job_start", "job_status", "model_pull", "service_define",
                      "service_status", "model_chat", "fs_transfer"):
                self.assertIn(t, tools)
            self.assertIn("node", tools["shell_run"]["inputSchema"]["properties"])
            self._mid = 1

            text = self.tool(p, "nodes_list", {})
            self.assertRegex(text, r"desk\s+online\s+direct")
            self.assertRegex(text, r"pod-a\s+online\s+relay")

            for node in ("desk", "pod-a"):
                text = self.tool(p, "shell_run", {"node": node, "command": f"echo hello-{node}"})
                self.assertIn(f"[{node}] exit 0", text)
                self.assertIn(f"hello-{node}", text)

            text = self.tool(p, "job_start", {"node": "pod-a", "name": "m2job", "argv": [
                sys.executable, "-c", "import time; time.sleep(1); print('m2-job-done')"]})
            job = re.search(r"started job (j_[A-Za-z0-9_-]+)", text).group(1)
            text = self.tool(p, "job_status", {"node": "pod-a", "job_id": job, "wait": 30})
            self.assertIn("exited · exit 0", text)
            self.assertIn("m2-job-done", text)

            # model_pull from the hfstub (the pod's models.hf_endpoint came from HIVE_HF_ENDPOINT at join)
            text = self.tool(p, "model_pull", {"node": "pod-a", "repo": "org/tiny", "dry_run": True})
            self.assertIn("3 files", text)
            self.assertIn(self.hf.url, text)
            text = self.tool(p, "model_pull", {"node": "pod-a", "repo": "org/tiny", "path": "~/models/tiny"})
            pull = re.search(r"as job (j_[A-Za-z0-9_-]+)", text).group(1)
            text = self.tool(p, "job_status", {"node": "pod-a", "job_id": pull, "wait": 60})
            self.assertIn("exit 0", text)
            target = self.pod_user / "models" / "tiny"
            for name, data in PULL_FILES.items():
                self.assertEqual((target / name).read_bytes(), data, name)
            self.assertTrue((target / ".hive-pull.json").exists())

            # a stub model server on both nodes -> service_status(wait) -> model_chat
            self.desk_port, self.pod_port = helpers.free_port(), helpers.free_port()
            self.tool(p, "service_define", self.stub_args("desk", self.desk_port, self.work, "desk-model"))
            self.tool(p, "service_define", self.stub_args("pod-a", self.pod_port, self.pod_user, "pod-model"))
            for node in ("desk", "pod-a"):
                text = self.tool(p, "service_status", {"node": node, "name": "llm", "wait": 30})
                self.assertIn("healthy", text)
                st = c.node(node).call("service.status", {"name": "llm"})
                self.service_pids.extend(st["resources"]["pids"] or [])
            text = self.tool(p, "model_chat", {"node": "desk", "service": "llm", "prompt": "ping desk"})
            self.assertIn("pong: ping desk", text)
            self.assertIn("desk-model", text)
            text = self.tool(p, "model_chat", {"node": "pod-a", "service": "llm", "prompt": "ping pod"})
            self.assertIn("pong: ping pod", text)
            self.assertIn("pod-model", text)

            self._gateway()

            # fs_transfer node <-> node (the tool host is this MCP process)
            blob = os.urandom(3 * 1024 * 1024 + 17)
            (self.work / "blob.bin").write_bytes(blob)
            text = self.tool(p, "fs_transfer", {"src": f"desk:{self.work / 'blob.bin'}", "dst": "pod-a:~/blob.bin"})
            self.assertIn("verified", text)
            self.assertEqual((self.pod_user / "blob.bin").read_bytes(), blob)
            text = self.tool(p, "fs_transfer", {"src": "pod-a:~/blob.bin", "dst": f"desk:{self.work / 'back.bin'}"})
            self.assertIn("verified", text)
            self.assertEqual((self.work / "back.bin").read_bytes(), blob)
        finally:
            rc = p.close()
        self.assertEqual(rc, 0, "hive mcp did not exit 0 on stdin EOF")

        # --- the hub's Streamable HTTP /mcp (server-hosted Toolkit)
        call = self.mcp_http(self.agent_token)
        names = {t["name"] for t in call("tools/list")["result"]["tools"]}
        self.assertIn("nodes_list", names)
        res = call("tools/call", {"name": "shell_run", "arguments": {"node": "pod-a", "command": "echo via-http-mcp"}})
        self.assertFalse(res["result"].get("isError"), res)
        self.assertIn("via-http-mcp", res["result"]["content"][0]["text"])
        res = call("tools/call", {"name": "fs_transfer", "arguments": {"src": "local:/etc/hostname",
                                                                         "dst": "desk:~/x"}})
        self.assertTrue(res["result"].get("isError"), res)              # server-hosted: no local: endpoints
        self.assertEqual(m2util.http_status("DELETE", self.hub_url + "/mcp", token=self.agent_token,
                                            headers={"Mcp-Session-Id": call.sid}), 200)

        self._hub_restart(c)
        self._relay_restart(c)
        self._audit()
        self._shutdown(c)

    # -----------------------------------------------------------------------------------------------------------
    def _gateway(self) -> None:
        """An OpenAI-style HTTP client (urllib) through the hub's gateway, both links."""
        for node, model in (("desk", "desk-model"), ("pod-a", "pod-model")):
            base = f"{self.hub_url}/openai/{node}/llm/v1"
            st, body = http_json("GET", base + "/models", token=self.gw_token)
            self.assertEqual((st, body["data"][0]["id"]), (200, model))
            st, body = http_json("POST", base + "/chat/completions", token=self.gw_token,
                                 body={"model": model, "messages": [{"role": "user", "content": "hi gw"}]})
            self.assertEqual(st, 200, body)
            # the app's Authorization never reaches the model server
            self.assertEqual(body["choices"][0]["message"]["content"], "pong: hi gw")
            st, body = http_json("POST", base + "/embeddings", token=self.gw_token, body={"model": model,
                                                                                           "input": "x"})
            self.assertEqual((st, body["data"][0]["embedding"]), (200, [0.1, 0.2]))
            # the /v1-less route form (apps that append /v1 themselves)
            st, body = http_json("GET", f"{self.hub_url}/openai/{node}/llm/models", token=self.gw_token)
            self.assertEqual(st, 200, body)
        # direct link: SSE chunks are passed through as they arrive
        r = http("POST", f"{self.hub_url}/openai/desk/llm/v1/chat/completions", token=self.gw_token,
                 body={"model": "desk-model", "stream": True, "messages": [{"role": "user", "content": "a b c"}]})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers.get("Content-Type", "").startswith("text/event-stream"))
        lines = m2util.sse_lines(r)
        data = [ln for _, ln in lines if ln.startswith("data: {")]
        self.assertEqual(len(data), 4, lines)                          # "pong:", " a", " b", " c"
        self.assertEqual(lines[-1][1], "data: [DONE]")
        self.assertGreater(lines[-1][0] - lines[0][0], 1.0, lines)     # incremental, not buffered
        # relay link: one synthesized chunk per choice (+ include_usage chunk) + [DONE]
        r = http("POST", f"{self.hub_url}/openai/pod-a/llm/v1/chat/completions", token=self.gw_token,
                 body={"model": "pod-model", "stream": True, "stream_options": {"include_usage": True},
                       "messages": [{"role": "user", "content": "relay me"}]})
        self.assertEqual(r.status, 200)
        self.assertTrue(r.headers.get("Content-Type", "").startswith("text/event-stream"))
        chunks = [ln for _, ln in m2util.sse_lines(r)]
        self.assertEqual(chunks[-1], "data: [DONE]")
        first = json.loads(chunks[0][len("data: "):])
        self.assertEqual(first["object"], "chat.completion.chunk")
        self.assertEqual(first["choices"][0]["delta"]["content"], "pong: relay me")
        usage = json.loads(chunks[1][len("data: "):])
        self.assertEqual((usage["choices"], usage["usage"]["total_tokens"]), ([], 5))
        # agent tokens cannot use the gateway; the gateway token cannot use /v1/call
        st, body = http_json("GET", f"{self.hub_url}/openai/desk/llm/v1/models", token=self.agent_token)
        self.assertEqual(st, 403, body)
        st, body = http_json("POST", self.hub_url + "/v1/call", token=self.gw_token,
                             body={"action": "node.info", "node": "desk", "params": {}})
        self.assertIn(st, (401, 403), body)

    def _hub_restart(self, c: Any) -> None:
        """``hive hub restart`` (control.json, exit 75 -> relaunched) with a relay job in flight."""
        jh = c.node("pod-a").start_job(f"{sys.executable} -c \"import time; time.sleep(4); print('survived')\"",
                                       name="across-hub-restart")
        old = self.hub_sup.pid()
        r = self.cli("hub", "restart")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("restarted", r.stdout)
        self.assertNotEqual(self.hub_sup.pid(), old)
        self.assertIn(75, self.hub_sup.exits)
        self.wait_online("pod-a")
        self.wait_online("desk")
        st = wait_for(lambda: (lambda s: s if s["state"] == "exited" else None)(
            c.node("pod-a").call("job.status", {"job_id": jh.job_id, "wait": 10})), 60, what="job after hub restart")
        self.assertEqual(st["exit_code"], 0)
        self.assertIn("survived", c.node("pod-a").call("job.logs", {"job_id": jh.job_id})["text"])
        # the gateway works again through both links
        for node in ("desk", "pod-a"):
            st_, body = http_json("POST", f"{self.hub_url}/openai/{node}/llm/v1/chat/completions",
                                  token=self.gw_token, body={"messages": [{"role": "user", "content": "again"}]})
            self.assertEqual(st_, 200, body)

    def _relay_restart(self, c: Any) -> None:
        """``hive hub restart-node pod-a``: relay refresh "restart" -> the pod exits 75, the loop relaunches it;
        a running job and the running service are adopted."""
        jh = c.node("pod-a").start_job(f"{sys.executable} -c \"import time; time.sleep(5); print('pod-adopted')\"",
                                       name="across-pod-restart")
        svc_before = c.node("pod-a").call("service.status", {"name": "llm"})
        starts = self.pod_sup.starts
        r = self.cli("hub", "restart-node", "pod-a")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        wait_for(lambda: self.pod_sup.starts > starts, 60, what="pod relaunch")
        self.assertIn(75, self.pod_sup.exits)
        self.wait_online("pod-a")
        st = wait_for(lambda: (lambda s: s if s["state"] == "exited" else None)(
            c.node("pod-a").call("job.status", {"job_id": jh.job_id, "wait": 10})), 60, what="adopted pod job")
        self.assertEqual(st["exit_code"], 0)
        self.assertIn("pod-adopted", c.node("pod-a").call("job.logs", {"job_id": jh.job_id})["text"])
        svc = c.node("pod-a").call("service.status", {"name": "llm"})
        self.assertEqual(svc["state"], "running")
        self.assertEqual(svc["pid"], svc_before["pid"])                 # adopted, not restarted
        self.assertEqual(c.node("pod-a").chat("after restart", service="llm"), "pong: after restart")

    def _audit(self) -> None:
        rows: List[dict] = []
        for f in sorted((self.home / "hub" / "audit").glob("audit-*.jsonl")):
            rows += [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        gw = [r for r in rows if r.get("kind") == "gateway"]
        self.assertTrue(gw)
        text = json.dumps(gw)
        self.assertNotIn("hi gw", text)                                  # never prompts or replies
        self.assertNotIn("pong:", text)
        self.assertTrue(any((r.get("extra") or {}).get("usage") or r.get("usage") for r in gw), gw[:2])
        calls = {(r.get("identity"), r.get("action"), r.get("node")) for r in rows if r.get("kind") == "call"}
        self.assertIn(("claude", "shell.run", "pod-a"), calls)
        self.assertIn(("claude", "model.pull", "pod-a"), calls)
        # the relay node's own audit names the hub's agent
        prow: List[dict] = []
        for f in sorted((self.pod_home / "node" / "audit").glob("audit-*.jsonl")):
            prow += [json.loads(ln) for ln in f.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertTrue(any(r.get("action") == "shell.run" and r.get("agent") == "claude" for r in prow),
                        prow[-3:])

    @staticmethod
    def _remove_llm(c: Any, node: str) -> bool:
        from hive.errors import HiveError
        try:
            c.node(node).call("service.remove", {"name": "llm", "wait": 20})
        except HiveError as e:
            if e.code != "not_found":
                raise
        return True

    def _shutdown(self, c: Any) -> None:
        for node in ("desk", "pod-a"):
            # Phase 2 review: after the hub/pod restarts a link can still be reconnecting under load; wait until
            # both are online and retry a call that could not connect (a remove that already ran -> not_found)
            self.wait_online(node)
            wait_for(lambda n=node: self._remove_llm(c, n), 90, interval=1.0, what=f"service.remove llm on {node}")
        c.close()
        r = self.pod_cli("node", "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.pod_sup.wait(30), self.pod_sup.output())
        self.assertEqual(self.pod_sup.exits[-1], 0)
        r = self.cli("node", "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.node_sup.wait(30))
        self.assertEqual(self.node_sup.exits[-1], 0)
        r = self.cli("hub", "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.hub_sup.wait(30))
        self.assertEqual(self.hub_sup.exits[-1], 0)


if __name__ == "__main__":
    unittest.main()
