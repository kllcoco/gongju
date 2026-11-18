"""WP6b review fixes (regression tests): /mcp session eviction skips busy sessions, duplicate in-flight JSON-RPC ids,
the /mcp JSON depth guard, closed sessions stop their transfers; mcp-config fails closed (owner tokens with the server
down, --remote only for a hub serving /mcp, no plaintext public URL), private .bak; gateway url escapes the model id
and sends the token; gateway token add prints the settings block; the T5 warning with a narrow --approval;
add-node --direct refuses plaintext public URLs; restart-node --outdated names every outdated node; rotate-token,
remove-node/remove-agent, ack-node with the hub down, approvals list with every place down."""
from __future__ import annotations

import json
import os
import stat
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hive import cli, config, mcp, tools, transfer, util
from hive.client import Client
from hive.errors import HiveError

from tests import helpers
from tests.test_cli import CliCase, run
from tests.test_cli_wp6b import _HubCli, _free_url, _write_conn
from tests.test_mcp import Sink, core
from tests.test_mcp_http import init_msg, post


# ---------------------------------------------------------------------------------------------------------------
# mcp.py

class _Core:
    def __init__(self):
        self.closed = 0
        self.toolkit = SimpleNamespace(close=self._tk_close)
        self.tk_closed = 0

    def _tk_close(self):
        self.tk_closed += 1
        return 0

    def close(self, session=None, wait=0):
        self.closed += 1
        return True


class TestSessionEviction(unittest.TestCase):
    def test_busy_session_not_evicted_per_token(self):
        now = [100.0]
        t = mcp.HttpSessions(per_token=2, total=100, clock=lambda: now[0])
        c1, c2, c3 = _Core(), _Core(), _Core()
        s1 = t.create("tok", c1, mcp.McpSession())
        now[0] += 1
        r1 = t.acquire(s1.id, "tok")                  # s1 is waiting on a long tools/call
        now[0] += 1
        s2 = t.create("tok", c2, mcp.McpSession())
        now[0] += 1
        r2 = t.acquire(s2.id, "tok")
        t.release(r2)                                 # s2 idle, but used more recently than s1
        now[0] += 1
        t.create("tok", c3, mcp.McpSession())
        self.assertEqual(c1.closed, 0)                # the active one survives
        self.assertEqual(c2.closed, 1)                # the oldest idle one goes
        self.assertEqual(c2.tk_closed, 1)             # and its transfers are stopped
        self.assertIsNone(t.acquire(s2.id, "tok"))
        t.release(r1)

    def test_all_busy_refused_without_cancelling(self):
        now = [100.0]
        t = mcp.HttpSessions(per_token=2, total=3, clock=lambda: now[0])
        cores = [_Core(), _Core()]
        recs = [t.create("tok", c, mcp.McpSession()) for c in cores]
        held = [t.acquire(r.id, "tok") for r in recs]
        with self.assertRaises(HiveError) as cm:
            t.create("tok", _Core(), mcp.McpSession())
        self.assertEqual(cm.exception.code, "unavailable")
        self.assertEqual([c.closed for c in cores], [0, 0])
        self.assertEqual(len(t), 2)
        # total cap: another token, the only idle session is the victim; then all busy -> refused
        other = _Core()
        o = t.create("tok2", other, mcp.McpSession())
        self.assertEqual(len(t), 3)
        held.append(t.acquire(o.id, "tok2"))
        with self.assertRaises(HiveError):
            t.create("tok3", _Core(), mcp.McpSession())
        self.assertEqual(other.closed, 0)
        t.release(held[2])
        now[0] += 1
        t.create("tok3", _Core(), mcp.McpSession())
        self.assertEqual(other.closed, 1)
        for h in held[:2]:
            t.release(h)

    def test_handler_answers_503_when_all_busy(self):
        table = mcp.HttpSessions(per_token=1)
        made = []

        def factory(ident, req, *, profile, wait_budget):
            c, _t = core()
            made.append(c)
            return c
        h = mcp.http_handler(factory, sessions=table)

        def req(body):
            return SimpleNamespace(identity=SimpleNamespace(name="a"), headers={}, method="POST",
                                   body=json.dumps(body).encode(), query={})
        st, hdrs, _b = h(req(init_msg()))
        self.assertEqual(st, 200)
        rec = table.acquire(hdrs["Mcp-Session-Id"], "a")
        st, _h, b = h(req(init_msg()))
        self.assertEqual(st, 503)
        self.assertEqual(json.loads(b)["error"]["code"], mcp.INTERNAL_ERROR)
        self.assertIsNotNone(table.acquire(rec.id, "a"))     # still alive
        table.release(rec)
        table.release(rec)
        table.close_all()


