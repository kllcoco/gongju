"""E2E-M1 (§21.2 Integration row, §22.1 step 3): one direct node on loopback, driven end to end through real
processes only.

* ``hive init node`` (real CLI subprocess) writes the node config, tokens and client.json in a temp ``HIVE_HOME``;
* ``hive node run`` (real node process) serves on a free 127.0.0.1 port;
* ``hive mcp --local`` (real MCP stdio subprocess, agent token "this-pc"): shell_run → job_start / job_status(wait) →
  fs_write / fs_read / fs_edit / fs_search / fs_list / fs_op → service_define a stub OpenAI server (preset llamacpp
  with a fake ``llama-server`` on POSIX) → service_status(wait) → model_chat → service_control remove;
* ``hive call`` / ``hive sh`` / ``hive node status`` / ``hive node stop`` (real CLI subprocesses, owner token
  "this-pc-owner" and agent token "this-pc") against the same node, cross-checking what the MCP session did.

Windows (D40 relaunch, PowerShell default shell) cannot run in this CI; the Windows branch defines the stub service
through ``options=['argv=[...]']`` instead of the POSIX shebang binary and uses commands every shell accepts.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
import urllib.request
from pathlib import Path

from hive import proctree, util

from tests import helpers
from tests.test_mcp import ROOT, McpProc

IS_WINDOWS = os.name == "nt"

# A tiny OpenAI-compatible server. Arguments: llama-server style (``-m MODEL --port N ...``) or a bare port.
STUB = textwrap.dedent("""\
    import json, os, sys
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]) if "--port" in args else int(args[0])
    model = os.path.basename(args[args.index("-m") + 1]) if "-m" in args else "stub-model"

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/health":
                self._send(200, {"status": "ok"})
            elif path in ("/v1/models", "/models"):
                self._send(200, {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "stub"}]})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            text = "pong: " + req["messages"][-1]["content"]
            self._send(200, {"id": "c1", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

        def log_message(self, *a):
            pass

    print("stub listening on", port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
""")

# variables that would point the CLI/MCP at another server or route loopback through a proxy
SCRUB = ("HIVE_URL", "HIVE_TOKEN", "HIVE_TOKEN_FILE", "HIVE_CONNECTION", "HIVE_NODE", "HIVE_PROXY", "HIVE_TLS_PIN",
         "HIVE_MCP_WATCH_INTERVAL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY",
         "all_proxy")


def _http_json(url: str, timeout: float = 2.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


class TestE2EM1(unittest.TestCase):
    """The M1 vertical slice through real processes (node, MCP stdio, CLI)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="hivem1-")).resolve()
        cls.home = cls.base / "hive-home"           # HIVE_HOME (no `.hive` component)
        cls.user_home = cls.base / "user"           # HOME of the node process (fs root "~", workdir)
        cls.work = cls.base / "work"                # an extra rw root
        cls.outside = cls.base / "outside"          # not under any root
        for d in (cls.home, cls.user_home, cls.work, cls.outside):
            d.mkdir()
        (cls.outside / "secret.txt").write_text("not for agents\n", encoding="utf-8")
        env = {k: v for k, v in os.environ.items() if k not in SCRUB}
        env["HIVE_HOME"] = str(cls.home)
        env["PYTHONPATH"] = str(ROOT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if not IS_WINDOWS:
            env["HOME"] = str(cls.user_home)
        cls.env = env
        cls.node_proc = None
        cls.node_log = None
        cls.service_pids = []
        try:
            cls._start_node()
        except BaseException:
            cls._teardown()
            raise

    @classmethod
    def _start_node(cls) -> None:
        cls.port = helpers.free_port()
        r = cls.cli("init", "node", "--yes", "--name", "m1", "--listen", f"127.0.0.1:{cls.port}",
                    "--root", str(cls.work), "--proxy", "off", "--mirror", "none")
        if r.returncode != 0:
            raise AssertionError(f"hive init node failed ({r.returncode}): {r.stdout}{r.stderr}")
        cfg_path = cls.home / "node" / "config.json"
        if not IS_WINDOWS and os.geteuid() == 0:
            # CI containers run as root; a home user's node refuses that unless the owner opts in (§13.1)
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            raw["allow_root"] = True
            cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            os.chmod(str(cfg_path), 0o600)
        cls.node_log = open(str(cls.base / "node.log"), "wb")
        cls.node_proc = subprocess.Popen([sys.executable, "-m", "hive", "node", "run"], cwd=str(ROOT), env=cls.env,
                                         stdin=subprocess.DEVNULL, stdout=cls.node_log, stderr=subprocess.STDOUT)
        url = f"http://127.0.0.1:{cls.port}/v1/health"
        end = time.monotonic() + 60
        while True:
            if cls.node_proc.poll() is not None:
                raise AssertionError(f"hive node run exited with {cls.node_proc.returncode}: {cls._node_output()}")
            try:
                if _http_json(url).get("result", {}).get("role") == "node":
                    return
            except (OSError, ValueError):
                pass
            if time.monotonic() > end:
                raise AssertionError(f"node did not answer {url} within 60 s: {cls._node_output()}")
            time.sleep(0.1)

    @classmethod
    def _node_output(cls) -> str:
        try:
            return (cls.base / "node.log").read_text(encoding="utf-8", errors="replace")[-4000:]
        except OSError:
            return ""

    @classmethod
    def tearDownClass(cls) -> None:
        cls._teardown()

    @classmethod
    def _teardown(cls) -> None:
        proc = cls.node_proc
        if proc is not None and proc.poll() is None:
            cls.cli("call", "service.remove", "name=llm", "wait=10")   # no-op when the test removed it already
            cls.cli("node", "stop")
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        for pid in cls.service_pids:                     # a stub server left behind by a failed test
            if util.pid_alive(pid):
                try:
                    proctree.kill_tree(pid, grace=1)
                except Exception:  # noqa: BLE001 - best effort cleanup
                    pass
        if cls.node_log is not None:
            cls.node_log.close()
        shutil.rmtree(str(cls.base), ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------------
    # process helpers

    @classmethod
    def cli(cls, *argv: str, timeout: float = 120) -> "subprocess.CompletedProcess[str]":
        """Run the real CLI (``python -m hive …``) against the test HIVE_HOME."""
        r = subprocess.run([sys.executable, "-m", "hive"] + list(argv), cwd=str(ROOT), env=cls.env,
                           stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout)
        out = r.stdout.decode("utf-8", "replace")
        err = r.stderr.decode("utf-8", "replace")
        if os.environ.get("HIVE_TEST_SHOW"):
            print(f"\n$ hive {' '.join(argv)}  -> {r.returncode}\n{out}{err}")
        return subprocess.CompletedProcess(r.args, r.returncode, out, err)

    def cli_json(self, *argv: str) -> dict:
        r = self.cli("--json", *argv)
        self.assertEqual(r.returncode, 0, f"hive {' '.join(argv)}: {r.stdout}{r.stderr}")
        return json.loads(r.stdout)

    def tool(self, p: McpProc, name: str, args: dict, *, expect_error: bool = False, timeout: float = 90) -> str:
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

    # -----------------------------------------------------------------------------------------------------------
    # the stub model server

    def stub_service_args(self, port: int) -> dict:
        """service_define arguments for the stub OpenAI server: preset llamacpp on POSIX (fake binary), plain argv
        on Windows."""
        script = self.work / "bin" / "stub_openai.py"
        script.parent.mkdir(exist_ok=True)
        script.write_text(STUB, encoding="utf-8")
        if IS_WINDOWS:  # pragma: no cover - Windows only
            return {"name": "llm", "port": port, "endpoint": f"http://127.0.0.1:{port}/v1",
                    "options": ["argv=" + json.dumps([sys.executable, str(script), str(port)]),
                                "endpoint_kind=openai", "health=http", f"health_url=http://127.0.0.1:{port}/health",
                                "start_period=60", "health_interval=2"],
                    "wait": 30}
        binary = self.work / "bin" / "llama-server"     # a sh wrapper: sys.executable may contain spaces
        binary.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n',
                          encoding="utf-8")
        binary.chmod(0o755)
        model = self.work / "models" / "tiny-q4.gguf"
        model.parent.mkdir(exist_ok=True)
        model.write_bytes(b"GGUF fake weights")
        return {"name": "llm", "preset": "llamacpp",
                "preset_args": [f"model_path={model}", f"binary={binary}", "n_gpu_layers=0", "ctx_size=2048"],
                "port": port, "options": ["health_interval=2"], "wait": 30}

    # -----------------------------------------------------------------------------------------------------------

    def test_m1(self) -> None:
        self._mid = 0
        model_id = "stub-model" if IS_WINDOWS else "tiny-q4.gguf"

        # --- CLI: the daemon is up and the owner sees it
        r = self.cli("node", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("node 'm1' running", r.stdout)
        info = self.cli_json("call", "node.info")
        self.assertEqual(info["name"], "m1")
        self.assertEqual(info["modes"]["listen"], f"127.0.0.1:{self.port}")

        # --- MCP stdio session with the agent connection written by init (this-pc)
        p = McpProc(["--local"], env=self.env)
        try:
            p.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "e2e-m1", "version": "1"}}})
            init = p.recv(0, timeout=60)["result"]
            self.assertEqual(init["serverInfo"]["name"], "hive")
            self.assertTrue(init.get("instructions"))
            p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            p.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            names = {t["name"] for t in p.recv(1)["result"]["tools"]}
            for t in ("shell_run", "job_start", "job_status", "fs_read", "fs_write", "fs_edit", "fs_search",
                      "fs_list", "fs_op", "service_define", "service_status", "service_control", "model_chat",
                      "guide"):
                self.assertIn(t, names)
            self._mid = 1

            # shell_run
            text = self.tool(p, "shell_run", {"command": "echo m1-ok"})
            self.assertIn("[m1] exit 0", text)
            self.assertIn("m1-ok", text)
            # a short hard timeout (yield_after == timeout on the wire) is a timeout, not a yielded job
            text = self.tool(p, "shell_run", {
                "argv": [sys.executable, "-c", "import time; print('slow', flush=True); time.sleep(30)"],
                "timeout": 1})
            self.assertIn("timeout after", text)
            self.assertNotIn("background job", text)

            # job_start -> job_status(wait)
            text = self.tool(p, "job_start", {
                "argv": [sys.executable, "-c", "import time; time.sleep(1); print('job-done')"], "name": "m1job"})
            job = re.search(r"started job (j_[A-Za-z0-9_-]+)", text).group(1)
            text = self.tool(p, "job_status", {"job_id": job, "wait": 30})
            self.assertIn("exited · exit 0", text)
            self.assertIn("job-done", text)
            # the owner's CLI sees the agent's job
            st = self.cli_json("call", "job.status", f"job_id={job}")
            self.assertEqual((st["state"], st["exit_code"], st["owner"]), ("exited", 0, "this-pc"))

            # fs.*
            notes = self.work / "notes.txt"
            self.tool(p, "fs_write", {"path": str(notes), "content": "alpha\nbeta\n"})
            text = self.tool(p, "fs_read", {"path": str(notes)})
            self.assertIn("alpha", text)
            self.assertIn("untrusted data", text)
            self.tool(p, "fs_edit", {"path": str(notes), "old_text": "beta", "new_text": "gamma"})
            self.assertEqual(notes.read_text(encoding="utf-8"), "alpha\ngamma\n")
            text = self.tool(p, "fs_search", {"pattern": "gam+a", "path": str(self.work)})
            self.assertIn("notes.txt", text)
            self.tool(p, "fs_op", {"op": "copy", "path": str(notes), "dst": str(self.work / "copy.txt")})
            text = self.tool(p, "fs_list", {"path": str(self.work)})
            self.assertIn("copy.txt", text)
            self.tool(p, "fs_op", {"op": "delete", "path": str(self.work / "copy.txt")})
            self.assertFalse((self.work / "copy.txt").exists())
            text = self.tool(p, "fs_read", {"path": str(self.outside / "secret.txt")}, expect_error=True)
            self.assertIn("policy_denied", text)
            self.assertNotIn("not for agents", text)
            rd = self.cli_json("call", "fs.read", f"path={notes}")
            self.assertEqual(rd["content"], "alpha\ngamma\n")

            # service_define a stub OpenAI server -> service_status(wait) -> model_chat
            port = helpers.free_port()
            text = self.tool(p, "service_define", self.stub_service_args(port))
            self.assertIn("llm", text)
            text = self.tool(p, "service_status", {"name": "llm", "wait": 30})
            self.assertIn("healthy", text)
            st = self.cli_json("call", "service.status", "name=llm")
            self.assertEqual((st["state"], st["health"]), ("running", "healthy"))
            self.service_pids.extend(st["resources"]["pids"] or [])
            if not IS_WINDOWS:
                self.assertEqual(st["definition"]["preset"], "llamacpp")
                self.assertIn("--port", st["definition"]["argv"])
            text = self.tool(p, "model_chat", {"service": "llm", "prompt": "ping"})
            self.assertIn("pong: ping", text)
            self.assertIn(model_id, text)
            # the same endpoint through the CLI (owner) and the agent connection
            chat = self.cli_json("call", "model.chat", "service=llm", "prompt=from-cli")
            self.assertEqual(chat["content"], "pong: from-cli")
            ep = self.cli_json("--connection", "this-pc", "call", "model.endpoints")
            row = [e for e in ep["endpoints"] if e["name"] == "llm"]
            self.assertTrue(row and row[0]["healthy"] and model_id in (row[0]["models"] or []), ep)

            # --- CLI sh: remote exit code, stdout/stderr kept apart
            r = self.cli("sh", "self", "--", "echo sh-ok; exit 3")
            self.assertEqual(r.returncode, 3, r.stderr)
            self.assertIn("sh-ok", r.stdout)
            r = self.cli("--connection", "this-pc", "sh", "self", "--", "echo agent-sh")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("agent-sh", r.stdout)
            # CLI call errors: policy denial -> exit 3, unknown action -> exit 1
            r = self.cli("call", "fs.read", f"path={self.outside / 'secret.txt'}")
            self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
            self.assertIn("policy_denied", r.stderr)
            self.assertNotIn("not for agents", r.stdout + r.stderr)
            r = self.cli("call", "no.such")
            self.assertEqual(r.returncode, 1)
            self.assertIn("not_found", r.stderr)

            # cleanup through the agent session
            self.tool(p, "service_control", {"op": "remove", "name": "llm", "wait": 30})
            self.assertEqual(self.cli_json("call", "service.list")["services"], [])
        finally:
            rc = p.close()
        self.assertEqual(rc, 0, "hive mcp did not exit 0 on stdin EOF")

        # --- audit: the agent's calls are recorded under its own identity, the owner's under "owner"
        rows = []
        for f in sorted((self.home / "node" / "audit").glob("audit-*.jsonl")):
            rows += [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines() if line.strip()]
        by = {(r.get("identity"), r.get("action")) for r in rows if r.get("kind") == "call"}
        for pair in (("this-pc", "shell.run"), ("this-pc", "job.start"), ("this-pc", "fs.write"),
                     ("this-pc", "service.define"), ("this-pc", "model.chat"), ("owner", "fs.read"),
                     ("owner", "model.chat")):
            self.assertIn(pair, by)

        # --- the daemon stops gracefully: exit 0 (D55)
        r = self.cli("node", "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.node_proc.wait(timeout=30), 0, self._node_output())
        r = self.cli("node", "status")
        self.assertIn("not running", r.stdout)


if __name__ == "__main__":
    unittest.main()
