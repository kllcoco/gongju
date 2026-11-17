"""WP2 proctree + winapi tests (§10.1, §21.2 proctree/shim/textlog row): shell argv per shell, build_argv with the
node PATH only, kill_tree (grandchildren, identity), descendants, run_quick, spawn_detached, spawn flags, the
Windows WMI relaunch env file (mocked off Windows) and Windows-only checks that skip elsewhere."""
from __future__ import annotations

import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import proctree, util, winapi
from hive.errors import HiveError
from tests.wp2util import PY, wait_for

POSIX = os.name != "nt"


class TestShellArgv(unittest.TestCase):
    def test_posix_shells(self):
        for name in ("bash", "sh"):
            if proctree.shell_path(name) is None:
                continue
            plan = proctree.build_shell_argv("echo 中文", name)
            self.assertEqual(plan.shell, name)
            self.assertTrue(os.path.isabs(plan.argv[0]))
            self.assertEqual(plan.argv[1:], ["-c", "echo 中文"])
        if proctree.shell_path("bash"):
            self.assertEqual(proctree.build_shell_argv("x", "bash", login=True).argv[1], "-lc")
            self.assertEqual(proctree.build_shell_argv("x", "sh", login=True).argv[1], "-c")   # sh has no -l
        auto = proctree.build_shell_argv("true", "auto")
        self.assertEqual(auto.shell, proctree.default_shell())

    def test_powershell_encoded(self):
        with mock.patch.object(proctree, "shell_path", return_value="/opt/pwsh/pwsh"):
            plan = proctree.build_shell_argv("Write-Output 'é'; exit 3", "pwsh")
        self.assertEqual(plan.argv[:6], ["/opt/pwsh/pwsh", "-NoLogo", "-NoProfile", "-NonInteractive",
                                         "-ExecutionPolicy", "Bypass"])
        self.assertEqual(plan.argv[6], "-EncodedCommand")
        script = base64.b64decode(plan.argv[7]).decode("utf-16-le")
        self.assertTrue(script.startswith(proctree.PS_PREFIX))
        self.assertTrue(script.endswith(proctree.PS_SUFFIX))
        self.assertIn("Write-Output 'é'; exit 3", script)
        self.assertIn("[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)", script)

    def test_powershell_long_script_uses_file(self):
        cmd = "Write-Output 'x'\n" * 3000
        with mock.patch.object(proctree, "shell_path", return_value="C:/pwsh.exe"):
            plan = proctree.build_shell_argv(cmd, "powershell")
        self.assertEqual(plan.argv[-2], "-File")
        self.assertIn("{run_dir}", plan.argv[-1])
        data = plan.files["command.ps1"]
        self.assertTrue(data.startswith("\ufeff".encode("utf-8")))
        self.assertIn(cmd.encode("utf-8"), data)

    def test_cmd_string(self):
        with mock.patch.object(proctree, "shell_path", return_value=r"C:\Windows\System32\cmd.exe"):
            plan = proctree.build_shell_argv("dir & echo 中文", "cmd")
        self.assertIsInstance(plan.argv, str)
        self.assertEqual(plan.argv, r'"C:\Windows\System32\cmd.exe" /d /s /c "chcp 65001>nul & dir & echo 中文"')

    def test_missing_shell_unsupported(self):
        with mock.patch.object(proctree, "shell_path", return_value=None):
            with self.assertRaises(HiveError) as cm:
                proctree.build_shell_argv("x", "zsh")
        self.assertEqual(cm.exception.code, "unsupported")
        if POSIX:
            with self.assertRaises(HiveError):
                proctree.build_shell_argv("x", "cmd")

    def test_available_shells_cached(self):
        rows = proctree.available_shells()
        self.assertTrue(rows)
        for r in rows:
            self.assertEqual(set(r), {"name", "path", "version"})
        self.assertEqual(proctree.available_shells(), rows)


