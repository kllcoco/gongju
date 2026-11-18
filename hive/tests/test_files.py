"""WP3a tests: the fs.* handlers (design §9.7, §21.2 files row) against the real WP1 Policy (PathGuard, denies,
deny_write) in a fake home, plus one pass through the real NodeRuntime pipeline (masking)."""
from __future__ import annotations

import base64
import codecs
import hashlib
import os
import time
import unittest
from pathlib import Path
from typing import Any, Dict
from unittest import mock

from hive import catalog, fsops, schema
from hive.actions import HANDLERS, load_all
from hive.errors import HiveError
from hive.secretstore import SecretStore
from tests import helpers, wp1util

load_all()
SECRET = "hf_" + "Q" * 30
POSIX = os.name != "nt"


class FilesCase(unittest.TestCase):
    preset = "standard"
    policy_overrides: Dict[str, Any] = {}

    def setUp(self) -> None:
        self.fh = wp1util.FakeHome()
        self.fh.__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)
        self.store = SecretStore(self.fh.data / "secrets.json", passthrough=(), env={})
        self.pol = wp1util.build_policy(self.fh, self.preset, secrets=self.store,
                                        policy=dict(self.policy_overrides))
        self.rt = helpers.FakeRuntime(workdir=self.fh.home)
        self.addCleanup(self.rt.cleanup)
        self.rt.secrets = self.store
        self.rt.parts = fsops.PartTracker(self.rt.state_dir / "parts.json")
        self.home = self.fh.home

    def call(self, action: str, params: dict, ident: Any = wp1util.OPERATOR, timeout: float = 30) -> dict:
        eff = self.pol.effective(ident, paused=False)
        ctx = helpers.make_ctx(action, runtime=self.rt, identity=ident, policy=eff, timeout=timeout)
        p = schema.validate(catalog.SPECS[action].params, params)
        out = HANDLERS[action](ctx, p)
        catalog_result = catalog.SPECS[action].result
        schema.validate(catalog_result, out, coerce=False, defaults=False, path="result")
        return out

    def err(self, action: str, params: dict, ident: Any = wp1util.OPERATOR, **kw: Any) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.call(action, params, ident, **kw)
        return cm.exception

    def rule(self, e: HiveError) -> tuple:
        return e.code, e.details.get("rule")


