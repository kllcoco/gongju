"""WP5b hub + relay protocol tests (§7, §8, §9.10; §21.2 relay/hub row)."""
from __future__ import annotations

import json
import os
import time
import unittest
from pathlib import Path

from hive import catalog, util
from hive.client import connect
from hive.errors import HiveError
from hive.hub import Hub, RelayIds, summary_from_info
from hive.transport import HttpTransport
from tests import helpers
from tests.wp5util import FAST_RELAY, FakeRelay, call_async, hub_token_entry, ok_env


class _HubCase(unittest.TestCase):
    relay_cfg = FAST_RELAY

    def make_hub(self, nodes=(), **cfg):
        cfg.setdefault("relay", dict(self.relay_cfg))
        url, toks, hub = helpers.start_hub(nodes=nodes, **cfg)
        self.addCleanup(hub.test_stop)
        return url, toks, hub

    def relay_entry(self, name="pod-a"):
        tok = util.new_token()
        return {"name": name, "mode": "relay", "token_sha256": util.hash_token(tok)}, tok


class TestRelayProtocol(_HubCase):
    def setUp(self):
        e, self.rtok = self.relay_entry()
        self.url, self.toks, self.hub = self.make_hub([e])
        self.fake = FakeRelay(self.url, self.rtok, "pod-a")
        self.client = connect(self.url, token=self.toks["agent"])
        self.addCleanup(self.client.close)

    def agent_call(self, action, params, **kw):
        return call_async(self.client.call, action, params, node="pod-a", **kw)

    def test_happy_path_register_poll_result(self):
        r = self.fake.register()
        self.assertEqual((r["node"], r["poll_wait"]), ("pod-a", 1.0))
        self.assertTrue(r["session_id"].startswith("s_"))
        box = self.agent_call("fs.stat", {"path": "/tmp"})
        tasks = self.fake.poll_until(1)
        self.assertEqual(len(tasks), 1)
        t = tasks[0]
        self.assertEqual((t["action"], t["params"], t["agent"], t["exec_gated"], t["bound_files"]),
                         ("fs.stat", {"path": "/tmp"}, "test-agent", False, None))
        self.assertGreater(t["timeout_s"], 0)
        self.assertEqual(self.fake.result(t["task_id"], ok_env({"path": "/tmp", "exists": True})),
                         {"accepted": True})
        box["thread"].join(10)
        self.assertEqual(box["result"]["exists"], True)
        # a second result for the same task is not accepted
        self.assertEqual(self.fake.result(t["task_id"], ok_env({})), {"accepted": False})

    def test_offline_immediate(self):
        t0 = time.monotonic()
        with self.assertRaises(HiveError) as cm:
            self.client.call("fs.stat", {"path": "/"}, node="pod-a")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual((cm.exception.code, cm.exception.details["reason"], cm.exception.details["maybe_executed"]),
                         ("unavailable", "offline", False))

    def test_pickup_timeout_never_executes_later(self):
        self.fake.register()
        box = self.agent_call("fs.write", {"path": "/tmp/x", "content": "y"})
        # the node polls but has no free slot: the task is not delivered
        for _ in range(4):
            self.assertEqual(self.fake.poll(free_slots=0)["tasks"], [])
            if "thread" in box and not box["thread"].is_alive():
                break
        box["thread"].join(10)
        err = box["error"]
        self.assertEqual((err.code, err.details["reason"], err.details["maybe_executed"]),
                         ("unavailable", "busy", False))
        # never delivered later
        self.assertEqual(self.fake.poll(free_slots=8)["tasks"], [])

    def test_not_polling_reason(self):
        self.fake.register()
        box = self.agent_call("fs.stat", {"path": "/"})
        box["thread"].join(10)
        self.assertEqual(box["error"].details["reason"], "not_polling")

    def test_lost_mutating_and_read(self):
        self.fake.register()
        for action, params, maybe in (("fs.write", {"path": "/tmp/x", "content": "1"}, True),
                                      ("fs.stat", {"path": "/"}, False)):
            box = self.agent_call(action, params)
            tasks = self.fake.poll_until(1)
            self.assertEqual(len(tasks), 1)
            # next poll does not list the task as running -> lost after lost_grace
            self.fake.poll(running=[])
            box["thread"].join(10)
            err = box["error"]
            self.assertEqual((err.code, err.details["reason"], err.details["maybe_executed"]),
                             ("unavailable", "lost", maybe))

    def test_reregister_fails_delivered_keeps_queued(self):
        self.fake.register()
        box = self.agent_call("fs.write", {"path": "/tmp/x", "content": "1"})
        tasks = self.fake.poll_until(1)
        self.assertTrue(tasks)
        self.fake.register()
        box["thread"].join(10)
        err = box["error"]
        self.assertEqual((err.code, err.details["reason"], err.details["maybe_executed"]),
                         ("unavailable", "session_replaced", True))
        # old session polls -> 409
        with self.assertRaises(HiveError) as cm:
            self.fake.poll(session_id="s_old")
        self.assertEqual(cm.exception.code, "conflict")

    def test_relay_timeout_s_without_slack(self):
        self.fake.register()
        self.agent_call("shell.run", {"command": "true", "timeout": 40})
        t = self.fake.poll_until(1)[0]
        # deadline = default_timeout + timeout (no call_timeout_slack)
        spec = catalog.SPECS["shell.run"]
        want = catalog.deadline(spec, {"command": "true", "timeout": 40})
        self.assertAlmostEqual(t["timeout_s"], want, delta=2)
        self.fake.result(t["task_id"], ok_env({}))

    def test_cancel_forwarded_and_timeout(self):
        self.fake.register()
        box = self.agent_call("fs.stat", {"path": "/"}, timeout=1)
        t = self.fake.poll_until(1)[0]
        cancels = []
        for _ in range(5):
            cancels += self.fake.poll(running=[t["task_id"]]).get("cancel") or []
            if cancels:
                break
        box["thread"].join(10)
        self.assertIn(t["task_id"], cancels)
        self.assertEqual((box["error"].code, box["error"].details["maybe_executed"]), ("timeout", False))

    def test_max_poll_bytes_splits(self):
        self.hub.links["pod-a"].cfg.max_poll_bytes = 70000
        self.fake.register()
        boxes = [self.agent_call("fs.write", {"path": f"/tmp/f{i}", "content": "x" * 40000}) for i in range(3)]
        time.sleep(0.3)
        first = self.fake.poll(free_slots=8)["tasks"]
        self.assertEqual(len(first), 1)
        second = self.fake.poll(free_slots=8)["tasks"]
        self.assertGreaterEqual(len(second), 1)
        for t in first + second:
            self.fake.result(t["task_id"], ok_env({}))
        for t in self.fake.poll_until(1):
            self.fake.result(t["task_id"], ok_env({}))
        for b in boxes:
            b["thread"].join(10)

    def test_forward_slot_reserve(self):
        self.fake.register()
        boxes = [self.agent_call("model.forward", {"service": "qwen", "path": "models"}) for _ in range(7)]
        time.sleep(0.3)
        st = self.agent_call("job.status", {"job_id": "j_abcdefgh"})
        time.sleep(0.3)
        tasks = self.fake.poll(free_slots=16)["tasks"]
        fwd = [t for t in tasks if t["action"] == "model.forward"]
        self.assertEqual(len(fwd), 6)                      # max(1, 8 - 2)
        self.assertIn("job.status", [t["action"] for t in tasks])
        for t in tasks:
            self.fake.result(t["task_id"], ok_env({"status": 200, "content_type": "application/json", "body": "{}",
                                                   "truncated": False, "elapsed_s": 0.1}))
        for t in self.fake.poll_until(1):
            self.fake.result(t["task_id"], ok_env({"status": 200, "content_type": "application/json",
                                                   "body": "{}", "truncated": False, "elapsed_s": 0.1}))
        for b in boxes + [st]:
            b["thread"].join(10)

    def test_approval_required_from_node_becomes_internal(self):
        self.fake.register()
        box = self.agent_call("fs.stat", {"path": "/"})
        t = self.fake.poll_until(1)[0]
        self.fake.result(t["task_id"], {"ok": False, "error": {"code": "approval_required", "message": "x",
                                                               "details": {"approval_id": "ap_evil123"}},
                                        "meta": {}})
        box["thread"].join(10)
        self.assertEqual(box["error"].code, "internal")
        self.assertIn("may not issue", box["error"].message)
        self.assertNotIn("approval_id", box["error"].details)

    def test_info_sanitized_tags_ignored(self):
        self.fake.info = dict(self.fake.info, hostname="evil‮host\x07" + "x" * 300, description="lies")
        self.fake.register()
        row = [n for n in self.client.nodes() if n["name"] == "pod-a"][0]
        self.assertEqual((row["tags"], row["description"], row["mode"], row["online"]), ([], "", "relay", True))
        info = self.hub.links["pod-a"].info
        self.assertEqual(info["hostname"][:8], "evilhost")
        self.assertLessEqual(len(info["hostname"]), 200)
        self.assertEqual(row["max_timeout"], 300)
        self.assertEqual(row["preset"], "standard")

    def test_token_and_route_kinds(self):
        e2, tok2 = self.relay_entry("pod-b")
        # node token A cannot register as B
        with self.assertRaises(HiveError) as cm:
            FakeRelay(self.url, self.rtok, "pod-b").register()
        self.assertEqual(cm.exception.code, "forbidden")
        # relay token cannot /v1/call (it is not an agent token on the hub)
        with self.assertRaises(HiveError) as cm:
            HttpTransport(self.url, self.rtok).request("POST", "/v1/call", {"action": "hub.nodes"})
        self.assertIn(cm.exception.code, ("unauthorized", "forbidden"))
        # agent token cannot poll
        with self.assertRaises(HiveError) as cm:
            HttpTransport(self.url, self.toks["agent"]).request("POST", "/v1/relay/poll", {"session_id": "s"})
        self.assertIn(cm.exception.code, ("unauthorized", "forbidden"))

    def test_owner_routes(self):
        self.fake.register()
        local = HttpTransport(self.url, self.toks["owner"])
        # poll in background so the node counts as online
        self.assertEqual(local.request("POST", "/v1/hub/nodes/pod-a/restart", {}), {"queued": True})
        r = self.fake.poll()
        self.assertEqual(r["refresh"], "restart")
        with self.assertRaises(HiveError) as cm:
            HttpTransport(self.url, self.toks["agent"]).request("POST", "/v1/hub/nodes/pod-a/restart", {})
        self.assertEqual(cm.exception.code, "forbidden")
        self.assertEqual(local.request("POST", "/v1/hub/nodes/pod-a/ack", {}),
                         {"bound": False, "pending": None, "refused": None})


