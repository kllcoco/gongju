"""WP3a tests: netutil.NetGuard (§5.4 NetGuard rules), guarded_request (§11.2, C1–C4), tcp_check (design §21.2
netutil/NetGuard row and the guarded_request part of the files/net/model row)."""
from __future__ import annotations

import json
import subprocess
import sys
import unittest
from typing import Any, Dict, List, Optional
from unittest import mock

from hive import netproxy, netutil
from hive.errors import HiveError
from hive.netutil import AuthSpec, NetGuard
from tests import helpers, wp1util
from tests.fixtures.connect_proxy import ConnectProxy, simple_open_tunnel

PUBLIC_IP = "93.184.216.34"
NO_PROXY = netproxy.ProxySettings(None, None, netproxy.ALWAYS_NO_PROXY, "none")


def guard(**kw: Any) -> NetGuard:
    """A real NetGuard (default allow loopback+public)."""
    args: Dict[str, Any] = dict(http_allow=["loopback", "public"], http_deny=[], download_allow=["loopback", "public"],
                                allow_metadata_ip=False, proxies=NO_PROXY)
    args.update(kw)
    return NetGuard(**args)


class Resolver:
    """Patch for netutil._resolve: a fixed name -> IPs table (unknown names do not resolve)."""

    def __init__(self, table: Dict[str, List[str]]):
        self.table = {k.lower(): v for k, v in table.items()}
        self.calls: List[str] = []

    def __call__(self, host: str, port: int, timeout: float = 5.0) -> List[str]:
        self.calls.append(host)
        if host == "localhost":
            return ["127.0.0.1"]
        return list(self.table.get(host.lower(), []))


def resolving(table: Dict[str, List[str]]) -> Any:
    return mock.patch.object(netutil, "_resolve", Resolver(table))


def code_rule(fn: Any) -> tuple:
    try:
        fn()
    except HiveError as e:
        return e.code, e.details.get("rule") or e.details.get("reason")
    return None, None


