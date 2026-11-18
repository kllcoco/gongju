"""v8.1 external security audit regressions (PHASE0_NOTES "External review fixes"): approval review cwd (finding 1),
service/job approved-file fingerprints (finding 2), cd in compound commands (finding 3), job visibility (finding 5).
Exec paths (finding 4) are in test_policy, the Host header (finding 6) in test_httpserver."""
from __future__ import annotations

import os
import time
import unittest
from unittest import mock

from hive import presets, proctree, services
from hive.errors import HiveError
from tests.wp2util import CAUTIOUS, OPERATOR, OWNER, PY, Node, ident, wait_for

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


class TestJobVisibility(unittest.TestCase):
    """Audit finding 5: job output and control were open to every token."""

    def test_gated_and_readonly_see_only_their_own_jobs(self):
        ro = ident(("read",), name="ro")
        with Node() as n:
            own = n.ok("job.start", {"argv": [PY, "-c", "print('OWNER-PRIVATE-OUTPUT')"]}, identity=OWNER)["job_id"]
            env = n.run_approved("job.start", {"argv": [PY, "-c",
                                                        "import time; print('CAUTIOUS-OUT', flush=True); time.sleep(30)"]})
            self.assertTrue(env["ok"], env)
            mine = env["result"]["job_id"]
            self.assertTrue(n.rt.jobs.wait_final(own, 15))
            for who in (ro, CAUTIOUS):
                with self.subTest(who=who.name):
                    ids = [j["job_id"] for j in n.ok("job.list", {}, identity=who)["jobs"]]
                    self.assertNotIn(own, ids)
                    for action in ("job.logs", "job.status"):
                        e = n.err(action, {"job_id": own}, identity=who)
                        self.assertEqual(e["code"], "not_found")
                        self.assertNotIn(own, e.get("hint") or "")
            self.assertEqual(n.err("job.stop", {"job_id": own}, identity=CAUTIOUS)["code"], "not_found")
            # the cautious agent's own job: visible to it, not to the readonly token
            self.assertIn(mine, [j["job_id"] for j in n.ok("job.list", {}, identity=CAUTIOUS)["jobs"]])
            wait_for(lambda: "CAUTIOUS-OUT" in str(n.ok("job.logs", {"job_id": mine}, identity=CAUTIOUS)), 10)
            self.assertNotIn(mine, [j["job_id"] for j in n.ok("job.list", {}, identity=ro)["jobs"]])
            self.assertEqual(n.err("job.logs", {"job_id": mine}, identity=ro)["code"], "not_found")
            # owner and an ungated operator keep seeing (and stopping) every job
            for who in (OWNER, OPERATOR):
                ids = [j["job_id"] for j in n.ok("job.list", {}, identity=who)["jobs"]]
                self.assertTrue({own, mine} <= set(ids), (who.name, ids))
            self.assertIn("OWNER-PRIVATE-OUTPUT", str(n.ok("job.logs", {"job_id": own}, identity=OPERATOR)))
            n.ok("job.stop", {"job_id": mine, "force": True}, identity=OPERATOR)

    def test_sees_all_jobs_rule(self):
        from hive.jobs import sees_all_jobs
        self.assertTrue(sees_all_jobs(OWNER))
        self.assertTrue(sees_all_jobs(OPERATOR))
        self.assertFalse(sees_all_jobs(CAUTIOUS))
        self.assertFalse(sees_all_jobs(ident(("read",), name="ro")))
        self.assertFalse(sees_all_jobs(ident(("read", "write"), name="files")))
        self.assertFalse(sees_all_jobs(ident(name="hub", kind="hub", via="hub", exec_gated=True)))
        self.assertTrue(sees_all_jobs(ident(name="hub", kind="hub", via="hub")))


