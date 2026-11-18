"""WP2 JobManager + job.* tests (§9.5, §10.4, §21.2 jobs/services row): adoption, lost, prompt waits, log offsets
and waits, merged-stream errors, extra_files + {run_dir}, progress.json/result.json, self_env for helper kinds,
caps, holds, masking of range edges, retention, result schemas."""
from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import jobs, proctree, util
from hive.errors import HiveError
from hive.policy import Limits
from tests import helpers
from tests.wp2util import PY, FakeRt, Node, assert_result_schema, handler_call, wait_for

POSIX = os.name != "nt"


class JobCase(unittest.TestCase):
    def setUp(self):
        self.rt = FakeRt()
        self.rt.jobs = jobs.JobManager(self.rt)
        self.rt.jobs.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        jm = self.rt.jobs
        for jid, r in list(jm._jobs.items()):
            if r.get("state") in ("starting", "running"):
                try:
                    jm.stop(jid, force=True)
                except Exception:  # noqa: BLE001
                    pass
        jm.shutdown()
        self.rt.cleanup()

    def call(self, action, params, **kw):
        return handler_call(action, params, runtime=self.rt, **kw)

    def start_job(self, argv, **kw):
        ctx = helpers.make_ctx("job.start", runtime=self.rt)
        args = dict(kind="job", argv_or_cmdline=argv, shell="none", cwd=self.rt.workdir, env=dict(os.environ),
                    stdin=None, name=None, timeout=0, merge_stderr=True, log_max_bytes=1 << 20)
        args.update(kw)
        return self.rt.jobs.start_job(ctx, **args), ctx


