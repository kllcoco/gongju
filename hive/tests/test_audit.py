"""WP1 audit tests (§21.2 audit row): redaction golden test, content hashing, files per UTC day, required fails
closed, tail/prune, secret-value mask."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import audit as audit_mod
from hive.audit import AuditLog, redact_env, redact_params, redact_text
from hive.auth import Identity
from hive.errors import HiveError


class TestRedaction(unittest.TestCase):
    def test_redact_text_golden(self):
        cases = {
            "curl --api-key sk_live_123 https://x": "curl --api-key *** https://x",
            "tool --token=abc123 run": "tool --token=*** run",
            "mysql -u root -p hunter2 db": "mysql -u root -p *** db",
            "pip install --proxy http://user:pass@10.0.0.1:7890 x": "pip install --proxy http://***@10.0.0.1:7890 x",
            "curl -H 'Authorization: Bearer abc.def' https://x": "curl -H 'Authorization: ***' https://x",
            "export AWS_SECRET_ACCESS_KEY=AbCd/123": "export AWS_SECRET_ACCESS_KEY=***",
            "HF_TOKEN=hf_abc python x.py": "HF_TOKEN=*** python x.py",
            "token hv_" + "a" * 43: "token hv_***",
            "key hf_" + "A" * 30: "key ***",
            "k sk-ant-" + "a" * 30: "k ***",
            "g ghp_" + "b" * 36: "g ***",
            "g github_pat_" + "c" * 30: "g ***",
            "s xoxb-123-abc": "s ***",
            "aws AKIA" + "ABCDEFGHIJKLMNOP": "aws ***",
            "ls -la /tmp": "ls -la /tmp",
        }
        for src, want in cases.items():
            with self.subTest(src=src):
                self.assertEqual(redact_text(src), want)

    def test_secret_value_mask(self):
        audit_mod.set_text_mask(lambda s: s.replace("plainsecretvalue", "«secret:X»"))
        try:
            self.assertEqual(redact_text("echo plainsecretvalue"), "echo «secret:X»")
        finally:
            audit_mod.set_text_mask(None)

    def test_redact_params(self):
        p = redact_params("shell.run", {
            "command": "curl --token abc http://x", "env": ["A=1", "SECRET=2"], "stdin": "data" * 10,
            "headers": ["Authorization: Bearer x", "X-Y: 1"], "password": "hunter2", "secret_env": ["HF_TOKEN"],
            "max_tokens": 100, "content": {"a": 1}, "note": "n" * 600, "argv": ["tool", "--api-key", "K", "x"]})
        self.assertEqual(p["command"], "curl --token *** http://x")
        self.assertEqual(p["env"], ["A", "SECRET"])
        self.assertEqual(p["headers"], ["Authorization", "X-Y"])
        self.assertEqual(p["stdin"]["len"], 40)
        self.assertEqual(len(p["stdin"]["sha256"]), 12)
        self.assertEqual(p["password"], "***")
        self.assertEqual(p["secret_env"], ["HF_TOKEN"])
        self.assertEqual(p["max_tokens"], 100)
        self.assertIn("sha256", p["content"])
        self.assertTrue(p["note"].startswith("n" * 500) and len(p["note"]) < 600)
        self.assertEqual(p["argv"], ["tool", "--api-key", "***", "x"])
        self.assertEqual(redact_env({"API_TOKEN": "x", "PATH": "/bin"}), {"API_TOKEN": "***", "PATH": "/bin"})


class TestAuditLog(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="hiveaud-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def rec(self, log, **kw):
        base = dict(kind="call", identity=Identity(name="a", kind="agent", classes=frozenset(), agent="cd"),
                    ip="127.0.0.1", via="direct", node="n", action="shell.run", params={"command": "ls"}, ok=True,
                    code=None, ms=3, request_id="r_1")
        base.update(kw)
        log.record(**base)

    def test_record_shape_and_tail(self):
        log = AuditLog(self.d, mask=lambda s: s.replace("topsecretvalue", "«secret:T»"))
        self.rec(log, params={"command": "echo topsecretvalue"}, extra={"pid": 5})
        self.rec(log, action="fs.read", params={"path": "/x"})
        files = list(self.d.iterdir())
        self.assertEqual(len(files), 1)
        self.assertRegex(files[0].name, r"^audit-\d{8}\.jsonl$")
        raw = files[0].read_text()
        self.assertNotIn("topsecretvalue", raw)
        rec = json.loads(raw.splitlines()[0])
        self.assertEqual(set(rec), {"ts", "id", "kind", "node", "hub", "identity", "agent", "ip", "via", "action",
                                    "rid", "params", "ok", "code", "ms", "extra"})
        self.assertEqual((rec["identity"], rec["agent"], rec["extra"]), ("a", "cd", {"pid": 5}))
        self.assertTrue(rec["ts"].endswith("Z"))
        self.assertEqual([r["action"] for r in log.tail(10)], ["shell.run", "fs.read"])
        self.assertEqual([r["action"] for r in log.tail(10, action="fs.*")], ["fs.read"])
        self.assertEqual(len(log.tail(1)), 1)
        if os.name != "nt":
            self.assertEqual(os.stat(files[0]).st_mode & 0o777, 0o600)

    def test_day_rollover_and_prune(self):
        log = AuditLog(self.d, retention_days=30)
        day = 86400
        base = time.time() - 2 * day
        with mock.patch("time.time", return_value=base):
            self.rec(log)
        with mock.patch("time.time", return_value=base + day):
            self.rec(log)
        self.assertEqual(len(list(self.d.iterdir())), 2)
        (self.d / "audit-20000101.jsonl").write_text("{}\n")
        log.prune()
        self.assertFalse((self.d / "audit-20000101.jsonl").exists())
        self.assertEqual(len(list(self.d.iterdir())), 2)

    def test_required_fails_closed(self):
        target = self.d / "notadir"
        target.write_text("x")
        log = AuditLog.__new__(AuditLog)
        AuditLog.__init__(log, self.d / "ok", required=True)
        log.dir = target          # writes now fail (a file where the folder should be)
        with self.assertRaises(HiveError) as cm:
            self.rec(log)
        self.assertEqual(cm.exception.code, "internal")
        lax = AuditLog(self.d / "ok2", required=False)
        lax.dir = target
        self.rec(lax)              # logged, not raised

    def test_disabled(self):
        log = AuditLog(self.d / "off", enabled=False)
        self.rec(log)
        self.assertFalse((self.d / "off").exists())



class TestBoundedRecordsVerification4(unittest.TestCase):
    """Verification round 4 (DoS): params containers are capped in audit records (a 48 MiB body of empty lists
    wrote a 50 MB line), and one record is at most 64 KiB."""

    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="hiveaud-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def record(self, params, action="node.info", extra=None):
        log = AuditLog(self.d)
        t = time.monotonic()
        log.record(kind="call", identity=None, ip="127.0.0.1", via="direct", node="n", action=action,
                   params=params, ok=False, code="bad_request", ms=1, request_id="r", extra=extra)
        took = time.monotonic() - t
        (f,) = list(self.d.iterdir())
        raw = f.read_bytes()
        f.unlink()
        self.assertLess(len(raw), 64 << 10)
        self.assertLess(took, 0.5)
        return json.loads(raw)

    def test_huge_containers(self):
        rec = self.record({"x": [[]] * (16 << 20)})                        # 16M elements
        self.assertEqual(len(rec["params"]["x"]), 33)
        self.assertEqual(rec["params"]["x"][-1], "…[+16777184 items]")
        rec = self.record({f"k{i}": i for i in range(1 << 20)})
        self.assertEqual(rec["params"]["…"], "[+1048544 keys]")
        rec = self.record({"argv": ["a"] * (1 << 20), "env": {f"E{i}": "v" for i in range(1 << 20)},
                           "headers": ["H: v"] * (1 << 20), "messages": [{"role": "user"}] * (1 << 20),
                           "content": [[]] * (1 << 20)})
        p = rec["params"]
        self.assertEqual((len(p["argv"]), p["argv"][-1]), (129, "…[+1048448 items]"))
        self.assertEqual((len(p["env"]), p["headers"][-1]), (33, "…[+1048544 names]"))
        self.assertEqual(p["messages"], {"items": 1 << 20, "sha256": None})
        deep: list = []
        for _ in range(500):
            deep = [deep, "s" * 400]
        rec = self.record({"deep": deep, "wide": [["y" * 600] * 32] * 32})      # depth / value budget
        self.assertIn("…", json.dumps(rec["params"], ensure_ascii=False))
        rec = self.record({"k": "v"}, extra={"big": [{"a": "z" * 499} for _ in range(3000)]})
        self.assertIn("…", json.dumps(rec["extra"], ensure_ascii=False))
        rec = self.record({f"{'k' * 100}{i}": ["v" * 499] * 32 for i in range(32)})   # still > 64 KiB: cut
        self.assertTrue(rec["params"]["…"].startswith("[record cut: "))
        rec = self.record({"a": 1}, action="x" * (1 << 20))
        self.assertEqual(rec["action"], "x" * 200)

    def test_small_params_unchanged(self):
        params = {"path": "/x", "argv": ["ls", "-la"], "env": {"A": "1"}, "items": list(range(10)),
                  "content": "hello"}
        self.assertEqual(redact_params("a", params, bounded=True), redact_params("a", params))
        self.assertEqual(self.record(params)["params"], redact_params("a", params))

if __name__ == "__main__":
    unittest.main()