class TestRead(FilesCase):
    def test_paging_and_numbers(self):
        f = self.fh.touch("t.txt", "".join(f"line{i}\n" for i in range(1, 11)))
        r = self.call("fs.read", {"path": "t.txt", "start_line": 3, "max_lines": 2})
        self.assertEqual(r["content"], "line3\nline4\n")
        self.assertEqual((r["start_line"], r["end_line"], r["next_start_line"], r["total_lines"]), (3, 4, 5, 10))
        self.assertEqual(r["version"], hashlib.sha256(f.read_bytes()).hexdigest())
        self.assertEqual(r["path"], str(f))
        r = self.call("fs.read", {"path": str(f), "start_line": 9, "line_numbers": True})
        self.assertEqual(r["content"], "     9\tline9\n    10\tline10\n")
        self.assertIsNone(r["next_start_line"])
        r = self.call("fs.read", {"path": str(f), "start_line": 50})
        self.assertEqual((r["content"], r["end_line"], r["next_start_line"]), ("", 49, None))

    def test_long_lines_and_max_bytes(self):
        self.fh.touch("long.txt", "a" * 2500 + "\nshort\n" + ("b" * 900 + "\n") * 5)
        r = self.call("fs.read", {"path": "long.txt"})
        self.assertTrue(r["content"].startswith("a" * 2000 + "…[+500 chars]\nshort\n"))
        self.assertEqual(r["long_lines_cut"], 1)
        r = self.call("fs.read", {"path": "long.txt", "max_bytes": 3000})
        self.assertLessEqual(len(r["content"].encode()), 3000)
        self.assertIsNotNone(r["next_start_line"])

    def test_encodings_and_binary(self):
        p = self.fh.p("bom.txt")
        p.write_bytes(codecs.BOM_UTF8 + "héllo\n".encode())
        r = self.call("fs.read", {"path": str(p)})
        self.assertEqual((r["content"], r["encoding"]), ("héllo\n", "utf-8-sig"))
        p16 = self.fh.p("u16.txt")
        p16.write_bytes(codecs.BOM_UTF16_LE + "中文\r\nx\r\n".encode("utf-16-le"))
        r = self.call("fs.read", {"path": str(p16)})
        self.assertEqual((r["content"], r["encoding"], r["total_lines"]), ("中文\r\nx\r\n", "utf-16-le", 2))
        b = self.fh.p("bin.dat")
        b.write_bytes(b"\x7fELF\x00\x01\x02" * 10)
        e = self.err("fs.read", {"path": str(b)})
        self.assertEqual(e.code, "unsupported")
        self.assertIn("fs.read_chunk", e.hint)
        gbk = self.fh.p("gbk.txt")
        gbk.write_bytes("中文\n".encode("gbk"))
        r = self.call("fs.read", {"path": str(gbk)})
        self.assertEqual(r["total_lines"], 1)

    def test_denied_and_missing(self):
        self.fh.touch(".ssh/id_rsa", "KEY")
        self.assertEqual(self.rule(self.err("fs.read", {"path": "~/.ssh/id_rsa"})), ("policy_denied", "fs_deny"))
        self.assertEqual(self.err("fs.read", {"path": "nope.txt"}).code, "not_found")
        self.assertEqual(self.err("fs.read", {"path": str(self.fh.data / "secrets.json")}).code, "policy_denied")
        self.fh.touch(".hive/node/config.json", "{}")
        self.assertEqual(self.rule(self.err("fs.read", {"path": "~/.HIVE/node/config.json"})),
                         ("policy_denied", "hive_internal"))


class TestReadLimit(FilesCase):
    policy_overrides = {"fs": {"max_read_bytes": 10000}}

    def test_max_bytes_above_node_limit(self):
        self.fh.touch("x.txt", "x\n")
        e = self.err("fs.read", {"path": "x.txt", "max_bytes": 20000})
        self.assertEqual(e.code, "bad_request")
        self.assertIn("10000", e.hint)
        self.assertEqual(self.err("fs.read_chunk", {"path": "x.txt", "length": 20000}).code, "bad_request")


