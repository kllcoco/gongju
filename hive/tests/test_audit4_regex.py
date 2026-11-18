"""Audit round 4 (DoS / local users), regex half: linear-time redaction and secret masking on megabyte adversarial
inputs, bounded proc.list / job command redaction (findings 1, 2); fs.search / ready_pattern regexes in the
_regexworker child with a hard kill, tightened check_regex (finding 4). Bounds are generous: the old code took
minutes. Also the btime FIFO (finding 5) and model listing caps (finding 8)."""
from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from unittest import mock

from hive import audit as audit_mod
from hive import fsops
from hive._regexworker import RegexWorker
from hive.errors import HiveError
from hive.audit import redact_env, redact_head, redact_params, redact_text
from hive.secretstore import SecretStore

MB = 1 << 20
PATTERNS = ["A", "a1", ":", "/", "=", "-", " ", "a:", "a=", "KEY", "--key", "mysql ", "mysql -p", "mysql -p ",
            "authorization:", "hv_", "_", "aA_-", "xoxa-", '"', "--x ", "\t", "\n", "a@", "a://", "-a", "--key=",
            "--key \"", "--token '", "KEY_", "PASS=", "mysql\n", "mysql;", " -p", "x://a", "Authorization: ", "é",
            "--a-key-", "hf_", "sk-", "://", "--password "]


