"""WP5a client tests (§15.2, §14.4, D45; §21.2 relay/hub/client row: client parts)."""
from __future__ import annotations

import json
import os
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List
from unittest import mock

from hive import client as hc
from hive import config, util
from hive.client import Client, HttpTarget, JobHandle, NodeHandle, ServiceHandle, Target, connect, split_location
from hive.errors import HiveError
from hive.transport import HttpTransport

from tests import helpers

ENV_KEYS = ("HIVE_URL", "HIVE_TOKEN", "HIVE_TOKEN_FILE", "HIVE_TLS_PIN", "HIVE_CONNECTION", "HIVE_NODE",
            "HIVE_PROXY")


def clean_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ENV_KEYS}


class FakeNode:
    """A StubHTTPServer answering /v1/call per action with scripted functions; records every call body."""

    def __init__(self) -> None:
        self.stub = helpers.StubHTTPServer()
        self.calls: List[dict] = []
        self.handlers: Dict[str, Any] = {}
        self.lock = threading.Lock()

        def call(req: dict):
            body = json.loads(req["body"])
            with self.lock:
                self.calls.append(body)
            fn = self.handlers.get(body["action"])
            if fn is None:
                return 404, {"ok": False, "error": {"code": "not_found", "message": "no handler"}}, {}
            try:
                return 200, {"ok": True, "result": fn(body["params"])}, {}
            except HiveError as e:
                return 400, {"ok": False, "error": e.to_dict()}, {}
        self.stub.add_handler("POST", "/v1/call", lambda r: (lambda s, b, h: (s, h, b))(*call(r)))
        self.stub.add("GET", "/v1/whoami", 200, {"ok": True, "result": {"server": {"role": "hub", "name": "h"}}})
        self.stub.add("GET", "/v1/nodes", 200, {"ok": True, "result": {"nodes": [{"name": "a"}, {"name": "b"}]}})
        self.stub.add_handler("GET", "/v1/actions", lambda r: (200, {}, {"ok": True, "result": {
            "node": None, "catalog_hash": "x", "actions": [{"name": "fs.read", "q": r["full_path"]}]}}))
        self.stub.add_handler("POST", "/v1/cancel", lambda r: (200, {}, {"ok": True, "result": {
            "cancelled": json.loads(r["body"])["request_id"] == "r_live"}}))
        self.stub.start()
        self.client = Client(HttpTarget(self.stub.url, "hv_tok"), default_node="desk")

    def stop(self) -> None:
        self.client.close()
        self.stub.stop()


