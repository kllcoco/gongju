"""Regression tests for the WP4 review findings (doctor, sysinfo disks, proc.kill/proc.list, WSL keepalive, install,
start-hive.sh; design §5.4, §5.8, §9.2, §12.4, §13.1, §20.1, §20.4, §20.7)."""
from __future__ import annotations

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

from hive import catalog, config, doctor, install, procinfo, proctree, schema, sysinfo, util, wsl
from hive.actions import HANDLERS, load_all
from hive.audit import AuditLog
from hive.errors import HiveError
from tests import helpers
from tests.test_doctor import DoctorCase
from tests import test_install as ti
from tests.test_install import OLD, NEW, START_HIVE, InstallCase, Seams
from tests.test_system import make_tool_dir

POSIX = os.name != "nt"


# ---------------------------------------------------------------------------------------------------------------
# doctor

class TestDoctorWslMirrored(DoctorCase):
    """§12.4 v5: mirrored networking + listen.host "tailscale" shows the resolved tailnet IP."""

    def setUp(self):
        super().setUp()
        self.edit(lambda r: r["listen"].update(host="tailscale"))
        for p in (mock.patch.object(wsl, "is_wsl", return_value=True),
                  mock.patch.object(wsl, "wsl_info", return_value={"distro": "Ubuntu-24.04", "networking": "mirrored",
                                                                   "systemd": True}),
                  mock.patch.object(wsl, "interop_enabled", return_value=False)):
            p.start()
            self.addCleanup(p.stop)

    def test_tailnet_ip_shown(self):
        from hive import httpserver
        with mock.patch.object(httpserver, "tailscale_info",
                               return_value=("100.101.102.103", ["desk.tail1234.ts.net"], "")):
            res = self.run_doc()
        row = self.assertRow(res, "wsl", "ok", "listen tailscale -> 100.101.102.103")
        self.assertIn("New-NetFirewallHyperVRule", row["fix"])
        self.assertIn(f"-LocalPorts {self.cfg.listen.port}", row["fix"])

    def test_tailscale_unavailable(self):
        from hive import httpserver
        with mock.patch.object(httpserver, "tailscale_info",
                               side_effect=HiveError("unavailable", "tailscale is not running")):
            res = self.run_doc()
        self.assertRow(res, "wsl", "ok", "listen tailscale -> tailnet IP not found")


class TestDoctorSkippedServiceCodeDir(DoctorCase):
    """§12.4 policy v6: a service code dir equal to ~ or a root is reported (the node drops it silently)."""

    def write_services(self, defs):
        state = self.data / "state"
        state.mkdir(parents=True, exist_ok=True)
        util.write_json_atomic(state / "services.json", {"v": 1, "services": defs}, private=True)

    def test_home_cwd_warned(self):
        self.write_services({"web": {"name": "web", "argv": ["python3", "-m", "http.server"], "cwd": "~"}})
        res = self.run_doc()
        row = self.assertRow(res, "policy", "warn", "service code dir")
        self.assertIn(str(self.home), row["detail"])
        self.assertIn("cannot protect it from writes", row["detail"])

    def test_own_folder_not_warned(self):
        app = self.home / "apps" / "web"
        app.mkdir(parents=True)
        self.write_services({"web": {"name": "web", "argv": ["python3", "-m", "http.server"], "cwd": str(app)}})
        res = self.run_doc()
        self.assertFalse([r for r in res if "service code dir" in r["detail"]], res)

    def test_no_services(self):
        res = self.run_doc()
        self.assertFalse([r for r in res if "service code dir" in r["detail"]], res)


class TestIcaclsSpacedProfile(unittest.TestCase):
    """§12.4 files group: an open ACE on the first icacls line of a path with a space is found."""

    PATH = r"C:\Users\John Smith\.hive\node\secrets.json"

    def test_first_line_with_space(self):
        text = self.PATH + " Everyone:(R)\n" + " " * 44 + r"DESK\John Smith:(F)" + "\n"
        self.assertEqual(doctor.parse_icacls(text, self.PATH), ["Everyone"])
        self.assertEqual(doctor.parse_icacls(text), ["Everyone"])          # without the path too
        own = self.PATH + r" DESK\John Smith:(F)" + "\n" + " " * 44 + r"NT AUTHORITY\SYSTEM:(F)" + "\n"
        self.assertEqual(doctor.parse_icacls(own, self.PATH), [])
        self.assertEqual(doctor.parse_icacls(own), [])
        users = self.PATH + r" BUILTIN\Users:(I)(RX)" + "\n"
        self.assertEqual(doctor.parse_icacls(users, self.PATH), ["BUILTIN\\Users"])
        self.assertEqual(doctor.parse_icacls(users), ["BUILTIN\\Users"])

    def test_plain_path_unchanged(self):
        text = r"C:\Users\alice\.hive\node\secrets.json Everyone:(R)" + "\n"
        self.assertEqual(doctor.parse_icacls(text), ["Everyone"])


