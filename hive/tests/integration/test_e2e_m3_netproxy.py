"""M3 (§22.1 step 6): netproxy routes end to end — a real node daemon whose ``network.proxy`` is the
``connect_proxy`` stub (with Basic auth), and a fake-ip DNS for the node and its ``_download`` children.

The node's ``PYTHONPATH`` carries the test ``sitecustomize`` of ``m3util`` (env ``HIVE_TEST_FAKE_DNS``), so for the
node and every child it starts: ``*.fake.m3.test`` names resolve only to Clash/Mihomo fake-ip addresses
(``198.18.0.0/15``), ``mixed.m3.test`` to a fake-ip plus a LAN address, ``lan.m3.test`` to 127.0.0.1; ``nodns.m3.test``
does not resolve (``.test`` is reserved). The proxy maps the requested ``host:port`` to local stub servers, so no
real network is used. Checked (§5.4 NetGuard rules 3–5c, §11.4, §10.1 step 3):

* all-fakeip names are public: ``http_request`` goes through the proxy by **hostname** (absolute-form for http, a
  ``CONNECT host:443`` for https); an unresolvable name goes through the proxy too (rule 4b);
* a literal fake-ip address is refused (reserved); a fake-ip + private mix is refused;
* a name resolving to loopback is never proxied; ``model_chat`` to a local service never touches the proxy;
* a gated (cautious) token's POST to an all-fakeip name → ``policy_denied`` ``loopback_write``, its GET passes;
* ``net_download`` and ``model.pull`` (``_download`` children) use the same routes: fake-ip endpoint, the CDN
  redirect hop re-vetted and proxied, an unresolvable host;
* children get ``HTTP(S)_PROXY`` and the fixed ``NO_PROXY`` list (§10.1 step 3); jobs too;
* the proxy password never shows in ``node.info``, ``sys_info``, tool texts, errors or the node log; a proxy that
  starts refusing the credentials → ``unavailable`` reason ``proxy_auth``.
"""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any

from hive.errors import HiveError

from tests import helpers
from tests.fixtures.connect_proxy import ConnectProxy
from tests.fixtures.hfstub import HFStub, Repo
from tests.integration.m3util import IS_WINDOWS, NodeDaemon, ensure_fake_dns, job_id_of, wait_job

PROXY_AUTH = "m3user:m3-proxy-pw"
FAKE_DNS = {"pub.fake.m3.test": ["198.18.0.10"], "hf.fake.m3.test": ["198.18.0.11"],
            "cdn.fake.m3.test": ["198.18.0.12", "198.18.0.13"], "mixed.m3.test": ["198.18.0.14", "192.168.77.5"],
            "lan.m3.test": ["127.0.0.1"]}
PAYLOAD = os.urandom(70_000)
HF_FILES = {"config.json": b'{"model_type": "llama"}', "model.safetensors": os.urandom(30_000)}


