"""WP3a review fixes: symbolic links in fs.delete/fs.move, guarded recursive copy, copy timeouts, exclusive create,
protected-path case keys, search masking, model service auth and audit route, netutil TLS / encoding rules."""
from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

from hive import catalog, fsops, netproxy, netutil, schema, util
from hive.actions import HANDLERS, load_all
from hive.actions import files as files_mod
from hive.actions import model as model_mod
from hive.errors import HiveError
from hive.netutil import AuthSpec, NetGuard
from tests import helpers, wp1util
from tests.fixtures.connect_proxy import ConnectProxy, simple_open_tunnel
from tests.test_files import FilesCase
from tests.test_model import FakeServices, ModelCase
from tests.test_netutil import Resolver, guard

load_all()
POSIX = os.name != "nt"


# ---------------------------------------------------------------------------------------------------------------
# fs.delete / fs.move on symbolic links (note 78)

@unittest.skipUnless(POSIX, "symlinks")
class TestLinkOps(FilesCase):
    def test_link_inside_denied_folder(self):
        keys = self.fh.touch("dotfiles/keys", "ssh-ed25519 AAA")
        self.fh.p(".ssh").mkdir()
        os.symlink(str(keys), str(self.fh.p(".ssh/authorized_keys")))
        for ident in (wp1util.OPERATOR, wp1util.FILES):
            e = self.err("fs.move", {"src": "~/.ssh/authorized_keys", "dst": "~/moved"}, ident=ident)
            self.assertEqual(e.code, "policy_denied")
            e = self.err("fs.delete", {"path": "~/.ssh/authorized_keys"}, ident=ident)
            self.assertEqual(e.code, "policy_denied")
        self.assertEqual(keys.read_text(), "ssh-ed25519 AAA")
        self.assertTrue(os.path.islink(str(self.fh.p(".ssh/authorized_keys"))))
        self.assertFalse(os.path.lexists(str(self.fh.p("moved"))))

    def test_link_to_outside_the_roots(self):
        outside = self.fh.base / "outside"
        outside.mkdir()
        tgt = outside / "hostname"
        tgt.write_text("box")
        os.symlink(str(tgt), str(self.fh.p("ln")))
        self.assertEqual(self.rule(self.err("fs.stat", {"path": "ln"}))[0], "policy_denied")
        self.assertEqual(self.err("fs.delete", {"path": "ln"}).code, "policy_denied")
        self.assertEqual(self.err("fs.move", {"src": "ln", "dst": "ln2"}).code, "policy_denied")
        self.assertTrue(os.path.islink(str(self.fh.p("ln"))))
        self.assertTrue(tgt.exists())

    def test_symlinked_bashrc_with_files_identity(self):
        real = self.fh.touch("dotfiles/bashrc", "alias x=y\n")
        os.symlink(str(real), str(self.fh.p(".bashrc")))
        e = self.err("fs.delete", {"path": "~/.bashrc"}, ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "exec_path"))
        e = self.err("fs.move", {"src": "~/.bashrc", "dst": "~/old-bashrc"}, ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "exec_path"))
        self.assertTrue(os.path.islink(str(self.fh.p(".bashrc"))))
        self.call("fs.delete", {"path": "~/.bashrc"})                 # the operator on standard may
        self.assertFalse(os.path.lexists(str(self.fh.p(".bashrc"))))
        self.assertTrue(real.exists())

    def test_symlinked_hive_home_and_ssh(self):
        os.symlink(str(self.fh.hive_home), str(self.fh.p(".hive")))
        for ident in (wp1util.OPERATOR, wp1util.FILES):
            self.assertEqual(self.err("fs.delete", {"path": "~/.hive"}, ident=ident).code, "policy_denied")
            self.assertEqual(self.err("fs.move", {"src": "~/.hive", "dst": "~/h2"}, ident=ident).code,
                             "policy_denied")
        self.assertTrue(os.path.islink(str(self.fh.p(".hive"))))
        sshreal = self.fh.p("sshreal")
        sshreal.mkdir()
        os.symlink(str(sshreal), str(self.fh.p(".ssh")))
        self.assertEqual(self.err("fs.delete", {"path": "~/.ssh"}, ident=wp1util.FILES).code, "policy_denied")
        self.assertTrue(os.path.islink(str(self.fh.p(".ssh"))))

    def test_regular_link_move(self):
        tgt = self.fh.touch("t.txt", "t")
        os.symlink("t.txt", str(self.fh.p("l1")))
        r = self.call("fs.move", {"src": "l1", "dst": "l2"})
        self.assertEqual(r["src"], str(self.fh.p("l1")))
        self.assertTrue(os.path.islink(str(self.fh.p("l2"))))
        self.assertTrue(tgt.exists())