class TestWindowsFixBySid(unittest.TestCase):
    """§20.5 item 12: the printed icacls fix names the SID, never %USERNAME%."""

    def test_sid(self):
        sid = "S-1-5-21-1111111111-2222222222-3333333333-1001"
        fix = doctor.win_fix_perm(r"C:\Users\John Smith\.hive\node\secrets.json", False, sid)
        self.assertNotIn("%USERNAME%", fix)
        self.assertIn(f'"*{sid}:F"', fix)
        self.assertIn('"C:\\Users\\John Smith\\.hive\\node\\secrets.json"', fix)
        self.assertIn("/inheritance:r", fix)
        self.assertIn("(OI)(CI)F", doctor.win_fix_perm(r"C:\x", True, sid))
        self.assertNotIn("%USERNAME%", doctor.win_fix_perm(r"C:\x", False, None))


class TestDoctorXffWarning(DoctorCase):
    """§12.4 proxy group: 'without X-Forwarded-For' only where a `proxy:` client IP means a missing header."""

    def audit(self):
        AuditLog(self.data / "audit").record(kind="call", identity=None, ip="proxy:127.0.0.1", via="direct",
                                             node="n", action="node.info", params=None, ok=True, code=None, ms=1,
                                             request_id="r_1")

    def main_port_log(self):
        (self.data / "logs").mkdir(exist_ok=True)
        (self.data / "logs" / "node.log").write_text(
            "2026-11-18 WARNING hive.httpserver forwarded request on the main port from 127.0.0.1; point the proxy "
            "at listen.proxy_port\n")

    def test_main_port_forwarding_with_proxy_port(self):
        self.edit(lambda r: r["listen"].update(proxy_port=helpers.free_port()))
        self.main_port_log()
        self.audit()
        res = self.run_doc()
        self.assertRow(res, "listen", "warn", "point the proxy at listen.proxy_port")
        self.assertFalse([r for r in res if "without X-Forwarded-For" in r["detail"]], res)

    def test_proxy_port_without_xff(self):
        self.edit(lambda r: r["listen"].update(proxy_port=helpers.free_port()))
        self.audit()
        res = self.run_doc()
        self.assertRow(res, "proxy", "warn", "without X-Forwarded-For")
        self.assertFalse([r for r in res if "point the proxy" in (r["fix"] or "")], res)


class TestKeepaliveStateReader(unittest.TestCase):
    """§12.4 wsl group (Windows): keepalive state read offline from state/wsl-keepalive-<distro>.json."""

    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="hivekas-"))
        self.addCleanup(shutil.rmtree, str(self.state), True)

    def write(self, distro, rec):
        wsl.keepalive_state_path(self.state, distro).write_text(json.dumps(rec), encoding="utf-8")

    def test_running_dead_corrupt_missing(self):
        me = os.getpid()
        self.write("Ubuntu-24.04", {"distro": "Ubuntu-24.04", "pid": me, "ctime": util.process_create_time(me),
                                    "argv": ["wsl.exe"], "started_at": "2026-11-18T00:00:00.000Z"})
        rec = wsl.read_keepalive_state(self.state, "Ubuntu-24.04")
        self.assertTrue(rec["running"])
        self.assertEqual(rec["pid"], me)
        p = subprocess.Popen(helpers.py_cmd("pass"))
        p.wait()
        self.write("Debian", {"distro": "Debian", "pid": p.pid, "ctime": None})
        self.assertFalse(wsl.read_keepalive_state(self.state, "Debian")["running"])
        # a live pid whose create time differs is a reused pid: not running
        self.write("Old", {"distro": "Old", "pid": me, "ctime": 1.0})
        self.assertFalse(wsl.read_keepalive_state(self.state, "Old")["running"])
        wsl.keepalive_state_path(self.state, "Bad").write_text("{not json", encoding="utf-8")
        self.assertIsNone(wsl.read_keepalive_state(self.state, "Bad"))
        self.write("NoPid", {"distro": "NoPid", "pid": True})
        self.assertIsNone(wsl.read_keepalive_state(self.state, "NoPid"))
        self.assertIsNone(wsl.read_keepalive_state(self.state, "Missing"))

    def test_doctor_rows(self):
        base = Path(tempfile.mkdtemp(prefix="hivekad-"))
        self.addCleanup(shutil.rmtree, str(base), True)
        d = doctor._Doctor("node", False, False)
        d.node_cfg = mock.Mock(data_dir=str(base))
        state = base / "state"
        state.mkdir()
        self.state = state
        me = os.getpid()
        self.write("Ubuntu", {"distro": "Ubuntu", "pid": me, "ctime": util.process_create_time(me)})
        d._wsl_keepalives([{"distro": "Ubuntu"}, {"distro": "Debian"}, "junk"])
        self.assertEqual([(r["status"], r["detail"], r["fix"]) for r in d.rows],
                         [("ok", f"keepalive for Ubuntu running (pid {me})", None),
                          ("warn", "keepalive for Debian not running", "hive node restart")])