class TestDuplicateIds(unittest.TestCase):
    def test_duplicate_inflight_id_rejected(self):
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
        msg = {"jsonrpc": "2.0", "id": 7, "method": "tools/call",
               "params": {"name": "job_status", "arguments": {"job_id": "j_abcdefgh", "wait": 30}}}
        self.assertIsNone(c.handle(msg, s, sink))
        self.assertTrue(started.wait(5))
        first = s.inflight[7]
        r = c.handle(msg, s, sink)
        self.assertEqual(r["error"]["code"], mcp.INVALID_REQUEST)
        self.assertIn("duplicate", r["error"]["message"])
        self.assertIs(s.inflight[7], first)            # the first call's handle is kept
        c.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 7}}, s, sink)
        self.assertEqual(t.cancelled, [first.request_id])
        release.set()
        helpers.wait_until(lambda: 7 not in s.inflight, timeout=5)
        c.close(s, wait=1)


class TestDepthGuard(unittest.TestCase):
    def test_deep_body_is_400(self):
        h = mcp.http_handler(lambda *a, **k: None)
        for n in (100000, 200):
            body = b"[" * n + b"]" * n
            req = SimpleNamespace(identity=SimpleNamespace(name="a"), headers={}, method="POST", body=body,
                                  query={})
            st, _h, b = h(req)
            self.assertEqual(st, 400)
            self.assertEqual(json.loads(b)["error"]["code"], mcp.INVALID_REQUEST)
        # a parser RecursionError below the pre-scan limit is a 400 too
        with mock.patch("hive.httpserver.json_depth_ok", return_value=True), \
                mock.patch("hive.mcp.json.loads", side_effect=RecursionError):
            st, _h, b = h(SimpleNamespace(identity=SimpleNamespace(name="a"), headers={}, method="POST",
                                          body=b"{}", query={}))
        self.assertEqual(st, 400)


class TestTransfersStopped(unittest.TestCase):
    def test_cancel_all_and_toolkit_close(self):
        tm = transfer.TransferManager(None)
        recs = [transfer._Rec(f"tx_{i}", "a:/x", "b:/y") for i in range(3)]
        recs[2].state = "done"
        for r in recs:
            tm._recs[r.id] = r
        self.assertEqual(tm.cancel_all(), 2)
        self.assertTrue(recs[0].cancel.is_set() and recs[1].cancel.is_set())
        self.assertFalse(recs[2].cancel.is_set())
        c, _t = core()
        self.assertEqual(c.toolkit.close(), 0)          # no transfer manager yet
        c.toolkit._tm = tm
        recs[0].cancel.clear()
        self.assertEqual(c.toolkit.close(), 1)
        # HttpSessions closes a dropped session's toolkit
        table = mcp.HttpSessions()
        fake = _Core()
        rec = table.create("a", fake, mcp.McpSession())
        self.assertTrue(table.drop(rec.id, "a"))
        self.assertEqual((fake.closed, fake.tk_closed), (1, 1))


# ---------------------------------------------------------------------------------------------------------------
# mcp-config

