"""WP3a tests: fsops helpers (design §11.1): encodings, line paging, edit matching, check_regex, walk, PartTracker."""
from __future__ import annotations

import codecs
import os
import tempfile
import time
import unittest
from pathlib import Path

from hive import fsops
from hive.errors import HiveError
from tests import helpers


class TestEncodings(unittest.TestCase):
    def test_detect(self):
        self.assertEqual(fsops.detect_encoding(codecs.BOM_UTF8 + b"x"), ("utf-8-sig", False))
        self.assertEqual(fsops.detect_encoding(codecs.BOM_UTF16_LE + "a".encode("utf-16-le")), ("utf-16-le", False))
        self.assertEqual(fsops.detect_encoding(codecs.BOM_UTF16_BE + "a".encode("utf-16-be")), ("utf-16-be", False))
        self.assertEqual(fsops.detect_encoding("中文".encode()), ("utf-8", False))
        self.assertEqual(fsops.detect_encoding("中文".encode()[:-1]), ("utf-8", False))     # cut multibyte at the end
        self.assertEqual(fsops.detect_encoding("中文".encode("gbk") + b"abc"), ("locale", False))
        self.assertEqual(fsops.detect_encoding(b"ab\x00cd"), ("utf-8", True))
        self.assertEqual(fsops.detect_encoding(b""), ("utf-8", False))

    def test_round_trip(self):
        for data in (codecs.BOM_UTF8 + "é\r\n".encode(), codecs.BOM_UTF16_BE + "é\n".encode("utf-16-be"),
                     b"caf\xe9\n", "plain\n".encode()):
            enc, _ = fsops.detect_encoding(data)
            text, codec, errors = fsops.decode_bytes(data, enc)
            self.assertEqual(fsops.encode_text(text, enc, codec, errors), data)
        self.assertEqual(fsops.display_text("a\udce9b"), "a�b")