# ---------------------------------------------------------------------------------------------------------------
# sysinfo disks

class TestDockerFileMounts(unittest.TestCase):
    """§9.2 disks: Docker's file bind mounts (/etc/hosts …) and repeated binds of one device are not disks."""

    MOUNTS = ("overlay / overlay rw 0 0\n/dev/nvme0n1p1 /etc/hosts ext4 rw 0 0\n"
              "/dev/nvme0n1p1 /etc/hostname ext4 rw 0 0\n/dev/nvme0n1p1 /etc/resolv.conf ext4 rw 0 0\n"
              "/dev/nvme1n1 /workspace ext4 rw 0 0\n/dev/nvme1n1 /root/autodl-tmp ext4 rw 0 0\n"
              "/dev/nvme2n1 /data ext4 rw 0 0\n")

    def test_parse_mounts_isdir(self):
        files = {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}
        self.assertEqual(sysinfo.parse_mounts(self.MOUNTS, isdir=lambda m: m not in files),
                         [("/", "overlay"), ("/workspace", "ext4"), ("/root/autodl-tmp", "ext4"), ("/data", "ext4")])
        self.assertIn(("/etc/hosts", "ext4"), sysinfo.parse_mounts(self.MOUNTS))   # no check: unchanged

    @unittest.skipUnless(sys.platform.startswith("linux"), "the /proc/mounts path")
    def test_disks(self):
        files = {"/etc/hosts", "/etc/hostname", "/etc/resolv.conf"}
        sizes = {"/": (100, 50), "/etc/hosts": (500, 100), "/etc/hostname": (500, 100), "/etc/resolv.conf": (500, 100),
                 "/workspace": (900, 300), "/root/autodl-tmp": (900, 300), "/data": (900, 300)}

        def fake_statvfs(path, timeout=2.0, *, dir_only=False):
            if dir_only and path in files:
                return None
            return sizes.get(path)
        with mock.patch.object(sysinfo, "_read", return_value=self.MOUNTS), \
                mock.patch.object(sysinfo, "_statvfs", side_effect=fake_statvfs), \
                mock.patch.object(wsl, "is_wsl", return_value=False):
            rows = sysinfo.disks()
        # /data has the same totals as /workspace but is another device: kept
        self.assertEqual([r["mount"] for r in rows], ["/", "/workspace", "/data"])

    @unittest.skipUnless(POSIX, "os.statvfs")
    def test_statvfs_dir_only(self):
        f = Path(tempfile.mkdtemp(prefix="hivesv-"))
        self.addCleanup(shutil.rmtree, str(f), True)
        (f / "hosts").write_text("x")
        self.assertIsNone(sysinfo._statvfs(str(f / "hosts"), dir_only=True))
        self.assertIsNotNone(sysinfo._statvfs(str(f / "hosts")))
        self.assertIsNotNone(sysinfo._statvfs(str(f), dir_only=True))


# ---------------------------------------------------------------------------------------------------------------
# proc.kill / proc.list

