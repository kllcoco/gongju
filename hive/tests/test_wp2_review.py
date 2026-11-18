"""Regression tests for the WP2 review fixes: no respawn after a final exit, orphaned service children, adopted
``starting`` services keep their start_period, endpoints ``server`` from endpoint_kind, service.status cancel, code
dirs resolved like a spawn, mask_edges context at small offsets, ``{run_dir}`` never substituted in agent text, the
jobs.max_running reservation, psutil cpu sampling, service.start during ``stopping``, and the shim's Windows
fallback stdin handle."""
from __future__ import annotations

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import _shim, jobs, services, util
from hive.errors import HiveError
from hive.policy import Limits
from tests import helpers
from tests.test_services import SLEEPER, SvcCase
from tests.wp2util import FILES, PY, FakeRt, Node, handler_call, wait_for

POSIX = os.name != "nt"
SECRET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ012345678"     # 35 bytes


def _chunks(s: str, n: int = 6):
    return [s[i:i + n] for i in range(0, len(s) - n + 1)]


class TestServiceExitAndOrphans(SvcCase):
    def test_on_failure_exit_zero_not_respawned(self):
        self.define(name="ok0", argv=[PY, "-c", "print('hi')"], restart="on-failure")
        self.wait_state("ok0", "stopped")
        time.sleep(services.TICK * 10)
        rt = self.sm.runtime("ok0")
        self.assertEqual((rt["state"], rt["desired"], rt["restarts"]), ("stopped", "stopped", []))
        self.assertEqual(self.n.ok("service.logs", {"name": "ok0"})["text"].count("hi"), 1)
        # a manual start runs it again, once
        self.n.ok("service.start", {"name": "ok0", "wait": 5})
        self.wait_state("ok0", "stopped")
        time.sleep(services.TICK * 5)
        self.assertEqual(self.n.ok("service.logs", {"name": "ok0"})["text"].count("hi"), 2)

    @unittest.skipUnless(POSIX, "SIGKILL of the shim")
    def test_shim_killed_child_killed_before_respawn(self):
        self.define(name="orph", argv=SLEEPER, restart="always", restart_delay=2, wait=10)
        rt = self.sm.runtime("orph")
        wait_for(lambda: self.sm.runtime("orph")["child_pid"], 10)
        rt = self.sm.runtime("orph")
        child, ct = rt["child_pid"], rt["child_ctime"]
        self.assertTrue(util.pid_alive(child, ct))
        os.kill(rt["shim_pid"], signal.SIGKILL)
        self.wait_state("orph", "backoff", timeout=10)
        wait_for(lambda: not util.pid_alive(child, ct), 5)     # gone before the respawn
        wait_for(lambda: self.sm.runtime("orph")["state"] == "running"
                 and self.sm.runtime("orph")["child_pid"] not in (None, child), 20)

    @unittest.skipUnless(POSIX, "SIGKILL of the shim")
    def test_dead_shim_at_node_start_kills_child(self):
        self.define(name="orph2", argv=SLEEPER, wait=10)
        wait_for(lambda: self.sm.runtime("orph2")["child_pid"], 10)
        rt = self.sm.runtime("orph2")
        child, ct = rt["child_pid"], rt["child_ctime"]
        self.n.stop_runtime()
        os.kill(rt["shim_pid"], signal.SIGKILL)
        wait_for(lambda: not util.pid_alive(rt["shim_pid"], rt["shim_ctime"]), 5)
        self.assertTrue(util.pid_alive(child, ct))
        self.n.rt = self.n.make_runtime()
        wait_for(lambda: not util.pid_alive(child, ct), 5)
        wait_for(lambda: self.sm.runtime("orph2")["state"] == "running"
                 and self.sm.runtime("orph2")["child_pid"] not in (None, child), 20)