@unittest.skipUnless(POSIX, "symlinks")
class TestLinkedRoot(FilesCase):
    def setUp(self) -> None:
        super().setUp()
        disk = self.fh.base / "disk" / "models"
        disk.mkdir(parents=True)
        (disk / "m.bin").write_text("w")
        os.symlink(str(disk), str(self.fh.p("models")))
        self.pol = wp1util.build_policy(self.fh, self.preset, secrets=self.store, policy={
            "fs": {"roots": [{"path": "~", "mode": "rw"}, {"path": str(self.fh.p("models")), "mode": "rw"}]}})
        self.disk = disk

    def test_linked_root_is_protected(self):
        self.assertEqual(self.rule(self.err("fs.delete", {"path": "~/models", "recursive": True})),
                         ("policy_denied", "protected_path"))
        self.assertEqual(self.rule(self.err("fs.move", {"src": "~/models", "dst": "~/m2"})),
                         ("policy_denied", "protected_path"))
        self.assertTrue(os.path.islink(str(self.fh.p("models"))))
        self.assertTrue((self.disk / "m.bin").exists())


class TestProtectedKeys(FilesCase):
    def test_case_insensitive_key(self):
        """macOS / WSL drive paths: PathGuard casefolds, so must the protected-path test (policy.path_key)."""
        eff = self.pol.effective(wp1util.OPERATOR, paused=False)
        ctx = helpers.make_ctx("fs.delete", runtime=self.rt, identity=wp1util.OPERATOR, policy=eff)
        other_case = Path(str(self.home).upper())
        with mock.patch.object(type(eff), "path_key",
                               lambda self_, p, follow=True: os.path.normpath(os.path.abspath(str(p))).casefold()):
            with self.assertRaises(HiveError) as cm:
                files_mod._refuse_protected(ctx, other_case, "delete", follow=False)
            self.assertEqual(cm.exception.details.get("rule"), "protected_path")
            self.assertTrue(files_mod._inside(ctx, Path(str(self.home).upper() + "/x"), self.home))


# ---------------------------------------------------------------------------------------------------------------
# recursive fs.copy: per-entry destination checks, timeouts