class TestBuildArgv(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivept-")).resolve()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def _exe(self, folder: Path, name: str) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        p = folder / (name + (".cmd" if not POSIX else ""))
        p.write_text("#!/bin/sh\necho planted\n" if POSIX else "@echo planted\r\n")
        p.chmod(0o755)
        return p

    def test_node_path_only(self):
        trusted = self._exe(self.d / "trusted", "mytool")
        planted = self._exe(self.d / "agent", "mytool")
        node_path = str(self.d / "trusted")
        # the agent's PATH (os.environ / spec env) never decides; only node_path does
        with mock.patch.dict(os.environ, {"PATH": str(self.d / "agent") + os.pathsep + os.environ.get("PATH", "")}):
            argv = proctree.build_argv(["mytool", "-x"], node_path)
        self.assertEqual(argv, [str(trusted), "-x"])
        self.assertNotEqual(argv[0], str(planted))

    def test_not_found_and_hint(self):
        with self.assertRaises(HiveError) as cm:
            proctree.build_argv(["hive-nonexistent-tool"], str(self.d))
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("path_prepend", cm.exception.hint)
        with mock.patch.dict(os.environ, {"INVOCATION_ID": "x"}):
            with self.assertRaises(HiveError) as cm:
                proctree.build_argv(["hive-nonexistent-tool"], str(self.d))
        self.assertIn("PATH was captured when the service was installed", cm.exception.hint)

    @unittest.skipUnless(POSIX, "POSIX relative path semantics")
    def test_relative_with_separator_uses_cwd(self):
        self._exe(self.d / "sub", "run.sh")
        argv = proctree.build_argv(["./sub/run.sh", "a"], "", cwd=self.d)
        self.assertEqual(argv, [str(self.d / "sub" / "run.sh"), "a"])
        with self.assertRaises(HiveError):
            proctree.build_argv(["./sub/run.sh"], "", cwd=self.d / "elsewhere")

    def test_absolute(self):
        self.assertEqual(proctree.build_argv([PY, "-V"], "")[0], PY)

    def test_windows_never_cwd(self):
        # proctree.which never consults the cwd, also when it holds a same-named program
        self._exe(self.d, "cwdtool")
        old = os.getcwd()
        try:
            os.chdir(str(self.d))
            self.assertIsNone(proctree.which("cwdtool", str(self.d / "nothing")))
        finally:
            os.chdir(old)


def _alive(pid: int) -> bool:
    return util.pid_alive(pid)


class TestKillTree(unittest.TestCase):
    def _spawn_tree(self, detach_grandchild: bool = False) -> tuple:
        d = Path(tempfile.mkdtemp(prefix="hivekt-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        pidfile = d / "gc.pid"
        gc_kw = "start_new_session=True" if detach_grandchild else ""
        code = ("import subprocess,sys,time\n"
                f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],{gc_kw})\n"
                f"open({str(pidfile)!r},'w').write(str(p.pid))\n"
                "time.sleep(60)\n")
        kw = {"start_new_session": True} if POSIX else {}
        child = subprocess.Popen([PY, "-c", code], **kw)
        wait_for(lambda: pidfile.exists() and pidfile.read_text().strip())
        gc = int(pidfile.read_text())
        return child, gc

    def test_kills_grandchild(self):
        child, gc = self._spawn_tree()
        self.assertIn(gc, proctree.descendants(child.pid))
        res = proctree.kill_tree(child.pid, grace=2)
        child.wait(5)
        wait_for(lambda: not _alive(gc), 5)
        self.assertIn(child.pid, res["killed"])
        self.assertIn(gc, res["killed"])
        self.assertEqual(res["still_alive"], [])
        self.assertIn(res["method"], ("killpg", "signal", "taskkill", "taskkill/F"))

    @unittest.skipUnless(POSIX, "sessions")
    def test_kills_grandchild_in_other_session(self):
        child, gc = self._spawn_tree(detach_grandchild=True)
        res = proctree.kill_tree(child.pid, force=True)
        child.wait(5)
        wait_for(lambda: not _alive(gc), 5)
        self.assertIn(gc, res["killed"])

    def test_identity_mismatch_kills_nothing(self):
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        ct = util.process_create_time(child.pid)
        self.assertFalse(util.pid_alive(child.pid, (ct or 0) - 1000))
        res = proctree.kill_tree(child.pid, create_time=(ct or 0) - 1000)
        self.assertEqual(res["killed"], [])
        self.assertIsNone(child.poll())
        self.assertTrue(proctree.pid_alive(child.pid, ct))

    @unittest.skipUnless(POSIX, "signals")
    def test_grace_then_kill(self):
        child = subprocess.Popen([PY, "-c", "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                                            "print('ready', flush=True); time.sleep(60)"],
                                 stdout=subprocess.PIPE, start_new_session=True)
        child.stdout.readline()
        t0 = time.monotonic()
        res = proctree.kill_tree(child.pid, grace=0.5)
        child.wait(5)
        child.stdout.close()
        self.assertGreaterEqual(time.monotonic() - t0, 0.45)
        self.assertEqual(child.returncode, -signal.SIGKILL)
        self.assertEqual(res["method"], "killpg")

    def test_dead_pid(self):
        p = subprocess.Popen([PY, "-c", "pass"])
        p.wait()
        self.assertEqual(proctree.kill_tree(p.pid)["killed"], [])


class TestRunQuick(unittest.TestCase):
    def test_output_and_code(self):
        code, out, err = proctree.run_quick([PY, "-c", "import sys; print('o'); sys.stderr.write('e'); sys.exit(4)"],
                                            timeout=10)
        self.assertEqual((code, out.strip(), err), (4, b"o", b"e"))

    def test_cap(self):
        code, out, _ = proctree.run_quick([PY, "-c", "print('x'*100000)"], timeout=10, max_output=1000)
        self.assertEqual((code, len(out)), (0, 1000))

    def test_timeout_kills_tree(self):
        d = Path(tempfile.mkdtemp(prefix="hiverq-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        pf = d / "pid"
        code = ("import subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                f"open({str(pf)!r},'w').write(str(p.pid)); time.sleep(60)")
        t0 = time.monotonic()
        with self.assertRaises(HiveError) as cm:
            proctree.run_quick([PY, "-c", code], timeout=1.5)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertIs(cm.exception.details.get("maybe_executed"), False)
        self.assertLess(time.monotonic() - t0, 10)
        if pf.exists():
            gc = int(pf.read_text())
            wait_for(lambda: not util.pid_alive(gc), 5)

    def test_missing(self):
        with self.assertRaises(HiveError) as cm:
            proctree.run_quick(["/nonexistent/hive-tool"], timeout=2)
        self.assertEqual(cm.exception.code, "not_found")


class TestSpawn(unittest.TestCase):
    def test_spawn_detached(self):
        d = Path(tempfile.mkdtemp(prefix="hivesd-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        out = d / "o"
        pid, ct = proctree.spawn_detached([PY, "-c", f"open({str(out)!r},'w').write('x'); import time; "
                                                     "time.sleep(30)"], env=dict(os.environ), cwd=d)
        try:
            wait_for(out.exists)
            self.assertTrue(util.pid_alive(pid, ct))
            if POSIX:
                self.assertEqual(os.getsid(pid), pid)   # own session (start_new_session), like shims
        finally:
            proctree.kill_tree(pid, force=True, create_time=ct)
        wait_for(lambda: not util.pid_alive(pid, ct), 5)
        proctree._reap_detached()

    def test_spawn_flags(self):
        self.assertTrue(winapi.spawn_flags("free") & winapi.CREATE_NO_WINDOW)
        self.assertTrue(winapi.spawn_flags("free") & winapi.CREATE_NEW_PROCESS_GROUP)
        self.assertFalse(winapi.spawn_flags("free") & winapi.CREATE_BREAKAWAY_FROM_JOB)
        self.assertTrue(winapi.spawn_flags("breakaway") & winapi.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(winapi.spawn_flags("job") & winapi.CREATE_BREAKAWAY_FROM_JOB)

    def test_request_stop(self):
        d = Path(tempfile.mkdtemp(prefix="hivers-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        proctree.request_stop(d, 7, force=True)
        self.assertEqual(json.loads((d / "stop").read_text()), {"v": 1, "grace": 7.0, "force": True})
        if POSIX:
            self.assertEqual((d / "stop").stat().st_mode & 0o777, 0o600)

    def test_trusted_path_default_and_set(self):
        try:
            proctree.set_trusted_path("/a" + os.pathsep + "/b")
            self.assertEqual(proctree.trusted_path(), "/a" + os.pathsep + "/b")
        finally:
            proctree._trusted_path = None
        self.assertTrue(proctree.trusted_path())


class TestWinapiOffWindows(unittest.TestCase):
    @unittest.skipIf(winapi.IS_WINDOWS, "neutral values off Windows")
    def test_neutral(self):
        self.assertEqual(winapi.spawn_mode(), "free")
        self.assertIsNone(winapi.session_winlogon_ctime())
        self.assertIsNone(winapi.platform_info())
        self.assertEqual(winapi.process_list(), [])
        self.assertFalse(winapi.resume_process(1))
        self.assertFalse(winapi.send_ctrl_break(1))
        self.assertFalse(winapi.keep_awake(True))
        with self.assertRaises(HiveError):
            winapi.JobObject()

    def test_relaunch_script_has_no_values(self):
        s = winapi.relaunch_script(r"C:\data\run\relaunch-env.json", '"C:\\py\\pythonw.exe" -m hive node run',
                                   r"C:\data")
        self.assertIn("relaunch-env.json", s)
        self.assertIn("Remove-Item", s)
        self.assertIn("Win32_Process", s)
        self.assertIn("CreateFlags=[uint32]0x08000200", s)
        self.assertIn("ShowWindow=[uint16]0", s)
        self.assertIn("EnvironmentVariables", s)
        self.assertIn("CurrentDirectory='C:\\data'", s)

    def test_relaunch_env_file_flow(self):
        """The full env (+ HIVE_SPAWN_ESCAPED) goes through a 0600 file that is deleted, never through argv."""
        d = Path(tempfile.mkdtemp(prefix="hivewmi-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        cfg = d / "config.json"
        seen = {}

        def fake_ps(script, timeout):
            path = Path(d / "run" / "relaunch-env.json")
            seen["exists"] = path.exists()
            seen["env"] = json.loads(path.read_text())
            seen["mode"] = path.stat().st_mode & 0o777
            seen["script"] = script
            return 0, f"HIVE-RELAUNCH 0 {os.getpid()}\n"

        env = {"USERPROFILE": r"C:\Users\me", "TEMP": r"C:\Temp", "APPDATA": r"C:\Users\me\AppData\Roaming",
               "SECRETISH": "s3cr3t-value", "HIVE_SPAWN_ESCAPED": "1"}
        argv = [PY, "-m", "hive", "node", "run", "--config", str(cfg), "--relaunched"]
        with mock.patch.object(winapi, "IS_WINDOWS", True), mock.patch.object(winapi, "_run_powershell", fake_ps), \
                mock.patch.object(util, "secure_file", return_value=True):
            pid, ct = winapi.relaunch_outside_job(argv, env)
        self.assertEqual(pid, os.getpid())
        self.assertIsNotNone(ct)
        self.assertTrue(seen["exists"])
        self.assertEqual(seen["env"], env)
        if POSIX:
            self.assertEqual(seen["mode"], 0o600)
        self.assertNotIn("s3cr3t-value", seen["script"])
        self.assertNotIn("s3cr3t-value", " ".join(argv))
        self.assertFalse((d / "run" / "relaunch-env.json").exists())

    def test_relaunch_failure(self):
        d = Path(tempfile.mkdtemp(prefix="hivewmi-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        with mock.patch.object(winapi, "IS_WINDOWS", True), \
                mock.patch.object(winapi, "_run_powershell", return_value=(1, "HIVE-RELAUNCH 9 \n")), \
                mock.patch.object(util, "secure_file", return_value=True):
            with self.assertRaises(HiveError) as cm:
                winapi.relaunch_outside_job(["x", "--config", str(d / "c.json")], {})
        self.assertEqual((cm.exception.code, cm.exception.details["maybe_executed"]), ("unavailable", False))
        self.assertFalse((d / "run" / "relaunch-env.json").exists())


@unittest.skipUnless(winapi.IS_WINDOWS, "Windows only")
class TestWindowsOnly(unittest.TestCase):  # pragma: no cover - Windows CI
    def test_spawn_mode_values(self):
        self.assertIn(winapi.spawn_mode(), ("free", "silent", "breakaway", "job"))
        info = winapi.platform_info()
        self.assertEqual(set(info), {"spawn_mode", "session_id", "in_job"})

    def test_job_object_mode_detection(self):
        """A process inside a test-created Job Object without BREAKAWAY_OK detects 'job'."""
        code = ("import subprocess,sys,ctypes\n"
                "from hive import winapi\n"
                "print(winapi.spawn_mode())\n")
        job = winapi.JobObject()
        try:
            p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                 creationflags=winapi.CREATE_SUSPENDED)
            self.assertTrue(job.assign(int(p._handle)))
            self.assertTrue(winapi.resume_process(int(p._handle)))
            out, _ = p.communicate(timeout=60)
            self.assertEqual(out.decode().strip(), "job")
        finally:
            job.close()

    def test_toolhelp_and_winlogon(self):
        rows = winapi.process_list()
        self.assertTrue(any(pid == os.getpid() for pid, _pp, _n in rows))
        self.assertIsNotNone(winapi.session_id(None))
        ct = winapi.session_winlogon_ctime()
        self.assertTrue(ct is None or ct > 0)

    def test_create_no_window_on_shims(self):
        with mock.patch("subprocess.Popen") as P:
            P.return_value.pid = os.getpid()
            proctree._popen_detached(["x"], env={}, cwd=None)
        flags = P.call_args.kwargs["creationflags"]
        self.assertTrue(flags & winapi.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
