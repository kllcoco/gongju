"""M3 (§22.1 step 6): WSL keepalive logic at unit level, wired into a real NodeRuntime (§20.7 step 6, §13.1).

The Windows-only start path of ``NodeRuntime.start`` (``IS_WINDOWS and cfg.wsl.keepalive``) runs here with
``hive.node.IS_WINDOWS`` patched for the duration of ``start()`` and ``hive.wsl.wsl_exe`` pointing at a fake
``wsl.exe`` (a POSIX script that records its argv and sleeps), spawned through the real
``proctree.spawn_detached``. Checked:

* the runtime starts one keepalive per entry (``-d <distro> --exec /bin/sleep infinity`` or the entry's
  ``command``), registers the 10 s check, records pid + ctime in ``state/wsl-keepalive-<distro>.json``;
* ``keepalive_pids()`` ⊂ ``managed_pids()``; ``proc.kill`` refuses the keepalive pid (protected, §5.4 kill guard);
* ``node.info.wsl`` on a Windows node is ``{"keepalive":[{"distro","pid","running"}],"distros":[…]}`` and matches
  the catalog's result schema;
* ``node.panic`` (offline) leaves the keepalive running (it holds nothing); stopping the runtime leaves it running and
  a new runtime adopts it (no second ``wsl.exe``); a changed ``command`` replaces it;
* a dead keepalive is restarted by the periodic check after the backoff;
* ``hive init node --wsl-keepalive DISTRO`` on an existing config appends the entry and keeps tokens; the
  ``pause``/``panic`` CLI lines name the distro on Windows only.
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
from unittest import mock

from hive import catalog, config, node, proctree, schema, util, wsl
from hive.errors import HiveError

from tests import helpers
from tests.integration import m2util

POSIX = os.name != "nt"


@unittest.skipUnless(POSIX, "the fake wsl.exe is a POSIX script; the Windows CI spawns the real one")
class TestM3WslKeepalive(unittest.TestCase):

    def setUp(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="hivem3wsl-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.argv_log = self.base / "wsl-argv.log"
        tool = self.base / "bin"
        tool.mkdir()
        self.exe = tool / "wsl.exe"
        self.exe.write_text(f"#!{sys.executable}\nimport sys, time\n"
                            f"open({str(self.argv_log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                            "time.sleep(120)\n", encoding="utf-8")
        self.exe.chmod(0o755)
        self.home = self.base / "home"
        self.home.mkdir()
        env = mock.patch.dict(os.environ, {"HIVE_HOME": str(self.home), "HOME": str(self.base / "user")})
        (self.base / "user").mkdir()
        env.start()
        self.addCleanup(env.stop)
        self.runtimes: List[Any] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for rt in self.runtimes:
            try:
                rt.stop()
            except Exception:  # noqa: BLE001
                pass
        pids = set()
        for f in (self.home / "node" / "state").glob("wsl-keepalive-*.json"):
            try:
                rec = json.loads(f.read_text(encoding="utf-8"))
                pids.add(rec["pid"])
                if util.pid_alive(rec["pid"], rec.get("ctime")):
                    os.kill(rec["pid"], signal.SIGKILL)
            except (OSError, ValueError, KeyError):
                pass
        # reap until the keepalives are gone from proctree's list: one poll right after SIGKILL can still see one
        # running, and its Popen then stayed there until interpreter exit ("subprocess N is still running")
        try:
            helpers.wait_until(lambda: proctree.reap_detached() is None and
                               not any(d.pid in pids for d in proctree._detached), 10)
        except AssertionError:
            pass

    def runtime(self, entries: list) -> Any:
        raw = helpers.make_node_config(data_dir=str(self.home / "node"), wsl={"keepalive": entries})
        path = self.home / "node" / "config.json"
        util.write_json_atomic(path, raw, private=True)
        cfg = config.load_node_config(str(path))
        rt = node.NodeRuntime(cfg, config_path=path, start_managers=True)
        with mock.patch.object(node, "IS_WINDOWS", True), mock.patch.object(wsl, "wsl_exe",
                                                                            return_value=str(self.exe)):
            rt.start()
        self.runtimes.append(rt)
        return rt

    def lines(self) -> List[str]:
        return self.argv_log.read_text(encoding="utf-8").splitlines() if self.argv_log.exists() else []

    # -------------------------------------------------------------------------------------------------------------
    def test_runtime_wiring_protection_info_panic_adoption(self) -> None:
        rt = self.runtime([{"distro": "Ubuntu-24.04", "command": None}])
        self.assertIsNotNone(rt.keepalive, rt.warnings)
        st = rt.keepalive.status()
        self.assertEqual([(s["distro"], s["running"]) for s in st], [("Ubuntu-24.04", True)])
        pid = st[0]["pid"]
        helpers.wait_until(lambda: bool(self.lines()), 10)
        self.assertEqual(self.lines(), ["-d Ubuntu-24.04 --exec /bin/sleep infinity"])
        rec = wsl.read_keepalive_state(rt.state_dir, "Ubuntu-24.04")
        self.assertEqual((rec["pid"], rec["running"]), (pid, True))
        self.assertEqual(rec["argv"], [str(self.exe), "-d", "Ubuntu-24.04", "--exec", "/bin/sleep", "infinity"])
        self.assertEqual(rt.keepalive_pids(), {pid: "wsl-keepalive:Ubuntu-24.04"})
        self.assertEqual(rt.managed_pids().get(pid), "wsl-keepalive:Ubuntu-24.04")
        self.assertEqual([(p["name"], p["interval"]) for p in rt._periodic if p["name"] == "wsl-keepalive"],
                         [("wsl-keepalive", 10.0)])

        # proc.kill refuses it (operator agent with exec)
        env = rt.call(helpers.identity(), "proc.kill", {"pid": pid})
        self.assertFalse(env["ok"], env)
        self.assertEqual(env["error"]["code"], "policy_denied", env)
        self.assertTrue(util.pid_alive(pid, rec.get("ctime")))

        # node.info on a Windows node: the keepalive block, schema-valid
        with mock.patch("hive.actions.node.IS_WINDOWS", True), \
                mock.patch.object(wsl, "list_distros", return_value=[{"name": "Ubuntu-24.04", "state": "Running",
                                                                     "version": 2, "default": True}]):
            env = rt.call(helpers.identity(), "node.info", {})
        self.assertTrue(env["ok"], env)
        block = env["result"]["wsl"]
        self.assertEqual(block, {"keepalive": [{"distro": "Ubuntu-24.04", "pid": pid, "running": True}],
                                 "distros": ["Ubuntu-24.04"]})
        schema.validate(catalog.get("node.info").result, env["result"], coerce=False, path="result",
                        defaults=False)

        # panic (offline, as the CLI runs it) stops jobs/services/containers but not the keepalive
        res = node.panic(rt.config, rt.data_dir, force_after=1)
        self.assertNotIn(pid, res.get("jobs") or [])
        self.assertTrue(util.pid_alive(pid, rec.get("ctime")))
        node.resume(rt.data_dir)

        # stopping the runtime leaves it; the next runtime adopts it (no second wsl.exe)
        rt.stop()
        self.runtimes.remove(rt)
        self.assertTrue(util.pid_alive(pid, rec.get("ctime")))
        rt2 = self.runtime([{"distro": "Ubuntu-24.04", "command": None}])
        self.assertEqual(rt2.keepalive_pids(), {pid: "wsl-keepalive:Ubuntu-24.04"})
        time.sleep(0.3)
        self.assertEqual(len(self.lines()), 1)

        # dead -> restarted by check() once the backoff has passed
        os.kill(pid, signal.SIGKILL)
        helpers.wait_until(lambda: proctree.reap_detached() is None and not util.pid_alive(pid, rec.get("ctime")),
                           10)
        ka = rt2.keepalive
        ka.check()                                      # notices the death, defers 10 s
        self.assertEqual(rt2.keepalive_pids(), {})
        ka._entries["Ubuntu-24.04"]["next_try"] = 0.0
        with mock.patch.object(wsl, "wsl_exe", return_value=str(self.exe)):
            ka.check()
        pids = rt2.keepalive_pids()
        self.assertEqual(len(pids), 1)
        self.assertNotIn(pid, pids)
        helpers.wait_until(lambda: len(self.lines()) == 2, 10)

        # a changed command replaces the old keepalive at the next start
        new_pid = next(iter(pids))
        rt2.stop()
        self.runtimes.remove(rt2)
        rt3 = self.runtime([{"distro": "Ubuntu-24.04", "command": ["/bin/sh", "-lc", "exec $HOME/run-node.sh"]}])
        pids3 = rt3.keepalive_pids()
        self.assertEqual(len(pids3), 1)
        self.assertNotIn(new_pid, pids3)
        helpers.wait_until(lambda: not util.pid_alive(new_pid) or proctree.reap_detached() is None and
                           not util.pid_alive(new_pid), 10)
        helpers.wait_until(lambda: len(self.lines()) == 3, 10)
        self.assertEqual(self.lines()[-1], "-d Ubuntu-24.04 --exec /bin/sh -lc exec $HOME/run-node.sh")

    def test_missing_wsl_exe_is_a_warning_not_a_failure(self) -> None:
        raw = helpers.make_node_config(data_dir=str(self.home / "node"), wsl={"keepalive": [{"distro": "X"}]})
        path = self.home / "node" / "config.json"
        util.write_json_atomic(path, raw, private=True)
        rt = node.NodeRuntime(config.load_node_config(str(path)), config_path=path, start_managers=True)
        with mock.patch.object(node, "IS_WINDOWS", True), mock.patch.object(wsl, "wsl_exe", return_value=None):
            rt.start()
        self.runtimes.append(rt)
        self.assertEqual(rt.keepalive.status(), [{"distro": "X", "pid": None, "running": False}])
        self.assertEqual(rt.keepalive_pids(), {})
        env = rt.call(helpers.identity(), "node.info", {})
        self.assertTrue(env["ok"], env)


class TestM3WslCli(unittest.TestCase):
    """``init node --wsl-keepalive`` on an existing config and the WSL lines of pause/panic."""

    def test_init_adds_entry_and_lines(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="hivem3wslcli-")).resolve()
        self.addCleanup(shutil.rmtree, str(base), True)
        env = m2util.base_env(HIVE_HOME=str(base / "h"), HOME=str(base), PYTHONPATH=str(m2util.ROOT))
        if not POSIX:  # pragma: no cover - Windows only
            env["USERPROFILE"] = str(base)
        r = m2util.run_cli(["init", "node", "--yes", "--name", "desk", "--listen", "127.0.0.1:0", "--proxy", "off",
                            "--mirror", "none"], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        cfg_path = base / "h" / "node" / "config.json"
        tok = (base / "h" / "tokens" / "this-pc.token").read_text(encoding="utf-8")
        r = m2util.run_cli(["init", "node", "--wsl-keepalive", "Ubuntu-24.04", "--no-restart"], env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("wsl.keepalive", r.stdout)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["wsl"]["keepalive"], [{"distro": "Ubuntu-24.04", "command": None}])
        self.assertEqual((base / "h" / "tokens" / "this-pc.token").read_text(encoding="utf-8"), tok)
        # adding the same distro again does not duplicate it
        m2util.run_cli(["init", "node", "--wsl-keepalive", "Ubuntu-24.04", "--no-restart"], env=env)
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
        self.assertEqual(len(raw["wsl"]["keepalive"]), 1, raw["wsl"])
        from hive import cli
        cfg = config.load_node_config(str(cfg_path))
        with mock.patch.object(cli.os, "name", "nt"):
            lines = cli._wsl_lines(cfg)
        self.assertEqual(lines, ["this PC also runs a WSL node; stop it too: wsl -d Ubuntu-24.04 -- "
                                 "~/.venvs/hive/bin/hive node panic --yes (or the hive path you installed there)"])
        if POSIX:
            self.assertEqual(cli._wsl_lines(cfg), [])


if __name__ == "__main__":
    unittest.main()
