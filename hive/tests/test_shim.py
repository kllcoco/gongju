"""WP2 shim tests (§10.2, §21.2 proctree/shim/textlog row): exit codes, signal exits, merge vs split, rotation,
stop file, stop_argv before the signal, post_exit_argv, shim-owned timeout, stdin file, spec deletion, "v" fields,
spawn failure, eager imports (import hook and a replaced pyz), uninstalled-checkout imports via self_env()."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from hive import _shim, proctree, textlog, util
from tests.wp2util import PY, wait_for

POSIX = os.name != "nt"
ROOT = Path(__file__).resolve().parents[1]


def base_spec(argv, **kw: Any) -> Dict[str, Any]:
    spec = {"v": 1, "argv": argv, "cmdline": None, "cwd": None, "env": dict(os.environ), "stdin_file": None,
            "merge_stderr": True, "logs": {"output": "output.log"}, "log_max_bytes": 1 << 20, "log_keep": 1,
            "timeout": 0, "timeout_grace": 2, "keep_awake": False, "stop_argv": None, "post_exit_argv": None,
            "files": {"status": "status.json", "exit": "exit.json", "stop": "stop", "ready": "ready.json"}}
    spec.update(kw)
    return spec


class ShimCase(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hiveshim-")).resolve()
        self.run_dir = self.d / "run"
        self.run_dir.mkdir()

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def start(self, argv, **kw: Any) -> proctree.ShimHandle:
        h = proctree.spawn_shim(base_spec(argv, **kw), self.run_dir)
        self.addCleanup(self._cleanup, h)
        return h

    def _cleanup(self, h: proctree.ShimHandle) -> None:
        if h.popen is not None and h.popen.poll() is None:
            proctree.request_stop(self.run_dir, 0.5, force=True)
            try:
                h.popen.wait(10)
            except subprocess.TimeoutExpired:
                h.popen.kill()
                h.popen.wait()

    def wait_exit(self, h: proctree.ShimHandle, timeout: float = 20) -> dict:
        rc = h.popen.wait(timeout)
        self.assertIn(rc, (0, 127))
        return json.loads((self.run_dir / "exit.json").read_text())

    def wait_ready(self) -> dict:
        wait_for(lambda: (self.run_dir / "ready.json").exists(), 15)
        return json.loads((self.run_dir / "ready.json").read_text())

    def log(self, name: str = "output.log") -> bytes:
        p = self.run_dir / name
        return p.read_bytes() if p.exists() else b""


class TestShimBasics(ShimCase):
    def test_exit_code_and_files(self):
        h = self.start([PY, "-c", "import sys; print('hi'); sys.exit(7)"])
        ex = self.wait_exit(h)
        self.assertEqual((ex["v"], ex["exit_code"], ex["signal"], ex["reason"]), (1, 7, None, "exit"))
        self.assertEqual(self.log(), b"hi\n")
        self.assertFalse((self.run_dir / "spec.json").exists())        # deleted right after reading
        ready = json.loads((self.run_dir / "ready.json").read_text())
        status = json.loads((self.run_dir / "status.json").read_text())
        self.assertEqual(ready["v"], 1)
        self.assertEqual(status["v"], 1)
        self.assertEqual(status["state"], "exited")
        self.assertEqual(status["shim_pid"], h.shim_pid)
        self.assertTrue(ready["child_pid"])
        if POSIX:
            for f in ("exit.json", "ready.json", "status.json"):
                self.assertEqual((self.run_dir / f).stat().st_mode & 0o777, 0o600, f)

    @unittest.skipUnless(POSIX, "POSIX signals")
    def test_signal_exit(self):
        h = self.start([PY, "-c", "import os,signal; os.kill(os.getpid(), signal.SIGKILL)"])
        ex = self.wait_exit(h)
        self.assertEqual((ex["exit_code"], ex["signal"]), (137, "SIGKILL"))

    def test_split_streams_and_utf8(self):
        h = self.start([PY, "-c", "import sys; sys.stdout.write('out 中文\\n'); sys.stderr.write('err\\n')"],
                       merge_stderr=False, logs={"stdout": "stdout.log", "stderr": "stderr.log"},
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        self.wait_exit(h)
        self.assertEqual(self.log("stdout.log").decode("utf-8"), "out 中文\n")
        self.assertEqual(self.log("stderr.log"), b"err\n")
        self.assertFalse((self.run_dir / "output.log").exists())

    def test_merged_streams(self):
        h = self.start([PY, "-c", "import sys; print('a', flush=True); sys.stderr.write('b\\n')"])
        self.wait_exit(h)
        self.assertEqual(sorted(self.log().split()), [b"a", b"b"])

    def test_rotation_monotonic_offsets(self):
        code = "import sys\nfor i in range(3000): sys.stdout.write('row %05d\\n' % i)\n"
        h = self.start([PY, "-c", code], log_max_bytes=3 * 4096, log_keep=2)
        self.wait_exit(h)
        log = self.run_dir / "output.log"
        self.assertTrue((self.run_dir / "output.log.1").exists())
        self.assertEqual(textlog.log_size(log), 3000 * 10)
        self.assertEqual(textlog.tail_lines(log, 1), b"row 02999\n")
        r = textlog.read_range(log, 2999 * 10, 100, None)
        self.assertEqual(r["data"], b"row 02999\n")

    def test_stdin_file_deleted(self):
        (self.run_dir / "stdin").write_text("from stdin\n")
        h = self.start([PY, "-c", "import sys; print(sys.stdin.read().upper())"], stdin_file="stdin")
        self.wait_exit(h)
        self.assertIn(b"FROM STDIN", self.log())
        self.assertFalse((self.run_dir / "stdin").exists())

    def test_env_and_cwd(self):
        h = self.start([PY, "-c", "import os; print(os.environ.get('HIVE_T'), os.getcwd())"],
                       env=dict(os.environ, HIVE_T="v1"), cwd=str(self.d))
        self.wait_exit(h)
        self.assertEqual(self.log().decode().split(), ["v1", str(self.d)])

    def test_spawn_failure(self):
        h = self.start(["/nonexistent/hive-program"])
        rc = h.popen.wait(20)
        self.assertEqual(rc, 127)
        ex = json.loads((self.run_dir / "exit.json").read_text())
        self.assertIsNone(ex["exit_code"])
        self.assertTrue(ex["error"])
        self.assertIn(b"could not start", self.log())


class TestShimStop(ShimCase):
    def test_stop_file_graceful(self):
        h = self.start([PY, "-c", "import time; print('up', flush=True); time.sleep(60)"])
        self.wait_ready()
        wait_for(lambda: b"up" in self.log())
        proctree.request_stop(self.run_dir, 5)
        ex = self.wait_exit(h)
        self.assertEqual(ex["reason"], "stop")
        if POSIX:
            self.assertEqual((ex["exit_code"], ex["signal"]), (143, "SIGTERM"))

    @unittest.skipUnless(POSIX, "SIGTERM semantics")
    def test_ignoring_child_hard_killed_after_grace(self):
        code = ("import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('up', flush=True); "
                "time.sleep(60)")
        h = self.start([PY, "-c", code])
        wait_for(lambda: b"up" in self.log())
        t0 = time.monotonic()
        proctree.request_stop(self.run_dir, 1)
        ex = self.wait_exit(h)
        self.assertGreaterEqual(time.monotonic() - t0, 0.9)
        self.assertEqual((ex["exit_code"], ex["signal"], ex["reason"]), (137, "SIGKILL", "stop"))

    def test_stop_argv_runs_before_signal(self):
        marker = self.d / "please-exit"
        code = ("import os,signal,sys,time\n"
                "if hasattr(signal,'SIGTERM'): signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('up', flush=True)\n"
                f"while not os.path.exists({str(marker)!r}): time.sleep(0.05)\n"
                "time.sleep(1.0)\n"   # the shim appends the stop_command output once that command has exited
                "print('clean shutdown', flush=True); sys.exit(0)\n")
        stop = [PY, "-c", f"open({str(marker)!r},'w').write('x'); print('stopping now')"]
        h = self.start([PY, "-c", code], stop_argv=stop)
        wait_for(lambda: b"up" in self.log())
        proctree.request_stop(self.run_dir, 10)
        ex = self.wait_exit(h)
        self.assertEqual((ex["exit_code"], ex["reason"]), (0, "stop"))
        text = self.log().decode()
        self.assertIn("[hive stop] stopping now", text)
        self.assertIn("[hive] stop_command exit 0", text)
        self.assertIn("clean shutdown", text)
        self.assertLess(text.index("[hive stop]"), text.index("clean shutdown"))

    def test_force_skips_stop_argv(self):
        marker = self.d / "stop-ran"
        h = self.start([PY, "-c", "import time; print('up', flush=True); time.sleep(60)"],
                       stop_argv=[PY, "-c", f"open({str(marker)!r},'w').write('x')"])
        wait_for(lambda: b"up" in self.log())
        proctree.request_stop(self.run_dir, 10, force=True)
        ex = self.wait_exit(h)
        self.assertEqual(ex["reason"], "stop")
        self.assertFalse(marker.exists())

    def test_post_exit_always_runs(self):
        marker = self.d / "post"
        h = self.start([PY, "-c", "import sys; sys.exit(3)"],
                       post_exit_argv=[PY, "-c", f"open({str(marker)!r},'w').write('done'); print('cleanup')"])
        ex = self.wait_exit(h)
        self.assertEqual(ex["exit_code"], 3)
        self.assertEqual(marker.read_text(), "done")
        self.assertIn(b"[hive post-exit] cleanup", self.log())

    def test_post_exit_after_crash(self):
        marker = self.d / "post2"
        h = self.start([PY, "-c", "import os; os._exit(9)"],
                       post_exit_argv=[PY, "-c", f"open({str(marker)!r},'w').write('x')"])
        self.wait_exit(h)
        self.assertTrue(marker.exists())

    def test_shim_owned_timeout(self):
        t0 = time.monotonic()
        h = self.start([PY, "-c", "import time; time.sleep(60)"], timeout=1, timeout_grace=1)
        ex = self.wait_exit(h)
        self.assertEqual(ex["reason"], "timeout")
        self.assertLess(time.monotonic() - t0, 15)

    @unittest.skipUnless(POSIX, "signals to the shim")
    def test_sigterm_to_shim_stops(self):
        import signal
        h = self.start([PY, "-c", "import time; print('up', flush=True); time.sleep(60)"])
        wait_for(lambda: b"up" in self.log())
        os.kill(h.shim_pid, signal.SIGTERM)
        ex = self.wait_exit(h)
        self.assertEqual(ex["reason"], "stop")

    @unittest.skipUnless(POSIX, "process groups")
    def test_hard_kill_sweeps_grandchildren(self):
        pf = self.d / "gc"
        code = ("import signal,subprocess,sys,time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],start_new_session=True)\n"
                f"open({str(pf)!r},'w').write(str(p.pid)); print('up', flush=True); time.sleep(60)")
        h = self.start([PY, "-c", code])
        wait_for(lambda: b"up" in self.log())
        gc = int(pf.read_text())
        proctree.request_stop(self.run_dir, 0.5)
        self.wait_exit(h)
        wait_for(lambda: not util.pid_alive(gc), 5)


class TestEagerImports(unittest.TestCase):
    def test_no_new_hive_modules_after_start(self):
        """An import hook asserts sys.modules gains no hive.* entry while the shim runs (stop, rotation, error and
        post_exit paths)."""
        d = Path(tempfile.mkdtemp(prefix="hiveeager-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        script = f"""