class TestProcKillTreeGuard(unittest.TestCase):
    """§5.4: a tree kill checks every process it would signal; a protected descendant refuses the call."""

    @classmethod
    def setUpClass(cls):
        load_all()

    def setUp(self):
        self.node = helpers.make_runtime("standard", start_managers=False)
        self.addCleanup(self.node.stop)

    def call(self, pid, **extra):
        from hive.auth import Identity
        ident = Identity(name="op", kind="agent", classes=frozenset({"read", "write", "exec", "net", "persist"}),
                         token_sha256="sha256:" + "0" * 64)
        return self.node.call(ident, "proc.kill", dict({"pid": pid, "grace": 1}, **extra))

    def spawn_parent(self, child_args, new_session=False):
        # the child's extra argv travels in the environment, so the parent's own command line stays plain
        code = ("import json, os, subprocess, sys, time\n"
                "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']"
                " + json.loads(os.environ['HIVE_TEST_CHILD_ARGS']))\n"
                "time.sleep(60)\n")
        p = subprocess.Popen([sys.executable, "-c", code], start_new_session=new_session,
                             env=dict(os.environ, HIVE_TEST_CHILD_ARGS=json.dumps(child_args)))
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())

        def child():
            for d in proctree.descendants(p.pid):
                info = procinfo.process_info(d) or {}
                if "time.sleep(60)" in " ".join(info.get("cmdline") or []):
                    return d
            return None
        helpers.wait_until(child, 10)
        c = child()
        self.addCleanup(lambda: util.pid_alive(c) and os.kill(c, signal.SIGKILL))
        return p, c

    @unittest.skipUnless(POSIX, "signals")
    def test_hive_child_under_plain_parent(self):
        p, c = self.spawn_parent(["-m", "hive", "hub", "run"])
        env = self.call(c)
        self.assertEqual(env["error"]["details"].get("rule"), "kill_protect")
        env = self.call(p.pid)
        self.assertFalse(env["ok"], env)
        self.assertEqual(env["error"]["code"], "policy_denied")
        self.assertEqual(env["error"]["details"].get("rule"), "kill_protect")
        self.assertEqual(env["error"]["details"].get("descendant"), c, env)
        self.assertIn("tree:false", env["error"]["hint"])
        self.assertTrue(util.pid_alive(p.pid) and util.pid_alive(c))
        # tree:false stops only the parent; the protected child survives
        env = self.call(p.pid, tree=False, force=True)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["result"]["killed"], [p.pid])
        self.assertTrue(util.pid_alive(c))

    @unittest.skipUnless(POSIX, "signals")
    def test_managed_child_and_group_member(self):
        p, c = self.spawn_parent([], new_session=True)
        with mock.patch.object(type(self.node), "managed_pids", lambda s: {c: "job:j_abcdef"}):
            env = self.call(p.pid)
        self.assertEqual(env["error"]["details"].get("rule"), "protected_process")
        self.assertIn("job_stop/service_control", env["error"]["hint"])
        self.assertTrue(util.pid_alive(c))
        # a group member that is not a descendant (reparented) is checked too: killpg would reach it
        with mock.patch.object(proctree, "descendants", return_value=[]), \
                mock.patch.object(type(self.node), "managed_pids", lambda s: {c: "job:j_abcdef"}):
            env = self.call(p.pid)
        self.assertEqual(env["error"]["details"].get("rule"), "protected_process")
        # nothing protected: the whole tree goes
        env = self.call(p.pid)
        self.assertTrue(env["ok"], env)
        self.assertEqual(sorted(env["result"]["killed"]), sorted([p.pid, c]))


class TestProcListFilterRedacted(unittest.TestCase):
    """§5.8: proc.list `filter` matches the redacted command line, so total/shown reveal no secret."""

    @classmethod
    def setUpClass(cls):
        load_all()

    SECRET = "sk-ABCD1234EFGH5678IJKL9012"

    def run_list(self, flt, cmdline=False):
        rows = [{"pid": 4242, "ppid": 1, "name": "python3", "user": "u", "uid": 1, "cpu_pct": None, "mem_bytes": 1,
                 "create_time": 1700000000.0,
                 "cmdline": ["python3", "serve.py", "--api-key", self.SECRET, "HF_TOKEN=hf_" + "x" * 34]
                 + ["--pad"] * 80 + ["--tail-marker"]}]
        with mock.patch.object(procinfo, "list_processes", return_value=(rows, "proc")):
            res = HANDLERS["proc.list"](helpers.make_ctx("proc.list"),
                                        schema.validate(catalog.get("proc.list").params,
                                                        {"filter": flt, "cmdline": cmdline}))
        return res["total"]

    def test_no_oracle(self):
        self.assertEqual(self.run_list("--api-key"), 1)
        self.assertEqual(self.run_list("serve.py"), 1)
        self.assertEqual(self.run_list("--tail-marker"), 1)       # past the 300-char cut of `cmd`
        for guess in ("--api-key sk-abcd1", "--api-key sk-abcd2", "sk-ABCD", "1234EFGH", "hf_xxxx"):
            self.assertEqual(self.run_list(guess), 0, guess)
            self.assertEqual(self.run_list(guess, cmdline=True), 0, guess)


