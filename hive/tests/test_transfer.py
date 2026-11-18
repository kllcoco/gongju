"""WP5b transfer engine tests (§15.3, D9; §21.2 relay/hub/client/transfer row: transfer parts)."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from hive import transfer
from hive.client import HttpTarget, connect
from hive.errors import HiveError
from hive.transfer import LocalFS, NodeFS, TransferManager, copy
from tests import helpers


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivetx-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.d), True)

    def file(self, name: str, data: bytes) -> Path:
        p = self.d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p


class TestLocalCopy(_Tmp):
    def test_copy_and_overwrite(self):
        data = os.urandom(10_000)
        src = self.file("a.bin", data)
        fs = LocalFS(None)
        r = copy(fs, str(src), fs, str(self.d / "out" / "b.bin"), chunk=3000)
        self.assertEqual((r["bytes"], r["sha256"], r["resumed_from"]), (10_000, sha(data), 0))
        self.assertEqual((self.d / "out" / "b.bin").read_bytes(), data)
        self.assertFalse((self.d / "out" / "b.bin.hive-part").exists())
        with self.assertRaises(HiveError) as cm:
            copy(fs, str(src), fs, str(self.d / "out" / "b.bin"))
        self.assertEqual(cm.exception.code, "conflict")
        copy(fs, str(src), fs, str(self.d / "out" / "b.bin"), overwrite=True)

    def test_empty_file(self):
        src = self.file("e", b"")
        fs = LocalFS(None)
        r = copy(fs, str(src), fs, str(self.d / "e2"))
        self.assertEqual((r["bytes"], r["sha256"]), (0, sha(b"")))
        self.assertEqual((self.d / "e2").read_bytes(), b"")

    def test_resume_from_part(self):
        data = os.urandom(9000)
        src = self.file("a.bin", data)
        dst = self.d / "b.bin"
        fs = LocalFS(None)
        # a copy killed after chunk 1 of 3: cancel from the progress callback
        ev = threading.Event()

        def prog(done, total):
            ev.set()
        with self.assertRaises(HiveError) as cm:
            copy(fs, str(src), fs, str(dst), chunk=3000, progress=prog, cancel=ev)
        self.assertEqual(cm.exception.code, "cancelled")
        part = Path(str(dst) + ".hive-part")
        self.assertEqual(part.stat().st_size, 3000)
        r = copy(fs, str(src), fs, str(dst), chunk=3000)
        self.assertEqual((r["resumed_from"], r["sha256"]), (3000, sha(data)))
        self.assertEqual(dst.read_bytes(), data)

    def test_resume_with_changed_source_conflicts(self):
        data = os.urandom(6000)
        src = self.file("a.bin", data)
        dst = self.d / "b.bin"
        Path(str(dst) + ".hive-part").write_bytes(b"x" * 3000)     # a part that does not match the source
        with self.assertRaises(HiveError) as cm:
            copy(LocalFS(None), str(src), LocalFS(None), str(dst), chunk=3000)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("source changed", cm.exception.hint or cm.exception.message)
        self.assertFalse(dst.exists())
        self.assertFalse(Path(str(dst) + ".hive-part").exists())     # deleted by the final check

    def test_larger_part_restarts(self):
        data = b"abc" * 100
        src = self.file("a", data)
        dst = self.d / "b"
        Path(str(dst) + ".hive-part").write_bytes(b"z" * 1000)
        r = copy(LocalFS(None), str(src), LocalFS(None), str(dst), chunk=64)
        self.assertEqual((r["resumed_from"], dst.read_bytes()), (0, data))

    def test_unexpected_chunk_length(self):
        class Shrinking(LocalFS):
            def read_chunk(self, path, offset, length):
                data, eof = super().read_chunk(path, offset, length)
                return data[:-1], eof
        src = self.file("a", b"x" * 100)
        with self.assertRaises(HiveError) as cm:
            copy(Shrinking(None), str(src), LocalFS(None), str(self.d / "b"), chunk=40)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertFalse((self.d / "b").exists())

    def test_local_roots(self):
        src = self.file("in/a", b"hi")
        fs = LocalFS([str(self.d / "in")])
        self.assertTrue(fs.stat(str(src))["exists"])
        self.assertTrue(fs.stat("a")["exists"])                    # relative to the first root
        with self.assertRaises(HiveError) as cm:
            fs.stat(str(self.d / "other"))
        self.assertEqual(cm.exception.code, "policy_denied")
        with self.assertRaises(HiveError):
            LocalFS([]).stat(str(src))

    def test_source_missing_or_dir(self):
        with self.assertRaises(HiveError) as cm:
            copy(LocalFS(None), str(self.d / "nope"), LocalFS(None), str(self.d / "x"))
        self.assertEqual(cm.exception.code, "not_found")
        with self.assertRaises(HiveError) as cm:
            copy(LocalFS(None), str(self.d), LocalFS(None), str(self.d / "x"))
        self.assertEqual(cm.exception.code, "bad_request")


class _FakeTarget:
    def __init__(self, fs: LocalFS, masked_at=None):
        self.fs = fs
        self.calls = []
        self.masked_at = masked_at

    def call(self, action, params, *, node=None, timeout=None, request_id=None, approval_id=None):
        import base64
        self.calls.append((action, dict(params), node, timeout))
        if action == "fs.stat":
            return self.fs.stat(params["path"], sha256=bool(params.get("sha256")))
        if action == "fs.read_chunk":
            data, eof = self.fs.read_chunk(params["path"], params["offset"], params["length"])
            r = {"data": base64.b64encode(data).decode(), "eof": eof, "offset": params["offset"],
                 "length": len(data)}
            if self.masked_at is not None and params["offset"] >= self.masked_at:
                r["masked"] = True
            return r
        if action == "fs.write_chunk":
            return self.fs.write_chunk(params["path"], params["offset"], base64.b64decode(params["data"]),
                                       final=params.get("final", False), sha256=params.get("sha256"),
                                       total_size=params.get("total_size"), overwrite=params.get("overwrite", False))
        raise AssertionError(action)


class TestNodeFS(_Tmp):
    def test_masked_chunk_refused_before_write(self):
        data = os.urandom(5000)
        src = self.file("s", data)
        t = _FakeTarget(LocalFS(None), masked_at=2000)
        with self.assertRaises(HiveError) as cm:
            copy(NodeFS(t, "desk"), str(src), LocalFS(None), str(self.d / "d"), chunk=2000)
        self.assertEqual(cm.exception.code, "conflict")
        self.assertIn("secret value of node desk", cm.exception.message)
        self.assertFalse((self.d / "d").exists())

    def test_resume_gets_source_sha_upfront_with_timeout(self):
        data = os.urandom(5000)
        src = self.file("s", data)
        dst = self.d / "d"
        Path(str(dst) + ".hive-part").write_bytes(data[:2000])
        t = _FakeTarget(LocalFS(None))
        r = copy(NodeFS(t, "desk"), str(src), LocalFS(None), str(dst), chunk=2000)
        self.assertEqual((r["resumed_from"], r["sha256"]), (2000, sha(data)))
        shacall = [c for c in t.calls if c[0] == "fs.stat" and c[1].get("sha256")][0]
        self.assertGreaterEqual(shacall[3], 120)
        self.assertEqual(shacall[2], "desk")

    def test_transfer_manager(self):
        data = os.urandom(3000)
        src = self.file("s", data)

        class C:
            target = _FakeTarget(LocalFS(None))
        tm = TransferManager(C(), local_roots=[str(self.d)])
        tid = tm.start(f"local:{src}", f"desk:{self.d / 'out'}")
        st = tm.status(tid, wait=10)
        self.assertEqual((st["state"], st["bytes_done"], st["bytes_total"], st["sha256"]),
                         ("done", 3000, 3000, sha(data)))
        self.assertEqual((self.d / "out").read_bytes(), data)
        self.assertFalse(tm.cancel(tid))
        with self.assertRaises(HiveError):
            tm.status("tx_nope")
        tid = tm.start(f"local:{src}", f"desk:{self.d / 'out'}")       # exists -> failed
        st = tm.status(tid, wait=10)
        self.assertEqual(st["state"], "failed")
        self.assertIn("overwrite", st["error"])
        with self.assertRaises(HiveError):
            TransferManager(C(), local_roots=()).start(f"local:{src}", "desk:/x")

    def test_transfer_manager_cancel_and_cap(self):
        gate = threading.Event()

        class Slow(_FakeTarget):
            def call(self, action, params, **kw):
                if action == "fs.stat":
                    gate.wait(5)
                return super().call(action, params, **kw)
        src = self.file("s", b"x" * 100)

        class C:
            target = Slow(LocalFS(None))
        tm = TransferManager(C(), local_roots=[str(self.d)])
        ids = [tm.start(f"local:{src}", f"desk:{self.d / f'o{i}'}") for i in range(4)]
        with self.assertRaises(HiveError) as cm:
            tm.start(f"local:{src}", f"desk:{self.d / 'o9'}")
        self.assertEqual(cm.exception.code, "rate_limited")
        self.assertTrue(tm.cancel(ids[0]))
        gate.set()
        st = tm.status(ids[0], wait=10)
        self.assertEqual(st["state"], "cancelled")


class TestRealNode(_Tmp):
    def test_local_node_roundtrip_and_secret_refusal(self):
        url, agent, rt = helpers.start_node_server(start_managers=True)
        self.addCleanup(rt.test_stop)
        c = connect(url, token=agent)
        self.addCleanup(c.close)
        wd = Path(rt.workdir) / f"hivetx-{os.getpid()}"
        self.addCleanup(shutil.rmtree, str(wd), True)
        data = os.urandom(70_000)
        src = self.file("a.bin", data)
        r = c.transfer(f"local:{src}", f"self:{wd / 'a.bin'}")
        self.assertEqual(r["sha256"], sha(data))
        back = self.d / "back.bin"
        r = c.node().download(str(wd / "a.bin"), str(back))
        self.assertEqual(back.read_bytes(), data)
        # a file containing a node secret value cannot be copied (only `result` reaches NodeFS)
        helpers.set_secret(rt, "MY_TOKEN", "hv_secretvalue_1234567890", hosts=["loopback"])
        (wd / "env").write_text("TOKEN=hv_secretvalue_1234567890\n")
        with self.assertRaises(HiveError) as cm:
            c.transfer(f"self:{wd / 'env'}", f"local:{self.d / 'env'}")
        self.assertEqual(cm.exception.code, "conflict")
        self.assertFalse((self.d / "env").exists())


if __name__ == "__main__":
    unittest.main()