class TestLines(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="hivefs-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))

    def test_split_lines(self):
        self.assertEqual(fsops.split_lines("a\r\nb\rc\nd"), ["a\r\n", "b\r", "c\n", "d"])
        self.assertEqual(fsops.split_lines("a\x0cb\n"), ["a\x0cb\n"])           # form feed is not a break
        self.assertEqual(fsops.split_lines(""), [])

    def test_read_lines_large_file_streaming(self):
        p = self.d / "big.txt"
        p.write_text("".join(f"row {i}\r\n" for i in range(1, 2001)), encoding="utf-8")
        old = fsops.WHOLE_FILE_MAX
        fsops.WHOLE_FILE_MAX = 100       # force the streaming path
        try:
            r = fsops.read_lines(p, 1500, 3, 65536)
        finally:
            fsops.WHOLE_FILE_MAX = old
        self.assertEqual(r["content"], "row 1500\r\nrow 1501\r\nrow 1502\r\n")
        self.assertEqual((r["total_lines"], r["version"], r["next_start_line"]), (None, None, 1503))
        r = fsops.read_lines(p, 1999, 10, 65536)
        self.assertEqual((r["end_line"], r["next_start_line"], r["total_lines"]), (2000, None, 2000))

    def test_read_lines_errors(self):
        b = self.d / "b"
        b.write_bytes(b"\x00\x01")
        with self.assertRaises(HiveError) as cm:
            fsops.read_lines(b, 1, 10, 1000)
        self.assertEqual(cm.exception.code, "unsupported")
        with self.assertRaises(HiveError) as cm:
            fsops.read_lines(self.d, 1, 10, 1000)
        self.assertEqual(cm.exception.code, "bad_request")
        with self.assertRaises(HiveError) as cm:
            fsops.read_lines(self.d / "missing", 1, 10, 1000)
        self.assertEqual(cm.exception.code, "not_found")


class TestEdit(unittest.TestCase):
    def test_apply(self):
        self.assertEqual(fsops.apply_edit("a b a", "b", "c", False), ("a c a", 1, [1]))
        self.assertEqual(fsops.apply_edit("x\ny\nx\n", "x", "z", True), ("z\ny\nz\n", 2, [1, 3]))
        with self.assertRaises(HiveError) as cm:
            fsops.apply_edit("x\ny\nx\n", "x", "z", False)
        self.assertEqual((cm.exception.code, cm.exception.details["lines"]), ("conflict", [1, 3]))
        with self.assertRaises(HiveError) as cm:
            fsops.apply_edit("alpha\nbeta\ngamma\ndelta\n", "gamme\ndelta", "x", False)
        self.assertEqual(cm.exception.code, "not_found")
        self.assertEqual(cm.exception.details["closest"]["start_line"], 1)
        self.assertIn("3: gamma", cm.exception.details["closest"]["lines"])

    def test_crlf(self):
        text = "one\r\ntwo\r\n"
        self.assertEqual(fsops.apply_edit(text, "one\ntwo", "1\n2", False)[0], "1\r\n2\r\n")
        self.assertEqual(fsops.apply_edit("a\nb\n", "a\nb", "c\nd", False)[0], "c\nd\n")


class TestCheckRegex(unittest.TestCase):
    def test_rejections(self):
        for bad in ("(a+)+", "(a|b)*", "(a*)*", "(?:x+y)+", "((ab)*c)+", "(a{2,3})*", "(a|b){2,}", "(a)\\1",
                    "(?P<n>a)(?P=n)", "a*b*c*d*e*", "a+b+c+d+e+", "x" * 201, "(unclosed"):
            with self.subTest(pattern=bad):
                with self.assertRaises(HiveError) as cm:
                    fsops.check_regex(bad)
                self.assertEqual(cm.exception.code, "bad_request")
                self.assertEqual(cm.exception.hint, "simplify or use literal=true")

    def test_accepted(self):
        for ok in ("err(or)?", r"\d+\.\d+", "a*b*c*d*", "(ab)*", "(?:foo|bar)?", "[(+*]+x", r"\(a+\)+",
                   "Uvicorn running on", r"^INFO.*started$", "(a|b)c", "x{3}y{2,4}", r"\\1"):
            with self.subTest(pattern=ok):
                self.assertIsNotNone(fsops.check_regex(ok))
        self.assertIsNotNone(fsops.check_regex("y" * 300, max_len=500))


class TestWalk(unittest.TestCase):
    def test_walk_depth_hidden_skip_deadline(self):
        d = Path(tempfile.mkdtemp(prefix="hivewalk-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        for rel in ("a/b/c.txt", ".h/x", "node_modules/m.js", "top.txt"):
            (d / rel).parent.mkdir(parents=True, exist_ok=True)
            (d / rel).write_text("x")
        pol = helpers.FakeEffectivePolicy(workdir=d)
        names = lambda **kw: sorted(os.path.relpath(e.path, d) for e in fsops.walk(  # noqa: E731
            d, guard=pol, deadline=time.monotonic() + 10, **kw))
        self.assertEqual(names(depth=1, include_hidden=False), ["a", "node_modules", "top.txt"])
        self.assertIn(os.path.join("a", "b", "c.txt"), names(depth=3, include_hidden=False))
        self.assertIn(".h", names(depth=1, include_hidden=True))
        self.assertNotIn(os.path.join("node_modules", "m.js"),
                         names(depth=2, include_hidden=False, skip_dirs=("node_modules",)))
        self.assertEqual(list(fsops.walk(d, depth=3, include_hidden=True, guard=pol, deadline=0)), [])
        self.assertTrue(fsops.is_hidden(d / ".h"))
        self.assertEqual(fsops.long_path(d), str(d))


class TestPartTracker(unittest.TestCase):
    def test_touch_done_prune(self):
        d = Path(tempfile.mkdtemp(prefix="hiveparts-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        tr = fsops.PartTracker(d / "state" / "parts.json")
        a, b = d / "a.hive-part", d / "b.hive-part"
        a.write_bytes(b"1")
        b.write_bytes(b"1")
        tr.touch(a)
        tr.touch(b)
        tr.done(b)
        self.assertEqual(set(helpers.util.read_json(d / "state" / "parts.json")["parts"]), {str(a)})
        old = time.time() - 3 * 86400
        os.utime(str(a), (old, old))
        parts = helpers.util.read_json(d / "state" / "parts.json")
        parts["parts"][str(a)] = helpers.util.iso_utc(old)
        helpers.util.write_json_atomic(d / "state" / "parts.json", parts)
        self.assertEqual(tr.prune(), 1)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())               # untracked parts are left alone
        tr.touch(d / "gone.hive-part")            # vanished files are forgotten
        self.assertEqual(tr.prune(), 0)
        self.assertEqual(helpers.util.read_json(d / "state" / "parts.json")["parts"], {})


if __name__ == "__main__":
    unittest.main()