class TestProtectedPidsWindowsWalk(unittest.TestCase):
    """protected_pids walks the ancestors over one ppid snapshot where one is available (Windows), not one
    process_info (PowerShell CIM) per ancestor."""

    def test_walk_over_map(self):
        from hive.actions import system as sysact
        pmap = {500: 400, 400: 300, 300: 4, 4: 0, 999: 500}
        self.assertEqual(sysact._ancestors(500, pmap), [500, 400, 300, 4])
        self.assertEqual(sysact._ancestors(1, {1: 1}), [1])
        ctx = helpers.make_ctx("proc.kill")
        ctx.rt.self_pid = 500
        with mock.patch.object(sysact, "_ppid_map", return_value=pmap), \
                mock.patch.object(procinfo, "process_info", side_effect=AssertionError("no per-pid probe")):
            prot = sysact.protected_pids(ctx)
        self.assertEqual({k for k, v in prot.items() if v == "hive node"}, {500, 400, 300, 4})

    def test_posix_has_no_map(self):
        from hive.actions import system as sysact
        if util.IS_WINDOWS:  # pragma: no cover
            self.skipTest("Windows builds a map")
        self.assertIsNone(sysact._ppid_map())


# ---------------------------------------------------------------------------------------------------------------
# WSL keepalive