class TestNetGuardRules(unittest.TestCase):
    def test_scheme_and_userinfo(self):
        g = guard()
        for url in ("ftp://example.com/", "file:///etc/passwd", "gopher://x/"):
            self.assertEqual(code_rule(lambda: g.check_url(url, purpose="http"))[0], "bad_request")
        with self.assertRaises(HiveError) as cm:
            g.check_url("http://user:pw@127.0.0.1/", purpose="http")
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("auth_env", cm.exception.message)

    def test_literal_categories(self):
        g = guard()
        t = g.check_url("http://127.0.0.1:8000/v1?x=1", purpose="http")
        self.assertEqual((t.category, t.route, t.ip, t.port, t.path), ("loopback", "direct", "127.0.0.1", 8000,
                                                                       "/v1?x=1"))
        self.assertEqual(g.check_url(f"https://{PUBLIC_IP}/", purpose="http").category, "public")
        cases = {"http://10.1.2.3/": "net_allow", "http://192.168.1.1/": "net_allow",
                 "http://169.254.169.254/latest": "ssrf", "http://[fd00:ec2::254]/": "ssrf",
                 "http://100.100.100.200/": "ssrf", "http://169.254.1.1/": "ssrf", "http://0.0.0.0/": "ssrf",
                 "http://198.18.3.4/": "ssrf", "http://[::ffff:169.254.169.254]/": "ssrf",
                 "http://[64:ff9b::a9fe:a9fe]/": "ssrf", "http://224.0.0.1/": "ssrf"}
        for url, rule in cases.items():
            with self.subTest(url=url):
                self.assertEqual(code_rule(lambda: g.check_url(url, purpose="http")), ("policy_denied", rule))
        # IPv4-mapped loopback is loopback
        self.assertEqual(g.check_url("http://[::ffff:127.0.0.1]/", purpose="http").category, "loopback")

    def test_metadata_names(self):
        g = guard()
        for name in ("metadata", "metadata.google.internal", "instance-data", "instance-data.ec2.internal"):
            self.assertEqual(code_rule(lambda: g.check_url(f"http://{name}/", purpose="http")),
                             ("policy_denied", "ssrf"))
        g2 = guard(allow_metadata_ip=True, http_allow=["loopback", "public", "169.254.169.254"])
        self.assertEqual(g2.check_url("http://169.254.169.254/", purpose="http").category, "metadata")

    def test_dns_private_denied_under_public(self):
        g = guard()
        with resolving({"intranet.example.com": ["10.0.0.5"], "mixed.example.com": [PUBLIC_IP, "192.168.1.9"],
                        "pub.example.com": [PUBLIC_IP]}):
            self.assertEqual(code_rule(lambda: g.check_url("http://intranet.example.com/", purpose="http")),
                             ("policy_denied", "net_allow"))
            self.assertEqual(code_rule(lambda: g.check_url("http://mixed.example.com/", purpose="http")),
                             ("policy_denied", "net_allow"))      # every IP must be allowed
            t = g.check_url("http://pub.example.com/", purpose="http")
            self.assertEqual((t.ip, t.route, t.category, t.host), (PUBLIC_IP, "direct", "public", "pub.example.com"))

    def test_deny_cidr_blocks_resolved_name(self):
        g = guard(http_deny=["93.184.0.0/16"])
        with resolving({"blocked.example.com": [PUBLIC_IP], "ok.example.com": ["1.1.1.1"]}):
            self.assertEqual(code_rule(lambda: g.check_url("http://blocked.example.com/", purpose="http")),
                             ("policy_denied", "http_deny"))
            self.assertEqual(g.check_url("http://ok.example.com/", purpose="http").ip, "1.1.1.1")
        self.assertEqual(code_rule(lambda: g.check_url(f"http://{PUBLIC_IP}/", purpose="http")),
                         ("policy_denied", "http_deny"))

    def test_cidr_and_host_glob_allow(self):
        g = guard(http_allow=["loopback", "10.0.0.0/8", "*.corp.example"])
        self.assertEqual(g.check_url("http://10.9.8.7/", purpose="http").category, "private")
        self.assertEqual(code_rule(lambda: g.check_url("http://192.168.1.1/", purpose="http")),
                         ("policy_denied", "net_allow"))
        with resolving({"nas.corp.example": ["192.168.7.7"], "x.other.example": [PUBLIC_IP],
                        "meta.corp.example": ["169.254.169.254"]}):
            self.assertEqual(g.check_url("http://nas.corp.example/", purpose="http").ip, "192.168.7.7")
            self.assertEqual(code_rule(lambda: g.check_url("http://x.other.example/", purpose="http")),
                             ("policy_denied", "net_allow"))
            # a host glob admits whatever the name resolves to, except metadata/linklocal/reserved
            self.assertEqual(code_rule(lambda: g.check_url("http://meta.corp.example/", purpose="http")),
                             ("policy_denied", "ssrf"))

    def test_http_deny_host_port(self):
        g = guard(http_deny=["127.0.0.1:9090", "[::1]:9090", "localhost:2375"])
        self.assertEqual(code_rule(lambda: g.check_url("http://127.0.0.1:9090/configs", purpose="http")),
                         ("policy_denied", "http_deny"))
        self.assertEqual(g.check_url("http://127.0.0.1:9091/", purpose="http").port, 9091)
        self.assertEqual(code_rule(lambda: g.check_url("http://[::1]:9090/", purpose="http")),
                         ("policy_denied", "http_deny"))
        self.assertEqual(code_rule(lambda: g.check_url("http://localhost:2375/containers/json", purpose="http")),
                         ("policy_denied", "http_deny"))
        self.assertEqual(g.check_url("http://localhost:2376/", purpose="http").ip, "127.0.0.1")
        # also for purpose model (loopback always allowed there, but http_deny still applies)
        self.assertEqual(code_rule(lambda: g.check_url("http://127.0.0.1:9090/", purpose="model")),
                         ("policy_denied", "http_deny"))

    def test_model_and_health_loopback_always_allowed(self):
        g = guard(http_allow=["public"])
        self.assertEqual(code_rule(lambda: g.check_url("http://127.0.0.1:8000/", purpose="http")),
                         ("policy_denied", "net_allow"))
        for purpose in ("model", "health"):
            self.assertEqual(g.check_url("http://127.0.0.1:8000/v1", purpose=purpose).category, "loopback")
        self.assertEqual(code_rule(lambda: g.check_url("http://10.0.0.1/", purpose="model")),
                         ("policy_denied", "net_allow"))

    def test_own_ports(self):
        g = guard(own_ports=[7701])
        self.assertEqual(code_rule(lambda: g.check_url("http://127.0.0.1:7701/v1/call", purpose="http")),
                         ("policy_denied", "own_port"))
        self.assertEqual(code_rule(lambda: g.check_url("http://localhost:7701/", purpose="http")),
                         ("policy_denied", "own_port"))
        self.assertEqual(g.check_url("http://127.0.0.1:7701/v1/health", purpose="health").port, 7701)

    def test_download_redirect_allow(self):
        g = guard(download_allow=["modelscope.cn", "*.modelscope.cn"],
                  download_redirect_allow=["*.aliyuncs.com"])
        with resolving({"x.oss-cn-hangzhou.aliyuncs.com": [PUBLIC_IP], "modelscope.cn": ["1.2.3.4"]}):
            url = "https://x.oss-cn-hangzhou.aliyuncs.com/f"
            self.assertEqual(code_rule(lambda: g.check_url(url, purpose="download")),
                             ("policy_denied", "net_allow"))
            self.assertEqual(g.check_url(url, purpose="download", redirect=True).host,
                             "x.oss-cn-hangzhou.aliyuncs.com")
            # the redirect list is only for downloads
            self.assertEqual(code_rule(lambda: guard(http_allow=["loopback"],
                                                     download_redirect_allow=["*.aliyuncs.com"]).check_url(
                url, purpose="http", redirect=True)), ("policy_denied", "net_allow"))
            self.assertEqual(g.check_url("https://modelscope.cn/x", purpose="download", method="POST").ip, "1.2.3.4")

    def test_fakeip(self):
        g = guard()
        with resolving({"fake.example.com": ["198.18.0.7", "198.18.0.9"], "mix.example.com": ["198.18.0.7",
                                                                                              "10.0.0.1"]}):
            t = g.check_url("https://fake.example.com/", purpose="http")
            self.assertEqual((t.category, t.route, t.ip), ("fakeip", "direct", "198.18.0.7"))
            self.assertEqual(code_rule(lambda: g.check_url("https://mix.example.com/", purpose="http")),
                             ("policy_denied", "ssrf"))
        g2 = guard(http_allow=["loopback"])
        with resolving({"fake.example.com": ["198.18.0.7"]}):
            self.assertEqual(code_rule(lambda: g2.check_url("https://fake.example.com/", purpose="http")),
                             ("policy_denied", "net_allow"))   # fake-ip names count as public

    def test_unresolvable(self):
        g = guard()
        with resolving({}):
            self.assertEqual(code_rule(lambda: g.check_url("https://nowhere.example.com/", purpose="http")),
                             ("unavailable", "dns"))
            px = netproxy.ProxySettings("http://127.0.0.1:7890", "http://127.0.0.1:7890", (), "config")
            gp = guard(proxies=px)
            with mock.patch.object(netproxy, "proxy_for", return_value="http://127.0.0.1:7890"):
                t = gp.check_url("https://blocked-dns.example.com/", purpose="http")
                self.assertEqual((t.route, t.ip, t.proxy), ("proxy", None, "http://127.0.0.1:7890"))
                # local names are never proxied
                self.assertEqual(code_rule(lambda: gp.check_url("http://nas.lan/", purpose="http")),
                                 ("unavailable", "dns"))
                # public not allowed and no glob -> no proxy fallback
                gl = guard(http_allow=["loopback"], proxies=px)
                self.assertEqual(code_rule(lambda: gl.check_url("https://blocked-dns.example.com/",
                                                                purpose="http")), ("unavailable", "dns"))
                gg = guard(http_allow=["loopback", "*.example.com"], proxies=px)
                self.assertEqual(gg.check_url("https://blocked-dns.example.com/", purpose="http").route, "proxy")

    def test_proxy_routes(self):
        px = netproxy.ProxySettings("http://127.0.0.1:7890", "http://127.0.0.1:7890", (), "config")
        g = guard(proxies=px)
        calls = []

        def proxy_for(settings: Any, scheme: str, host: str, ips: Any = ()) -> Optional[str]:
            calls.append((scheme, host, list(ips)))
            return "http://127.0.0.1:7890"
        with mock.patch.object(netproxy, "proxy_for", proxy_for), \
                resolving({"huggingface.co": [PUBLIC_IP], "nas.lan": ["192.168.1.5"],
                           "fake.example.com": ["198.18.0.3"]}):
            t = g.check_url("https://huggingface.co/api", purpose="download")
            self.assertEqual((t.route, t.ip, t.pin_ip, t.host), ("proxy", PUBLIC_IP, False, "huggingface.co"))
            self.assertEqual(calls[-1], ("https", "huggingface.co", [PUBLIC_IP]))
            self.assertEqual(g.check_url("https://fake.example.com/", purpose="http").route, "proxy")
            # loopback / private / literal private never proxied
            for url in ("http://127.0.0.1:8000/", "http://localhost/"):
                self.assertEqual(g.check_url(url, purpose="http").route, "direct")
            gp = guard(proxies=px, http_allow=["loopback", "private", "public"])
            for url in ("http://10.1.1.1/", "http://nas.lan/"):
                self.assertEqual(gp.check_url(url, purpose="http").route, "direct")
            # health checks never use a proxy
            self.assertEqual(g.check_url(f"http://{PUBLIC_IP}/", purpose="health").route, "direct")
            # strict_dns -> CONNECT to the vetted IP (pin_ip), but not to a fake-ip
            gs = guard(proxies=netproxy.ProxySettings("http://127.0.0.1:7890", "http://127.0.0.1:7890", (),
                                                      "config", strict_dns=True))
            t = gs.check_url("https://huggingface.co/", purpose="http")
            self.assertEqual((t.route, t.pin_ip, t.ip), ("proxy", True, PUBLIC_IP))
            self.assertFalse(gs.check_url("https://fake.example.com/", purpose="http").pin_ip)

    def test_round_trip(self):
        px = netproxy.ProxySettings("http://u:p@127.0.0.1:7890", "http://u:p@127.0.0.1:7890", ("corp",), "env",
                                    strict_dns=True, ca_file="/x/ca.pem")
        g = guard(http_deny=["127.0.0.1:9090"], own_ports=[7701], download_redirect_allow=["*.cdn.example"],
                  proxies=px)
        d = json.loads(json.dumps(g.to_dict()))
        g2 = NetGuard.from_dict(d)
        self.assertEqual(g2.to_dict(), g.to_dict())
        self.assertEqual(g2.proxies, px)
        self.assertEqual(code_rule(lambda: g2.check_url("http://127.0.0.1:9090/", purpose="http")),
                         ("policy_denied", "http_deny"))

    def test_netutil_imports_no_wp1_module(self):
        code = ("import sys; import hive.netutil, hive.netproxy, hive.fsops; "
                "print(','.join(sorted(m for m in sys.modules if m.startswith('hive.'))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             cwd=str(helpers.Path(netutil.__file__).parent.parent))
        mods = set(out.stdout.strip().split(","))
        self.assertTrue({"hive.netutil", "hive.netproxy"} <= mods, out.stderr)
        self.assertLessEqual(mods, {"hive.netutil", "hive.netproxy", "hive.util", "hive.errors", "hive.fsops"})


class TestGuardedRequest(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = helpers.StubHTTPServer().start()
        self.srv._httpd.handle_error = lambda *a: None   # a client that timed out closes early: no traceback noise
        self.addCleanup(self.srv.stop)

    def test_get_post_and_max_bytes(self):
        self.srv.add("GET", "/hello", 200, b"x" * 1000, {"X-Test": "1"})
        g = guard()
        r = netutil.guarded_request("GET", self.srv.url + "/hello", guard=g, purpose="http", max_bytes=100)
        self.assertEqual((r.status, len(r.body), r.truncated, r.route), (200, 100, True, "direct"))
        self.assertEqual(r.headers.get("X-Test"), "1")
        r = netutil.guarded_request("GET", self.srv.url + "/hello", guard=g, purpose="http", max_bytes=5000)
        self.assertEqual((len(r.body), r.truncated), (1000, False))
        self.srv.add_handler("POST", "/echo", lambda req: (201, {}, req["body"]))
        r = netutil.guarded_request("POST", self.srv.url + "/echo", guard=g, purpose="http", body=b'{"a":1}',
                                    headers={"Content-Type": "application/json"})
        self.assertEqual((r.status, r.body), (201, b'{"a":1}'))
        req = self.srv.requests[-1]
        self.assertEqual(req["headers"].get("Host"), f"127.0.0.1:{self.srv.port}")

    def test_dns_pinning_with_stubbed_resolver(self):
        self.srv.add("GET", "/p", 200, "pinned")
        with resolving({"pinned.test": ["127.0.0.1"]}):
            r = netutil.guarded_request("GET", f"http://pinned.test:{self.srv.port}/p", guard=guard(),
                                        purpose="http")
        self.assertEqual(r.body, b"pinned")
        self.assertEqual(self.srv.requests[-1]["headers"].get("Host"), f"pinned.test:{self.srv.port}")

    def test_redirects_rechecked(self):
        self.srv.add("GET", "/to-meta", 302, b"", {"Location": "http://169.254.169.254/latest/meta-data"})
        self.srv.add("GET", "/to-ok", 302, b"", {"Location": "/ok"})
        self.srv.add("GET", "/ok", 200, "fine")
        self.srv.add("POST", "/see-other", 303, b"", {"Location": "/ok"})
        g = guard()
        self.assertEqual(code_rule(lambda: netutil.guarded_request("GET", self.srv.url + "/to-meta", guard=g,
                                                                   purpose="http")), ("policy_denied", "ssrf"))
        r = netutil.guarded_request("GET", self.srv.url + "/to-ok", guard=g, purpose="http")
        self.assertEqual((r.status, r.body, r.url), (200, b"fine", self.srv.url + "/ok"))
        r = netutil.guarded_request("GET", self.srv.url + "/to-ok", guard=g, purpose="http", follow_redirects=False)
        self.assertEqual(r.status, 302)
        r = netutil.guarded_request("POST", self.srv.url + "/see-other", guard=g, purpose="http", body=b"x")
        self.assertEqual((r.status, self.srv.requests[-1]["method"]), (200, "GET"))
        # loop -> too many redirects
        self.srv.add("GET", "/loop", 302, b"", {"Location": "/loop"})
        self.assertEqual(code_rule(lambda: netutil.guarded_request("GET", self.srv.url + "/loop", guard=g,
                                                                   purpose="http"))[0], "unavailable")

    def test_proxied_public_redirect_to_loopback_denied(self):
        g = guard(http_allow=["public"])
        self.srv.add("GET", "/r", 302, b"", {"Location": "http://127.0.0.1:1/x"})
        # first hop: loopback allowed only through the stubbed guard below; hop 2 re-vetted by the real NetGuard
        fake = helpers.FakeNetGuard()
        calls = []

        class Mixed:
            def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> Any:
                calls.append((url, method, redirect))
                return (g if redirect else fake).check_url(url, purpose=purpose, method=method, redirect=redirect)
        self.assertEqual(code_rule(lambda: netutil.guarded_request("GET", self.srv.url + "/r", guard=Mixed(),
                                                                   purpose="http")), ("policy_denied", "net_allow"))
        self.assertEqual([c[2] for c in calls], [False, True])

    def test_method_and_redirect_passed_to_guard(self):
        self.srv.add("PUT", "/a", 307, b"", {"Location": "/b"})
        self.srv.add("PUT", "/b", 200, "ok")
        fg = helpers.FakeNetGuard()
        r = netutil.guarded_request("PUT", self.srv.url + "/a", guard=fg, purpose="http", body=b"1")
        self.assertEqual(r.status, 200)
        self.assertEqual([(c[2], c[3]) for c in fg.calls], [("PUT", False), ("PUT", True)])
        self.assertEqual(self.srv.requests[-1]["body"], b"1")

    def test_credentials(self):
        g = guard()
        loop = AuthSpec(name="LOCAL_KEY", header="Authorization", value="Bearer s3cret-value", hosts=("loopback",))
        hf = AuthSpec(name="HF_TOKEN", header="Authorization", value="Bearer hf_x", hosts=("huggingface.co",))
        self.srv.add("GET", "/a", 200, "ok")
        with resolving({"public.example.com": [PUBLIC_IP], "cdn.test": ["127.0.0.1"]}):
            # C1: secret over plain http to a public host
            self.assertEqual(code_rule(lambda: netutil.guarded_request(
                "GET", "http://public.example.com/", guard=g, purpose="http", auth=hf)),
                ("policy_denied", "plaintext_credentials"))
            # C4: verify_tls=false with a secret
            self.assertEqual(code_rule(lambda: netutil.guarded_request(
                "GET", "https://public.example.com/", guard=g, purpose="http", auth=hf, verify_tls=False)),
                ("policy_denied", "unverified_tls"))
            # first hop not bound -> secret_host
            self.assertEqual(code_rule(lambda: netutil.guarded_request(
                "GET", self.srv.url + "/a", guard=g, purpose="http", auth=hf)), ("policy_denied", "secret_host"))
            # bound host: header sent; redirected to another host: dropped (C2), user credential headers too
            self.srv.add("GET", "/redir", 302, b"", {"Location": f"http://cdn.test:{self.srv.port}/a"})
            r = netutil.guarded_request("GET", f"http://localhost:{self.srv.port}/redir", guard=g, purpose="http",
                                        auth=loop, headers={"Cookie": "c=1", "X-Api-Key": "k", "Accept": "x/y"})
            self.assertEqual(r.status, 200)
            first, second = self.srv.requests[-2], self.srv.requests[-1]
            self.assertEqual(first["headers"].get("Authorization"), "Bearer s3cret-value")
            self.assertEqual(first["headers"].get("Cookie"), "c=1")
            self.assertIsNone(second["headers"].get("Authorization"))
            self.assertIsNone(second["headers"].get("Cookie"))
            self.assertIsNone(second["headers"].get("X-Api-Key"))
            self.assertEqual(second["headers"].get("Accept"), "x/y")
            self.assertEqual(second["headers"].get("Host"), f"cdn.test:{self.srv.port}")
        self.assertNotIn("s3cret", repr(loop))

    def test_header_rules(self):
        g = guard()
        for h in ("Host", "Content-Length", "Transfer-Encoding", "Connection", "Upgrade", "TE", "Keep-Alive",
                  "Trailer", "Proxy-Authorization"):
            with self.subTest(header=h):
                self.assertEqual(code_rule(lambda: netutil.guarded_request(
                    "GET", self.srv.url + "/", guard=g, purpose="http", headers={h: "x"}))[0], "bad_request")
        auth = AuthSpec(name="K", header="Authorization", value="Bearer abcdefgh", hosts=("loopback",))
        self.assertEqual(code_rule(lambda: netutil.guarded_request(
            "GET", self.srv.url + "/", guard=g, purpose="http", headers={"Authorization": "x"}, auth=auth))[0],
            "bad_request")
        self.assertEqual(code_rule(lambda: netutil.guarded_request(
            "GET", self.srv.url + "/", guard=g, purpose="http", headers={"X-A": "1\r\nInjected: 1"}))[0],
            "bad_request")

    def test_errors(self):
        g = guard()
        port = helpers.free_port()
        with self.assertRaises(HiveError) as cm:
            netutil.guarded_request("GET", f"http://127.0.0.1:{port}/", guard=g, purpose="http", timeout=3)
        e = cm.exception
        self.assertEqual((e.code, e.details["reason"], e.details["maybe_executed"]), ("unavailable", "connect",
                                                                                      False))
        self.assertIn(f"127.0.0.1:{port}", e.hint)
        self.srv.add("GET", "/slow", 200, "late", delay=2.0)
        with self.assertRaises(HiveError) as cm:
            netutil.guarded_request("GET", self.srv.url + "/slow", guard=g, purpose="http", timeout=0.5)
        self.assertEqual((cm.exception.code, cm.exception.details["maybe_executed"]), ("timeout", True))

    def test_absolute_form_through_proxy(self):
        self.srv.add("GET", "/v", 200, "via proxy")
        self.srv.add("POST", "/v", 200, "posted")
        with ConnectProxy(routes={f"public.test:{self.srv.port}": ("127.0.0.1", self.srv.port),
                                  f"{PUBLIC_IP}:{self.srv.port}": ("127.0.0.1", self.srv.port)}) as px, \
                resolving({"public.test": [PUBLIC_IP]}), \
                mock.patch.object(netproxy, "proxy_for", return_value=px.url):
            g = guard()
            r = netutil.guarded_request("GET", f"http://public.test:{self.srv.port}/v", guard=g, purpose="http")
            self.assertEqual((r.body, r.route), (b"via proxy", "proxy"))
            self.assertEqual(px.log[-1]["kind"], "absolute")
            self.assertEqual(px.log[-1]["target"], f"http://public.test:{self.srv.port}/v")
            self.assertEqual(self.srv.requests[-1]["headers"].get("Host"), f"public.test:{self.srv.port}")
            # rule 5c through a real EffectivePolicy: a gated identity's POST is pinned to the vetted IP, its GET
            # still names the host; an operator's POST is unchanged
            with wp1util.FakeHome() as fh:
                pol = wp1util.build_policy(fh, guard=NetGuard)
                gated = pol.effective(wp1util.READNET, paused=False)
                op = pol.effective(wp1util.OPERATOR, paused=False)
                r = netutil.guarded_request("POST", f"http://public.test:{self.srv.port}/v", guard=gated,
                                            purpose="http", body=b"1")
                self.assertEqual(r.body, b"posted")
                self.assertEqual(px.log[-1]["target"], f"http://{PUBLIC_IP}:{self.srv.port}/v")
                self.assertEqual(self.srv.requests[-1]["headers"].get("Host"), f"public.test:{self.srv.port}")
                netutil.guarded_request("GET", f"http://public.test:{self.srv.port}/v", guard=gated, purpose="http")
                self.assertEqual(px.log[-1]["target"], f"http://public.test:{self.srv.port}/v")
                netutil.guarded_request("POST", f"http://public.test:{self.srv.port}/v", guard=op, purpose="http",
                                        body=b"1")
                self.assertEqual(px.log[-1]["target"], f"http://public.test:{self.srv.port}/v")

    def test_proxy_auth_and_unreachable(self):
        self.srv.add("GET", "/v", 200, "ok")
        with ConnectProxy(routes={f"public.test:{self.srv.port}": ("127.0.0.1", self.srv.port)},
                          auth="u:pw") as px, resolving({"public.test": [PUBLIC_IP]}):
            url = f"http://public.test:{self.srv.port}/v"
            with mock.patch.object(netproxy, "proxy_for", return_value=px.url):
                r = netutil.guarded_request("GET", url, guard=guard(), purpose="http")
                self.assertEqual(r.body, b"ok")
                self.assertTrue(px.log[-1]["headers"]["proxy-authorization"].startswith("Basic "))
            bare = f"http://127.0.0.1:{px.port}"
            with mock.patch.object(netproxy, "proxy_for", return_value=bare):
                self.assertEqual(code_rule(lambda: netutil.guarded_request("GET", url, guard=guard(),
                                                                           purpose="http")),
                                 ("unavailable", "proxy_auth"))
            dead = f"http://127.0.0.1:{helpers.free_port()}"
            with mock.patch.object(netproxy, "proxy_for", return_value=dead):
                with self.assertRaises(HiveError) as cm:
                    netutil.guarded_request("GET", url, guard=guard(), purpose="http")
                self.assertEqual(cm.exception.details["reason"], "proxy")
                self.assertIn("Clash", cm.exception.hint)

    def test_connect_tunnel_paths(self):
        """https and credentialed http go through netproxy.open_tunnel (CONNECT); pin_ip names the IP."""
        seen = []

        def fake_tunnel(proxy_url: str, host: str, port: int, *, timeout: float) -> Any:
            seen.append((proxy_url, host, port))
            raise netutil.unavailable("stop here", reason="proxy", maybe_executed=False)
        px = netproxy.ProxySettings("http://127.0.0.1:7890", "http://127.0.0.1:7890", (), "config")
        with resolving({"huggingface.co": [PUBLIC_IP]}), \
                mock.patch.object(netproxy, "proxy_for", return_value="http://127.0.0.1:7890"), \
                mock.patch.object(netproxy, "open_tunnel", fake_tunnel):
            code_rule(lambda: netutil.guarded_request("GET", "https://huggingface.co/x", guard=guard(proxies=px),
                                                      purpose="http"))
            self.assertEqual(seen[-1], ("http://127.0.0.1:7890", "huggingface.co", 443))
            strict = netproxy.ProxySettings("http://127.0.0.1:7890", "http://127.0.0.1:7890", (), "config",
                                            strict_dns=True)
            code_rule(lambda: netutil.guarded_request("GET", "https://huggingface.co/x",
                                                      guard=guard(proxies=strict), purpose="http"))
            self.assertEqual(seen[-1][1], PUBLIC_IP)

    def test_credentialed_http_uses_connect(self):
        """A secret-carrying http:// request through a proxy is tunnelled: the proxy never sees the header."""
        self.srv.add("GET", "/s", 200, "secret ok")
        auth = AuthSpec(name="K", header="Authorization", value="Bearer topsecret1", hosts=("private",))
        target = netutil.VettedTarget("http", "10.1.2.3", 80, "/s", "10.1.2.3", "proxy", None, "private")

        class G:
            def check_url(self, url: str, **kw: Any) -> Any:
                return target

        with ConnectProxy(routes={"10.1.2.3:80": ("127.0.0.1", self.srv.port)}) as px, \
                mock.patch.object(netproxy, "open_tunnel", simple_open_tunnel):
            object.__setattr__(target, "proxy", px.url)
            r = netutil.guarded_request("GET", "http://10.1.2.3/s", guard=G(), purpose="http", auth=auth)
            self.assertEqual(r.body, b"secret ok")
            self.assertEqual(px.log[-1]["kind"], "connect")
            self.assertNotIn("topsecret1", json.dumps(px.log))
            self.assertEqual(self.srv.requests[-1]["headers"].get("Authorization"), "Bearer topsecret1")

    def test_tcp_check(self):
        ok, lat, err = netutil.tcp_check("127.0.0.1", self.srv.port, 1.0)
        self.assertTrue(ok)
        self.assertIsNotNone(lat)
        self.assertIsNone(err)
        ok, lat, err = netutil.tcp_check("127.0.0.1", helpers.free_port(), 0.5)
        self.assertFalse(ok)
        self.assertIsNone(lat)
        self.assertTrue(err)


class TestEffectivePolicyRule5aWithRealGuard(unittest.TestCase):
    """The WP1 identity rules on top of the real NetGuard (the WP1 tests use FakeNetGuard)."""

    def test_loopback_write_and_known_endpoints(self):
        with wp1util.FakeHome() as fh, helpers.StubHTTPServer() as srv:
            srv.openai()
            srv.add("POST", "/containers/create", 201, "{}")
            eps = [{"name": "local", "base_url": f"http://127.0.0.1:{srv.port}/v1", "api_key_env": None}]
            pol = wp1util.build_policy(fh, guard=NetGuard, model_endpoints=eps)
            gated = pol.effective(wp1util.READNET, paused=False)
            self.assertEqual(code_rule(lambda: netutil.guarded_request(
                "POST", srv.url + "/containers/create", guard=gated, purpose="http", body=b"{}")),
                ("policy_denied", "loopback_write"))
            r = netutil.guarded_request("GET", srv.url + "/v1/models", guard=gated, purpose="http")
            self.assertEqual(r.status, 200)
            r = netutil.guarded_request("POST", srv.url + "/v1/chat/completions", guard=gated, purpose="model",
                                        body=b'{"messages":[]}', headers={"Content-Type": "application/json"})
            self.assertEqual(r.status, 200)
            op = pol.effective(wp1util.OPERATOR, paused=False)
            self.assertEqual(netutil.guarded_request("POST", srv.url + "/containers/create", guard=op,
                                                     purpose="http", body=b"{}").status, 201)


if __name__ == "__main__":
    unittest.main()