class TestAdoptionKeepsStarting(SvcCase):
    def test_adopted_starting_keeps_start_period(self):
        with mock.patch.object(services, "_http_status", return_value=None):
            self.define(name="load", argv=SLEEPER, health_url="http://127.0.0.1:9/", start_period=1000,
                        unhealthy_threshold=1, health_interval=2, restart_on_unhealthy=True)
            self.assertEqual(self.state("load"), "starting")
            time.sleep(1.0)
            shim = self.sm.runtime("load")["shim_pid"]
            self.n.restart_runtime()
            rt = self.sm.runtime("load")
            self.assertEqual((rt["state"], rt["adopted"], rt["shim_pid"]), ("starting", True, shim))
            # the original start time is kept (start_period counts from the spawn, not the adoption)
            self.assertLessEqual(self.sm._rs["load"]["_started_mono"], services._mono() - 0.9)
            time.sleep(3.0)                           # several failing checks: still starting, never restarted
            rt = self.sm.runtime("load")
            self.assertEqual((rt["state"], rt["shim_pid"], rt["restarts"]), ("starting", shim, []))

    def test_adopted_starting_short_period_runs_out(self):
        with mock.patch.object(services, "_http_status", return_value=None):
            self.define(name="short", argv=SLEEPER, health_url="http://127.0.0.1:9/", start_period=3)
            time.sleep(1.5)
            self.n.restart_runtime()
            self.assertEqual(self.state("short"), "starting")
            self.wait_state("short", "unhealthy", timeout=10)


class TestEndpointsServer(SvcCase):
    def test_server_from_endpoint_kind(self):
        self.define(name="olla", argv=SLEEPER, endpoint="http://127.0.0.1:11434/v1", endpoint_kind="ollama",
                    health="none", start=False)
        self.define(name="plain", argv=SLEEPER, endpoint="http://127.0.0.1:8001/v1", health="none", start=False)
        rows = {r["name"]: r for r in self.sm.endpoints()}
        self.assertEqual((rows["olla"]["server"], rows["olla"]["api"]), ("ollama", "ollama"))
        self.assertEqual((rows["plain"]["server"], rows["plain"]["api"]), ("unknown", "openai"))
        self.assertEqual(services._server_of({"preset": "vllm", "endpoint_kind": "openai"}), "vllm")


class TestStatusCancel(SvcCase):
    def test_status_wait_cancelled(self):
        with mock.patch.object(services, "_http_status", return_value=None):
            self.define(name="slow", argv=SLEEPER, health_url="http://127.0.0.1:9/", start_period=1000)
            ev = threading.Event()
            threading.Timer(0.5, ev.set).start()
            t0 = time.monotonic()
            env = self.n.call("service.status", {"name": "slow", "wait": 30}, cancel=ev)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertFalse(env["ok"])
            self.assertEqual(env["error"]["code"], "cancelled")


@unittest.skipUnless(POSIX, "executable script and POSIX paths")
class TestCodeDirsResolved(SvcCase):
    def test_relative_cwd_and_argv0(self):
        home = self.n.home
        (home / "proj").mkdir()
        (home / "proj" / "app.py").write_text("import time\ntime.sleep(60)\n")
        (home / "tools").mkdir()
        sh = home / "tools" / "serve.sh"
        sh.write_text("#!/bin/sh\nsleep 60\n")
        sh.chmod(0o755)
        (home / "notes.txt").write_text("x")
        self.define(name="rel", argv=[PY, "app.py"], cwd="proj", health="none", start=False)
        self.define(name="tool", argv=["tools/serve.sh"], health="none", start=False)
        dirs = self.sm.policy_view()["code_dirs"]
        self.assertIn(str(home / "proj"), dirs)
        self.assertIn(str(home / "tools"), dirs)
        eff = self.n.rt.effective(FILES)
        for p in ("~/proj/app.py", "~/tools/serve.sh", str(sh)):
            with self.assertRaises(HiveError, msg=p) as cm:
                eff.resolve_path(p, write=True)
            self.assertIn("managed service", cm.exception.message)
        eff.resolve_path("~/notes.txt", write=True)      # the workdir (a root) itself is not locked

    def test_home_var_cwd(self):
        (self.n.home / "svc").mkdir()
        r = self.define(name="hv", argv=SLEEPER, cwd="$HOME/svc", health="none", start=False)
        self.assertIn(str(self.n.home / "svc"), self.sm.policy_view()["code_dirs"])
        self.assertEqual(r["warnings"], [])