@unittest.skipUnless(POSIX, "fake wsl.exe is a POSIX script here")
class TestKeepaliveCommandChange(unittest.TestCase):
    """§20.7: a recorded keepalive is adopted only when it runs the entry's current command."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="hivekac-"))
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.state = self.base / "state"
        self.log = self.base / "argv.log"
        self.tool = make_tool_dir("wsl.exe", "import os, sys, time\n"
                                             f"open({str(self.log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                                             "time.sleep(60)\n")
        self.addCleanup(shutil.rmtree, str(self.tool), True)
        self.kas = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for ka in self.kas:
            for pid in ka.pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        proctree.reap_detached()

    def make(self):
        ka = wsl.Keepalive(self.state, wsl_exe=str(self.tool / "wsl.exe"), clock=lambda: 1000.0)
        self.kas.append(ka)
        return ka

    def test_changed_command_replaces_old(self):
        ka = self.make()
        ka.start([{"distro": "Ubuntu", "command": None}])
        old = ka.status()[0]["pid"]
        old_ct = util.process_create_time(old)
        helpers.wait_until(lambda: self.log.exists() and self.log.read_text().strip())
        new_cmd = ["/bin/sh", "-lc", "exec $HOME/.hive/node/run-node.sh"]
        ka2 = self.make()
        ka2.start([{"distro": "Ubuntu", "command": new_cmd}])
        st = ka2._entries["Ubuntu"]
        self.assertFalse(st["adopted"])
        self.assertNotEqual(st["pid"], old)
        proctree.reap_detached()
        helpers.wait_until(lambda: not util.pid_alive(old, old_ct) or proctree.reap_detached() or False, 10)
        self.assertFalse(util.pid_alive(old, old_ct))
        rec = json.loads(wsl.keepalive_state_path(self.state, "Ubuntu").read_text())
        self.assertEqual(rec["argv"][-3:], new_cmd)
        helpers.wait_until(lambda: len(self.log.read_text().splitlines()) == 2)
        self.assertEqual(self.log.read_text().splitlines()[1], "-d Ubuntu --exec " + " ".join(new_cmd))
        # same command again: adopted, nothing new started
        ka3 = self.make()
        ka3.start([{"distro": "Ubuntu", "command": new_cmd}])
        self.assertTrue(ka3._entries["Ubuntu"]["adopted"])
        self.assertEqual(ka3._entries["Ubuntu"]["pid"], st["pid"])


# ---------------------------------------------------------------------------------------------------------------
# install

class TestSystemInstallForSudoUser(InstallCase):
    """§20.1: `--system` under sudo renders the SUDO_USER's HIVE_HOME, not root's."""

    def test_dry_run_uses_the_users_home(self):
        user_home = self.base / "alice"
        user_home.mkdir()
        with mock.patch.dict(os.environ, {"HIVE_HOME": str(user_home / ".hive")}):
            ucfg, _t = config.init_node(consent_by="tester")
        root_home = self.base / "roothome"
        root_home.mkdir()
        env = {k: v for k, v in os.environ.items() if k != "HIVE_HOME"}
        env.update(HOME=str(root_home), SUDO_USER="alice")
        texts = []
        orig = install.render_systemd

        def capture(*a, **kw):
            t = orig(*a, **kw)
            texts.append(t)
            return t
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(install, "_user_home", return_value=user_home), \
                mock.patch.object(install, "render_systemd", side_effect=capture):
            install.install("node", None, method="systemd", system=True, dry_run=True)
            texts_cfg = len(texts)
            # an explicit --config under the user's HIVE_HOME gives the same home
            install.install("node", user_home / ".hive" / "node" / "config.json", method="systemd", system=True,
                            dry_run=True)
        self.assertEqual(texts_cfg, 1)
        for unit in texts:
            self.assertIn("User=alice", unit)
            self.assertIn(f'Environment="HIVE_HOME={user_home / ".hive"}"', unit)
            wd = [ln for ln in unit.splitlines() if ln.startswith("WorkingDirectory=")][0]
            self.assertEqual(wd, f"WorkingDirectory={Path(ucfg.data_dir).resolve()}")
            self.assertNotIn(str(root_home), unit)
            self.assertIn(str(user_home / ".hive" / "node" / "config.json"), unit)

    def test_user_unit_unchanged(self):
        texts = []
        orig = install.render_systemd
        with mock.patch.dict(os.environ, {"SUDO_USER": "alice"}), \
                mock.patch.object(install, "render_systemd", side_effect=lambda *a, **kw: texts.append(
                    orig(*a, **kw)) or texts[-1]):
            install.install("node", self.config_path, method="systemd", dry_run=True)
        self.assertIn(f'Environment="HIVE_HOME={self.hive_home}"', texts[0])


class TestRestartLauncherWithoutRecord(InstallCase):
    """§13.1: a daemon with no install record whose launcher relaunches it (start-hive.sh) restarts fine."""

    def setUp(self):
        super().setUp()
        install._execute.side_effect = None

    def test_relaunched_by_its_loop(self):
        s = Seams(self, method=None, holders=[(0, OLD), (2, None), (7, NEW)], alive_until=2)
        res = install.restart("node")
        self.assertEqual(s.controls, ["restart"])
        self.assertEqual(s.commands, [])
        self.assertIn("restarted", " ".join(res.notes))

    def test_true_foreground_still_unavailable(self):
        s = Seams(self, method=None, holders=[(0, OLD), (2, None)], alive_until=2)
        with self.assertRaises(HiveError) as cm:
            install.restart("node")
        self.assertEqual(cm.exception.code, "unavailable")
        self.assertGreaterEqual(s.t, 2 + install.HOLDER_WAIT)


@unittest.skipUnless(POSIX and shutil.which("sh"), "start-hive.sh is POSIX sh")
class TestStartHiveIsolatedBootstrap(ti.TestStartHive):
    """D56: the inline bootstrap never imports from the start cwd (an agent-writable /workspace)."""

    def test_planted_module_in_cwd_ignored(self):
        marker = self.base / "planted.txt"
        for mod in ("json", "ssl", "socket", "base64", "hashlib"):
            (self.base / f"{mod}.py").write_text(f"open({str(marker)!r}, 'a').write('{mod}\\n')\n")
        self.serve()
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(marker.exists(), marker.read_text() if marker.exists() else "")
        self.assertTrue((self.ws / ".hive" / "hive.pyz").exists())
        self.assertIn('"$PY" -I -', START_HIVE.read_text())


# keep the inherited TestStartHive tests from running twice
for _n in [n for n in vars(ti.TestStartHive) if n.startswith("test_")]:
    setattr(TestStartHiveIsolatedBootstrap, _n, None)


if __name__ == "__main__":
    unittest.main()
