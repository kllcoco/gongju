"""util.py (P0, §4.2): tokens, time, sizes, IP categories, names, files, locks, pid identity, self invocation."""
from __future__ import annotations

import hmac
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import util
from hive.errors import HiveError

PKG_PARENT = Path(util.__file__).resolve().parent.parent


class TestTokens(unittest.TestCase):
    def test_new_token(self):
        t = util.new_token()
        self.assertTrue(t.startswith("hv_"))
        self.assertGreaterEqual(len(t), 40)
        self.assertNotEqual(t, util.new_token())
        self.assertTrue(util.new_token(8, prefix="x_").startswith("x_"))

    def test_hash_token(self):
        h = util.hash_token("hv_abc")
        self.assertTrue(h.startswith("sha256:"))
        self.assertEqual(len(h), 7 + 64)

    def test_match_token_hash_compares_all_entries(self):
        stored = [util.hash_token("a"), util.hash_token("hv_x"), util.hash_token("b"), util.hash_token("c")]
        real = hmac.compare_digest
        with mock.patch("hive.util.hmac.compare_digest", side_effect=real) as cd:
            self.assertEqual(util.match_token_hash("hv_x", stored), 1)
        self.assertEqual(cd.call_count, len(stored))
        self.assertEqual(util.match_token_hash("nope", stored), -1)
        self.assertEqual(util.match_token_hash("x", []), -1)


