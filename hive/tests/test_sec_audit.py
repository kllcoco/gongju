"""v8.1 external security audit regressions (PHASE0_NOTES "External review fixes"): approval review cwd (finding 1),
service/job approved-file fingerprints (finding 2), cd in compound commands (finding 3), job visibility (finding 5).
Exec paths (finding 4) are in test_policy, the Host header (finding 6) in test_httpserver."""
from __future__ import annotations

import os
import unittest

from hive import presets, proctree
from tests.wp2util import CAUTIOUS, Node

HAS_BASH = proctree.shell_path("bash") is not None
POSIX = os.name != "nt"


class TestBindableWords(unittest.TestCase):
    def test_cd_and_dash_c_unbind_later_relative_words(self):
        self.assertEqual(presets.bindable_words("cd sub && bash setup.sh"), (["cd"], ["sub", "bash", "setup.sh"]))
        self.assertEqual(presets.bindable_words("bash a.sh; cd sub; bash a.sh ~/b.sh /abs/c.sh"),
                         (["bash", "a.sh", "cd", "~/b.sh", "/abs/c.sh"], ["sub", "bash", "a.sh"]))
        for cmd in ("pushd sub && bash x.sh", "chdir sub && bash x.sh", "git -C sub run x.sh",
                    "make -Csub x.sh", "make --directory=sub x.sh", "env --chdir=sub bash x.sh",
                    "terraform -chdir=sub apply x.sh"):
            with self.subTest(cmd=cmd):
                self.assertIn("x.sh", presets.bindable_words(cmd)[1])
                self.assertNotIn("x.sh", presets.bindable_words(cmd)[0])
        for cmd in ("Set-Location sub; .\\x.ps1", "sl sub; .\\x.ps1", "cd sub & x.ps1", "CD sub && x.ps1"):
            with self.subTest(cmd=cmd):
                w, m = presets.bindable_words(cmd, shell="powershell")
                self.assertTrue(any(x.endswith("x.ps1") for x in m), (w, m))
                self.assertFalse(any(x.endswith("x.ps1") for x in w), (w, m))
        self.assertEqual(presets.bindable_words(["git", "-C", "sub", "x.sh"]), (["git"], ["sub", "x.sh"]))
        self.assertEqual(presets.bindable_words("bash run.sh"), (["bash", "run.sh"], []))
        self.assertEqual(presets.bindable_words("bash -c 'echo hi'"), presets.bindable_words("bash -c 'echo hi'"))
        self.assertEqual(presets.candidate_words("cd sub && bash setup.sh"), ["cd", "sub", "bash", "setup.sh"])

    def test_moved_items(self):
        self.assertEqual(presets.moved_items(["sub", "bash", "setup.sh", "setup"], lambda w: w == "setup"),
                         ["setup.sh (cwd may change)", "setup (cwd may change)"])


@unittest.skipUnless(HAS_BASH and POSIX, "needs bash")
class TestReviewCwd(unittest.TestCase):
    """Audit finding 1: a cwd that does not exist yet was replaced by the workdir in the review."""

    def review(self, n, params):
        e = n.err("shell.run", params, identity=CAUTIOUS)
        self.assertEqual(e["code"], "approval_required", e)
        aid = e["details"]["approval_id"]
        return aid, n.rt.approvals.get(aid)["review"]

    def test_missing_cwd_not_replaced_by_workdir(self):
        with Node() as n:
            (n.home / "setup.sh").write_text("echo benign\n")
            params = {"command": "bash setup.sh", "cwd": "~/build-x"}
            aid, rv = self.review(n, params)
            build = os.path.realpath(str(n.home / "build-x"))
            self.assertIn(f"cwd: ~/build-x (= {build}) (does not exist here yet)", rv["lines"])
            self.assertEqual(rv["cwd"], build)
            paths = {f["path"]: f["bind"] for f in rv["files"]}
            self.assertEqual(paths, {os.path.join(build, "setup.sh"): "absent"})
            self.assertNotIn("echo benign", "\n".join(rv["lines"]))
            self.assertIn("setup.sh (missing here)", rv["lines"][0])
            n.approve(aid)
            (n.home / "build-x").mkdir()
            (n.home / "build-x" / "setup.sh").write_text("echo EVIL > evil.txt\n")
            e = n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)
            self.assertEqual(e["code"], "conflict")
            self.assertFalse((n.home / "build-x" / "evil.txt").exists())

    def test_cwd_resolving_elsewhere_at_consume_conflicts(self):
        with Node() as n:
            (n.home / "other").mkdir()
            params = {"command": "echo hi > out.txt", "cwd": "~/build-y"}
            aid, rv = self.review(n, params)
            n.approve(aid)
            os.symlink(str(n.home / "other"), str(n.home / "build-y"))
            e = n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)
            self.assertEqual(e["code"], "conflict")
            self.assertIn("working folder", e["message"])
            self.assertFalse((n.home / "other" / "out.txt").exists())

    def test_same_cwd_runs(self):
        with Node() as n:
            (n.home / "w").mkdir()
            (n.home / "w" / "run.sh").write_text("echo ran-in-w\n")
            env = n.run_approved("shell.run", {"command": "bash run.sh", "cwd": "~/w"})
            self.assertTrue(env["ok"], env)
            self.assertIn("ran-in-w", str(env["result"]))

    def test_cd_decoy_not_bound(self):
        # audit finding 3: "cd sub && bash setup.sh" bound and showed ~/setup.sh (a decoy) instead of sub/setup.sh
        with Node() as n:
            (n.home / "setup.sh").write_text("echo benign\n")
            (n.home / "sub").mkdir()
            aid, rv = self.review(n, {"command": "cd sub && bash setup.sh"})
            self.assertEqual(rv["files"], [])
            self.assertNotIn("echo benign", "\n".join(rv["lines"]))
            self.assertIn("not bound to their content (may change after approval): setup.sh (cwd may change)",
                          rv["lines"][0])


if __name__ == "__main__":
    unittest.main()