class TestReadLogMaskEdges(unittest.TestCase):
    def _check(self, n, read):
        size = len("token: ") + len(SECRET) + 1
        for off in range(0, size):
            for mb in (1024, 4096):
                r = read(off, mb)
                for c in _chunks(SECRET, 4):
                    self.assertNotIn(c, r["text"], f"offset {off} max_bytes {mb}: {r}")

    def test_job_and_service_logs_small_offsets(self):
        with Node() as n:
            helpers.set_secret(n.rt, "MY_TOKEN", SECRET)
            r = n.ok("job.start", {"argv": [PY, "-c", f"print('token: {SECRET}')"]})
            n.ok("job.status", {"job_id": r["job_id"], "wait": 10})
            self._check(n, lambda off, mb: n.ok("job.logs", {"job_id": r["job_id"], "offset": off,
                                                             "max_bytes": mb}))
            n.ok("service.define", {"name": "lg", "argv": [PY, "-c", f"print('token: {SECRET}')"],
                                    "restart": "no", "health": "none", "wait": 5})
            wait_for(lambda: SECRET.encode() in (n.rt.services._run_dir("lg") / "output.log").read_bytes(), 10)
            self._check(n, lambda off, mb: n.ok("service.logs", {"name": "lg", "offset": off, "max_bytes": mb}))


