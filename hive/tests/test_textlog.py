"""WP2 textlog tests (§10.3, §21.2 proctree/shim/textlog row): rotation keeps monotonic offsets, tail/range reads
across rotation, UTF-8/GBK decoding, clean_terminal_text golden tests."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hive import textlog, util


class TestRotation(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivetl-"))
        self.log = self.d / "output.log"

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _write_lines(self, w, n, start=0):
        data = b""
        for i in range(start, start + n):
            line = b"line %05d\n" % i
            w.write(line)
            data += line
        return data

    def test_rotation_keeps_offsets(self):
        w = textlog.RotatingWriter(self.log, max_bytes=3 * 4096, keep=2)   # per file 4096
        everything = self._write_lines(w, 2000)                             # 22000 bytes -> several rotations
        w.close()
        self.assertTrue((self.d / "output.log.1").exists())
        self.assertTrue((self.d / "output.log.2").exists())
        self.assertFalse((self.d / "output.log.3").exists())
        size = textlog.log_size(self.log)
        self.assertEqual(size, len(everything))
        rot = textlog.rotated_bytes(self.log)
        self.assertEqual(rot + self.log.stat().st_size, size)
        # an offset inside the retained window returns exactly the original bytes
        earliest = size - sum(p.stat().st_size for p in self.d.glob("output.log*") if not p.name.endswith("state"))
        r = textlog.read_range(self.log, earliest + 5, 3000, None)
        self.assertEqual(r["data"], everything[earliest + 5:earliest + 5 + 3000])
        self.assertEqual(r["next_offset"], earliest + 5 + 3000)
        self.assertEqual(r["skipped_bytes"], 0)
        self.assertTrue(r["truncated"])
        # a rotated-away offset skips to the earliest byte and reports it
        r = textlog.read_range(self.log, 10, 100, None)
        self.assertEqual(r["offset"], earliest)
        self.assertEqual(r["skipped_bytes"], earliest - 10)
        self.assertEqual(r["data"], everything[earliest:earliest + 100])
        # beyond the end: empty, not an error
        r = textlog.read_range(self.log, size + 50, 100, None)
        self.assertEqual((r["data"], r["offset"], r["next_offset"]), (b"", size, size))

    def test_reopen_continues_offsets(self):
        w = textlog.RotatingWriter(self.log, max_bytes=2048, keep=1)
        a = self._write_lines(w, 300)
        w.close()
        w = textlog.RotatingWriter(self.log, max_bytes=2048, keep=1)
        b = self._write_lines(w, 300, start=300)
        w.close()
        size = textlog.log_size(self.log)
        self.assertEqual(size, len(a) + len(b))
        tail = textlog.read_range(self.log, size - 11, 100, None)["data"]
        self.assertEqual(tail, b"line 00599\n")

    def test_keep_zero_truncates(self):
        w = textlog.RotatingWriter(self.log, max_bytes=4096, keep=0)
        everything = self._write_lines(w, 1000)
        w.close()
        self.assertFalse((self.d / "output.log.1").exists())
        self.assertEqual(textlog.log_size(self.log), len(everything))
        self.assertTrue(textlog.read_range(self.log, 0, 10, None)["skipped_bytes"] > 0)

    def test_tail_across_rotation(self):
        w = textlog.RotatingWriter(self.log, max_bytes=3 * 1024, keep=2)
        self._write_lines(w, 250)
        w.close()
        t = textlog.tail_lines(self.log, 5)
        self.assertEqual(t, b"".join(b"line %05d\n" % i for i in range(245, 250)))
        # a tail spanning the rotation boundary (current file holds fewer lines than requested)
        cur_lines = self.log.read_bytes().count(b"\n")
        t = textlog.tail_lines(self.log, cur_lines + 3)
        self.assertEqual(t.count(b"\n"), cur_lines + 3)
        self.assertTrue(t.endswith(b"line 00249\n"))
        r = textlog.read_range(self.log, None, 4096, 3)
        self.assertEqual(r["data"], b"line 00247\nline 00248\nline 00249\n")
        self.assertEqual(r["next_offset"], r["size"])
        self.assertEqual(r["offset"], r["size"] - len(r["data"]))
        self.assertEqual(textlog.tail_lines(self.log, 1000, max_bytes=33), b"line 00247\nline 00248\nline 00249\n")

    def test_missing_log(self):
        r = textlog.read_range(self.d / "nope.log", None, 100, 10)
        self.assertEqual((r["data"], r["size"]), (b"", 0))
        self.assertEqual(textlog.tail_lines(self.d / "nope.log", 3), b"")
        self.assertEqual(textlog.read_at(self.d / "nope.log", 0, 5), b"")


class TestDecoding(unittest.TestCase):
    def test_decode_chunk_drops_partial_utf8(self):
        s = "中文ok".encode("utf-8")
        for cut in range(1, len(s) + 1):
            text, used = textlog.decode_chunk(s[:cut])
            self.assertTrue("中文ok".startswith(text), (cut, text))
            self.assertEqual(s[:used].decode("utf-8"), text)
        self.assertEqual(textlog.decode_chunk("中".encode("utf-8")[:2]), ("", 0))
        self.assertEqual(textlog.decode_chunk(b"abc"), ("abc", 3))

    def test_gbk_decoding(self):
        raw = "编码".encode("gbk")
        text, used = textlog.decode_chunk(raw)
        self.assertEqual(used, len(raw))
        if util.IS_WINDOWS:   # pragma: no cover - Windows decodes with mbcs/gbk
            self.assertEqual(text, "编码")
        else:                  # elsewhere undecodable bytes become replacement chars, never an exception
            self.assertTrue(text)
        with mock.patch.object(util, "IS_WINDOWS", True):
            # the Windows path tries mbcs (absent off Windows) then gbk
            self.assertEqual(util.decode_text(raw), "编码")


class TestCleanTerminal(unittest.TestCase):
    def test_golden(self):
        cases = [
            ("a\r\nb\r\n", "a\nb\n"),
            ("\x1b[31mred\x1b[0m plain", "red plain"),
            ("\x1b]0;title\x07after", "after"),
            ("\x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\", "link"),
            ("progress 10%\rprogress 50%\rprogress 100%\n", "progress 100%\n"),
            ("done\r\n", "done\n"),
            ("abc\b\bX", "aX"),
            ("x\x1bcy", "xy"),
            ("\x1b[2K\x1b[1Gcleared", "cleared"),
        ]
        for raw, want in cases:
            self.assertEqual(textlog.clean_terminal_text(raw), want, raw)

    def test_repeats_collapse(self):
        s = "\n".join(["a"] + ["same"] * 6 + ["b"])
        self.assertEqual(textlog.clean_terminal_text(s), "a\nsame\n⟨previous line repeated 5 times⟩\nb")
        s3 = "same\nsame\nsame\nend"
        self.assertEqual(textlog.clean_terminal_text(s3), s3)
        self.assertEqual(textlog.clean_terminal_text(""), "")

    def test_last_line(self):
        d = Path(tempfile.mkdtemp(prefix="hivetl-"))
        try:
            p = d / "x.log"
            p.write_bytes(b"first\n\x1b[31mError: boom\x1b[0m\n\n")
            self.assertEqual(textlog.last_line(p), "Error: boom")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