class TestCopyGuard(FilesCase):
    def test_entry_added_during_walk_is_not_planted(self):
        self.fh.touch("stage/readme.txt", "r")
        self.fh.p("stage/.config").mkdir(parents=True)
        e = self.err("fs.write", {"path": "~/.config/autostart/evil.desktop", "content": "x"}, ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "exec_path"))
        real = fsops.entry_allowed
        planted = []

        def inject(g: Any, path: str) -> bool:
            if not planted:
                planted.append(1)
                self.fh.touch("stage/.config/autostart/evil.desktop", "[Desktop Entry]\nExec=evil\n")
            return real(g, path)
        with mock.patch.object(fsops, "entry_allowed", inject):
            e = self.err("fs.copy", {"src": "~/stage", "dst": "~", "recursive": True, "overwrite": True},
                         ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "contains_denied"))
        self.assertFalse(self.fh.p(".config/autostart/evil.desktop").exists())

    @unittest.skipUnless(POSIX, "symlinks")
    def test_linked_folder_inside_destination(self):
        self.fh.p(".config/autostart").mkdir(parents=True)
        self.fh.p("dst").mkdir()
        os.symlink(str(self.fh.p(".config/autostart")), str(self.fh.p("dst/x")))
        self.fh.touch("src/x/evil.desktop", "x")
        e = self.err("fs.copy", {"src": "src", "dst": "dst", "recursive": True, "overwrite": True},
                     ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "contains_denied"))
        self.assertFalse(self.fh.p(".config/autostart/evil.desktop").exists())

    def test_copy_timeout_is_not_success(self):
        for i in range(30):
            self.fh.touch(f"many/f{i:02d}.txt", "x")
        real = fsops.entry_allowed

        def slow(g: Any, path: str) -> bool:
            time.sleep(0.05)
            return real(g, path)
        with mock.patch.object(fsops, "entry_allowed", slow):
            e = self.err("fs.copy", {"src": "many", "dst": "copy", "recursive": True}, timeout=1.6)
        self.assertEqual(e.code, "timeout")
        self.assertTrue(e.details["maybe_executed"])
        self.assertIn("copy incomplete", e.hint)
        self.assertLess(e.details["files"], 30)

    def test_move_across_filesystems_is_guarded(self):
        """EXDEV: a guarded copy + delete (never shutil.move); an entry the destination may not hold is refused
        and the source is kept."""
        self.fh.touch("mv/a.txt", "a")
        self.fh.touch("mv/sub/b.txt", "b")
        real_replace = os.replace

        def exdev(src: Any, dst: Any) -> None:
            if os.path.basename(str(src)) in ("mv", "mv2"):
                raise OSError(18, "Invalid cross-device link")
            real_replace(src, dst)
        with mock.patch.object(files_mod.os, "replace", exdev):
            r = self.call("fs.move", {"src": "mv", "dst": "moved"})
        self.assertEqual(r["dst"], str(self.fh.p("moved")))
        self.assertEqual(self.fh.p("moved/sub/b.txt").read_text(), "b")
        self.assertFalse(self.fh.p("mv").exists())
        # a file planted below the source during the copy is refused at its destination
        self.fh.touch("mv2/x.txt", "x")
        self.fh.p("mv2/sub").mkdir()
        self.fh.p("proj").mkdir()
        real = fsops.entry_allowed
        planted = []

        def inject(g: Any, path: str) -> bool:
            if not planted:
                planted.append(1)
                self.fh.touch("mv2/sub/.git/hooks/pre-commit", "#!/bin/sh\nevil\n")
            return real(g, path)
        with mock.patch.object(files_mod.os, "replace", exdev), mock.patch.object(fsops, "entry_allowed", inject):
            e = self.err("fs.move", {"src": "mv2", "dst": "proj/m2"}, ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "contains_denied"))
        self.assertFalse(self.fh.p("proj/m2/sub/.git/hooks/pre-commit").exists())
        self.assertTrue(self.fh.p("mv2/x.txt").exists())


# ---------------------------------------------------------------------------------------------------------------
# fs.write create, fs.search masking

class TestWriteSearch(FilesCase):
    def test_create_is_exclusive(self):
        p = self.fh.p("c.txt")
        real_make_parent = files_mod._make_parent

        def racing(path: Path, make_dirs: bool) -> None:   # another call creates the file after the existence check
            real_make_parent(path, make_dirs)
            p.write_text("first")
        with mock.patch.object(files_mod, "_make_parent", racing):
            e = self.err("fs.write", {"path": "c.txt", "content": "second", "mode": "create"})
        self.assertEqual(e.code, "conflict")
        self.assertEqual(p.read_text(), "first")
        self.assertEqual([x for x in os.listdir(str(self.home)) if x.endswith(".hive-tmp")], [])

    def test_search_masks_cut_secret(self):
        secret = "TOPSECRET-0123456789"
        self.store.set("API_TOKEN", secret)
        self.fh.touch("s/f.txt", "q" * (files_mod.TEXT_CAP - 5) + secret + "\n")
        r = self.call("fs.search", {"pattern": "qqq", "path": "s", "literal": True})
        text = r["matches"][0]["text"]
        self.assertNotIn("TOPSE", text)
        self.assertIn("«secret:API_TOKEN»", text)