class TestIdentityBinding(_HubCase):
    relay_cfg = dict(FAST_RELAY, offline_after=5)

    def setUp(self):
        e, self.rtok = self.relay_entry()
        self.entry = e
        self.url, self.toks, self.hub = self.make_hub([e])
        self.owner = HttpTransport(self.url, self.toks["owner"])

    def go_offline(self):
        link = self.hub.links["pod-a"]
        with link.lock:
            link.last_poll_end = time.monotonic() - 100

    def test_binding_lifecycle(self):
        a = FakeRelay(self.url, self.rtok, "pod-a")
        a.register()
        ids = json.loads((self.hub.data_dir / "state" / "relay_ids.json").read_text())
        rec = ids["nodes"]["pod-a"]
        self.assertEqual(ids["v"], 1)
        self.assertEqual(rec["key_sha256"], "sha256:" + __import__("hashlib").sha256(a.key.encode()).hexdigest())
        # another key while online -> 409
        b = FakeRelay(self.url, self.rtok, "pod-a")
        with self.assertRaises(HiveError) as cm:
            b.register()
        self.assertEqual(cm.exception.code, "conflict")
        # while offline: accepted but identity_changed, calls refused
        self.go_offline()
        b.register()
        c = connect(self.url, token=self.toks["agent"])
        self.addCleanup(c.close)
        row = [n for n in c.nodes() if n["name"] == "pod-a"][0]
        self.assertTrue(row["identity_changed"])
        with self.assertRaises(HiveError) as cm:
            c.call("fs.write", {"path": "/tmp/x", "content": "1"}, node="pod-a")
        self.assertEqual((cm.exception.details["reason"], cm.exception.details["maybe_executed"]),
                         ("identity_changed", False))
        self.assertIn("rotate-token", cm.exception.hint)
        self.assertEqual(b.poll()["tasks"], [])
        # the key never appears in hub.nodes, audit or relay_ids.json plaintext
        audit = "".join(p.read_text() for p in (self.hub.data_dir / "audit").glob("*.jsonl"))
        for k in (a.key, b.key):
            self.assertNotIn(k, json.dumps(c.nodes()))
            self.assertNotIn(k, audit)
            self.assertNotIn(k, (self.hub.data_dir / "state" / "relay_ids.json").read_text())
        # preview
        prev = self.owner.request("POST", "/v1/hub/nodes/pod-a/ack", {})
        self.assertFalse(prev["bound"])
        self.assertIsNotNone(prev["pending"]["sha256"])
        self.assertIn("ip", prev["pending"])
        # stale hash binds nothing
        r = self.owner.request("POST", "/v1/hub/nodes/pod-a/ack", {"pending_sha256": "sha256:" + "0" * 64})
        self.assertFalse(r["bound"])
        # pending survives a hub restart
        ids2 = RelayIds(self.hub.data_dir / "state" / "relay_ids.json")
        ids2.load([self.hub.cfg.nodes[0]])
        self.assertEqual(ids2.get("pod-a")["pending_sha256"], prev["pending"]["sha256"])
        # ack binds
        r = self.owner.request("POST", "/v1/hub/nodes/pod-a/ack", {"pending_sha256": prev["pending"]["sha256"]})
        self.assertTrue(r["bound"])
        self.assertFalse([n for n in c.nodes() if n["name"] == "pod-a"][0]["identity_changed"])

    def test_recovery_by_bound_key(self):
        a = FakeRelay(self.url, self.rtok, "pod-a")
        a.register()
        self.go_offline()
        b = FakeRelay(self.url, self.rtok, "pod-a")
        b.register()
        self.assertTrue(self.hub.links["pod-a"].identity_changed)
        self.go_offline()
        a.register()                           # the real pod comes back first
        self.assertFalse(self.hub.links["pod-a"].identity_changed)
        rec = self.hub.relay_ids.get("pod-a")
        self.assertIsNone(rec["pending_sha256"])
        audit = "".join(p.read_text() for p in (self.hub.data_dir / "audit").glob("*.jsonl"))
        self.assertIn("identity recovered", audit)

    def test_ack_refused_when_bound_seen_later(self):
        a = FakeRelay(self.url, self.rtok, "pod-a")
        a.register()
        self.go_offline()
        FakeRelay(self.url, self.rtok, "pod-a").register()
        prev = self.owner.request("POST", "/v1/hub/nodes/pod-a/ack", {})
        rec = self.hub.relay_ids.get("pod-a")
        rec["bound_seen_at"] = util.iso_utc(time.time() + 5)
        r = self.owner.request("POST", "/v1/hub/nodes/pod-a/ack", {"pending_sha256": prev["pending"]["sha256"]})
        self.assertFalse(r["bound"])
        self.assertIn("rotate the token", r["refused"])

    def test_rotate_token_resets_binding(self):
        a = FakeRelay(self.url, self.rtok, "pod-a")
        a.register()
        path = self.hub.data_dir / "state" / "relay_ids.json"
        newtok = util.new_token()
        entry = type(self.hub.cfg.nodes[0])(**dict(self.hub.cfg.nodes[0].__dict__,
                                                   token_sha256=util.hash_token(newtok)))
        ids = RelayIds(path)
        ids.load([entry])
        self.assertIsNone(ids.get("pod-a")["key_sha256"])


