"""WP1 review fixes: live secret reload for masking, error masking flag, hub ports in own_ports, fail-closed empty
lists, FileHolds on subtree/rebase writes, allowlist '..' argv[0], kill-guard owner fallback, relay plaintext rule,
explicit tailnet IP binds, Tailscale fallback only on WSL, unauthenticated bodies, CORS preflight bodies."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import socket
import unittest
from pathlib import Path
from unittest import mock

from hive import config, httpserver, util
from hive.actions import HANDLERS
from hive.approvals import FileHolds, file_fingerprint
from hive.auth import identity_from_entry
from hive.config import ListenConfig
from hive.errors import HiveError, bad_request
from hive.httpserver import ApiServer
from hive.secretstore import SecretStore
from tests import helpers
from tests.test_config import SHIPPED
from tests.test_httpserver import Server
from tests.wp1util import FILES, OPERATOR, FakeHome, build_policy

SECRET = "hf_abcdefghijklmnopqrstuvwxyz0123"


class _FH(unittest.TestCase):
    def setUp(self) -> None:
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)

    def assertRule(self, fn, code: str, rule=None):  # noqa: N802
        with self.assertRaises(HiveError) as cm:
            fn()
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        if rule is not None:
            self.assertEqual(cm.exception.details.get("rule"), rule, cm.exception.message)
        return cm.exception


# --------------------------------------------------------------------------------------------- secrets / masking
class TestSecretReload(_FH):
    def test_has_values_sees_cli_set_at_once(self):
        path = self.fh.data / "secrets.json"
        daemon = SecretStore(path, passthrough=(), env={})
        self.assertFalse(daemon.has_values)
        daemon.mask("warm the 1 s reload throttle")
        SecretStore(path, passthrough=(), env={}).set("HF_TOKEN", SECRET)   # the CLI, another process
        self.assertTrue(daemon.has_values)


class TestRuntimeMasking(_FH):
    def setUp(self) -> None:
        super().setUp()
        self.rt = helpers.make_runtime(start_managers=False)
        self.addCleanup(self.rt.stop)

    def handler(self, name: str, fn) -> None:
        p = mock.patch.dict(HANDLERS, {name: fn})
        p.start()
        self.addCleanup(p.stop)

    def test_secret_set_while_running_is_masked_on_next_call(self):
        self.handler("fs.read", lambda ctx, p: {"text": "key=" + SECRET})
        self.assertNotIn("masked", self.rt.call(OPERATOR, "fs.read", {"path": "~/x"})["result"])
        SecretStore(self.rt.data_dir / "secrets.json", passthrough=(), env={}).set("OPENAI_API_KEY", SECRET)
        with mock.patch.object(self.rt.audit, "record", lambda **kw: None):   # audit off: nothing else reloads
            env = self.rt.call(OPERATOR, "fs.read", {"path": "~/x"})
        self.assertEqual(env["result"]["text"], "key=«secret:OPENAI_API_KEY»")
        self.assertIs(env["meta"]["masked"], True)

    def test_error_masking_sets_meta_flag(self):
        self.rt.secrets.set("HF_TOKEN", SECRET)

        def boom(ctx, p):
            raise bad_request("bad " + SECRET, token=SECRET)
        self.handler("fs.read", boom)
        env = self.rt.call(OPERATOR, "fs.read", {"path": "~/x"})
        self.assertFalse(env["ok"])
        self.assertEqual(env["error"]["message"], "bad «secret:HF_TOKEN»")
        self.assertNotIn(SECRET, json.dumps(env))
        self.assertIs(env["meta"]["masked"], True)
        self.handler("fs.read", lambda ctx, p: (_ for _ in ()).throw(bad_request("plain")))
        self.assertNotIn("masked", self.rt.call(OPERATOR, "fs.read", {"path": "~/x"})["meta"])

    def test_hub_ports_are_own_ports(self):
        self.rt.stop()
        from hive import config as cfgmod
        hub_raw = {"listen": {"port": 7799, "proxy_port": 7798}}
        hub_cfg = mock.Mock()
        hub_cfg.listen.port, hub_cfg.listen.proxy_port = hub_raw["listen"]["port"], hub_raw["listen"]["proxy_port"]
        seen = {}
        real_policy = __import__("hive.policy", fromlist=["Policy"]).Policy

        def spy(*a, **kw):
            seen["own_ports"] = list(kw.get("own_ports") or ())
            return real_policy(*a, **kw)
        paths = dict(cfgmod.default_paths())
        hub_path = Path(self.fh.base / "hubcfg.json")
        hub_path.write_text("{}", encoding="utf-8")
        paths["hub_config"] = hub_path
        with mock.patch.object(cfgmod, "default_paths", return_value=paths), \
                mock.patch.object(cfgmod, "load_hub_config", return_value=hub_cfg), \
                mock.patch.object(cfgmod, "protected_paths", return_value=([], [])), \
                mock.patch("hive.policy.Policy", side_effect=spy):
            self.rt = helpers.make_runtime(start_managers=False)
        self.addCleanup(self.rt.stop)
        self.assertIn(7799, seen["own_ports"])
        self.assertIn(7798, seen["own_ports"])


# --------------------------------------------------------------------------------------------- empty lists
class TestEmptyListsFailClosed(_FH):
    def test_empty_classes(self):
        self.assertEqual(identity_from_entry({"kind": "agent", "name": "a", "classes": []}, "agent").classes,
                         frozenset())
        self.assertEqual(identity_from_entry({"kind": "hub", "name": "h", "classes": []}, "hub").classes, frozenset())
        self.assertEqual(len(identity_from_entry({"kind": "agent", "name": "a"}, "agent").classes), 5)

    def test_empty_allow_actions(self):
        from hive import catalog
        pol = build_policy(self.fh, policy={"allow_actions": []})
        self.assertEqual(pol.allow_actions, ())
        e = pol.effective(OPERATOR, paused=False)
        with self.assertRaises(HiveError):
            e.check_action(catalog.SPECS["shell.run"])
        with self.assertRaises(HiveError):
            e.check_action(catalog.SPECS["fs.read"])

    def test_empty_roots(self):
        err = self.assertRule(lambda: build_policy(self.fh, policy={"fs": {"roots": []}}), "bad_request")
        self.assertIn("roots", err.message)


# --------------------------------------------------------------------------------------------- holds
class TestHoldsSubtree(_FH):
    def setUp(self) -> None:
        super().setUp()
        self.script = self.fh.touch("proj/run.sh", "echo hi")
        self.fh.touch("evil/run.sh", "rm -rf ~")
        self.fh.touch("other/a.txt", "x")
        self.holds = FileHolds()
        self.pol = build_policy(self.fh)
        self.e = self.pol.effective(FILES, paused=False, holds=self.holds)
        self.hid = self.holds.acquire({str(self.script): file_fingerprint(str(self.script))}, policy=self.e)

    def test_copy_over_held_file(self):
        self.assertTrue(self.e.denied_under(self.fh.p("evil"), write=True, rebase=self.fh.p("proj")))
        # copying the held script elsewhere only reads it
        self.assertEqual(self.e.denied_under(self.fh.p("proj"), write=True, rebase=self.fh.p("copy")), [])

    def test_move_or_delete_parent_of_held_file(self):
        self.assertRule(lambda: self.e.resolve_path(str(self.fh.p("proj")), write=True, subtree=True), "conflict",
                        "approved_input")
        self.assertRule(lambda: self.e.resolve_path(str(self.fh.home), write=True, subtree=True), "conflict",
                        "approved_input")
        self.e.resolve_path(str(self.fh.p("other")), write=True, subtree=True)

    def test_ungated_and_released(self):
        op = self.pol.effective(OPERATOR, paused=False, holds=self.holds)
        op.resolve_path(str(self.fh.p("proj")), write=True, subtree=True)
        self.holds.release(self.hid)
        self.e.resolve_path(str(self.fh.p("proj")), write=True, subtree=True)
        self.assertEqual(self.e.denied_under(self.fh.p("evil"), write=True, rebase=self.fh.p("proj")), [])


# --------------------------------------------------------------------------------------------- exec / kill
@unittest.skipIf(os.name == "nt", "POSIX paths")
class TestAllowlistDotDot(_FH):
    def test_dotdot_argv0_refused(self):
        pol = build_policy(self.fh, policy={"exec": {"mode": "allowlist", "command_allow": ["/usr/lib/.*"]}})
        e = pol.effective(OPERATOR, paused=False)
        self.assertRule(lambda: e.check_command(["/usr/lib/../../bin/sh", "-c", "id"], "bash"), "policy_denied",
                        "command_allow")


class TestKillOwner(_FH):
    def setUp(self) -> None:
        super().setUp()
        self.e = build_policy(self.fh).effective(OPERATOR, paused=False)

    def test_uid_none_falls_back_to_user(self):
        from hive import policy as policy_mod
        me = policy_mod._current_user()
        if not me:
            self.skipTest("no account name for this uid")
        self.assertEqual(self.e.check_kill(9999, {"name": "x", "uid": None, "user": me}, protected={}), {})
        self.assertRule(lambda: self.e.check_kill(9999, {"name": "x", "uid": None, "user": me + "zz"}, protected={}),
                        "policy_denied", "other_user")
        self.assertRule(lambda: self.e.check_kill(9999, {"name": "x", "user": "DOMAIN\\" + me + "zz"},
                                                  protected={}), "policy_denied", "other_user")

    def test_owner_unknown_is_allowed_and_flagged(self):
        self.assertEqual(self.e.check_kill(9999, {"name": "x", "uid": None, "user": None}, protected={}),
                         {"owner_unknown": True})
        if hasattr(os, "getuid"):
            self.assertEqual(self.e.check_kill(9999, {"name": "x", "uid": os.getuid()}, protected={}), {})

    def test_windows_user_compare(self):
        from hive import policy as policy_mod
        with mock.patch.object(policy_mod, "IS_WINDOWS", True), \
                mock.patch.object(policy_mod, "_current_user", return_value="Alice"):
            self.assertEqual(self.e.check_kill(9999, {"name": "x", "uid": 5, "user": "DESK\\alice"}, protected={}),
                             {})
            self.assertRule(lambda: self.e.check_kill(9999, {"name": "x", "user": "NT AUTHORITY\\SYSTEM"},
                                                      protected={}), "policy_denied", "other_user")
            self.assertEqual(self.e.check_kill(9999, {"name": "x"}, protected={}), {"owner_unknown": True})


# --------------------------------------------------------------------------------------------- config startup checks
class TestStartupChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.ctx = helpers.tmp_home()
        self.hh = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        p = mock.patch.object(config, "is_privileged", return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def cfg(self, **listen):
        raw = json.loads(json.dumps(SHIPPED))
        raw["listen"].update(listen)
        path = self.hh / "node" / "config.json"
        util.write_json_atomic(path, raw, private=True)
        return config.load_node_config(str(path))

    def relay(self, url: str, insecure: bool = True):
        c = self.cfg()
        c.relay = dataclasses.replace(c.relay, enabled=True, hub_url=url, allow_insecure=insecure)
        return c

    def check(self, c):
        return config.startup_safety_checks(c, "node")

    def test_relay_plaintext_public_name_refused(self):
        public = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        with mock.patch("socket.getaddrinfo", return_value=public):
            with self.assertRaises(HiveError) as cm:
                self.check(self.relay("http://hub.example.com:7700"))
        self.assertEqual(cm.exception.code, "forbidden")
        with self.assertRaises(HiveError):
            self.check(self.relay("http://8.8.8.8:7700"))
        with self.assertRaises(HiveError):
            self.check(self.relay("http://10.0.0.5:7700", insecure=False))

    def test_relay_plaintext_private_ok(self):
        self.check(self.relay("http://10.0.0.5:7700"))
        self.check(self.relay("http://100.101.1.5:7700"))
        self.check(self.relay("http://desk.tail1.ts.net:7700"))
        self.check(self.relay("http://127.0.0.1:7700", insecure=False))
        private = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.20", 0))]
        with mock.patch("socket.getaddrinfo", return_value=private):
            self.check(self.relay("http://home-hub:7700"))

    def test_explicit_tailnet_ip_plaintext_allowed(self):
        self.check(self.cfg(host="100.101.1.5"))
        with self.assertRaises(HiveError):
            self.check(self.cfg(host="192.168.1.5"))


# --------------------------------------------------------------------------------------------- httpserver
class TestTailscaleFallback(unittest.TestCase):
    def test_no_cli_lone_cgnat_address_outside_wsl_refused(self):
        with mock.patch("shutil.which", return_value=None), mock.patch("os.path.exists", return_value=False), \
                mock.patch.object(httpserver, "_running_in_wsl", return_value=False), \
                mock.patch.object(httpserver, "_local_ipv4s", return_value=["100.80.1.2"]):
            with self.assertRaises(HiveError) as cm:
                httpserver.tailscale_info()
        self.assertIn("tailscale up", cm.exception.hint)

    def test_cli_down_refused_even_on_wsl(self):
        with mock.patch("shutil.which", return_value="/usr/bin/tailscale"), \
                mock.patch.object(httpserver, "_run", return_value=(1, "")), \
                mock.patch.object(httpserver, "_running_in_wsl", return_value=True), \
                mock.patch.object(httpserver, "_local_ipv4s", return_value=["100.80.1.2"]):
            with self.assertRaises(HiveError):
                httpserver.tailscale_info()


def _api(host: str) -> ApiServer:
    from tests.test_httpserver import relay_store, store
    return ApiServer(ListenConfig(host=host, port=0), role="node", tokens=store, relay_tokens=relay_store, throttle=None,
                     logger=logging.getLogger("hive.test.http"), audit=mock.Mock())


class TestExplicitTailnetIp(unittest.TestCase):
    def test_matching_ip_binds_as_tailnet(self):
        api = _api("100.101.1.5")
        with mock.patch.object(httpserver, "tailscale_info", return_value=("100.101.1.5", ["d.t.ts.net", "d"], "")):
            plan = api._bind_plan()
        self.assertEqual(plan, [("100.101.1.5", "tailnet"), ("127.0.0.1", "main")])
        self.assertEqual((api.tailnet_ip, api.tailnet_names), ("100.101.1.5", ["d.t.ts.net", "d"]))

    def test_mismatch_or_no_tailscale_refused(self):
        with mock.patch.object(httpserver, "tailscale_info", return_value=("100.101.1.6", [], "")):
            with self.assertRaises(HiveError) as cm:
                _api("100.101.1.5")._bind_plan()
        self.assertIn("not this machine's tailnet address", cm.exception.message)
        with mock.patch.object(httpserver, "tailscale_info",
                               side_effect=HiveError("unavailable", "Tailscale is not available", hint="x")):
            with self.assertRaises(HiveError):
                _api("100.101.1.5").start()


class TestBodies(unittest.TestCase):
    def setUp(self) -> None:
        self.s = Server()
        self.addCleanup(self.s.stop)

    def _all(self, data: bytes) -> bytes:
        c = socket.create_connection(("127.0.0.1", self.s.port), timeout=5)
        try:
            c.sendall(data)
            out = b""
            while True:
                try:
                    chunk = c.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            c.close()

    def test_unauthenticated_body_not_read(self):
        out = self._all(b"GET /v1/open HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Length: 2097152\r\n\r\n")
        self.assertTrue(out.startswith(b"HTTP/1.1 413"), out[:200])
        self.assertEqual(out.count(b"HTTP/1.1 "), 1)

    def test_preflight_body_not_smuggled(self):
        inner = b"GET /v1/open HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n"
        out = self._all(b"OPTIONS /openai/n/s/x HTTP/1.1\r\nHost: 127.0.0.1\r\nOrigin: http://app\r\n"
                        b"Access-Control-Request-Method: POST\r\nContent-Length: " + str(len(inner)).encode() +
                        b"\r\n\r\n" + inner)
        self.assertTrue(out.startswith(b"HTTP/1.1 204"), out[:200])
        self.assertEqual(out.count(b"HTTP/1.1 "), 1)


if __name__ == "__main__":
    unittest.main()