@unittest.skipUnless(util.IS_WINDOWS, "Windows long paths and symlink privileges")
class TestWindowsOnly(FilesCase):    # pragma: no cover - Windows only
    def test_long_paths(self):
        rel = "/".join(["d" * 50] * 6) + "/f.txt"
        self.call("fs.write", {"path": rel, "content": "x"})
        self.assertEqual(self.call("fs.read", {"path": rel})["content"], "x")
        self.assertTrue(self.call("fs.stat", {"path": rel})["exists"])
        self.call("fs.write_chunk", {"path": rel + ".bin", "data": "eA==", "offset": 0})
        self.call("fs.write_chunk", {"path": rel + ".bin", "data": "eA==", "offset": 1, "final": True})
        self.call("fs.delete", {"path": "d" * 50, "recursive": True})

    def test_copy_skips_links_without_privilege(self):
        self.fh.touch("w/a.txt", "a")
        err = OSError(22, "privilege not held")
        err.winerror = files_mod.WINERROR_PRIVILEGE_NOT_HELD   # type: ignore[attr-defined]
        try:
            os.symlink("a.txt", str(self.fh.p("w/l")))
        except OSError:
            self.skipTest("cannot create a link to copy")
        with mock.patch.object(files_mod.os, "symlink", side_effect=err):
            r = self.call("fs.copy", {"src": "w", "dst": "w2", "recursive": True})
        self.assertEqual(r.get("skipped_links"), 1)


# ---------------------------------------------------------------------------------------------------------------
# model.*: key-protected managed services, audit route/dns

class TestModelServiceAuth(ModelCase):
    def _protected_stub(self) -> AuthSpec:
        def models(req: dict) -> Any:
            if req["headers"].get("Authorization") != "Bearer k-123":
                return 401, {}, {"error": "no key"}
            return 200, {}, {"object": "list", "data": [{"id": "qwen", "owned_by": "vllm"}]}
        self.srv.openai(models=("qwen",))
        self.srv.add_handler("GET", "/v1/models", models)
        return AuthSpec(name="VLLM_KEY", header="Authorization", value="Bearer k-123", hosts=("loopback",))

    def test_listing_and_auto_target(self):
        auth = self._protected_stub()
        self.rt.services = FakeServices({"qwen": (self.srv.url + "/v1", "openai", auth)})
        with mock.patch.object(model_mod, "PROBE_PORTS", ()):
            rows = self.call("model.endpoints", {})["endpoints"]
            row = [x for x in rows if x["source"] == "service"][0]
            self.assertEqual((row["healthy"], row["models"]), (True, ["qwen"]))
            r = self.call("model.chat", {"prompt": "hi"})
        self.assertEqual(r["model"], "qwen")
        self.assertEqual(self.srv.requests[-1]["headers"].get("Authorization"), "Bearer k-123")

    def test_audit_route_and_dns(self):
        self.srv.openai(models=("m",))
        ctx = helpers.make_ctx("model.chat", runtime=self.rt, policy=self.pol, timeout=30)
        HANDLERS["model.chat"](ctx, schema.validate(catalog.SPECS["model.chat"].params,
                                                    {"port": self.srv.port, "prompt": "hi"}))
        self.assertEqual((ctx.audit_extra.get("route"), ctx.audit_extra.get("dns")), ("direct", "local"))
        ctx = helpers.make_ctx("model.endpoints", runtime=self.rt, policy=self.pol, timeout=30)
        HANDLERS["model.endpoints"](ctx, schema.validate(catalog.SPECS["model.endpoints"].params,
                                                         {"port": self.srv.port}))
        self.assertEqual((ctx.audit_extra.get("route"), ctx.audit_extra.get("dns")), ("direct", "local"))

    def test_dns_side(self):
        t = netutil.VettedTarget("https", "example.com", 443, "/", None, "proxy", "http://p:1", "public")
        self.assertEqual(netutil._dns_side(t), "remote")
        t = netutil.VettedTarget("https", "93.184.216.34", 443, "/", "93.184.216.34", "proxy", "http://p:1",
                                 "public")
        self.assertEqual(netutil._dns_side(t), "local")
        t = netutil.VettedTarget("https", "example.com", 443, "/", "93.184.216.34", "direct", None, "public")
        self.assertEqual(netutil._dns_side(t), "local")


