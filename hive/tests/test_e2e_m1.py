"""E2E-M1 (§21.2 Integration row, §22.1 M1) on one direct node: MCP subprocess → shell_run → job_start /
job_status(wait) → service_define a stub OpenAI server → service_status(wait) → model_chat."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import textwrap
import unittest

from tests import helpers
from tests.test_mcp import McpProc

STUB = textwrap.dedent("""\
    import json, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.rstrip("/") in ("/v1/models", "/models"):
                self._send(200, {"object": "list", "data": [{"id": "stub-model", "object": "model",
                                                             "owned_by": "stub"}]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            text = "pong: " + req["messages"][-1]["content"]
            self._send(200, {"id": "c1", "object": "chat.completion", "model": req.get("model") or "stub-model",
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

        def log_message(self, *a):
            pass

    print("stub ready", flush=True)
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
""")


@unittest.skipIf(os.name == "nt", "the M1 script uses bash quoting; Windows CI runs the PowerShell variant later")
class TestE2EM1(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = helpers.tmp_home()
        cls.home.__enter__()
        cls.url, cls.tok, cls.rt = helpers.start_node_server(start_managers=True)
        fd, cls.tokfile = tempfile.mkstemp()
        os.write(fd, (cls.tok + "\n").encode())
        os.close(fd)
        cls.tmp = tempfile.mkdtemp(prefix="hivem1-")
        cls.script = os.path.join(cls.tmp, "stub_openai.py")
        with open(cls.script, "w", encoding="utf-8") as f:
            f.write(STUB)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.rt.call(cls._owner(), "service.remove", {"name": "stub", "wait": 10})
        except Exception:  # noqa: BLE001
            pass
        cls.rt.test_stop()
        os.unlink(cls.tokfile)
        import shutil
        shutil.rmtree(cls.tmp, ignore_errors=True)
        cls.home.__exit__(None, None, None)

    @classmethod
    def _owner(cls):
        from hive.auth import system_identity
        return system_identity()

    def call(self, p, mid, name, args, timeout=90):
        p.send({"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": args}})
        r = p.recv(mid, timeout=timeout)["result"]
        if os.environ.get("HIVE_TEST_SHOW"):
            print(f"\n>>> {name} {args}\n{r['content'][0]['text']}")
        return r["content"][0]["text"], r["isError"]

    def test_m1(self):
        env = dict(os.environ)
        env.pop("HIVE_URL", None)
        p = McpProc(["--url", self.url, "--token-file", self.tokfile, "--wait-budget", "30"], env=env)
        try:
            p.send({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {"protocolVersion": "2025-11-25"}})
            p.recv(0)
            p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            text, err = self.call(p, 1, "shell_run", {"command": "echo m1-ok"})
            self.assertFalse(err, text)
            self.assertIn("m1-ok", text)
            text, err = self.call(p, 2, "job_start", {"command": "sleep 1; echo job-done"})
            self.assertFalse(err, text)
            job = re.search(r"job (j_[A-Za-z0-9_-]+)", text).group(1)
            text, err = self.call(p, 3, "job_status", {"job_id": job, "wait": 20})
            self.assertFalse(err, text)
            self.assertIn("exited · exit 0", text)
            self.assertIn("job-done", text)
            port = helpers.free_port()
            text, err = self.call(p, 4, "service_define", {
                "name": "stub", "command": f"{sys.executable} {self.script} {port}", "port": port,
                "endpoint": f"http://127.0.0.1:{port}/v1", "options": ["start_period=30", "health_interval=2"],
                "wait": 20})
            self.assertFalse(err, text)
            text, err = self.call(p, 5, "service_status", {"name": "stub", "wait": 20})
            self.assertFalse(err, text)
            self.assertIn("healthy", text)
            self.assertIn("hive gateway url", text)
            text, err = self.call(p, 6, "model_chat", {"service": "stub", "prompt": "ping"})
            self.assertFalse(err, text)
            self.assertIn("pong: ping", text)
            self.assertIn("stub-model", text)
            text, err = self.call(p, 7, "service_control", {"op": "remove", "name": "stub", "wait": 20})
            self.assertFalse(err, text)
        finally:
            self.assertEqual(p.close(), 0)


if __name__ == "__main__":
    unittest.main()