class TestRunDirNotSubstituted(unittest.TestCase):
    def test_agent_text_kept_literal(self):
        with Node() as n:
            code = "import sys; print(sys.argv[1])"
            r = n.ok("shell.run", {"argv": [PY, "-c", code, "{run_dir}/x"]})
            self.assertEqual(r["output"].strip(), "{run_dir}/x")
            if POSIX:
                r = n.ok("shell.run", {"command": "echo '{run_dir}'"})
                self.assertEqual(r["output"].strip(), "{run_dir}")
            j = n.ok("job.start", {"argv": [PY, "-c", code, "{run_dir}"]})
            n.ok("job.status", {"job_id": j["job_id"], "wait": 10})
            self.assertEqual(n.ok("job.logs", {"job_id": j["job_id"]})["text"].strip(), "{run_dir}")
            n.ok("service.define", {"name": "rd", "argv": [PY, "-c", code, "{run_dir}"], "restart": "no",
                                    "health": "none", "wait": 5})
            wait_for(lambda: (n.rt.services._run_dir("rd") / "output.log").exists()
                     and b"run_dir" in (n.rt.services._run_dir("rd") / "output.log").read_bytes(), 10)
            self.assertEqual(n.ok("service.logs", {"name": "rd"})["text"].strip(), "{run_dir}")

    def test_extra_file_path_still_substituted_for_jobs(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        rt.jobs = jobs.JobManager(rt)
        rt.jobs.start()
        self.addCleanup(rt.jobs.shutdown)
        ctx = helpers.make_ctx("job.start", runtime=rt)
        info = rt.jobs.start_job(ctx, kind="job", argv_or_cmdline=[PY, os.path.join("{run_dir}", "s.py"),
                                                                    "{run_dir}"],
                                 shell="none", cwd=rt.workdir, env=dict(os.environ), stdin=None, name=None,
                                 timeout=0, merge_stderr=True, log_max_bytes=1 << 20,
                                 extra_files={"s.py": b"import sys; print('arg=' + sys.argv[1])"})
        self.assertTrue(rt.jobs.wait_final(info["job_id"], 10))
        out = (rt.jobs.run_dir(info["job_id"]) / "output.log").read_text().strip()
        self.assertEqual(out, "arg={run_dir}")


class TestRunningCapReservation(unittest.TestCase):
    def test_concurrent_starts_respect_cap(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        rt.jobs = jobs.JobManager(rt)
        rt.jobs.start()

        def cleanup():
            for jid, r in list(rt.jobs._jobs.items()):
                if r.get("state") in ("starting", "running"):
                    try:
                        rt.jobs.stop(jid, force=True)
                    except Exception:  # noqa: BLE001
                        pass
            rt.jobs.shutdown()
        self.addCleanup(cleanup)
        lim = Limits(jobs_max_running=2)
        pol = helpers.FakeEffectivePolicy(workdir=rt.workdir, data_dir=rt.data_dir, limits=lim)
        real = util.atomic_write_bytes

        def slow(*a, **kw):
            time.sleep(0.3)
            return real(*a, **kw)
        results = []
        barrier = threading.Barrier(6)

        def one():
            barrier.wait()
            try:
                handler_call("job.start", {"argv": [PY, "-c", "import time; time.sleep(30)"], "stdin": "x"},
                             runtime=rt, policy=pol)
                results.append("ok")
            except HiveError as e:
                results.append(e.code)
        with mock.patch.object(jobs.util, "atomic_write_bytes", side_effect=slow):
            ts = [threading.Thread(target=one) for _ in range(6)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
        self.assertEqual(sorted(results), ["ok", "ok"] + ["rate_limited"] * 4)
        self.assertEqual(rt.jobs.running_count(None), 2, [(r.get("state"), r.get("error")) for r in rt.jobs._jobs.values()])
        self.assertEqual(rt.jobs._reserved, {})


class _FakeProc:
    def __init__(self, pid, ct=1.0):
        self.pid = pid
        self.ct = ct
        self.calls = 0

    def is_running(self):
        return True

    def memory_info(self):
        return mock.Mock(rss=1000)

    def cpu_percent(self, interval=None):
        self.calls += 1
        return 0.0 if self.calls == 1 else 25.0


class TestCpuSampling(unittest.TestCase):
    def test_cpu_null_first_then_real(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        sm = services.ServiceManager(rt)
        made = []

        def proc(pid):
            p = _FakeProc(pid)
            made.append(p)
            return p
        fake = mock.Mock()
        fake.Process.side_effect = proc
        rs = {"state": "running", "shim_pid": 11, "child_pid": 12}
        with mock.patch.dict(sys.modules, {"psutil": fake}), \
                mock.patch.object(services.proctree, "descendants", return_value=[]):
            first = sm._resources("s", rs)
            second = sm._resources("s", rs)
        self.assertIsNone(first["cpu_pct"])
        self.assertEqual(second["cpu_pct"], 50.0)
        self.assertEqual(second["rss_total"], 2000)
        self.assertEqual(len(made), 2)                 # the Process objects are kept between calls


class TestStartDuringStopping(SvcCase):
    @unittest.skipUnless(POSIX, "SIGTERM-ignoring child")
    def test_start_while_stopping_spawns_with_caller(self):
        code = ("import signal,time\nsignal.signal(signal.SIGTERM, lambda *a: (time.sleep(1.0), exit(0)))\n"
                "print('up', flush=True)\nwhile True: time.sleep(0.1)")
        self.define(name="sd", argv=[PY, "-c", code], stop_timeout=10, health="none", wait=10)
        old = self.sm.runtime("sd")["shim_pid"]
        self.n.ok("service.stop", {"name": "sd", "wait": 0})
        self.assertEqual(self.state("sd"), "stopping")
        seen = []
        real = self.sm._spawn

        def spy(name, **kw):
            seen.append(kw)
            return real(name, **kw)
        with mock.patch.object(self.sm, "_spawn", side_effect=spy):
            st = self.n.ok("service.start", {"name": "sd", "wait": 10})
        self.assertEqual(st["state"], "running")
        self.assertNotEqual(self.sm.runtime("sd")["shim_pid"], old)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].get("manual"))
        self.assertIsNotNone(seen[0].get("ctx"))
        self.assertEqual(getattr(seen[0]["eff"].identity, "name", None), "op")
        time.sleep(services.TICK * 5)
        self.assertNotEqual(self.sm.runtime("sd")["shim_pid"], old)


class TestShimWindowsFallbackStdin(unittest.TestCase):
    def test_reopened_stdin_closed(self):
        d = Path(tempfile.mkdtemp(prefix="hiveshim-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        (d / "stdin").write_bytes(b"secret")
        sh = _shim.Shim({"argv": ["x"], "stdin_file": "stdin"}, d)
        opened = []
        real = sh._stdin

        def track():
            f = real()
            opened.append(f)
            return f

        class FakeP:
            _handle = 7

            def kill(self):
                pass

            def wait(self, t=None):
                return 0

        class FakeJob:
            def assign(self, h):
                return True

            def close(self):
                pass
        first = track()
        with mock.patch.object(sh, "_stdin", side_effect=track), \
                mock.patch.object(_shim.subprocess, "Popen", return_value=FakeP()), \
                mock.patch.object(_shim.winapi, "JobObject", FakeJob), \
                mock.patch.object(_shim.winapi, "resume_process", return_value=False):
            sh._spawn_windows(["x"], {"stdin": first})
        first.close()
        self.assertEqual(len(opened), 2)
        self.assertTrue(opened[1].closed)


if __name__ == "__main__":
    unittest.main()