class TestWrite(FilesCase):
    def test_modes_and_versions(self):
        r = self.call("fs.write", {"path": "d/new.txt", "content": "hello"})
        p = self.fh.p("d/new.txt")
        self.assertEqual((r["created"], r["bytes"], p.read_text()), (True, 5, "hello"))
        self.assertEqual(r["version"], hashlib.sha256(b"hello").hexdigest())
        self.assertEqual(self.err("fs.write", {"path": "d/new.txt", "content": "x", "mode": "create"}).code,
                         "conflict")
        r = self.call("fs.write", {"path": "d/new.txt", "content": " world", "mode": "append"})
        self.assertEqual((p.read_text(), r["created"]), ("hello world", False))
        self.assertEqual(r["version"], hashlib.sha256(b"hello world").hexdigest())
        e = self.err("fs.write", {"path": "d/new.txt", "content": "x", "if_version": "0" * 64})
        self.assertEqual(e.code, "conflict")
        self.assertIn("re-read", e.hint)
        self.call("fs.write", {"path": "d/new.txt", "content": "v2", "if_version": "sha256:" + r["version"]})
        self.assertEqual(p.read_text(), "v2")
        self.call("fs.write", {"path": "d/b.bin", "content": base64.b64encode(b"\x00\x01").decode(),
                               "encoding": "base64"})
        self.assertEqual(self.fh.p("d/b.bin").read_bytes(), b"\x00\x01")
        self.assertEqual(self.err("fs.write", {"path": "d/c.bin", "content": "!!", "encoding": "base64"}).code,
                         "bad_request")
        self.assertEqual(self.err("fs.write", {"path": "zz/y.txt", "content": "x", "make_dirs": False}).code,
                         "not_found")
        self.assertEqual(self.err("fs.write", {"path": "d", "content": "x"}).code, "bad_request")

    @unittest.skipUnless(POSIX, "POSIX mode bits")
    def test_mode_bits(self):
        p = self.fh.touch("run.sh", "echo 1\n")
        os.chmod(p, 0o750)
        self.call("fs.write", {"path": "run.sh", "content": "echo 2\n"})
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o750)
        self.call("fs.write", {"path": "m.txt", "content": "x", "file_mode": "600"})
        self.assertEqual(os.stat(self.fh.p("m.txt")).st_mode & 0o777, 0o600)

    def test_masked_placeholders(self):
        self.store.set("HF_TOKEN", SECRET)
        p = self.fh.touch(".env", "HF_TOKEN=" + SECRET + "\n")
        e = self.err("fs.write", {"path": ".env", "content": "HF_TOKEN=«secret:HF_TOKEN»\n"})
        self.assertEqual(e.code, "conflict")
        self.assertIn("fs_edit", e.message)
        self.assertEqual(p.read_text(), "HF_TOKEN=" + SECRET + "\n")
        self.call("fs.write", {"path": "other.txt", "content": "X=«secret:NOT_STORED»"})
        self.assertEqual(self.fh.p("other.txt").read_text(encoding="utf-8"), "X=«secret:NOT_STORED»")
        b64 = base64.b64encode("t=«secret:HF_TOKEN»".encode()).decode()
        self.assertEqual(self.err("fs.write", {"path": "b.txt", "content": b64, "encoding": "base64"}).code,
                         "conflict")
        self.assertEqual(self.err("fs.edit", {"path": ".env", "old_text": "HF_TOKEN=",
                                              "new_text": "T=«secret:HF_TOKEN»\nHF_TOKEN="}).code, "conflict")
        self.assertEqual(self.err("fs.write_chunk", {"path": "c.bin", "offset": 0, "data": b64}).code, "conflict")

    def test_exec_paths(self):
        e = self.err("fs.write", {"path": "~/.bashrc", "content": "evil"}, ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "exec_path"))
        self.call("fs.write", {"path": "~/.bashrc", "content": "ok"})            # operator on standard
        self.assertEqual(self.fh.p(".bashrc").read_text(), "ok")
        e = self.err("fs.write", {"path": "proj/.git/hooks/pre-commit", "content": "x"}, ident=wp1util.FILES)
        self.assertEqual(self.rule(e), ("policy_denied", "exec_path"))


class TestEdit(FilesCase):
    def test_unique_multi_notfound(self):
        p = self.fh.touch("a.py", "def f():\n    return 1\n\ndef g():\n    return 1\n")
        r = self.call("fs.edit", {"path": "a.py", "old_text": "def f():\n    return 1", "new_text":
                                  "def f():\n    return 2"})
        self.assertEqual(r["replacements"], 1)
        self.assertIn("return 2", r["snippet"])
        self.assertEqual(r["version"], hashlib.sha256(p.read_bytes()).hexdigest())
        e = self.err("fs.edit", {"path": "a.py", "old_text": "return", "new_text": "yield"})
        self.assertEqual(e.code, "conflict")
        self.assertEqual(e.details["lines"], [2, 5])
        r = self.call("fs.edit", {"path": "a.py", "old_text": "return", "new_text": "yield", "replace_all": True})
        self.assertEqual(r["replacements"], 2)
        e = self.err("fs.edit", {"path": "a.py", "old_text": "def g():\n    yeld 1", "new_text": "x"})
        self.assertEqual(e.code, "not_found")
        self.assertEqual(e.details["closest"]["start_line"], 2)
        self.assertTrue(any("def g():" in ln for ln in e.details["closest"]["lines"]))
        e = self.err("fs.edit", {"path": "a.py", "old_text": "yield", "new_text": "x", "if_version": "1" * 64})
        self.assertEqual(e.code, "conflict")

    def test_crlf_and_bom_preserved(self):
        p = self.fh.p("w.txt")
        p.write_bytes(codecs.BOM_UTF8 + b"one\r\ntwo\r\nthree\r\n")
        self.call("fs.edit", {"path": "w.txt", "old_text": "one\ntwo", "new_text": "1\n2\n2b"})
        self.assertEqual(p.read_bytes(), codecs.BOM_UTF8 + b"1\r\n2\r\n2b\r\nthree\r\n")
        u = self.fh.p("u.txt")
        u.write_bytes(codecs.BOM_UTF16_LE + "aé\nb\n".encode("utf-16-le"))
        self.call("fs.edit", {"path": "u.txt", "old_text": "b", "new_text": "中"})
        self.assertEqual(u.read_bytes(), codecs.BOM_UTF16_LE + "aé\n中\n".encode("utf-16-le"))
        bad = self.fh.p("latin.txt")
        bad.write_bytes(b"caf\xe9 au lait\nend\n")
        self.call("fs.edit", {"path": "latin.txt", "old_text": "end", "new_text": "fin"})
        self.assertEqual(bad.read_bytes(), b"caf\xe9 au lait\nfin\n")


