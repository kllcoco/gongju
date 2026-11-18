"""WP6a: mcp.py (McpCore methods, stdio transport, subprocess end-to-end incl. stdout purity) (§18, §21.2)."""
from __future__ import annotations

import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hive import mcp, tools
from hive.client import Client

from tests import helpers
from tests.wp6util import FakeTarget

ROOT = Path(__file__).resolve().parents[1]


class Sink:
    def __init__(self):
        self.msgs = []
        self.ev = threading.Event()

    def __call__(self, msg):
        self.msgs.append(msg)
        self.ev.set()

    def wait_for(self, pred, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for m in list(self.msgs):
                if pred(m):
                    return m
            time.sleep(0.01)
        raise AssertionError(f"no matching message in {self.msgs}")


def core(**kw):
    t = kw.pop("target", None) or FakeTarget(kind="node", name="desk")
    c = mcp.McpCore(tools.Toolkit(Client(t), wait_budget=45), **kw)
    c.watch = False
    return c, t


class TestCore(unittest.TestCase):
    def test_initialize_versions(self):
        c, _ = core()
        for req, want in (("2025-11-25", "2025-11-25"), ("2025-06-18", "2025-06-18"), ("2025-03-26", "2025-03-26"),
                          ("2024-11-05", "2025-11-25"), (None, "2025-11-25")):
            with self.subTest(req=req):
                s = mcp.McpSession()
                r = c.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": req, "clientInfo": {"name": "t"}}}, s, Sink())
                res = r["result"]
                self.assertEqual(res["protocolVersion"], want)
                self.assertEqual(s.protocol_version, want)
                self.assertEqual(res["capabilities"], {"tools": {"listChanged": True}, "logging": {}})
                self.assertEqual(res["serverInfo"]["name"], "hive")
                self.assertEqual("title" in res["serverInfo"], want >= "2025-06-18")
                self.assertEqual(res["instructions"], tools.INSTRUCTIONS)

    def test_tools_list_per_version(self):
        c, _ = core()
        for v in mcp.SUPPORTED_VERSIONS:
            s = mcp.McpSession(protocol_version=v)
            r = c.handle({"jsonrpc": "2.0", "id": "a", "method": "tools/list", "params": {"cursor": "x"}}, s, Sink())
            ts = r["result"]["tools"]
            self.assertEqual(r["id"], "a")
            self.assertTrue(ts)
            self.assertEqual("title" in ts[0], v >= "2025-06-18")
            self.assertIn("inputSchema", ts[0])
            self.assertIn("annotations", ts[0])

    def test_misc_methods(self):
        c, _ = core()
        s = mcp.McpSession()
        self.assertEqual(c.handle({"jsonrpc": "2.0", "id": 7, "method": "ping"}, s, Sink()),
                         {"jsonrpc": "2.0", "id": 7, "result": {}})
        self.assertEqual(c.handle({"jsonrpc": "2.0", "id": 8, "method": "logging/setLevel",
                                   "params": {"level": "debug"}}, s, Sink())["result"], {})
        self.assertEqual(s.log_level, "debug")
        self.assertEqual(c.handle({"jsonrpc": "2.0", "id": 9, "method": "resources/list"}, s, Sink())["result"],
                         {"resources": []})
        self.assertEqual(c.handle({"jsonrpc": "2.0", "id": 9, "method": "prompts/list"}, s, Sink())["result"],
                         {"prompts": []})
        self.assertEqual(c.handle({"jsonrpc": "2.0", "id": 10, "method": "x/y"}, s, Sink())["error"]["code"], -32601)
        self.assertEqual(c.handle([{"jsonrpc": "2.0", "id": 1, "method": "ping"}], s, Sink())["error"],
                         {"code": -32600, "message": "batches not supported"})
        self.assertEqual(c.handle("x", s, Sink())["error"]["code"], -32600)
        self.assertIsNone(c.handle({"jsonrpc": "2.0", "method": "notifications/whatever"}, s, Sink()))
        self.assertIsNone(c.handle({"jsonrpc": "2.0", "id": 3, "result": {}}, s, Sink()))
        self.assertEqual(c.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "zz"}},
                                  s, Sink())["error"], {"code": -32602, "message": "Unknown tool: zz"})

    def test_tools_call_results(self):
        c, t = core(structured_content=True)
        t.handler = lambda a, p, **kw: {"path": "/x", "entries": [], "total": 0, "next_offset": None}
        s = mcp.McpSession(protocol_version="2025-06-18")
        sink = Sink()
        self.assertIsNone(c.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                                    "params": {"name": "fs_list", "arguments": {"path": "/x"}}}, s, sink))
        m = sink.wait_for(lambda m: m.get("id") == 5)
        self.assertFalse(m["result"]["isError"])
        self.assertEqual(m["result"]["content"][0]["type"], "text")
        self.assertIn("[desk] /x", m["result"]["content"][0]["text"])
        self.assertEqual(m["result"]["structuredContent"]["path"], "/x")
        sink2 = Sink()
        c.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                  "params": {"name": "fs_list", "arguments": {"depth": "deep"}}}, s, sink2)
        m = sink2.wait_for(lambda m: m.get("id") == 6)
        self.assertTrue(m["result"]["isError"])
        self.assertIn("bad_request", m["result"]["content"][0]["text"])
        self.assertNotIn("structuredContent", m["result"])
        s_old = mcp.McpSession(protocol_version="2025-03-26")
        sink3 = Sink()
        c.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "fs_list"}}, s_old, sink3)
        self.assertNotIn("structuredContent", sink3.wait_for(lambda m: m.get("id") == 7)["result"])

    def test_cancellation(self):
        started = threading.Event()
        release = threading.Event()

        def handler(a, p, **kw):
            started.set()
            release.wait(5)
            return {"job_id": "j_abcdefgh", "state": "running"}
        c, t = core()
        t.handler = handler
        s = mcp.McpSession()
        sink = Sink()
        c.handle({"jsonrpc": "2.0", "id": "c1", "method": "tools/call",
                  "params": {"name": "job_status", "arguments": {"job_id": "j_abcdefgh", "wait": 30}}}, s, sink)
        self.assertTrue(started.wait(5))
        self.assertIn("c1", s.inflight)
        rid = s.inflight["c1"].request_id
        c.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "c1"}}, s, sink)
        self.assertEqual(t.cancelled, [rid])
        release.set()
        helpers.wait_until(lambda: "c1" not in s.inflight, timeout=5)
        time.sleep(0.05)
        self.assertFalse(any(m.get("id") == "c1" for m in sink.msgs))

    def test_list_changed_after_approval(self):
        from hive.errors import approval_required
        state = {"approvals": False}

        def handler(a, p, **kw):
            state["approvals"] = True
            raise approval_required(approval_id="ap_abcdefgh", expires_at="", where="", how="")
        t = FakeTarget(kind="node", name="desk", handler=handler)
        orig = t.whoami
        t.whoami = lambda: dict(orig(), approvals=state["approvals"])
        c = mcp.McpCore(tools.Toolkit(Client(t)))
        s = mcp.McpSession()
        sink = Sink()
        c.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "shell_run", "arguments": {"command": "ls"}}}, s, sink)
        sink.wait_for(lambda m: m.get("id") == 1)
        sink.wait_for(lambda m: m.get("method") == "notifications/tools/list_changed")
        c.close(s, wait=1)

    def test_watcher_emits_list_changed(self):
        t = FakeTarget(kind="node", name="desk", actions=["fs.read", "fs.list"])
        from hive.errors import unavailable
        t.fail_whoami = unavailable("down", reason="connect")
        c = mcp.McpCore(tools.Toolkit(Client(t)))
        c.watch_interval = 0.05
        s = mcp.McpSession()
        sink = Sink()
        c.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}, s, sink)
        self.assertTrue(s.initialized)
        time.sleep(0.15)
        self.assertEqual(sink.msgs, [])
        t.fail_whoami = None
        sink.wait_for(lambda m: m.get("method") == "notifications/tools/list_changed", timeout=5)
        c.close(s, wait=1)

    def test_serve_stdio(self):
        c, t = core()
        t.handler = lambda a, p, **kw: {"state": "exited", "exit_code": 0, "output": "hi", "shell": "bash",
                                        "duration_s": 0.1, "output_bytes": 2}
        lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}), "",
                 "{not json", json.dumps([1]), json.dumps({"jsonrpc": "2.0", "id": 2, "method": "ping"})]
        inp = io.BytesIO(("\n".join(lines) + "\n").encode())
        out = io.BytesIO()
        self.assertEqual(mcp.serve_stdio(c, inp, out), 0)
        got = [json.loads(x) for x in out.getvalue().decode().splitlines()]
        self.assertEqual(got[0]["id"], 1)
        self.assertEqual(got[1], {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        self.assertEqual(got[2]["error"]["code"], -32600)
        self.assertEqual(got[3], {"jsonrpc": "2.0", "id": 2, "result": {}})
        self.assertTrue(out.getvalue().endswith(b"\n"))
        self.assertNotIn(b": ", out.getvalue().split(b"\n")[3])  # compact separators


class McpProc:
    def __init__(self, argv, env=None):
        self.p = subprocess.Popen([sys.executable, "-m", "hive", "mcp"] + argv, cwd=str(ROOT), env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.q: "queue.Queue" = queue.Queue()
        self.raw = []
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.p.stdout:
            self.raw.append(line)
            self.q.put(line)
        self.q.put(None)

    def send(self, msg):
        self.p.stdin.write((json.dumps(msg) + "\n").encode())
        self.p.stdin.flush()

    def recv(self, mid, timeout=60):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                line = self.q.get(timeout=max(0.1, end - time.monotonic()))
            except queue.Empty:
                break
            if line is None:
                break
            m = json.loads(line)
            if m.get("id") == mid:
                return m
        raise AssertionError(f"no response {mid}")

    def close(self):
        try:
            self.p.stdin.close()
        except OSError:
            pass
        rc = self.p.wait(timeout=30)
        self.p.stdout.close()
        self.p.stderr.close()
        return rc


class TestSubprocess(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = helpers.tmp_home()
        cls.home.__enter__()
        cls.url, cls.tok, cls.rt = helpers.start_node_server(start_managers=True)
        fd, cls.tokfile = tempfile.mkstemp()
        os.write(fd, (cls.tok + "\n").encode())
        os.close(fd)

    @classmethod
    def tearDownClass(cls):
        cls.rt.test_stop()
        os.unlink(cls.tokfile)
        cls.home.__exit__(None, None, None)

    def env(self):
        e = dict(os.environ)
        e.pop("HIVE_URL", None)
        e["HIVE_HOME"] = os.environ["HIVE_HOME"]
        return e

    def test_end_to_end_and_stdout_purity(self):
        p = McpProc(["--url", self.url, "--token-file", self.tokfile, "--wait-budget", "30"], env=self.env())
        try:
            p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}})
            self.assertEqual(p.recv(1)["result"]["protocolVersion"], "2025-06-18")
            p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            p.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            names = {t["name"] for t in p.recv(2)["result"]["tools"]}
            self.assertIn("shell_run", names)
            p.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "shell_run", "arguments": {"command": "echo mcp-42"}}})
            r = p.recv(3)["result"]
            self.assertFalse(r["isError"], r)
            self.assertIn("mcp-42", r["content"][0]["text"])
            p.send({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "fs_read", "arguments": {"max_lines": 5}}})
            r = p.recv(4)["result"]
            self.assertTrue(r["isError"])
            p.send({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}})
            self.assertEqual(p.recv(5)["error"]["code"], -32602)
            p.p.stdin.write(b"{garbage\n")
            p.p.stdin.flush()
            p.send({"jsonrpc": "2.0", "id": 6, "method": "ping"})
            self.assertEqual(p.recv(6)["result"], {})
        finally:
            self.assertEqual(p.close(), 0)
        for line in p.raw:  # stdout purity: every line is one JSON-RPC message
            m = json.loads(line)
            self.assertEqual(m.get("jsonrpc"), "2.0")

    def test_unreachable_at_startup(self):
        port = helpers.free_port()
        p = McpProc(["--url", f"http://127.0.0.1:{port}", "--token-file", self.tokfile], env=self.env())
        try:
            p.send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
            self.assertIn("instructions", p.recv(1)["result"])
            p.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            self.assertEqual(len(p.recv(2)["result"]["tools"]), 28)
            p.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "fs_list", "arguments": {"path": "."}}})
            r = p.recv(3)["result"]
            self.assertTrue(r["isError"])
            self.assertIn("unavailable", r["content"][0]["text"])
        finally:
            self.assertEqual(p.close(), 0)

    def test_bad_connection_exits_2(self):
        out = subprocess.run([sys.executable, "-m", "hive", "mcp", "--connection", "nope"], cwd=str(ROOT),
                             env=self.env(), stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
        self.assertEqual(out.returncode, 2)
        self.assertEqual(out.stdout, b"")
        self.assertIn(b"not_found", out.stderr)


if __name__ == "__main__":
    unittest.main()
