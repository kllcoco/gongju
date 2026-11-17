"""WP1 NodeRuntime tests (§21.2 httpserver/node core row): the §13.2 pipeline (order, mapping, masking, holds,
approvals, gate, cancel), node routes over HTTP, pause/resume/panic, run_node exit codes, supervise()."""
from __future__ import annotations

import base64
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import catalog, node as node_mod, schema, util
from hive.actions import HANDLERS
from hive.approvals import file_fingerprint
from hive.auth import Identity
from hive.errors import HiveError, bad_request
from hive.node import pause, panic, resume
from tests import helpers
from tests.wp1util import CAUTIOUS, FILES, OPERATOR, READONLY, FakeHome, http, ident, node_server

PKG_ROOT = Path(__file__).resolve().parent.parent
SECRET = "hf_abcdefghijklmnopqrstuvwxyz0123"


class Base(unittest.TestCase):
    policy: dict = {}

    def setUp(self) -> None:
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)
        self.rt = helpers.make_runtime(start_managers=False, **self.policy)
        self.addCleanup(self.rt.stop)
        self.calls = []

    def handler(self, name: str, fn):
        p = mock.patch.dict(HANDLERS, {name: fn})
        p.start()
        self.addCleanup(p.stop)

    def call(self, who, action, params=None, **kw):
        return self.rt.call(who, action, params, **kw)