class TestSearchList(FilesCase):
    def setUp(self) -> None:
        super().setUp()
        self.fh.touch("proj/a.py", "def foo():\n    return 'FOO'\n# end\n")
        self.fh.touch("proj/sub/b.txt", "foo bar\nnothing\n")
        self.fh.touch("proj/.hidden/c.txt", "foo hidden\n")
        self.fh.touch("proj/node_modules/d.txt", "foo module\n")
        self.fh.p("proj/bin.dat").write_bytes(b"foo\x00\x00")
        self.fh.touch(".ssh/id_rsa", "foo secret key\n")

    def files(self, r: dict) -> set:
        return {os.path.relpath(m["file"], str(self.home)).replace(os.sep, "/") for m in r["matches"]}

    def test_search_filters(self):
        r = self.call("fs.search", {"pattern": "fo+", "path": "proj"})
        self.assertEqual(self.files(r), {"proj/a.py", "proj/sub/b.txt"})
        self.assertFalse(r["truncated"])
        r = self.call("fs.search", {"pattern": "fo+", "path": "proj", "include_hidden": True})
        self.assertIn("proj/.hidden/c.txt", self.files(r))
        r = self.call("fs.search", {"pattern": "foo", "path": "~", "include_hidden": True})
        self.assertNotIn(".ssh/id_rsa", self.files(r))           # denied subtree skipped
        self.assertNotIn("proj/node_modules/d.txt", self.files(r))
        r = self.call("fs.search", {"pattern": "foo", "path": "proj", "glob": "*.py"})
        self.assertEqual(self.files(r), {"proj/a.py"})
        r = self.call("fs.search", {"pattern": "FOO", "path": "proj", "ignore_case": True, "literal": True})
        self.assertEqual(len(r["matches"]), 3)
        r = self.call("fs.search", {"pattern": "return", "path": "proj", "context": 1})
        self.assertEqual((r["matches"][0]["before"], r["matches"][0]["after"]), (["def foo():"], ["# end"]))
        r = self.call("fs.search", {"pattern": "*.txt", "path": "proj", "names_only": True})
        self.assertEqual(self.files(r), {"proj/sub/b.txt"})
        self.assertIsNone(r["matches"][0]["line"])
        self.assertEqual((r["matches"][0]["before"], r["matches"][0]["after"]), ([], []))   # §9.7 row shape
        r = self.call("fs.search", {"pattern": "o", "path": "proj", "max_results": 1})
        self.assertEqual((len(r["matches"]), r["truncated"], r["reason"]), (1, True, "max_results"))
        r = self.call("fs.search", {"pattern": "bar", "path": "proj/sub/b.txt"})
        self.assertEqual(r["matches"][0]["line"], 1)

    def test_regex_rules_and_line_cap(self):
        for bad in ("(a+)+", "(a|b)*", "(a)\\1", "a*b*c*d*e*", "x" * 201):
            with self.subTest(pattern=bad):
                e = self.err("fs.search", {"pattern": bad, "path": "proj"})
                self.assertEqual(e.code, "bad_request")
                self.assertIn("literal=true", e.hint)
        self.call("fs.search", {"pattern": "x" * 400, "path": "proj", "literal": True})
        self.fh.touch("proj/long.txt", "x" * 3000 + "needle\n" + "needle" + "y" * 3000 + "\n")
        r = self.call("fs.search", {"pattern": "needle", "path": "proj/long.txt"})
        self.assertEqual([m["line"] for m in r["matches"]], [2])
        self.assertTrue(r["matches"][0]["cut"])
        self.assertLess(len(r["matches"][0]["text"]), 600)

    def test_search_timeout_partial(self):
        for i in range(20):
            self.fh.touch(f"many/f{i}.txt", "foo\n")
        real = fsops.entry_allowed

        def slow(guard: Any, path: str) -> bool:
            time.sleep(0.1)
            return real(guard, path)
        with mock.patch.object(fsops, "entry_allowed", slow):
            r = self.call("fs.search", {"pattern": "foo", "path": "many"}, timeout=1.2)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["reason"], "timeout")
        self.assertLess(len(r["matches"]), 20)

    def test_list(self):
        r = self.call("fs.list", {"path": "proj"})
        names = [e["path"] for e in r["entries"]]
        self.assertEqual(names, ["a.py", "bin.dat", "node_modules/", "sub/"])
        self.assertEqual(r["entries"][0]["type"], "file")
        self.assertIsNone(r["next_offset"])
        r = self.call("fs.list", {"path": "proj", "depth": 2, "include_hidden": True})
        self.assertIn(".hidden/c.txt", [e["path"] for e in r["entries"]])
        self.assertIn("sub/b.txt", [e["path"] for e in r["entries"]])
        r = self.call("fs.list", {"path": "proj", "depth": 2, "pattern": "*.txt"})
        self.assertEqual([e["path"] for e in r["entries"]], ["node_modules/d.txt", "sub/b.txt"])
        r = self.call("fs.list", {"path": "proj", "limit": 2, "offset": 1})
        self.assertEqual(([e["path"] for e in r["entries"]], r["next_offset"], r["total"]),
                         (["bin.dat", "node_modules/"], 3, 4))
        r = self.call("fs.list", {"path": "proj", "sort": "size"})
        self.assertEqual(r["entries"][0]["path"], "a.py")
        r = self.call("fs.list", {"path": "~", "include_hidden": True})
        self.assertNotIn(".ssh/", [e["path"] for e in r["entries"]])   # denied entries are skipped
        self.assertEqual(self.rule(self.err("fs.list", {"path": "~/.ssh"}))[0], "policy_denied")
        self.assertEqual(self.err("fs.list", {"path": "proj/a.py"}).code, "bad_request")

    @unittest.skipUnless(POSIX, "symlinks")
    def test_list_symlink_outside(self):
        outside = self.fh.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("x")
        os.symlink(str(outside), str(self.fh.p("proj/out")))
        r = self.call("fs.list", {"path": "proj", "depth": 3})
        rows = {e["path"]: e["type"] for e in r["entries"]}
        self.assertEqual(rows.get("out"), "link")
        self.assertFalse(any(k.startswith("out/") for k in rows))
        st = self.call("fs.stat", {"path": "proj/a.py"})
        self.assertEqual(st["type"], "file")
        self.assertEqual(self.rule(self.err("fs.stat", {"path": "proj/out"}))[0], "policy_denied")