class TestTargetAndClient(unittest.TestCase):
    def setUp(self):
        self.f = FakeNode()
        self.addCleanup(self.f.stop)

    def test_target_basics(self):
        t = self.f.client.target
        self.assertIsInstance(t, Target)
        self.assertEqual(t.kind, "hub")                          # learned from whoami.server.role
        self.assertEqual([n["name"] for n in t.nodes()], ["a", "b"])
        self.assertEqual(t.actions("pod a")[0]["q"], "/v1/actions?node=pod%20a")
        self.assertEqual(t.actions()[0]["q"], "/v1/actions")
        self.assertTrue(t.cancel("r_live"))
        self.assertFalse(self.f.client.cancel("r_other"))
        self.assertEqual(self.f.client.whoami()["server"]["role"], "hub")
        unreachable = HttpTarget(f"http://127.0.0.1:{helpers.free_port()}", "hv_x")
        with mock.patch("hive.transport.time.sleep"):
            self.assertEqual(unreachable.kind, "unknown")
        self.assertEqual(HttpTarget("http://127.0.0.1:1", kind="node").kind, "node")
        self.assertEqual(object.__new__(HttpTarget).kind, "unknown")
        with self.assertRaises(HiveError):
            HttpTarget(HttpTransport("http://127.0.0.1:1"), "hv_x")

    def test_call_body(self):
        self.f.handlers["node.info"] = lambda p: {"name": "desk", "masked": True}
        r = self.f.client.call("node.info")
        self.assertEqual(r, {"name": "desk", "masked": True})
        body = self.f.calls[-1]
        self.assertEqual((body["action"], body["params"], body["node"]), ("node.info", {}, "desk"))
        self.assertRegex(body["request_id"], r"^r_[A-Za-z0-9_.:-]{1,78}$")
        self.assertNotIn("timeout", body)
        self.assertNotIn("approval_id", body)
        self.f.client.call("node.info", node="other", timeout=12, request_id="r_mine", approval_id="ap_1")
        body = self.f.calls[-1]
        self.assertEqual((body["node"], body["timeout"], body["request_id"], body["approval_id"]),
                         ("other", 12, "r_mine", "ap_1"))
        ids = set()
        for _ in range(5):
            self.f.client.call("node.info")
            ids.add(self.f.calls[-1]["request_id"])
        self.assertEqual(len(ids), 5)
        with self.assertRaises(HiveError) as cm:
            self.f.client.call("fs.nothing")
        self.assertEqual(cm.exception.code, "not_found")

    def test_toolkit_and_transfer_delegate(self):
        with mock.patch("hive.tools.Toolkit") as tk:
            self.f.client.toolkit(profile="minimal")
        tk.assert_called_once_with(self.f.client, profile="minimal")
        from hive import transfer
        with mock.patch.object(transfer, "copy", return_value={"bytes": 1}) as cp, \
                mock.patch.object(transfer, "LocalFS", side_effect=lambda roots: ("local", roots)), \
                mock.patch.object(transfer, "NodeFS", side_effect=lambda target, node: ("node", node)):
            self.assertEqual(self.f.client.transfer("local:/tmp/a", "pod-a:/w/a", overwrite=True), {"bytes": 1})
            args, kw = cp.call_args
            self.assertEqual(args, (("local", None), "/tmp/a", ("node", "pod-a"), "/w/a"))
            self.assertTrue(kw["overwrite"])
            self.f.client.node("desk").download("/r/x", "/l/x")
            self.assertEqual(cp.call_args[0], (("node", "desk"), "/r/x", ("local", None), "/l/x"))
            self.f.client.node().upload("/l/y", "/r/y")
            self.assertEqual(cp.call_args[0][2], ("node", "desk"))       # the client's default node

    def test_split_location(self):
        self.assertEqual(split_location("local:C:\\x\\y"), ("local", "C:\\x\\y"))
        self.assertEqual(split_location("Desk-WSL:/home/me/a"), ("desk-wsl", "/home/me/a"))
        for bad in ("/tmp/x", "local:", "bad name:/x", ":x"):
            with self.subTest(bad=bad), self.assertRaises(HiveError):
                split_location(bad)


