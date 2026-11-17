"""WP1 config tests (§21.2 httpserver/node core row, config parts): strict loading, comments, placeholders, presets,
env-only mode, init_* and edit_config, protected paths, startup safety checks."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hive import config, dist, policy, util
from hive.errors import HiveError
from tests import helpers

SHIPPED = {
    "version": 1, "name": "desk-4090", "node_id": "n_18f3a2c1d4e5a6b7", "description": "Home desktop",
    "tags": ["home", "gpu"], "consent": {"by": "alice@DESK", "at": "2026-11-17T10:00:00.000Z", "text": "ok"},
    "allow_root": False, "workdir": "~", "data_dir": None,
    "listen": {"enabled": True, "host": "127.0.0.1", "port": 7701,
               "_host": "127.0.0.1 | \"tailscale\" | an IP", "proxy_port": None, "_proxy_port": "loopback listener",
               "tls": {"cert": None, "key": None}, "allow_insecure_remote": False, "allowed_hosts": [],
               "allowed_origins": [], "trusted_proxies": [], "max_connections": 64, "max_body_bytes": 50331648},
    "relay": {"enabled": False, "hub_url": None, "token": None, "token_file": None, "tls_pin": None, "ca_file": None,
              "allow_insecure": False, "max_parallel": 8, "proxy": "auto", "role": "operator", "deny_actions": [],
              "auto_update": False},
    "auth": {"tokens": [{"name": "owner", "sha256": util.hash_token("hv_ownerownerowner"), "kind": "owner",
                         "note": "local CLI"},
                        {"name": "chatbox", "sha256": util.hash_token("hv_chatchatchat"), "kind": "models",
                         "models": ["qwen"], "expires": None, "_note": "nested comment inside a list item"}],
             "throttle": {"max_failures": 10, "window": 60, "block": 300}},
    "gateway": {"enabled": True, "cors": True, "max_body_bytes": 8388608, "max_response_bytes": 67108864,
                "timeout": 1800, "idle_timeout": 300, "max_inflight_per_token": 4},
    "network": {"proxy": None, "_proxy": "null = env…", "no_proxy": [], "use_system_proxy": True,
                "proxy_children": True, "strict_dns": False, "ca_file": None},
    "models": {"dir": None, "_dir": "…", "hf_endpoint": None, "_hf": "…", "modelscope_endpoint": "https://www.modelscope.cn",
               "max_concurrent_pulls": 2},
    "secrets": {"env_passthrough": ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MODELSCOPE_API_TOKEN"]},
    "wsl": {"keepalive": []},
    "policy": {"preset": "standard", "read_only": False, "allow_actions": ["*"], "deny_actions": [],
               "fs": {"roots": [{"path": "~", "mode": "rw"}], "deny": ["<DEFAULT_SECRET_PATHS>"],
                      "deny_write": ["<DEFAULT_EXEC_PATHS>"], "max_read_bytes": 8388608,
                      "max_write_bytes": 16777216, "max_chunk_bytes": 8388608, "max_delete_entries": 10000},
               "approvals": {"ttl": 600, "max_pending": 50, "decide_from": ["loopback"], "max_grant_minutes": 240}},
    "model_endpoints": [{"name": "lmstudio", "base_url": "http://127.0.0.1:1234/v1", "api_key_env": None}],
    "audit": {"enabled": True, "retention_days": 30, "required": False},
    "log": {"level": "info", "file": "logs/node.log", "max_mb": 10, "keep": 5},
}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.home_ctx = helpers.tmp_home()
        self.hh = self.home_ctx.__enter__()
        self.addCleanup(self.home_ctx.__exit__, None, None, None)

    def write(self, raw: dict, name: str = "config.json") -> Path:
        p = self.hh / "node" / name
        util.write_json_atomic(p, raw, private=True)
        return p

    def err(self, fn):
        with self.assertRaises(HiveError) as cm:
            fn()
        return cm.exception


class TestLoad(Base):
    def test_shipped_config_with_comments(self):
        p = self.write(SHIPPED)
        cfg = config.load_node_config(str(p))
        self.assertEqual(cfg.name, "desk-4090")
        self.assertEqual(cfg.data_dir, str(p.parent.resolve()))
        self.assertIsInstance(cfg.listen, config.ListenConfig)
        self.assertEqual(cfg.auth.tokens[1].models, ["qwen"])
        self.assertIn("~/.ssh/**", cfg.policy.fs["deny"])                 # placeholder expanded
        self.assertNotIn("<DEFAULT_SECRET_PATHS>", cfg.policy.fs["deny"])
        self.assertIn(policy.DEFAULT_COMMAND_DENY[0], cfg.policy.exec["command_deny"])   # preset default merged
        self.assertEqual(cfg.policy.exec["child_env_defaults"]["PYTHONUNBUFFERED"], "1")
        self.assertEqual(cfg.model_endpoints[0]["name"], "lmstudio")

    def test_unknown_key_did_you_mean(self):
        raw = json.loads(json.dumps(SHIPPED))
        raw["listen"]["hots"] = "x"
        e = self.err(lambda: config.load_node_config(str(self.write(raw))))
        self.assertEqual(e.code, "bad_request")
        self.assertIn("did you mean 'host'", e.message)
        raw2 = json.loads(json.dumps(SHIPPED))
        raw2["polcy"] = {}
        self.assertIn("polcy", self.err(lambda: config.load_node_config(str(self.write(raw2)))).message)

    def test_cross_field_checks(self):
        for mut in (lambda r: r["listen"]["tls"].update(cert="c.pem"),
                    lambda r: r["relay"].update(enabled=True),
                    lambda r: r["auth"]["tokens"].append(dict(r["auth"]["tokens"][0])),
                    lambda r: r["auth"]["tokens"][0].update(require_approval=["shell.run"]),
                    lambda r: r["policy"].update(exec={"command_deny": ["("]}),
                    lambda r: r.update(version=2)):
            raw = json.loads(json.dumps(SHIPPED))
            mut(raw)
            with self.subTest(raw=str(mut)):
                self.assertEqual(self.err(lambda: config.load_node_config(str(self.write(raw)))).code, "bad_request")

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_world_writable_refused(self):
        p = self.write(SHIPPED)
        os.chmod(p, 0o666)
        self.assertEqual(self.err(lambda: config.load_node_config(str(p))).code, "forbidden")
        os.chmod(p, 0o644)
        config.load_node_config(str(p))
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_presets(self):
        for preset, check in (
                ("readonly", lambda c: c.policy.read_only and not c.policy.exec["enabled"]
                 and c.policy.fs["roots"] == [{"path": "~", "mode": "ro"}] and "net.*" in c.policy.deny_actions),
                ("cautious", lambda c: c.policy.net["http_allow"] == ["loopback"]
                 and "*.aliyuncs.com" in c.policy.net["download_redirect_allow"]
                 and "huggingface.co" in c.policy.net["download_allow"]),
                ("full", lambda c: c.policy.fs["roots"] == [{"path": "*", "mode": "rw"}] and c.policy.fs["deny"] == []
                 and c.policy.exec["command_deny"] == [] and "private" in c.policy.net["http_allow"])):
            with self.subTest(preset=preset):
                raw = helpers.make_node_config(preset, data_dir=str(self.hh / "node"))
                self.assertTrue(check(config._parse_node(raw, self.hh / "node" / "config.json")))

    def test_client_map_validation(self):
        p = self.hh / "client.json"
        util.write_json_atomic(p, {"version": 1, "default": "home",
                                   "connections": {"home": {"url": "http://127.0.0.1:7700", "token_file": "x"}}})
        c = config.load_client_config(str(p))
        self.assertEqual(c.connections["home"].timeout, 120)
        util.write_json_atomic(p, {"version": 1, "connections": {"home": {"url": "http://x", "timeout": "slow"}}})
        e = self.err(lambda: config.load_client_config(str(p)))
        self.assertIn("connections.home.timeout", e.message)
        self.assertEqual(config.load_client_config(str(self.hh / "missing.json")).connections, {})

    def test_edit_config_keeps_comments(self):
        p = self.write(SHIPPED)
        config.edit_config(p, lambda r: r.update(description="changed"), kind="node")
        raw = json.loads(p.read_text())
        self.assertEqual(raw["description"], "changed")
        self.assertIn("_host", raw["listen"])
        self.assertEqual(raw["policy"]["fs"]["deny"], ["<DEFAULT_SECRET_PATHS>"])
        with self.assertRaises(HiveError):
            config.edit_config(p, lambda r: r.update(bogus=1), kind="node")
        self.assertNotIn("bogus", json.loads(p.read_text()))

    def test_hub_config(self):
        raw = {"version": 1, "name": "home", "consent": {"by": "a"},
               "listen": {"host": "tailscale", "port": 7700, "mcp_profile": "full"},
               "tokens": [{"name": "owner", "sha256": util.hash_token("hv_xxxxxxxxxx"), "kind": "owner"}],
               "nodes": [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token("hv_yyyyyyyyyy")},
                         {"name": "desk", "mode": "direct", "url": "http://127.0.0.1:7701", "token": "hv_z"}]}
        p = self.hh / "hub" / "config.json"
        util.write_json_atomic(p, raw, private=True)
        h = config.load_hub_config(str(p))
        self.assertEqual((h.listen.mcp_profile, h.relay.poll_wait, len(h.nodes)), ("full", 25, 2))
        raw["listen"]["enabled"] = True                      # a node-only key
        util.write_json_atomic(p, raw, private=True)
        self.assertEqual(self.err(lambda: config.load_hub_config(str(p))).code, "bad_request")
        raw["listen"].pop("enabled")
        raw["tokens"].append({"name": "hubtok", "sha256": util.hash_token("hv_wwwwwwwwww"), "kind": "hub"})
        util.write_json_atomic(p, raw, private=True)
        self.assertEqual(self.err(lambda: config.load_hub_config(str(p))).code, "bad_request")


class TestEnvOnly(Base):
    def test_env_only_join(self):
        code = dist.encode_join({"u": "http://100.101.1.5:7700", "t": "hv_relaytokenrelaytoken", "p": None,
                                 "n": "pod-a"})
        env = {"HIVE_HOME": str(self.hh), "HIVE_JOIN": code, "HIVE_WATCHDOG": "1", "HIVE_PROXY": "http://p:1",
               "HIVE_RELAY_ROLE": "operator"}
        cfg = config.load_node_config(None, env=env)
        self.assertTrue(getattr(cfg, "_env_only"))
        self.assertNotIn("HIVE_JOIN", env)
        self.assertEqual((cfg.name, cfg.relay.enabled, cfg.relay.allow_insecure, cfg.relay.auto_update,
                          cfg.listen.enabled, cfg.network.proxy),
                         ("pod-a", True, True, True, False, "http://p:1"))
        self.assertEqual(cfg.consent["by"], "env")
        self.assertEqual(cfg.data_dir, str((self.hh / "node").resolve()))
        node_id = (self.hh / "node" / "node_id").read_text().strip()
        self.assertEqual(cfg.node_id, node_id)
        cfg2 = config.load_node_config(None, env={"HIVE_HOME": str(self.hh), "HIVE_RELAY_URL": "https://hub.example",
                                                   "HIVE_RELAY_TOKEN": "hv_abcdefghijk", "HIVE_RELAY_ROLE": "files"})
        self.assertEqual((cfg2.node_id, cfg2.relay.allow_insecure, cfg2.relay.auto_update, cfg2.relay.role),
                         (node_id, False, False, "files"))
        with mock.patch.object(config, "is_privileged", return_value=False):
            config.startup_safety_checks(cfg, "node")

    def test_no_config(self):
        self.assertEqual(self.err(lambda: config.load_node_config(None, env={"HIVE_HOME": str(self.hh)})).code,
                         "not_found")

    def test_default_paths_workspace(self):
        ws = Path(tempfile.mkdtemp(prefix="hivews-"))
        self.addCleanup(shutil.rmtree, ws, True)
        (ws / ".hive").mkdir()
        fake_home = Path(tempfile.mkdtemp(prefix="hivefh-"))
        self.addCleanup(shutil.rmtree, fake_home, True)
        with mock.patch.object(config, "WORKSPACE", ws), mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            env = {k: v for k, v in os.environ.items() if k != "HIVE_HOME"}
            self.assertEqual(config.default_paths(env)["home"], ws / ".hive")
            (fake_home / ".hive").mkdir()
            self.assertEqual(config.default_paths(env)["home"], fake_home / ".hive")
        self.assertEqual(config.default_paths()["node_config"], self.hh / "node" / "config.json")


class TestInit(Base):
    def test_init_node_fresh_and_edit(self):
        cfg, toks = config.init_node(consent_by="me@host", preset="cautious", roots=["/data:ro"])
        self.assertTrue(toks["owner"].startswith("hv_") and toks["agent"].startswith("hv_"))
        paths = config.default_paths()
        self.assertTrue(paths["node_config"].exists())
        raw = json.loads(paths["node_config"].read_text())
        self.assertNotIn(toks["owner"], json.dumps(raw))
        agent = [t for t in cfg.auth.tokens if t.name == "this-pc"][0]
        self.assertEqual((agent.kind, agent.role), ("agent", "operator"))
        self.assertIn("shell.run", agent.require_approval)
        self.assertIn({"path": "/data", "mode": "ro"}, cfg.policy.fs["roots"])
        client = config.load_client_config()
        self.assertEqual(client.default, "this-pc")
        self.assertEqual(Path(client.connections["this-pc"].token_file).read_text().strip(), toks["agent"])
        self.assertEqual(Path(client.connections["this-pc-owner"].token_file).read_text().strip(), toks["owner"])
        # existing config, no changeable flag -> refused; a listen flag edits only listen
        self.assertEqual(self.err(lambda: config.init_node(consent_by="me")).code, "bad_request")
        cfg2, toks2 = config.init_node(consent_by="me", topology="tailscale", wsl_keepalive=["Ubuntu"])
        self.assertEqual((toks2, cfg2.listen.host, cfg2.wsl.keepalive[0]["distro"]), ({}, "tailscale", "Ubuntu"))
        self.assertEqual([t.name for t in cfg2.auth.tokens], ["owner", "this-pc"])

    def test_join_on_existing_wsl_config(self):
        cfg, _ = config.init_node(consent_by="me", wsl=True)
        self.assertEqual(cfg.listen.port, 7702)
        self.assertIn("wsl", cfg.tags)
        code = dist.encode_join({"u": "http://100.64.0.7:7700", "t": "hv_relayrelayrelay", "p": None,
                                 "n": "desk-wsl"})
        cfg2, toks = config.init_node(consent_by="me", join=code)
        self.assertEqual(toks, {})
        self.assertEqual((cfg2.name, cfg2.relay.enabled, cfg2.relay.allow_insecure, cfg2.listen.port, cfg2.tags),
                         ("desk-wsl", True, True, 7702, cfg.tags))

    def test_lan_and_pod_and_public_http(self):
        self.assertEqual(self.err(lambda: config.init_node(consent_by="me", topology="lan")).code, "bad_request")
        self.assertEqual(self.err(lambda: config.init_node(consent_by="me", relay_url="http://8.8.8.8:7700",
                                                           relay_token="hv_xxxxxxxxxxxx")).code, "bad_request")

    def test_init_hub_and_client(self):
        config.init_node(consent_by="me")
        hub, toks = config.init_hub(consent_by="me", local_node=True, name="home")
        self.assertEqual(hub.nodes[0].url, "http://127.0.0.1:7701")
        node_cfg = config.load_node_config()
        self.assertIn("hub-home", [t.name for t in node_cfg.auth.tokens if t.kind == "hub"])
        self.assertEqual(config.load_client_config().connections["hub-local"].url, "http://127.0.0.1:7700")
        c = config.init_client(url="https://hub.example:7700", token="hv_abcdefghijkl", name="remote",
                               make_default=True)
        self.assertEqual(c.default, "remote")
        self.assertEqual(Path(c.connections["remote"].token_file).read_text().strip(), "hv_abcdefghijkl")


class TestProtectedAndSafety(Base):
    def test_protected_paths(self):
        p = self.write(dict(SHIPPED, relay=dict(SHIPPED["relay"], token_file="/etc/relay.tok"),
                            listen=dict(SHIPPED["listen"], tls={"cert": "/c/cert.pem", "key": "/c/key.pem"},
                                        host="127.0.0.1")))
        cfg = config.load_node_config(str(p))
        code_dir = self.hh / "hub" / "run"
        code_dir.mkdir(parents=True)
        util.write_json_atomic(code_dir / "hub-code.json", {"v": 1, "package_dir": "/opt/hub/hive",
                                                            "executable": "/opt/hub/bin/python",
                                                            "prefixes": ["/opt/hub"], "sys_path": ["/opt/hub/lib"]})
        deny_all, deny_write = config.protected_paths(cfg)
        for want in (p, Path("/etc/relay.tok"), Path("/c/key.pem"), config.default_paths()["tokens"]):
            self.assertIn(want, deny_all)
        self.assertEqual(set(map(str, deny_write)), {"/opt/hub/hive", "/opt/hub/bin/python", "/opt/hub",
                                                     "/opt/hub/lib"})

    def test_startup_refusals(self):
        base = json.loads(json.dumps(SHIPPED))
        base["allow_root"] = True
        cfg = config.load_node_config(str(self.write(base)))
        config.startup_safety_checks(cfg, "node")
        for mut, code in ((lambda r: r.pop("consent"), "forbidden"),
                          (lambda r: r["listen"].update(host="0.0.0.0"), "forbidden"),          # no TLS
                          (lambda r: (r["listen"].update(host="0.0.0.0", allow_insecure_remote=True),
                                      r["auth"].update(tokens=[])), "forbidden")):              # no tokens
            raw = json.loads(json.dumps(base))
            mut(raw)
            with self.subTest(mut=str(mut)):
                c = config.load_node_config(str(self.write(raw)))
                self.assertEqual(self.err(lambda: config.startup_safety_checks(c, "node")).code, code)
        raw = json.loads(json.dumps(base))
        raw["listen"].update(host="0.0.0.0", allow_insecure_remote=True)
        warns = config.startup_safety_checks(config.load_node_config(str(self.write(raw))), "node")
        self.assertTrue(any("PLAINTEXT" in w for w in warns))
        raw = json.loads(json.dumps(base))
        raw["listen"]["host"] = "tailscale"
        config.startup_safety_checks(config.load_node_config(str(self.write(raw))), "node")
        with mock.patch.object(config, "is_privileged", return_value=True):
            raw = json.loads(json.dumps(SHIPPED))
            c = config.load_node_config(str(self.write(raw)))
            self.assertEqual(self.err(lambda: config.startup_safety_checks(c, "node")).code, "forbidden")


if __name__ == "__main__":
    unittest.main()
