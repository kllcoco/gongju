"""WP1 approvals tests (§21.2 approvals row): requests, params binding, single use, expiry, deciders, review size,
file fingerprints copied/returned, FileHolds, grants keyed by token name + sha256."""
from __future__ import annotations

import dataclasses
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hive.approvals import ApprovalStore, FileHolds, file_fingerprint
from hive.auth import Identity
from hive.errors import HiveError
from tests.wp1util import FILES, FakeHome, build_policy

AGENT = Identity(name="claude", kind="agent", classes=frozenset({"read", "write", "exec"}), token_sha256="sha256:aa",
                 require_approval=("shell.run",))
OWNER = Identity(name="owner", kind="owner", classes=frozenset(), source_ip="127.0.0.1")
REVIEW = {"action": "shell.run", "lines": ["command: ls"], "changes_what_runs": [], "files": []}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="hiveappr-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.store = ApprovalStore(self.d / "approvals.json", ttl=600, max_pending=3, where="node:desk")

    def ask(self, params=None, review=None, who=AGENT, node="desk", action="shell.run", aid=None):
        with self.assertRaises(HiveError) as cm:
            self.store.require(who, node, action, params or {"command": "ls"}, review or REVIEW, aid)
        return cm.exception


class TestRequests(Base):
    def test_create_idempotent_and_shape(self):
        e = self.ask()
        self.assertEqual(e.code, "approval_required")
        aid = e.details["approval_id"]
        self.assertRegex(aid, r"^ap_[A-Za-z0-9_-]{6,40}$")
        self.assertEqual(e.details["where"], "node:desk")
        self.assertIn(f"hive approvals approve {aid} (run on node:desk as owner", e.details["how"])
        self.assertEqual(self.ask().details["approval_id"], aid)              # same call -> same pending record
        rec = self.store.list()[0]
        self.assertEqual((rec["requester"], rec["requester_key"], rec["status"], rec["files"]),
                         ("claude", "sha256:aa", "pending", {}))
        self.assertEqual(rec["review"], REVIEW)
        if os.name != "nt":
            self.assertEqual(os.stat(self.d / "approvals.json").st_mode & 0o777, 0o600)

    def test_consume_single_use_and_params_binding(self):
        aid = self.ask().details["approval_id"]
        self.assertEqual(self.ask(aid=aid).code, "approval_required")         # still pending
        self.store.decide(aid, True, OWNER)
        self.assertEqual(self.ask(params={"command": "rm -rf x"}, aid=aid).code, "forbidden")
        self.assertEqual(self.ask(action="job.start", aid=aid).code, "forbidden")
        self.assertEqual(self.ask(node="other", aid=aid).code, "forbidden")
        self.assertEqual(self.store.require(AGENT, "desk", "shell.run", {"command": "ls"}, REVIEW, aid), {})
        self.assertEqual(self.ask(aid=aid).code, "forbidden")                  # used
        self.assertEqual(self.ask(aid="bad").code, "bad_request")

    def test_reissued_token_does_not_match(self):
        aid = self.ask().details["approval_id"]
        self.store.decide(aid, True, OWNER, grant_minutes=30)
        readded = dataclasses.replace(AGENT, token_sha256="sha256:bb")
        self.assertEqual(self.ask(who=readded, aid=aid).code, "forbidden")
        self.assertIsNone(self.store.granted(readded, "desk", "shell.run"))
        self.assertIsNotNone(self.store.granted(AGENT, "desk", "shell.run"))

    def test_expiry_and_denied(self):
        aid = self.ask().details["approval_id"]
        with mock.patch("time.time", return_value=time.time() + 601):
            self.assertEqual(self.store.list(include_done=True)[0]["status"], "expired")
            self.assertEqual(self.ask(aid=aid).code, "forbidden")
        aid2 = self.ask(params={"command": "x"}).details["approval_id"]
        self.store.decide(aid2, False, OWNER, note="no")
        self.assertEqual(self.ask(params={"command": "x"}, aid=aid2).code, "forbidden")
        with self.assertRaises(HiveError) as cm:
            self.store.decide(aid2, True, OWNER)
        self.assertEqual(cm.exception.code, "conflict")

    def test_pending_cap_and_review_size(self):
        for i in range(3):
            self.ask(params={"command": str(i)})
        e = self.ask(params={"command": "4"})
        self.assertEqual((e.code, e.details["reason"]), ("rate_limited", "approvals_pending"))
        big = dict(REVIEW, lines=["x" * 1024] * 300)
        st = ApprovalStore(self.d / "b.json", ttl=600, max_pending=50, where="node:desk")
        with self.assertRaises(HiveError) as cm:
            st.require(AGENT, "desk", "shell.run", {"command": "y"}, big, None)
        self.assertEqual((cm.exception.code, cm.exception.hint), ("too_large", "split the input or ask the owner"))
        shown = dict(REVIEW, lines=["x" * 1024] * 300,
                     files=[{"path": "/a", "bind": "sha256", "sha256": "ab", "size": 300 * 1024, "shown": True}])
        with self.assertRaises(HiveError) as cm:
            st.require(AGENT, "desk", "shell.run", {"command": "z"}, shown, None)
        self.assertEqual(cm.exception.code, "approval_required")              # shown file contents count apart

    def test_files_copied_and_returned(self):
        review = dict(REVIEW, files=[
            {"path": "/w/run.sh", "bind": "sha256", "sha256": "sha256:" + "a" * 64, "size": 10, "shown": True},
            {"path": "/w/model.gguf", "bind": "stat", "sha256": None, "size": 9, "mtime": "2026-01-01T00:00:00.000Z",
             "shown": False},
            {"path": "/w/setup.sh", "bind": "absent", "sha256": None, "size": None, "shown": False},
            {"path": "/w/ninth.sh", "bind": "none", "sha256": None, "size": None, "shown": False}])
        aid = self.ask(review=review).details["approval_id"]
        self.store.decide(aid, True, OWNER)
        files = self.store.require(AGENT, "desk", "shell.run", {"command": "ls"}, review, aid)
        self.assertEqual(files, {"/w/run.sh": "sha256:" + "a" * 64,
                                 "/w/model.gguf": "stat:9:2026-01-01T00:00:00.000Z", "/w/setup.sh": None})


