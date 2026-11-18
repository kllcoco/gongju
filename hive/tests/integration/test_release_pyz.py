"""Phase 2 release gate (§20.2, §22.1 Phase 2): ``hive dist build`` produces a self-contained ``hive.pyz`` that runs
a real node from a folder without the source checkout: ``--version``, ``init node``, ``node run``, ``sh`` (remote
exit code), a job, MCP ``tools/list`` over stdio, ``node stop``. Loopback only, no network."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hive import __version__
from tests.integration import m2util


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestReleasePyz(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="hive-release-"))
        cls.home = cls.tmp / "home"
        cls.work = cls.tmp / "work"      # cwd without the source checkout: the pyz must carry everything
        cls.work.mkdir()
        cls.env = m2util.base_env(HIVE_HOME=str(cls.home), HOME=str(cls.tmp / "user"),
                                  USERPROFILE=str(cls.tmp / "user"))
        (cls.tmp / "user").mkdir()
        r = m2util.run_cli(["dist", "build", "--out", str(cls.work / "hive.pyz")], env=cls.env)
        assert r.returncode == 0, r.stdout + r.stderr
        cls.pyz = [sys.executable, str(cls.work / "hive.pyz")]
        cls.port = _free_port()
        cls.sup = None

    @classmethod
    def tearDownClass(cls):
        if cls.sup is not None:
            cls.sup.kill()
        shutil.rmtree(str(cls.tmp), ignore_errors=True)

    def cli(self, *argv, **kw):
        return m2util.run_cli(list(argv), env=self.env, cwd=str(self.work), prefix=self.pyz, **kw)

    def test_pyz_node_smoke(self):
        r = self.cli("--version")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), f"hive {__version__}")

        r = self.cli("init", "node", "--yes", "--name", "smoke", "--listen", f"127.0.0.1:{self.port}",
                     "--proxy", "off")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("hive.pyz", r.stdout)            # the printed MCP snippet runs this pyz
        m2util.allow_root(self.home / "node" / "config.json")

        type(self).sup = m2util.Supervisor(self.pyz + ["node", "run"], env=self.env, cwd=str(self.work),
                                           log=self.tmp / "node.log", name="pyz-node").start()
        m2util.wait_for(lambda: self.cli("nodes").returncode == 0, timeout=60, what="pyz node up")

        r = self.cli("sh", "self", "--", "echo hello-pyz")
        self.assertEqual((r.returncode, r.stdout.strip()), (0, "hello-pyz"), r.stderr)
        r = self.cli("sh", "self", "--", "exit 7")       # sh / PowerShell alike
        self.assertEqual(r.returncode, 7, r.stdout + r.stderr)

        r = self.cli("call", "job.start", "command=echo job-out", "--raw")
        self.assertEqual(r.returncode, 0, r.stderr)
        job_id = json.loads(r.stdout)["job_id"]
        m2util.wait_for(lambda: "job-out" in self.cli("logs", "self", job_id).stdout, timeout=30, what="job log")

        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                 "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                            "clientInfo": {"name": "release-test", "version": "0"}}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        r = self.cli("mcp", "--local", stdin="".join(json.dumps(x) + "\n" for x in reqs), timeout=60)
        replies = [json.loads(line) for line in r.stdout.splitlines() if line.strip()]
        tools = [x for x in replies if x.get("id") == 2][0]["result"]["tools"]
        self.assertEqual(len(tools), 28)

        r = self.cli("node", "stop")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.sup.wait(30), self.sup.output())
        self.assertEqual(self.sup.exits[-1], 0)
        self.assertNotIn("Traceback", self.sup.output())
        self.assertNotIn("cannot restrict permissions", self.sup.output())


@unittest.skipIf(os.name == "nt", "POSIX file modes")
class TestPrivateAtomicWrite(unittest.TestCase):
    def test_private_write_is_0600_without_path_chmod(self):
        from unittest import mock
        from hive import util
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            with mock.patch.object(util, "secure_file", side_effect=AssertionError("path chmod on POSIX")):
                util.write_json_atomic(p, {"a": 1}, private=True)
            self.assertEqual(os.stat(str(p)).st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(p.read_text(encoding="utf-8")), {"a": 1})


if __name__ == "__main__":
    unittest.main()