class TestHandles(unittest.TestCase):
    def setUp(self):
        self.f = FakeNode()
        self.addCleanup(self.f.stop)
        self.n = self.f.client.node("pod-a")

    def test_sh(self):
        self.f.handlers["shell.run"] = lambda p: (
            {"state": "exited", "exit_code": 0, "stdout": "hi\n", "stderr": ""} if p["command"] == "ok"
            else {"state": "exited", "exit_code": 3, "stdout": "", "stderr": "boom"})
        self.assertEqual(self.n.sh("ok"), "hi\n")
        self.assertEqual(self.f.calls[-1]["params"], {"command": "ok", "merge_stderr": False})
        self.assertEqual(self.f.calls[-1]["node"], "pod-a")
        with self.assertRaises(HiveError) as cm:
            self.n.sh("bad")
        self.assertEqual((cm.exception.code, cm.exception.details["exit_code"], cm.exception.details["stderr"]),
                         ("internal", 3, "boom"))
        self.assertEqual(self.n.shell("x", cwd="/w")["exit_code"], 3)
        self.assertEqual(self.f.calls[-1]["params"], {"command": "x", "cwd": "/w"})

    def test_read_text_pages(self):
        lines = [f"line {i}\n" for i in range(1, 8)]

        def read(p):
            s = p["start_line"]
            page = lines[s - 1:s + 2]
            nxt = s + 3 if s + 3 <= len(lines) else None
            return {"content": "".join(page), "next_start_line": nxt}
        self.f.handlers["fs.read"] = read
        self.assertEqual(self.n.read_text("/a.txt", max_lines=3), "".join(lines))
        self.assertEqual([c["params"]["start_line"] for c in self.f.calls], [1, 4, 7])
        self.assertEqual(self.f.calls[0]["params"]["max_lines"], 3)
        self.f.handlers["fs.read"] = lambda p: {"content": "x", "next_start_line": 1}
        with self.assertRaises(HiveError):
            self.n.read_text("/loop")

    def test_simple_mappings(self):
        echo = lambda p: dict(p)  # noqa: E731
        for a in ("fs.write", "fs.edit", "fs.list", "fs.search", "fs.stat", "system.info", "node.info",
                  "service.define"):
            self.f.handlers[a] = echo
        self.assertEqual(self.n.write_text("/a", "t", mode="append"), {"path": "/a", "content": "t", "mode": "append"})
        self.assertEqual(self.n.edit("/a", "o", "n"), {"path": "/a", "old_text": "o", "new_text": "n"})
        self.assertEqual(self.n.ls(), {"path": "."})
        self.assertEqual(self.n.search("x", "/w", glob="*.py"), {"pattern": "x", "path": "/w", "glob": "*.py"})
        self.assertEqual(self.n.stat("/a"), {"path": "/a"})
        self.assertEqual(self.n.sys_info(["gpu"]), {"sections": ["gpu"]})
        self.assertEqual(self.n.sys_info(), {})
        self.assertEqual(self.n.info(), {})
        self.assertEqual(self.n.define_service("qwen", preset="vllm"), {"name": "qwen", "preset": "vllm"})
        self.f.handlers["model.chat"] = lambda p: {"content": "pong:" + (p.get("prompt") or p["messages"][0]["content"])}
        self.assertEqual(self.n.chat("ping", service="qwen"), "pong:ping")
        self.assertEqual(self.n.chat([{"role": "user", "content": "m"}]), "pong:m")
        self.f.handlers["net.download"] = lambda p: {"job_id": "j_dl", "path": p["path"], "state": "running"}
        jh = self.n.fetch("https://x/y", "/d/y")
        self.assertIsInstance(jh, JobHandle)
        self.assertEqual((jh.job_id, jh.node), ("j_dl", "pod-a"))
        self.f.handlers["model.pull"] = lambda p: ({"files": [], "fits": True} if p.get("dry_run")
                                                   else {"job_id": "j_pull", "state": "running"})
        self.assertEqual(self.n.pull_model("org/m", dry_run=True), {"files": [], "fits": True})
        self.assertEqual(self.n.pull_model("org/m").job_id, "j_pull")

    def test_job_handle(self):
        states = iter(["running", "running", "exited"])
        waits: List[float] = []

        def status(p):
            waits.append(p.get("wait"))
            return {"job_id": p["job_id"], "state": next(states)}
        self.f.handlers["job.start"] = lambda p: {"job_id": "j_1", "state": "running"}
        self.f.handlers["job.status"] = status
        self.f.handlers["job.stop"] = lambda p: {"job_id": p["job_id"], "state": "killed", "force": p.get("force")}
        self.f.handlers["job.logs"] = lambda p: {"text": "log", "job_id": p["job_id"]}
        jh = self.n.start_job("sleep 1", name="s")
        self.assertEqual(self.f.calls[-1]["params"], {"command": "sleep 1", "name": "s"})
        self.assertEqual(jh.wait()["state"], "exited")
        self.assertEqual(waits, [30.0, 30.0, 30.0])
        self.assertEqual(jh.stop(force=True)["force"], True)
        self.assertEqual(jh.logs(tail_lines=5)["text"], "log")
        self.assertEqual(self.f.calls[-1]["params"], {"tail_lines": 5, "job_id": "j_1"})
        states2 = iter(["running"] * 100)
        self.f.handlers["job.status"] = lambda p: (waits.append(p["wait"]), {"state": next(states2)})[1]
        waits.clear()
        self.assertEqual(self.n.job("j_2").wait(timeout=0)["state"], "running")
        self.assertEqual(waits, [0.0])

    def test_service_handle(self):
        for a in ("service.status", "service.start", "service.stop", "service.restart", "service.logs",
                  "service.remove"):
            self.f.handlers[a] = (lambda name: (lambda p: dict(p, action=name)))(a)
        sh = self.n.service("qwen")
        self.assertIsInstance(sh, ServiceHandle)
        self.assertEqual(sh.status()["action"], "service.status")
        self.assertEqual(sh.start(wait=5), {"name": "qwen", "wait": 5, "action": "service.start"})
        self.assertEqual(sh.stop()["action"], "service.stop")
        self.assertEqual(sh.restart()["wait"], 0)
        self.assertEqual(sh.logs(tail_lines=3)["tail_lines"], 3)
        self.assertEqual(sh.remove()["action"], "service.remove")
        self.assertTrue(all(c["node"] == "pod-a" for c in self.f.calls))

    def test_follow_logs_job(self):
        script = iter([
            {"text": "a\n", "next_offset": 2, "state": "running", "eof": True},
            {"text": "", "next_offset": 2, "state": "running", "eof": True},
            {"text": "b\n", "next_offset": 4, "state": "exited", "eof": True},
            {"text": "", "next_offset": 4, "state": "exited", "eof": True},
        ])
        offsets: List[Any] = []
        self.f.handlers["job.logs"] = lambda p: (offsets.append(p.get("offset")), next(script))[1]
        out = list(self.n.follow_logs(job_id="j_1", poll=0.01))
        self.assertEqual(out, ["a\n", "b\n"])
        self.assertEqual(offsets, [None, 2, 2, 4])
        with self.assertRaises(HiveError):
            next(self.n.follow_logs())

    def test_follow_logs_service(self):
        n = {"i": 0}

        def logs(p):
            n["i"] += 1
            return {"text": f"l{n['i']}\n", "next_offset": n["i"], "state": "running"}
        self.f.handlers["service.logs"] = logs
        gen = self.n.follow_logs(service="qwen", poll=0.01)
        self.assertEqual([next(gen) for _ in range(3)], ["l1\n", "l2\n", "l3\n"])
        gen.close()
        self.assertEqual(self.f.calls[-1]["params"], {"name": "qwen", "offset": 2})