class TestHubChecks(_HubCase):
    def setUp(self):
        e, self.rtok = self.relay_entry()
        self.url, self.toks, self.hub = self.make_hub([e])
        self.fake = FakeRelay(self.url, self.rtok, "pod-a", actions=["fs.stat", "fs.read", "shell.run"])
        self.fake.register()
        self.client = connect(self.url, token=self.toks["agent"])
        self.addCleanup(self.client.close)

    def test_unknown_action_and_not_implemented(self):
        with self.assertRaises(HiveError) as cm:
            self.client.call("fs.teleport", {}, node="pod-a")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("upgrade the hub", cm.exception.hint)
        with self.assertRaises(HiveError) as cm:
            self.client.call("fs.write", {"path": "/x", "content": ""}, node="pod-a")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("lacks 'fs.write'", cm.exception.hint)
        self.assertEqual(self.fake.poll()["tasks"], [])

    def test_params_validated_at_hub(self):
        with self.assertRaises(HiveError) as cm:
            self.client.call("fs.stat", {"path": "/", "bogus": 1}, node="pod-a")
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertEqual(self.fake.poll()["tasks"], [])

    def test_params_forwarded_without_defaults(self):
        box = call_async(self.client.call, "fs.read", {"path": "/x", "max_lines": "5"}, node="pod-a")
        t = self.fake.poll_until(1)[0]
        self.assertEqual(t["params"], {"path": "/x", "max_lines": 5})
        self.fake.result(t["task_id"], ok_env({}))
        box["thread"].join(10)

    def test_bound_files_from_agent_refused(self):
        r = HttpTransport(self.url, self.toks["agent"]).request(
            "POST", "/v1/call", {"action": "fs.stat", "node": "pod-a", "params": {"path": "/"},
                                 "bound_files": {"/x": None}}, raw=True)
        self.assertEqual(r[0], 400)

    def test_cautious_preset_gates_shell_not_read(self):
        self.fake.info["policy"]["preset"] = "cautious"
        self.fake.register()
        box = call_async(self.client.call, "shell.run", {"command": "echo hi"}, node="pod-a")
        stats = []
        for _ in range(10):        # the hub's file binding stats each word over the link (not path-like: unbound)
            for t in self.fake.poll().get("tasks") or []:
                stats.append((t["action"], t["params"], t["request_id"]))
                self.fake.result(t["task_id"], ok_env({"path": "/w/" + t["params"]["path"], "exists": False}))
            if not box["thread"].is_alive():
                break
        box["thread"].join(10)
        self.assertEqual([s[0] for s in stats], ["fs.stat", "fs.stat"])
        self.assertTrue(all(".b" in s[2] for s in stats))
        cm = type("cm", (), {"exception": box["error"]})
        self.assertEqual(cm.exception.code, "approval_required")
        self.assertEqual(cm.exception.details["where"], "hub:test-hub")
        box = call_async(self.client.call, "fs.stat", {"path": "/"}, node="pod-a")
        t = self.fake.poll_until(1)[0]
        self.fake.result(t["task_id"], ok_env({"exists": True}))
        box["thread"].join(10)
        self.assertTrue(box["result"]["exists"])

    def test_hub_nodes_action_and_scope(self):
        r = self.client.call("hub.nodes", {})
        self.assertEqual([n["name"] for n in r["nodes"]], ["pod-a"])
        acts = self.client.actions()
        hubrow = [a for a in acts if a["name"] == "hub.nodes"][0]
        self.assertEqual(hubrow["nodes"], [])
        self.assertEqual([a["nodes"] for a in acts if a["name"] == "fs.stat"], [["pod-a"]])
        audit = "".join(p.read_text() for p in (self.hub.data_dir / "audit").glob("*.jsonl"))
        self.assertIn('"action":"hub.nodes"', audit)

    def test_node_resolution(self):
        with self.assertRaises(HiveError) as cm:
            self.client.call("fs.stat", {"path": "/"}, node="pod-z")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("pod-a", cm.exception.hint)

    def test_readonly_identity_forbidden(self):
        url, toks, hub = self.make_hub([], agent_entry={"role": "readonly"})
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        with self.assertRaises(HiveError) as cm:
            c.call("fs.teleport", {})
        self.assertIn(cm.exception.code, ("not_found", "bad_request"))

    def test_dist_pyz_no_token_rate_limited(self):
        t = HttpTransport(self.url)
        st, hdrs, body = t.request("GET", "/v1/dist/hive.pyz", raw=True)
        self.assertEqual(st, 200)
        import hashlib
        self.assertEqual(hdrs.get("x-hive-sha256"), hashlib.sha256(body).hexdigest())
        codes = [t.request("GET", "/v1/dist/hive.pyz", raw=True)[0] for _ in range(6)]
        self.assertIn(429, codes)


