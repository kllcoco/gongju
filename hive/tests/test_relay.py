"""WP5b RelayAgent tests (§7.5, §7.1 instance key, §5.9 hub URL rules, D3 relay identity)."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hive import relay, util
from hive.client import connect
from hive.errors import HiveError
from tests import helpers
from tests.fixtures.connect_proxy import ConnectProxy
from tests.test_transport import TEST_PIN, CertFiles
from tests.wp5util import FAST_RELAY, start_relay_node


def rcfg(url, **kw):
    d = dict(enabled=True, hub_url=url, token="hv_" + "r" * 40, token_file=None, tls_pin=None, ca_file=None,
             allow_insecure=False, proxy="off", max_parallel=8, role="operator", deny_actions=[], auto_update=False)
    d.update(kw)
    return SimpleNamespace(**d)


class TestUnits(unittest.TestCase):
    def test_hub_url_rules(self):
        relay.check_hub_url(rcfg("https://hub.example.com"))
        relay.check_hub_url(rcfg("http://127.0.0.1:7700"))
        relay.check_hub_url(rcfg("http://100.101.1.7:7700", allow_insecure=True))
        relay.check_hub_url(rcfg("http://192.168.1.5:7700", allow_insecure=True))
        for url, kw in (("http://100.101.1.7:7700", {}), ("http://93.184.216.34:7700", {"allow_insecure": True})):
            with self.subTest(url=url), self.assertRaises(HiveError) as cm:
                relay.check_hub_url(rcfg(url, **kw))
            self.assertEqual(cm.exception.details.get("rule"), "plaintext_credentials")

    def test_instance_key_file(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        k = relay.load_instance_key(d)
        self.assertTrue(k.startswith("ik_") and len(k) > 40)
        self.assertEqual(relay.load_instance_key(d), k)
        if os.name == "posix":
            self.assertEqual((d / "instance_key").stat().st_mode & 0o777, 0o600)

    def test_token_file(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / "t").write_text("hv_abcdefghij\nignored\n")
        self.assertEqual(relay.relay_token(rcfg("https://h", token=None, token_file=str(d / "t"))), "hv_abcdefghij")


class _Env(unittest.TestCase):
    def hub(self, **cfg):
        tok = util.new_token()
        e = {"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(tok)}
        cfg.setdefault("relay", dict(FAST_RELAY))
        url, toks, hub = helpers.start_hub([e], **cfg)
        self.addCleanup(hub.test_stop)
        return url, toks, hub, tok


class TestTlsAndProxy(_Env):
    @classmethod
    def setUpClass(cls):
        cls.certs = CertFiles()

    @classmethod
    def tearDownClass(cls):
        cls.certs.cleanup()

    def tls_hub(self):
        return self.hub(listen={"tls": {"cert": str(self.certs.cert), "key": str(self.certs.key)}})

    def test_pinned_tls_relay_and_wrong_pin(self):
        url, toks, hub, tok = self.tls_hub()
        surl = url.replace("http://", "https://")
        rt = start_relay_node(surl, tok, start_managers=False, relay={"tls_pin": TEST_PIN})
        self.addCleanup(rt.test_stop)
        helpers.wait_until(lambda: hub.links["pod-a"].online(), 15)
        # a wrong pin fails before any byte (and the token) is sent; not retryable
        bad = "sha256:" + "00" * 32
        t = relay.relay_transport(rcfg(surl, tls_pin=bad), token=tok)
        n_before = len(hub.audit.tail(1000))
        with self.assertRaises(HiveError) as cm:
            t.request("POST", "/v1/relay/register", {"node": "pod-a"})
        self.assertEqual((cm.exception.details["reason"], cm.exception.retryable), ("tls_pin", False))
        self.assertEqual(len(hub.audit.tail(1000)), n_before)

    def test_explicit_proxy_for_private_hub_and_auto_bypass(self):
        url, toks, hub, tok = self.hub()
        with ConnectProxy() as px:
            rt = start_relay_node(url, tok, start_managers=False, relay={"proxy": px.url})
            self.addCleanup(rt.test_stop)
            helpers.wait_until(lambda: hub.links["pod-a"].online(), 15)
            self.assertTrue(any(e["kind"] == "connect" for e in px.log))
            # "auto" with a node proxy configured: the loopback hub stays direct
            n = len(px.log)
            t = relay.relay_transport(rcfg(url, proxy="auto"), network={"proxy": px.url, "no_proxy": [],
                                                                         "use_system_proxy": False},
                                      token=tok)
            self.assertEqual(t.proxy_route()[0], None)
            self.assertEqual(len(px.log), n)


class TestRelayNode(_Env):
    def test_role_files_and_gateway_hint(self):
        url, toks, hub, tok = self.hub()
        rt = start_relay_node(url, tok, start_managers=True, relay={"role": "files"})
        self.addCleanup(rt.test_stop)
        helpers.wait_until(lambda: hub.links["pod-a"].online(), 15)
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        with self.assertRaises(HiveError) as cm:
            c.call("shell.run", {"command": "echo x"}, node="pod-a")
        self.assertEqual(cm.exception.code, "forbidden")
        if "model.forward" in rt.implemented():
            with self.assertRaises(HiveError) as cm:
                c.call("model.forward", {"service": "qwen", "path": "models"}, node="pod-a")
            self.assertEqual(cm.exception.code, "forbidden")
            self.assertIn("relay role on this pod needs the net class", cm.exception.hint)

    def test_exec_gated_agent_and_bound_files(self):
        url, toks, hub, tok = self.hub(agent_entry={"role": "files"})
        rt = start_relay_node(url, tok, start_managers=True)
        self.addCleanup(rt.test_stop)
        helpers.wait_until(lambda: hub.links["pod-a"].online(), 15)
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        bashrc = os.path.join(os.path.expanduser("~"), ".bashrc")
        with self.assertRaises(HiveError) as cm:
            c.call("fs.write", {"path": bashrc, "content": "x", "mode": "append"}, node="pod-a")
        self.assertEqual(cm.exception.code, "policy_denied")
        # a relay task carrying bound_files whose file changed -> conflict, nothing runs
        p = Path(rt.workdir) / f"bound-{os.getpid()}.sh"
        p.write_text("echo one\n")
        self.addCleanup(lambda: p.exists() and p.unlink())
        from hive.auth import relay_identity
        import dataclasses
        ident = dataclasses.replace(relay_identity(rt.config), agent="test")
        env = rt.call(ident, "fs.stat", {"path": str(p)}, via="relay",
                      bound_files={str(p): "sha256:" + "0" * 64})
        self.assertEqual(env["error"]["code"], "conflict")

    def test_relay_disabled_stops_polling(self):
        url, toks, hub, tok = self.hub()
        rt = start_relay_node(url, tok, start_managers=False)
        self.addCleanup(rt.test_stop)
        helpers.wait_until(lambda: hub.links["pod-a"].online(), 15)
        helpers.wait_until(lambda: rt.relay.status["connected"], 10)
        (Path(rt.data_dir) / "relay.disabled").write_text("{}")
        helpers.wait_until(lambda: rt.relay.session_id is None, 10)
        link = hub.links["pod-a"]
        with link.lock:
            link.last_poll_end = time.monotonic() - 100
        self.assertFalse(link.online())


if __name__ == "__main__":
    unittest.main()
