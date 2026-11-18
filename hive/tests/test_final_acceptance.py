"""Final acceptance (independent session) fixes: Part A (tests never use the default ports 7700/7701) and Part B items
1-5g (PHASE0_NOTES "External review fixes" > "Final acceptance")."""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hive import cli, config, install, presets, util
from hive import hub as hubmod
from hive.actions.node import auto_update_off
from hive.auth import CAUTIOUS_ACTIONS
from hive.client import connect
from hive.errors import HiveError

from tests import helpers
from tests.test_cli import CliCase, run
from tests.wp5util import FAST_RELAY, FakeRelay, call_async, start_relay_node

ROOT = Path(__file__).resolve().parents[1]
POSIX = os.name != "nt"


def init_node(name: str = "blk") -> None:
    """``hive init node`` on a free port (Part A: never the default 7701)."""
    code, out, err = run(["init", "node", "--yes", "--name", name, "--listen", f"127.0.0.1:{helpers.free_port()}"])
    assert code == 0, err


def init_hub() -> None:
    code, out, err = run(["init", "hub", "--yes", "--listen", f"127.0.0.1:{helpers.free_port()}"])
    assert code == 0, err


# ---------------------------------------------------------------------------------------------------------------
# Part A

class TestNoDefaultPorts(CliCase):
    """Part A: a CLI test that creates a node/hub config gets a free port (tests.test_cli.free_port_init), so a
    developer's own node on 7701 (or hub on 7700) can never answer a test's ``node status`` or ``call``."""

    def test_new_configs_get_a_free_port(self):
        from tests.test_cli import free_port_init
        argv = free_port_init(["init", "node", "--yes"])
        self.assertEqual(argv[:3], ["init", "node", "--yes"])
        self.assertEqual(argv[3], "--listen")
        self.assertNotIn(argv[4], ("127.0.0.1:7701", "127.0.0.1:7700"))
        for chosen in (["init", "node", "--no-listen"], ["init", "node", "--listen", "127.0.0.1:9"],
                       ["init", "hub", "--tailscale"], ["init", "client", "--url", "http://x:1"], ["nodes"]):
            self.assertEqual(free_port_init(chosen), chosen)
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        self.assertEqual(run(["init", "hub", "--yes"])[0], 0)
        node = json.loads((self.home / "node" / "config.json").read_text())["listen"]["port"]
        hub = json.loads((self.home / "hub" / "config.json").read_text())["listen"]["port"]
        self.assertNotIn(node, (7700, 7701))
        self.assertNotIn(hub, (7700, 7701))
        self.assertEqual(free_port_init(["init", "node", "--mirror", "cn"]), ["init", "node", "--mirror", "cn"])
        self.assertIn("--listen", free_port_init(["init", "node", "--force", "--yes"]))
        cc = json.loads((self.home / "client.json").read_text())["connections"]
        for name, c in cc.items():
            self.assertNotRegex(c["url"], r":770[01]$", name)


# ---------------------------------------------------------------------------------------------------------------
# B1 stub

