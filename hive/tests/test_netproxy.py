"""WP3b tests: netproxy (design §11.4, §21.2 netproxy row): precedence, off, socks/https ignored, bypass matching,
open_tunnel against a stub proxy, credential redaction, child env, wsl-host, Windows registry parsing."""
from __future__ import annotations

import logging
import os
import socket
import sys
import tempfile
import threading
import types
import unittest
from typing import Any, Dict, List
from unittest import mock

from hive import netproxy
from hive.errors import HiveError
from tests import helpers
from tests.fixtures.connect_proxy import ConnectProxy


def load(cfg: Any = None, env: Dict[str, str] = None, **kw: Any) -> netproxy.ProxySettings:
    netproxy.clear_cache()
    with mock.patch.object(netproxy, "_system_proxies", return_value=(None, None, [])):
        return netproxy.load_proxy_settings(cfg, env or {}, **kw)


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class ProxyCase(unittest.TestCase):
    def setUp(self) -> None:
        netproxy.clear_cache()
        netproxy._warned.clear()
        self.log = LogCapture()
        lg = logging.getLogger("hive.netproxy")
        lg.addHandler(self.log)
        self.addCleanup(lg.removeHandler, self.log)
        self.addCleanup(netproxy.clear_cache)


class TestPrecedence(ProxyCase):
    def test_config_wins_over_env_and_system(self):
        env = {"HTTPS_PROXY": "http://envproxy:1", "HTTP_PROXY": "http://envproxy:2"}
        with mock.patch.object(netproxy, "_system_proxies", return_value=("http://sys:3", "http://sys:3", [])):
            s = netproxy.load_proxy_settings({"proxy": "http://127.0.0.1:7890"}, env)
        self.assertEqual((s.http, s.https, s.source), ("http://127.0.0.1:7890", "http://127.0.0.1:7890", "config"))

    def test_env_then_system(self):
        s = load({}, {"HTTPS_PROXY": "http://127.0.0.1:1080", "http_proxy": "http://127.0.0.1:1081"})
        self.assertEqual((s.http, s.https, s.source), ("http://127.0.0.1:1081", "http://127.0.0.1:1080", "env"))
        s = load({}, {"ALL_PROXY": "http://127.0.0.1:9"})
        self.assertEqual((s.http, s.https, s.source), ("http://127.0.0.1:9", "http://127.0.0.1:9", "env"))
        netproxy.clear_cache()
        with mock.patch.object(netproxy, "_system_proxies", return_value=("http://10.0.0.1:7890", None, [])):
            s = netproxy.load_proxy_settings({}, {})
        self.assertEqual((s.http, s.https, s.source), ("http://10.0.0.1:7890", None, "system"))
        netproxy.clear_cache()
        with mock.patch.object(netproxy, "_system_proxies", return_value=("http://10.0.0.1:7890", None, [])):
            s = netproxy.load_proxy_settings({"use_system_proxy": False}, {})
        self.assertEqual(s.source, "none")

    def test_off(self):
        s = load({"proxy": "off"}, {"HTTPS_PROXY": "http://127.0.0.1:1"})
        self.assertEqual((s.http, s.https, s.source), (None, None, "none"))
        self.assertEqual(s.describe(), "none")
        self.assertEqual(load(None, {"HTTPS_PROXY": "http://x:1"}, explicit="off").source, "none")

    def test_explicit(self):
        s = load(None, {"HTTPS_PROXY": "http://x:1"}, explicit="http://u:p@127.0.0.1:3128/")
        self.assertEqual((s.http, s.https, s.source), ("http://u:p@127.0.0.1:3128", "http://u:p@127.0.0.1:3128",
                                                       "config"))
        with self.assertRaises(HiveError) as cm:
            load(None, {}, explicit="socks5://127.0.0.1:1080")
        self.assertEqual(cm.exception.code, "bad_request")

    def test_socks_and_https_ignored_with_one_warning(self):
        env = {"HTTPS_PROXY": "socks5://127.0.0.1:7891", "HTTP_PROXY": "https://127.0.0.1:7892"}
        s = load({}, env)
        self.assertEqual((s.http, s.https, s.source), (None, None, "none"))
        s = load({}, env)
        warns = [x for x in self.log.lines if "only HTTP proxies are supported" in x]
        self.assertEqual(len(warns), 2)           # one per bad value, not per load
        s = load({"proxy": "socks5://127.0.0.1:1"}, {})
        self.assertEqual(s.source, "none")

    def test_no_proxy_merge_and_flags(self):
        s = load({"proxy": "http://127.0.0.1:7890", "no_proxy": ["corp.lan"], "strict_dns": True,
                  "ca_file": "/x/ca.pem"}, {"NO_PROXY": "a.com,.b.org"})
        self.assertEqual(s.no_proxy[:3], ("corp.lan", "a.com", ".b.org"))
        for x in netproxy.ALWAYS_NO_PROXY:
            self.assertIn(x, s.no_proxy)
        self.assertTrue(s.strict_dns)
        self.assertEqual(s.ca_file, "/x/ca.pem")
        self.assertEqual(s.to_dict()["ca_file"], "/x/ca.pem")

    def test_cache_60s(self):
        clock = [1000.0]
        with mock.patch.object(netproxy, "_now", lambda: clock[0]), \
                mock.patch.object(netproxy, "_system_proxies", return_value=("http://10.0.0.1:1", None, [])) as sp:
            netproxy.clear_cache()
            netproxy.load_proxy_settings({}, {})
            netproxy.load_proxy_settings({}, {})
            self.assertEqual(sp.call_count, 1)
            clock[0] += 61
            netproxy.load_proxy_settings({}, {})
            self.assertEqual(sp.call_count, 2)