class TestJobActions(JobCase):
    def test_start_status_logs_list(self):
        r = self.call("job.start", {"argv": [PY, "-c", "import time\nfor i in range(3): print('n', i, flush=True)"],
                                    "name": "counter", "tags": ["t1"]})
        assert_result_schema(self, "job.start", r)
        self.assertEqual((r["name"], r["state"]), ("counter", "running"))
        self.assertTrue(r["log_path"].endswith("output.log"))
        st = self.call("job.status", {"job_id": r["job_id"], "wait": 10})
        assert_result_schema(self, "job.status", st)
        self.assertEqual((st["state"], st["exit_code"], st["kind"], st["tags"]), ("exited", 0, "job", ["t1"]))
        self.assertEqual(st["tail"], "n 0\nn 1\nn 2\n")
        self.assertEqual(st["output_bytes"], 12)
        lg = self.call("job.logs", {"job_id": r["job_id"], "offset": 4, "max_bytes": 1024})
        assert_result_schema(self, "job.logs", lg)
        self.assertEqual((lg["text"], lg["offset"], lg["next_offset"], lg["size"], lg["eof"]),
                         ("n 1\nn 2\n", 4, 12, 12, True))
        tail = self.call("job.logs", {"job_id": r["job_id"], "tail_lines": 1})
        self.assertEqual(tail["text"], "n 2\n")
        lst = self.call("job.list", {})
        assert_result_schema(self, "job.list", lst)
        self.assertEqual(lst["jobs"][0]["job_id"], r["job_id"])
        self.assertEqual(self.call("job.list", {"state": "running"})["jobs"], [])

    def test_wait_returns_promptly(self):
        r = self.call("job.start", {"argv": [PY, "-c", "import time; time.sleep(0.3)"]})
        t0 = time.monotonic()
        st = self.call("job.status", {"job_id": r["job_id"], "wait": 20})
        self.assertEqual(st["state"], "exited")
        self.assertLess(time.monotonic() - t0, 8)          # well before the 20 s wait

    def test_logs_wait_for_new_data(self):
        r = self.call("job.start", {"argv": [PY, "-c", "import time; print('a', flush=True); time.sleep(1); "
                                                       "print('b', flush=True); time.sleep(1)"]})
        wait_for(lambda: self.call("job.logs", {"job_id": r["job_id"]})["size"] >= 2)
        t0 = time.monotonic()
        lg = self.call("job.logs", {"job_id": r["job_id"], "offset": 2, "wait": 10})
        self.assertEqual(lg["text"], "b\n")
        self.assertLess(time.monotonic() - t0, 5)
        self.assertFalse(lg["eof"])

    def test_merged_stream_choice(self):
        r = self.call("job.start", {"argv": [PY, "-c", "print(1)"]})
        with self.assertRaises(HiveError) as cm:
            self.call("job.logs", {"job_id": r["job_id"], "stream": "stderr"})
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("this job merged stderr; use stream='output'", cm.exception.hint)
        r2 = self.call("job.start", {"argv": [PY, "-c", "import sys; print('o'); sys.stderr.write('e\\n')"],
                                     "merge_stderr": False})
        self.call("job.status", {"job_id": r2["job_id"], "wait": 10})
        self.assertEqual(self.call("job.logs", {"job_id": r2["job_id"], "stream": "stderr"})["text"], "e\n")
        self.assertEqual(self.call("job.logs", {"job_id": r2["job_id"], "stream": "output"})["text"], "o\n")

    def test_stop(self):
        r = self.call("job.start", {"command": "sleep 30" if POSIX else "Start-Sleep 30"})
        st = self.call("job.stop", {"job_id": r["job_id"], "grace": 2})
        assert_result_schema(self, "job.stop", st)
        self.assertEqual(st["state"], "killed")
        self.assertNotIn("tail", st)
        self.assertEqual(self.call("job.stop", {"job_id": r["job_id"]})["state"], "killed")   # idempotent

    def test_unknown_job(self):
        with self.assertRaises(HiveError) as cm:
            self.call("job.status", {"job_id": "j_doesnotexist"})
        self.assertEqual(cm.exception.code, "not_found")

    def test_timeout_job(self):
        r = self.call("job.start", {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 1})
        st = self.call("job.status", {"job_id": r["job_id"], "wait": 25})
        self.assertEqual(st["state"], "timeout")

    def test_max_runtime_policy(self):
        lim = Limits(jobs_max_runtime=100)
        pol = helpers.FakeEffectivePolicy(workdir=self.rt.workdir, data_dir=self.rt.data_dir, limits=lim)
        with self.assertRaises(HiveError) as cm:
            handler_call("job.start", {"argv": [PY, "-c", "pass"], "timeout": 500}, runtime=self.rt, policy=pol)
        self.assertIn("max timeout on this node is 100", cm.exception.hint)
        r = handler_call("job.start", {"argv": [PY, "-c", "pass"]}, runtime=self.rt, policy=pol)
        self.assertEqual(self.rt.jobs.record(r["job_id"])["timeout"], 100.0)   # 0 = unlimited -> max_runtime

    def test_keep_awake_warning(self):
        r = self.call("job.start", {"argv": [PY, "-c", "pass"], "keep_awake": True})
        if not util.IS_WINDOWS and not util.IS_MAC:
            self.assertEqual(r["warnings"], [jobs.KEEP_AWAKE_WARNING])
        else:  # pragma: no cover
            self.assertNotIn("warnings", r)

    def test_failed_spawn(self):
        with self.assertRaises(HiveError) as cm:
            self.call("job.start", {"argv": ["/nonexistent/x"]})
        self.assertEqual(cm.exception.code, "not_found")   # build_argv refuses a missing program up front


