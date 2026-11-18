"""WP4: install.py (systemd / launchd / schtasks / watchdog, restart sequence, launcher check) and
deploy/runpod/start-hive.sh (design §13.1, §20.1, §20.4; §21.2 row "install/doctor")."""
from __future__ import annotations

import configparser
import hashlib
import json
import os
import plistlib
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest import mock

from hive import config, dist, install, util
from hive.errors import HiveError
from tests import helpers

POSIX = os.name != "nt"
ROOT = Path(__file__).resolve().parents[1]
START_HIVE = ROOT / "deploy" / "runpod" / "start-hive.sh"
NS = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}


class InstallCase(unittest.TestCase):
    """Temp HIVE_HOME with a node config, temp HOME for unit/plist locations; the launcher check is patched."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="hiveinst-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.home = self.base / "home"
        self.home.mkdir()
        self.hive_home = self.base / "hh"
        env = mock.patch.dict(os.environ, {"HIVE_HOME": str(self.hive_home), "HOME": str(self.home),
                                           "XDG_CONFIG_HOME": str(self.home / ".config")})
        env.start()
        self.addCleanup(env.stop)
        self.cfg, self.tokens = config.init_node(consent_by="tester")
        self.config_path = config.default_paths()["node_config"]
        self.data = Path(self.cfg.data_dir)
        chk = mock.patch.object(install, "check_launcher", lambda data_dir: None)
        chk.start()
        self.addCleanup(chk.stop)
        ex = mock.patch.object(install, "_execute", side_effect=AssertionError("dry run must not execute"))
        ex.start()
        self.addCleanup(ex.stop)

    def ctx(self):
        return install._context("node", self.config_path)


class TestRenderers(InstallCase):
    def test_systemd_unit(self):
        unit = install.render_systemd(self.ctx())
        cp = configparser.ConfigParser(strict=False, interpolation=None)
        cp.optionxform = str
        cp.read_string(unit)
        svc = cp["Service"]
        self.assertEqual(svc["Restart"], "on-failure")                        # D55
        self.assertEqual(svc["KillMode"], "process")
        self.assertEqual(svc["WorkingDirectory"], str(self.data))            # D56
        self.assertEqual(shlex.split(svc["ExecStart"]), util.launcher_argv("node", self.config_path))
        self.assertEqual(cp["Install"]["WantedBy"], "default.target")
        env = self._env_lines(unit)
        self.assertEqual(env["HIVE_HOME"], str(self.hive_home))
        self.assertEqual(env["PYTHONUNBUFFERED"], "1")
        self.assertTrue(env["PATH"].endswith("/usr/local/bin:/usr/bin:/bin") or "/usr/bin" in env["PATH"])

    @staticmethod
    def _env_lines(unit: str) -> dict:
        out = {}
        for line in unit.splitlines():
            if line.startswith("Environment="):
                (word,) = shlex.split(line)                    # systemd quoting: one word per line
                k, v = word[len("Environment="):].split("=", 1)
                out[k] = v.replace("%%", "%")
        return out

    def test_systemd_path_with_space_and_percent(self):
        weird = "/mnt/c/Program Files/NVIDIA Corporation/x:/opt/100%/bin:/usr/local/bin:/usr/bin:/bin"
        with mock.patch.dict(os.environ, {"PATH": weird}):
            unit = install.render_systemd(self.ctx())
        lines = [ln for ln in unit.splitlines() if ln.startswith('Environment="PATH=')]
        self.assertEqual(len(lines), 1)
        self.assertIn("100%%", lines[0])
        self.assertEqual(self._env_lines(unit)["PATH"], weird)

    def test_systemd_no_capture_path(self):
        unit = install.render_systemd(self.ctx(), capture_path=False)
        self.assertNotIn("PATH=", unit)

    def test_pyz_launcher(self):
        pyz = self.hive_home / "hive.pyz"
        pyz.write_bytes(b"#!/usr/bin/env python3\n")
        with mock.patch.object(util, "_zipapp_path", return_value=pyz):
            unit = install.render_systemd(self.ctx())
            script = install.render_watchdog(self.ctx())
        argv = shlex.split([ln for ln in unit.splitlines() if ln.startswith("ExecStart=")][0][len("ExecStart="):])
        self.assertEqual(argv[1:], [str(pyz), "node", "run", "--config", str(self.config_path)])
        self.assertNotIn("-m", argv)
        self.assertIn(f"'{pyz}' '_update'", script)

    def test_plist(self):
        d = plistlib.loads(install.render_plist(self.ctx()))
        self.assertEqual(d["Label"], "ai.hive.node")
        self.assertEqual(d["KeepAlive"], {"SuccessfulExit": False})                       # D55
        self.assertEqual(d["WorkingDirectory"], str(self.data))                            # D56
        self.assertEqual(d["ProgramArguments"], util.launcher_argv("node", self.config_path))
        self.assertTrue(d["RunAtLoad"])
        self.assertEqual(d["ProcessType"], "Interactive")
        self.assertEqual(d["EnvironmentVariables"]["HIVE_HOME"], str(self.hive_home))
        self.assertTrue(d["EnvironmentVariables"]["PATH"].startswith("/opt/homebrew/bin:/usr/local/bin"))
        self.assertTrue(d["StandardOutPath"].startswith(str(self.data)))

    def test_task_xml(self):
        text = install.render_task_xml(self.ctx(), user="DESK\\alice")
        root = ET.fromstring(text.encode("utf-16"))
        self.assertEqual(root.find("t:Settings/t:Priority", NS).text, "5")
        self.assertEqual(root.find("t:Settings/t:ExecutionTimeLimit", NS).text, "PT0S")
        self.assertEqual(root.find("t:Settings/t:RestartOnFailure/t:Count", NS).text, "999")
        self.assertEqual(root.find("t:Settings/t:MultipleInstancesPolicy", NS).text, "IgnoreNew")
        self.assertEqual(root.find("t:Settings/t:DisallowStartIfOnBatteries", NS).text, "false")
        self.assertEqual(root.find("t:Triggers/t:LogonTrigger/t:UserId", NS).text, "DESK\\alice")
        pr = root.find("t:Principals/t:Principal", NS)
        self.assertEqual((pr.find("t:LogonType", NS).text, pr.find("t:RunLevel", NS).text),
                         ("InteractiveToken", "LeastPrivilege"))
        ex = root.find("t:Actions/t:Exec", NS)
        argv = util.launcher_argv("node", self.config_path, windowless=True, supervise=True)
        self.assertEqual(ex.find("t:Command", NS).text, argv[0])
        self.assertTrue(ex.find("t:Arguments", NS).text.endswith("--supervise"))
        self.assertEqual(ex.find("t:WorkingDirectory", NS).text, str(self.data))

    def test_watchdog_script(self):
        s = install.render_watchdog(self.ctx())
        self.assertIn(f"export HIVE_HOME='{self.hive_home}'", s)
        self.assertIn("export HIVE_WATCHDOG=1", s)
        self.assertIn("export PATH=", s)
        self.assertIn(f"cd '{self.data}'", s)
        loop = s[s.index("while true; do"):]
        self.assertIn("'_update'", loop)                                   # every round
        self.assertIn("'node' 'run' '--config'", loop)
        self.assertIn('[ "$?" -eq 0 ] && break', loop)
        self.assertNotIn("PATH=", install.render_watchdog(self.ctx(), capture_path=False))

    def test_templates_match(self):
        tmpl = (ROOT / "deploy" / "systemd" / "hive-node.service.tmpl").read_text()
        for key in ("Restart=on-failure", "KillMode=process", "WorkingDirectory={data_dir}", "TimeoutStopSec=60"):
            self.assertIn(key, tmpl)
        txml = (ROOT / "deploy" / "windows" / "task.xml.tmpl").read_text(encoding="utf-8")
        ET.fromstring(txml.replace('encoding="UTF-16"', ""))
        self.assertIn("<Priority>5</Priority>", txml)
        self.assertIn("SuccessfulExit", (ROOT / "deploy" / "launchd" / "ai.hive.node.plist.tmpl").read_text())


class TestDryRun(InstallCase):
    def test_every_method(self):
        for method in ("systemd", "launchd", "schtasks", "watchdog"):
            with self.subTest(method=method):
                res = install.install("node", self.config_path, method=method, dry_run=True)
                self.assertEqual(res.method, method)
                self.assertTrue(res.files and res.commands)
                self.assertTrue(res.notes[0].startswith("dry run"))
                for f in res.files:
                    self.assertFalse(Path(f).exists(), f)
        res = install.install("node", self.config_path, method="systemd", dry_run=True)
        self.assertEqual(res.commands[-1][-3:], ["enable", "--now", "hive-node.service"])
        self.assertIn("--user", res.commands[-1])
        res = install.install("node", self.config_path, method="schtasks", dry_run=True)
        self.assertEqual(res.commands[0][1:4], ["/Create", "/TN", "Hive Node"])
        auto = install.install("node", self.config_path, dry_run=True)
        self.assertIn(auto.method, install.METHODS)

    def test_system_needs_sudo_user(self):
        env = {k: v for k, v in os.environ.items() if k != "SUDO_USER"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(HiveError) as cm:
                install.install("node", self.config_path, method="systemd", system=True, dry_run=True)
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("SUDO_USER", cm.exception.message)
        with mock.patch.dict(os.environ, {"SUDO_USER": "alice"}):
            res = install.install("node", self.config_path, method="systemd", system=True, dry_run=True)
            unit = install.render_systemd(self.ctx(), system=True, user="alice")
        self.assertEqual(res.files, [str(install._SYSTEMD_SYSTEM_DIR / "hive-node.service")])
        self.assertNotIn("--user", res.commands[-1])
        self.assertIn("User=alice", unit)
        self.assertIn("WantedBy=multi-user.target", unit)
        with self.assertRaises(HiveError):
            install.install("node", self.config_path, method="launchd", system=True, dry_run=True)

    def test_unknown_method_and_kind(self):
        with self.assertRaises(HiveError):
            install.install("node", self.config_path, method="cron", dry_run=True)
        with self.assertRaises(HiveError):
            install.install("relay", self.config_path, dry_run=True)

    def test_restart_dry_run_plan(self):
        res = install.restart("node", dry_run=True)
        self.assertEqual(res.method, "foreground")
        self.assertIn("hive node run", res.notes[-1])


class TestLauncherCheck(unittest.TestCase):
    """v7: install() refuses when ``self_command() + ["version"]`` fails from the data dir without PYTHONPATH."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivelch-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.d), True)

    def test_uninstalled_checkout_refused(self):
        with mock.patch.object(util, "self_command", return_value=[sys.executable, "-m", "hive_not_installed_x"]):
            with self.assertRaises(HiveError) as cm:
                install.check_launcher(self.d)
        self.assertEqual(cm.exception.code, "unsupported")
        self.assertIn("pip install hive-agent", cm.exception.hint)

    def test_pythonpath_is_not_used(self):
        with mock.patch.object(util, "self_command", return_value=[sys.executable, "-c",
                                                                    "import os,sys; print('hive 0' if "
                                                                    "os.environ.get('PYTHONPATH') else 'x'); "
                                                                    "sys.exit(0 if 'PYTHONPATH' not in os.environ "
                                                                    "else 3)"]), \
                mock.patch.dict(os.environ, {"PYTHONPATH": str(ROOT)}):
            with self.assertRaises(HiveError):          # exit 0 but no version printed
                install.check_launcher(self.d)

    def test_pyz_proceeds(self):
        try:
            data = dist.build_pyz()
        except HiveError as e:
            if e.code == "unsupported":
                self.skipTest("dist.build_pyz is the P0 stub (WP5b)")
            raise
        pyz = self.d / "hive.pyz"
        pyz.write_bytes(data)
        with mock.patch.object(util, "_zipapp_path", return_value=pyz):
            self.assertEqual(util.self_command(), [sys.executable, str(pyz)])
            install.check_launcher(self.d)

    def test_editable_install_proceeds(self):
        venv = self.d / "venv"
        try:
            subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=120,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (subprocess.SubprocessError, OSError):
            self.skipTest("venv not available")
        py = venv / ("Scripts/python.exe" if util.IS_WINDOWS else "bin/python")
        out = subprocess.run([str(py), "-c", "import site; print(site.getsitepackages()[0])"], capture_output=True,
                             text=True, timeout=60)
        site = Path(out.stdout.strip())
        site.mkdir(parents=True, exist_ok=True)
        (site / "hive_editable.pth").write_text(str(ROOT) + "\n")       # what `pip install -e` leaves behind
        with mock.patch.object(util, "self_command", return_value=[str(py), "-m", "hive"]):
            install.check_launcher(self.d)