# ---------------------------------------------------------------------------------------------------------------
# netutil: encoding rules, TLS

class TestEncoding(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = helpers.StubHTTPServer().start()
        self.srv._httpd.handle_error = lambda *a: None
        self.addCleanup(self.srv.stop)

    def test_non_ascii_path_and_header(self):
        self.srv.add("GET", "/%E6%A8%A1%E5%9E%8B/v1/models", 200, "ok")
        r = netutil.guarded_request("GET", self.srv.url + "/模型/v1/models", guard=guard(), purpose="http")
        self.assertEqual((r.status, r.body), (200, b"ok"))
        n = len(self.srv.requests)
        for hdrs in ({"X-Note": "中文"}, {"Bad Name": "x"}, {"X(y)": "1"}):
            with self.subTest(headers=hdrs):
                with self.assertRaises(HiveError) as cm:
                    netutil.guarded_request("GET", self.srv.url + "/", guard=guard(), purpose="http", headers=hdrs)
                self.assertEqual(cm.exception.code, "bad_request")
        self.assertEqual(len(self.srv.requests), n)          # refused before any connection


def _make_cert(d: str, name: str, *alt: str) -> Optional[tuple]:
    if not shutil.which("openssl"):
        return None
    cert, key = os.path.join(d, "c.pem"), os.path.join(d, "k.pem")
    r = subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", cert,
                        "-days", "1", "-subj", f"/CN={name}", "-addext", "subjectAltName=" + ",".join(f"DNS:{n}" for n in (name,) + alt)],
                       capture_output=True, timeout=60)
    return (cert, key) if r.returncode == 0 else None


class TlsStub(helpers.StubHTTPServer):
    """StubHTTPServer serving HTTPS (records the SNI each handshake named)."""

    def __init__(self, cert: str, key: str) -> None:
        super().__init__()
        self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.ctx.load_cert_chain(cert, key)
        self.sni: list = []
        self.ctx.sni_callback = lambda sock, name, c: self.sni.append(name)  # type: ignore[assignment]

    def start(self) -> "TlsStub":
        import http.server
        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), helpers._StubHandler)
        httpd.daemon_threads = True
        httpd.stub = self  # type: ignore[attr-defined]
        httpd.socket = self.ctx.wrap_socket(httpd.socket, server_side=True, do_handshake_on_connect=False)
        httpd.handle_error = lambda *a: None  # type: ignore[assignment]
        self._httpd = httpd
        self._thread = threading.Thread(target=httpd.serve_forever, name="stub-https", daemon=True)
        self._thread.start()
        return self


