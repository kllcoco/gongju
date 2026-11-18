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


class TestEditDoSVerification4(unittest.TestCase):
    """Verification round 4 (DoS): fs.edit's not_found / conflict / replace_all paths and literal search are linear
    (closest_window was cubic in old_text lines, line numbers were matches x file size, Python 3.9's str.find is
    O(n*m) on 'A'*k+'B'+'A'*k). Generous bounds: these used to take seconds to hours."""

    def timed(self, fn, bound=1.0):
        t = time.monotonic()
        try:
            out = fn()
        except HiveError as e:
            out = e
        self.assertLess(time.monotonic() - t, bound)
        return out

    def test_closest_window_bounded(self):
        e = self.timed(lambda: fsops.apply_edit("a\n" * 8192, "b\n" * 800, "x", False))       # 16 KB / 1.6 KB
        self.assertEqual(e.code, "not_found")
        self.assertIsNotNone(e.details["closest"])
        e = self.timed(lambda: fsops.apply_edit("a\n" * 8192, "\n" * (1 << 20), "x", False))  # 1 MB old_text
        self.assertEqual(e.code, "not_found")
        r = self.timed(lambda: fsops.closest_window("a\n" * (1 << 20), "\n" * (1 << 20) + "x"))
        self.assertTrue(200 <= len(r["lines"]) <= 204)      # 200 compared lines ± 2 of context
        self.assertIsNone(fsops.closest_window("a\nb\n" * 1000, "c\n" * 50, deadline=time.monotonic() - 1))
        e = self.timed(lambda: fsops.apply_edit("a\nb\n", "zzz", "x", False, deadline=time.monotonic() - 1))
        self.assertEqual(e.code, "not_found")
        self.assertIsNone(e.details["closest"])

    def test_closest_window_quality(self):
        text = "".join(f"def f{i}(x):\n    return x + {i}\n\n" for i in range(2000))
        r = fsops.closest_window(text, "def f1234(x):\n    retrun x + 1234")
        self.assertIn("3703: def f1234(x):", r["lines"])
        r = fsops.closest_window(text, "    def f777(x):\n        return x + 777")    # indentation differs
        self.assertIn("2332: def f777(x):", r["lines"])

    def test_many_matches_linear(self):
        text = "a\n" * (8 << 20)                                                       # 16 MB, 8M matches
        e = self.timed(lambda: fsops.apply_edit(text, "a", "b", False))
        self.assertEqual(e.code, "conflict")
        self.assertEqual(e.details["lines"], list(range(1, 101)))
        self.assertIn("matches 8388608 times", e.message)
        new, count, lines = self.timed(lambda: fsops.apply_edit(text, "a", "b", True))
        self.assertEqual((count, lines[:3], len(lines), new[:4]), (8 << 20, [1, 2, 3], 100, "b\nb\n"))

    def test_long_needle_linear(self):
        k = 50_000
        old = "A" * k + "B" + "A" * k                                                  # 100 KB, 2 MB file
        new, count, lines = self.timed(lambda: fsops.apply_edit("A" * 2_000_000 + old + "\n", old, "x", False))
        self.assertEqual((new, count, lines), ("A" * 2_000_000 + "x\n", 1, [1]))
        e = self.timed(lambda: fsops.apply_edit("A" * 2_000_000, old, "x", False))
        self.assertEqual(e.code, "not_found")
        e = self.timed(lambda: fsops.apply_edit("A" * 2_000_000 + old + old, old, "x", False))
        self.assertEqual(e.code, "conflict")
        new, count, _l = self.timed(lambda: fsops.apply_edit("A" * 2_000_000 + old + old, old, "x", True))
        self.assertEqual((new, count), ("A" * 2_000_000 + "xx", 2))
        from hive.actions import files
        needle = "A" * 250 + "B" + "A" * 249
        hits = self.timed(lambda: list(files._literal_hits(["A" * (16 << 20), "x" + needle], needle, False,
                                                            time.monotonic() + 60)))
        self.assertEqual(hits, [1])

    def test_literal_search_matches_str(self):
        import random
        import re
        rnd = random.Random(4)
        for _ in range(300):
            hay = "".join(rnd.choice("ab\n") for _ in range(rnd.randint(0, 60)))
            needle = "".join(rnd.choice("ab") for _ in range(rnd.randint(1, 4)))
            for force in (False, True):
                ls = fsops.LiteralSearch(needle)
                if force:
                    ls._rx = re.compile(re.escape(needle))
                self.assertEqual((ls.find(hay), ls.find(hay, 3), ls.count(hay), ls.contains(hay),
                                  ls.replace(hay, "\\1")),
                                 (hay.find(needle), hay.find(needle, 3), hay.count(needle), needle in hay,
                                  hay.replace(needle, "\\1")))

    def test_snippet_offset(self):
        for text in ("l1\nl2\nl3\nl4\nl5\nNEW\nl7\nl8\nl9\nl10\n", "a\r\nb\r\nNEW\r\nc\r\n",
                     "NEW", "x\nNEW", "1\n2\n3\n4\nNEW\nmore\n"):
            off = text.index("NEW")
            line = fsops._line_of(text, off)
            self.assertEqual(fsops.snippet(text, line, 1, offset=off), fsops.snippet(text, line, 1))


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


