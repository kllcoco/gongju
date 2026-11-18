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


def seed_meta(dest: Path, url: str, etag: Optional[str] = None) -> None:
    """The resume validator a previous run of ``url`` into ``dest`` left next to its part."""
    Path(str(dest) + ".hive-part.json").write_text(json.dumps({"v": 1, "url": url, "etag": etag,
                                                                "last_modified": None}))


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

    def test_close_unread_stream_closes_connection(self):
        """Review fix: closing the chunk iterator without reading it (non-2xx answers) closes the connection."""
        closed: List[Any] = []
        real_close = netutil._Conn.close

        def spy(conn: Any) -> None:
            closed.append(conn)
            real_close(conn)
        with mock.patch.object(netutil._Conn, "close", spy):
            status, _h, _f, chunks = netutil.open_download(self.srv.url + "/missing", guard=guard())
            self.assertEqual(status, 404)
            self.assertEqual(closed, [])
            chunks.close()
            self.assertEqual(len(closed), 1)
            self.assertIsNone(closed[0].sock)

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
        seed_meta(dest, self.srv.url + "/f.bin")
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest),
                                                sha256=hashlib.sha256(DATA).hexdigest()))
        self.assertEqual(code, 0)
        self.assertEqual(self.srv.requests[-1]["headers"].get("Range"), "bytes=30000-")
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertFalse(Path(str(dest) + ".hive-part.json").exists())       # the validator goes with the part

    def test_206_without_content_range_restarts(self):
        """Review fix: a 206 to ``Range: bytes=30000-`` without Content-Range is never taken as the whole body."""
        def h(req: dict) -> Any:
            if req["headers"].get("Range"):
                return 206, {}, DATA[30000:]
            return 200, {}, DATA
        self.srv.add_handler("GET", "/f.bin", h)
        dest = self.tmp / "f.bin"
        Path(str(dest) + ".hive-part").write_bytes(DATA[:30000])
        seed_meta(dest, self.srv.url + "/f.bin")
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/f.bin", path=str(dest)))
        self.assertEqual(code, 0)
        self.assertEqual(dest.read_bytes(), DATA)
        self.assertEqual(res["size"], len(DATA))
        self.assertEqual([q["headers"].get("Range") for q in self.srv.requests], ["bytes=30000-", None])
        # a 206 whose Content-Range starts elsewhere restarts too
        self.srv.requests.clear()

        def h2(req: dict) -> Any:
            if req["headers"].get("Range"):
                return 206, {"Content-Range": f"bytes 100-{len(DATA) - 1}/{len(DATA)}"}, DATA[100:]
            return 200, {}, DATA
        self.srv.add_handler("GET", "/g.bin", h2)
        dest = self.tmp / "g.bin"
        Path(str(dest) + ".hive-part").write_bytes(DATA[:30000])
        seed_meta(dest, self.srv.url + "/g.bin")
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/g.bin", path=str(dest)))
        self.assertEqual((code, dest.read_bytes()), (0, DATA))

    def test_resume_validator(self):
        """Review fix: a part is resumed only for the same URL, with If-Range; a changed resource restarts."""
        new = bytes(reversed(DATA))
        self.srv.add_file("/v.bin", new, {"ETag": '"v2"'})
        dest = self.tmp / "v.bin"
        part = Path(str(dest) + ".hive-part")
        # (a) a part without a validator (unknown origin) or of another URL is not resumed
        for meta_url in (None, self.srv.url + "/other.bin"):
            with self.subTest(meta_url=meta_url):
                self.srv.requests.clear()
                part.write_bytes(DATA[:30000])
                if meta_url:
                    seed_meta(dest, meta_url, etag='"v1"')
                code, _r, _p = self.run_spec(file_spec(self.srv.url + "/v.bin", path=str(dest), overwrite=True))
                self.assertEqual((code, dest.read_bytes()), (0, new))
                self.assertEqual([q["headers"].get("Range") for q in self.srv.requests], [None])
        # (b) same URL, the server's ETag changed: If-Range is sent, the server answers 200 -> restart from zero
        def h(req: dict) -> Any:
            ok = req["headers"].get("If-Range") == '"v2"' and req["headers"].get("Range") == "bytes=30000-"
            if ok:
                return 206, {"ETag": '"v2"', "Content-Range": f"bytes 30000-{len(new) - 1}/{len(new)}"}, new[30000:]
            return 200, {"ETag": '"v2"'}, new
        self.srv.add_handler("GET", "/w.bin", h)
        dest = self.tmp / "w.bin"
        part = Path(str(dest) + ".hive-part")
        part.write_bytes(DATA[:30000])
        seed_meta(dest, self.srv.url + "/w.bin", etag='"v1"')
        self.srv.requests.clear()
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/w.bin", path=str(dest)))
        self.assertEqual((code, dest.read_bytes()), (0, new))
        self.assertEqual(self.srv.requests[0]["headers"].get("If-Range"), '"v1"')
        # (c) the validator matches: resumed (206) and the result is whole
        dest.unlink()
        part.write_bytes(new[:30000])
        seed_meta(dest, self.srv.url + "/w.bin", etag='"v2"')
        self.srv.requests.clear()
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/w.bin", path=str(dest),
                                               sha256=hashlib.sha256(new).hexdigest()))
        self.assertEqual((code, dest.read_bytes()), (0, new))
        self.assertEqual(len(self.srv.requests), 1)
        # (d) a failed download leaves part + validator; the retry resumes with the stored ETag
        self.srv.requests.clear()
        dest.unlink()
        code, _r, _p = self.run_spec(file_spec(self.srv.url + "/w.bin", path=str(dest), sha256="0" * 64))
        self.assertEqual(code, 3)
        meta = json.loads(Path(str(dest) + ".hive-part.json").read_text())
        self.assertEqual((meta["url"], meta["etag"]), (self.srv.url + "/w.bin", '"v2"'))

    def test_stop_between_chunks_is_stopped(self):
        """Review fix: a stop that surfaces as ``cancelled`` from the stream ends with exit 7, not a failure."""
        stop = threading.Event()

        def fake_open(url: str, **kw: Any) -> Any:
            def chunks() -> Any:
                yield DATA[:100]
                stop.set()
                raise HiveError("cancelled", "cancelled by the caller")
            return 200, {"Content-Length": str(len(DATA))}, url, chunks()
        dest = self.tmp / "s.bin"
        with RunDir(), mock.patch.object(netutil, "open_download", fake_open), \
                mock.patch.object(_download, "_say") as say:
            code = _download.run(file_spec("http://127.0.0.1:1/s.bin", path=str(dest)), stop)
        self.assertEqual(code, 7)
        self.assertEqual(say.call_args_list[-1][0][0], "stopped")
        self.assertFalse(dest.exists())

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

    def test_uncreatable_server_name(self):
        """Review fix: a name longer than NAME_MAX bytes (102 CJK chars = 306 bytes) becomes download.bin, and a
        name the filesystem still refuses at publish time leaves no hidden part behind."""
        long_name = "模型" * 51 + ".bin"
        self.assertLessEqual(len(long_name), 255)
        self.assertEqual(_download.choose_name({"Content-Disposition": "attachment; filename*=UTF-8''"
                                                + "%E6%A8%A1%E5%9E%8B" * 51 + ".bin"}, "http://h/x"), "download.bin")
        with mock.patch.object(_download, "IS_WINDOWS", True):
            self.assertEqual(_download.choose_name({"Content-Disposition": 'attachment; filename="a?b.bin"'},
                                                   "http://h/x"), "download.bin")
        self.srv.add("GET", "/long", 200, DATA[:100],
                     {"Content-Disposition": "attachment; filename*=UTF-8''" + "%E6%A8%A1%E5%9E%8B" * 51 + ".bin"})
        d = self.tmp / "long"
        d.mkdir()
        code, res, _p = self.run_spec(file_spec(self.srv.url + "/long", directory=str(d)))
        self.assertEqual((code, os.path.basename(res["path"])), (0, "download.bin"))
        self.assertEqual(os.listdir(str(d)), ["download.bin"])
        # the filesystem refuses a name choose_name accepted: falls back to download.bin, nothing hidden is left
        self.srv.add("GET", "/odd", 200, DATA[:100], {"Content-Disposition": 'attachment; filename="odd.bin"'})
        d = self.tmp / "odd"
        d.mkdir()
        real_open = os.open

        def picky_open(path: Any, flags: int, *a: Any, **kw: Any) -> int:
            if os.path.basename(str(path)) == "odd.bin":
                raise OSError(36, "File name too long")
            return real_open(path, flags, *a, **kw)
        with mock.patch.object(_download.os, "open", picky_open):
            code, res, _p = self.run_spec(file_spec(self.srv.url + "/odd", directory=str(d)))
        self.assertEqual((code, os.listdir(str(d))), (0, ["download.bin"]))
        # and when even download.bin cannot be created, the job fails without leaving the hidden part
        d = self.tmp / "odd2"
        d.mkdir()

        def no_open(path: Any, flags: int, *a: Any, **kw: Any) -> int:
            if flags & os.O_EXCL:
                raise OSError(22, "Invalid argument")
            return real_open(path, flags, *a, **kw)
        with mock.patch.object(_download.os, "open", no_open):
            code, res, _p = self.run_spec(file_spec(self.srv.url + "/odd", directory=str(d)))
        self.assertEqual((code, res), (1, None))
        self.assertEqual(os.listdir(str(d)), [])

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

    def test_audit_route_dns(self):
        """Review fix (§5.4 NetGuard rule 8): net.download records route and dns in audit extra."""
        with helpers.StubHTTPServer() as srv, wp2util.Node() as node:
            srv._httpd.handle_error = lambda *a: None
            srv.add_file("/a.bin", DATA[:10])
            r = node.ok("net.download", {"url": srv.url + "/a.bin", "path": str(node.home / "a.bin")})
            node.ok("job.status", {"job_id": r["job_id"], "wait": 30})
            extra = node.rt.audit.tail(10, action="net.download")[-1]["extra"]
            self.assertEqual((extra["route"], extra["dns"], extra["job_id"]), ("direct", "local", r["job_id"]))

    def test_venv_bin_folder_refused_for_gated_identity(self):
        """Review fix (§5.4 PathGuard step 7, v7): a server-named download into ``<venv>/bin`` is refused for a
        gated identity like fs.write of a file there; the operator may still do it."""
        with helpers.StubHTTPServer() as srv, wp2util.Node() as node:
            srv._httpd.handle_error = lambda *a: None
            srv.add("GET", "/t", 200, b"#!/bin/sh\n", {"Content-Disposition": 'attachment; filename="pytest"'})
            venv = node.home / "proj" / "venv"
            (venv / "bin").mkdir(parents=True)
            (venv / "pyvenv.cfg").write_text("home = /usr/bin\n")
            e = node.err("fs.write", {"path": str(venv / "bin" / "pytest"), "content": "x"},
                         identity=wp2util.CAUTIOUS)
            self.assertEqual((e["code"], e["details"]["rule"]), ("policy_denied", "exec_path"))
            e = node.err("net.download", {"url": srv.url + "/t", "path": str(venv / "bin")},
                         identity=wp2util.CAUTIOUS)
            self.assertEqual((e["code"], e["details"]["rule"]), ("policy_denied", "exec_path"))
            self.assertEqual(os.listdir(str(venv / "bin")), [])
            r = node.ok("net.download", {"url": srv.url + "/t", "path": str(venv / "bin")})
            st = node.ok("job.status", {"job_id": r["job_id"], "wait": 30})
            self.assertEqual(st["exit_code"], 0, st)

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