@unittest.skipUnless(POSIX, "watchdog scripts are POSIX sh")
class TestWatchdogLoop(InstallCase):
    def test_relaunch_after_75_and_stop_after_0(self):
        fake = self.base / "fakehive.py"
        log = self.base / "calls.log"
        fake.write_text(
            "import os, sys\n"
            f"log = {str(log)!r}\n"
            "args = sys.argv[1:]\n"
            "open(log, 'a').write(' '.join(args[:2]) + ' ' + os.environ.get('HIVE_WATCHDOG', '') + ' '"
            " + os.environ.get('HIVE_HOME', '') + '\\n')\n"
            "if args[:2] == ['node', 'run']:\n"
            "    runs = sum(1 for l in open(log) if l.startswith('node run'))\n"
            "    sys.exit(75 if runs == 1 else 0)\n")
        with mock.patch.object(util, "self_command", return_value=[sys.executable, str(fake)]):
            script = install.render_watchdog(self.ctx())
        path = self.base / "run-node.sh"
        path.write_text(script)
        (self.data / "logs").mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        r = subprocess.run(["/bin/sh", str(path)], timeout=60, cwd=str(self.base))
        self.assertEqual(r.returncode, 0)
        calls = log.read_text().splitlines()
        self.assertEqual([c.split()[0:2] for c in calls],
                         [["_update", "1"], ["node", "run"], ["_update", "1"], ["node", "run"]])
        self.assertTrue(all(c.endswith(str(self.hive_home)) for c in calls))
        self.assertGreaterEqual(time.monotonic() - t0, 4.5)          # sleep 5 between rounds