class TestBypass(ProxyCase):
    def setUp(self) -> None:
        super().setUp()
        self.s = netproxy.ProxySettings("http://p:1", "http://p:1",
                                        ("example.com", ".corp.org", "*.glob.net", "10.1.2.3", "192.0.2.0/24",
                                         "host.with:8080", "<local>", "::1", "localhost", "127.0.0.1"), "config")

    def test_names(self):
        b = netproxy.bypass
        self.assertTrue(b(self.s, "example.com"))
        self.assertTrue(b(self.s, "API.Example.com"))
        self.assertFalse(b(self.s, "ample.com"))
        self.assertFalse(b(self.s, "notexample.com"))
        self.assertTrue(b(self.s, "corp.org"))
        self.assertTrue(b(self.s, "x.y.corp.org"))
        self.assertTrue(b(self.s, "a.glob.net"))
        self.assertTrue(b(self.s, "host.with"))           # :port suffix ignored
        self.assertTrue(b(self.s, "intranet"))            # <local>: no dot
        self.assertFalse(b(self.s, "huggingface.co"))
        self.assertTrue(b(netproxy.ProxySettings("http://p:1", None, ("*",), "env"), "anything.com"))

    def test_ips(self):
        b = netproxy.bypass
        self.assertTrue(b(self.s, "10.1.2.3"))
        self.assertTrue(b(self.s, "192.0.2.77"))
        self.assertTrue(b(self.s, "[::1]"))
        self.assertTrue(b(self.s, "pub.example.net", ["192.0.2.5"]))   # resolved address inside a CIDR
        self.assertFalse(b(self.s, "93.184.216.34"))

    def test_proxy_for(self):
        pf = netproxy.proxy_for
        s = netproxy.ProxySettings("http://p:1", "http://p:2", netproxy.ALWAYS_NO_PROXY, "env")
        self.assertEqual(pf(s, "https", "huggingface.co"), "http://p:2")
        self.assertEqual(pf(s, "http", "huggingface.co"), "http://p:1")
        self.assertIsNone(pf(s, "https", "localhost"))
        self.assertIsNone(pf(s, "http", "127.0.0.1"))
        self.assertIsNone(pf(s, "http", "192.168.1.5"))
        self.assertIsNone(pf(s, "http", "nas"))                         # local name
        self.assertIsNone(pf(s, "http", "printer.local"))
        self.assertEqual(pf(s, "https", "93.184.216.34"), "http://p:2")
        # v6: resolved addresses decide; tailnet -> direct, public -> proxy, fake-ip counts as public
        self.assertIsNone(pf(s, "https", "home-hub.tail1.ts.net", ["100.101.1.7"]))
        self.assertEqual(pf(s, "https", "home-hub.tail1.ts.net", ["34.1.2.3"]), "http://p:2")
        self.assertEqual(pf(s, "https", "x.com", ["198.18.0.5"]), "http://p:2")
        self.assertIsNone(pf(s, "https", "x.com", ["93.184.216.34", "10.0.0.1"]))
        self.assertIsNone(pf(netproxy.ProxySettings(None, None, (), "none"), "https", "x.com"))

    def test_client_hive_proxy_keeps_loopback_direct(self):
        s = load({"proxy": "http://127.0.0.1:7890", "no_proxy": [], "use_system_proxy": True}, {})
        self.assertIsNone(netproxy.proxy_for(s, "http", "127.0.0.1", ["127.0.0.1"]))
        self.assertIsNone(netproxy.proxy_for(s, "http", "hub.lan", ["192.168.1.2"]))
        self.assertEqual(netproxy.proxy_for(s, "https", "hub.example.com", ["93.184.216.34"]),
                         "http://127.0.0.1:7890")