class TestMcpConfigFailClosed(CliCase):
    def test_owner_token_with_server_down(self):
        self.assertEqual(run(["init", "hub", "--yes", "--name", "home", "--listen", "127.0.0.1:17998"])[0], 0)
        owner = (config.default_paths()["tokens"] / "hub-owner.token").read_text().strip()
        _write_conn("home", _free_url(), owner)
        code, out, err = run(["mcp-config", "--connection", "home"])
        self.assertEqual(code, 3, out + err)
        self.assertIn("owner token", err)
        code, out, err = run(["mcp-config", "--connection", "home", "--remote", "--client", "claude-code"])
        self.assertEqual(code, 3, out + err)
        self.assertNotIn(owner, out + err)
        # a token whose kind nothing on this machine knows: refused while the server is down
        other = util.new_token()
        _write_conn("elsewhere", _free_url(), other)
        code, out, err = run(["mcp-config", "--connection", "elsewhere"])
        self.assertEqual(code, 4, out + err)
        self.assertIn("--allow-owner", err)
        code, out, err = run(["mcp-config", "--connection", "elsewhere", "--allow-owner"])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn(other, out)
        # --remote cannot be checked while the server is down: refused, no token printed
        code, out, err = run(["mcp-config", "--connection", "elsewhere", "--allow-owner", "--remote"])
        self.assertEqual(code, 4, out + err)
        self.assertNotIn(other, out + err)

    def test_local_agent_token_checked_offline(self):
        self.assertEqual(run(["init", "node", "--yes", "--listen", f"127.0.0.1:{helpers.free_port()}"])[0], 0)
        code, out, err = run(["mcp-config", "--local"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("checked locally", out)
        code, out, err = run(["mcp-config", "--connection", "this-pc-owner"])
        self.assertEqual(code, 3, out + err)

    def test_remote_refused_for_node(self):
        url, tok, rt = helpers.start_node_server()
        self.addCleanup(rt.test_stop)
        _write_conn("this-pc", url, tok, default=True)
        code, out, err = run(["mcp-config", "--local", "--remote", "--client", "claude-code"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("served by a hub only", err)
        self.assertNotIn(tok, out + err)

    def test_remote_hub(self):
        url, toks, hub = helpers.start_hub()
        self.addCleanup(hub.test_stop)
        _write_conn("home", url, toks["agent"])
        code, out, err = run(["mcp-config", "--connection", "home", "--remote", "--client", "claude-code"])
        self.assertEqual(code, 0, out + err)
        self.assertIn(f'"{url}/mcp?wait_budget=110"', out)
        self.assertIn(f"Bearer {toks['agent']}", out)

    def test_remote_hub_without_mcp_http(self):
        url, toks, hub = helpers.start_hub(listen={"mcp_http": False})
        self.addCleanup(hub.test_stop)
        _write_conn("home", url, toks["agent"])
        code, out, err = run(["mcp-config", "--connection", "home", "--remote"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("mcp_http off", err)
        self.assertNotIn(toks["agent"], out + err)

    def test_remote_plain_http_public_refused(self):
        tok = util.new_token()
        _write_conn("pub", "http://203.0.113.5:7700", tok)

        class FakeTr:
            def request(self, *a, **k):
                return 400, {}, b"{}"

        class FakeClient:
            target = SimpleNamespace(transport=FakeTr())

            def whoami(self):
                return {"identity": {"kind": "agent"}, "server": {"role": "hub"}}

            def close(self):
                pass
        with mock.patch("hive.client.connect", return_value=FakeClient()):
            code, out, err = run(["mcp-config", "--connection", "pub", "--remote", "--client", "claude-code"])
            self.assertEqual(code, 2, out + err)
            self.assertIn("plain http", err)
            self.assertNotIn(tok, out + err)
            _write_conn("tail", "http://100.101.2.3:7700", tok)
            code, out, err = run(["mcp-config", "--connection", "tail", "--remote", "--client", "claude-code"])
            self.assertEqual(code, 0, out + err)
            self.assertIn(f"Bearer {tok}", out)

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_write_backup_is_private(self):
        url, tok, rt = helpers.start_node_server()
        self.addCleanup(rt.test_stop)
        _write_conn("this-pc", url, tok, default=True)
        cur = self.home / ".cursor" / "mcp.json"
        cur.parent.mkdir(parents=True)
        cur.write_text(json.dumps({"mcpServers": {"gh": {"command": "x", "env": {"GITHUB_TOKEN": "ghp_secret"}}}}))
        os.chmod(str(cur), 0o600)
        old = os.umask(0o022)
        try:
            with mock.patch.object(Path, "home", lambda: self.home), \
                    mock.patch.object(cli, "_console_command", lambda: ["/usr/bin/hive"]):
                code, out, err = run(["mcp-config", "--local", "--client", "cursor", "--write"])
        finally:
            os.umask(old)
        self.assertEqual(code, 0, out + err)
        bak = Path(str(cur) + ".bak")
        self.assertIn("ghp_secret", bak.read_text())
        self.assertEqual(stat.S_IMODE(bak.stat().st_mode), 0o600)
        self.assertIn("gh", json.loads(cur.read_text())["mcpServers"])


# ---------------------------------------------------------------------------------------------------------------
# hub / gateway / approvals CLI

class TestHubCliFixes(_HubCli):
    def test_t5_warning_with_narrow_approval(self):
        self.init_hub()
        run(["hub", "add-node", "loc", "--direct", "http://127.0.0.1:7701", "--generate"])
        code, out, err = run(["hub", "add-agent", "ag2", "--approval", "fs.delete", "--nodes", "*"])
        self.assertEqual(code, 0, err)
        self.assertIn("can act as the hub", out)
        code, out, err = run(["hub", "add-agent", "ag3", "--deny", "shell.run"])     # job.start still runs commands
        self.assertIn("can act as the hub", out)
        code, out, err = run(["hub", "add-agent", "ag4", "--approval", "shell.run,job.start,service.*"])
        self.assertNotIn("can act as the hub", out)
        code, out, err = run(["hub", "add-agent", "ag5", "--approval", "*"])
        self.assertNotIn("can act as the hub", out)

    def test_add_node_direct_plaintext_public_refused(self):
        self.init_hub()
        code, out, err = run(["hub", "add-node", "pub", "--direct", "http://203.0.113.5:7701", "--generate"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("tailnet/LAN address", err)
        self.assertEqual(config.load_hub_config().nodes, [])
        self.assertNotIn("hive node token add", out)
        for url in ("http://192.168.1.5:7701", "http://100.101.2.3:7701"):
            code, out, err = run(["hub", "add-node", "n" + url.split(".")[-1].split(":")[0], "--direct", url,
                                  "--generate"])
            self.assertEqual(code, 0, out + err)
        code, out, err = run(["hub", "add-node", "tlsn", "--direct", "https://203.0.113.5:7701", "--generate"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("without --tls-pin", out)
        code, out, err = run(["hub", "add-node", "tlsp", "--direct", "https://203.0.113.6:7701", "--generate",
                              "--tls-pin", "sha256:" + "ab" * 32])
        self.assertEqual(code, 0, out + err)
        self.assertNotIn("without --tls-pin", out)

    def test_gateway_token_add_prints_settings(self):
        self.init_hub()
        code, out, err = run(["gateway", "token", "add", "chatbox", "--models", "desk-wsl/qwen"])
        self.assertEqual(code, 0, err)
        tok = [line for line in out.splitlines() if line.startswith("hv_")][0]
        self.assertEqual(out.count(tok), 1)                          # the token is printed once
        self.assertIn("Provider: OpenAI-compatible · Base URL: http://127.0.0.1:17999/openai/desk-wsl/qwen/v1 · "
                      "API key: the API key above", out)
        self.assertIn("Base URL http://127.0.0.1:17999/openai/desk-wsl/qwen\n", out)
        # node side
        port = helpers.free_port()
        self.assertEqual(run(["init", "node", "--yes", "--listen", f"127.0.0.1:{port}"])[0], 0)
        code, out, err = run(["gateway", "token", "add", "app", "--node", "--models", "qwen"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"Base URL: http://127.0.0.1:{port}/openai/self/qwen/v1", out)
        # a glob: only the pointer
        code, out, err = run(["gateway", "token", "add", "many", "--models", "pod-a/*"])
        self.assertEqual(code, 0, err)
        self.assertNotIn("Provider:", out)
        self.assertIn("hive gateway url NODE SERVICE", out)

    def test_gateway_url_escapes_model_and_sends_token(self):
        stub = helpers.StubHTTPServer()
        stub.add("GET", "/openai/desk/qwen/v1/models", 200,
                 {"object": "list", "data": [{"id": "evil\x1b]0;pwned\x07model"}]})
        stub.start()
        self.addCleanup(stub.stop)
        self.init_hub()
        config.edit_config(config.default_paths()["hub_config"], lambda r: r["listen"].update(port=stub.port),
                           kind="hub")
        tf = self.home / "gw.token"
        gtok = util.new_token()
        tf.write_text(gtok)
        code, out, err = run(["gateway", "url", "desk", "qwen", "--token-file", str(tf)])
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\x07", out)
        self.assertIn("Model: evil", out)
        auths = [r["headers"].get("Authorization") for r in stub.requests if r["path"].endswith("/v1/models")]
        self.assertEqual(auths, [f"Bearer {gtok}"])

    def test_rotate_token_branches(self):
        self.init_hub()
        run(["hub", "add-node", "desk", "--direct", "http://127.0.0.1:7701", "--generate"])
        old = [n for n in config.load_hub_config().nodes if n.name == "desk"][0].token
        code, out, err = run(["hub", "rotate-token", "desk"])
        self.assertEqual(code, 0, out + err)
        new = [n for n in config.load_hub_config().nodes if n.name == "desk"][0].token
        self.assertNotEqual(new, old)
        self.assertIn("hive node token revoke hub-home", out)
        self.assertIn(f"--sha256 {util.hash_token(new)}", out)
        self.assertNotIn(new, out)                                   # only the hash crosses machines
        run(["hub", "add-agent", "claude"])
        old_sha = [t for t in config.load_hub_config().tokens if t.name == "claude"][0].sha256
        code, out, err = run(["hub", "rotate-token", "claude"])
        self.assertEqual(code, 0, out + err)
        tok = [line for line in out.splitlines() if line.startswith("hv_")][0]
        t = [t for t in config.load_hub_config().tokens if t.name == "claude"][0]
        self.assertNotEqual(t.sha256, old_sha)
        self.assertEqual(t.sha256, util.hash_token(tok))
        self.assertFalse(any(x.sha256 == old_sha for x in config.load_hub_config().tokens))   # old one is gone
        self.assertEqual(run(["hub", "rotate-token", "owner"])[0], 2)
        self.assertEqual(run(["hub", "rotate-token", "nobody"])[0], 1)

    def test_remove_node_and_agent(self):
        self.init_hub()
        run(["hub", "add-node", "desk", "--direct", "http://127.0.0.1:7701", "--generate"])
        config.edit_config(config.default_paths()["hub_config"], lambda r: r.update(default_node="desk"),
                           kind="hub")
        self.assertEqual(run(["hub", "remove-node", "desk"])[0], 0)
        cfg = config.load_hub_config()
        self.assertEqual((cfg.nodes, cfg.default_node), ([], None))
        run(["hub", "add-agent", "claude"])
        self.assertEqual(run(["hub", "remove-agent", "claude"])[0], 0)
        self.assertFalse(any(t.name == "claude" for t in config.load_hub_config().tokens))
        self.assertEqual(run(["hub", "remove-agent", "claude"])[0], 1)
        self.assertEqual(run(["hub", "remove-agent", "owner"])[0], 1)
        self.assertTrue(any(t.kind == "owner" for t in config.load_hub_config().tokens))

    def _relay_ids(self, rec):
        cfg = config.load_hub_config()
        path = Path(cfg.data_dir) / "state" / "relay_ids.json"
        util.write_json_atomic(path, {"nodes": {"pod-a": rec}}, private=True)
        return path

    def test_ack_offline(self):
        self.init_hub()
        self.assertEqual(run(["hub", "add-node", "pod-a", "--relay", "--host", "192.168.1.9"])[0], 0)
        pend = {"ip": "203.0.113.7", "at": "2026-11-18T10:00:00Z", "hostname": "pod-x", "pod_id": "abc"}
        # refused: the bound instance registered after this attempt
        path = self._relay_ids({"key_sha256": "sha256:" + "1" * 64, "pending_sha256": "sha256:" + "2" * 64,
                                "pending": pend, "bound_seen_at": "2026-11-18T11:00:00Z"})
        code, out, err = run(["hub", "ack-node", "pod-a", "--yes"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("rotate-token", err)
        self.assertEqual(util.read_json(path)["nodes"]["pod-a"]["key_sha256"], "sha256:" + "1" * 64)
        # without --yes and no TTY: nothing changes
        self._relay_ids({"key_sha256": "sha256:" + "1" * 64, "pending_sha256": "sha256:" + "2" * 64,
                         "pending": pend, "bound_seen_at": "2026-11-18T09:00:00Z"})
        code, out, err = run(["hub", "ack-node", "pod-a"])
        self.assertEqual(code, 2, out + err)
        self.assertIn("pending registration for 'pod-a': from 203.0.113.7", out)
        code, out, err = run(["hub", "ack-node", "pod-a", "--yes"])
        self.assertEqual(code, 0, out + err)
        rec = util.read_json(path)["nodes"]["pod-a"]
        self.assertEqual(rec["key_sha256"], "sha256:" + "2" * 64)
        self.assertIsNone(rec["pending_sha256"])
        code, out, err = run(["hub", "ack-node", "pod-a", "--yes"])
        self.assertIn("no pending registration", out)
        self.assertEqual(run(["hub", "ack-node", "nope", "--yes"])[0], 1)

    def test_restart_outdated_lists_every_outdated_node(self):
        self.init_hub()
        for n in ("pod-a", "pod-b", "pod-c"):
            self.assertEqual(run(["hub", "add-node", n, "--relay", "--host", "192.168.1.9"])[0], 0)
        run(["hub", "add-node", "desk", "--direct", "http://127.0.0.1:7701", "--generate"])
        state = {"restarted": False}

        def rows():
            return [{"name": "pod-a", "online": True, "outdated": not state["restarted"], "auto_update": True,
                     "version": "0.9.0"},
                    {"name": "pod-b", "online": False, "outdated": True, "auto_update": True, "version": "0.8.0"},
                    {"name": "pod-c", "online": True, "outdated": True, "auto_update": False, "version": "0.9.\x1b[2J"},
                    {"name": "desk", "online": True, "outdated": True, "version": "0.9.1"}]

        class Tr:
            def request(self, method, path, body=None):
                if path == "/v1/hub/nodes/pod-a/restart":
                    state["restarted"] = True
                    return {"queued": True}
                return {"queued": False}

        class FakeClient:
            target = SimpleNamespace(transport=Tr())

            def nodes(self):
                return rows()

            def close(self):
                pass
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        with mock.patch.object(cli, "_hub_client", lambda args: FakeClient()), \
                mock.patch.object(cli, "_now", lambda: clock[0]), mock.patch.object(cli, "_sleep", sleep):
            code, out, err = run(["hub", "restart-node", "--outdated"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("pod-a: restart queued", out)
        self.assertIn("pod-b: outdated (version 0.8.0), offline", out)
        self.assertIn("pod-c: outdated", out)
        self.assertIn("auto_update off: upgrade by hand", out)
        self.assertIn("desk: outdated (version 0.9.1), direct node: upgrade by hand", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("all restarted nodes are up to date", out)
        # only direct outdated nodes: named, nothing queued
        state["restarted"] = True
        with mock.patch.object(cli, "_hub_client", lambda args: FakeClient()), \
                mock.patch.object(cli, "_now", lambda: clock[0]), mock.patch.object(cli, "_sleep", sleep):
            code, out, err = run(["hub", "restart-node", "--outdated"])
        self.assertIn("desk: outdated", out)
        self.assertIn("no auto-updating outdated relay node to restart", out)


class TestApprovalsAllDown(CliCase):
    def test_list_every_place_down(self):
        _write_conn("this-pc-owner", _free_url(), util.new_token())
        _write_conn("hub-local", _free_url(), util.new_token())
        code, out, err = run(["approvals", "list"])
        self.assertEqual(code, 4, out + err)
        self.assertIn("node not running", out)
        self.assertIn("hub not running", out)
        self.assertNotIn("no pending approvals", out)
        code, out, err = run(["approvals", "list", "--node"])
        self.assertEqual(code, 4, out + err)


if __name__ == "__main__":
    unittest.main()