def adversarial(n: int = MB):
    for p in PATTERNS:
        yield p, (p * (n // len(p) + 1))[:n]
    yield "mysqlA", "mysql" + "A" * n
    yield "--keyA=", "--key" + "A" * n + "="
    yield "KEYA", "KEY" + "A" * n


class Timed:
    def __init__(self, case, label, bound):
        self.case, self.label, self.bound = case, label, bound

    def __enter__(self):
        self.t = time.perf_counter()

    def __exit__(self, *exc):
        took = time.perf_counter() - self.t
        self.case.assertLess(took, self.bound, f"{self.label} took {took:.2f}s")


class TestLinearRedaction(unittest.TestCase):
    def test_redact_text_megabyte_inputs(self):
        # whole-text redaction is linear (~0.3 s/MB here; 256 KB keeps the suite fast); the quadratic rules took
        # over a minute on 200 KB and hours on 1 MB
        for name, s in adversarial(256 * 1024):
            with self.subTest(pattern=name), Timed(self, f"redact_text({name!r})", 1.0):
                redact_text(s)

    def test_redact_params_env_megabyte_inputs(self):
        # audit params are cut before redaction: a megabyte value costs milliseconds (200 KB took 54 s+ before)
        for name, s in adversarial():
            with self.subTest(pattern=name), Timed(self, f"redact_params({name!r})", 0.5):
                out = redact_params("shell.run", {"command": s, "argv": [s, "x"], "path": s, "q": [s]})
            self.assertLess(len(out["command"]), audit_mod._COMMAND_MAX + 40)
            self.assertLess(len(out["path"]), audit_mod._STR_MAX + 40)
            with self.subTest(pattern=name), Timed(self, f"redact_env({name!r})", 1.0):
                redact_env({"A": s[:256 * 1024], "MY_TOKEN": s})

    def test_command_display_megabyte_inputs(self):
        from hive.actions import system
        from hive.jobs import display_command
        for name, s in adversarial():
            with self.subTest(pattern=name), Timed(self, f"proc/job cmd({name!r})", 0.5):
                cmd = system._cmd_text([s, s])
                flt = system._filter_text([s])
                disp = display_command([s, s])
            self.assertLessEqual(len(cmd), system.CMD_MAX)
            self.assertLessEqual(len(flt), system.FILTER_MAX)
            self.assertLessEqual(len(disp), 200)

    def test_other_hot_regexes_linear(self):
        # found by scanning every module-level regex of the package with adversarial inputs
        from hive import hfhub, presets
        for s in ("\n" * MB, "; " + " " * MB, "(" * MB, "do" + "\n" * MB):
            with self.subTest(text=s[:4]), Timed(self, "presets._DYNAMIC_TEXT_RE", 0.5):
                presets.text_may_change_cwd(s)
        for s in ("<" * 65536, "<" + "a" * 65536 + ">; rel=next"):
            with Timed(self, "hfhub.LINK_NEXT_RE", 0.5):
                hfhub.LINK_NEXT_RE.search(s)
        from hive import _download, policy
        for s in ("hive " * (MB // 5), "/hive " * (MB // 6), "-m hive " * (MB // 8), "-m" + " " * MB):
            with self.subTest(cmd=s[:6]), Timed(self, "policy._is_hive_cmd", 0.5):
                self.assertFalse(policy._is_hive_cmd(s))
        self.assertTrue(policy._is_hive_cmd("x\n/usr/bin/python3 -m hive node run --config c"))
        self.assertFalse(policy._is_hive_cmd("hive\nnode"))
        with Timed(self, "_download._disposition_name", 0.5):
            _download._disposition_name("filename*=" * 6500)
        self.assertEqual(_download._disposition_name("attachment; filename*=UTF-8''na%C3%AFve.txt"), "naïve.txt")
        self.assertTrue(presets.text_may_change_cwd("a;\n  $X"))
        self.assertTrue(presets.text_may_change_cwd("x\n. ./env"))
        self.assertEqual(hfhub.LINK_NEXT_RE.search('<https://h/x?p=2>; rel="next"').group(1), "https://h/x?p=2")


class TestRedactHead(unittest.TestCase):
    def test_short_text_unchanged_semantics(self):
        self.assertEqual(redact_head("curl --token=abc x", 100), ("curl --token=*** x", False))
        self.assertEqual(redact_head("a" * 10, 5), ("aaaaa", True))

    def test_cut_word_is_dropped_not_half_redacted(self):
        # a key prefix rule needs 20+ chars: the half token at the window edge must not be shown
        s = "x " * 20 + "hf_" + "Q" * 40
        out, cut = redact_head(s + " tail" * 100, 25)
        self.assertTrue(cut)
        self.assertNotIn("QQQ", out)
        head = "w " * 70                                  # 140 chars, window for limit 30 is 124
        out, cut = redact_head(head + "hf_abcdefghijklmnop" + "z" * 4000, 30)
        self.assertTrue(cut)
        self.assertNotIn("hf_", out)

    def test_secret_value_across_the_window_is_masked_whole(self):
        secret = "S3cr3tValue" + "v" * 3000
        audit_mod.set_text_mask(lambda t: t.replace(secret, "«secret:X»"))
        try:
            s = "run " + secret + " and more " * 100
            out, cut = redact_head(s, 500)
            self.assertTrue(cut)
            self.assertNotIn("S3cr3t", out)
            self.assertIn("«secret:X»", out)
            short = audit_mod._short(s, 500)
            self.assertNotIn("S3cr3t", short)
            self.assertRegex(short, r"…\[\d+ chars\]$")
        finally:
            audit_mod.set_text_mask(None)

    def test_rules_still_redact(self):
        cases = {
            "mysql -u root -p hunter2 db": "mysql -u root -p *** db",
            "mysql -u root\n-phunter2": "mysql -u root\n-p***",
            "mysql -h h -P 3306 -p x; psql -pS": "mysql -h h -P *** -p x; psql -p***",
            "echo x://u:p@h; " + "a" * 100 + "http://user:pw@host": "echo x://***@h; " + "a" * 100 + "http://***@host",
            "--my-api-key-file=/x --other v": "--my-api-key-file=*** --other v",
            "X_MY_TOKEN_2=abc Y=1": "X_MY_TOKEN_2=*** Y=1",
            "a-b --token  'q x' y": "a-b --token  *** y",
        }
        for s, want in cases.items():
            with self.subTest(s=s):
                self.assertEqual(redact_text(s), want)
        self.assertEqual(redact_params("x", {"argv": ["tool", "--api-key", "v", "--x" * 200]})["argv"][:3],
                         ["tool", "--api-key", "***"])


class TestSecretMaskLinear(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hive-a4-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.st = SecretStore(self.tmp / "secrets.json", passthrough=(), env={})

    def test_long_prefix_secret_on_megabyte_text(self):
        # the old alternation regex re-compared the value at every position: ~50 s for 1 MB of 'A'
        self.st.set("LONG", "A" * 4095 + "B")
        self.st.set("SHORT", "abcdefgh12")
        for s in ("A" * MB, "abcdefgh1" * (MB // 9), "AB" * (MB // 2)):
            with Timed(self, "mask", 0.5):
                self.st.mask(s)

    def test_overlapping_values_masked_as_union(self):
        self.st.set("ONE", "abcdefgh")
        self.st.set("TWO", "efghijklmn")
        self.assertEqual(self.st.mask("xx abcdefghijklmn yy"), "xx «secret:ONE» yy")
        self.assertEqual(self.st.mask("abcdefghabcdefgh"), "«secret:ONE»«secret:ONE»")
        self.st.set("THREE", "abcdefghijk")
        self.assertEqual(self.st.mask("-abcdefghijk-"), "-«secret:THREE»-")
        self.assertEqual(self.st.mask("nothing here"), "nothing here")


class TestCheckRegexTightened(unittest.TestCase):
    def test_polynomial_patterns_rejected(self):
        for bad in ("A*A*A*A*c", "A*A*c", r"\w+\d+", r".*\s*x", ".*a.*a.*c", "a.*a.*a.*b", "(?:a*)(?:a*)b",
                    "[ab]*a?[a-c]*x", "(?i)a*A*b", ".+.+x", "(a+)+", "(a|b)*"):
            with self.subTest(pattern=bad), self.assertRaises(HiveError) as cm:
                fsops.check_regex(bad)
            self.assertEqual(cm.exception.code, "bad_request")
            self.assertIn("literal=true", cm.exception.hint)
        with self.assertRaises(HiveError):
            fsops.check_regex("a*A*b", flags=2)          # re.IGNORECASE from ignore_case=true

    def test_common_patterns_accepted(self):
        for good in ("foo", ".*foo.*", r"def \w+\(.*\)", r"\w+\s*=\s*\w+", r"\d+\.\d+", "class .*Error.*:",
                     "[a-z]+[0-9]*", r"^\s*#.*$", "import (os|sys)", "foo.*bar.*baz", "TODO|FIXME", "a.*b.*c",
                     r"https?://\S+", r"\s+$", "(?:ab)+c", "x{2,}y", r"\$\{\w+\}", "(?i)error: .*"):
            with self.subTest(pattern=good):
                fsops.check_regex(good)


class TestRegexWorker(unittest.TestCase):
    def test_matches_in_child(self):
        with RegexWorker("b+", 0, deadline=time.monotonic() + 30) as w:
            self.assertEqual(w.search_lines(["abc", "x", "bb", "\udcff b"]), [0, 2, 3])
            self.assertEqual(w.search_lines([]), [])
        with RegexWorker("B", 2, deadline=time.monotonic() + 30) as w:
            self.assertEqual(w.search_lines(["abc"]), [0])
        w = RegexWorker("(", 0, deadline=time.monotonic() + 30)
        self.assertIsNotNone(w.error)
        self.assertIsNone(w.search_lines(["("]))

    def test_catastrophic_regex_is_killed_and_node_keeps_running(self):
        # (a+)+$ on 40 a's runs for ages in-process (holding the GIL); in the child it is killed at the deadline
        ticks = []
        stop = threading.Event()

        def ticker():
            while not stop.is_set():
                ticks.append(time.monotonic())
                time.sleep(0.01)
        th = threading.Thread(target=ticker, daemon=True)
        th.start()
        t0 = time.monotonic()
        with RegexWorker("(a+)+$", 0, deadline=time.monotonic() + 1.0) as w:
            self.assertIsNone(w.search_lines(["a" * 40 + "!"]))
            self.assertTrue(w.timed_out)
            proc = w.proc
        took = time.monotonic() - t0
        stop.set()
        th.join(5)
        self.assertLess(took, 8.0)
        self.assertIsNotNone(proc.poll())                   # reaped
        self.assertGreater(len([t for t in ticks if t0 < t < t0 + 1.0]), 10)   # this process never froze

    def test_cancel_kills(self):
        ev = threading.Event()
        threading.Timer(0.3, ev.set).start()
        t0 = time.monotonic()
        with RegexWorker("(a+)+$", 0, deadline=time.monotonic() + 60, cancel=ev) as w:
            self.assertIsNone(w.search_lines(["a" * 40 + "!"]))
            self.assertTrue(w.cancelled)
        self.assertLess(time.monotonic() - t0, 8.0)

    def test_ready_pattern_runs_in_child(self):
        from hive import services
        self.assertTrue(services.ServiceManager._ready_match(r"READY\b", "boot\nserver READY now\n"))
        self.assertFalse(services.ServiceManager._ready_match(r"READY\b", "READYX\n"))
        t0 = time.monotonic()
        with mock.patch.object(services, "READY_MATCH_S", 0.5):
            self.assertFalse(services.ServiceManager._ready_match("(a+)+$", "a" * 40 + "!\n"))
        self.assertLess(time.monotonic() - t0, 8.0)


class TestSearchUsesWorker(unittest.TestCase):
    def setUp(self):
        from tests import helpers, wp1util
        from hive.actions import load_all
        load_all()
        self.fh = wp1util.FakeHome()
        self.fh.__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)
        self.pol = wp1util.build_policy(self.fh, "standard")
        self.rt = helpers.FakeRuntime(workdir=self.fh.home)
        self.addCleanup(self.rt.cleanup)
        self.helpers, self.wp1util = helpers, wp1util

    def call(self, params, timeout=30.0):
        from hive import catalog, schema
        from hive.actions import HANDLERS
        eff = self.pol.effective(self.wp1util.OPERATOR, paused=False)
        ctx = self.helpers.make_ctx("fs.search", runtime=self.rt, identity=self.wp1util.OPERATOR, policy=eff,
                                    timeout=timeout)
        return HANDLERS["fs.search"](ctx, schema.validate(catalog.SPECS["fs.search"].params, params))

    def test_audit_example_rejected_and_killed(self):
        self.fh.touch("d/f.txt", "A" * 250 + "\nhello\n")
        with self.assertRaises(HiveError):
            self.call({"pattern": "A*A*A*A*c", "path": "d"})
        # even a pattern that slipped past check_regex cannot freeze the node: the child is killed at the deadline
        t0 = time.monotonic()
        with mock.patch.object(fsops, "check_regex", lambda *a, **k: None):
            r = self.call({"pattern": "A*A*A*A*c", "path": "d"}, timeout=2.5)
        self.assertLess(time.monotonic() - t0, 10.0)
        self.assertTrue(r["truncated"])
        self.assertEqual(r["reason"], "timeout")

    def test_regex_never_matched_in_process(self):
        self.fh.touch("d/f.txt", "x\nfoo 1\n" + "y" * 1500 + "foo\n" + "foo" + "z" * 1500 + "\n")
        real = fsops.re.compile

        class NoMatch:
            def __init__(self, rx):
                self.flags = rx.flags

            def __getattr__(self, name):
                raise AssertionError(f"agent regex used in the node process ({name})")

        def guarded(pattern, flags=0):
            rx = real(pattern, flags)
            return NoMatch(rx) if pattern == "fo+" else rx      # check_regex may compile (validation), not match
        with mock.patch("re.compile", guarded):
            r = self.call({"pattern": "fo+", "path": "d"})
        self.assertEqual([(m["line"], bool(m.get("cut"))) for m in r["matches"]], [(2, False), (4, True)])
        r = self.call({"pattern": "FOO", "path": "d", "ignore_case": True, "literal": True})
        self.assertEqual(len(r["matches"]), 3)


@unittest.skipUnless(hasattr(os, "mkfifo") and os.path.exists("/proc/sys/kernel/random/boot_id"), "Linux only")
class TestBtimeFifo(unittest.TestCase):
    def setUp(self):
        from hive import util
        self.util = util
        self.tmp = Path(tempfile.mkdtemp(prefix="hive-a4bt-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.shared = self.tmp / "shm"
        self.shared.mkdir()
        os.chmod(self.shared, 0o1777)
        self.private = self.tmp / "run"
        self.private.mkdir(mode=0o700)
        self.name = f"hive-btime-{os.getuid()}-{util._linux_boot_id()}"
        self.fifos = []

    def fifo(self, path: Path) -> None:
        os.mkfifo(str(path), 0o600)
        self.fifos.append(path)

    def tearDown(self):
        for f in self.fifos:             # release a reader that might still be blocked
            try:
                fd = os.open(str(f), os.O_WRONLY | os.O_NONBLOCK)
                os.close(fd)
            except OSError:
                pass

    def run_btime(self, private_dirs, legacy_dirs):
        res = {}
        u = self.util

        def go():
            with mock.patch.object(u, "_BTIME_CACHE", None), \
                    mock.patch.object(u, "_btime_dirs", lambda create=False: [str(d) for d in private_dirs]), \
                    mock.patch.object(u, "_legacy_btime_dirs", lambda: [str(d) for d in legacy_dirs]):
                res["v"] = u._stable_btime()
        th = threading.Thread(target=go, daemon=True)
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "btime read blocked")
        return res["v"]

    def test_fifo_in_shared_or_private_dir_never_blocks(self):
        self.fifo(self.shared / self.name)
        v = self.run_btime([self.private], [self.shared])
        self.assertAlmostEqual(v, self.util._linux_btime(), delta=5)
        self.assertTrue((self.private / self.name).is_file())
        (self.private / self.name).unlink()
        self.fifo(self.private / self.name)
        v = self.run_btime([self.private], [])
        self.assertAlmostEqual(v, self.util._linux_btime(), delta=5)

    def test_only_private_regular_files_count(self):
        bad = self.private / self.name
        bad.write_text("12345")
        os.chmod(bad, 0o644)
        v = self.run_btime([self.private], [])
        self.assertNotEqual(v, 12345.0)
        bad.unlink()
        legacy = self.shared / self.name
        legacy.write_text("1700000000.0")
        os.chmod(legacy, 0o600)
        (self.private / f"hive-btime-{os.getuid()}-old-boot").write_text("1")
        self.assertEqual(self.run_btime([self.private], [self.shared]), 1700000000.0)   # carried over mid-boot
        self.assertEqual((self.private / self.name).read_text(), "1700000000.0")
        self.assertFalse((self.private / f"hive-btime-{os.getuid()}-old-boot").exists())  # earlier boots pruned

    def test_shared_dirs_are_never_btime_dirs(self):
        u = self.util
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.shared), "HIVE_HOME": str(self.tmp / "hh")}):
            dirs = u._btime_dirs(create=True)
        self.assertNotIn(str(self.shared), dirs)
        self.assertNotIn("/dev/shm", dirs)
        self.assertNotIn(tempfile.gettempdir(), dirs)
        self.assertIn(str(self.tmp / "hh" / "run"), dirs)
        self.assertEqual(os.stat(self.tmp / "hh" / "run").st_mode & 0o777, 0o700)
        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(self.private)}):
            self.assertEqual(u._btime_dirs()[0], str(self.private))


class TestListingCaps(unittest.TestCase):
    """Finding 8: a hostile mirror cannot make the node hold an unbounded listing."""

    def setUp(self):
        from hive import hfhub
        self.hf = hfhub
        hfhub.clear_cache()
        self.addCleanup(hfhub.clear_cache)
        self.calls = []

    def fake(self, pages):
        import json as _json
        from types import SimpleNamespace
        sha = "a" * 40

        def req(url, guard, auth, end):
            self.calls.append(url)
            if "/revision/" in url:
                body = {"sha": sha}
                headers = {}
            else:
                i = int(url.rsplit("page=", 1)[1]) if "page=" in url else 0
                body = pages[i]
                headers = {"Link": f'<https://hf.test/next?page={i + 1}>; rel="next"'} if i + 1 < len(pages) else {}
            return SimpleNamespace(status=200, body=_json.dumps(body).encode(), headers=headers, truncated=False)
        return req

    def entries(self, n, start=0):
        return [{"type": "file", "path": f"f{i}.bin", "size": 1, "oid": "b" * 40} for i in range(start, start + n)]

    def list(self, source="huggingface"):
        return self.hf.list_repo(source, "https://hf.test", "org/repo", "model", "main", guard=None, auth=None,
                                 deadline=time.monotonic() + 30)

    def test_entry_cap_across_pages(self):
        pages = [self.entries(600), self.entries(600, 600)]
        with mock.patch.object(self.hf, "MAX_ENTRIES", 1000), mock.patch.object(self.hf, "_request", self.fake(pages)):
            with self.assertRaises(HiveError) as cm:
                self.list()
        self.assertEqual(cm.exception.code, "too_large")
        self.assertIn("net.download", cm.exception.hint)
        self.assertEqual(cm.exception.details.get("max_entries"), 1000)

    def test_modelscope_entry_cap(self):
        import json as _json
        from types import SimpleNamespace
        body = {"Code": 200, "Data": {"Files": [{"Type": "blob", "Path": f"f{i}", "Size": 1} for i in range(50)]}}

        def req(url, guard, auth, end):
            return SimpleNamespace(status=200, body=_json.dumps(body).encode(), headers={}, truncated=False)
        with mock.patch.object(self.hf, "MAX_ENTRIES", 40), mock.patch.object(self.hf, "_request", req):
            with self.assertRaises(HiveError) as cm:
                self.list("modelscope")
        self.assertEqual(cm.exception.code, "too_large")

    def test_only_small_listings_are_cached(self):
        with mock.patch.object(self.hf, "CACHE_MAX_ENTRIES", 20), \
                mock.patch.object(self.hf, "_request", self.fake([self.entries(30)])):
            self.assertEqual(len(self.list()[1]), 30)
            n = len(self.calls)
            self.list()
            self.assertEqual(len(self.calls), 2 * n)          # listed again: too big to cache
        self.hf.clear_cache()
        self.calls.clear()
        with mock.patch.object(self.hf, "CACHE_MAX_ENTRIES", 20), \
                mock.patch.object(self.hf, "_request", self.fake([self.entries(10)])):
            self.list()
            n = len(self.calls)
            self.list()
            self.assertEqual(len(self.calls), n)              # cached


if __name__ == "__main__":
    unittest.main()
