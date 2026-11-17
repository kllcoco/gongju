"""WP1 invariants suite (§5.1 item 6): Hive internals are unreachable through PathGuard in every preset, mutating
actions are denied for readonly identities / read-only nodes / paused nodes, tokens and secret values never reach
audit, errors or results, gateway tokens cannot use /v1/call."""
from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from hive import catalog, config, util
from hive.actions import HANDLERS
from hive.errors import HiveError
from tests import helpers
from tests.wp1util import FILES, OPERATOR, READONLY, FakeHome, build_policy, http, ident, node_server

PRESETS = ("readonly", "standard", "cautious", "full")
SECRET = "hf_invariantsecretvalue0123456789"


class TestInternalsUnreachable(unittest.TestCase):
    def test_every_preset_and_identity(self):
        with FakeHome() as fh:
            tokdir = config.default_paths()["tokens"]
            client = config.default_paths()["client_config"]
            targets = [fh.hive_home / "node" / "config.json", fh.hive_home / "node" / "secrets.json",
                       fh.hive_home / "node" / "approvals.json", fh.hive_home / "node" / "audit" / "a.jsonl",
                       tokdir / "this-pc-owner.token", client, fh.p("proj/.hive/x"), fh.p(".hive")]
            for preset in PRESETS:
                pol = build_policy(fh, preset)
                for who in (OPERATOR, FILES, READONLY, ident(name="hub", kind="hub", via="hub")):
                    e = pol.effective(who, paused=False)
                    for t in targets:
                        for write in (False, True):
                            with self.subTest(preset=preset, who=who.name, path=str(t), write=write):
                                with self.assertRaises(HiveError) as cm:
                                    e.resolve_path(str(t), write=write)
                                self.assertIn(cm.exception.code, ("policy_denied",))
                    with self.assertRaises(HiveError):
                        e.resolve_path(str(fh.base), write=True, subtree=True)   # ancestor of HIVE_HOME


class TestMutatingDenied(unittest.TestCase):
    def test_readonly_identity_readonly_node_paused(self):
        with FakeHome() as fh:
            std = build_policy(fh)
            ro_node = build_policy(fh, policy={"read_only": True})
            for name, spec in catalog.SPECS.items():
                if spec.scope != "node" or spec.risk == "read":
                    continue
                for e in (std.effective(READONLY, paused=False), ro_node.effective(OPERATOR, paused=False),
                          std.effective(OPERATOR, paused=True)):
                    with self.subTest(action=name):
                        with self.assertRaises(HiveError) as cm:
                            e.check_action(spec)
                        self.assertIn(cm.exception.code, ("forbidden", "policy_denied"))


class TestNoLeaks(unittest.TestCase):
    def test_tokens_and_secrets_never_leak(self):
        with FakeHome(), node_server() as (url, tok, rt):
            rt.secrets.set("HF_TOKEN", SECRET)
            bad = util.new_token()
            http(url + "/v1/whoami", token=bad)
            with mock.patch.dict(HANDLERS, {"shell.run": lambda c, p: {"stdout": "token=" + SECRET},
                                            "fs.read": lambda c, p: (_ for _ in ()).throw(RuntimeError(SECRET))}):
                st, _, env = http(url + "/v1/call", "POST", {"action": "shell.run", "params": {
                    "command": f"curl -H 'Authorization: Bearer {tok}' --token {SECRET} x"}}, token=tok)
                self.assertTrue(env["ok"])
                self.assertNotIn(SECRET, json.dumps(env))
                st, _, env = http(url + "/v1/call", "POST", {"action": "fs.read", "params": {"path": "~/x"}},
                                  token=tok)
                self.assertNotIn(SECRET, json.dumps(env))
            info = http(url + "/v1/call", "POST", {"action": "node.info"}, token=tok)[2]["result"]
            self.assertEqual([s["name"] for s in info["secrets"]], ["HF_TOKEN"])
            self.assertNotIn(SECRET, json.dumps(info))
            audit_text = "".join(p.read_text() for p in (rt.data_dir / "audit").iterdir())
            for value in (SECRET, tok, bad, rt.test_owner_token):
                self.assertNotIn(value, audit_text)
            self.assertIn('"auth_fail"', audit_text)

    def test_models_token_cannot_call(self):
        models = util.new_token()
        with FakeHome(), node_server(auth={"tokens": [{"name": "chat", "sha256": util.hash_token(models),
                                                       "kind": "models", "models": ["qwen"]}]}) as (url, tok, rt):
            st, _, b = http(url + "/v1/call", "POST", {"action": "node.info"}, token=models)
            self.assertEqual((st, b["error"]["code"]), (403, "forbidden"))
            self.assertEqual(http(url + "/v1/whoami", token=models)[0], 403)


if __name__ == "__main__":
    unittest.main()