class TestTunnel(ProxyCase):
    def test_tunnel_200(self):
        with helpers.StubHTTPServer() as srv, ConnectProxy(routes={"target.test:80": ("127.0.0.1", srv.port)}) as px:
            srv.add("GET", "/hello", 200, "hi")
            s = netproxy.open_tunnel(px.url, "target.test", 80, timeout=5)
            try:
                s.sendall(b"GET /hello HTTP/1.1\r\nHost: target.test\r\nConnection: close\r\n\r\n")
                data = b""
                while True:
                    b = s.recv(4096)
                    if not b:
                        break
                    data += b
            finally:
                s.close()
            self.assertIn(b"200", data.split(b"\r\n", 1)[0])
            self.assertTrue(data.endswith(b"hi"))
            self.assertEqual(px.log[0]["kind"], "connect")
            self.assertEqual(px.log[0]["target"], "target.test:80")

    def test_407_and_credentials(self):
        with ConnectProxy(auth="user:secretpw") as px:
            bare = f"http://127.0.0.1:{px.port}"
            with self.assertRaises(HiveError) as cm:
                netproxy.open_tunnel(bare, "x.test", 443, timeout=5)
            e = cm.exception
            self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]),
                             ("unavailable", "proxy_auth", False))
            self.assertIn("network.proxy", e.hint)
            wrong = f"http://user:wrongpw@127.0.0.1:{px.port}"
            with self.assertRaises(HiveError) as cm:
                netproxy.open_tunnel(wrong, "x.test", 443, timeout=5)
            text = repr(cm.exception.to_dict())
            self.assertNotIn("wrongpw", text)
            self.assertIn("***", cm.exception.details["proxy"])
        # right credentials pass (the target refuses to connect -> 502 from the proxy, not 407)
        with ConnectProxy(auth="user:secretpw") as px:
            with self.assertRaises(HiveError) as cm:
                netproxy.open_tunnel(f"http://user:secretpw@127.0.0.1:{px.port}", "127.0.0.1", helpers.free_port(),
                                     timeout=5)
            self.assertEqual((cm.exception.details["reason"], cm.exception.details["status"]), ("proxy", 502))
            self.assertEqual(px.log[0]["headers"]["proxy-authorization"], "Basic dXNlcjpzZWNyZXRwdw==")
            self.assertNotIn("secretpw", repr(cm.exception.to_dict()))

    def test_403(self):
        with ConnectProxy(refuse_connect=403) as px:
            with self.assertRaises(HiveError) as cm:
                netproxy.open_tunnel(px.url, "x.test", 443, timeout=5)
            self.assertEqual((cm.exception.details["reason"], cm.exception.details["status"]), ("proxy", 403))
            self.assertIn("403", cm.exception.message)

    def test_unreachable_hint(self):
        port = helpers.free_port()
        with self.assertRaises(HiveError) as cm:
            netproxy.open_tunnel(f"http://u:pw@127.0.0.1:{port}", "x.test", 443, timeout=2)
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "proxy", False))
        self.assertIn(f"127.0.0.1:{port} is not reachable; is Clash/v2rayN running? (network.proxy)", e.hint)
        self.assertNotIn("pw", e.message + (e.hint or "") + repr(e.details).replace("***", ""))

    def _raw_proxy(self, reply: bytes) -> Any:
        ls = socket.socket()
        ls.bind(("127.0.0.1", 0))
        ls.listen(1)

        def run() -> None:
            try:
                c, _ = ls.accept()
                c.recv(65536)
                c.sendall(reply)
                c.close()
            except OSError:
                pass
        threading.Thread(target=run, daemon=True).start()
        self.addCleanup(ls.close)
        return ls.getsockname()[1]

    def test_oversized_headers(self):
        port = self._raw_proxy(b"HTTP/1.1 200 OK\r\n" + b"X-Pad: " + b"a" * 20000 + b"\r\n\r\n")
        with self.assertRaises(HiveError) as cm:
            netproxy.open_tunnel(f"http://127.0.0.1:{port}", "x.test", 443, timeout=5)
        self.assertEqual(cm.exception.details["reason"], "proxy")
        self.assertIn("oversized", cm.exception.message)

    def test_tunnel_keeps_early_bytes(self):
        # bytes after the header block belong to the tunnel and must not be swallowed
        port = self._raw_proxy(b"HTTP/1.1 200 Connection established\r\n\r\nEARLY")
        s = netproxy.open_tunnel(f"http://127.0.0.1:{port}", "x.test", 443, timeout=5)
        try:
            s.settimeout(5)
            self.assertEqual(s.recv(5), b"EARLY")
        finally:
            s.close()


