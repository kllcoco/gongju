"""WP3b tests: netutil.open_download (§11.2), the ``_download`` child in file mode (§11.3) and the net.download
handler (§9.8) — Range resume, sha verify, progress, server-chosen names, exclusive create, re-vetted redirects."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from hive import _download, netproxy, netutil, util
from hive.errors import HiveError
from hive.netutil import AuthSpec, NetGuard
from tests import helpers, wp2util
from tests.fixtures.connect_proxy import ConnectProxy

NO_PROXY = netproxy.ProxySettings(None, None, netproxy.ALWAYS_NO_PROXY, "none")
DATA = bytes(range(256)) * 400          # 102400 bytes


def guard(**kw: Any) -> NetGuard:
    args: Dict[str, Any] = dict(http_allow=["loopback", "public"], http_deny=[], download_allow=["loopback", "public"],
                                allow_metadata_ip=False, proxies=NO_PROXY)
    args.update(kw)
    return NetGuard(**args)


def resolving(table: Dict[str, List[str]]) -> Any:
    real = netutil._resolve

    def res(host: str, port: int, timeout: float = 5.0) -> List[str]:
        if host.lower() in table:
            return list(table[host.lower()])
        return real(host, port, timeout)
    return mock.patch.object(netutil, "_resolve", res)


class RunDir:
    """chdir into a temp run dir for in-process ``_download.run`` (it writes progress.json/result.json there)."""

    def __init__(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="hivedl-run-"))
        self.old = os.getcwd()

    def __enter__(self) -> Path:
        os.chdir(str(self.path))
        return self.path

    def __exit__(self, *exc: Any) -> None:
        os.chdir(self.old)
        shutil.rmtree(str(self.path), ignore_errors=True)


def file_spec(url: str, *, path: Optional[str] = None, directory: Optional[str] = None, **kw: Any) -> dict:
    spec = {"v": 1, "mode": "file", "net": guard().to_dict(), "auth": None, "url": url, "path": path,
            "dir": directory, "deny": [], "headers": {}, "sha256": None, "overwrite": False, "resume": True,
            "make_dirs": True}
    spec.update(kw)
    return spec


class DownloadCase(unittest.TestCase):
    def setUp(self) -> None:
        self.srv = helpers.StubHTTPServer().start()
        self.srv._httpd.handle_error = lambda *a: None
        self.addCleanup(self.srv.stop)
        self.tmp = Path(tempfile.mkdtemp(prefix="hivedl-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def run_spec(self, spec: dict) -> tuple:
        with RunDir() as rd:
            code = _download.run(spec)
            res = json.loads((rd / "result.json").read_text()) if (rd / "result.json").exists() else None
            prog = json.loads((rd / "progress.json").read_text()) if (rd / "progress.json").exists() else None
        return code, res, prog


class TestOpenDownload(DownloadCase):
    def test_range_and_stream(self):
        self.srv.add_file("/f.bin", DATA)
        status, headers, final, chunks = netutil.open_download(self.srv.url + "/f.bin", guard=guard(),
                                                               range_start=1000)
        body = b"".join(chunks)
        self.assertEqual(status, 206)
        self.assertEqual(body, DATA[1000:])
        self.assertEqual(self.srv.requests[-1]["headers"]["Range"], "bytes=1000-")
        self.assertEqual(final, self.srv.url + "/f.bin")
        _s, _h, _f, chunks = netutil.open_download(self.srv.url + "/f.bin", guard=guard())
        self.assertEqual(b"".join(chunks), DATA)
        self.assertNotIn("Range", self.srv.requests[-1]["headers"])

    def test_redirect_revetted_and_auth_dropped_on_cdn_hop(self):
        self.srv.add_file("/cdn/f.bin", DATA)
        self.srv.add("GET", "/resolve/f.bin", 302, b"",
                     {"Location": f"http://cdn.test:{self.srv.port}/cdn/f.bin"})
        auth = AuthSpec(name="HF_TOKEN", header="Authorization", value="Bearer hf_secret", hosts=("loopback",))
        g = helpers.FakeNetGuard(resolve={"cdn.test": ["127.0.0.1"]})
        status, _h, final, chunks = netutil.open_download(f"http://localhost:{self.srv.port}/resolve/f.bin",
                                                          guard=g, auth=auth, range_start=5,
                                                          headers={"Cookie": "c=1", "X-Plain": "y"})
        self.assertEqual(b"".join(chunks), DATA[5:])
        self.assertEqual(status, 206)
        self.assertTrue(final.startswith("http://cdn.test:"))
        first, second = self.srv.requests[-2], self.srv.requests[-1]
        self.assertEqual(first["headers"].get("Authorization"), "Bearer hf_secret")
        self.assertEqual(first["headers"].get("Cookie"), "c=1")
        self.assertNotIn("Authorization", second["headers"])
        self.assertNotIn("Cookie", second["headers"])            # user credential header dropped cross-host
        self.assertEqual(second["headers"].get("X-Plain"), "y")
        self.assertEqual(second["headers"].get("Range"), "bytes=5-")
        self.assertEqual([c[1:] for c in g.calls], [("download", "GET", False), ("download", "GET", True)])

    def test_redirect_to_denied_target(self):
        self.srv.add("GET", "/r", 302, b"", {"Location": "http://169.254.169.254/latest/meta-data"})
        with self.assertRaises(HiveError) as cm:
            netutil.open_download(self.srv.url + "/r", guard=guard())
        self.assertEqual((cm.exception.code, cm.exception.details.get("rule")), ("policy_denied", "ssrf"))
        self.srv.add("GET", "/loop", 302, b"", {"Location": "/loop"})
        with self.assertRaises(HiveError) as cm:
            netutil.open_download(self.srv.url + "/loop", guard=guard(), max_redirects=3)
        self.assertEqual(cm.exception.details["reason"], "redirects")

    def test_cancel_and_errors(self):
        self.srv.add_file("/f.bin", DATA)
        ev = threading.Event()
        ev.set()
        with self.assertRaises(HiveError) as cm:
            netutil.open_download(self.srv.url + "/f.bin", guard=guard(), cancel=ev)
        self.assertEqual(cm.exception.code, "cancelled")
        with self.assertRaises(HiveError) as cm:
            netutil.open_download(f"http://127.0.0.1:{helpers.free_port()}/x", guard=guard())
        self.assertEqual((cm.exception.details["reason"], cm.exception.details["maybe_executed"]), ("connect", False))

    def test_through_connect_proxy(self):
        self.srv.add_file("/f.bin", DATA)
        px_settings = None
        with ConnectProxy(routes={f"dl.example.com:{self.srv.port}": ("127.0.0.1", self.srv.port)}) as px:
            px_settings = netproxy.ProxySettings(px.url, px.url, netproxy.ALWAYS_NO_PROXY, "config")
            with resolving({"dl.example.com": ["93.184.216.34"]}):
                _s, _h, _f, chunks = netutil.open_download(f"http://dl.example.com:{self.srv.port}/f.bin",
                                                           guard=guard(proxies=px_settings))
                self.assertEqual(b"".join(chunks), DATA)
            self.assertEqual(px.log[-1]["kind"], "absolute")
            # a secret-carrying http:// request through the proxy is tunnelled (the proxy never sees the header)
            auth = AuthSpec(name="K", header="Authorization", value="Bearer topsecret9", hosts=("private",))
            self.srv.add_file("/p.bin", DATA)

            class G:
                def check_url(self, url: str, **kw: Any) -> Any:
                    return netutil.VettedTarget("http", "10.1.2.3", 80, "/p.bin", "10.1.2.3", "proxy", px.url,
                                                "private")
            px.routes["10.1.2.3:80"] = ("127.0.0.1", self.srv.port)
            _s, _h, _f, chunks = netutil.open_download("http://10.1.2.3/p.bin", guard=G(), auth=auth)
            self.assertEqual(b"".join(chunks), DATA)
            self.assertEqual(px.log[-1]["kind"], "connect")
            self.assertNotIn("topsecret9", json.dumps(px.log))
            self.assertEqual(self.srv.requests[-1]["headers"].get("Authorization"), "Bearer topsecret9")


class TestFileMode(DownloadCase):
    def test_download_sha_progress(self):
        self.srv.add_file("/f.bin", DATA)
        dest = self.tmp / "sub" / "f.bin"
        code, res, prog = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest),
                                                  sha256=hashlib.sha256(DATA).hexdigest()))
        self.assertEqual(code, 0)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(res, {"v": 1, "path": str(dest), "size": len(DATA), "sha256": hashlib.sha256(DATA).hexdigest()})
        self.assertEqual((prog["done"], prog["total"], prog["percent"]), (len(DATA), len(DATA), 100.0))
        for k in ("v", "done", "total", "percent", "rate_bps", "eta_s", "note"):
            self.assertIn(k, prog)
        self.assertFalse(Path(str(dest) + ".hive-part").exists())

    def test_range_resume(self):
        self.srv.add_file("/f.bin", DATA)
        dest = self.tmp / "f.bin"
        Path(str(dest) + ".hive-part").write_bytes(DATA[:30000])
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest),
                                                sha256=hashlib.sha256(DATA).hexdigest()))
        self.assertEqual(code, 0)
        self.assertEqual(self.srv.requests[-1]["headers"].get("Range"), "bytes=30000-")
        self.assertEqual(dest.read_bytes(), DATA)

    def test_200_instead_of_206_restarts(self):
        self.srv.add("GET", "/plain.bin", 200, DATA, {"Content-Type": "application/octet-stream"})
        dest = self.tmp / "p.bin"
        Path(str(dest) + ".hive-part").write_bytes(b"garbage-garbage")
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/plain.bin", path=str(dest)))
        self.assertEqual(code, 0)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(res["sha256"], hashlib.sha256(DATA).hexdigest())

    def test_sha_mismatch_keeps_part(self):
        self.srv.add_file("/f.bin", DATA)
        dest = self.tmp / "f.bin"
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest), sha256="0" * 64))
        self.assertEqual((code, res), (3, None))
        self.assertFalse(dest.exists())
        self.assertTrue(Path(str(dest) + ".hive-part").exists())

    def test_existing_not_overwritten(self):
        self.srv.add_file("/f.bin", DATA)
        dest = self.tmp / "f.bin"
        dest.write_bytes(b"old")
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest)))
        self.assertEqual(code, 1)
        self.assertEqual(dest.read_bytes(), b"old")
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest), overwrite=True))
        self.assertEqual((code, dest.read_bytes()), (0, DATA))

    def test_http_error_and_disk(self):
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/missing", path=str(self.tmp / "m")))
        self.assertEqual(code, 1)
        self.srv.add_file("/f.bin", DATA)
        with mock.patch.object(_download, "_free_bytes", return_value=10):
            code, _r, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(self.tmp / "d.bin")))
        self.assertEqual(code, 4)

    def test_server_chosen_names(self):
        cases = {'attachment; filename=".netrc"': "download.bin",
                 'attachment; filename="../x"': "download.bin",
                 'attachment; filename="con.txt"': "download.bin",
                 "attachment; filename*=UTF-8''%E6%A8%A1%E5%9E%8B.gguf": "模型.gguf",
                 'attachment; filename="model.gguf"': "model.gguf"}
        for i, (cd, want) in enumerate(cases.items()):
            with self.subTest(cd=cd):
                self.srv.add("GET", f"/n{i}", 200, DATA[:100], {"Content-Disposition": cd})
                d = self.tmp / f"d{i}"
                d.mkdir()
                code, res, _p = self.run_spec(file_spec(self.srv.url + f"/n{i}", directory=str(d)))
                self.assertEqual(code, 0)
                self.assertEqual(os.path.basename(res["path"]), want)
                self.assertEqual(os.listdir(str(d)), [want])
        self.assertEqual(_download.choose_name({}, "http://h/a/b/weights.bin?x=1"), "weights.bin")
        self.assertEqual(_download.choose_name({}, "http://h/"), "download.bin")

    def test_never_overwrites_server_name(self):
        self.srv.add("GET", "/dl/file.tar.gz", 200, DATA[:10])
        d = self.tmp / "d"
        d.mkdir()
        (d / "file.tar.gz").write_bytes(b"mine")
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/dl/file.tar.gz", directory=str(d)))
        self.assertEqual(code, 0)
        self.assertEqual(os.path.basename(res["path"]), "file.tar-1.gz")
        self.assertEqual((d / "file.tar.gz").read_bytes(), b"mine")
        os.symlink(str(self.tmp / "elsewhere"), str(d / "file.tar-2.gz"))
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/dl/file.tar.gz", directory=str(d)))
        self.assertEqual(os.path.basename(res["path"]), "file.tar-3.gz")
        self.assertFalse((self.tmp / "elsewhere").exists())

    def test_windows_safe_exclusive_create(self):
        """v6: without O_NOFOLLOW (Windows) the name is still created exclusively."""
        class NoNofollow:
            def __getattr__(self, name: str) -> Any:
                if name == "O_NOFOLLOW":
                    raise AttributeError(name)
                return getattr(os, name)
        self.srv.add("GET", "/dl/w.bin", 200, DATA[:10])
        d = self.tmp / "w"
        d.mkdir()
        (d / "w.bin").write_bytes(b"x")
        with mock.patch.object(_download, "os", NoNofollow()):
            code, res, _p = self.run_spec(file_spec(self.srv.url + "/dl/w.bin", directory=str(d)))
        self.assertEqual((code, os.path.basename(res["path"])), (0, "w-1.bin"))

    def test_denied_name(self):
        home = self.tmp / "home"
        kube = home / ".kube"
        kube.mkdir(parents=True)
        self.srv.add("GET", "/k", 200, b"apiVersion: v1", {"Content-Disposition": 'attachment; filename="config"'})
        deny = [util.path_key(str(kube.resolve() / "config"), casefold=False)]
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/k", directory=str(kube), deny=deny,
                                                casefold=False))
        self.assertEqual((code, res), (5, None))
        self.assertEqual(os.listdir(str(kube)), [])


class TestChildProcess(DownloadCase):
    def test_main_reads_and_deletes_spec(self):
        self.srv.add_file("/f.bin", DATA)
        rd = self.tmp / "run"
        rd.mkdir()
        spec_path = rd / "download.json"
        util.atomic_write_bytes(spec_path, json.dumps(file_spec(self.srv.url + "/f.bin",
                                                                path=str(self.tmp / "o.bin"))).encode(),
                                private=True)
        env = dict(os.environ)
        env.update(util.self_env())
        out = subprocess.run(util.self_command() + ["_download", "--spec", str(spec_path)], cwd=str(self.tmp),
                             capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(spec_path.exists())
        self.assertTrue((rd / "result.json").exists())
        self.assertTrue((rd / "progress.json").exists())
        self.assertEqual((self.tmp / "o.bin").read_bytes(), DATA)

    def test_import_rules(self):
        """§22.3: _download/hfhub import no WP1 module; _download has no function-level hive import."""
        code = ("import sys; import hive._download, hive.hfhub; "
                "print(','.join(sorted(m for m in sys.modules if m.startswith('hive.'))))")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60,
                             cwd=str(Path(_download.__file__).parent.parent))
        mods = set(out.stdout.strip().split(","))
        self.assertLessEqual(mods, {"hive._download", "hive.hfhub", "hive.netutil", "hive.netproxy", "hive.util",
                                    "hive.errors"}, out.stderr)
        tree = ast.parse(Path(_download.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for sub in ast.walk(node):
                    if isinstance(sub, ast.ImportFrom):
                        self.assertFalse(sub.level or (sub.module or "").startswith("hive"),
                                         f"function-level hive import in {node.name}")


class TestNetDownloadHandler(unittest.TestCase):
    """net.download through a real NodeRuntime: a job of kind download, progress and result in job.status."""

    def test_job_end_to_end(self):
        with helpers.StubHTTPServer() as srv, wp2util.Node() as node:
            srv._httpd.handle_error = lambda *a: None
            srv.add_file("/model.gguf", DATA)
            dest = node.home / "dl" / "model.gguf"
            r = node.ok("net.download", {"url": srv.url + "/model.gguf", "path": str(dest),
                                         "sha256": hashlib.sha256(DATA).hexdigest()})
            wp2util.assert_result_schema(self, "net.download", r)
            self.assertEqual((r["state"], r["path"]), ("running", str(dest)))
            st = node.ok("job.status", {"job_id": r["job_id"], "wait": 30})
            self.assertEqual(st["state"], "exited", st)
            self.assertEqual(st["exit_code"], 0, st)
            self.assertEqual(st["result"]["sha256"], hashlib.sha256(DATA).hexdigest())
            self.assertEqual(st["result"]["path"], str(dest))
            self.assertEqual(dest.read_bytes(), DATA)
            rec = node.rt.jobs.record(r["job_id"])
            self.assertEqual(rec["kind"], "download")
            self.assertFalse((node.rt.jobs.run_dir(r["job_id"]) / "download.json").exists())
            # existing file without overwrite -> conflict at the handler
            e = node.err("net.download", {"url": srv.url + "/model.gguf", "path": str(dest)})
            self.assertEqual(e["code"], "conflict")
            # server-chosen name into a folder
            srv.add("GET", "/get", 200, DATA[:50], {"Content-Disposition": 'attachment; filename="w.bin"'})
            r = node.ok("net.download", {"url": srv.url + "/get", "path": str(node.home / "dl")})
            st = node.ok("job.status", {"job_id": r["job_id"], "wait": 30})
            self.assertEqual(st["result"]["path"], str(node.home / "dl" / "w.bin"))
            # a server-chosen 'config' into ~/.kube is refused by the child (default secret paths)
            (node.home / ".kube").mkdir()
            srv.add("GET", "/kube", 200, b"x", {"Content-Disposition": 'attachment; filename="config"'})
            r = node.ok("net.download", {"url": srv.url + "/kube", "path": str(node.home / ".kube")})
            st = node.ok("job.status", {"job_id": r["job_id"], "wait": 30})
            self.assertEqual(st["exit_code"], 5, st)
            self.assertIn("denied", st["error"] or "")
            self.assertFalse((node.home / ".kube" / "config").exists())

    def test_handler_refusals(self):
        with wp2util.Node() as node:
            e = node.err("net.download", {"url": "http://169.254.169.254/x", "path": str(node.home / "x")})
            self.assertEqual((e["code"], e["details"]["rule"]), ("policy_denied", "ssrf"))
            e = node.err("net.download", {"url": "http://127.0.0.1:1/x", "path": str(node.home / "x"),
                                          "sha256": "nothex"})
            self.assertEqual(e["code"], "bad_request")
            e = node.err("net.download", {"url": "http://127.0.0.1:1/x", "path": str(node.home / "x"),
                                          "headers": ["Host: evil"]})
            self.assertEqual(e["code"], "bad_request")
            e = node.err("net.download", {"url": "http://127.0.0.1:1/x", "path": str(node.home / ".ssh" / "k")})
            self.assertEqual(e["code"], "policy_denied")
            # a secret bound to huggingface.co is not sent to another host (first hop refused)
            node.rt.secrets.set("HF_TOKEN", "hf_abcdefghijklmnop")
            e = node.err("net.download", {"url": "http://127.0.0.1:1/x", "path": str(node.home / "x"),
                                          "auth_env": "HF_TOKEN"})
            self.assertEqual((e["code"], e["details"]["rule"]), ("policy_denied", "secret_host"))


if __name__ == "__main__":
    unittest.main()
