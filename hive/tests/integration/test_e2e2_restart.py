"""E2E 2 (M2, §21.2 Integration row; §10.4 adoption, §10.5 node start rules, §13.1 control.json, D55): restart the
node mid-job through ``control.json`` (``hive node restart``: exit 75, relaunched by the watchdog loop) -> the job is
adopted and finishes, the services keep running (same pid); a node crash within the same boot restarts a service
whose shim died; a reboot simulated with a changed ``boot_key`` restarts only the autostart service and marks the
running job ``lost``.

Real processes on loopback: ``hive init node``, ``hive node run`` under a restart-on-75 supervisor, the Python client
over HTTP with the ``this-pc`` agent token, and ``hive node restart|stop`` (CLI).
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, List

from hive import proctree, util
from hive import client as hclient

from tests import helpers
from tests.integration import m2util
from tests.integration.m2util import IS_WINDOWS, ROOT, STUB_OPENAI, Supervisor, http_json, run_cli, wait_for


@unittest.skipIf(IS_WINDOWS, "POSIX E2E (the Windows variant runs in the lead's Windows CI)")
class TestE2E2Restart(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="hivee2-")).resolve()
        cls.home, cls.user, cls.work = cls.base / "hive-home", cls.base / "user", cls.base / "work"
        for d in (cls.home, cls.user, cls.work):
            d.mkdir()
        cls.env = m2util.base_env(HIVE_HOME=str(cls.home), HOME=str(cls.user),
                                  PYTHONPATH=str(ROOT) + (os.pathsep + os.environ["PYTHONPATH"]
                                                          if os.environ.get("PYTHONPATH") else ""))
        cls.sups: List[Supervisor] = []
        cls.pids: List[int] = []
        try:
            cls.port = helpers.free_port()
            cls.url = f"http://127.0.0.1:{cls.port}"
            r = run_cli(["init", "node", "--yes", "--name", "rst", "--listen", f"127.0.0.1:{cls.port}",
                         "--root", str(cls.work), "--proxy", "off", "--mirror", "none"], env=cls.env)
            assert r.returncode == 0, r.stdout + r.stderr
            m2util.allow_root(cls.home / "node" / "config.json")
            cls.agent_token = (cls.home / "tokens" / "this-pc.token").read_text(encoding="utf-8").strip()
            cls.start_daemon()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def start_daemon(cls) -> Supervisor:
        sup = Supervisor([sys.executable, "-m", "hive", "node", "run"], env=cls.env, cwd=str(ROOT),
                         log=cls.base / "node.log", name="rst").start()
        cls.sups.append(sup)
        cls.sup = sup
        cls.wait_up()
        return sup

    @classmethod
    def wait_up(cls) -> None:
        wait_for(lambda: http_json("GET", cls.url + "/v1/health", timeout=2)[1]["result"]["role"] == "node", 60,
                 what="node health")

    @classmethod
    def tearDownClass(cls) -> None:
        for sup in getattr(cls, "sups", []):
            if sup.running:
                sup.kill()
        for pid in getattr(cls, "pids", []):
            if pid and util.pid_alive(pid):
                try:
                    proctree.kill_tree(pid, force=True, grace=1)
                except Exception:  # noqa: BLE001 - best effort
                    pass
        if os.environ.get("HIVE_TEST_KEEP"):
            print("kept", cls.base)
        else:
            shutil.rmtree(str(cls.base), ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------------
    def define(self, c: Any, name: str, *, autostart: bool) -> dict:
        port = helpers.free_port()
        script = self.work / "stub_openai.py"
        script.write_text(STUB_OPENAI, encoding="utf-8")
        st = c.call("service.define", {"name": name, "argv": [sys.executable, str(script), str(port), name],
                                        "port": port, "endpoint": f"http://127.0.0.1:{port}/v1",
                                        "health": "http", "health_url": f"http://127.0.0.1:{port}/health",
                                        "health_interval": 2, "autostart": autostart, "wait": 30})
        self.assertEqual((st["state"], st["health"]), ("running", "healthy"), st)
        return st

    def svc_runtime(self, name: str) -> dict:
        return json.loads((self.home / "node" / "state" / "services" / name / "runtime.json")
                          .read_text(encoding="utf-8"))

    def svc_pids(self, name: str) -> List[int]:
        rt = self.svc_runtime(name)
        return [int(p) for p in (rt.get("shim_pid"), rt.get("child_pid")) if p]

    def job_record(self, job_id: str) -> dict:
        return json.loads((self.home / "node" / "state" / "jobs" / job_id / "job.json").read_text(encoding="utf-8"))

    def wait_final(self, c: Any, job_id: str, timeout: float = 60) -> dict:
        return wait_for(lambda: (lambda s: s if s["state"] not in ("running", "starting") else None)(
            c.call("job.status", {"job_id": job_id, "wait": 10})), timeout, what=f"job {job_id} final")

    def test_e2e_2(self) -> None:
        # data dir: HIVE_HOME/node (§14.1 data_dir default)
        self.assertTrue((self.home / "node" / "config.json").exists())
        c = hclient.connect(self.url, token=self.agent_token)
        try:
            self._restart_mid_job(c)
            self._crash_same_boot(c)
            self._reboot(c)
        finally:
            c.close()
        r = run_cli(["node", "stop"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.sup.wait(30), self.sup.output())
        self.assertEqual(self.sup.exits[-1], 0)

    # -----------------------------------------------------------------------------------------------------------
    def _restart_mid_job(self, c: Any) -> None:
        """``hive node restart`` mid-job: exit 75 -> relaunched; the job is adopted, services keep running."""
        svc = self.define(c, "llm", autostart=False)
        auto = self.define(c, "auto", autostart=True)
        self.pids += self.svc_pids("llm") + self.svc_pids("auto")
        marker = self.work / "job-finished.txt"
        job = c.call("job.start", {"name": "long", "argv": [
            sys.executable, "-c", "import time,sys; print('job-begin', flush=True); time.sleep(6); "
                                  f"open({str(marker)!r}, 'w').write('ok'); print('job-end-adopted')"]})
        jid = job["job_id"]
        wait_for(lambda: "job-begin" in c.call("job.logs", {"job_id": jid})["text"], 20, what="job output")
        old_pid = self.sup.pid()
        r = run_cli(["node", "restart"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        wait_for(lambda: self.sup.starts >= 2, 60, what="node relaunch")
        self.assertIn(75, self.sup.exits)                    # D55: a requested restart exits 75
        self.wait_up()
        self.assertNotEqual(self.sup.pid(), old_pid)
        st = c.call("job.status", {"job_id": jid})
        self.assertEqual(st["state"], "running", st)         # the shim kept the job alive across the restart
        self.assertTrue(st["adopted"], st)
        self.assertFalse(marker.exists())
        st = self.wait_final(c, jid)
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0), st)
        self.assertIn("job-end-adopted", c.call("job.logs", {"job_id": jid})["text"])
        self.assertTrue(marker.exists())
        # services kept running: same pid, healthy again after the next check
        for name, before in (("llm", svc), ("auto", auto)):
            now = c.call("service.status", {"name": name, "wait": 20})
            self.assertEqual(now["state"], "running", now)
            self.assertEqual(now["pid"], before["pid"], (name, now, before))
            self.assertEqual(now["health"], "healthy", now)
        self.assertEqual(c.node().chat("still here?", service="llm"), "pong: still here?")

    def _crash_same_boot(self, c: Any) -> None:
        """A node crash (SIGKILL, no graceful exit) in the same boot while the service's shim also died: the next
        start restarts it because desired == running and the boot key matches (§10.5)."""
        before = c.call("service.status", {"name": "llm"})
        node_pid = self.sup.pid()
        assert node_pid
        dead = self.svc_pids("llm")
        os.kill(node_pid, signal.SIGKILL)                     # exit -9: the supervisor stops (not 75)
        self.assertTrue(self.sup.wait(30))
        for pid in dead:
            if util.pid_alive(pid):
                proctree.kill_tree(pid, force=True, grace=1)
        wait_for(lambda: not any(util.pid_alive(p) for p in dead), 20, what="service shim gone")
        self.start_daemon()
        now = c.call("service.status", {"name": "llm", "wait": 30})
        self.pids += self.svc_pids("llm")     # QA F2: registered before asserting, so a failure leaks nothing
        self.assertEqual((now["state"], now["health"]), ("running", "healthy"), now)
        self.assertNotEqual(now["pid"], before["pid"])
        auto = c.call("service.status", {"name": "auto"})     # its shim survived the crash: adopted
        self.assertEqual(auto["state"], "running", auto)

    def _reboot(self, c: Any) -> None:
        """A reboot: every process is gone and the boot key differs. Only the autostart service comes back; a job
        that was running is ``lost``."""
        job = c.call("job.start", {"name": "sleeper", "argv": [sys.executable, "-c", "import time; time.sleep(300)"]})
        jid = job["job_id"]
        self.pids.append(int(self.job_record(jid)["shim_pid"]))
        r = run_cli(["node", "stop"], env=self.env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.sup.wait(30))
        self.assertEqual(self.sup.exits[-1], 0)              # stop exits 0 and is not relaunched
        # "power off": kill every shim and child Hive started, then age the stored boot keys
        victims = self.svc_pids("llm") + self.svc_pids("auto")
        rec = self.job_record(jid)
        victims += [int(p) for p in (rec.get("shim_pid"), rec.get("pid")) if p]
        for pid in victims:
            if util.pid_alive(pid):
                proctree.kill_tree(pid, force=True, grace=1)
        wait_for(lambda: not any(util.pid_alive(p) for p in victims), 20, what="processes gone")
        for name in ("llm", "auto"):
            m2util.edit_json(self.home / "node" / "state" / "services" / name / "runtime.json",
                             lambda d: d.__setitem__("boot_key", "00000000-old-boot:1"))
        self.assertNotEqual(util.boot_key(), "00000000-old-boot:1")
        self.start_daemon()
        st = c.call("job.status", {"job_id": jid})
        self.assertEqual(st["state"], "lost", st)
        llm = c.call("service.status", {"name": "llm"})
        self.assertEqual(llm["state"], "stopped", llm)
        self.assertIn("not restarted after reboot", llm["last_error"] or "")
        auto = c.call("service.status", {"name": "auto", "wait": 40})
        self.assertEqual((auto["state"], auto["health"]), ("running", "healthy"), auto)
        self.pids += self.svc_pids("auto")
        # the owner (agent) starts the stopped one again by hand
        llm = c.call("service.start", {"name": "llm", "wait": 30})
        self.assertEqual(llm["state"], "running", llm)
        self.pids += self.svc_pids("llm")
        for name in ("llm", "auto"):
            c.call("service.remove", {"name": name, "wait": 20})
        time.sleep(0.2)


if __name__ == "__main__":
    unittest.main()