class TestPipeline(Base):
    def test_lookup_errors(self):
        self.assertEqual(self.call(OPERATOR, "nope.x")["error"]["code"], "not_found")
        self.assertEqual(self.call(OPERATOR, "hub.nodes")["error"]["code"], "bad_request")
        with mock.patch.dict(HANDLERS, {}, clear=True):
            self.assertEqual(self.call(OPERATOR, "node.info")["error"]["code"], "unsupported")
        self.assertEqual(self.call(OPERATOR, "node.info", request_id="bad id!")["error"]["code"], "bad_request")

    def test_node_info_validates_against_result_schema(self):
        env = self.call(READONLY, "node.info")
        self.assertTrue(env["ok"], env)
        schema.validate(catalog.SPECS["node.info"].result, env["result"], coerce=False, defaults=False)
        r = env["result"]
        self.assertEqual((r["name"], r["identity"]["kind"], r["policy"]["read_only"]), ("test-node", "agent", False))
        self.assertIn("node.info", r["actions"])
        self.assertIn("fs.write", r["policy"]["denied_actions"])
        self.assertEqual(env["meta"]["node"], "test-node")

    def test_order_validation_then_approval_then_execution(self):
        self.handler("shell.run", lambda ctx, p: self.calls.append(p) or {"exit_code": 0})
        env = self.call(CAUTIOUS, "shell.run", {"bogus": 1})
        self.assertEqual(env["error"]["code"], "bad_request")
        self.assertEqual(self.rt.approvals.list(), [])
        env = self.call(CAUTIOUS, "shell.run", {"command": "ls"})
        self.assertEqual(env["error"]["code"], "approval_required")
        self.assertEqual(self.calls, [])
        aid = env["error"]["details"]["approval_id"]
        owner = Identity(name="owner", kind="owner", classes=frozenset(), source_ip="127.0.0.1")
        self.rt.approvals.decide(aid, True, owner, grant_minutes=5)
        self.assertTrue(self.call(CAUTIOUS, "shell.run", {"command": "ls"}, approval_id=aid)["ok"])
        self.assertEqual(len(self.calls), 1)
        env = self.call(CAUTIOUS, "shell.run", {"command": "pwd"})          # the grant covers the next call
        self.assertTrue(env["ok"])
        recs = self.rt.audit.tail(10, action="shell.run")
        self.assertTrue(recs[-1]["extra"]["grant"].startswith("gr_"))
        hub = dataclasses.replace(CAUTIOUS, kind="hub", via="hub", name="hub-home")
        self.assertTrue(self.call(hub, "shell.run", {"command": "x"}, via="hub")["ok"])   # node never gates hub calls

    def test_given_and_ctx(self):
        seen = {}

        def h(ctx, p):
            seen.update(given=ctx.given, via=ctx.via, remaining=ctx.remaining())
            return {}
        self.handler("shell.run", h)
        self.assertTrue(self.call(OPERATOR, "shell.run", {"command": "ls", "cwd": None})["ok"])
        self.assertEqual(seen["given"], frozenset({"command"}))
        self.assertEqual(seen["via"], "direct")
        self.assertTrue(70 < seen["remaining"] <= 75)

    def test_error_mapping(self):
        def boom(ctx, p):
            raise RuntimeError("leaked hv_" + "x" * 40 + " " + SECRET)
        self.rt.secrets.set("HF_TOKEN", SECRET)
        self.handler("fs.read", boom)
        env = self.call(OPERATOR, "fs.read", {"path": "~/x"})
        self.assertEqual(env["error"]["code"], "internal")
        msg = env["error"]["message"]
        self.assertTrue(msg.startswith("RuntimeError: leaked hv_***"), msg)
        self.assertNotIn(SECRET, msg)
        self.assertNotIn("Traceback", json.dumps(env))
        self.handler("fs.write", lambda ctx, p: (_ for _ in ()).throw(TimeoutError()))
        e = self.call(OPERATOR, "fs.write", {"path": "~/x", "content": "y"})["error"]
        self.assertEqual((e["code"], e["details"]["maybe_executed"]), ("timeout", True))
        self.handler("fs.stat", lambda ctx, p: (_ for _ in ()).throw(TimeoutError()))
        self.assertIs(self.call(OPERATOR, "fs.stat", {"path": "~"})["error"]["details"]["maybe_executed"], False)
        self.handler("job.stop", lambda ctx, p: ctx.sleep(5) or {})
        e = self.call(OPERATOR, "job.stop", {"job_id": "j_abcdefg"}, timeout=1)["error"]
        self.assertEqual((e["code"], e["details"]["maybe_executed"]), ("timeout", True))
        self.handler("node.info", lambda ctx, p: ["not", "a", "dict"])
        self.assertEqual(self.call(OPERATOR, "node.info")["error"]["code"], "internal")
        self.handler("fs.list", lambda ctx, p: (_ for _ in ()).throw(bad_request("bad " + SECRET)))
        e = self.call(OPERATOR, "fs.list", {})["error"]
        self.assertEqual(e["message"], "bad «secret:HF_TOKEN»")

    def test_masking(self):
        self.rt.secrets.set("HF_TOKEN", SECRET)
        data = base64.b64encode(b"\x00\xff" + SECRET.encode() + b"\x01").decode()
        self.handler("fs.read_chunk", lambda ctx, p: {"data": data, "offset": 0, "length": 3, "size": 3,
                                                      "eof": True, "mtime": "x"})
        env = self.call(OPERATOR, "fs.read_chunk", {"path": "~/x"})
        self.assertTrue(env["ok"])
        raw = base64.b64decode(env["result"]["data"])
        self.assertNotIn(SECRET.encode(), raw)
        self.assertIn("«secret:HF_TOKEN»".encode(), raw)
        self.assertTrue(raw.startswith(b"\x00\xff"))
        self.assertIs(env["result"]["masked"], True)
        self.assertIs(env["meta"]["masked"], True)
        self.handler("fs.read", lambda ctx, p: {"text": "x " + SECRET, "nested": [{"v": SECRET}]})
        r = self.call(OPERATOR, "fs.read", {"path": "~/x"})["result"]
        self.assertEqual((r["text"], r["nested"][0]["v"], r["masked"]), ("x «secret:HF_TOKEN»", "«secret:HF_TOKEN»",
                                                                         True))
        self.handler("fs.stat", lambda ctx, p: {"exists": False, "path": "x"})
        env = self.call(OPERATOR, "fs.stat", {"path": "~/x"})
        self.assertNotIn("masked", env["result"])
        self.assertNotIn("masked", env["meta"])
        self.assertNotIn(SECRET, json.dumps(self.rt.audit.tail(50)))

    def test_too_large(self):
        self.rt.stop()
        self.rt = helpers.make_runtime(start_managers=False, limits={"max_result_bytes": 65536})
        self.handler("fs.read", lambda ctx, p: {"text": "x" * 70000})
        e = self.call(OPERATOR, "fs.read", {"path": "~/x"})["error"]
        self.assertEqual(e["code"], "too_large")

    def test_bound_files_and_holds(self):
        f = self.fh.touch("run.sh", "echo 1")
        self.handler("shell.run", lambda ctx, p: self.calls.append(ctx.hold) or {})
        e = self.call(OPERATOR, "shell.run", {"command": "x"}, bound_files={str(f): None})["error"]
        self.assertEqual((e["code"], e["message"]), ("bad_request", "bound_files is only accepted from a hub"))
        hub = ident(name="hub-home", kind="hub", via="hub")
        e = self.call(hub, "shell.run", {"command": "bash run.sh"}, bound_files={str(f): "sha256:" + "0" * 64})
        self.assertEqual(e["error"]["code"], "conflict")
        self.assertEqual(self.calls, [])
        env = self.call(hub, "shell.run", {"command": "bash run.sh"}, bound_files={str(f): file_fingerprint(str(f))})
        self.assertTrue(env["ok"])
        self.assertTrue(self.calls[0].startswith("ho_"))
        self.assertFalse(self.rt.holds._count)                   # released when the handler returned

    def test_pause_blocks_non_read(self):
        self.handler("fs.write", lambda ctx, p: {})
        self.handler("shell.run", lambda ctx, p: {})
        owner = Identity(name="owner", kind="owner", classes=frozenset(), source_ip="127.0.0.1")
        self.call(CAUTIOUS, "shell.run", {"command": "ls"})
        aid = self.rt.approvals.list()[0]["id"]
        self.rt.approvals.decide(aid, True, owner, grant_minutes=5)
        pause(self.rt.data_dir)
        self.rt._paused_cache = (0.0, False)
        e = self.call(OPERATOR, "fs.write", {"path": "~/x", "content": "y"})["error"]
        self.assertEqual(e["details"]["rule"], "paused")
        self.assertTrue(self.call(OPERATOR, "node.info")["ok"])
        self.assertTrue(self.call(OPERATOR, "node.info")["result"]["paused"])
        self.rt._watch_paused()
        self.assertEqual(self.rt.approvals.grants(), [])           # PAUSED revokes this node's grants
        resume(self.rt.data_dir)
        self.rt._paused_cache = (0.0, False)
        self.assertTrue(self.call(OPERATOR, "fs.write", {"path": "~/x", "content": "y"})["ok"])

    def test_gate_busy_and_cancel(self):
        started = threading.Event()

        def slow(ctx, p):
            started.set()
            ctx.cancel.wait(5)
            ctx.check()
            return {}
        self.handler("shell.run", slow)
        self.rt.gate = __import__("hive.limits", fromlist=["x"]).ConcurrencyGate(1)
        out = {}
        t = threading.Thread(target=lambda: out.update(self.call(OPERATOR, "shell.run", {"command": "x"},
                                                                  request_id="r_slow")))
        t.start()
        started.wait(5)
        with mock.patch.object(node_mod, "GATE_WAIT", 0.1):
            e = self.call(OPERATOR, "node.info")["error"]
        self.assertEqual((e["code"], e["details"]["reason"], e["details"]["maybe_executed"]),
                         ("unavailable", "busy", False))
        self.assertFalse(self.rt.cancel("r_slow", ident(name="other")))
        self.assertTrue(self.rt.cancel("r_slow", OPERATOR))
        t.join(5)
        self.assertEqual(out["error"]["code"], "cancelled")
        self.assertFalse(self.rt.cancel("r_slow", OPERATOR))

    def test_views(self):
        with mock.patch.dict(HANDLERS, {"shell.run": lambda c, p: {}, "fs.read": lambda c, p: {}}):
            names = [a["name"] for a in self.rt.actions_for(FILES)]
            self.assertIn("fs.read", names)
            self.assertNotIn("shell.run", names)
            rows = {a["name"]: a for a in self.rt.actions_for(CAUTIOUS)}
            self.assertTrue(rows["shell.run"]["approval"])
            self.assertFalse(rows["fs.read"]["approval"])
        row = self.rt.summary_row()
        self.assertEqual((row["mode"], row["online"], row["preset"], row["max_timeout"]), ("self", True, "standard",
                                                                                           3600))
        self.assertEqual(self.rt.managed_pids(), {})


