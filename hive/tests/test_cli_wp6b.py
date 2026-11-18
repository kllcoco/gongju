"""WP6b: the remaining CLI (§19, §21.2 tools/render/mcp/cli row): join (HIVE_JOIN, --pod detection, --wsl, an existing
config keeps everything but relay), hub (add-node/add-agent incl. the T5 warning, list, rotate/remove, restart-node and
ack-node over hub-local, stop/restart of a real hub process), gateway token/url, approvals over the owner routes,
mcp-config (agent vs owner, console script), service, tls-pin, doctor, audit tail, dist build, push/pull, logs."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hive import cli, config, util
from hive.dist import encode_join
from hive.errors import HiveError

from tests import helpers
from tests.test_cli import CliCase, run
from tests.wp5util import FAST_RELAY, FakeRelay, hub_token_entry


def _write_conn(name: str, url: str, token: str, *, default: bool = False) -> None:
    """Add a client.json connection with a token file (as init would)."""
    tf = config.default_paths()["tokens"] / f"{name}.token"
    tf.parent.mkdir(parents=True, exist_ok=True)
    tf.write_text(token + "\n")

    def upd(c: dict) -> None:
        c.setdefault("connections", {})[name] = {"url": url, "token_file": str(tf)}
        if default:
            c["default"] = name
    config._client_update(upd)


def _free_url() -> str:
    return f"http://127.0.0.1:{helpers.free_port()}"


class TestJoin(CliCase):
    def code(self, name="pod-a", url=None):
        return encode_join({"u": url or _free_url(), "t": util.new_token(), "p": None, "n": name})

    def test_join_reads_env_and_records_consent(self):
        code, out, err = run(["join", "--yes"], env={"HIVE_JOIN": self.code(), "HIVE_RELAY_PROXY": "off"})
        self.assertEqual(code, 0, err)
        self.assertIn("not reachable now", out)
        cfg = config.load_node_config(str(self.home / "node" / "config.json"))
        self.assertEqual(cfg.name, "pod-a")
        self.assertTrue(cfg.relay.enabled)
        self.assertEqual(cfg.relay.proxy, "off")
        self.assertEqual(cfg.consent["by"], "join")
        self.assertEqual(cfg.consent["text"], "consent given by join code")
        self.assertNotIn("hv_", out + err)

    def test_join_stdin_and_missing(self):
        code, out, err = run(["join", "-", "--relay-proxy", "http://127.0.0.1:1055"], stdin=self.code("box") + "\n",
                             env={"HIVE_RELAY_PROXY": ""})
        self.assertEqual(code, 0, err)
        self.assertEqual(config.load_node_config().relay.proxy, "http://127.0.0.1:1055")
        with mock.patch.dict(os.environ, {"HIVE_JOIN": ""}):
            self.assertEqual(run(["join"])[0], 2)
        self.assertEqual(run(["join", "hvj1.garbage"])[0], 2)

    def test_join_implies_pod(self):
        with mock.patch("hive.sysinfo.cloud_info", return_value={"provider": "autodl", "pod_id": None}):
            code, out, err = run(["join"], env={"HIVE_JOIN": self.code()})
        self.assertEqual(code, 0, err)
        cfg = config.load_node_config()
        self.assertIn("cloud", cfg.tags)
        self.assertFalse(cfg.listen.enabled)
        self.assertIn("pod topology", out)
        # --no-pod overrides the detection
        shutil.rmtree(str(self.home / "node"))
        with mock.patch("hive.sysinfo.cloud_info", return_value={"provider": "autodl", "pod_id": None}):
            code, out, err = run(["join", "--no-pod"], env={"HIVE_JOIN": self.code()})
        self.assertEqual(code, 0, err)
        self.assertNotIn("cloud", config.load_node_config().tags)

    def test_join_wsl_new_and_existing(self):
        code, out, err = run(["join", "--wsl"], env={"HIVE_JOIN": self.code("ignored")})
        self.assertEqual(code, 0, err)
        cfg = config.load_node_config()
        self.assertEqual(cfg.listen.port, 7702)
        self.assertTrue(any(t.startswith("wsl-host:") for t in cfg.tags))
        tokens = [t.sha256 for t in cfg.auth.tokens]
        port, tags = cfg.listen.port, list(cfg.tags)
        # existing config: only relay (+ name) changes; --wsl/--mirror are ignored with a note
        code, out, err = run(["join", "--wsl", "--mirror", "cn"], env={"HIVE_JOIN": self.code("other-box")})
        self.assertEqual(code, 0, err)
        self.assertIn("applies only when creating", out)
        cfg2 = config.load_node_config()
        self.assertEqual(cfg2.name, "other-box")
        self.assertEqual([t.sha256 for t in cfg2.auth.tokens], tokens)
        self.assertEqual((cfg2.listen.port, cfg2.tags), (port, tags))
        self.assertIsNone(cfg2.models.hf_endpoint)

    def test_join_pin_mismatch_refused(self):
        stub = helpers.StubHTTPServer()
        stub.add("GET", "/v1/health", 200, {"ok": True})
        stub.start()
        self.addCleanup(stub.stop)
        # a plaintext public URL is refused before anything is written
        code, out, err = run(["join"], env={"HIVE_JOIN": encode_join({"u": "http://93.184.216.34:7700",
                                                                       "t": util.new_token(), "p": None,
                                                                       "n": "x"})})
        self.assertNotEqual(code, 0)
        self.assertFalse((self.home / "node" / "config.json").exists())


class _HubCli(CliCase):
    def init_hub(self, *extra):
        code, out, err = run(["init", "hub", "--yes", "--name", "home", "--listen", "127.0.0.1:17999"] + list(extra))
        self.assertEqual(code, 0, err)
        return config.default_paths()["hub_config"]


class TestHubConfigCommands(_HubCli):
    def test_add_node_relay_and_direct(self):
        self.init_hub()
        # loopback listen + no --host: refused, nothing written
        code, out, err = run(["hub", "add-node", "pod-a", "--relay"])
        self.assertEqual(code, 1, out + err)
        self.assertIn("cannot reach", err)
        self.assertEqual(config.load_hub_config().nodes, [])
        code, out, err = run(["hub", "add-node", "pod-a", "--relay", "--host", "192.168.1.9", "--tags", "cloud,gpu"])
        self.assertEqual(code, 0, err)
        self.assertIn("HIVE_JOIN=", out)
        n = config.load_hub_config().nodes[0]
        self.assertEqual((n.name, n.mode, n.tags), ("pod-a", "relay", ["cloud", "gpu"]))
        self.assertEqual(run(["hub", "add-node", "pod-a", "--relay", "--host", "h"])[0], 2)
        code, out, err = run(["hub", "add-node", "desk", "--direct", "http://127.0.0.1:7701", "--generate"])
        self.assertEqual(code, 0, err)
        self.assertIn("hive node token add hub-home --kind hub --sha256 sha256:", out)
        n = [x for x in config.load_hub_config().nodes if x.name == "desk"][0]
        self.assertIn(util.hash_token(n.token), out)
        self.assertNotIn(n.token, out)                 # only the hash crosses machines
        # rotate a relay token: a fresh block; the stored hash changes
        old = config.load_hub_config().nodes[0].token_sha256
        code, out, err = run(["hub", "rotate-token", "pod-a", "--host", "192.168.1.9"])
        self.assertEqual(code, 0, err)
        self.assertIn("HIVE_JOIN=", out)
        self.assertNotEqual(config.load_hub_config().nodes[0].token_sha256, old)
        code, out, err = run(["hub", "list", "--json"])
        self.assertEqual({n["name"] for n in json.loads(out)["nodes"]}, {"pod-a", "desk"})
        self.assertEqual(run(["hub", "remove-node", "desk"])[0], 0)
        self.assertEqual(run(["hub", "remove-node", "desk"])[0], 1)

    def test_add_agent_t5_warning(self):
        self.init_hub()
        run(["hub", "add-node", "hubhost", "--direct", "http://127.0.0.1:7701", "--generate"])
        code, out, err = run(["hub", "add-agent", "claude"])
        self.assertEqual(code, 0, err)
        t = [x for x in config.load_hub_config().tokens if x.name == "claude"][0]
        tok = [line for line in out.splitlines() if line.startswith("hv_")][0]
        self.assertEqual(util.hash_token(tok), t.sha256)
        self.assertIn("hive init client --name home", out)
        self.assertIn("--default", out)
        self.assertIn("can act as the hub", out)
        code, out, err = run(["hub", "add-agent", "careful", "--cautious"])
        self.assertNotIn("can act as the hub", out)
        self.assertTrue([x for x in config.load_hub_config().tokens if x.name == "careful"][0].require_approval)
        code, out, err = run(["hub", "add-agent", "pods-only", "--nodes", "pod-*"])
        self.assertNotIn("can act as the hub", out)
        code, out, err = run(["hub", "add-agent", "reader", "--role", "readonly"])
        self.assertNotIn("can act as the hub", out)
        self.assertEqual(run(["hub", "add-agent", "claude"])[0], 2)
        self.assertEqual(run(["hub", "remove-agent", "owner"])[0], 1)
        self.assertEqual(run(["hub", "remove-agent", "claude"])[0], 0)

    def test_gateway_tokens(self):
        self.init_hub()
        code, out, err = run(["gateway", "token", "add", "chatbox", "--models", "desk-wsl/qwen,pod-a/*"])
        self.assertEqual(code, 0, err)
        tok = [line for line in out.splitlines() if line.startswith("hv_")][0]
        t = [x for x in config.load_hub_config().tokens if x.name == "chatbox"][0]
        self.assertEqual((t.kind, t.models, t.sha256), ("models", ["desk-wsl/qwen", "pod-a/*"], util.hash_token(tok)))
        self.assertIn("hive gateway url desk-wsl qwen", out)
        self.assertEqual(run(["gateway", "token", "add", "bad", "--models", "qwen"])[0], 2)
        code, out, err = run(["gateway", "token", "list", "--json"])
        self.assertEqual(json.loads(out), [{"name": "chatbox", "models": ["desk-wsl/qwen", "pod-a/*"],
                                            "expires": None}])
        self.assertEqual(run(["gateway", "token", "revoke", "chatbox"])[0], 0)
        self.assertEqual(run(["gateway", "token", "revoke", "chatbox"])[0], 1)
        # node side
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        code, out, err = run(["gateway", "token", "add", "app", "--node", "--models", "qwen"])
        self.assertEqual(code, 0, err)
        nt = [x for x in config.load_node_config().auth.tokens if x.name == "app"][0]
        self.assertEqual((nt.kind, nt.models), ("models", ["qwen"]))

    def test_gateway_url_bases(self):
        self.init_hub()
        with mock.patch("hive.httpserver.tailscale_info", return_value=("100.101.1.7", ["home-hub.tail1.ts.net."],
                                                                        "")):
            def fn(raw):
                raw["listen"]["host"] = "tailscale"
                raw["public_url"] = "https://home-hub.tail1.ts.net"
            config.edit_config(config.default_paths()["hub_config"], fn, kind="hub")
            code, out, err = run(["gateway", "url", "desk-wsl", "qwen"])
        self.assertEqual(code, 0, err)
        for base in ("http://100.101.1.7:17999/openai/desk-wsl/qwen", "http://home-hub.tail1.ts.net:17999/openai/"
                     "desk-wsl/qwen", "https://home-hub.tail1.ts.net/openai/desk-wsl/qwen",
                     "http://127.0.0.1:17999/openai/desk-wsl/qwen",
                     "http://host.docker.internal:17999/openai/desk-wsl/qwen"):
            self.assertIn(f"Base URL: {base}/v1 ", out)
            self.assertIn(f"Base URL {base}\n", out)        # the /v1-less variant
        self.assertIn("apps on your tailnet (recommended)", out)
        self.assertIn("100.64.0.0/10 and *.ts.net", out)
        self.assertIn("browser apps on https pages", out)
        # TLS listen -> https scheme
        config.edit_config(config.default_paths()["hub_config"],
                           lambda r: r["listen"].update(host="127.0.0.1", tls={"cert": "/c.pem", "key": "/k.pem"}),
                           kind="hub")
        code, out, err = run(["gateway", "url", "pod-a", "qwen"])
        self.assertIn("https://127.0.0.1:17999/openai/pod-a/qwen/v1", out)
        # --node uses the node's own port
        self.assertEqual(run(["init", "node", "--yes", "--listen", "127.0.0.1:17702"])[0], 0)
        code, out, err = run(["gateway", "url", "self", "qwen", "--node"])
        self.assertEqual(code, 0, err)
        self.assertIn("http://127.0.0.1:17702/openai/self/qwen/v1", out)
        self.assertEqual(run(["gateway", "url", "x", "Bad Name"])[0], 2)

    def test_gateway_url_lists_models(self):
        stub = helpers.StubHTTPServer()
        stub.add("GET", "/openai/desk/qwen/v1/models", 200, {"object": "list", "data": [{"id": "qwen2.5-7b"}]})
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
        self.assertIn("served models: qwen2.5-7b", out)
        self.assertIn("Model: qwen2.5-7b", out)
        self.assertEqual([r["headers"].get("Authorization") for r in stub.requests], [f"Bearer {gtok}"])


class TestHubOwnerRoutes(CliCase):
    """restart-node / ack-node over hub-local (§19 v5/v6)."""

    def setUp(self):
        super().setUp()
        self.rtok = util.new_token()
        self.url, self.toks, self.hub = helpers.start_hub(
            [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(self.rtok)}], relay=dict(FAST_RELAY))
        self.addCleanup(self.hub.test_stop)
        _write_conn("hub-local", self.url, self.toks["owner"])
        self.cfgp = str(self.hub.config_path)

    def go_offline(self):
        link = self.hub.links["pod-a"]
        with link.lock:
            link.last_poll_end = time.monotonic() - 100

    def test_restart_node(self):
        code, out, err = run(["hub", "restart-node", "pod-a"])
        self.assertEqual(code, 4, out + err)                 # offline: nothing queued
        a = FakeRelay(self.url, self.rtok, "pod-a")
        self.addCleanup(a.t.close)
        a.register()
        a.poll()
        code, out, err = run(["hub", "restart-node", "pod-a"])
        self.assertEqual(code, 0, err)
        self.assertIn("restart queued", out)
        self.assertEqual(a.poll().get("refresh"), "restart")
        self.assertEqual(run(["hub", "restart-node"])[0], 2)
        # an agent token cannot use the owner route
        _write_conn("agentconn", self.url, self.toks["agent"])
        code, out, err = run(["hub", "restart-node", "pod-a", "--connection", "agentconn"])
        self.assertEqual(code, 3, out + err)

    def test_restart_outdated_waits_and_reports(self):
        a = FakeRelay(self.url, self.rtok, "pod-a", info={"name": "pod-a", "version": "0.9.0", "os": "Linux",
                                                          "policy": {"preset": "standard"},
                                                          "modes": {"auto_update": True}})
        a.register()
        a.poll()
        clock = [0.0]

        def sleep(s):
            clock[0] += s
            a.poll()
        with mock.patch.object(cli, "_now", lambda: clock[0]), mock.patch.object(cli, "_sleep", sleep):
            code, out, err = run(["hub", "restart-node", "--outdated", "--config", self.cfgp])
        self.assertEqual(code, 4, out + err)
        self.assertIn("pod-a: restart queued", out)
        self.assertIn("still outdated or offline: pod-a", out)
        # a node without auto_update is listed for a manual upgrade
        a.info["modes"]["auto_update"] = False
        a.register()
        a.poll()
        with mock.patch.object(cli, "_now", lambda: clock[0]), mock.patch.object(cli, "_sleep", sleep):
            code, out, err = run(["hub", "restart-node", "--outdated", "--config", self.cfgp])
        self.assertEqual(code, 0, out + err)
        self.assertIn("upgrade by hand", out)

    def test_ack_node_prompt_and_bind(self):
        a = FakeRelay(self.url, self.rtok, "pod-a")
        a.register()
        self.go_offline()
        b = FakeRelay(self.url, self.rtok, "pod-a")
        b.register()
        self.assertTrue(self.hub.links["pod-a"].identity_changed)
        code, out, err = run(["hub", "ack-node", "pod-a"])           # no TTY, no --yes: nothing changes
        self.assertEqual(code, 2, out + err)
        self.assertIn("pending registration for 'pod-a': from 127.0.0.1", out)
        self.assertTrue(self.hub.links["pod-a"].identity_changed)
        code, out, err = run(["hub", "ack-node", "pod-a", "--yes"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("bound to the new instance", out)
        self.assertFalse(self.hub.links["pod-a"].identity_changed)
        code, out, err = run(["hub", "ack-node", "pod-a", "--yes"])
        self.assertIn("no pending registration", out)

    def test_hub_status_marks(self):
        a = FakeRelay(self.url, self.rtok, "pod-a", info={"name": "pod-a", "version": "0.9.0", "os": "Linux",
                                                          "policy": {"preset": "standard"},
                                                          "modes": {"auto_update": False}})
        a.register()
        a.poll()
        code, out, err = run(["hub", "status", "--config", self.cfgp])
        self.assertEqual(code, 0, err)
        self.assertIn("outdated (upgrade by hand)", out)


class TestHubProcess(_HubCli):
    """A real ``hive hub run`` process: status over hub-local, stop exits 0, restart of a foreground hub."""

    def start(self):
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        p = subprocess.Popen([sys.executable, "-m", "hive", "hub", "run"], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.PIPE, cwd=str(self.home))
        self.addCleanup(p.stderr.close)
        self.addCleanup(lambda: p.poll() is None and (p.kill() or p.wait()))
        helpers.wait_until(lambda: run(["hub", "status"])[1].find("running (version") >= 0 or p.poll() is not None,
                           30, 0.2)
        self.assertIsNone(p.poll(), p.stderr.read().decode() if p.poll() is not None else "")
        return p

    def test_stop_and_restart(self):
        port = helpers.free_port()
        cfgp = self.init_hub()
        config.edit_config(cfgp, lambda r: r["listen"].update(port=port) or r.update(allow_root=True), kind="hub")
        config._client_update(lambda c: c["connections"]["hub-local"].update(url=f"http://127.0.0.1:{port}"))
        p = self.start()
        code, out, err = run(["hub", "status"])
        self.assertIn("hub 'home' running", out)
        code, out, err = run(["hub", "stop"])
        self.assertEqual(code, 0, out + err)
        self.assertEqual(p.wait(30), 0)
        code, out, err = run(["hub", "status"])
        self.assertIn("not running", out)
        p = self.start()
        code, out, err = run(["hub", "restart"])
        self.assertEqual(code, 4, out + err)                 # a foreground hub: stopped, "start it again"
        self.assertIn("hive hub run", err)
        self.assertEqual(p.wait(30), 75)


class TestApprovalsCli(CliCase):
    def setUp(self):
        super().setUp()
        self.url, self.tok, self.rt = helpers.start_node_server(
            start_managers=True, agent_entry={"require_approval": ["shell.run"]})
        self.addCleanup(self.rt.test_stop)
        _write_conn("this-pc-owner", self.url, self.rt.test_owner_token)
        from hive.client import connect
        self.agent = connect(self.url, token=self.tok)
        self.addCleanup(self.agent.close)

    def request(self):
        with self.assertRaises(HiveError) as cm:
            self.agent.call("shell.run", {"command": "echo approved-$((1+1))"})
        self.assertEqual(cm.exception.code, "approval_required")
        return cm.exception.details["approval_id"]

    def test_list_approve_grant_revoke(self):
        aid = self.request()
        code, out, err = run(["approvals", "list"])
        self.assertEqual(code, 0, err)
        self.assertIn(aid, out)
        self.assertIn("shell.run", out)
        code, out, err = run(["approvals", "approve", aid])            # no TTY and no --yes: nothing decided
        self.assertEqual(code, 2)
        self.assertIn("echo approved-$((1+1))", out)                    # the review is printed before y/N
        code, out, err = run(["approvals", "approve", aid, "--yes", "--for", "30m"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("approved", out)
        r = self.agent.call("shell.run", {"command": "echo approved-$((1+1))"}, approval_id=aid)
        self.assertIn("approved-2", r.get("stdout") or r.get("output") or "")
        code, out, err = run(["approvals", "grants", "--json"])
        grants = json.loads(out)
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0]["action"], "shell.run")
        code, out, err = run(["approvals", "revoke", "--all"])
        self.assertEqual(code, 0, err)
        self.assertIn("revoked 1", out)
        aid2 = self.request()
        code, out, err = run(["approvals", "deny", aid2, "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("denied", out)
        self.assertEqual(run(["approvals", "approve", "ap_doesnotexist1"])[0], 1)
        self.assertEqual(run(["approvals", "approve", "nope"])[0], 2)

    def test_agent_cannot_decide(self):
        aid = self.request()
        with self.assertRaises(HiveError) as cm:
            self.agent.target.transport.request("POST", f"/v1/approvals/{aid}/approve", {})
        self.assertEqual(cm.exception.code, "forbidden")

    def test_daemon_down(self):
        _write_conn("this-pc-owner", _free_url(), self.rt.test_owner_token)
        code, out, err = run(["approvals", "grants"])
        self.assertIn("node not running", out)
        code, out, err = run(["approvals", "approve", "ap_abcdefgh"])
        self.assertEqual(code, 4)
        self.assertIn("node not running", err)
        code, out, err = run(["approvals", "list", "--hub"])
        self.assertEqual(code, 2)


class TestMcpConfig(CliCase):
    def setUp(self):
        super().setUp()
        self.url, self.tok, self.rt = helpers.start_node_server()
        self.addCleanup(self.rt.test_stop)
        _write_conn("this-pc", self.url, self.tok, default=True)
        _write_conn("this-pc-owner", self.url, self.rt.test_owner_token)
        self.bindir = Path(tempfile.mkdtemp(prefix="hivebin-"))
        self.addCleanup(shutil.rmtree, str(self.bindir), True)
        exe = self.bindir / ("hive.exe" if os.name == "nt" else "hive")
        exe.write_text("#!/bin/sh\n")
        exe.chmod(0o755)

    def env(self):
        return {"PATH": str(self.bindir) + os.pathsep + os.environ.get("PATH", "")}

    def test_local_agent_snippet_uses_console_script(self):
        with mock.patch.object(cli, "_console_command",
                               lambda: [str(self.bindir / "hive")]) if os.name == "nt" else mock.patch.dict({}):
            code, out, err = run(["mcp-config", "--local"], env=self.env())
        self.assertEqual(code, 0, err)
        data = json.loads(out[out.index("{"):out.rindex("}") + 1])
        srv = data["mcpServers"]["hive"]
        self.assertTrue(os.path.isabs(srv["command"]))
        self.assertNotIn("-m", srv["args"])
        self.assertEqual(srv["args"], ["mcp", "--connection", "this-pc"])
        self.assertNotIn(self.tok, out)

    def test_owner_refused_and_default_named(self):
        code, out, err = run(["mcp-config", "--connection", "this-pc-owner"], env=self.env())
        self.assertEqual(code, 3, out + err)
        self.assertIn("agent token", err)
        code, out, err = run(["mcp-config", "--connection", "this-pc-owner", "--allow-owner"], env=self.env())
        self.assertEqual(code, 0, err)
        code, out, err = run(["mcp-config", "--client", "claude-code"], env=self.env())
        self.assertEqual(code, 0, err)
        self.assertIn("default connection 'this-pc'", out)
        self.assertIn("claude mcp add hive -- ", out)
        self.assertIn("--wait-budget 110", out)
        self.assertEqual(run(["mcp-config", "--connection", "nope"], env=self.env())[0], 2)

    def test_write_cursor(self):
        with mock.patch.object(Path, "home", lambda: self.home):
            code, out, err = run(["mcp-config", "--local", "--client", "cursor", "--write"], env=self.env())
        self.assertEqual(code, 0, err)
        data = json.loads((self.home / ".cursor" / "mcp.json").read_text())
        self.assertEqual(data["mcpServers"]["hive"]["args"][-1], "this-pc")


class TestLocalTools(CliCase):
    def test_service_commands(self):
        from hive.install import InstallResult
        self.assertEqual(run(["service", "install", "node"])[0], 2)        # no config
        run(["init", "node", "--yes"])
        res = InstallResult(method="watchdog", files=["/x/watchdog.sh"], commands=[["sh", "/x/watchdog.sh"]],
                            notes=["dry run: nothing was written or started"])
        with mock.patch("hive.install.install", return_value=res) as m:
            code, out, err = run(["service", "install", "node", "--dry-run", "--method", "watchdog"])
        self.assertEqual(code, 0, err)
        self.assertEqual(m.call_args[1]["method"], "watchdog")
        self.assertTrue(m.call_args[1]["dry_run"])
        self.assertIn("file: /x/watchdog.sh", out)
        with mock.patch("hive.install.status", return_value={"kind": "node", "installed": False, "method": None,
                                                              "system": False, "files": [], "running": False,
                                                              "pid": None, "data_dir": "/d", "active": None}):
            code, out, err = run(["service", "status", "node"])
        self.assertIn("not installed", out)

    @unittest.skipUnless(shutil.which("openssl"), "openssl CLI needed to make a test certificate")
    def test_tls_pin(self):
        import ssl
        from hive.transport import cert_pin
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"),
                        "-out", str(d / "c.pem"), "-days", "2", "-subj", "/CN=hive-test"], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code, out, err = run(["tls-pin", str(d / "c.pem")])
        self.assertEqual(code, 0, err)
        der = ssl.PEM_cert_to_DER_cert((d / "c.pem").read_text())
        self.assertEqual(out.strip(), cert_pin(der))
        self.assertTrue(out.startswith("sha256:"))
        self.assertEqual(run(["tls-pin", str(d / "k.pem")])[0], 2)

    def test_doctor_exit_and_escaping(self):
        rows = [{"check": "config", "status": "ok", "detail": "fine\x1b[2J", "fix": None},
                {"check": "listen", "status": "fail", "detail": "port busy", "fix": "pick another"}]
        with mock.patch("hive.doctor.run", return_value=rows) as m:
            code, out, err = run(["doctor", "client", "--no-network"])
        self.assertEqual(code, 1)
        self.assertEqual(m.call_args[0][0], "client")
        self.assertFalse(m.call_args[1]["network"])
        self.assertNotIn("\x1b", out)
        self.assertIn("fix: pick another", out)
        with mock.patch("hive.doctor.run", return_value=rows[:1]):
            self.assertEqual(run(["doctor", "client"])[0], 0)

    def test_audit_tail(self):
        from hive.audit import AuditLog
        run(["init", "node", "--yes"])
        cfg = config.load_node_config()
        log = AuditLog(Path(cfg.data_dir) / "audit")
        for i in range(3):
            log.record(kind="call", identity=None, ip="127.0.0.1", via="direct", node="n", action=f"fs.read",
                       params={"path": f"/x{i}\x1b[31m"}, ok=True, code=None, ms=3, request_id=f"r_{i}")
        code, out, err = run(["audit", "tail", "-n", "2"])
        self.assertEqual(code, 0, err)
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertNotIn("\x1b", out)
        code, out, err = run(["audit", "tail", "--json", "-n", "5"])
        self.assertEqual(len(out.strip().splitlines()), 3)
        self.assertEqual(json.loads(out.splitlines()[-1])["rid"], "r_2")

    def test_dist_build(self):
        target = self.home / "hive.pyz"
        code, out, err = run(["dist", "build", "--out", str(target)])
        self.assertEqual(code, 0, err)
        import hashlib
        self.assertIn(hashlib.sha256(target.read_bytes()).hexdigest(), out)
        r = subprocess.run([sys.executable, str(target), "version"], stdout=subprocess.PIPE, timeout=60,
                           cwd=str(self.home))
        self.assertIn(b"hive 1.0.0", r.stdout)


class TestPushPullLogs(CliCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="hivepp-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.root), True)
        self.url, self.tok, self.rt = helpers.start_node_server(
            start_managers=True, workdir=str(self.root),
            policy={"fs": {"roots": [{"path": str(self.root), "mode": "rw"}]}})
        self.addCleanup(self.rt.test_stop)
        self.env = {"HIVE_URL": self.url, "HIVE_TOKEN": self.rt.test_owner_token}

    def test_push_pull_roundtrip(self):
        src = self.home / "data.bin"
        data = os.urandom(300000)
        src.write_bytes(data)
        remote = str(self.root / "up" / "data.bin")
        code, out, err = run(["push", "self", str(src), remote], env=self.env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(Path(remote).read_bytes(), data)
        self.assertEqual(run(["push", "self", str(src), remote], env=self.env)[0], 1)    # exists: conflict
        self.assertEqual(run(["push", "self", str(src), remote, "--overwrite"], env=self.env)[0], 0)
        back = self.home / "back.bin"
        code, out, err = run(["pull", "self", remote, str(back)], env=self.env)
        self.assertEqual(code, 0, out + err)
        self.assertEqual(back.read_bytes(), data)
        import hashlib
        self.assertIn(hashlib.sha256(data).hexdigest(), out)

    @unittest.skipIf(os.name == "nt", "POSIX shell syntax")
    def test_logs_job_and_follow(self):
        from hive.client import connect
        c = connect(self.url, token=self.rt.test_owner_token)
        self.addCleanup(c.close)
        job = c.call("job.start", {"command": "echo one; printf '\\033[31mred\\033[0m\\n'; sleep 1; echo two"})
        code, out, err = run(["logs", "self", job["job_id"], "-f"], env=self.env)
        self.assertEqual(code, 0, err)
        self.assertIn("one", out)
        self.assertIn("two", out)
        self.assertNotIn("\x1b", out)
        code, out, err = run(["logs", "self", job["job_id"], "--tail", "1"], env=self.env)
        self.assertEqual(out.strip(), "two")
        self.assertEqual(run(["logs", "self"], env=self.env)[0], 2)


if __name__ == "__main__":
    unittest.main()