class TestTime(unittest.TestCase):
    def test_iso_utc(self):
        self.assertEqual(util.iso_utc(1763370723.12), "2025-11-17T09:12:03.120Z")
        self.assertRegex(util.iso_utc(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")

    def test_parse_iso(self):
        self.assertAlmostEqual(util.parse_iso("2025-11-17T09:12:03.120Z"), 1763370723.12, places=3)
        self.assertAlmostEqual(util.parse_iso("2025-11-17T10:12:03.120+01:00"), 1763370723.12, places=3)
        self.assertEqual(util.parse_iso("2027-01-01"), 1798761600.0)
        t = time.time()
        self.assertAlmostEqual(util.parse_iso(util.iso_utc(t)), t, delta=0.001)
        with self.assertRaises(HiveError):
            util.parse_iso("yesterday")
        with self.assertRaises(HiveError):
            util.parse_iso("2026-02-30T00:00:00Z")
        for bad in ("2026-11-17T25:61:61Z", "2026-11-17T24:00:00Z", "2026-11-17T23:60:00Z", "2026-11-17T23:59:61Z",
                    "2026-11-17T10:00:00+15:00", "2026-11-17T10:00:00+01:60"):
            with self.assertRaises(HiveError, msg=bad):
                util.parse_iso(bad)
        self.assertEqual(util.parse_iso("2026-11-17T10:00:00+14:00"), util.parse_iso("2026-11-16T20:00:00Z"))


class TestJsonHash(unittest.TestCase):
    def test_canonical_json(self):
        self.assertEqual(util.canonical_json({"b": 1, "a": ["é", 2]}), '{"a":["é",2],"b":1}')

    def test_sha256_text(self):
        self.assertEqual(util.sha256_text(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")


class TestTruncation(unittest.TestCase):
    def test_truncate_bytes(self):
        self.assertEqual(util.truncate_bytes(b"abc", 10), (b"abc", False))
        data = bytes(range(100))
        out, cut = util.truncate_bytes(data, 40, head_ratio=0.25)
        self.assertTrue(cut)
        self.assertTrue(out.startswith(data[:10]))
        self.assertTrue(out.endswith(data[-30:]))
        self.assertIn(b"truncated 60 bytes", out)

    def test_truncate_text_line_boundaries(self):
        s = "".join(f"line {i:03d}\n" for i in range(200))
        out, omitted = util.truncate_text(s, 400)
        self.assertGreater(omitted, 0)
        head, _, rest = out.partition("…[")
        self.assertTrue(head.endswith("\n"))
        self.assertTrue(all(line.startswith("line ") for line in head.splitlines()))
        tail = rest.split("]…\n", 1)[1]
        self.assertTrue(all(line.startswith("line ") and len(line) == 8 for line in tail.splitlines()))
        self.assertEqual(len(head) + len(tail) + omitted, len(s))
        self.assertEqual(util.truncate_text("short", 100), ("short", 0))


class TestSizes(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(util.human_bytes(512), "512 B")
        self.assertEqual(util.human_bytes(1536), "1.5 KiB")
        self.assertEqual(util.human_bytes(int(23.9 * 1024 ** 3)), "23.9 GiB")

    def test_parse_size(self):
        self.assertEqual(util.parse_size("64MiB"), 67108864)
        self.assertEqual(util.parse_size("16g"), 16 * 1024 ** 3)
        self.assertEqual(util.parse_size("2GB"), 2 * 1000 ** 3)
        self.assertEqual(util.parse_size("512"), 512)
        self.assertEqual(util.parse_size(1024), 1024)
        self.assertEqual(util.parse_size("1.5 KiB"), 1536)
        for bad in ("lots", "-1", "", True):
            with self.subTest(bad=bad), self.assertRaises(HiveError):
                util.parse_size(bad)


class TestNames(unittest.TestCase):
    def test_glob_match(self):
        self.assertTrue(util.glob_match("shell.run", ["*"]))
        self.assertTrue(util.glob_match("fs.read", ["net.*", "fs.*"]))
        self.assertFalse(util.glob_match("fs.read", ["net.*"]))
        self.assertFalse(util.glob_match("x", []))

    def test_valid_name(self):
        for ok in ("desk-4090", "a", "pod_a.1", "x" * 64):
            self.assertTrue(util.valid_name(ok), ok)
        for bad in ("", "-a", ".a", "a b", "x" * 65, "a/b", None, "Desk", "GPU-Box", "desk\n"):
            self.assertFalse(util.valid_name(bad), bad)
        self.assertTrue(util.valid_name("x" * 10, maxlen=10))
        self.assertFalse(util.valid_name("x" * 11, maxlen=10))


class TestIpCategory(unittest.TestCase):
    TABLE = [
        ("169.254.169.254", "metadata"), ("100.100.100.200", "metadata"), ("fd00:ec2::254", "metadata"),
        ("64:ff9b::a9fe:a9fe", "metadata"), ("::ffff:169.254.169.254", "metadata"),
        ("2001:0:4136:e378:8000:63bf:5601:5601", "metadata"),          # Teredo-wrapped 169.254.169.254
        ("2002:a9fe:a9fe::1", "metadata"),                             # 6to4-wrapped
        ("127.0.0.1", "loopback"), ("127.8.9.10", "loopback"), ("::1", "loopback"),
        ("::ffff:127.0.0.1", "loopback"),
        ("169.254.1.1", "linklocal"), ("fe80::1", "linklocal"), ("fe80::1%eth0", "linklocal"),
        ("198.18.3.4", "fakeip"), ("198.19.255.255", "fakeip"),
        ("10.1.2.3", "private"), ("172.16.0.1", "private"), ("172.31.255.255", "private"),
        ("192.168.1.1", "private"), ("100.64.0.1", "private"), ("100.101.1.7", "private"), ("fd7a:115c:a1e0::1",
                                                                                           "private"),
        ("0.0.0.0", "reserved"), ("0.1.2.3", "reserved"), ("224.0.0.1", "reserved"), ("240.0.0.1", "reserved"),
        ("255.255.255.255", "reserved"), ("::", "reserved"), ("ff02::1", "reserved"), ("192.0.2.1", "reserved"),
        ("198.51.100.7", "reserved"), ("203.0.113.9", "reserved"),
        ("93.184.216.34", "public"), ("8.8.8.8", "public"), ("2606:4700::1111", "public"),
        ("172.32.0.1", "public"), ("100.128.0.1", "public"), ("[::1]", "loopback"),
    ]

    def test_table(self):
        for ip, cat in self.TABLE:
            with self.subTest(ip=ip):
                self.assertEqual(util.ip_category(ip), cat)

    def test_invalid(self):
        with self.assertRaises(HiveError):
            util.ip_category("example.com")

    def test_is_loopback_host(self):
        for h in ("localhost", "LOCALHOST", "127.0.0.1", "::1", "[::1]", "::ffff:127.0.0.1", "localhost."):
            self.assertTrue(util.is_loopback_host(h), h)
        for h in ("10.0.0.1", "example.com", "localhost.example.com", ""):
            self.assertFalse(util.is_loopback_host(h), h)

    def test_is_local_name(self):
        for h in ("localhost", "desk", "nas.local", "router.lan", "x.internal", "a.home.arpa", "b.localdomain",
                  "DESK.LAN"):
            self.assertTrue(util.is_local_name(h), h)
        for h in ("example.com", "home-hub.tail1.ts.net", "127.0.0.1", "::1", "", "hf-mirror.com"):
            self.assertFalse(util.is_local_name(h), h)


class TestSafeRemoteName(unittest.TestCase):
    def test_rejects(self):
        for bad in ("../x", "a/../../x", "C:x", "con.txt", "CON", "lpt1.log", "a\\b", "a\x00b", "file.", "dir /x",
                    "", "/abs", "a//b", "./a", "a/.", "a/", "x" * 256, "tab\there", "a/com9.bin"):
            with self.subTest(name=bad):
                self.assertIsNone(util.safe_remote_name(bad))
        self.assertIsNone(util.safe_remote_name("a" * 20, max_total=10))

    def test_accepts(self):
        for ok in ("model.safetensors", "sub/dir/config.json", "tokenizer_config.json", "中文.txt", "a..b",
                   "console.txt"):
            with self.subTest(name=ok):
                self.assertEqual(util.safe_remote_name(ok), ok)


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hiveutil-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_write_json_atomic_fsync_and_perm(self):
        p = self.d / "sub" / "x.json"
        with mock.patch("hive.util.os.fsync", wraps=os.fsync) as fs:
            util.write_json_atomic(p, {"a": "é"}, private=True)
        self.assertGreaterEqual(fs.call_count, 1)
        self.assertEqual(util.read_json(p), {"a": "é"})
        if not util.IS_WINDOWS:
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual([x.name for x in p.parent.iterdir()], ["x.json"])  # no temp files left

    def test_atomic_write_bytes(self):
        p = self.d / "b.bin"
        util.atomic_write_bytes(p, b"\x00\x01")
        self.assertEqual(p.read_bytes(), b"\x00\x01")

    def test_read_json(self):
        self.assertEqual(util.read_json(self.d / "missing.json", default=7), 7)
        bad = self.d / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        with self.assertRaises(HiveError) as cm:
            util.read_json(bad)
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("invalid JSON in", cm.exception.message)
        bom = self.d / "bom.json"
        bom.write_bytes(b"\xef\xbb\xbf" + '{"name": "desk-4090", "x": "\u00e9"}'.encode("utf-8"))
        self.assertEqual(util.read_json(bom), {"name": "desk-4090", "x": "\u00e9"})

    def test_replace_with_retry(self):
        a, b = self.d / "a", self.d / "b"
        a.write_text("1")
        util.replace_with_retry(a, b)
        self.assertEqual(b.read_text(), "1")

    def test_secure_mkdir_posix(self):
        if util.IS_WINDOWS:
            self.skipTest("POSIX modes")
        p = self.d / "s" / "t"
        self.assertTrue(util.secure_mkdir(p))
        self.assertEqual(p.stat().st_mode & 0o777, 0o700)

    def test_secure_file_windows_argv(self):
        f = self.d / "secret.json"
        f.write_text("{}")
        calls = []
        with mock.patch.object(util, "IS_WINDOWS", True), \
                mock.patch.object(util, "_current_user_sid", return_value="S-1-5-21-111-222-333-1001"), \
                mock.patch.object(util, "_run_icacls", side_effect=lambda argv: calls.append(argv) or True):
            self.assertTrue(util.secure_file(f))
            self.assertTrue(util.secure_mkdir(self.d / "dir"))
        self.assertEqual(calls[0], ["icacls", str(f), "/inheritance:r", "/grant:r", "*S-1-5-21-111-222-333-1001:F",
                                    "/grant:r", "*S-1-5-18:F"])
        self.assertIn("*S-1-5-21-111-222-333-1001:(OI)(CI)F", calls[1])

    def test_secure_file_required_failure(self):
        with mock.patch.object(util, "IS_WINDOWS", True), mock.patch.object(util, "_current_user_sid",
                                                                            return_value=None):
            self.assertFalse(util.secure_file(self.d / "x"))
            with self.assertRaises(HiveError) as cm:
                util.secure_file(self.d / "x", required=True)
        self.assertEqual(cm.exception.code, "internal")


class TestFileLock(unittest.TestCase):
    def test_holder_json_readable_while_locked(self):
        d = Path(tempfile.mkdtemp(prefix="hivelock-"))
        p = d / "node.lock"
        a, b = util.FileLock(p), util.FileLock(p)
        self.assertTrue(a.acquire(note="j_123"))
        try:
            self.assertFalse(b.acquire())
            h = b.holder()
            self.assertEqual(h["pid"], os.getpid())
            self.assertEqual(h["note"], "j_123")
            self.assertEqual(b.holder_pid(), os.getpid())
            raw = p.read_bytes()[:4096].decode().strip()
            self.assertEqual(json.loads(raw.split("\n")[0])["pid"], os.getpid())
        finally:
            a.release()
        self.assertIsNone(b.holder())
        self.assertTrue(b.acquire())
        b.release()

    def test_holder_dead_pid(self):
        d = Path(tempfile.mkdtemp(prefix="hivelock-"))
        p = d / "x.lock"
        p.write_text(json.dumps({"pid": 2 ** 22 + 12345, "ctime": 1.0, "note": ""}))
        self.assertIsNone(util.FileLock(p).holder())


class TestPidIdentity(unittest.TestCase):
    def test_own_pid(self):
        ct = util.process_create_time(os.getpid())
        self.assertIsNotNone(ct)
        self.assertLess(abs(ct - time.time()), 10 * 24 * 3600 * 365)
        self.assertTrue(util.pid_alive(os.getpid()))
        self.assertTrue(util.pid_alive(os.getpid(), ct))
        self.assertFalse(util.pid_alive(os.getpid(), ct - 100))

    def test_dead_pid(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        self.assertFalse(util.pid_alive(p.pid))
        self.assertFalse(util.pid_alive(0))
        self.assertFalse(util.pid_alive(-5))

    @unittest.skipIf(util.IS_WINDOWS, "POSIX zombies")
    def test_unreaped_zombie_is_dead(self):
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            deadline = time.time() + 20
            while time.time() < deadline and util.pid_alive(p.pid):
                time.sleep(0.05)
            self.assertFalse(util.pid_alive(p.pid))   # exited but not yet reaped (no wait() so far)
        finally:
            p.wait()

    def test_posix_zombie_probe_ps(self):
        # the macOS/BSD path: `ps -o stat= -p PID` with LC_ALL=C, state "Z…" = zombie
        res = subprocess.CompletedProcess(["ps"], 0, stdout=b"Z+\n", stderr=b"")
        with mock.patch.dict(sys.modules, {"psutil": None}), \
                mock.patch.object(util.subprocess, "run", return_value=res) as run:
            self.assertTrue(util._posix_is_zombie(123))
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["ps", "-o", "stat=", "-p", "123"])
        self.assertEqual(run.call_args[1]["env"]["LC_ALL"], "C")
        for out, rc in ((b"S\n", 0), (b"", 1)):
            res = subprocess.CompletedProcess(["ps"], rc, stdout=out, stderr=b"")
            with mock.patch.dict(sys.modules, {"psutil": None}), \
                    mock.patch.object(util.subprocess, "run", return_value=res):
                self.assertFalse(util._posix_is_zombie(123))

    def test_no_wp2_module_imported(self):
        code = ("import sys, os; from hive import util; "
                "assert util.pid_alive(os.getpid()); assert util.process_create_time(os.getpid()) is not None; "
                "bad=[m for m in sys.modules if m in ('hive.proctree','hive.winapi','hive.jobs','hive.services')]; "
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code], cwd=str(PKG_PARENT), capture_output=True, text=True,
                             timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "")


class TestSelfInvocation(unittest.TestCase):
    def test_self_command_module(self):
        with mock.patch.object(util, "_zipapp_path", return_value=None):
            cmd = util.self_command()
        self.assertEqual(cmd[0], sys.executable)
        if sys.version_info >= (3, 11):
            self.assertEqual(cmd[1:], ["-P", "-m", "hive"])
        else:
            self.assertEqual(cmd[1:], ["-m", "hive"])

    def test_self_command_pyz_never_P(self):
        pyz = Path("/opt/x/hive.pyz")
        with mock.patch.object(util, "_zipapp_path", return_value=pyz), \
                mock.patch.object(util.sys, "version_info", (3, 13, 0)):
            self.assertEqual(util.self_command(), [sys.executable, str(pyz)])

    def test_launcher_argv(self):
        cfg = Path(tempfile.gettempdir()) / "cfg.json"
        with mock.patch.object(util, "_zipapp_path", return_value=None):
            argv = util.launcher_argv("node", cfg)
            self.assertEqual(argv[-4:], ["node", "run", "--config", str(cfg.resolve())])
            self.assertIn("hive", argv)
            self.assertEqual(util.launcher_argv("hub", cfg, supervise=True)[-1], "--supervise")
        with mock.patch.object(util, "_zipapp_path", return_value=Path("/w/hive.pyz")):
            argv = util.launcher_argv("node", cfg)
        self.assertEqual(argv, [sys.executable, str(Path("/w/hive.pyz")), "node", "run", "--config",
                                str(cfg.resolve())])

    def test_self_env(self):
        with mock.patch.object(util, "_zipapp_path", return_value=None), \
                mock.patch.object(util, "_site_dirs", return_value=[]), \
                mock.patch.dict(os.environ, {"PYTHONPATH": "/extra"}):
            env = util.self_env()
        self.assertEqual(env, {"PYTHONPATH": str(PKG_PARENT) + os.pathsep + "/extra"})
        with mock.patch.object(util, "_zipapp_path", return_value=Path("/w/hive.pyz")):
            self.assertEqual(util.self_env(), {})
        with mock.patch.object(util, "_zipapp_path", return_value=None), \
                mock.patch.object(util, "_site_dirs", return_value=[util._norm_dir(str(PKG_PARENT))]):
            self.assertEqual(util.self_env(), {})

    def test_console_python(self):
        self.assertEqual(util.console_python(), sys.executable if not util.IS_WINDOWS else util.console_python())

    def test_prune_cwd_path(self):
        saved_path, saved_cwd = list(sys.path), os.getcwd()
        d = tempfile.mkdtemp(prefix="hiveprune-")
        try:
            os.chdir(d)
            sys.path[:] = ["", d, "/some/where"] + saved_path
            util.prune_cwd_path()
            self.assertNotIn("", sys.path)
            self.assertNotIn(d, sys.path)
            self.assertIn("/some/where", sys.path)
            # running from the checkout that holds the hive package: nothing is removed
            os.chdir(str(PKG_PARENT))
            sys.path[:] = ["", str(PKG_PARENT)] + saved_path
            util.prune_cwd_path()
            self.assertEqual(sys.path[:2], ["", str(PKG_PARENT)])
        finally:
            os.chdir(saved_cwd)
            sys.path[:] = saved_path

    def test_main_prunes_before_any_import(self):
        # D56: `python -m hive` started in a writable folder must not import planted stdlib modules (the prune in
        # hive/__main__.py runs before any hive.* import; hive/__init__.py imports nothing)
        d = tempfile.mkdtemp(prefix="hiveplant-")
        try:
            for m in ("ipaddress", "subprocess", "json", "logging", "calendar", "fnmatch", "secrets", "tempfile",
                      "hashlib", "__future__"):
                Path(d, m + ".py").write_text("import sys\nsys.stderr.write('PLANTED %s\\n')\nsys.exit(99)\n" % m,
                                              encoding="utf-8")
            env = dict(os.environ)
            env.pop("PYTHONSAFEPATH", None)
            env["PYTHONPATH"] = str(PKG_PARENT) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
            r = subprocess.run([sys.executable, "-m", "hive", "--version"], cwd=d, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, universal_newlines=True, timeout=60)
            self.assertNotIn("PLANTED", r.stdout + r.stderr)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("1.0.0", r.stdout)
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)


class TestBoot(unittest.TestCase):
    def test_boot_key_stable(self):
        a, b = util.boot_key(), util.boot_key()
        self.assertTrue(util.same_boot(a, b), (a, b))

    def test_windows_winlogon(self):
        import hive.winapi as winapi
        with mock.patch.object(util, "IS_WINDOWS", True), \
                mock.patch.object(winapi, "session_winlogon_ctime", return_value=1763370723.9):
            self.assertEqual(util.boot_key(), "w:1763370723")

    def test_windows_fallbacks(self):
        import hive.winapi as winapi
        with mock.patch.object(util, "IS_WINDOWS", True), mock.patch.object(util, "_tick_count_s", return_value=100.0), \
                mock.patch.object(util.time, "time", return_value=1000.04):
            with mock.patch.object(winapi, "session_winlogon_ctime", return_value=None):
                self.assertEqual(util.boot_key(), "t:900.0")
            with mock.patch.dict(sys.modules, {"hive.winapi": None}):
                saved = sys.modules.get("hive").__dict__.pop("winapi", None)
                try:
                    self.assertEqual(util.boot_key(), "t:900.0")
                finally:
                    if saved is not None:
                        sys.modules["hive"].__dict__["winapi"] = saved

    def test_same_boot(self):
        self.assertTrue(util.same_boot("t:1000.0", "t:1012.4"))
        self.assertFalse(util.same_boot("t:1000.0", "t:1040.0"))
        self.assertTrue(util.same_boot("w:123", "w:123"))
        self.assertFalse(util.same_boot("w:123", "w:124"))
        self.assertFalse(util.same_boot("w:123", "t:123"))
        self.assertFalse(util.same_boot(None, "t:1"))
        self.assertFalse(util.same_boot("", ""))
        self.assertTrue(util.same_boot("abc:12", "abc:12"))


class TestPathsDisplay(unittest.TestCase):
    def test_path_key(self):
        self.assertEqual(util.path_key("C:\\Users\\Me\\", casefold=True), "c:/users/me")
        self.assertEqual(util.path_key("/home/Me/", casefold=False), "/home/Me")
        self.assertEqual(util.path_key("/", casefold=False), "/")

    def test_match_deny(self):
        pats = ["/home/u/.ssh/**", "**/.git/hooks/**", "**/*.pth", "/etc/passwd"]
        self.assertTrue(util.match_deny(pats, "/home/u/.ssh"))
        self.assertTrue(util.match_deny(pats, "/home/u/.ssh/id_rsa"))
        self.assertFalse(util.match_deny(pats, "/home/u/.sshx"))
        self.assertTrue(util.match_deny(pats, "/p/repo/.git/hooks"))
        self.assertTrue(util.match_deny(pats, "/p/repo/.git/hooks/pre-commit"))
        self.assertTrue(util.match_deny(pats, "/x/site-packages/evil.pth"))
        self.assertTrue(util.match_deny(pats, "/etc/passwd"))
        self.assertFalse(util.match_deny(pats, "/etc/passwd2"))
        self.assertFalse(util.match_deny([], "/a"))

    def test_escape_terminal(self):
        self.assertEqual(util.escape_terminal("a\x1b[2Kb"), "a\\x1b[2Kb")
        self.assertEqual(util.escape_terminal("x\u202ey"), "x\\u{202E}y")
        self.assertEqual(util.escape_terminal("l1\n\tl2\r"), "l1\n\tl2\\x0d")
        self.assertEqual(util.escape_terminal("\x9b\x7f\ufeff\u200b"), "\\x9b\\x7f\\u{FEFF}\\u{200B}")
        once = util.escape_terminal("a\x1b\u2066b")
        self.assertEqual(util.escape_terminal(once), once)
        self.assertEqual(util.escape_terminal("中文 ok"), "中文 ok")

    def test_kv(self):
        self.assertEqual(util.kv_list({"A": "1", "B": 2}), ["A=1", "B=2"])
        self.assertEqual(util.kv_list(["A=1"]), ["A=1"])
        self.assertEqual(util.kv_list(None), [])
        self.assertEqual(util.parse_kv(["A=1", "B=x=y"]), {"A": "1", "B": "x=y"})
        self.assertEqual(util.parse_kv(["Accept: text/html"], sep=":"), {"Accept": "text/html"})
        with self.assertRaises(HiveError):
            util.parse_kv(["novalue"])

    def test_iso_local_kept(self):
        self.assertRegex(util.iso(0), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()