class TestStubInReadme(unittest.TestCase):
    def test_readme_uses_the_shipped_stub(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertTrue((ROOT / "examples" / "stub_openai.py").is_file())
        self.assertIn("hive push desk hive/examples/stub_openai.py '~/hive-test/stub_openai.py'", text)
        self.assertIn("--argv 'python3 stub_openai.py 8000' --cwd '~/hive-test'", text)
        self.assertNotIn("stub.py`", text)


# ---------------------------------------------------------------------------------------------------------------
# B2 automatic hub connection

class TestAutoHubConnection(CliCase):
    def setUp(self):
        super().setUp()
        init_node("blk")
        init_hub()

    def ns(self, **kw):
        return argparse.Namespace(**kw)

    def test_rules(self):
        auto = cli._auto_hub_connection
        self.assertEqual(auto(self.ns(), "desk", owner_default=True), "hub-local")
        self.assertEqual(auto(self.ns(), "DESK", owner_default=False), "hub-local")     # default this-pc
        for same in ("blk", "self", "local", ".", "-", None, ""):
            self.assertIsNone(auto(self.ns(), same, owner_default=True), same)
        self.assertIsNone(auto(self.ns(connection="this-pc-owner"), "desk", owner_default=True))
        with mock.patch.dict(os.environ, {"HIVE_URL": "http://127.0.0.1:1"}):
            self.assertIsNone(auto(self.ns(), "desk", owner_default=True))
        with mock.patch.dict(os.environ, {"HIVE_CONNECTION": "this-pc"}):
            self.assertIsNone(auto(self.ns(), "desk", owner_default=True))

    def test_no_hub_connection_keeps_the_local_one(self):
        cc = json.loads((self.home / "client.json").read_text())
        cc["connections"].pop("hub-local")
        (self.home / "client.json").write_text(json.dumps(cc))
        self.assertIsNone(cli._auto_hub_connection(self.ns(), "desk", owner_default=True))

    def test_remote_default_connection_is_used(self):
        cc = json.loads((self.home / "client.json").read_text())
        cc["connections"]["home"] = dict(cc["connections"].pop("hub-local"))
        (self.home / "client.json").write_text(json.dumps(cc))
        tok = self.home / "tokens" / "home.token"
        tok.write_text("hv_" + "h" * 40)
        code, out, err = run(["init", "client", "--name", "home", "--url", f"http://127.0.0.1:{helpers.free_port()}",
                              "--token-file", str(tok), "--default"])
        self.assertEqual(code, 0, err)
        self.assertEqual(cli._auto_hub_connection(self.ns(), "desk", owner_default=True), "home")

    def test_commands_say_which_connection(self):
        for argv in (["call", "node.info", "--node", "desk"], ["actions", "--node", "desk"],
                     ["sh", "desk", "--", "echo", "hi"], ["logs", "desk", "--service", "x"],
                     ["service", "define", "x", "--argv", "python3 x.py", "--node", "desk"]):
            code, out, err = run(argv)
            self.assertIn("using the hub connection 'hub-local'", err, argv)
            self.assertIn("'desk' is not this computer's node ('blk')", err, argv)
        code, out, err = run(["call", "node.info", "--node", "blk"])
        self.assertNotIn("hub connection", err)

    def test_through_a_running_hub(self):
        url, toks, hub = helpers.start_hub()
        self.addCleanup(hub.test_stop)
        cc = json.loads((self.home / "client.json").read_text())
        Path(cc["connections"]["hub-local"]["token_file"]).write_text(toks["owner"] + "\n")
        cc["connections"]["hub-local"]["url"] = url
        (self.home / "client.json").write_text(json.dumps(cc))
        code, out, err = run(["call", "node.info", "--node", "desk"])
        self.assertEqual(code, 1, err)
        self.assertIn("node 'desk' not found", err)                 # answered by the hub, not by the local node


# ---------------------------------------------------------------------------------------------------------------
# B3 token set

class TestTokenSet(CliCase):
    def tokens(self):
        return {t["name"]: t for t in json.loads((self.home / "node" / "config.json").read_text())["auth"]["tokens"]}

    def test_node_token_set(self):
        init_node()
        before = self.tokens()["this-pc"]
        code, out, err = run(["node", "token", "set", "this-pc", "--cautious"])
        self.assertEqual(code, 0, err)
        self.assertIn("the token itself is unchanged", out)
        t = self.tokens()["this-pc"]
        self.assertEqual((t["sha256"], t["require_approval"]), (before["sha256"], list(CAUTIOUS_ACTIONS)))
        self.assertEqual(run(["node", "token", "set", "this-pc", "--no-cautious"])[0], 0)
        self.assertNotIn("require_approval", self.tokens()["this-pc"])
        self.assertEqual(run(["node", "token", "set", "this-pc"])[0], 2)
        self.assertEqual(run(["node", "token", "set", "this-pc", "--cautious", "--no-cautious"])[0], 2)
        self.assertEqual(run(["node", "token", "set", "nope", "--cautious"])[0], 1)
        code, out, err = run(["node", "token", "set", "owner", "--cautious"])
        self.assertEqual(code, 2)
        self.assertIn("agent tokens only", err)
        code, out, err = run(["node", "token", "list"])
        self.assertIn("this-pc", out)

    def test_hub_set_agent(self):
        init_hub()
        self.assertEqual(run(["hub", "add-agent", "claude"])[0], 0)
        code, out, err = run(["hub", "set-agent", "claude", "--cautious"])
        self.assertEqual(code, 0, err)
        toks = {t["name"]: t for t in json.loads((self.home / "hub" / "config.json").read_text())["tokens"]}
        self.assertEqual(toks["claude"]["require_approval"], list(CAUTIOUS_ACTIONS))
        self.assertEqual(run(["hub", "set-agent", "claude", "--no-cautious"])[0], 0)
        toks = {t["name"]: t for t in json.loads((self.home / "hub" / "config.json").read_text())["tokens"]}
        self.assertNotIn("require_approval", toks["claude"])
        self.assertEqual(run(["hub", "set-agent", "nope", "--cautious"])[0], 1)


# ---------------------------------------------------------------------------------------------------------------
# B4 mcp-config --remove

class TestMcpRemove(CliCase):
    def test_desktop_and_cursor(self):
        cfgf = self.home / "Claude" / "claude_desktop_config.json"
        cfgf.parent.mkdir(parents=True)
        cfgf.write_text(json.dumps({"mcpServers": {"hive": {"command": "hive"}, "other": {"command": "x"}},
                                    "theme": "dark"}))
        with mock.patch.object(cli, "_desktop_config_path", return_value=cfgf):
            code, out, err = run(["mcp-config", "--client", "claude-desktop", "--remove"])
            self.assertEqual(code, 0, err)
            self.assertIn('removed "hive"', out)
            data = json.loads(cfgf.read_text())
            self.assertEqual(data, {"mcpServers": {"other": {"command": "x"}}, "theme": "dark"})
            self.assertIn('"hive"', (cfgf.parent / "claude_desktop_config.json.bak").read_text())
            code, out, err = run(["mcp-config", "--remove"])                   # default client: claude-desktop
            self.assertIn("nothing to remove", out)
        with mock.patch.object(Path, "home", return_value=self.home):
            cur = self.home / ".cursor" / "mcp.json"
            cur.parent.mkdir()
            cur.write_text(json.dumps({"mcpServers": {"hive": {}}}))
            self.assertEqual(run(["mcp-config", "--client", "cursor", "--remove"])[0], 0)
            self.assertEqual(json.loads(cur.read_text()), {"mcpServers": {}})
        code, out, err = run(["mcp-config", "--client", "claude-code", "--remove"])
        self.assertIn("claude mcp remove hive", out)
        self.assertEqual(run(["mcp-config", "--client", "generic", "--remove"])[0], 2)

    def test_works_without_any_hive_config(self):
        missing = self.home / "none" / "claude_desktop_config.json"
        with mock.patch.object(cli, "_desktop_config_path", return_value=missing):
            code, out, err = run(["mcp-config", "--remove"])
        self.assertEqual(code, 0, err)
        self.assertIn(f"nothing to remove: {missing} does not exist", out)
        self.assertNotIn("no \"hive\" entry", out)
        # FA-C4: cursor without ~/.cursor/mcp.json does not suggest the file exists
        with mock.patch.object(Path, "home", return_value=self.home / "nohome"):
            code, out, err = run(["mcp-config", "--client", "cursor", "--remove"])
        self.assertEqual(code, 0, err)
        self.assertIn("does not exist", out)
        self.assertNotIn("no \"hive\" entry", out)

    def test_remove_with_write_is_a_usage_error(self):
        cfgf = self.home / "Claude" / "claude_desktop_config.json"
        cfgf.parent.mkdir(parents=True)
        cfgf.write_text(json.dumps({"mcpServers": {"hive": {"command": "hive"}}}))
        with mock.patch.object(cli, "_desktop_config_path", return_value=cfgf):
            code, out, err = run(["mcp-config", "--remove", "--write"])
        self.assertEqual(code, 2)
        self.assertIn("--remove and --write cannot be used together", out + err)
        self.assertIn('"hive"', cfgf.read_text())                  # nothing removed

    def test_readme_mentions_it(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("hive mcp-config --client claude-desktop --remove", text)


# ---------------------------------------------------------------------------------------------------------------
# B5a unknown action through the hub

class TestHubUnknownAction(unittest.TestCase):
    def test_typo_and_newer_node(self):
        e = {"name": "pod-a", "mode": "relay", "token_sha256": None}
        rtok = util.new_token()
        e["token_sha256"] = util.hash_token(rtok)
        url, toks, hub = helpers.start_hub([e], relay=dict(FAST_RELAY))
        self.addCleanup(hub.test_stop)
        fake = FakeRelay(url, rtok, "pod-a", actions=["fs.stat", "fs.teleport"])
        fake.register()
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        with self.assertRaises(HiveError) as cm:
            c.call("fs.sta", {}, node="pod-a")
        self.assertIn("did you mean 'fs.stat'", cm.exception.hint)
        self.assertIn("hive actions", cm.exception.hint)
        self.assertNotIn("upgrade", cm.exception.hint)
        self.assertIn("fs.stat", cm.exception.details["suggestions"])
        with self.assertRaises(HiveError) as cm:
            c.call("fs.teleport", {}, node="pod-a")                 # the node knows it, the hub does not
        self.assertIn("upgrade the hub", cm.exception.hint)
        fake.register(version="0.9.0", info=dict(fake.info, version="0.9.0"))
        with self.assertRaises(HiveError) as cm:
            c.call("fs.sta", {}, node="pod-a")
        self.assertIn("node 'pod-a' hive 0.9.0", cm.exception.hint)


# ---------------------------------------------------------------------------------------------------------------
# B5b echo arguments in the approval review

class TestEchoWordsNotListed(unittest.TestCase):
    def listed(self, cmd):
        words, _m, _u, quiet = presets.bind_plan_ex(cmd, shell="bash", os_name="posix")
        return sorted(w for w in words if presets.listable_missing(w, quiet))

    def test_echo_printf(self):
        self.assertEqual(self.listed("echo hello world"), [])
        self.assertEqual(self.listed("printf done; echo -n ok"), [])
        self.assertEqual(self.listed(["echo", "hello"]), [])
        self.assertEqual(self.listed("sudo echo plain"), [])
        self.assertEqual(self.listed("echo hello; cat hello"), ["hello"])       # used as a file elsewhere: listed
        self.assertEqual(self.listed("echo a.txt"), ["a.txt"])                  # path-like words keep the rule
        self.assertEqual(self.listed("ls notes"), ["notes"])
        self.assertIn("./run3", self.listed("python run3"))                     # interpreter operands unchanged

    def test_piped_into_a_shell_is_still_planned(self):
        words, _m, _u, quiet = presets.bind_plan_ex("echo 'python run3' | sh", shell="bash", os_name="posix")
        self.assertIn("./run3", words)


# ---------------------------------------------------------------------------------------------------------------
# B5c relay goodbye

class TestRelayGoodbye(unittest.TestCase):
    def hub(self):
        tok = util.new_token()
        relay = dict(FAST_RELAY, offline_after=60)          # "offline" below can only come from the goodbye
        url, toks, hub = helpers.start_hub([{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(tok)}],
                                           relay=relay)
        self.addCleanup(hub.test_stop)
        return url, toks, hub, tok

    def test_unregister_route(self):
        url, toks, hub, tok = self.hub()
        fake = FakeRelay(url, tok, "pod-a")
        fake.register()
        link = hub.links["pod-a"]
        self.assertTrue(link.online())
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        box = call_async(c.call, "fs.stat", {"path": "/tmp"}, node="pod-a")
        helpers.wait_until(lambda: link.queue, 5)
        self.assertEqual(fake.t.request("POST", "/v1/relay/unregister", {"session_id": "s_stale"}),
                         {"unregistered": False})
        self.assertTrue(link.online())
        self.assertEqual(fake.t.request("POST", "/v1/relay/unregister", {"session_id": fake.session,
                                                                         "reason": "panic"}),
                         {"unregistered": True})
        self.assertFalse(link.online())
        box["thread"].join(10)
        e = box["error"]
        self.assertEqual((e.code, e.details.get("reason")), ("unavailable", "offline"))
        self.assertIn("hive node resume", e.hint)
        self.assertFalse([r for r in c.nodes() if r["name"] == "pod-a"][0]["online"])
        fake.register()                                         # back again
        self.assertTrue(link.online())
        self.assertIsNone(link.gone)

    def test_panic_and_stop_tell_the_hub(self):
        url, toks, hub, tok = self.hub()
        rt = start_relay_node(url, tok, start_managers=False)
        self.addCleanup(rt.test_stop)
        link = hub.links["pod-a"]
        helpers.wait_until(link.online, 15)
        util.write_json_atomic(Path(rt.data_dir) / "relay.disabled", {"at": util.iso_utc(), "by": "panic"},
                               private=True)
        t0 = time.monotonic()
        helpers.wait_until(lambda: not link.online(), 5)        # the node's 1 s watcher, not offline_after (60 s)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(link.gone, "panic")
        os.unlink(str(Path(rt.data_dir) / "relay.disabled"))
        helpers.wait_until(link.online, 15)
        rt.relay.stop(goodbye=True)
        self.assertFalse(link.online())
        self.assertEqual(link.gone, "stop")

    def test_restart_sends_no_goodbye(self):
        src = (ROOT / "hive" / "node.py").read_text(encoding="utf-8")
        self.assertIn("relay.stop(goodbye=rt.exit_code != EXIT_RESTART)", src)


# ---------------------------------------------------------------------------------------------------------------
# B5d gateway url --token-file

class TestGatewayUrlTokenFile(CliCase):
    def test_no_token_hint_only_without_a_token(self):
        init_node()
        code, out, err = run(["gateway", "url", "self", "qwen", "--node"])
        self.assertEqual(code, 0, err)
        self.assertIn("no token yet?", out)
        tf = self.home / "gw.token"
        tf.write_text("hv_" + "g" * 40 + "\n")
        code, out, err = run(["gateway", "url", "self", "qwen", "--node", "--token-file", str(tf)])
        self.assertEqual(code, 0, err)
        self.assertNotIn("no token yet?", out)


# ---------------------------------------------------------------------------------------------------------------
# B5e direct nodes: "checking", then a fast probe

class TestDirectNodeChecking(unittest.TestCase):
    def test_checking_until_the_first_probe(self):
        port = helpers.free_port()
        url, toks, hub = helpers.start_hub([{"name": "desk", "mode": "direct", "url": f"http://127.0.0.1:{port}",
                                             "token": "hv_" + "d" * 40}])
        self.addCleanup(hub.test_stop)
        link = hub.links["desk"]
        with link.lock:
            link._probes_done = 0
        row = link.summary()
        self.assertEqual((row["online"], row["checking"]), (False, True))
        with link.lock:
            link._probes_done = 1
        self.assertFalse(link.summary()["checking"])
        self.assertEqual(cli._node_state({"online": False, "checking": True}), "checking")
        self.assertEqual(cli._node_state({"online": False}), "offline")
        self.assertEqual(cli._node_state({"online": True, "checking": True}), "online")

    def test_a_node_that_comes_up_later_is_online_within_seconds(self):
        port = helpers.free_port()
        hub_tok = util.new_token()
        with mock.patch.object(hubmod, "PROBE_INTERVAL", 3600.0):
            url, toks, hub = helpers.start_hub([{"name": "desk", "mode": "direct",
                                                 "url": f"http://127.0.0.1:{port}", "token": hub_tok}])
            self.addCleanup(hub.test_stop)
            link = hub.links["desk"]
            helpers.wait_until(lambda: link._probes_done >= 1, 10)
            self.assertFalse(link.online())
            nurl, _tok, rt = helpers.start_node_server(
                listen={"port": port}, auth={"tokens": [{"name": "hub-home", "sha256": util.hash_token(hub_tok),
                                                         "kind": "hub", "role": "operator"}]})
            self.addCleanup(rt.test_stop)
            self.assertTrue(nurl.endswith(f":{port}"))
            helpers.wait_until(link.online, 12)                  # fast re-probe (2, 4, then 5 s), not 3600 s

    def test_node_that_refuses_the_hub_token_gets_a_note(self):
        # FA-C3: the node runs but does not know the hub token yet (401): say so instead of a bare "offline"
        nurl, _tok, rt = helpers.start_node_server()
        self.addCleanup(rt.test_stop)
        with mock.patch.object(hubmod, "PROBE_INTERVAL", 3600.0):
            url, toks, hub = helpers.start_hub([{"name": "desk", "mode": "direct", "url": nurl,
                                                 "token": util.new_token()}])
            self.addCleanup(hub.test_stop)
            link = hub.links["desk"]
            helpers.wait_until(lambda: link._probes_done >= 1, 10)
            row = link.summary()
            self.assertEqual((row["online"], row["checking"], row["hub_token_rejected"]), (False, False, True))
            note = cli._node_notes(row, "vm")
            self.assertIn("hub token not accepted", note)
            self.assertIn("hive node token add hub-vm", note)
            self.assertIn("hub-…", cli._hub_token_note(row))
        # a machine that is switched off (nothing listening) gets no such note
        port = helpers.free_port()
        with mock.patch.object(hubmod, "PROBE_INTERVAL", 3600.0):
            url2, toks2, hub2 = helpers.start_hub([{"name": "off", "mode": "direct",
                                                    "url": f"http://127.0.0.1:{port}", "token": util.new_token()}])
            self.addCleanup(hub2.test_stop)
            link2 = hub2.links["off"]
            helpers.wait_until(lambda: link2._probes_done >= 1, 10)
            row2 = link2.summary()
            self.assertEqual((row2["online"], row2["hub_token_rejected"]), (False, False))
            self.assertEqual(cli._node_notes(row2, "vm"), "")
        self.assertEqual(cli._hub_token_note({"online": True, "hub_token_rejected": True}), "")

    def test_hive_nodes_prints_the_note(self):
        rows = [{"name": "desk", "online": False, "mode": "direct", "hub_token_rejected": True},
                {"name": "pod", "online": True, "mode": "relay"}]
        fake = mock.Mock()
        fake.nodes.return_value = rows
        buf = io.StringIO()
        with mock.patch.object(cli, "_connect", return_value=fake), \
                mock.patch.object(cli, "_hub_local_hint", return_value=None), mock.patch("sys.stdout", buf):
            self.assertEqual(cli.cmd_nodes(argparse.Namespace(), cli.Out()), 0)
        text = buf.getvalue()
        self.assertIn("note: desk: hub token not accepted", text)
        self.assertNotIn("note: pod", text)


# ---------------------------------------------------------------------------------------------------------------
# B5f "applied by the watchdog"

class TestWatchdogApplies(CliCase):
    def test_message(self):
        init_node()
        with mock.patch.object(install, "supervisor", return_value="watchdog"):
            code, out, err = run(["init", "node", "--no-listen"])
        self.assertEqual(code, 0, err)
        self.assertIn("applied by the watchdog", out)
        self.assertNotIn("will apply on next start", out)
        with mock.patch.object(install, "supervisor", return_value=None):
            code, out, err = run(["init", "node", "--listen", f"127.0.0.1:{helpers.free_port()}"])
        self.assertIn("will apply on next start", out)

    def test_supervisor_detection(self):
        init_node()
        with mock.patch.object(install, "_watchdog_alive", return_value=4242):
            self.assertEqual(install.supervisor("node"), "watchdog")
        with mock.patch.object(install, "_watchdog_alive", return_value=None), \
                mock.patch.object(install, "_start_hive_alive", return_value=False), \
                mock.patch.object(install, "_launcher_alive", return_value=True):
            self.assertEqual(install.supervisor("node"), "launcher")
        with mock.patch.object(install, "_watchdog_alive", return_value=None), \
                mock.patch.object(install, "_start_hive_alive", return_value=False), \
                mock.patch.object(install, "_launcher_alive", return_value=False), \
                mock.patch.object(install, "_installed", return_value=(None, False)):
            self.assertIsNone(install.supervisor("node"))

    @unittest.skipUnless(POSIX, "POSIX sh")
    def test_start_hive_pid(self):
        init_node()
        run_dir = self.home / "node" / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        p = subprocess.Popen(["sh", "-c", "sleep 30", "start-hive"])
        self.addCleanup(p.wait, 10)
        self.addCleanup(p.kill)
        (run_dir / "start-hive.pid").write_text(str(p.pid))
        ctx = install._context("node", strict=False)
        helpers.wait_until(lambda: install._start_hive_alive(ctx), 5)
        (run_dir / "start-hive.pid").write_text(str(os.getpid()))      # not a start-hive.sh loop
        self.assertFalse(install._start_hive_alive(ctx))
        self.assertIn('run/start-hive.pid', (ROOT / "deploy" / "runpod" / "start-hive.sh").read_text())


# ---------------------------------------------------------------------------------------------------------------
# B5g why auto-update is off

class TestAutoUpdateWhy(CliCase):
    def test_reason(self):
        R = SimpleNamespace
        self.assertIsNone(auto_update_off(None))
        self.assertIsNone(auto_update_off(R(enabled=False, auto_update=False)))
        self.assertIsNone(auto_update_off(R(enabled=True, auto_update=True, role="operator")))
        self.assertEqual(auto_update_off(R(enabled=True, auto_update=False, role="files")), "role:files")
        self.assertEqual(auto_update_off(R(enabled=True, auto_update=False, role="operator", deny_actions=["x"])),
                         "deny_actions")
        with mock.patch.dict(os.environ, {"HIVE_WATCHDOG": "0"}):
            self.assertEqual(auto_update_off(R(enabled=True, auto_update=False, role="operator")), "not_pod")
        with mock.patch.dict(os.environ, {"HIVE_WATCHDOG": "1"}):
            self.assertEqual(auto_update_off(R(enabled=True, auto_update=False, role="operator")), "off")
        row = hubmod.summary_from_info("p", "relay", {"modes": {"auto_update_off": "role:files"}}, {}, entry=None)
        self.assertEqual(row["auto_update_off"], "role:files")
        row = hubmod.summary_from_info("p", "relay", {"modes": {"auto_update_off": "\x1b[31mx"}}, {}, entry=None)
        self.assertIsNone(row["auto_update_off"])

    def test_restart_node_lines(self):
        text = "\n".join(cli._auto_update_off_lines("pod-a", "0.9", "role:files"))
        self.assertIn("relay role is 'files'", text)
        self.assertIn("hive init node --relay-role operator --auto-update", text)
        text = "\n".join(cli._auto_update_off_lines("pod-a", "0.9", "not_pod"))
        self.assertIn("not a cloud pod", text)
        self.assertIn("hive node restart", text)
        text = "\n".join(cli._auto_update_off_lines("pod-a", "0.9", None))       # an older node: both reasons
        self.assertIn("relay role operator", text)
        self.assertNotIn("auto_update off: upgrade by hand", text)

    def test_init_node_auto_update(self):
        code, out, err = run(["init", "node", "--yes", "--relay", "http://127.0.0.1:7", "--token-env", "RT",
                              "--relay-role", "files"], env={"RT": "hv_" + "r" * 40})
        self.assertEqual(code, 0, err)
        rel = lambda: json.loads((self.home / "node" / "config.json").read_text())["relay"]  # noqa: E731
        self.assertFalse(rel()["auto_update"])
        code, out, err = run(["init", "node", "--auto-update"])
        self.assertEqual(code, 2)
        self.assertIn("relay role operator", err)
        code, out, err = run(["init", "node", "--relay-role", "operator", "--auto-update"])
        self.assertEqual(code, 0, err)
        self.assertTrue(rel()["auto_update"])
        self.assertEqual(run(["init", "node", "--no-auto-update"])[0], 0)
        self.assertFalse(rel()["auto_update"])

    def test_init_node_auto_update_needs_a_relay(self):
        init_node()
        code, out, err = run(["init", "node", "--auto-update"])
        self.assertEqual(code, 2)
        self.assertIn("relay nodes", err)


if __name__ == "__main__":
    unittest.main()
