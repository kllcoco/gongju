"""tests/helpers.py (P0, §21.1): the real fakes behave as specified."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from hive import netutil
from hive.errors import HiveError
from hive.netutil import VettedTarget
from tests import helpers


class TestFakeNetGuard(unittest.TestCase):
    def test_accepts_netguard_kwargs_and_records(self):
        g = helpers.FakeNetGuard(http_allow=["loopback"], http_deny=["10.0.0.0/8"], download_allow=["public"],
                                 allow_metadata_ip=False, proxies=None, own_ports=(7701,),
                                 download_redirect_allow=["*.cdn.example"], resolve={"internal.corp": ["10.1.1.1"]})
        t = g.check_url("https://huggingface.co:8443/api?x=1", purpose="download", redirect=True)
        self.assertEqual(t, VettedTarget("https", "huggingface.co", 8443, "/api?x=1", "93.184.216.34", "direct", None,
                                         "public"))
        self.assertEqual(g.calls, [("https://huggingface.co:8443/api?x=1", "download", "GET", True)])
        self.assertEqual(g.check_url("http://localhost/", purpose="http", method="POST").category, "loopback")
        self.assertEqual(g.check_url("http://internal.corp/", purpose="http").category, "private")
        self.assertEqual(g.check_url("http://198.18.0.5/", purpose="http").category, "fakeip")
        self.assertEqual(g.calls[1][2], "POST")

    def test_ssrf_categories(self):
        g = helpers.FakeNetGuard(resolve={"metadata.google.internal": ["169.254.169.254"]})
        for url in ("http://169.254.169.254/latest", "http://[fe80::1]/", "http://0.0.0.0:80/",
                    "http://metadata.google.internal/", "http://[::ffff:169.254.169.254]/"):
            with self.subTest(url=url), self.assertRaises(HiveError) as cm:
                g.check_url(url, purpose="http")
            self.assertEqual((cm.exception.code, cm.exception.details["rule"]), ("policy_denied", "ssrf"))

    def test_bad_urls(self):
        g = helpers.FakeNetGuard()
        for url in ("ftp://x/", "http://user:pw@h/", "nonsense"):
            with self.subTest(url=url), self.assertRaises(HiveError):
                g.check_url(url, purpose="http")

    def test_round_trip(self):
        g = helpers.FakeNetGuard(http_allow=["public"], resolve={"a.b": ["1.2.3.4"]})
        g2 = helpers.FakeNetGuard.from_dict(json.loads(json.dumps(g.to_dict())))
        self.assertEqual(g2.to_dict(), g.to_dict())
        self.assertEqual(g2.check_url("http://a.b/", purpose="http").ip, "1.2.3.4")

    def test_patchable_as_netutil_netguard(self):
        with mock.patch("hive.netutil.NetGuard", helpers.FakeNetGuard):
            g = netutil.NetGuard(http_allow=[], http_deny=[], download_allow=[], allow_metadata_ip=False,
                                 proxies=None)
            self.assertEqual(g.check_url("http://127.0.0.1/", purpose="health").route, "direct")


class TestFakeEffectivePolicy(unittest.TestCase):
    def setUp(self):
        self.rt = helpers.FakeRuntime()
        self.pol = helpers.FakeEffectivePolicy(workdir=self.rt.workdir, data_dir=self.rt.data_dir)

    def tearDown(self):
        self.rt.cleanup()

    def test_resolve_path(self):
        p = self.pol.resolve_path("a/b.txt", write=True)
        self.assertEqual(p, (self.rt.workdir / "a" / "b.txt").resolve())
        with self.assertRaises(HiveError) as cm:
            self.pol.resolve_path(str(self.rt.data_dir / "x"))
        self.assertEqual(cm.exception.details["rule"], "hive_internal")
        with self.assertRaises(HiveError):
            self.pol.resolve_path("/tmp/a/.hive/b")
        with self.assertRaises(HiveError) as cm:
            self.pol.resolve_path("missing.txt", must_exist=True)
        self.assertEqual(cm.exception.code, "not_found")
        for bad in ("", "a\x00b"):
            with self.assertRaises(HiveError):
                self.pol.resolve_path(bad)

    def test_surface(self):
        pol = self.pol
        self.assertIsNone(pol.check_action(None))
        self.assertFalse(pol.needs_approval(None))
        self.assertEqual(pol.roots(), [{"path": "*", "mode": "rw"}])
        self.assertFalse(pol.write_exec_paths_denied)
        self.assertEqual(pol.check_cwd(None), self.rt.workdir)
        self.assertIs(pol.net_guard, pol)
        self.assertEqual(pol.proxies.source, "none")
        self.assertEqual(pol.check_timeout(None, 60, 3600), 60)
        self.assertEqual(pol.check_timeout(10, 60, 3600), 10)
        with self.assertRaises(HiveError) as cm:
            pol.check_timeout(7200, 60, 3600)
        self.assertIn("max timeout on this node is 3600 s", cm.exception.hint)
        with self.assertRaises(HiveError):
            pol.auth_spec("HF_TOKEN")
        self.assertIn("exec", pol.summary())

    def test_child_env(self):
        with mock.patch.dict(os.environ, {"HIVE_SECRET_X": "1", "HIVE_HOME": "/h", "KEEP": "k", "DROP": "d"}):
            env = self.pol.child_env({"A": "1"}, unset=["DROP"], gpus="0,1")
        self.assertNotIn("HIVE_SECRET_X", env)
        self.assertNotIn("HIVE_HOME", env)
        self.assertNotIn("DROP", env)
        self.assertEqual((env["KEEP"], env["A"], env["CUDA_VISIBLE_DEVICES"]), ("k", "1", "0,1"))
        self.assertEqual(self.pol.child_env({}, gpus="none")["HIP_VISIBLE_DEVICES"], "")

    def test_check_url(self):
        self.assertEqual(self.pol.check_url("http://127.0.0.1:8000/v1", purpose="model").category, "loopback")
        self.assertEqual(self.pol.check_url("https://example.com/", purpose="http").category, "public")
        with self.assertRaises(HiveError):
            self.pol.check_url("http://10.0.0.5/", purpose="http")
        self.assertEqual(self.pol.url_calls[0], ("http://127.0.0.1:8000/v1", "model", "GET", False))


class TestFakeRuntimeAndCtx(unittest.TestCase):
    def test_make_ctx_defaults(self):
        ctx = helpers.make_ctx("shell.run", timeout=12)
        self.assertEqual(ctx.spec.name, "shell.run")
        self.assertIsInstance(ctx.rt, helpers.FakeRuntime)
        self.assertIsInstance(ctx.policy, helpers.FakeEffectivePolicy)
        self.assertEqual(ctx.identity.classes, frozenset(helpers.ALL_CLASSES))
        self.assertEqual(ctx.via, "direct")
        self.assertTrue(ctx.request_id.startswith("r_test"))
        self.assertLessEqual(ctx.remaining(), 12)
        self.assertEqual(ctx.rt.managed_pids(), {})
        self.assertEqual(ctx.rt.keepalive_pids(), {})
        self.assertIsNone(ctx.rt.audit.record(kind="call"))
        ctx.rt.cleanup()

    def test_make_ctx_temp_dir_removed_without_cleanup(self):
        import gc
        ctx = helpers.make_ctx("fs.read")
        base = ctx.rt.base
        self.assertTrue(base.is_dir())
        del ctx
        gc.collect()
        self.assertFalse(base.exists())
        rt = helpers.FakeRuntime()
        rt.cleanup()
        rt.cleanup()   # idempotent
        self.assertFalse(rt.base.exists())

    def test_make_ctx_unknown_action(self):
        with self.assertRaises(HiveError):
            helpers.make_ctx("no.such")

    def test_identity_helper(self):
        i = helpers.identity(("read",), name="ro", via="hub")
        self.assertTrue(i.read_only)
        self.assertEqual((i.name, i.kind, i.via), ("ro", "agent", "hub"))

    def test_set_secret_and_masking(self):
        rt = helpers.FakeRuntime()
        try:
            helpers.set_secret(rt, "HF_TOKEN", "hf_abcdefghijklmnop")
            self.assertTrue(rt.secrets.has_values)
            self.assertEqual(rt.secrets.mask("x hf_abcdefghijklmnop y"), "x «secret:HF_TOKEN» y")
            self.assertEqual(rt.secrets.names()[0]["hosts"][0], "huggingface.co")
            a = rt.secrets.resolve_auth("HF_TOKEN")
            self.assertEqual(a.value, "Bearer hf_abcdefghijklmnop")
            self.assertEqual(rt.secrets.resolve("HF_TOKEN", purpose="auth", host="huggingface.co"),
                             "hf_abcdefghijklmnop")
            with self.assertRaises(HiveError):
                rt.secrets.resolve("HF_TOKEN", purpose="auth", host="evil.example")
            with self.assertRaises(HiveError):
                rt.secrets.resolve("NOPE", purpose="exec")
        finally:
            rt.cleanup()

    def test_make_node_config(self):
        c = helpers.make_node_config("cautious", name="x", listen={"port": 1234})
        self.assertEqual((c["name"], c["policy"]["preset"], c["listen"]["port"], c["listen"]["host"]),
                         ("x", "cautious", 1234, "127.0.0.1"))

    def test_wp_dependent_helpers_skip(self):
        # WP1 merged: make_runtime/start_node_server build real runtimes; the hub (WP5) still skips
        with helpers.tmp_home(), self.assertRaises(unittest.SkipTest):
            helpers.start_hub()
        with helpers.tmp_home():
            rt = helpers.make_runtime(start_managers=False)
            try:
                self.assertEqual(rt.name, "test-node")
            finally:
                rt.stop()
        with helpers.tmp_home():
            url, token, rt = helpers.start_node_server()
            try:
                self.assertTrue(url.startswith("http://127.0.0.1:") and token.startswith("hv_"))
            finally:
                rt.test_stop()


class TestUtilities(unittest.TestCase):
    def test_tmp_home(self):
        old = os.environ.get("HIVE_HOME")
        with helpers.tmp_home() as h:
            self.assertEqual(os.environ["HIVE_HOME"], str(h))
            self.assertTrue(h.is_dir())
            self.assertNotIn(".hive", h.parts)
        self.assertEqual(os.environ.get("HIVE_HOME"), old)
        self.assertFalse(h.exists())

    def test_py_cmd_wait_until_free_port(self):
        out = subprocess.run(helpers.py_cmd("print(6*7)"), capture_output=True, text=True, timeout=60)
        self.assertEqual(out.stdout.strip(), "42")
        helpers.wait_until(lambda: True)
        with self.assertRaises(AssertionError):
            helpers.wait_until(lambda: False, timeout=0.1)
        self.assertGreater(helpers.free_port(), 0)

    def test_fake_tool_on_path(self):
        with helpers.fake_tool_on_path("nvidia-smi", "print('GPU 0: Fake RTX')") as d:
            self.assertIn(str(d), os.environ["PATH"])
            exe = "nvidia-smi.cmd" if os.name == "nt" else "nvidia-smi"
            out = subprocess.run([str(Path(d) / exe)], capture_output=True, text=True, timeout=60)
            self.assertEqual(out.stdout.strip(), "GPU 0: Fake RTX")
        self.assertNotIn(str(d), os.environ["PATH"])


class TestStubHTTPServer(unittest.TestCase):
    def test_routes_openai_sse_range(self):
        with helpers.StubHTTPServer() as srv:
            srv.openai(models=["qwen"])
            srv.add("GET", "/plain", 200, "hi")
            srv.add_file("/file.bin", b"0123456789")
            srv.add_sse("/events", [{"a": 1}, "raw"])
            with urllib.request.urlopen(srv.url + "/v1/models") as r:
                self.assertEqual(json.loads(r.read())["data"][0]["id"], "qwen")
            req = urllib.request.Request(srv.url + "/v1/chat/completions", method="POST",
                                         data=json.dumps({"messages": []}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual(json.loads(r.read())["choices"][0]["message"]["content"], "hello from stub")
            with urllib.request.urlopen(srv.url + "/plain") as r:
                self.assertEqual(r.read(), b"hi")
            req = urllib.request.Request(srv.url + "/file.bin", headers={"Range": "bytes=4-"})
            with urllib.request.urlopen(req) as r:
                self.assertEqual((r.status, r.read(), r.headers["Content-Range"]), (206, b"456789", "bytes 4-9/10"))
            for rng, want in (("bytes=2-4", (206, b"234", "bytes 2-4/10")),
                              ("bytes=-3", (206, b"789", "bytes 7-9/10")),
                              ("bytes=-30", (206, b"0123456789", "bytes 0-9/10")),
                              ("bytes=8-100", (206, b"89", "bytes 8-9/10")),
                              ("bytes=9-", (206, b"9", "bytes 9-9/10"))):
                req = urllib.request.Request(srv.url + "/file.bin", headers={"Range": rng})
                with urllib.request.urlopen(req) as r:
                    self.assertEqual((r.status, r.read(), r.headers["Content-Range"]), want, rng)
            for rng in ("bytes=10-", "bytes=100-200"):   # start at/after the end (resume of a complete .part)
                req = urllib.request.Request(srv.url + "/file.bin", headers={"Range": rng})
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    urllib.request.urlopen(req)
                self.assertEqual((cm.exception.code, cm.exception.headers["Content-Range"]), (416, "bytes */10"), rng)
                cm.exception.close()
            req = urllib.request.Request(srv.url + "/file.bin", headers={"Range": "bytes=5-2"})   # invalid -> 200
            with urllib.request.urlopen(req) as r:
                self.assertEqual((r.status, r.read()), (200, b"0123456789"))
            req = urllib.request.Request(srv.url + "/events", method="POST", data=b"{}")
            with urllib.request.urlopen(req) as r:
                self.assertEqual(r.read(), b'data: {"a": 1}\n\ndata: raw\n\ndata: [DONE]\n\n')
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(srv.url + "/nope")
            self.assertEqual(cm.exception.code, 404)
            self.assertEqual(srv.requests[0]["path"], "/v1/models")


if __name__ == "__main__":
    unittest.main()
