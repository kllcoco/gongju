"""Audit round 4 (DoS / local users), regex half: linear-time redaction and secret masking on megabyte adversarial
inputs, bounded proc.list / job command redaction (findings 1, 2). Bounds are generous: the old code took minutes."""
from __future__ import annotations

import shutil
import tempfile
import time
import unittest
from pathlib import Path

from hive import audit as audit_mod
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


if __name__ == "__main__":
    unittest.main()