class TestStatChunks(FilesCase):
    def test_stat(self):
        r = self.call("fs.stat", {"path": "missing.txt"})
        self.assertEqual(r, {"path": str(self.fh.p("missing.txt")), "exists": False})
        p = self.fh.touch("s.txt", "abc")
        r = self.call("fs.stat", {"path": "s.txt", "sha256": True})
        self.assertEqual((r["exists"], r["type"], r["size"], r["sha256"]),
                         (True, "file", 3, hashlib.sha256(b"abc").hexdigest()))
        self.assertTrue(r["mtime"].endswith("Z"))
        self.assertTrue(r["mode"].startswith("0o"))
        self.assertTrue(r["readable"])
        self.assertEqual(self.call("fs.stat", {"path": "~"})["type"], "dir")
        if POSIX:
            os.symlink(str(p), str(self.fh.p("l.txt")))
            r = self.call("fs.stat", {"path": "l.txt"})
            self.assertEqual((r["type"], r["link_target"], r["size"]), ("link", str(p), 3))

    def test_read_chunk_masks_split_secret(self):
        self.store.set("API_TOKEN", "TOPSECRET-0123456789")
        data = b"A" * 100 + b"TOPSECRET-0123456789" + b"B" * 100
        self.fh.p("blob.bin").write_bytes(data)
        r1 = self.call("fs.read_chunk", {"path": "blob.bin", "offset": 0, "length": 110})
        r2 = self.call("fs.read_chunk", {"path": "blob.bin", "offset": 110, "length": 200})
        c1, c2 = base64.b64decode(r1["data"]), base64.b64decode(r2["data"])
        self.assertTrue(r1.get("masked") and r2.get("masked"))
        self.assertNotIn(b"TOPSECRET", c1)
        self.assertNotIn(b"0123456789", c2)
        self.assertEqual((r1["offset"], r1["length"], r1["size"], r1["eof"]), (0, 110, 220, False))
        self.assertEqual((r2["length"], r2["eof"]), (110, True))
        r = self.call("fs.read_chunk", {"path": "blob.bin", "offset": 0, "length": 50})
        self.assertNotIn("masked", r)
        self.assertEqual(base64.b64decode(r["data"]), b"A" * 50)

    def test_write_chunk_resume_and_sha(self):
        payload = os.urandom(3000)
        parts = [payload[:1000], payload[1000:2000], payload[2000:]]
        b64 = [base64.b64encode(x).decode() for x in parts]
        r = self.call("fs.write_chunk", {"path": "up/f.bin", "data": b64[0], "offset": 0, "total_size": 3000})
        self.assertEqual((r["part_bytes"], r["done"]), (1000, False))
        part = Path(str(self.fh.p("up/f.bin")) + ".hive-part")
        self.assertTrue(part.exists())
        if POSIX:
            self.assertEqual(os.stat(part).st_mode & 0o777, 0o600)
        self.assertIn(str(part), (helpers.util.read_json(self.rt.state_dir / "parts.json") or {})["parts"])
        e = self.err("fs.write_chunk", {"path": "up/f.bin", "data": b64[2], "offset": 2000})
        self.assertEqual((e.code, e.details["part_size"]), ("conflict", 1000))
        self.call("fs.write_chunk", {"path": "up/f.bin", "data": b64[1], "offset": 1000})
        r = self.call("fs.write_chunk", {"path": "up/f.bin", "data": b64[2], "offset": 2000, "final": True,
                                         "sha256": hashlib.sha256(payload).hexdigest()})
        self.assertEqual((r["done"], r["sha256"], r["part_bytes"]), (True, hashlib.sha256(payload).hexdigest(), 3000))
        self.assertEqual(self.fh.p("up/f.bin").read_bytes(), payload)
        self.assertFalse(part.exists())
        self.assertNotIn(str(part), helpers.util.read_json(self.rt.state_dir / "parts.json")["parts"])
        # destination exists -> conflict at offset 0; sha mismatch -> part deleted
        self.assertEqual(self.err("fs.write_chunk", {"path": "up/f.bin", "data": b64[0], "offset": 0}).code,
                         "conflict")
        self.call("fs.write_chunk", {"path": "up/g.bin", "data": b64[0], "offset": 0})
        e = self.err("fs.write_chunk", {"path": "up/g.bin", "data": b64[1], "offset": 1000, "final": True,
                                        "sha256": "0" * 64})
        self.assertEqual(e.code, "conflict")
        self.assertFalse(Path(str(self.fh.p("up/g.bin")) + ".hive-part").exists())
        self.assertFalse(self.fh.p("up/g.bin").exists())
        e = self.err("fs.write_chunk", {"path": "up/h.bin", "data": b64[0], "offset": 0, "total_size": 10 ** 17})
        self.assertEqual(e.code, "too_large")
        if POSIX:   # overwrite keeps the replaced file's mode bits (note 78); new files get 0644
            os.chmod(self.fh.p("up/f.bin"), 0o750)
            self.call("fs.write_chunk", {"path": "up/f.bin", "data": b64[0], "offset": 0, "overwrite": True,
                                         "final": True})
            self.assertEqual(os.stat(self.fh.p("up/f.bin")).st_mode & 0o777, 0o750)
            self.call("fs.write_chunk", {"path": "up/new.bin", "data": b64[0], "offset": 0, "final": True})
            self.assertEqual(os.stat(self.fh.p("up/new.bin")).st_mode & 0o777, 0o644)

    def test_part_prune(self):
        tr = self.rt.parts
        p = self.fh.p("x.bin.hive-part")
        p.write_bytes(b"1")
        keep = self.fh.touch("not-a-part.bin", "1")
        tr.touch(p)
        tr.touch(keep)
        self.assertEqual(tr.prune(), 0)
        self.assertEqual(tr.prune(max_idle_s=-1), 1)
        self.assertFalse(p.exists())
        self.assertTrue(keep.exists())