class TestRealNode(unittest.TestCase):
    """HttpTarget/Client/NodeHandle against a real node server (WP1 + WP2 handlers)."""

    @classmethod
    def setUpClass(cls):
        cls.url, cls.token, cls.rt = helpers.start_node_server(start_managers=True)
        cls.client = connect(cls.url, cls.token)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.rt.test_stop()

    def test_identity_nodes_actions(self):
        c = self.client
        self.assertEqual(c.target.kind, "node")
        self.assertEqual(c.whoami()["identity"]["kind"], "agent")
        rows = c.nodes()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mode"], "self")
        names = [a["name"] for a in c.actions()]
        self.assertIn("node.info", names)
        self.assertIn("node.info", c.target.actions_info()["implemented"])
        self.assertEqual(c.node().info()["name"], self.rt.name)
        self.assertEqual(c.call("node.info", node="self")["name"], self.rt.name)
        self.assertFalse(c.cancel("r_not_running"))

    def test_shell_and_job(self):
        n = self.client.node()
        try:
            out = n.sh("echo hive-client-ok")
        except HiveError as e:
            if e.code in ("unsupported", "not_found"):
                self.skipTest(f"shell.run not available: {e.message}")
            raise
        self.assertEqual(out.strip(), "hive-client-ok")
        with self.assertRaises(HiveError) as cm:
            n.sh("exit 3")
        self.assertEqual(cm.exception.details["exit_code"], 3)
        jh = n.start_job("echo from-job")
        st = jh.wait(timeout=30)
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0))
        self.assertIn("from-job", "".join(n.follow_logs(job_id=jh.job_id, poll=0.05)))