class Seams:
    """Scripted holder/pid/clock state for restart() (no processes, no service managers)."""

    def __init__(self, test, *, method, holders, alive_until=None, launcher_alive=False, watchdog_alive=False):
        self.t = 0.0
        self.holders = list(holders)        # [(time, holder-or-None)]: holder value from that time on
        self.alive_until = alive_until      # old pid alive until this time
        self.commands = []
        self.controls = []
        ctx_method = (method, False)
        patches = [
            mock.patch.object(install, "_installed", return_value=ctx_method),
            mock.patch.object(install, "_holder", side_effect=self.holder),
            mock.patch.object(install, "_pid_alive", side_effect=lambda pid, ct: self.alive_until is not None
                              and self.t < self.alive_until),
            mock.patch.object(install, "_execute", side_effect=self.execute),
            mock.patch.object(install, "_sleep", side_effect=self.sleep),
            mock.patch.object(install, "_clock", side_effect=lambda: self.t),
            mock.patch.object(install, "_write_control", side_effect=lambda ctx, cmd: self.controls.append(cmd)),
            mock.patch.object(install, "_launcher_alive", return_value=launcher_alive),
            mock.patch.object(install, "_watchdog_alive", return_value=watchdog_alive),
            mock.patch.object(install, "_start_watchdog", side_effect=lambda ctx: self.commands.append(["watchdog"])),
        ]
        for p in patches:
            p.start()
            test.addCleanup(p.stop)
        self.on_command = {}

    def holder(self, ctx):
        cur = None
        for t, h in self.holders:
            if self.t >= t:
                cur = h
        return cur

    def execute(self, argv, timeout=60.0):
        self.commands.append(list(argv))
        key = argv[-1] if argv[-1] != "Hive Node" else argv[1]
        cb = self.on_command.get(key)
        if cb:
            cb()
        return 0, ""

    def sleep(self, s):
        self.t += s