class TestRedactionAndChildEnv(ProxyCase):
    def test_describe_and_repr_hide_credentials(self):
        s = load({"proxy": "http://alice:topsecret@127.0.0.1:7890"}, {})
        self.assertEqual(s.describe(), "http://***@127.0.0.1:7890 (config)")
        self.assertNotIn("topsecret", repr(s))
        self.assertNotIn("topsecret", s.describe())
        self.assertNotIn("topsecret", " ".join(self.log.lines))
        self.assertEqual(s.to_dict()["https"], "http://alice:topsecret@127.0.0.1:7890")   # 0600 specs only

    def test_child_proxy_env(self):
        s = netproxy.ProxySettings("http://u:p@127.0.0.1:7890", "http://u:p@127.0.0.1:7890", ("x",), "env")
        env = netproxy.child_proxy_env(s, ["corp.lan"])
        for k in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
            self.assertEqual(env[k], "http://u:p@127.0.0.1:7890")
        no = env["NO_PROXY"].split(",")
        self.assertEqual(env["no_proxy"], env["NO_PROXY"])
        for x in ("localhost", "127.0.0.1", "::1", ".local", ".lan", ".ts.net", "10.0.0.0/8", "172.16.0.0/12",
                  "192.168.0.0/16", "100.64.0.0/10", "corp.lan"):
            self.assertIn(x, no)
        self.assertEqual(netproxy.child_proxy_env(netproxy.ProxySettings(None, None, (), "none")), {})
        only = netproxy.child_proxy_env(netproxy.ProxySettings("http://h:1", None, (), "env"))
        self.assertNotIn("HTTPS_PROXY", only)