class TestTls(unittest.TestCase):
    NAME = "name.test"

    @classmethod
    def setUpClass(cls) -> None:
        cls.d = tempfile.mkdtemp(prefix="hivetls-")
        made = _make_cert(cls.d, cls.NAME, "cdn.test")
        if made is None:
            shutil.rmtree(cls.d, ignore_errors=True)
            raise unittest.SkipTest("openssl not available")
        cls.cert, cls.key = made

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.d, ignore_errors=True)

    def setUp(self) -> None:
        self.srv = TlsStub(self.cert, self.key).start()
        self.addCleanup(self.srv.stop)
        self.srv.add("GET", "/a", 200, "secure")
        self.srv.add("GET", "/to-cdn", 302, b"", {"Location": f"https://cdn.test:{self.srv.port}/a"})
        self.url = f"https://{self.NAME}:{self.srv.port}"

    def _guard(self, ca_file: Optional[str]) -> NetGuard:
        return guard(proxies=netproxy.ProxySettings(None, None, netproxy.ALWAYS_NO_PROXY, "none", ca_file=ca_file))

    def _resolving(self) -> Any:
        return mock.patch.object(netutil, "_resolve", Resolver({self.NAME: ["127.0.0.1"], "cdn.test": ["127.0.0.1"]}))

    def test_https_with_ca_file_sni_and_auth(self):
        auth = AuthSpec(name="K", header="Authorization", value="Bearer tls-secret-1", hosts=(self.NAME,))
        with self._resolving():
            r = netutil.guarded_request("GET", self.url + "/a", guard=self._guard(self.cert), purpose="http",
                                        auth=auth)
            self.assertEqual((r.status, r.body), (200, b"secure"))
            req = self.srv.requests[-1]
            self.assertEqual(req["headers"].get("Host"), f"{self.NAME}:{self.srv.port}")
            self.assertEqual(req["headers"].get("Authorization"), "Bearer tls-secret-1")
            self.assertEqual(self.srv.sni[-1], self.NAME)
            # C4/C2: kept on the bound https host, dropped on the redirect to a CDN https host
            r = netutil.guarded_request("GET", self.url + "/to-cdn", guard=self._guard(self.cert), purpose="http",
                                        auth=auth)
            self.assertEqual((r.status, r.body), (200, b"secure"))
            first, second = self.srv.requests[-2], self.srv.requests[-1]
            self.assertEqual(first["headers"].get("Authorization"), "Bearer tls-secret-1")
            self.assertIsNone(second["headers"].get("Authorization"))
            self.assertEqual(second["headers"].get("Host"), f"cdn.test:{self.srv.port}")
            self.assertEqual(self.srv.sni[-1], "cdn.test")

    def test_untrusted_certificate_is_tls(self):
        with self._resolving():
            with self.assertRaises(HiveError) as cm:
                netutil.guarded_request("GET", self.url + "/a", guard=self._guard(None), purpose="http")
        self.assertEqual((cm.exception.code, cm.exception.details.get("reason")), ("unavailable", "tls"))

    def test_https_to_plain_http_port_is_tls(self):
        plain = helpers.StubHTTPServer().start()
        self.addCleanup(plain.stop)
        plain._httpd.handle_error = lambda *a: None
        with self._resolving():
            with self.assertRaises(HiveError) as cm:
                netutil.guarded_request("GET", f"https://{self.NAME}:{plain.port}/", guard=self._guard(self.cert),
                                        purpose="http", timeout=5)
        self.assertEqual((cm.exception.code, cm.exception.details.get("reason")), ("unavailable", "tls"))

    def test_https_through_connect_proxy(self):
        with ConnectProxy(routes={f"{self.NAME}:{self.srv.port}": ("127.0.0.1", self.srv.port)}) as px, \
                mock.patch.object(netutil, "_resolve", Resolver({self.NAME: ["93.184.216.34"]})), \
                mock.patch.object(netproxy, "open_tunnel", simple_open_tunnel):
            px_settings = netproxy.ProxySettings(px.url, px.url, (), "config", ca_file=self.cert)
            with mock.patch.object(netproxy, "proxy_for", return_value=px.url):
                r = netutil.guarded_request("GET", self.url + "/a", guard=guard(proxies=px_settings),
                                            purpose="http")
            self.assertEqual((r.status, r.body, r.route), (200, b"secure", "proxy"))
            self.assertEqual(px.log[-1]["kind"], "connect")
            self.assertEqual(self.srv.sni[-1], self.NAME)
            self.assertEqual(r.dns, "remote")


if __name__ == "__main__":
    unittest.main()