OLD = {"pid": 100, "ctime": 1.0, "note": "node"}
NEW = {"pid": 200, "ctime": 2.0, "note": "node"}


class TestRestartSequence(InstallCase):
    def setUp(self):
        super().setUp()
        install._execute.side_effect = None

    def test_systemd_relaunches_by_itself(self):
        s = Seams(self, method="systemd", holders=[(0, OLD), (2, None), (7, NEW)], alive_until=2)
        res = install.restart("node")
        self.assertEqual(s.controls, ["restart"])
        self.assertEqual(s.commands, [])                                  # no double start
        self.assertEqual(res.method, "systemd")

    def test_systemd_start_command_once(self):
        s = Seams(self, method="systemd", holders=[(0, OLD), (2, None)], alive_until=2)
        s.on_command["hive-node.service"] = lambda: s.holders.append((s.t + 1, NEW))
        install.restart("node")
        self.assertEqual(len(s.commands), 1)
        self.assertEqual(s.commands[0][-2:], ["start", "hive-node.service"])
        self.assertGreaterEqual(s.t, 2 + install.HOLDER_WAIT)

    def test_not_running_installed_starts_at_once(self):
        s = Seams(self, method="launchd", holders=[(0, None)])
        s.on_command["gui/" + str(os.getuid() if hasattr(os, "getuid") else 0) + "/ai.hive.node"] = \
            lambda: s.holders.append((s.t + 1, NEW))
        install.restart("node")
        self.assertEqual(s.commands[0][-2], "kickstart")
        self.assertLess(s.t, 5)                                            # no 20 s wait first

    def test_watchdog_alive_no_start(self):
        s = Seams(self, method="watchdog", holders=[(0, OLD), (2, None), (30, NEW)], alive_until=2,
                  watchdog_alive=True)
        install.restart("node")
        self.assertEqual(s.commands, [])

    def test_watchdog_dead_relaunched(self):
        s = Seams(self, method="watchdog", holders=[(0, OLD), (2, None)], alive_until=2)
        orig = install._start_watchdog.side_effect
        install._start_watchdog.side_effect = lambda ctx: (orig(ctx), s.holders.append((s.t + 1, NEW)))
        install.restart("node")
        self.assertEqual(s.commands, [["watchdog"]])

    def test_windows_launcher_relaunches(self):
        s = Seams(self, method="schtasks", holders=[(0, OLD), (2, None), (3, NEW)], alive_until=2,
                  launcher_alive=True)
        install.restart("node")
        self.assertEqual(s.commands, [])                                   # no /Run

    def test_windows_run_then_end_run_retry(self):
        s = Seams(self, method="schtasks", holders=[(0, OLD), (2, None)], alive_until=2)
        runs = []

        def on_run():
            runs.append(s.t)
            if len(runs) == 2:                                             # the retry's /Run works
                s.holders.append((s.t + 1, NEW))
        s.on_command["/Run"] = on_run
        install.restart("node")
        verbs = [c[1] for c in s.commands]
        self.assertEqual(verbs, ["/Run", "/End", "/Run"])

    def test_windows_launcher_alive_skips_run_but_retries(self):
        s = Seams(self, method="schtasks", holders=[(0, OLD), (2, None)], alive_until=2, launcher_alive=True)
        with self.assertRaises(HiveError) as cm:
            install.restart("node")
        self.assertEqual([c[1] for c in s.commands], ["/End", "/Run"])
        self.assertEqual(cm.exception.code, "unavailable")

    def test_foreground_stops_and_says_so(self):
        s = Seams(self, method=None, holders=[(0, OLD), (2, None)], alive_until=2)
        with self.assertRaises(HiveError) as cm:
            install.restart("node")
        self.assertEqual(s.controls, ["restart"])
        self.assertEqual(cm.exception.code, "unavailable")
        self.assertIn("hive node run", cm.exception.hint)

    def test_old_holder_never_exits(self):
        Seams(self, method="systemd", holders=[(0, OLD)], alive_until=10 ** 6)
        with self.assertRaises(HiveError) as cm:
            install.restart("node")
        self.assertEqual(cm.exception.code, "timeout")

    def test_status_and_uninstall_none(self):
        st = install.status("node")
        self.assertEqual((st["installed"], st["method"], st["running"]), (False, None, False))
        res = install.uninstall("node", dry_run=True)
        self.assertEqual(res.method, "none")