class TestRealNodes(_HubCase):
    """A hub with a real direct node and a real relay node (NodeRuntime + RelayAgent)."""

    def setUp(self):
        self.hub_tok = util.new_token()
        self.nurl, _a, self.nrt = helpers.start_node_server(start_managers=True,
                                                            auth={"tokens": [hub_token_entry(self.hub_tok)]})
        self.addCleanup(self.nrt.test_stop)
        e, self.rtok = self.relay_entry()
        self.url, self.toks, self.hub = self.make_hub(
            [{"name": "desk", "mode": "direct", "url": self.nurl, "token": self.hub_tok, "aliases": ["desktop"]}, e])
        from tests.wp5util import start_relay_node
        self.prt = start_relay_node(self.url, self.rtok)
        self.addCleanup(self.prt.test_stop)
        helpers.wait_until(lambda: self.hub.links["pod-a"].online(), 15)
        self.client = connect(self.url, token=self.toks["agent"])
        self.addCleanup(self.client.close)

    def test_calls_both_links(self):
        rows = {n["name"]: n for n in self.client.nodes()}
        self.assertEqual((rows["desk"]["mode"], rows["pod-a"]["mode"]), ("direct", "relay"))
        self.assertTrue(rows["desk"]["online"] and rows["pod-a"]["online"])
        self.assertEqual(rows["desk"]["name"], "desk")
        self.assertIn("max_timeout", rows["desk"])
        out = self.client.call("shell.run", {"command": "echo direct"}, node="desktop")
        self.assertIn("direct", out["output"])
        out = self.client.call("shell.run", {"command": "echo relayed"}, node="pod-a")
        self.assertIn("relayed", out["output"])
        # node audit shows the agent name, via hub / relay
        audit = "".join(p.read_text() for p in (Path(self.nrt.data_dir) / "audit").glob("*.jsonl"))
        self.assertIn('"agent":"test-agent"', audit)
        self.assertIn('"via":"hub"', audit)
        raudit = "".join(p.read_text() for p in (Path(self.prt.data_dir) / "audit").glob("*.jsonl"))
        self.assertIn('"via":"relay"', raudit)
        self.assertNotIn(self.prt.relay.instance_key, raudit)
        st = os.stat(Path(self.prt.data_dir) / "instance_key")
        if os.name == "posix":
            self.assertEqual(st.st_mode & 0o777, 0o600)

    def test_refresh_restart_exit_75(self):
        owner = HttpTransport(self.url, self.toks["owner"])
        self.assertEqual(owner.request("POST", "/v1/hub/nodes/pod-a/restart", {}), {"queued": True})
        helpers.wait_until(lambda: self.prt.shutdown_event.is_set(), 10)
        self.assertEqual(self.prt.exit_code, 75)
        with self.assertRaises(HiveError) as cm:
            owner.request("POST", "/v1/hub/nodes/desk/restart", {})
        self.assertEqual(cm.exception.code, "bad_request")

    def test_hub_token_role_files_caps(self):
        tok = util.new_token()
        nurl, _a, nrt = helpers.start_node_server(start_managers=True,
                                                  auth={"tokens": [hub_token_entry(tok, role="files")]})
        self.addCleanup(nrt.test_stop)
        url, toks, hub = self.make_hub([{"name": "box", "mode": "direct", "url": nurl, "token": tok}])
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        with self.assertRaises(HiveError) as cm:
            c.call("shell.run", {"command": "echo x"}, node="box")
        self.assertEqual(cm.exception.code, "forbidden")

    def test_files_hub_agent_exec_gated_on_both_links(self):
        url, toks, hub = self.make_hub([{"name": "desk", "mode": "direct", "url": self.nurl, "token": self.hub_tok}],
                                       agent_entry={"role": "files"})
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        bashrc = os.path.join(os.path.expanduser("~"), ".bashrc")
        with self.assertRaises(HiveError) as cm:
            c.call("fs.write", {"path": bashrc, "content": "evil", "mode": "append"}, node="desk")
        self.assertEqual(cm.exception.code, "policy_denied")

    def test_cautious_direct_node_fail_closed_unreachable(self):
        tok = util.new_token()
        port = helpers.free_port()
        url, toks, hub = self.make_hub([{"name": "gone", "mode": "direct", "url": f"http://127.0.0.1:{port}",
                                         "token": tok}])
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        with self.assertRaises(HiveError) as cm:
            c.call("shell.run", {"command": "echo x"}, node="gone")
        self.assertEqual((cm.exception.code, cm.exception.details["reason"], cm.exception.details["maybe_executed"]),
                         ("unavailable", "connect", False))

    def test_hub_approval_binds_file_over_link(self):
        url, toks, hub = self.make_hub([{"name": "desk", "mode": "direct", "url": self.nurl, "token": self.hub_tok}],
                                       agent_entry={"require_approval": ["shell.run"]})
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        wd = Path(self.nrt.workdir)
        script = wd / f"run-{os.getpid()}.sh"
        script.write_text("echo approved-run\n")
        self.addCleanup(lambda: script.exists() and script.unlink())
        params = {"command": f"bash {script}"}
        with self.assertRaises(HiveError) as cm:
            c.call("shell.run", params, node="desk")
        aid = cm.exception.details["approval_id"]
        rec = hub.approvals.get(aid)
        self.assertTrue(any(str(script) in line for line in rec["review"]["lines"]))
        self.assertIn("echo approved-run", rec["review"]["lines"])
        self.assertEqual(list(rec["files"]), [str(script)])
        owner = HttpTransport(url, toks["owner"])
        owner.request("POST", f"/v1/approvals/{aid}/approve", {})
        script.write_text("echo tampered\n")
        with self.assertRaises(HiveError) as cm:
            c.call("shell.run", params, node="desk", approval_id=aid)
        self.assertEqual(cm.exception.code, "conflict")


class TestSummary(unittest.TestCase):
    def test_summary_from_info(self):
        class E:
            tags = ["cfg"]
            description = "configured"
        info = {"os": "Windows", "os_release": "11", "version": "0.9", "gpus_summary": "evil\nline",
                "wsl": {"keepalive": [], "distros": ["Ubuntu"]}, "policy": {"preset": "cautious"},
                "secrets": [{"name": "HF_TOKEN"}]}
        r = summary_from_info("w", "relay", info, {}, entry=E())
        self.assertEqual((r["os_label"], r["gpus_summary"], r["outdated"], r["preset"], r["tags"], r["secrets"]),
                         ("Windows 11", None, True, "cautious", ["cfg"], ["HF_TOKEN"]))
        self.assertFalse(r["wsl"])
        r = summary_from_info("w", "relay", {"os": "Linux", "wsl": {"distro": "Ubuntu"}}, {}, entry=E())
        self.assertEqual(r["os_label"], "Linux (WSL2)")


if __name__ == "__main__":
    unittest.main()