class TestE2EM3NetProxy(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.web = helpers.StubHTTPServer()
        cls.web.add("GET", "/hello", 200, "hello via route")
        cls.web.add("POST", "/hello", 200, "posted")
        cls.web.add_file("/file.bin", PAYLOAD)
        cls.web.openai(models=("local-model",), reply="local pong")
        cls.web.start()
        wport = cls.web.port
        cls.hf = HFStub({"org/px": Repo(HF_FILES)}, cdn_host="cdn.fake.m3.test").start()
        cls.px = ConnectProxy(routes={
            "pub.fake.m3.test:80": ("127.0.0.1", wport), "pub.fake.m3.test:443": ("127.0.0.1", wport),
            "nodns.m3.test:80": ("127.0.0.1", wport),
            "hf.fake.m3.test:80": ("127.0.0.1", cls.hf.port),
            f"cdn.fake.m3.test:{cls.hf.port}": ("127.0.0.1", cls.hf.port),
        }, auth=PROXY_AUTH).start()
        cls.proxy_url = f"http://{PROXY_AUTH}@127.0.0.1:{cls.px.port}"
        cls.node = None
        try:
            base_dns = Path(__import__("tempfile").mkdtemp(prefix="hivem3dns-")).resolve()
            cls.dns_dir = ensure_fake_dns(base_dns)

            def cfg(raw: dict) -> None:
                raw.setdefault("models", {}).update(hf_endpoint="http://hf.fake.m3.test", dir="~/models")
            def tokens(node: NodeDaemon) -> None:
                cls.gated = node.add_token("gated", "--cautious")
            cls.node = NodeDaemon("m3proxy", init_args=("--proxy", cls.proxy_url, "--mirror", "none"),
                                  cfg_edit=cfg, pythonpath_extra=(str(cls.dns_dir),),
                                  env_extra={"HIVE_TEST_FAKE_DNS": json.dumps(FAKE_DNS)}, before_run=tokens)
            cls.node.start()
            cls.mcp = cls.node.mcp()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "node", None) is not None:
            cls.node.stop()
        for s in ("web", "hf", "px"):
            o = getattr(cls, s, None)
            if o is not None:
                o.stop()
        d = getattr(cls, "dns_dir", None)
        if d is not None:
            import shutil
            shutil.rmtree(str(d), ignore_errors=True)

    def setUp(self) -> None:
        self.px.auth = PROXY_AUTH

    # -------------------------------------------------------------------------------------------------------------
    def tool(self, name: str, args: dict, *, error: bool = False) -> str:
        text, is_err = self.mcp.tool(name, args)
        self.assertEqual(is_err, error, f"{name}: {text}")
        self.assertNotIn("m3-proxy-pw", text)
        return text

    def http(self, url: str, *, method: str = "GET", token: Any = None, **kw: Any) -> dict:
        p = {"url": url, "method": method}
        p.update(kw)
        return self.node.call("net.http", p, token=token or self.node.agent)

    def http_err(self, url: str, **kw: Any) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.http(url, **kw)
        self.assertNotIn("m3-proxy-pw", json.dumps(cm.exception.to_dict()))
        return cm.exception

    def proxied(self, needle: str) -> list:
        return [e for e in self.px.log if needle in e["target"]]

    # -------------------------------------------------------------------------------------------------------------
    def test_01_fakeip_name_goes_through_proxy_by_hostname(self) -> None:
        n = len(self.px.log)
        text = self.tool("http_request", {"url": "http://pub.fake.m3.test/hello"})
        self.assertIn("hello via route", text)
        new = self.px.log[n:]
        self.assertTrue(new, "the request did not use the proxy")
        self.assertEqual(new[0]["kind"], "absolute")
        self.assertTrue(new[0]["target"].startswith("http://pub.fake.m3.test/"), new[0])
        self.assertNotIn("198.18.", json.dumps(new))          # the proxy resolves the name, not the fake-ip
        # https: a CONNECT tunnel to the hostname (the stub behind it is not TLS, so the handshake fails)
        n = len(self.px.log)
        e = self.http_err("https://pub.fake.m3.test/hello", timeout=10)
        self.assertEqual(e.code, "unavailable", e.to_dict())
        new = self.px.log[n:]
        self.assertEqual([(x["kind"], x["target"]) for x in new][:1], [("connect", "pub.fake.m3.test:443")])

    def test_02_unresolvable_name_via_proxy(self) -> None:
        n = len(self.px.log)
        r = self.http("http://nodns.m3.test/hello")
        self.assertEqual((r["status"], r["body"]), (200, "hello via route"))
        self.assertTrue(self.proxied("nodns.m3.test")[0:], self.px.log[n:])

    def test_03_literal_fakeip_and_mixed_refused(self) -> None:
        n = len(self.px.log)
        e = self.http_err("http://198.18.0.10/hello")
        self.assertEqual(e.code, "policy_denied")
        e = self.http_err("http://mixed.m3.test/hello")
        self.assertEqual(e.code, "policy_denied")
        self.assertEqual(self.px.log[n:], [])

    def test_04_loopback_never_proxied(self) -> None:
        n = len(self.px.log)
        r = self.http(f"http://lan.m3.test:{self.web.port}/hello")
        self.assertEqual(r["body"], "hello via route")
        r = self.http(f"http://127.0.0.1:{self.web.port}/hello")
        self.assertEqual(r["status"], 200)
        text = self.tool("model_chat", {"base_url": f"http://127.0.0.1:{self.web.port}/v1", "prompt": "hi"})
        self.assertIn("local pong", text)
        self.assertEqual(self.px.log[n:], [])

    def test_05_gated_post_to_fakeip_refused(self) -> None:
        e = self.http_err("http://pub.fake.m3.test/hello", method="POST", token=self.gated, body="x")
        self.assertEqual((e.code, e.details.get("rule")), ("policy_denied", "loopback_write"), e.to_dict())
        r = self.http("http://pub.fake.m3.test/hello", token=self.gated)
        self.assertEqual(r["status"], 200)
        # the exec-capable agent token keeps rule 4a: its POST goes through the proxy by name
        r = self.http("http://pub.fake.m3.test/hello", method="POST", body="x")
        self.assertEqual((r["status"], r["body"]), (200, "posted"))

    def test_06_download_child_uses_the_routes(self) -> None:
        for host in ("pub.fake.m3.test", "nodns.m3.test"):
            n = len(self.px.log)
            text = self.tool("net_download", {"url": f"http://{host}/file.bin", "path": f"~/dl/{host}.bin",
                                              "make_dirs": True})
            st = wait_job(self.node, job_id_of(text))
            self.assertEqual(st["exit_code"], 0, st)
            self.assertEqual((self.node.user / "dl" / f"{host}.bin").read_bytes(), PAYLOAD)
            self.assertTrue(any(host in x["target"] for x in self.px.log[n:]), self.px.log[n:])

    def test_07_model_pull_fakeip_endpoint_and_cdn_hop(self) -> None:
        n = len(self.px.log)
        text = self.tool("model_pull", {"repo": "org/px"})
        st = wait_job(self.node, job_id_of(text))
        self.assertEqual(st["exit_code"], 0, st)
        for name, data in HF_FILES.items():
            self.assertEqual((self.node.user / "models" / "org--px" / name).read_bytes(), data)
        targets = [x["target"] for x in self.px.log[n:]]
        self.assertTrue(any("hf.fake.m3.test" in t for t in targets), targets)
        self.assertTrue(any("cdn.fake.m3.test" in t for t in targets), targets)   # the redirect hop
        self.assertTrue(any(q.get("cdn") for q in self.hf.requests))

    def test_08_child_proxy_env(self) -> None:
        cmd = "env | grep -i -E '^(https?_proxy|no_proxy)='" if not IS_WINDOWS else "set"
        res = self.node.call("shell.run", {"command": cmd}, token=self.node.agent)
        text = res.get("stdout") or res.get("output") or ""
        env = dict(line.split("=", 1) for line in text.splitlines() if "=" in line)
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            self.assertEqual(env.get(k), self.proxy_url, env)
        no_proxy = env.get("NO_PROXY", "").split(",")
        for x in ("localhost", "127.0.0.1"):
            self.assertIn(x, no_proxy)
        # a job (shim-run child) gets the same
        st = self.node.call("job.start", {"command": f'{sys.executable} -c "import os;print(os.environ.get(\'HTTPS_PROXY\'))"'})
        st = wait_job(self.node, st["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertIn(f"127.0.0.1:{self.px.port}", st["tail"])

    def test_09_credentials_never_shown(self) -> None:
        info = self.node.call("node.info")
        self.assertNotIn("m3-proxy-pw", json.dumps(info))
        self.assertIn(str(self.px.port), json.dumps(info))     # the redacted proxy is listed
        text = self.tool("sys_info", {})
        self.assertNotIn("m3-proxy-pw", text)
        r = self.node.cli("node", "status")
        self.assertNotIn("m3-proxy-pw", r.stdout + r.stderr)

    def test_10_proxy_auth_refused(self) -> None:
        self.px.auth = "m3user:other"
        e = self.http_err("http://pub.fake.m3.test/hello")
        self.assertEqual((e.code, e.details.get("reason")), ("unavailable", "proxy_auth"), e.to_dict())
        text = self.tool("http_request", {"url": "http://nodns.m3.test/hello"}, error=True)
        self.assertIn("proxy", text.lower())

    def test_99_node_log_has_no_proxy_password(self) -> None:
        self.assertNotIn("m3-proxy-pw", self.node.output(1 << 20))


if __name__ == "__main__":
    unittest.main()