class TestTreeOps(FilesCase):
    def test_mkdir(self):
        self.assertTrue(self.call("fs.mkdir", {"path": "a/b/c"})["created"])
        self.assertFalse(self.call("fs.mkdir", {"path": "a/b/c"})["created"])
        self.fh.touch("file", "x")
        self.assertEqual(self.err("fs.mkdir", {"path": "file"}).code, "conflict")
        self.assertEqual(self.err("fs.mkdir", {"path": "q/r", "parents": False}).code, "not_found")

    def test_move(self):
        self.fh.touch("m/a.txt", "a")
        self.fh.touch("m/b.txt", "b")
        r = self.call("fs.move", {"src": "m/a.txt", "dst": "m/c.txt"})
        self.assertEqual(r["dst"], str(self.fh.p("m/c.txt")))
        self.assertEqual(self.err("fs.move", {"src": "m/c.txt", "dst": "m/b.txt"}).code, "conflict")
        self.call("fs.move", {"src": "m/c.txt", "dst": "m/b.txt", "overwrite": True})
        self.assertEqual(self.fh.p("m/b.txt").read_text(), "a")
        self.assertEqual(self.err("fs.move", {"src": "m", "dst": "m/inner"}).code, "bad_request")
        self.fh.touch(".ssh/id_rsa", "k")
        self.assertEqual(self.err("fs.move", {"src": "~/.ssh", "dst": "x"}).code, "policy_denied")
        self.assertEqual(self.rule(self.err("fs.move", {"src": "~", "dst": "/tmp/whatever"}))[0], "policy_denied")
        # destination check: a prepared folder moved onto proj/.git by a files token
        self.fh.touch("tmp/y/hooks/pre-commit", "#!/bin/sh\n")
        self.fh.p("proj").mkdir()
        e = self.err("fs.move", {"src": "tmp/y", "dst": "proj/.git"}, ident=wp1util.FILES)
        self.assertEqual(e.code, "policy_denied")
        self.assertTrue(self.fh.p("tmp/y/hooks/pre-commit").exists())
        self.call("fs.move", {"src": "tmp/y", "dst": "proj/.git"})          # operator on standard may

    def test_copy(self):
        self.fh.touch("c/a.txt", "a")
        self.fh.touch("c/sub/b.txt", "bb")
        r = self.call("fs.copy", {"src": "c/a.txt", "dst": "c/a2.txt"})
        self.assertEqual((r["files"], r["bytes"]), (1, 1))
        self.assertEqual(self.err("fs.copy", {"src": "c/a.txt", "dst": "c/a2.txt"}).code, "conflict")
        self.assertEqual(self.err("fs.copy", {"src": "c", "dst": "d"}).code, "bad_request")
        if POSIX:
            os.symlink("a.txt", str(self.fh.p("c/link")))
        r = self.call("fs.copy", {"src": "c", "dst": "d", "recursive": True})
        self.assertEqual(self.fh.p("d/sub/b.txt").read_text(), "bb")
        self.assertEqual(r["bytes"], 1 + 1 + 2)
        if POSIX:
            self.assertTrue(os.path.islink(str(self.fh.p("d/link"))))
        self.assertEqual(self.err("fs.copy", {"src": "c", "dst": "c/inside", "recursive": True}).code,
                         "bad_request")
        self.fh.touch(".config/gcloud/creds.json", "{}")
        self.fh.touch(".config/app.ini", "x")
        self.assertEqual(self.err("fs.copy", {"src": "~/.config", "dst": "cfg", "recursive": True}).code,
                         "policy_denied")

    def test_delete(self):
        self.fh.touch("del/a.txt", "a")
        self.fh.touch("del/sub/b.txt", "b")
        self.assertEqual(self.err("fs.delete", {"path": "del"}).code, "conflict")
        r = self.call("fs.delete", {"path": "del/a.txt"})
        self.assertEqual((r["deleted_files"], r["deleted_dirs"]), (1, 0))
        r = self.call("fs.delete", {"path": "del", "recursive": True})
        self.assertEqual((r["deleted_files"], r["deleted_dirs"]), (1, 2))
        self.assertFalse(self.fh.p("del").exists())
        self.assertEqual(self.err("fs.delete", {"path": "del"}).code, "not_found")
        self.assertEqual(self.call("fs.delete", {"path": "del", "missing_ok": True})["deleted_files"], 0)
        for path in ("~", str(self.home), "/"):
            with self.subTest(path=path):
                self.assertEqual(self.err("fs.delete", {"path": path, "recursive": True}).code, "policy_denied")
        self.fh.touch("e/x", "1")
        self.call("fs.delete", {"path": "e/x"})
        self.call("fs.delete", {"path": "e"})       # empty dir without recursive
        if POSIX:
            target = self.fh.touch("keep.txt", "keep")
            os.symlink(str(target), str(self.fh.p("ln")))
            self.call("fs.delete", {"path": "ln"})
            self.assertTrue(target.exists())
            self.assertFalse(os.path.lexists(str(self.fh.p("ln"))))