class TestReadonlyPreset(Base):
    policy = {"preset": "readonly"}

    def test_readonly_node_denies_every_mutating_action(self):
        e = self.rt.effective(OPERATOR)
        for name, spec in catalog.SPECS.items():
            if spec.scope == "node" and spec.risk != "read":
                with self.assertRaises(HiveError):
                    e.check_action(spec)


class TestKillSwitchOffline(unittest.TestCase):
    def test_pause_resume_panic_with_daemon_down(self):
        d = Path(tempfile.mkdtemp(prefix="hivepanic-"))
        self.addCleanup(shutil.rmtree, d, True)
        pause(d)
        self.assertTrue((d / "PAUSED").exists())
        resume(d)
        self.assertFalse((d / "PAUSED").exists())
        cfg = mock.Mock(node_id="n_test0000")
        out = panic(cfg, d, force_after=0.1)
        self.assertEqual(set(out), {"jobs", "services", "containers", "errors"})
        self.assertTrue((d / "PAUSED").exists() and (d / "relay.disabled").exists())
        resume(d, relay=False)
        self.assertTrue((d / "relay.disabled").exists())
        resume(d)
        self.assertFalse((d / "relay.disabled").exists())


class TestHttpRoutes(unittest.TestCase):
    def test_routes(self):
        with FakeHome(), node_server(agent_entry={"require_approval": ["shell.run"]}) as (url, tok, rt):
            owner = rt.test_owner_token
            st, h, env = http(url + "/v1/call", "POST", {"action": "node.info", "node": "self"}, token=tok)
            self.assertEqual((st, env["ok"], env["meta"]["action"]), (200, True, "node.info"))
            self.assertEqual(env["result"]["modes"]["listen"], url.split("//")[1])
            st, _, env = http(url + "/v1/call", "POST", {"action": "node.info", "node": "other"}, token=tok)
            self.assertEqual((st, env["error"]["code"]), (404, "not_found"))
            st, _, env = http(url + "/v1/call", "POST", {"action": "fs.nothing"}, token=tok)
            self.assertEqual((st, env["ok"]), (404, False))
            st, _, env = http(url + "/v1/call", "POST", {"action": "node.info", "bound_files": {}}, token=tok)
            self.assertEqual((st, env["error"]["message"]), (400, "bound_files is only accepted from a hub"))
            st, _, env = http(url + "/v1/call", "POST", {"action": "node.info", "x": 1}, token=tok)
            self.assertEqual(st, 400)
            st, _, b = http(url + "/v1/whoami", token=tok)
            self.assertEqual((b["result"]["identity"]["name"], b["result"]["approvals"], b["result"]["server"]["role"]),
                             ("test-agent", True, "node"))
            b = http(url + "/v1/actions", token=tok)[2]["result"]
            # WP2 CONTRACT-REQUEST: other WPs' handlers are registered once merged; implemented = handlers present
            from hive.actions import HANDLERS
            self.assertEqual(b["node"], "test-node")
            self.assertIn("node.info", b["implemented"])
            self.assertTrue(all(n in HANDLERS for n in b["implemented"]))
            self.assertEqual(b["catalog_hash"], catalog.catalog_hash())
            self.assertEqual(http(url + "/v1/nodes", token=tok)[2]["result"]["nodes"][0]["name"], "test-node")
            self.assertEqual(http(url + "/v1/cancel", "POST", {"request_id": "r_none"}, token=tok)[2]["result"],
                             {"cancelled": False})
            self.assertEqual(http(url + "/v1/health")[2]["result"], {"role": "node", "api": 1})
            # approvals over HTTP: owner on loopback decides, an agent cannot
            with mock.patch.dict(HANDLERS, {"shell.run": lambda c, p: {"exit_code": 0}}):
                st, _, env = http(url + "/v1/call", "POST", {"action": "shell.run", "params": {"command": "ls"}},
                                  token=tok)
                self.assertEqual((st, env["error"]["code"]), (428, "approval_required"))
                aid = env["error"]["details"]["approval_id"]
                self.assertEqual(http(url + "/v1/approvals", token=tok)[0], 403)
                self.assertEqual(http(url + f"/v1/approvals/{aid}/approve", "POST", {}, token=tok)[0], 403)
                self.assertEqual(http(url + "/v1/approvals", token=owner)[2]["result"]["approvals"][0]["id"], aid)
                st, _, b = http(url + f"/v1/approvals/{aid}/approve", "POST", {"grant_minutes": 30}, token=owner)
                self.assertEqual((st, b["result"]["status"]), (200, "approved"))
                st, _, env = http(url + "/v1/call", "POST", {"action": "shell.run", "params": {"command": "ls"},
                                                             "approval_id": aid}, token=tok)
                self.assertTrue(env["ok"])
                grants = http(url + "/v1/approvals/grants", token=owner)[2]["result"]["grants"]
                self.assertEqual(len(grants), 1)
                self.assertEqual(http(url + "/v1/approvals/grants/revoke", "POST", {"all": True}, token=owner)[2]
                                 ["result"], {"revoked": 1})


