"""WP6b: examples (§22.4) compile, the MCP snippets are valid JSON, and agent_openai.py runs a tool-calling loop
against a stub OpenAI-compatible server and a real node (stdlib only; no real network)."""
from __future__ import annotations

import json
import os
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import helpers

EX = Path(__file__).resolve().parents[1] / "examples"


class TestExamples(unittest.TestCase):
    def test_files_exist_and_compile(self):
        for name in ("agent_anthropic.py", "agent_openai.py", "python_client.py", "raw_http.sh"):
            self.assertTrue((EX / name).is_file(), name)
        with tempfile.TemporaryDirectory() as d:
            for name in ("agent_anthropic.py", "agent_openai.py", "python_client.py"):
                py_compile.compile(str(EX / name), cfile=os.path.join(d, name + "c"), doraise=True)
        src = (EX / "agent_anthropic.py").read_text(encoding="utf-8")
        for needle in ('"claude-opus-5-5"', '{"type": "adaptive"}', '{"effort": "high"}', "max_tokens=16000",
                       "wait_budget=90", "server-side-fallback-2026-07-01", 'fallbacks="default"',
                       "tk.anthropic_tools()", "tk.instructions()", "destructiveHint", '"refusal"'):
            self.assertIn(needle, src)
        self.assertNotIn("tool_choice=", src)
        for p in (EX / "mcp").glob("*.json"):
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertIn("hive", data["mcpServers"])
            self.assertNotIn("hv_", p.read_text(encoding="utf-8"))

    def test_agent_openai_loop(self):
        h = helpers.tmp_home()
        h.__enter__()
        self.addCleanup(h.__exit__, None, None, None)
        if True:
            url, tok, rt = helpers.start_node_server()
            self.addCleanup(rt.test_stop)
            stub = helpers.StubHTTPServer()
            seen = []

            def chat(req):
                body = json.loads(req["body"] or b"{}")
                seen.append(body)
                if len(seen) == 1:
                    msg = {"role": "assistant", "content": None, "tool_calls": [
                        {"id": "call_1", "type": "function",
                         "function": {"name": "sys_info", "arguments": json.dumps({"sections": ["os"]})}}]}
                else:
                    msg = {"role": "assistant", "content": "done: " + str(len(body["messages"]))}
                return 200, {"Content-Type": "application/json"}, json.dumps(
                    {"choices": [{"index": 0, "message": msg, "finish_reason": "stop"}]}).encode()
            stub.add_handler("POST", "/v1/chat/completions", chat)
            stub.start()
            self.addCleanup(stub.stop)
            env = dict(os.environ, HIVE_URL=url, HIVE_TOKEN=tok, OPENAI_BASE_URL=stub.url + "/v1",
                       OPENAI_API_KEY="sk-test", OPENAI_MODEL="stub",
                       PYTHONPATH=str(EX.parent))
            for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
                env.pop(k, None)
            r = subprocess.run([sys.executable, str(EX / "agent_openai.py"), "what OS?"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(r.returncode, 0, r.stderr.decode())
            self.assertIn(b"done: 4", r.stdout)
            tool_msg = seen[1]["messages"][-1]
            self.assertEqual((tool_msg["role"], tool_msg["tool_call_id"]), ("tool", "call_1"))
            self.assertIn(f"[{rt.name}]", tool_msg["content"])
            self.assertEqual(len(seen[0]["tools"]), 28)


if __name__ == "__main__":
    unittest.main()