class TestDeciders(Base):
    def test_may_decide(self):
        s = self.store
        self.assertTrue(s.may_decide(OWNER))
        self.assertFalse(s.may_decide(dataclasses.replace(OWNER, kind="agent")))
        self.assertFalse(s.may_decide(dataclasses.replace(OWNER, source_ip="192.168.1.2")))
        self.assertFalse(s.may_decide(dataclasses.replace(OWNER, source_ip="proxy:127.0.0.1")))
        self.assertFalse(s.may_decide(dataclasses.replace(OWNER, source_ip="")))
        priv = ApprovalStore(self.d / "p.json", ttl=600, max_pending=5, where="hub:h", decide_from=["private"])
        self.assertTrue(priv.may_decide(dataclasses.replace(OWNER, source_ip="100.101.1.2")))
        self.assertFalse(priv.may_decide(dataclasses.replace(OWNER, source_ip="8.8.8.8")))
        anyw = ApprovalStore(self.d / "a.json", ttl=600, max_pending=5, where="hub:h", decide_from=["any"])
        self.assertTrue(anyw.may_decide(dataclasses.replace(OWNER, source_ip="proxy:1.2.3.4")))
        aid = self.ask().details["approval_id"]
        with self.assertRaises(HiveError) as cm:
            s.decide(aid, True, dataclasses.replace(OWNER, source_ip="10.0.0.5"))
        self.assertEqual(cm.exception.code, "forbidden")


class TestGrants(Base):
    def test_grants(self):
        aid = self.ask().details["approval_id"]
        with self.assertRaises(HiveError):
            self.store.decide(aid, True, OWNER, grant_minutes=241)
        rec = self.store.decide(aid, True, OWNER, grant_minutes=30)
        gid = rec["grant"]["id"]
        self.assertRegex(gid, r"^gr_[A-Za-z0-9_-]{6,40}$")
        self.assertEqual(self.store.granted(AGENT, "desk", "shell.run"), gid)
        self.assertIsNone(self.store.granted(AGENT, "desk", "job.start"))
        self.assertIsNone(self.store.granted(AGENT, "other", "shell.run"))
        self.assertIsNone(self.store.granted(dataclasses.replace(AGENT, name="x"), "desk", "shell.run"))
        with mock.patch("time.time", return_value=time.time() + 31 * 60):
            self.assertIsNone(self.store.granted(AGENT, "desk", "shell.run"))
        self.assertEqual(len(self.store.grants()), 1)
        reloaded = ApprovalStore(self.d / "approvals.json", ttl=600, max_pending=3, where="node:desk")
        self.assertEqual(reloaded.granted(AGENT, "desk", "shell.run"), gid)
        self.assertEqual(self.store.revoke(), 1)
        self.assertIsNone(self.store.granted(AGENT, "desk", "shell.run"))
        self.assertEqual(self.store.revoke("gr_nonexistent"), 0)


class TestFileHolds(unittest.TestCase):
    def test_acquire_conflicts(self):
        with FakeHome() as fh:
            f = fh.touch("run.sh", "echo 1")
            big = fh.touch("big.bin", "b")
            pol = build_policy(fh)
            e = pol.effective(FILES, paused=False)
            holds = FileHolds()
            fp = file_fingerprint(str(f))
            self.assertTrue(fp.startswith("sha256:"))
            hid = holds.acquire({str(f): fp, str(fh.p("absent.sh")): None}, policy=e)
            self.assertTrue(hid.startswith("ho_"))
            self.assertTrue(holds.held(e._key(f)))
            self.assertTrue(holds.held(e._key(fh.p("absent.sh"))))
            holds.release(hid)
            self.assertFalse(holds.held(e._key(f)))
            for files in ({str(f): "sha256:" + "0" * 64},          # changed
                          {str(fh.p("gone.sh")): fp},               # vanished
                          {str(f): None},                           # newly present
                          {"/etc/passwd": None}):                   # error (outside roots) counts as changed
                with self.subTest(files=files):
                    with self.assertRaises(HiveError) as cm:
                        holds.acquire(files, policy=e)
                    self.assertEqual(cm.exception.code, "conflict")
                    self.assertEqual(cm.exception.message,
                                     "a file used by the approved command changed; request approval again")
                    self.assertTrue(cm.exception.details["changed"])
            self.assertFalse(holds._count)
            with mock.patch("hive.approvals.HASH_MAX", 0):
                st = file_fingerprint(str(big))
            self.assertTrue(st.startswith("stat:1:"))
            holds.release(holds.acquire({str(big): st}, policy=e))


if __name__ == "__main__":
    unittest.main()