class TestRunNode(unittest.TestCase):
    def _run(self, fh: FakeHome, cmd: str) -> subprocess.CompletedProcess:
        raw = helpers.make_node_config(data_dir=str(fh.data))
        util.write_json_atomic(fh.data / "config.json", raw, private=True)
        util.write_json_atomic(fh.data / "control.json", {"cmd": cmd, "at": util.iso_utc()}, private=True)
        code = ("import sys; sys.stderr = None; sys.stdout = None; from hive.node import run_node; "
                f"raise SystemExit(run_node({str(fh.data / 'config.json')!r}))")
        env = dict(os.environ)
        env.update(util.self_env())
        env["PYTHONPATH"] = str(PKG_ROOT)
        return subprocess.run([sys.executable, "-c", code], cwd=str(fh.base), env=env, timeout=60,
                              capture_output=True)

    def test_exit_codes_and_banner_in_log(self):
        with FakeHome() as fh:
            self.assertEqual(self._run(fh, "stop").returncode, 0)
            log = (fh.data / "logs" / "node.log").read_text()
            self.assertIn("Hive node 'test-node' ACTIVE", log)
            self.assertIn("Listen: http://127.0.0.1:", log)
            self.assertEqual(self._run(fh, "restart").returncode, 75)
            self.assertFalse((fh.data / "control.json").exists())
            self.assertIn('"event":"restart requested"', "".join(p.read_text() for p in (fh.data / "audit")
                                                                    .iterdir()))