class TestApprovedFilesSurviveRestarts(unittest.TestCase):
    """Audit finding 2: a crash restart / re-hold ran (or accepted) a bound file the owner never approved."""

    def setUp(self):
        for attr, val in (("TICK", 0.2), ("NO_HEALTH_ALIVE_S", 0.5)):
            p = mock.patch.object(services, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.n = Node()
        self.n.__enter__()
        self.addCleanup(self.n.__exit__, None, None, None)
        (self.n.home / "lib").mkdir()
        (self.n.home / "svc").mkdir()
        self.x = self.n.home / "lib" / "x.py"

    def rs(self, name):
        return self.n.rt.services.runtime(name)

    def test_crash_restart_refuses_changed_script(self):
        self.x.write_text("import time\nprint('benign v1', flush=True)\ntime.sleep(1.5)\nraise SystemExit(1)\n")
        p = {"name": "x", "argv": [PY, str(self.x)], "cwd": "~/svc", "restart": "on-failure", "health": "none",
             "restart_delay": 1}
        env = self.n.run_approved("service.define", p)
        self.assertTrue(env["ok"], env)
        self.assertEqual(list(self.rs("x")["approved_files"]), [os.path.realpath(str(self.x))])
        # the gated agent cannot rewrite it, neither while it runs nor between runs (service code while defined)
        e = self.n.err("fs.write", {"path": str(self.x), "content": "print('EVIL')"}, identity=CAUTIOUS)
        self.assertIn(e["code"], ("policy_denied", "conflict"))
        wait_for(lambda: self.rs("x")["state"] in ("backoff", "stopped", "failed"), 20)
        # someone else (or anything while the node was down) changes it: the respawn must not run it
        self.x.write_text("print('EVIL v2 - never approved', flush=True)\n")
        wait_for(lambda: self.rs("x")["state"] == "failed", 20)
        self.assertEqual(self.rs("x")["last_error"], services.APPROVED_CHANGED_ERROR)
        time.sleep(0.5)
        log = self.n.ok("service.logs", {"name": "x"})
        self.assertIn("benign v1", str(log))
        self.assertNotIn("EVIL", str(log))
        self.assertEqual(self.n.rt.holds.held_keys(), [])
        # a new approved start binds the new content and runs it
        env = self.n.run_approved("service.start", {"name": "x"})
        self.assertTrue(env["ok"], env)
        wait_for(lambda: "EVIL v2" in str(self.n.ok("service.logs", {"name": "x"})), 20)

    def test_unchanged_script_restarts(self):
        self.x.write_text("import time\nprint('run', flush=True)\ntime.sleep(0.8)\nraise SystemExit(1)\n")
        p = {"name": "y", "argv": [PY, str(self.x)], "restart": "on-failure", "health": "none", "restart_delay": 1}
        self.assertTrue(self.n.run_approved("service.define", p)["ok"])
        wait_for(lambda: len(self.rs("y")["restarts"]) >= 1, 20)
        self.assertNotEqual(self.rs("y")["last_error"], services.APPROVED_CHANGED_ERROR)

    def test_rehold_after_node_restart_checks_stored_fingerprints(self):
        self.x.write_text("import time\nprint('MARK-STARTED', flush=True)\ntime.sleep(5)\nraise SystemExit(1)\n")
        p = {"name": "z", "argv": [PY, str(self.x)], "restart": "on-failure", "health": "none", "restart_delay": 1}
        self.assertTrue(self.n.run_approved("service.define", p)["ok"])
        wait_for(lambda: self.rs("z")["held_paths"], 10)
        wait_for(lambda: "MARK-STARTED" in str(self.n.ok("service.logs", {"name": "z"})), 10)   # python has read x.py
        self.n.stop_runtime()
        self.x.write_text("print('EVIL while the node was down')\n")
        self.n.rt = self.n.make_runtime()
        rs = self.rs("z")
        self.assertTrue(rs["adopted"])
        self.assertTrue(rs["last_error"].startswith(services.APPROVED_CHANGED_ERROR), rs["last_error"])
        self.assertEqual(rs["held_paths"], [])
        # it exits with 1 (on-failure): the supervisor must not run the changed file
        wait_for(lambda: self.rs("z")["state"] == "failed", 30)
        self.assertEqual(self.rs("z")["last_error"], services.APPROVED_CHANGED_ERROR)
        self.assertNotIn("EVIL", str(self.n.ok("service.logs", {"name": "z"})))

    def test_autostart_checks_stored_fingerprints(self):
        self.x.write_text("import time\ntime.sleep(60)\n")
        p = {"name": "a", "argv": [PY, str(self.x)], "health": "none", "autostart": True}
        self.assertTrue(self.n.run_approved("service.define", p)["ok"])
        self.n.ok("service.stop", {"name": "a", "wait": 20})
        self.n.stop_runtime()
        self.x.write_text("print('EVIL at boot')\n")
        with mock.patch.object(services.util, "same_boot", return_value=False):
            self.n.rt = self.n.make_runtime()
            wait_for(lambda: self.rs("a")["state"] == "failed", 20)
        self.assertEqual(self.rs("a")["last_error"], services.APPROVED_CHANGED_ERROR)
        self.assertNotIn("EVIL", str(self.n.ok("service.logs", {"name": "a"})))

    def test_owner_start_reapproves_current_content(self):
        self.x.write_text("import time\ntime.sleep(60)\n")
        self.assertTrue(self.n.run_approved("service.define", {"name": "o", "argv": [PY, str(self.x)],
                                                               "health": "none"})["ok"])
        self.n.ok("service.stop", {"name": "o", "wait": 20})
        self.x.write_text("import time\ntime.sleep(61)\n")       # the owner edits their service
        self.n.ok("service.start", {"name": "o"}, identity=OWNER)
        from hive.approvals import file_fingerprint
        real = os.path.realpath(str(self.x))
        self.assertEqual(self.rs("o")["approved_files"], {real: file_fingerprint(real)})

    def test_job_rehold_after_node_restart_checks_stored_fingerprints(self):
        self.x.write_text("import time\nprint('MARK-STARTED', flush=True)\ntime.sleep(60)\n")
        env = self.n.run_approved("job.start", {"argv": [PY, str(self.x)]})
        self.assertTrue(env["ok"], env)
        jid = env["result"]["job_id"]
        wait_for(lambda: "MARK-STARTED" in str(self.n.ok("job.logs", {"job_id": jid})), 10)    # python has read x.py
        rec = self.n.rt.jobs.record(jid)
        self.assertEqual(list(rec["approved_files"]), [os.path.realpath(str(self.x))])
        self.n.stop_runtime()
        self.x.write_text("print('changed')\n")
        self.n.rt = self.n.make_runtime()
        rec = self.n.rt.jobs.record(jid)
        self.assertTrue(rec["adopted"])
        self.assertIn("approved file changed while the node was down", rec["error"])
        self.assertEqual(rec["held_paths"], [])
        self.n.ok("fs.write", {"path": str(self.x), "content": "x = 1\n"}, identity=CAUTIOUS)   # not held
        self.n.ok("job.stop", {"job_id": jid, "force": True})


if __name__ == "__main__":
    unittest.main()