class TestConnect(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, clean_env(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.home_cm = helpers.tmp_home()
        self.home = self.home_cm.__enter__()
        self.addCleanup(self.home_cm.__exit__, None, None, None)

    def add(self, name: str, url: str, token: str, **kw: Any) -> None:
        config.init_client(url=url, token=token, name=name, **kw)

    def test_default_named_and_local(self):
        self.add("home", "http://127.0.0.1:7700", "hv_home", make_default=True)
        self.add("this-pc", "http://127.0.0.1:7701", "hv_agent")
        self.add("this-pc-owner", "http://127.0.0.1:7701", "hv_owner")
        c = connect()
        self.assertEqual((c.connection, c.target.url), ("home", "http://127.0.0.1:7700"))
        self.assertEqual(c.target.transport._token, "hv_home")
        self.assertIsNotNone(c.config)
        c = connect("local")
        self.assertEqual(c.connection, "this-pc")
        self.assertEqual(c.target.transport._token, "hv_agent")      # never the owner token (D45, §5.1)
        self.assertIn("hive node run", c.target.transport.offline_hint)
        c = Client.from_config(connection="this-pc-owner")
        self.assertEqual(c.target.transport._token, "hv_owner")
        with mock.patch.dict(os.environ, {"HIVE_CONNECTION": "this-pc"}):
            self.assertEqual(connect().connection, "this-pc")
        with self.assertRaises(HiveError) as cm:
            connect("hom")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(cm.exception.details["suggestions"], ["home"])
        with self.assertRaises(HiveError):
            connect("home", bogus=1)

    def test_local_without_daemon_hint(self):
        port = helpers.free_port()
        self.add("this-pc", f"http://127.0.0.1:{port}", "hv_agent")
        c = connect("local")
        with mock.patch("hive.transport.time.sleep"):
            with self.assertRaises(HiveError) as cm:
                c.call("node.info")
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "connect", False))
        self.assertEqual(e.hint, "start it: hive node run (or hive service install node)")

    def test_local_missing_connection(self):
        with self.assertRaises(HiveError) as cm:
            connect("local")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("hive init node", cm.exception.hint)
        with self.assertRaises(HiveError) as cm:
            connect()
        self.assertIn("hive init client", cm.exception.hint)

    def test_env_overrides(self):
        self.add("home", "http://127.0.0.1:7700", "hv_home", make_default=True)
        tf = self.home / "env.token"
        tf.write_text("hv_from_file\n")
        with mock.patch.dict(os.environ, {"HIVE_URL": "https://127.0.0.1:7999", "HIVE_TOKEN_FILE": str(tf),
                                          "HIVE_TLS_PIN": "sha256:" + "aa" * 32, "HIVE_NODE": "pod-a"}):
            c = connect()
            self.assertEqual(c.target.url, "https://127.0.0.1:7999")
            self.assertEqual(c.target.transport._token, "hv_from_file")
            self.assertEqual(c.target.transport.tls_pin, "sha256:" + "aa" * 32)
            self.assertEqual(c.default_node, "pod-a")
            with mock.patch.dict(os.environ, {"HIVE_TOKEN": "hv_env"}):
                self.assertEqual(connect().target.transport._token, "hv_env")
                # explicit args come first
                self.assertEqual(connect(token="hv_arg").target.transport._token, "hv_arg")
                self.assertEqual(connect("home").target.transport._token, "hv_home")
                c = connect("https://127.0.0.1:7999")
                self.assertEqual(c.target.transport._token, "hv_env")
                self.assertEqual(c.target.transport.tls_pin, "sha256:" + "aa" * 32)  # same URL as HIVE_URL
                c = connect("https://127.0.0.1:8000", default_node="x", timeout=5)
                self.assertIsNone(c.target.transport.tls_pin)                       # pin is for HIVE_URL only
                self.assertEqual((c.default_node, c.target.transport.timeout), ("x", 5.0))

    def test_connection_fields(self):
        raw = {"version": 1, "default": "lan", "connections": {
            "lan": {"url": "https://127.0.0.1:7700", "token_file": "tokens/lan.token",
                    "tls_pin": "sha256:" + "bb" * 32, "default_node": "desk", "timeout": 30, "proxy": "off"},
            "gone": {"url": "http://127.0.0.1:7700", "token_file": "tokens/missing.token"}}}
        (self.home / "tokens").mkdir(exist_ok=True)
        (self.home / "tokens" / "lan.token").write_text("hv_lan\n")
        util.write_json_atomic(self.home / "client.json", raw, private=True)
        c = connect()
        tr = c.target.transport
        self.assertEqual((tr._token, tr.tls_pin, tr.timeout, tr.proxy, c.default_node),
                         ("hv_lan", "sha256:" + "bb" * 32, 30.0, "off", "desk"))
        self.assertEqual(connect(proxy="http://127.0.0.1:7890").target.transport.proxy, "http://127.0.0.1:7890")
        with self.assertRaises(HiveError) as cm:
            connect("gone")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("token file", cm.exception.message)

    def test_config_path_argument(self):
        p = Path(self.home) / "other.json"
        util.write_json_atomic(p, {"version": 1, "default": "x",
                                   "connections": {"x": {"url": "http://127.0.0.1:7709", "token": "hv_inline"}}},
                               private=True)
        c = connect(config=str(p))
        self.assertEqual((c.connection, c.target.transport._token), ("x", "hv_inline"))
        self.assertEqual(Client.from_config(str(p)).connection, "x")


if __name__ == "__main__":
    unittest.main()