class TestSupervise(unittest.TestCase):
    def test_loop(self):
        d = Path(tempfile.mkdtemp(prefix="hivesup-"))
        self.addCleanup(shutil.rmtree, d, True)
        script = d / "daemon.py"
        script.write_text(
            "import os, sys, pathlib\n"
            f"d = pathlib.Path({str(d)!r})\n"
            "c = d / 'count'\n"
            "n = int(c.read_text()) if c.exists() else 0\n"
            "c.write_text(str(n + 1))\n"
            "(d / f'launcher-seen-{n}').write_text(str((d / 'run' / 'launcher.json').exists()))\n"
            "(d / f'args-{n}').write_text(' '.join(sys.argv[1:]))\n"
            "sys.exit([75, 1, 0][n])\n")
        sleeps = []
        with mock.patch.object(node_mod, "_sleep", side_effect=lambda s: sleeps.append(s)), \
                mock.patch("hive.winapi.spawn_mode", return_value="free"):
            code = node_mod.supervise("node", [sys.executable, str(script)], d)
        self.assertEqual(code, 0)
        self.assertEqual((d / "count").read_text(), "3")
        self.assertEqual(sleeps, [5])                      # 75 -> at once; 1 -> backoff 5 s; 0 -> end
        self.assertEqual((d / "launcher-seen-0").read_text(), "True")
        self.assertEqual((d / "args-0").read_text(), "--relaunched")
        self.assertFalse((d / "run" / "launcher.json").exists())


class TestEnvOnlyRuntime(unittest.TestCase):
    def test_env_only_starts_without_listen(self):
        from hive import config, dist
        with FakeHome() as fh:
            code = dist.encode_join({"u": "http://100.101.1.5:7700", "t": "hv_relayrelayrelayrelay", "p": None,
                                     "n": "pod-a"})
            env = {"HIVE_HOME": str(fh.hive_home), "HIVE_JOIN": code}
            cfg = config.load_node_config(None, env=env)
            cfg.allow_root = True
            rt = node_mod.NodeRuntime(cfg, start_managers=False)
            rt.start()
            try:
                self.assertEqual(rt.name, "pod-a")
                self.assertTrue(rt.call(OPERATOR, "node.info")["ok"])
            finally:
                rt.stop()


if __name__ == "__main__":
    unittest.main()