import json, os, sys, threading, time
sys.path.insert(0, {str(ROOT)!r})
import hive._shim as shim
from hive import proctree
before = {{m for m in sys.modules if m.startswith('hive')}}
class Hook:
    def find_module(self, name, path=None):
        if name.startswith('hive'):
            raise SystemExit('late import of ' + name)
    def find_spec(self, name, path=None, target=None):
        if name.startswith('hive'):
            print('LATE', name, flush=True)
            os._exit(99)
        return None
sys.meta_path.insert(0, Hook())
run = {str(d)!r}
spec = {{"v": 1, "argv": [sys.executable, "-c",
         "import signal,sys,time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
         "[sys.stdout.write('x'*100+'\\\\n') for _ in range(400)]\\nsys.stdout.flush()\\ntime.sleep(30)"],
         "cwd": None, "env": dict(os.environ), "merge_stderr": True, "logs": {{"output": "output.log"}},
         "log_max_bytes": 8192, "log_keep": 1, "timeout": 0, "timeout_grace": 1,
         "stop_argv": [sys.executable, "-c", "print('stopper')"],
         "post_exit_argv": ["/nonexistent/post-exit"]}}
def stopper():
    time.sleep(1.0)
    proctree.request_stop(run, 0.5)
threading.Thread(target=stopper, daemon=True).start()
rc = shim.Shim(spec, __import__('pathlib').Path(run)).run()
after = {{m for m in sys.modules if m.startswith('hive')}}
print('NEW', sorted(after - before), 'RC', rc, flush=True)
"""
        out = subprocess.run([PY, "-c", script], capture_output=True, text=True, timeout=60, cwd=str(d))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("NEW [] RC 0", out.stdout)
        ex = json.loads((d / "exit.json").read_text())
        self.assertEqual(ex["reason"], "stop")
        self.assertTrue((d / "output.log.1").exists())   # rotation happened

    def test_replaced_pyz_after_ready(self):
        """Run the shim from a built pyz, overwrite the pyz after ready.json, then exercise stop and post_exit:
        exit.json is still written cleanly (no late reads of the archive)."""
        d = Path(tempfile.mkdtemp(prefix="hivepyz-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        pyz = d / "hive.pyz"
        with zipfile.ZipFile(pyz, "w") as z:
            for p in (ROOT / "hive").rglob("*.py"):
                z.write(p, str(p.relative_to(ROOT)))
            z.writestr("__main__.py", "import sys\nfrom hive.__main__ import _main\nsys.exit(_main())\n")
        run = d / "run"
        run.mkdir()
        marker = d / "post"
        spec = base_spec([PY, "-c", "import time; print('up', flush=True); time.sleep(60)"],
                         post_exit_argv=[PY, "-c", f"open({str(marker)!r},'w').write('x')"],
                         log_max_bytes=4096)
        util.write_json_atomic(run / "spec.json", spec, private=True)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        p = subprocess.Popen([PY, str(pyz), "_shim", "--spec", str(run / "spec.json")], cwd=str(run), env=env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(lambda: (run / "ready.json").exists(), 20)
            pyz.write_bytes(b"garbage, not a zip" * 1000)     # upgrade replaced the archive
            proctree.request_stop(run, 2)
            self.assertEqual(p.wait(30), 0)
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
        ex = json.loads((run / "exit.json").read_text())
        self.assertEqual((ex["v"], ex["reason"]), (1, "stop"))
        self.assertTrue(marker.exists())


class TestSelfEnv(unittest.TestCase):
    def test_uninstalled_checkout_shim(self):
        """spawn_shim runs the shim with cwd = run dir; an uninstalled checkout imports hive through self_env()."""
        env_add = util.self_env()
        if not env_add:
            self.skipTest("hive is installed (site-packages); self_env() is empty")
        d = Path(tempfile.mkdtemp(prefix="hiveselfenv-")).resolve()
        self.addCleanup(shutil.rmtree, str(d), True)
        run = d / "run"
        clean = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        with unittest.mock.patch.dict(os.environ, clean, clear=True):
            h = proctree.spawn_shim(base_spec([PY, "-c", "print('ok')"]), run)
            self.assertEqual(h.popen.wait(20), 0)
        self.assertEqual((run / "output.log").read_bytes(), b"ok\n")
        self.assertIn(str(ROOT), env_add["PYTHONPATH"])

    def test_main_rejects_missing_spec(self):
        self.assertEqual(_shim.main([]), 64)
        self.assertEqual(_shim.main(["--spec", "/nonexistent/spec.json"]), 66)


@unittest.skipUnless(os.name == "nt", "Windows only")
class TestShimWindows(ShimCase):  # pragma: no cover - Windows CI
    def test_child_in_job_before_it_runs(self):
        code = ("import ctypes\nfrom ctypes import wintypes\nr=wintypes.BOOL()\n"
                "ctypes.windll.kernel32.IsProcessInJob(ctypes.windll.kernel32.GetCurrentProcess(), None, "
                "ctypes.byref(r))\nprint('injob', bool(r.value))")
        h = self.start([PY, "-c", code])
        self.wait_exit(h)
        self.assertIn(b"injob True", self.log())
        status = json.loads((self.run_dir / "status.json").read_text())
        self.assertTrue(status.get("job_object"))

    def test_ctrl_break_reaches_child(self):
        marker = self.d / "got-break"
        code = ("import signal,sys,time\n"
                f"def h(*a):\n open({str(marker)!r},'w').write('x'); sys.exit(0)\n"
                "signal.signal(signal.SIGBREAK, h)\nprint('up', flush=True)\n"
                "while True: time.sleep(0.1)")
        h = self.start([PY, "-c", code])
        wait_for(lambda: b"up" in self.log())
        proctree.request_stop(self.run_dir, 5)
        ex = self.wait_exit(h)
        self.assertEqual((ex["reason"], ex["exit_code"]), ("stop", 0))
        self.assertTrue(marker.exists())


import unittest.mock  # noqa: E402  (used above via unittest.mock.patch)

if __name__ == "__main__":
    unittest.main()