@unittest.skipUnless(POSIX and shutil.which("sh"), "start-hive.sh is POSIX sh")
class TestStartHive(unittest.TestCase):
    """deploy/runpod/start-hive.sh against a stub hub (§20.4 bootstrap behaviour)."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="hivepod-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.ws = self.base / "workspace"
        self.ws.mkdir()
        self.log = self.base / "pyz.log"
        self.body = ("import os, sys\n"
                     f"log = {str(self.log)!r}\n"
                     "open(log, 'a').write(' '.join(sys.argv[1:3]) + '|' + os.environ.get('HIVE_WATCHDOG', '') + '|'"
                     " + os.environ.get('HIVE_HOME', '') + '|' + os.getcwd() + '\\n')\n"
                     "if sys.argv[1:3] == ['node', 'run']:\n"
                     "    n = sum(1 for l in open(log) if l.startswith('node run'))\n"
                     "    sys.exit(0 if n >= int(os.environ.get('FAKE_RUNS', '1')) else 75)\n").encode()
        self.stub = helpers.StubHTTPServer().start()
        self.addCleanup(self.stub.stop)

    def serve(self, sha=None):
        self.stub.add("GET", "/v1/dist/hive.pyz", 200, self.body,
                      headers={"X-Hive-Sha256": sha or hashlib.sha256(self.body).hexdigest(),
                               "Content-Type": "application/octet-stream"})

    def run_script(self, **env):
        e = {k: v for k, v in os.environ.items() if not k.startswith("HIVE_") and k not in ("HTTPS_PROXY",
                                                                                          "https_proxy",
                                                                                          "HTTP_PROXY",
                                                                                          "http_proxy")}
        e.update(HIVE_POD_WORKSPACE=str(self.ws), HIVE_PYTHON=sys.executable,
                 HIVE_JOIN=dist.encode_join({"u": self.stub.url, "t": "hv_" + "t" * 40, "p": None, "n": "pod-a"}))
        e.update(env)
        return subprocess.run(["sh", str(START_HIVE)], env=e, cwd=str(self.base), capture_output=True, text=True,
                               timeout=60)

    def test_bootstrap_and_loop(self):
        self.serve()
        r = self.run_script(FAKE_RUNS="2")
        self.assertEqual(r.returncode, 0, r.stderr)
        hh = self.ws / ".hive"
        self.assertEqual((hh / "hive.pyz").read_bytes(), self.body)
        self.assertFalse((hh / "hive.pyz.new").exists())
        wrapper = (hh / "hive").read_text()
        self.assertIn(f"export HIVE_HOME='{hh}'", wrapper)
        self.assertIn(f"'{hh}/hive.pyz'", wrapper)
        self.assertTrue(os.access(hh / "hive", os.X_OK))
        calls = self.log.read_text().splitlines()
        self.assertEqual([c.split("|")[0] for c in calls], ["_update", "node run", "_update", "node run"])
        self.assertTrue(all(c.split("|")[1:3] == ["1", str(hh)] for c in calls))
        self.assertTrue(all(c.split("|")[3] == str(hh) for c in calls))
        req = [q for q in self.stub.requests if q["path"] == "/v1/dist/hive.pyz"]
        self.assertEqual(len(req), 1)
        self.assertNotIn("Authorization", req[0]["headers"])               # no token is sent
        # second start: the pyz exists, nothing is fetched
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len([q for q in self.stub.requests if q["path"] == "/v1/dist/hive.pyz"]), 1)

    def test_header_mismatch_writes_nothing(self):
        self.serve(sha="0" * 64)
        r = self.run_script()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("X-Hive-Sha256", r.stderr)
        hh = self.ws / ".hive"
        self.assertFalse((hh / "hive.pyz").exists())
        self.assertFalse((hh / "hive.pyz.new").exists())
        self.assertFalse(self.log.exists())

    def test_pinned_sha_mismatch(self):
        self.serve()
        r = self.run_script(HIVE_PYZ_SHA256="1" * 64)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("HIVE_PYZ_SHA256", r.stderr)
        self.assertFalse((self.ws / ".hive" / "hive.pyz").exists())
        r = self.run_script(HIVE_PYZ_SHA256=hashlib.sha256(self.body).hexdigest())
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_explicit_home_and_relay_url(self):
        self.serve()
        home = self.base / "elsewhere"
        r = self.run_script(HIVE_HOME=str(home), HIVE_JOIN="", HIVE_RELAY_URL=self.stub.url)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((home / "hive.pyz").exists())
        self.assertFalse((self.ws / ".hive").exists())


@unittest.skipUnless(POSIX, "watchdog method")
class TestPyzEndToEnd(unittest.TestCase):
    """A pyz install run by the generated run-node.sh with HOME != HIVE_HOME and hive not importable: the node
    keeps its data under HIVE_HOME, a killed node is relaunched, and a graceful stop ends the watchdog."""

    def test_watchdog_pyz(self):
        try:
            data = dist.build_pyz()
        except HiveError as e:
            if e.code == "unsupported":
                self.skipTest("dist.build_pyz is the P0 stub until WP5b")
            raise
        base = Path(tempfile.mkdtemp(prefix="hivee2e-")).resolve()
        self.addCleanup(shutil.rmtree, str(base), True)
        hh, home = base / "hh", base / "home"
        home.mkdir()
        port = helpers.free_port()
        env = {"HIVE_HOME": str(hh), "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
        with mock.patch.dict(os.environ, env):
            os.environ.pop("PYTHONPATH", None)
            cfg, toks = config.init_node(consent_by="tester", listen=f"127.0.0.1:{port}", proxy="off")
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                config.edit_config(config.default_paths()["node_config"], lambda r: r.update(allow_root=True),
                                   kind="node")
            pyz = hh / "hive.pyz"
            pyz.write_bytes(data)
            with mock.patch.object(util, "_zipapp_path", return_value=pyz):
                res = install.install("node", config.default_paths()["node_config"], method="watchdog")
            data_dir = Path(cfg.data_dir)
            self.addCleanup(self._cleanup, data_dir)
            lock = util.FileLock(data_dir / "node.lock")
            helpers.wait_until(lambda: lock.holder() is not None, 60)
            first = lock.holder()
            info = self._call(port, toks["owner"])
            self.assertEqual(info["data_dir"], str(data_dir))
            self.assertTrue(info["data_dir"].startswith(str(hh)))
            os.kill(first["pid"], signal.SIGKILL)
            helpers.wait_until(lambda: (lock.holder() or {}).get("pid") not in (None, first["pid"]), 60)
            st = install.status("node")
            self.assertEqual((st["method"], st["active"], st["running"]), ("watchdog", "running", True))
            res = install.uninstall("node")
            self.assertIn("stopping the running node", " ".join(res.notes))
            helpers.wait_until(lambda: lock.holder() is None, 30)
            self.assertFalse((data_dir / "run-node.sh").exists())

    @staticmethod
    def _call(port, token):
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/call", method="POST",
                                     data=json.dumps({"action": "node.info", "params": {}}).encode(),
                                     headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        end = time.monotonic() + 30
        while True:
            try:
                with opener.open(req, timeout=10) as r:
                    env = json.loads(r.read())
                    return env["result"]
            except OSError:
                if time.monotonic() > end:
                    raise
                time.sleep(0.3)

    @staticmethod
    def _cleanup(data_dir: Path):
        pidf = data_dir / "run" / "watchdog.pid"
        try:
            os.kill(int(pidf.read_text().strip()), signal.SIGKILL)
        except (OSError, ValueError):
            pass
        h = util.FileLock(data_dir / "node.lock").holder()
        if h:
            try:
                os.kill(h["pid"], signal.SIGKILL)
            except OSError:
                pass
        for p in list(install._children):
            try:
                p.wait(10)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        install._children.clear()


if __name__ == "__main__":
    unittest.main()