class TestDeleteCap(FilesCase):
    policy_overrides = {"fs": {"max_delete_entries": 5}}

    def test_entry_cap(self):
        for i in range(8):
            self.fh.touch(f"big/f{i}", "x")
        self.assertEqual(self.err("fs.delete", {"path": "big", "recursive": True}).code, "too_large")
        self.assertTrue(self.fh.p("big/f0").exists())


class TestPipeline(unittest.TestCase):
    """fs.* through the real NodeRuntime pipeline: registered handlers, validation, step 10a masking."""

    def test_write_read_masked(self):
        with wp1util.FakeHome() as fh:
            rt = helpers.make_runtime(start_managers=False)
            try:
                ident = wp1util.ident(name="test-agent")
                env = rt.call(ident, "fs.write", {"path": "~/notes/a.txt", "content": "key=" + SECRET + "\n"})
                self.assertTrue(env["ok"], env)
                helpers.set_secret(rt, "HF_TOKEN", SECRET)
                env = rt.call(ident, "fs.read", {"path": "~/notes/a.txt"})
                self.assertTrue(env["ok"], env)
                self.assertNotIn(SECRET, env["result"]["content"])
                self.assertIn("«secret:HF_TOKEN»", env["result"]["content"])
                self.assertTrue(env["result"].get("masked"))
                env = rt.call(ident, "fs.stat", {"path": str(fh.data)})
                self.assertFalse(env["ok"])
                self.assertEqual(env["error"]["code"], "policy_denied")
                for name in ("fs.list", "fs.edit", "fs.search", "fs.copy", "fs.delete", "model.chat",
                             "model.endpoints"):
                    self.assertIn(name, rt.implemented())
                self.assertNotIn("model.pull", rt.implemented())
            finally:
                rt.stop()


if __name__ == "__main__":
    unittest.main()
