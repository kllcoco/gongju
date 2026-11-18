"""WP5b distribution tests: build_pyz, self_update (`hive _update`), join codes, add-node blocks (§20.1–§20.4)."""
from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hive import __version__, dist
from tests import helpers


def relay_cfg(url: str, **kw):
    d = dict(enabled=True, hub_url=url, token=None, token_file=None, tls_pin=None, ca_file=None,
             allow_insecure=False, proxy="off", auto_update=True)
    d.update(kw)
    return SimpleNamespace(**d)


class TestPyz(unittest.TestCase):
    def test_deterministic_and_contents(self):
        a, b = dist.build_pyz(), dist.build_pyz()
        self.assertEqual(dist.pyz_sha256(a), dist.pyz_sha256(b))
        self.assertTrue(a.startswith(b"#!/usr/bin/env python3\n"))
        names = zipfile.ZipFile(io.BytesIO(a)).namelist()
        self.assertEqual(names[0], "__main__.py")
        self.assertIn("hive/__init__.py", names)
        self.assertIn("hive/actions/__init__.py", names)
        self.assertFalse([n for n in names if "__pycache__" in n or n.endswith(".pyc") or n.startswith("tests/")])

    def test_pyz_runs_version(self):
        d = tempfile.mkdtemp(prefix="hivepyz-")
        self.addCleanup(shutil.rmtree, d, True)
        p = Path(d) / "hive.pyz"
        p.write_bytes(dist.build_pyz())
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        env["HIVE_HOME"] = d
        r = subprocess.run([sys.executable, str(p), "version"], cwd=d, env=env, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(__version__, r.stdout)
        # the pyz rebuilds itself identically from inside the zipapp
        code = "import sys; from hive import dist; sys.stdout.write(dist.pyz_sha256(dist.build_pyz()))"
        r = subprocess.run([sys.executable, "-c", f"import sys; sys.path.insert(0, {str(p)!r}); {code}"], cwd=d,
                           env=env, capture_output=True, text=True, timeout=60)
        self.assertEqual(r.stdout, dist.pyz_sha256(p.read_bytes()), r.stderr)


class TestSelfUpdate(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hiveupd-"))
        self.addCleanup(shutil.rmtree, str(self.d), True)
        self.pyz = self.d / "hive.pyz"
        self.pyz.write_bytes(b"old")
        self.new = b"new pyz bytes"
        self.stub = helpers.StubHTTPServer().start()
        self.addCleanup(self.stub.stop)

    def serve(self, body: bytes, sha: str, status: int = 200, headers=None):
        h = {"X-Hive-Sha256": sha}
        h.update(headers or {})
        self.stub.add("GET", "/v1/dist/hive.pyz", status, body, h)

    def test_no_request_without_auto_update(self):
        self.serve(self.new, hashlib.sha256(self.new).hexdigest())
        self.assertFalse(dist.self_update(relay_cfg(self.stub.url, auto_update=False), pyz_path=self.pyz))
        self.assertEqual(self.stub.requests, [])
        # env-only mode with a narrowed role under the watchdog: auto_update stays false -> no request
        env = {"HIVE_HOME": str(self.d / "home"), "HIVE_RELAY_URL": self.stub.url, "HIVE_RELAY_TOKEN": "hv_" + "a" * 40,
               "HIVE_WATCHDOG": "1", "HIVE_RELAY_ROLE": "files"}
        with mock.patch.dict(os.environ, env):
            self.assertFalse(dist.self_update(None, pyz_path=self.pyz))
        self.assertEqual(self.stub.requests, [])

    def test_replaces_only_verified_different(self):
        sha = hashlib.sha256(self.new).hexdigest()
        self.serve(self.new, "0" * 64)                       # header does not match the body
        self.assertFalse(dist.self_update(relay_cfg(self.stub.url), pyz_path=self.pyz))
        self.assertEqual(self.pyz.read_bytes(), b"old")
        self.serve(self.new, sha)
        self.assertTrue(dist.self_update(relay_cfg(self.stub.url), pyz_path=self.pyz))
        self.assertEqual(self.pyz.read_bytes(), self.new)
        self.assertFalse((self.d / "hive.pyz.new").exists())
        self.assertFalse(dist.self_update(relay_cfg(self.stub.url), pyz_path=self.pyz))   # same sha: nothing
        # no token is ever sent
        self.assertTrue(all("Authorization" not in r["headers"] for r in self.stub.requests))

    def test_429_retry_after(self):
        sha = hashlib.sha256(self.new).hexdigest()
        calls = {"n": 0}

        def h(req):
            calls["n"] += 1
            if calls["n"] < 3:
                return 429, {"Retry-After": "7"}, b"{}"
            return 200, {"X-Hive-Sha256": sha}, self.new
        self.stub.add_handler("GET", "/v1/dist/hive.pyz", h)
        slept = []
        with mock.patch.object(dist, "_sleep", slept.append):
            self.assertTrue(dist.self_update(relay_cfg(self.stub.url), pyz_path=self.pyz))
        self.assertEqual(slept, [7.0, 7.0])

    def test_not_from_zipapp(self):
        self.serve(self.new, hashlib.sha256(self.new).hexdigest())
        self.assertFalse(dist.self_update(relay_cfg(self.stub.url)))     # running from a source checkout
        self.assertEqual(self.stub.requests, [])


class TestJoinAndAddNode(unittest.TestCase):
    def hub_cfg(self, host="tailscale", public=None, cert=None):
        tls = SimpleNamespace(cert=cert, key=None)
        listen = SimpleNamespace(host=host, port=7700, tls=tls)
        return SimpleNamespace(listen=listen, public_url=public)

    def test_tailnet_hub_variant_t_and_public(self):
        out = dist.add_node_instructions(self.hub_cfg(), "desk-wsl", "hv_" + "t" * 40, tailnet_ip="100.101.1.7")
        self.assertEqual(out["variants"], ["T"])
        self.assertEqual(dist.decode_join(out["codes"]["T"])["u"], "http://100.101.1.7:7700")
        self.assertTrue(any("hive join" in ln for ln in out["lines"]))
        out = dist.add_node_instructions(self.hub_cfg(public="https://home-hub.tail1.ts.net"), "pod-a",
                                         "hv_" + "t" * 40, tailnet_ip="100.101.1.7")
        self.assertEqual(out["variants"], ["T", "A"])
        self.assertEqual(dist.decode_join(out["codes"]["A"])["u"], "https://home-hub.tail1.ts.net")
        self.assertTrue(any("Cloud pod (not on your tailnet)" in ln for ln in out["lines"]))
        out = dist.add_node_instructions(self.hub_cfg(public="https://home-hub.tail1.ts.net"), "pod-a",
                                         "hv_" + "t" * 40, via="public")
        self.assertEqual(out["variants"], ["A"])
        out = dist.add_node_instructions(self.hub_cfg(), "pod-b", "hv_" + "t" * 40, via="tailnet",
                                         tailnet_ip="100.101.1.7")
        self.assertEqual(out["variants"], ["T", "C"])
        self.assertTrue(any("localhost:1055" in ln for ln in out["lines"]))

    def test_self_signed_variant_b(self):
        out = dist.add_node_instructions(self.hub_cfg(host="203.0.113.5", cert="/nonexistent.pem"), "pod-a",
                                         "hv_" + "t" * 40, tls_pin="sha256:" + "ab" * 32)
        self.assertEqual(out["variants"], ["B"])
        jd = dist.decode_join(out["codes"]["B"])
        self.assertEqual((jd["u"], jd["p"]), ("https://203.0.113.5:7700", "sha256:" + "ab" * 32))
        self.assertTrue(any("curl -fsSL -k" in ln for ln in out["lines"]))


if __name__ == "__main__":
    unittest.main()