class TestReviewFixes(unittest.TestCase):
    """WP3a review fixes: verbose/bounded-nested regexes, walk truncation flag, read_lines check/mask/FIFO."""

    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="hivefs-")).resolve()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))

    def test_regex_verbose_and_bounded_nesting(self):
        for bad in ("(?x) ( a + ) + $", "(?x)#[\n(a+)+$\n]", "(?ix)a", "(?x:a b)", "(a?){22}a{22}",
                    "((a?){5}){5}", "(a|b){11}", "(?:a+){3,40}"):
            with self.subTest(pattern=bad):
                with self.assertRaises(HiveError) as cm:
                    fsops.check_regex(bad)
                self.assertEqual(cm.exception.code, "bad_request")
        for ok in ("(?i)abc", "(?i:x)", r"(\d{1,3}\.){3}\d{1,3}", "(?:ab|cd){3}", "(ab){100}", "x{1000}",
                   "((a?){2}){5}"):
            with self.subTest(pattern=ok):
                self.assertIsNotNone(fsops.check_regex(ok))

    def test_walk_reports_truncation(self):
        for i in range(3):
            (self.d / f"f{i}").write_text("x")
        pol = helpers.FakeEffectivePolicy(workdir=self.d)
        st: dict = {}
        self.assertEqual(list(fsops.walk(self.d, depth=2, include_hidden=True, guard=pol, deadline=0, state=st)), [])
        self.assertTrue(st["truncated"])
        st = {}
        self.assertEqual(len(list(fsops.walk(self.d, depth=2, include_hidden=True, guard=pol,
                                             deadline=time.monotonic() + 10, state=st))), 3)
        self.assertFalse(st["truncated"])

    def test_read_lines_check_called_while_streaming(self):
        p = self.d / "big.txt"
        p.write_text("x" * 100 + "\n" + ("y" * 70000 + "\n") * 4, encoding="utf-8")
        calls = []

        def check() -> None:
            calls.append(1)
            if len(calls) >= 2:
                raise HiveError("timeout", "deadline", {"maybe_executed": False})
        old = fsops.WHOLE_FILE_MAX
        fsops.WHOLE_FILE_MAX = 100
        try:
            with self.assertRaises(HiveError) as cm:
                fsops.read_lines(p, 10 ** 9, 10, 65536, check=check)
        finally:
            fsops.WHOLE_FILE_MAX = old
        self.assertEqual(cm.exception.code, "timeout")

    def test_read_lines_masks_cut_edges(self):
        secret = "SECRETVALUE-0123456789"

        def mask(kept: str, rest: str):
            both = kept + rest[:len(secret) - 1]
            i = both.find(secret)
            if i < 0 or i >= len(kept) or i + len(secret) <= len(kept):
                return kept, False
            return kept[:i] + "«secret:S»", True
        p = self.d / "one.txt"
        p.write_text("a" * 1990 + secret + "tail\n", encoding="utf-8")
        r = fsops.read_lines(p, 1, 10, 65536, mask=mask)
        self.assertNotIn(secret[:10], r["content"])
        self.assertIn("«secret:S»…[+", r["content"])
        self.assertTrue(r["masked"])
        q = self.d / "two.txt"
        q.write_text("b" * 1020 + secret + "\n", encoding="utf-8")
        r = fsops.read_lines(q, 1, 10, 1030, mask=mask)
        self.assertEqual(r["content"], "b" * 1020 + "«secret:S»")
        self.assertTrue(r["masked"])
        r = fsops.read_lines(q, 1, 10, 1030)
        self.assertNotIn("masked", r)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "FIFOs")
    def test_fifo_refused_without_blocking(self):
        f = self.d / "pipe"
        os.mkfifo(str(f))
        t0 = time.monotonic()
        for fn in (lambda: fsops.read_lines(f, 1, 10, 65536), lambda: fsops.open_regular(f)):
            with self.assertRaises(HiveError) as cm:
                fn()
            self.assertEqual(cm.exception.code, "bad_request")
        self.assertLess(time.monotonic() - t0, 2)


if __name__ == "__main__":
    unittest.main()