class TestJobManager(JobCase):
    def test_adoption_after_runtime_restart(self):
        d = self.rt.workdir
        gate = d / "go"
        info, _ = self.start_job([PY, "-c", f"import os,time\nwhile not os.path.exists({str(gate)!r}): "
                                            "time.sleep(0.05)\nprint('finished')"])
        jid = info["job_id"]
        wait_for(lambda: self.rt.jobs.record(jid)["state"] == "running")
        self.rt.jobs.shutdown()                       # drop the manager; the job keeps running
        jm2 = jobs.JobManager(self.rt)
        jm2.start()
        self.rt.jobs = jm2
        rec = jm2.record(jid)
        self.assertTrue(rec["adopted"])
        self.assertEqual(rec["state"], "running")
        self.assertIn(rec["shim_pid"], jm2.managed_pids())
        gate.write_text("x")
        self.assertTrue(jm2.wait_final(jid, 20))
        rec = jm2.record(jid)
        self.assertEqual((rec["state"], rec["exit_code"]), ("exited", 0))

    @unittest.skipUnless(POSIX, "SIGKILL")
    def test_lost_when_shim_killed(self):
        info, _ = self.start_job([PY, "-c", "import time; time.sleep(60)"])
        jid = info["job_id"]
        rec = self.rt.jobs.record(jid)
        wait_for(lambda: self.rt.jobs.record(jid).get("pid"))
        child = self.rt.jobs.record(jid)["pid"]
        os.kill(rec["shim_pid"], signal.SIGKILL)
        self.assertTrue(self.rt.jobs.wait_final(jid, 10))
        rec = self.rt.jobs.record(jid)
        self.assertEqual(rec["state"], "lost")
        wait_for(lambda: not util.pid_alive(child), 5)   # the orphaned child is not left running

    def test_extra_files_and_run_dir(self):
        info, _ = self.start_job([PY, os.path.join("{run_dir}", "x.py"), "{run_dir}"], kind="download",
                                 extra_files={"x.py": b"import sys; print(open(sys.argv[1] + '/data.txt').read())",
                                              "data.txt": b"payload"})
        self.assertTrue(self.rt.jobs.wait_final(info["job_id"], 10))
        rd = self.rt.jobs.run_dir(info["job_id"])
        self.assertEqual((rd / "output.log").read_text().strip(), "payload")
        if POSIX:
            self.assertEqual((rd / "x.py").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(HiveError):
            self.start_job([PY, "-c", "pass"], extra_files={"../evil": b"x"})

    def test_progress_and_result(self):
        code = ("import json,sys,time,os\nrd=sys.argv[1]\n"
                "def w(n,d):\n p=os.path.join(rd,n); open(p+'.t','w').write(json.dumps(d)); os.replace(p+'.t',p)\n"
                "w('progress.json',{'v':1,'done':5,'total':10,'percent':50.0,'note':'half'})\ntime.sleep(1.5)\n"
                "w('result.json',{'v':1,'path':'/x','size':10,'sha256':'ab'})\n")
        info, _ = self.start_job([PY, "-c", code, "{run_dir}"], kind="download")
        jid = info["job_id"]
        wait_for(lambda: (self.rt.jobs.record(jid).get("progress") or {}).get("done") == 5, 10)
        self.assertTrue(self.rt.jobs.wait_final(jid, 10))
        st = self.rt.jobs.get(jid)
        self.assertEqual(st["result"], {"path": "/x", "size": 10, "sha256": "ab"})
        self.assertEqual(st["progress"]["percent"], 50.0)
        self.assertIsNone(st["error"])
        self.assertEqual(self.rt.jobs.running_count("download"), 0)

    def test_download_error_from_last_line(self):
        info, _ = self.start_job([PY, "-c", "import sys; print('step'); print('Error: 404 not found'); sys.exit(3)"],
                                 kind="pull")
        self.assertTrue(self.rt.jobs.wait_final(info["job_id"], 10))
        self.assertEqual(self.rt.jobs.get(info["job_id"])["error"], "Error: 404 not found")

    def test_self_env_only_for_helper_kinds(self):
        seen = []
        real = proctree.spawn_shim

        def spy(spec, run_dir):
            seen.append(dict(spec["env"]))
            return real(spec, run_dir)
        selfenv = {"PYTHONPATH": "/x/checkout"}
        with mock.patch.object(proctree, "spawn_shim", spy), mock.patch.object(util, "self_env",
                                                                              return_value=selfenv):
            self.start_job(util.self_command() + ["--version"], kind="download", env={"A": "1"})
            self.start_job([PY, "-c", "pass"], kind="job", env={"A": "1"})
        self.assertEqual(seen[0].get("PYTHONPATH"), "/x/checkout")
        self.assertNotIn("PYTHONPATH", seen[1])

    def test_running_cap(self):
        lim = Limits(jobs_max_running=1)
        pol = helpers.FakeEffectivePolicy(workdir=self.rt.workdir, data_dir=self.rt.data_dir, limits=lim)
        handler_call("job.start", {"argv": [PY, "-c", "import time; time.sleep(30)"]}, runtime=self.rt, policy=pol)
        with self.assertRaises(HiveError) as cm:
            handler_call("job.start", {"argv": [PY, "-c", "pass"]}, runtime=self.rt, policy=pol)
        self.assertEqual((cm.exception.code, cm.exception.details["reason"]), ("rate_limited", "jobs_running"))

    def test_hold_taken_over_and_released(self):
        holds = mock.Mock()
        holds.paths.return_value = ["/w/run.sh"]
        self.rt.holds = holds
        gate = self.rt.workdir / "g"
        ctx = helpers.make_ctx("job.start", runtime=self.rt)
        ctx.hold = "ho_test"
        info = self.rt.jobs.start_job(ctx, kind="job", argv_or_cmdline=[PY, "-c", f"import os,time\nwhile not "
                                      f"os.path.exists({str(gate)!r}): time.sleep(0.05)"], shell="none",
                                      cwd=self.rt.workdir, env=dict(os.environ), stdin=None, name=None, timeout=0,
                                      merge_stderr=True, log_max_bytes=1 << 20)
        self.assertIsNone(ctx.hold)
        self.assertEqual(self.rt.jobs.record(info["job_id"])["held_paths"], ["/w/run.sh"])
        holds.release.assert_not_called()
        gate.write_text("x")
        self.assertTrue(self.rt.jobs.wait_final(info["job_id"], 10))
        holds.release.assert_called_once_with("ho_test")

    def test_prune(self):
        info, _ = self.start_job([PY, "-c", "pass"])
        info2, _ = self.start_job([PY, "-c", "pass"], kind="shell")
        for j in (info["job_id"], info2["job_id"]):
            self.assertTrue(self.rt.jobs.wait_final(j, 10))
        old = util.iso_utc(time.time() - 2 * 3600)
        with self.rt.jobs._lock:
            self.rt.jobs._jobs[info2["job_id"]]["ended_at"] = old
        self.rt.jobs.prune()
        self.assertIn(info["job_id"], self.rt.jobs._jobs)          # a job kind is kept (retention 7 days)
        self.assertNotIn(info2["job_id"], self.rt.jobs._jobs)      # shell jobs go after 1 h
        self.assertFalse((self.rt.jobs.dir / info2["job_id"]).exists())
        with self.rt.jobs._lock:
            self.rt.jobs._jobs[info["job_id"]]["ended_at"] = util.iso_utc(time.time() - 8 * 86400)
        self.rt.jobs.prune()
        self.assertNotIn(info["job_id"], self.rt.jobs._jobs)

    def test_missing_v_and_unknown_fields(self):
        info, _ = self.start_job([PY, "-c", "import time; time.sleep(30)"])
        jid = info["job_id"]
        wait_for(lambda: self.rt.jobs.record(jid).get("pid"))
        self.rt.jobs.shutdown()
        rd = self.rt.jobs.run_dir(jid)
        proctree.request_stop(rd, 0.2, force=True)
        wait_for(lambda: (rd / "exit.json").exists())
        ex = json.loads((rd / "exit.json").read_text())
        ex.pop("v")
        ex["future_field"] = 1
        (rd / "exit.json").write_text(json.dumps(ex))
        jm2 = jobs.JobManager(self.rt)
        jm2.start()
        self.rt.jobs = jm2
        self.assertEqual(jm2.record(jid)["state"], "killed")


class TestJobLogsMasking(unittest.TestCase):
    def test_offset_inside_secret(self):
        with Node() as n:
            secret = "hf_" + "Z" * 30
            helpers.set_secret(n.rt, "HF_TOKEN", secret)
            pad = "x" * 1010
            r = n.ok("job.start", {"argv": [PY, "-c", f"print('{pad}token is {secret} end')"]})
            n.ok("job.status", {"job_id": r["job_id"], "wait": 10})
            whole = n.ok("job.logs", {"job_id": r["job_id"]})
            self.assertIn("«secret:HF_TOKEN»", whole["text"])
            self.assertNotIn(secret, json.dumps(whole))
            # a range that ends in the middle of the secret and one that starts there
            mid = 1024
            self.assertTrue(len(pad) + len("token is ") < mid < len(pad) + len("token is ") + len(secret))
            a = n.ok("job.logs", {"job_id": r["job_id"], "offset": 0, "max_bytes": mid})
            b = n.ok("job.logs", {"job_id": r["job_id"], "offset": mid, "max_bytes": 4096})
            for part in (a, b):
                self.assertTrue(part.get("masked"))
                self.assertNotIn("Z" * 8, part["text"])
            self.assertEqual(a["next_offset"], mid)


if __name__ == "__main__":
    unittest.main()