class TestWslHost(ProxyCase):
    def _route(self, gw_hex: str) -> str:
        d = tempfile.mkdtemp(prefix="hiveroute-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = os.path.join(d, "route")
        with open(p, "w") as f:
            f.write("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n")
            f.write("eth0\t0010A8C0\t00000000\t0001\t0\t0\t0\t00F0FFFF\t0\t0\t0\n")
            f.write(f"eth0\t00000000\t{gw_hex}\t0003\t0\t0\t0\t00000000\t0\t0\t0\n")
        return p

    def test_substitution_and_refresh(self):
        p = self._route("0110A8C0")          # 192.168.16.1 little-endian
        clock = [5000.0]
        with mock.patch.object(netproxy, "_ROUTE_FILE", p), mock.patch.object(netproxy, "_is_wsl", return_value=True), \
                mock.patch.object(netproxy, "_now", lambda: clock[0]):
            netproxy.clear_cache()
            s = netproxy.load_proxy_settings({"proxy": "http://wsl-host:7890"}, {})
            self.assertEqual(s.https, "http://192.168.16.1:7890")
            self.assertEqual(netproxy.child_proxy_env(s)["HTTPS_PROXY"], "http://192.168.16.1:7890")
            with open(p, "w") as f:      # Windows rebooted: new host address
                f.write("Iface\tDestination\tGateway\n")
                f.write("eth0\t00000000\t0120A8C0\t0003\n")
            self.assertEqual(netproxy.load_proxy_settings({"proxy": "http://wsl-host:7890"}, {}).https,
                             "http://192.168.16.1:7890")          # cached
            clock[0] += 61
            s2 = netproxy.load_proxy_settings({"proxy": "http://wsl-host:7890"}, {})
            self.assertEqual(s2.https, "http://192.168.32.1:7890")
            self.assertEqual(netproxy.child_proxy_env(s2)["HTTP_PROXY"], "http://192.168.32.1:7890")

    def test_outside_wsl_is_a_config_error(self):
        with mock.patch.object(netproxy, "_is_wsl", return_value=False):
            with self.assertRaises(HiveError) as cm:
                load({"proxy": "http://wsl-host:7890"}, {})
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("wsl-host", cm.exception.message)


class FakeWinreg(types.ModuleType):
    HKEY_CURRENT_USER = 1

    def __init__(self, values: Dict[str, Any]):
        super().__init__("winreg")
        self.values = values

    class _Key:
        def __enter__(self) -> "FakeWinreg._Key":
            return self

        def __exit__(self, *a: Any) -> None:
            return None

    def OpenKey(self, root: Any, path: str) -> "FakeWinreg._Key":  # noqa: N802
        return FakeWinreg._Key()

    def QueryValueEx(self, key: Any, name: str) -> Any:  # noqa: N802
        if name not in self.values:
            raise OSError("missing")
        return self.values[name], 1


class TestWindowsRegistry(ProxyCase):
    def _load(self, values: Dict[str, Any]) -> netproxy.ProxySettings:
        netproxy.clear_cache()
        with mock.patch.dict(sys.modules, {"winreg": FakeWinreg(values)}), \
                mock.patch.object(netproxy, "_IS_WINDOWS", True), \
                mock.patch("urllib.request.getproxies", side_effect=AssertionError("getproxies called")):
            return netproxy.load_proxy_settings({}, {})

    def test_bare_host_port(self):
        s = self._load({"ProxyEnable": 1, "ProxyServer": "127.0.0.1:7890", "ProxyOverride": "<local>;*.corp.com"})
        self.assertEqual((s.http, s.https, s.source), ("http://127.0.0.1:7890", "http://127.0.0.1:7890", "system"))
        self.assertIn("<local>", s.no_proxy)
        self.assertTrue(netproxy.bypass(s, "intranet"))
        self.assertTrue(netproxy.bypass(s, "a.corp.com"))

    def test_per_scheme(self):
        s = self._load({"ProxyEnable": 1, "ProxyServer": "http=127.0.0.1:7890;https=127.0.0.1:7890"})
        self.assertEqual((s.http, s.https), ("http://127.0.0.1:7890", "http://127.0.0.1:7890"))

    def test_socks_only(self):
        s = self._load({"ProxyEnable": 1, "ProxyServer": "socks=127.0.0.1:7891"})
        self.assertEqual((s.http, s.https, s.source), (None, None, "none"))
        self.assertEqual(len([x for x in self.log.lines if "socks" in x]), 1)

    def test_disabled(self):
        s = self._load({"ProxyEnable": 0, "ProxyServer": "127.0.0.1:7890"})
        self.assertEqual(s.source, "none")


if __name__ == "__main__":
    unittest.main()
