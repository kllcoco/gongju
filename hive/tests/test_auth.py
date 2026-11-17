"""WP1 auth/limits tests (§21.2 auth/limits row): hashed tokens, hv_ prefix, expiry, bearer parsing, identities per
kind, relay identity, throttle and the bounded helpers."""
from __future__ import annotations

import time
import unittest
from types import SimpleNamespace
from unittest import mock

from hive import auth, util
from hive.auth import TokenStore, identity_from_entry, parse_bearer, relay_identity
from hive.errors import HiveError
from hive.limits import LRU, AuthThrottle, ConcurrencyGate


def entry(name, token, kind="agent", **kw):
    d = {"name": name, "sha256": util.hash_token(token), "kind": kind}
    d.update(kw)
    return d


class TestTokens(unittest.TestCase):
    def test_bearer(self):
        self.assertEqual(parse_bearer("Bearer hv_abc"), "hv_abc")
        self.assertEqual(parse_bearer("bearer   hv_abc  "), "hv_abc")
        for bad in (None, "", "Basic xyz", "Bearer", "Bearer a b", "hv_abc"):
            self.assertIsNone(parse_bearer(bad), bad)

    def test_authenticate(self):
        a, o = util.new_token(), util.new_token()
        self.assertTrue(a.startswith("hv_"))
        store = TokenStore([entry("claude", a, role="files"), entry("owner", o, kind="owner")], kind_default="agent")
        i = store.authenticate(a, "127.0.0.1")
        self.assertEqual((i.name, i.kind, i.classes, i.source_ip), ("claude", "agent", frozenset({"read", "write"}),
                                                                    "127.0.0.1"))
        self.assertEqual(i.token_sha256, util.hash_token(a))
        self.assertEqual(store.authenticate(o, "").classes, frozenset(auth.CLASSES))
        for bad, msg in ((None, "missing token"), ("", "missing token"), ("xx_" + a[3:], "invalid token"),
                         (util.new_token(), "invalid token"), ("hv_short", "invalid token")):
            with self.assertRaises(HiveError) as cm:
                store.authenticate(bad, "1.2.3.4")
            self.assertEqual((cm.exception.code, cm.exception.message), ("unauthorized", msg))
        self.assertNotIn(a, repr(store.entries))

    def test_compares_every_entry(self):
        toks = [util.new_token() for _ in range(5)]
        store = TokenStore([entry(f"t{i}", t) for i, t in enumerate(toks)], kind_default="agent")
        with mock.patch("hmac.compare_digest", wraps=__import__("hmac").compare_digest) as cd:
            store.authenticate(toks[0], "")
        self.assertEqual(cd.call_count, 5)

    def test_expiry(self):
        t = util.new_token()
        past = TokenStore([entry("old", t, expires="2020-01-01")], kind_default="agent")
        with self.assertRaises(HiveError) as cm:
            past.authenticate(t, "")
        self.assertEqual(cm.exception.message, "token expired")
        future = TokenStore([entry("new", t, expires="2999-01-01")], kind_default="agent")
        self.assertEqual(future.authenticate(t, "").name, "new")

    def test_identity_kinds(self):
        i = identity_from_entry({"name": "x", "sha256": "s", "kind": "agent", "role": "operator",
                                 "classes": ["read", "net"], "require_approval": ["shell.run"], "nodes": ["pod-*"]},
                                "agent")
        self.assertEqual((i.classes, i.require_approval, i.nodes), (frozenset({"read", "net"}), ("shell.run",),
                                                                    ("pod-*",)))
        h = identity_from_entry({"name": "hub-home", "sha256": "s", "kind": "hub", "role": "files"}, "agent")
        self.assertEqual((h.via, h.classes), ("hub", frozenset({"read", "write"})))
        n = identity_from_entry({"name": "pod-a", "sha256": "s", "kind": "node"}, "node")
        self.assertEqual((n.classes, n.node_name), (frozenset(), "pod-a"))
        m = identity_from_entry({"name": "chat", "sha256": "s", "kind": "models",
                                 "models": ["desk-wsl/qwen", "pod-*/*"]}, "agent")
        self.assertEqual((m.classes, m.models, m.nodes), (frozenset({"net"}), ("desk-wsl/qwen", "pod-*/*"),
                                                          ("desk-wsl", "pod-*")))
        self.assertEqual(identity_from_entry({"name": "x", "sha256": "s"}, "agent").classes,
                         frozenset(auth.ROLE_PRESETS["operator"]))
        o = identity_from_entry({"name": "o", "sha256": "s", "kind": "owner", "role": "readonly"}, "agent")
        self.assertTrue(o.approver and o.classes == frozenset(auth.CLASSES))

    def test_relay_identity(self):
        cfg = SimpleNamespace(relay=SimpleNamespace(role="files", deny_actions=["fs.delete"]))
        r = relay_identity(cfg)
        self.assertEqual((r.name, r.kind, r.via, r.classes, r.deny_actions),
                         ("relay", "relay", "relay", frozenset({"read", "write"}), ("fs.delete",)))
        self.assertEqual(relay_identity(SimpleNamespace(relay=SimpleNamespace(role="operator", deny_actions=[])))
                         .classes, frozenset(auth.CLASSES))


class TestLimits(unittest.TestCase):
    def test_throttle(self):
        th = AuthThrottle(max_failures=3, window=60, block=300)
        self.assertIsNone(th.check("1.2.3.4"))
        for _ in range(3):
            th.fail("1.2.3.4")
        left = th.check("1.2.3.4")
        self.assertTrue(left and 299 < left <= 300)
        self.assertIsNone(th.check("5.6.7.8"))
        th.ok("1.2.3.4")
        self.assertIsNotNone(th.check("1.2.3.4"))        # ok() never lifts an active block
        th2 = AuthThrottle(max_failures=2, window=0.05, block=10)
        th2.fail("a")
        time.sleep(0.1)
        th2.fail("a")
        self.assertIsNone(th2.check("a"))                 # failures outside the window do not count

    def test_gate(self):
        g = ConcurrencyGate(2)
        self.assertTrue(g.acquire(0.1) and g.acquire(0.1))
        self.assertEqual(g.inflight, 2)
        t0 = time.monotonic()
        self.assertFalse(g.acquire(0.1))
        self.assertGreaterEqual(time.monotonic() - t0, 0.09)
        g.release()
        self.assertTrue(g.acquire(0))
        g.release(); g.release(); g.release()            # noqa: E702 - extra release is ignored
        self.assertEqual(g.inflight, 0)

    def test_lru(self):
        c = LRU(2)
        c.put("a", 1); c.put("b", 2); c.get("a"); c.put("c", 3)   # noqa: E702
        self.assertIn("a", c)
        self.assertNotIn("b", c)
        self.assertEqual(c.pop("a"), 1)
        self.assertEqual(len(c), 1)
        t = LRU(10, ttl=0.05)
        t.put("x", 1)
        time.sleep(0.08)
        self.assertNotIn("x", t)
        self.assertEqual(t.get("x", "d"), "d")


if __name__ == "__main__":
    unittest.main()
