"""WP4: procinfo, sysinfo, gpu, wsl and the system.info / proc.list / proc.kill handlers (design §9.2-§9.3, §12,
§20.7; §21.2 row "system/procs/gpu/wsl")."""
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

from hive import catalog, gpu, procinfo, proctree, schema, sysinfo, util, wsl
from hive.actions import HANDLERS, load_all
from hive.errors import HiveError
from tests import helpers

FIX = Path(__file__).resolve().parent / "fixtures" / "wp4"
POSIX = os.name != "nt"
LINUX = sys.platform.startswith("linux")


def fixture(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def validate_result(action: str, result: dict) -> None:
    schema.validate(catalog.get(action).result, result, coerce=False, defaults=False)


class TrustedPath:
    """Context manager: proctree.set_trusted_path(value) and restore the module value afterwards."""

    def __init__(self, value):
        self.value = value

    def __enter__(self):
        self.old = proctree._trusted_path
        proctree.set_trusted_path(self.value)
        sysinfo.clear_cache()
        gpu.clear_cache()
        return self

    def __exit__(self, *exc):
        proctree._trusted_path = self.old
        sysinfo.clear_cache()
        gpu.clear_cache()


def make_tool_dir(name: str, script: str) -> Path:
    """A temp dir holding an executable ``name`` (POSIX script or Windows .cmd + .py)."""
    d = Path(tempfile.mkdtemp(prefix="hivewp4tool-")).resolve()
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        (d / f"{name}.py").write_text(script, encoding="utf-8")
        (d / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', encoding="utf-8")
    else:
        p = d / name
        p.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
        p.chmod(0o755)
    return d


# ---------------------------------------------------------------------------------------------------------------
# procinfo

class TestProcinfoParsers(unittest.TestCase):
    def test_proc_stat(self):
        raw = "4242 (python3 (x) y) S 1 4242 4242 0 -1 4194560 100 0 0 0 250 50 0 0 20 0 3 0 123456 1000 300 " \
              "18446744073709551615 0 0 0"
        st = procinfo.parse_proc_stat(raw)
        self.assertEqual((st["comm"], st["ppid"], st["ticks"], st["starttime"], st["rss_pages"]),
                         ("python3 (x) y", 1, 300, 123456, 300))
        self.assertIsNone(procinfo.parse_proc_stat("garbage"))

    def test_proc_net_tcp(self):
        rows = procinfo.parse_proc_net_tcp(fixture("proc_net_tcp.txt"))
        self.assertEqual(rows, [("127.0.0.1", 8080, 41001), ("0.0.0.0", 7701, 41002)])   # ESTABLISHED dropped
        rows6 = procinfo.parse_proc_net_tcp(fixture("proc_net_tcp6.txt"))
        self.assertEqual(rows6, [("::1", 8081, 41010), ("127.0.0.1", 9090, 41011)])

    def test_netstat_localized(self):
        rows = procinfo.parse_netstat(fixture("netstat_ano.txt"), {4321: "python.exe"})
        got = {(r["address"], r["port"], r["pid"]) for r in rows}
        self.assertEqual(got, {("0.0.0.0", 135, 1100), ("127.0.0.1", 7701, 4321), ("::", 445, 4), ("::1", 11434, 777)})
        self.assertEqual(next(r for r in rows if r["port"] == 7701)["name"], "python.exe")

    def test_lsof(self):
        rows = procinfo.parse_lsof(fixture("lsof_listen.txt"))
        self.assertEqual([(r["address"], r["port"], r["pid"], r["name"]) for r in rows],
                         [("0.0.0.0", 8080, 501, "ovmon"), ("127.0.0.1", 11434, 777, "ollama"),
                          ("::1", 11434, 777, "ollama")])

    def test_lstart_utc(self):
        # §20.6: ps runs with LC_ALL=C TZ=UTC0, so lstart is a C-locale UTC time
        self.assertEqual(procinfo.parse_lstart("Wed Nov 18 03:23:47 2026"), 1794972227.0)
        self.assertEqual(procinfo.parse_lstart("Mon Jan  5 01:02:03 2026"), 1767574923.0)
        self.assertIsNone(procinfo.parse_lstart("Mi 18 Nov 03:23:47 2026"))

    def test_ps_rows(self):
        text = ("  501     1 alice    204800   3.5 Wed Nov 18 03:23:47 2026     /Applications/My App.app/Contents/x\n"
                "  502   501 root         10   0,0 Wed Nov 18 03:24:00 2026     sshd\n")
        rows = procinfo.parse_ps_rows(text)
        self.assertEqual(rows[0]["name"], "x")
        self.assertEqual((rows[0]["pid"], rows[0]["ppid"], rows[0]["user"], rows[0]["mem_bytes"], rows[0]["cpu_pct"]),
                         (501, 1, "alice", 204800 * 1024, 3.5))
        self.assertEqual(rows[0]["create_time"], 1794972227.0)
        self.assertEqual(rows[1]["cpu_pct"], 0.0)
        self.assertEqual(procinfo.parse_ps_args(" 501 /bin/x -a b\n 502 sshd\n"), {501: "/bin/x -a b", 502: "sshd"})

    def test_windows_cmdline_split(self):
        self.assertEqual(procinfo.split_windows_cmdline(r'"C:\Program Files\x.exe" -m hive "a b" c\\"d'),
                         [r"C:\Program Files\x.exe", "-m", "hive", "a b", 'c\\d'])


class TestProcinfoLive(unittest.TestCase):
    def test_process_info_shape(self):
        info = procinfo.process_info(os.getpid())
        self.assertEqual(set(info), {"pid", "ppid", "name", "cmdline", "uid", "user", "create_time"})
        self.assertEqual(info["pid"], os.getpid())
        self.assertEqual(info["ppid"], os.getppid())
        self.assertIsInstance(info["cmdline"], list)
        self.assertAlmostEqual(info["create_time"], util.process_create_time(os.getpid()), delta=2)
        if POSIX:
            self.assertEqual(info["uid"], os.getuid())

    def test_gone_process(self):
        p = subprocess.Popen(helpers.py_cmd("pass"))
        p.wait()
        self.assertIsNone(procinfo.process_info(p.pid))
        self.assertIsNone(procinfo.process_info(-5))

    def test_list_processes_without_psutil(self):
        with mock.patch.object(procinfo, "_psutil", return_value=None):
            rows, method = procinfo.list_processes()
        self.assertIn(method, ("proc", "ps", "toolhelp"))
        me = [r for r in rows if r["pid"] == os.getpid()]
        self.assertEqual(len(me), 1)
        self.assertEqual(set(me[0]), {"pid", "ppid", "name", "user", "uid", "cpu_pct", "mem_bytes", "create_time",
                                      "cmdline"})
        self.assertGreater(me[0]["mem_bytes"] or 0, 0)

    @unittest.skipUnless(LINUX, "Linux /proc")
    def test_listeners_proc_sees_own_socket(self):
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        try:
            with mock.patch.object(procinfo, "_psutil", return_value=None):
                rows, method = procinfo.listeners()
            self.assertEqual(method, "proc")
            port = s.getsockname()[1]
            mine = [r for r in rows if r["port"] == port]
            self.assertEqual(len(mine), 1)
            self.assertEqual((mine[0]["address"], mine[0]["pid"]), ("127.0.0.1", os.getpid()))
        finally:
            s.close()

    @unittest.skipUnless(LINUX, "Linux /proc layout")
    def test_proc_fixture_tree(self):
        root = Path(tempfile.mkdtemp(prefix="hiveproc-"))
        self.addCleanup(shutil.rmtree, str(root), True)
        (root / "self").mkdir()
        (root / "stat").write_text("cpu  1 2 3 4\nbtime 1700000000\n")
        d = root / "77"
        d.mkdir()
        (d / "stat").write_text("77 (verylongprocessn) S 1 77 77 0 -1 0 0 0 0 0 5 5 0 0 20 0 1 0 1000 0 10 0\n")
        (d / "cmdline").write_bytes(b"/opt/verylongprocessname\x00--flag\x00")
        (d / "status").write_text("Name:\tverylongprocessn\nUid:\t1234\t1234\t1234\t1234\n")
        with mock.patch.object(procinfo, "_PROC", str(root)), mock.patch.object(procinfo, "_psutil",
                                                                                 return_value=None):
            info = procinfo.process_info(77)
            rows, method = procinfo.list_processes()
        self.assertEqual(method, "proc")
        self.assertEqual(info["name"], "verylongprocessname")
        self.assertEqual(info["cmdline"], ["/opt/verylongprocessname", "--flag"])
        self.assertEqual(info["uid"], 1234)
        self.assertAlmostEqual(info["create_time"], 1700000000 + 1000 / procinfo._clk_tck())
        self.assertEqual([r["pid"] for r in rows], [77])
        self.assertEqual(rows[0]["mem_bytes"], 10 * procinfo._page_size())


# ---------------------------------------------------------------------------------------------------------------
# sysinfo

class TestSysinfo(unittest.TestCase):
    def setUp(self):
        sysinfo.clear_cache()
        p = mock.patch.object(sysinfo, "_psutil", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_sections_without_psutil(self):
        o = sysinfo.os_info()
        for k in ("system", "release", "version", "machine", "hostname", "distro", "python", "boot_time",
                  "uptime_s", "timezone", "cloud", "wsl"):
            self.assertIn(k, o)
        c = sysinfo.cpu_info()
        self.assertEqual(c["logical"], os.cpu_count())
        m = sysinfo.memory()
        self.assertGreater(m["total"], 0)
        self.assertTrue(0 <= m["used_pct"] <= 100)
        ds = sysinfo.disks()
        self.assertTrue(ds and all(d["total"] > 0 for d in ds))
        n = sysinfo.network()
        self.assertEqual(set(n), {"primary_ip", "addresses", "tailscale_ip"})
        if LINUX:
            self.assertIsNotNone(o["uptime_s"])
            self.assertIsNotNone(c["load_avg"])

    def test_cloud_info(self):
        with mock.patch.dict(os.environ, {"RUNPOD_POD_ID": "abc123"}, clear=False):
            self.assertEqual(sysinfo.cloud_info(), {"provider": "runpod", "pod_id": "abc123"})
        env = {k: v for k, v in os.environ.items() if not k.startswith(("RUNPOD_", "VAST_"))}
        with mock.patch.dict(os.environ, dict(env, VAST_CONTAINERLABEL="C.42"), clear=True):
            self.assertEqual(sysinfo.cloud_info(), {"provider": "vast", "pod_id": "C.42"})
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(sysinfo, "_AUTODL", d):
            self.assertEqual(sysinfo.cloud_info()["provider"], "autodl")

    def test_container_limit(self):
        d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(d), True)
        with mock.patch.object(sysinfo, "_CGROUP", str(d)):
            self.assertIsNone(sysinfo.container_limit(1 << 34))
            (d / "memory.max").write_text("max\n")
            self.assertIsNone(sysinfo.container_limit(1 << 34))
            (d / "memory.max").write_text(str(8 << 30) + "\n")
            self.assertEqual(sysinfo.container_limit(1 << 34), 8 << 30)
            (d / "memory.max").unlink()
            (d / "memory").mkdir()
            (d / "memory" / "memory.limit_in_bytes").write_text("9223372036854771712\n")   # v1 "unlimited"
            self.assertIsNone(sysinfo.container_limit(1 << 34))

    def test_mounts_filter(self):
        text = ("overlay / overlay rw 0 0\nproc /proc proc rw 0 0\ntmpfs /dev/shm tmpfs rw 0 0\n"
                "/dev/sda1 /workspace ext4 rw 0 0\nC:\\134 /mnt/c 9p rw 0 0\ndrvfs /mnt/d drvfs rw 0 0\n"
                "none /usr/lib/wsl/drivers 9p ro 0 0\nnone /mnt/wsl tmpfs rw 0 0\n/dev/sdc /mnt/wsl/docker ext4 rw 0 0\n"
                "cgroup2 /sys/fs/cgroup cgroup2 rw 0 0\nsquashfs /snap/core squashfs ro 0 0\n"
                "/dev/sdb /my\\040disk ext4 rw 0 0\n")
        self.assertEqual(sysinfo.parse_mounts(text, wsl=True),
                         [("/", "overlay"), ("/workspace", "ext4"), ("/mnt/c", "9p"), ("/mnt/d", "drvfs"),
                          ("/my disk", "ext4")])
        self.assertIn(("/mnt/wsl/docker", "ext4"), sysinfo.parse_mounts(text, wsl=False))

    def test_df_and_vm_stat(self):
        df = ("Filesystem 1024-blocks Used Available Capacity Mounted on\n"
              "/dev/disk3s1s1 971350180 10000000 500000000 3% /\n"
              "/dev/disk3s5 971350180 400000000 500000000 45% /System/Volumes/Data\n"
              "map auto_home 0 0 0 100% /System/Volumes/Data/home\n")
        rows = sysinfo.parse_df(df)
        self.assertEqual(rows[0], ("/", 971350180 * 1024, 500000000 * 1024, "/dev/disk3s1s1"))
        vm = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:  1000.\n'
              'Pages active: 5.\nPages inactive:  2000.\nPages speculative:  10.\nPages purgeable: 0.\n')
        self.assertEqual(sysinfo.parse_vm_stat(vm), 3010 * 16384)

    def test_tailscale_ip(self):
        self.assertEqual(sysinfo.tailscale_ip(["192.168.1.2", "100.101.1.7"]), "100.101.1.7")
        self.assertEqual(sysinfo.tailscale_ip(["fd7a:115c:a1e0::1"]), "fd7a:115c:a1e0::1")
        self.assertIsNone(sysinfo.tailscale_ip(["10.0.0.1"]))

    def test_live_status_small(self):
        st = sysinfo.live_status()
        self.assertEqual(set(st), {"cpu_pct", "mem_pct", "gpus"})
        self.assertLessEqual(len(json.dumps(st)), 4096)


class TestTrustedPathProbes(unittest.TestCase):
    """§9.2 v3: probes resolve programs only on proctree.trusted_path(); a fake in an rw root is never executed."""

    def setUp(self):
        self.mark = Path(tempfile.mkdtemp()) / "ran"
        self.addCleanup(shutil.rmtree, str(self.mark.parent), True)
        self.tooldir = make_tool_dir("nvidia-smi", fixture("fake_nvidia_smi.py"))
        self.addCleanup(shutil.rmtree, str(self.tooldir), True)
        env = mock.patch.dict(os.environ, {"FAKE_SMI_MARK": str(self.mark), "FAKE_SMI_DIR": str(FIX),
                                           "PATH": str(self.tooldir) + os.pathsep + os.environ.get("PATH", "")})
        env.start()
        self.addCleanup(env.stop)
        nowsl = mock.patch.object(gpu, "_is_wsl", return_value=False)
        nowsl.start()
        self.addCleanup(nowsl.stop)

    def test_fake_in_rw_root_not_executed(self):
        # the node PATH has the folder, but it is inside an rw root, so Policy.trusted_path left it out
        with TrustedPath(os.pathsep.join(d for d in os.environ["PATH"].split(os.pathsep) if d != str(self.tooldir))):
            with mock.patch.object(gpu, "FIXED_SMI", []):
                d = gpu.detect()
                self.assertEqual(d["gpus"], [])
                self.assertIn(gpu.NO_SMI_NOTE, d["errors"])
                sw = sysinfo.software(["nvidia-smi"], refresh=True)
                self.assertIsNone(sw["nvidia-smi"])
                w = sysinfo.which(["nvidia-smi"], node_path=os.environ["PATH"])
        self.assertFalse(self.mark.exists())
        self.assertEqual(w["found"]["nvidia-smi"], {"path": str(self.tooldir / "nvidia-smi") if POSIX else
                                                    w["found"]["nvidia-smi"]["path"], "version": None,
                                                    "note": "in a writable root; not executed"})

    def test_fake_on_trusted_path_is_executed(self):
        with TrustedPath(str(self.tooldir) + os.pathsep + os.environ.get("PATH", "")):
            d = gpu.detect()
        self.assertTrue(self.mark.exists())
        self.assertEqual(d["backend"], "nvidia")
        self.assertEqual(len(d["gpus"]), 2)
        g0, g1 = d["gpus"]
        self.assertEqual((g0["name"], g0["memory_total_mb"], g0["memory_used_mb"], g0["utilization_pct"],
                          g0["temperature_c"], g0["power_w"], g0["driver"], g0["cuda"]),
                         ("NVIDIA GeForce RTX 4090", 24564, 1024, 7.0, 41.0, 35.2, "560.94", "12.6"))
        self.assertEqual((g1["memory_used_mb"], g1["utilization_pct"], g1["temperature_c"], g1["power_w"]),
                         (None, None, None, None))                                   # [N/A] / [Not Supported]
        self.assertEqual(g0["processes"], [{"pid": 4242, "name": "/usr/bin/python3", "memory_mb": 900}])
        self.assertEqual(g1["processes"], [{"pid": 4243, "name": "ollama", "memory_mb": None}])
        self.assertEqual((d["driver"], d["cuda"]), ("560.94", "12.6"))
        self.assertEqual(gpu.summary(d["gpus"]), "1× NVIDIA GeForce RTX 4090 24GB, 1× NVIDIA GeForce RTX 3060 12GB")
        with TrustedPath(str(self.tooldir) + os.pathsep + os.environ.get("PATH", "")):
            self.assertEqual(gpu.process_memory(), {4242: 900})
            w = sysinfo.which(["nvidia-smi"], node_path=str(self.tooldir))
        self.assertIn("NVIDIA-SMI", w["found"]["nvidia-smi"]["version"] or "")

    def test_fail_safe_default_excludes_temp(self):
        # v6: without set_trusted_path (CLI, tests) the default drops PATH entries under home, cwd and the temp dir
        old = proctree._trusted_path
        proctree._trusted_path = None
        try:
            self.assertNotIn(str(self.tooldir), proctree.trusted_path().split(os.pathsep))
            with mock.patch.object(gpu, "FIXED_SMI", []):
                found = gpu.find_tool("nvidia-smi")        # a real one in /usr/bin may exist on a GPU runner
                self.assertFalse(found and Path(found).parent == self.tooldir)
        finally:
            proctree._trusted_path = old
        self.assertFalse(self.mark.exists())

    def test_wsl_nvidia_path_first_and_no_processes(self):
        with TrustedPath(""), mock.patch.object(gpu, "_is_wsl", return_value=True), \
                mock.patch.object(gpu, "WSL_SMI", str(self.tooldir / "nvidia-smi")):
            if not POSIX:  # pragma: no cover
                self.skipTest("WSL path is Linux-only")
            self.assertEqual(gpu.find_tool("nvidia-smi"), str(self.tooldir / "nvidia-smi"))
            d = gpu.detect()
            self.assertEqual(gpu.process_memory(), {})
        self.assertEqual(len(d["gpus"]), 2)
        self.assertEqual(d["processes"], [])
        self.assertTrue(all(g["processes"] == [] for g in d["gpus"]))
        self.assertIn(gpu.WSL_NOTE, d["errors"])
        self.assertEqual(gpu.note(d), gpu.WSL_NOTE)

    def test_version_probe_only_for_known_names(self):
        d = make_tool_dir("mytool", "print('mytool 9.9')\nopen(__import__('os').environ['FAKE_SMI_MARK'],'a')")
        self.addCleanup(shutil.rmtree, str(d), True)
        with TrustedPath(str(d)):
            w = sysinfo.which(["mytool", "nope"], node_path=str(d))
        self.assertEqual(w["found"]["mytool"]["version"], None)
        self.assertEqual(w["missing"], ["nope"])
        self.assertFalse(self.mark.exists())


class TestGpuParsers(unittest.TestCase):
    def test_lspci_amd_note(self):
        rows = gpu.parse_lspci(fixture("lspci_mm.txt"))
        self.assertEqual([r["vendor"] for r in rows], ["intel", "amd"])
        self.assertIn("Radeon RX 7900", rows[1]["name"])

    def test_no_gpu_path(self):
        with mock.patch.object(gpu, "find_tool", return_value=None), \
                mock.patch.object(gpu, "_lspci", return_value=[]), mock.patch.object(gpu, "IS_MAC", False), \
                mock.patch.object(gpu, "IS_WINDOWS", False):
            gpu.clear_cache()
            d = gpu.detect()
        self.assertEqual((d["backend"], d["gpus"], d["errors"]), (None, [], [gpu.NO_SMI_NOTE]))

    def test_lspci_fallback_with_amd(self):
        rows = gpu.parse_lspci(fixture("lspci_mm.txt"))
        with mock.patch.object(gpu, "find_tool", return_value=None), \
                mock.patch.object(gpu, "_lspci", return_value=rows), mock.patch.object(gpu, "IS_MAC", False), \
                mock.patch.object(gpu, "IS_WINDOWS", False):
            gpu.clear_cache()
            d = gpu.detect()
        gpu.clear_cache()
        self.assertEqual(d["backend"], "lspci")
        self.assertIn(gpu.AMD_NOTE, d["errors"])
        self.assertEqual(d["gpus"][1]["memory_total_mb"], 0)

    def test_apple_and_windows(self):
        rows = gpu.parse_system_profiler(json.loads(fixture("system_profiler_displays.json")), memsize_mb=32768)
        self.assertEqual((rows[0]["vendor"], rows[0]["name"], rows[0]["unified_memory"], rows[0]["memory_total_mb"]),
                         ("apple", "Apple M2 Pro", True, 32768))
        self.assertEqual(gpu.summary(rows), "Apple M2 Pro (unified 32GB)")
        w = gpu.parse_win_video([{"Name": "NVIDIA GeForce RTX 4090", "AdapterRAM": 4293918720, "DriverVersion": "32"},
                                 {"Name": "Microsoft Basic Display Adapter", "AdapterRAM": None}])
        self.assertEqual((w[0]["vendor"], w[0]["memory_total_mb"], w[1]["memory_total_mb"]), ("nvidia", 4095, 0))

    def test_failed_smi(self):
        d = make_tool_dir("nvidia-smi", "import sys\nprint('NVIDIA-SMI has failed because it could not communicate "
                                        "with the NVIDIA driver.')\nsys.exit(9)")
        self.addCleanup(shutil.rmtree, str(d), True)
        with TrustedPath(str(d)), mock.patch.object(gpu, "_lspci", return_value=[]), \
                mock.patch.object(gpu, "_is_wsl", return_value=False):
            r = gpu.detect()
        self.assertEqual(r["gpus"], [])
        self.assertTrue(r["errors"][0].startswith("nvidia-smi: NVIDIA-SMI has failed"))


@unittest.skipUnless(POSIX, "fake docker script uses a shebang")
class TestCapabilities(unittest.TestCase):
    def caps_with(self, script: str):
        d = make_tool_dir("docker", script)
        self.addCleanup(shutil.rmtree, str(d), True)
        with TrustedPath(str(d)), mock.patch("os.path.exists", side_effect=lambda p: False
                                             if p == "/etc/cdi/nvidia.yaml" else os.path.lexists(p)):
            return sysinfo.capabilities(refresh=True)

    def test_docker_daemon_down(self):
        caps = self.caps_with("import sys\nsys.stderr.write('Cannot connect to the Docker daemon')\nsys.exit(1)")
        self.assertEqual(set(caps), {"psutil", "nvidia_smi", "docker", "docker_gpu", "wsl"})
        self.assertFalse(caps["docker"])

    def test_docker_gpu_runtime(self):
        script = ("import sys\na=' '.join(sys.argv[1:])\n"
                  "print('{\"io.containerd.runc.v2\":{},\"nvidia\":{\"path\":\"nvidia-container-runtime\"}}' "
                  "if 'Runtimes' in a else '27.1.1')")
        caps = self.caps_with(script)
        self.assertTrue(caps["docker"])
        if LINUX:
            self.assertTrue(caps["docker_gpu"])

    def test_docker_without_gpu_runtime(self):
        caps = self.caps_with("import sys\na=' '.join(sys.argv[1:])\nprint('{\"runc\":{}}' if 'Runtimes' in a "
                              "else '27.1.1')")
        self.assertTrue(caps["docker"])
        if LINUX:
            self.assertIs(caps["docker_gpu"], False)

    def test_no_docker(self):
        with TrustedPath(tempfile.gettempdir() + "/hive-no-such-dir"):
            caps = sysinfo.capabilities(refresh=True)
        self.assertFalse(caps["docker"])
        self.assertEqual(caps["docker_gpu"], False if LINUX else None)


# ---------------------------------------------------------------------------------------------------------------
# wsl

class TestWsl(unittest.TestCase):
    def setUp(self):
        wsl.clear_cache()
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, str(self.d), True)

    @unittest.skipIf(util.IS_WINDOWS or util.IS_MAC, "Linux detection")
    def test_detection_fixtures(self):
        osr = self.d / "osrelease"
        osr.write_text("6.8.0-48-generic\n")
        with mock.patch.object(wsl, "_INTEROP", str(self.d / "WSLInterop")), \
                mock.patch.object(wsl, "_OSRELEASE", str(osr)):
            self.assertFalse(wsl.is_wsl())
            self.assertIsNone(wsl.wsl_info())
            osr.write_text(fixture("wsl_osrelease.txt"))
            self.assertTrue(wsl.is_wsl())
            self.assertFalse(wsl.interop_enabled())
            osr.write_text("6.8.0\n")
            (self.d / "WSLInterop").write_text("enabled\n")
            self.assertTrue(wsl.is_wsl())
            self.assertTrue(wsl.interop_enabled())

    @unittest.skipIf(util.IS_WINDOWS or util.IS_MAC, "Linux detection")
    def test_wsl_info(self):
        comm = self.d / "comm"
        comm.write_text("systemd\n")
        tool = make_tool_dir("wslinfo", "import sys\nprint('mirrored' if '--networking-mode' in sys.argv else '')")
        self.addCleanup(shutil.rmtree, str(tool), True)
        with mock.patch.object(wsl, "is_wsl", return_value=True), mock.patch.object(wsl, "_PID1_COMM", str(comm)), \
                mock.patch.dict(os.environ, {"WSL_DISTRO_NAME": "Ubuntu-24.04"}), TrustedPath(str(tool)):
            self.assertEqual(wsl.wsl_info(), {"distro": "Ubuntu-24.04", "networking": "mirrored", "systemd": True})
            wsl.clear_cache()
            comm.write_text("init\n")
            with mock.patch.object(wsl, "_wslinfo", return_value=None):
                self.assertEqual(wsl.wsl_info(), {"distro": "Ubuntu-24.04", "networking": None, "systemd": False})
        wsl.clear_cache()

    def test_list_parsing_utf16_and_utf8(self):
        text = "  NAME              STATE           VERSION\r\n* Ubuntu-24.04      Running         2\r\n" \
               "  docker-desktop    Stopped         2\r\n"
        want = [{"name": "Ubuntu-24.04", "state": "Running", "version": 2},
                {"name": "docker-desktop", "state": "Stopped", "version": 2}]
        self.assertEqual(wsl.parse_list_verbose(text.encode("utf-16-le")), want)
        self.assertEqual(wsl.parse_list_verbose(b"\xff\xfe" + text.encode("utf-16-le")), want)
        self.assertEqual(wsl.parse_list_verbose(text.encode("utf-8")), want)
        zh = "  名称              状态           版本\r\n* Ubuntu          正在运行         2\r\n"
        self.assertEqual(wsl.parse_list_verbose(zh.encode("utf-16-le")),
                         [{"name": "Ubuntu", "state": "Running", "version": 2}])
        self.assertEqual(wsl.parse_list_quiet("Ubuntu\r\nDebian\r\n".encode("utf-16-le")), ["Ubuntu", "Debian"])
        self.assertEqual(wsl.parse_list_quiet(b""), [])

    def test_not_windows_lists_nothing(self):
        if util.IS_WINDOWS:  # pragma: no cover
            self.skipTest("Windows lists real distros")
        self.assertEqual(wsl.list_distros(), [])
        self.assertFalse(wsl.has_distros())


@unittest.skipUnless(POSIX, "fake wsl.exe is a POSIX script here; the Windows CI runs the real spawn flags")
class TestKeepalive(unittest.TestCase):
    """§20.7 step 6 with a fake wsl.exe spawned through proctree.spawn_detached."""

    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="hiveka-"))
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.state = self.base / "state"
        self.log = self.base / "argv.log"
        self.tool = make_tool_dir("wsl.exe", "import os, sys, time\n"
                                             f"open({str(self.log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
                                             "time.sleep(60)\n")
        self.addCleanup(shutil.rmtree, str(self.tool), True)
        self.now = [1000.0]
        self.kas = []

    def tearDown(self):
        for ka in self.kas:
            for pid in ka.pids():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        proctree.reap_detached()

    def make(self):
        ka = wsl.Keepalive(self.state, wsl_exe=str(self.tool / "wsl.exe"), clock=lambda: self.now[0])
        self.kas.append(ka)
        return ka

    def test_start_restart_backoff_adopt(self):
        ka = self.make()
        ka.start([{"distro": "Ubuntu-24.04", "command": None}])
        st = ka.status()
        self.assertEqual(len(st), 1)
        self.assertTrue(st[0]["running"])
        pid = st[0]["pid"]
        self.assertEqual(ka.pids(), {pid: "wsl-keepalive:Ubuntu-24.04"})
        rec = json.loads((self.state / "wsl-keepalive-Ubuntu-24.04.json").read_text())
        self.assertEqual(rec["pid"], pid)
        helpers.wait_until(lambda: self.log.exists() and self.log.read_text().strip())
        self.assertEqual(self.log.read_text().split("\n")[0], "-d Ubuntu-24.04 --exec /bin/sleep infinity")
        # adoption: a new Keepalive (node restart) finds the live process and does not start a second one
        ka2 = self.make()
        ka2.start([{"distro": "Ubuntu-24.04"}])
        self.assertEqual(ka2.pids(), {pid: "wsl-keepalive:Ubuntu-24.04"})
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        # death -> restart after the backoff (10 s), doubling up to 300 s
        os.kill(pid, signal.SIGKILL)
        helpers.wait_until(lambda: not util.pid_alive(pid, rec["ctime"]) or proctree.reap_detached() or False, 5)
        ka2.check()
        self.assertEqual(ka2.status()[0]["running"], False)
        self.now[0] += 5
        ka2.check()
        self.assertFalse(ka2.status()[0]["running"])          # still inside the 10 s backoff
        self.now[0] += 6
        ka2.check()
        st = ka2.status()[0]
        self.assertTrue(st["running"])
        self.assertNotEqual(st["pid"], pid)
        self.assertEqual(ka2._entries["Ubuntu-24.04"]["backoff"], 20.0)

    def test_custom_command_and_missing_exe(self):
        ka = self.make()
        ka.start([{"distro": "Debian", "command": ["/bin/sh", "-lc", "exec $HOME/.hive/node/run-node.sh"]}])
        helpers.wait_until(lambda: self.log.exists() and self.log.read_text().strip())
        self.assertEqual(self.log.read_text().splitlines()[0],
                         "-d Debian --exec /bin/sh -lc exec $HOME/.hive/node/run-node.sh")
        bad = wsl.Keepalive(self.state, wsl_exe=str(self.base / "missing.exe"), clock=lambda: self.now[0])
        bad.start([{"distro": "X"}])
        self.assertEqual(bad.status(), [{"distro": "X", "pid": None, "running": False}])
        self.assertGreater(bad._entries["X"]["next_try"], self.now[0])


# ---------------------------------------------------------------------------------------------------------------
# handlers

class TestSystemInfoHandler(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all()

    def call(self, params, given=None, policy=None):
        ctx = helpers.make_ctx("system.info", policy=policy)
        raw = dict(params)
        p = schema.validate(catalog.get("system.info").params, raw)
        ctx.given = frozenset(given if given is not None else raw)
        res = HANDLERS["system.info"](ctx, p)
        validate_result("system.info", res)
        return res

    def test_default_sections_and_key_mapping(self):
        res = self.call({})
        self.assertEqual(set(res), {"os", "cpu", "memory", "disks", "gpus", "gpu_backend", "gpu_note"})
        res = self.call({"sections": ["network", "memory"]})
        self.assertEqual(set(res), {"network", "memory"})
        res = self.call({"sections": ["all"], "which": ["sh"]})
        self.assertEqual(set(res), {"os", "cpu", "memory", "disks", "gpus", "gpu_backend", "gpu_note", "network",
                                    "software", "which"})
        self.assertEqual(set(res["software"]), set(sysinfo.SOFTWARE))

    def test_which_alone(self):
        exe = "cmd" if util.IS_WINDOWS else "sh"
        res = self.call({"which": [exe, "definitely-not-here"]})
        self.assertEqual(set(res), {"which"})
        self.assertEqual(res["which"]["missing"], ["definitely-not-here"])
        self.assertIn(exe, res["which"]["found"])

    def test_which_uses_node_path_and_trusted_path(self):
        d = make_tool_dir("git", "print('git version 9.9.9')")
        self.addCleanup(shutil.rmtree, str(d), True)

        pol = helpers.FakeEffectivePolicy(helpers.identity(), workdir=Path(tempfile.gettempdir()),
                                          data_dir=Path(tempfile.gettempdir()))
        pol.node_path = str(d)
        with TrustedPath(""):                      # d is not trusted (inside an rw root)
            res = self.call({"which": ["git"]}, policy=pol)
        self.assertEqual(res["which"]["found"]["git"]["version"], None)
        self.assertEqual(res["which"]["found"]["git"]["note"], "in a writable root; not executed")
        with TrustedPath(str(d)):
            res = self.call({"which": ["git"]}, policy=pol)
            res2 = self.call({"which": ["git"], "versions": False}, policy=pol)
        self.assertEqual(res["which"]["found"]["git"]["version"], "git version 9.9.9")
        self.assertIsNone(res2["which"]["found"]["git"]["version"])


class TestProcHandlers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_all()

    def ctx(self, action, runtime=None, policy=None):
        return helpers.make_ctx(action, runtime=runtime, policy=policy)

    def test_proc_list_filter_sort_managed(self):
        p = subprocess.Popen(helpers.py_cmd("import time; time.sleep(30)  # hive-proc-list-marker"))
        self.addCleanup(p.wait)
        self.addCleanup(p.kill)
        rt = helpers.FakeRuntime()
        rt.managed_pids = lambda: {p.pid: "job:j_abcdef"}
        ctx = self.ctx("proc.list", runtime=rt)
        params = schema.validate(catalog.get("proc.list").params, {"filter": "HIVE-PROC-LIST-MARKER", "sort": "pid"})
        res = HANDLERS["proc.list"](ctx, params)
        validate_result("proc.list", res)
        self.assertEqual([r["pid"] for r in res["processes"]], [p.pid])
        row = res["processes"][0]
        self.assertEqual(row["managed"], "job:j_abcdef")
        self.assertIn("hive-proc-list-marker", row["cmd"])
        self.assertEqual((res["total"], res["shown"]), (1, 1))
        self.assertIn(res["method"], ("psutil", "proc", "ps", "toolhelp"))
        res = HANDLERS["proc.list"](ctx, schema.validate(catalog.get("proc.list").params,
                                                         {"limit": 3, "cmdline": False}))
        validate_result("proc.list", res)
        self.assertEqual(res["shown"], 3)
        self.assertTrue(all(r["cmd"] is None for r in res["processes"]))
        mems = [r["mem_bytes"] or 0 for r in res["processes"]]
        self.assertEqual(mems, sorted(mems, reverse=True))

    def test_proc_list_cmd_redacted_and_cut(self):
        rows = [{"pid": 10, "ppid": 1, "name": "x", "user": "u", "uid": 1, "cpu_pct": None, "mem_bytes": 1,
                 "create_time": 1700000000.0, "cmdline": ["x", "--token", "hv_" + "s" * 40] + ["y" * 50] * 10}]
        with mock.patch.object(procinfo, "list_processes", return_value=(rows, "proc")):
            res = HANDLERS["proc.list"](self.ctx("proc.list"),
                                        schema.validate(catalog.get("proc.list").params, {}))
        cmd = res["processes"][0]["cmd"]
        self.assertNotIn("s" * 40, cmd)
        self.assertLessEqual(len(cmd), 300)
        self.assertEqual(res["processes"][0]["started"], "2023-11-14T22:13:20.000Z")

    def _kill_ctx(self, runtime=None):
        from hive.policy import Policy  # noqa: F401 - real kill guard via a real runtime
        return self.ctx("proc.kill", runtime=runtime)

    @unittest.skipUnless(POSIX, "signals")
    def test_proc_kill_tree_and_single(self):
        p = subprocess.Popen(helpers.py_cmd("import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',"
                                            "'import time; time.sleep(60)']); time.sleep(60)"),
                             start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        helpers.wait_until(lambda: proctree.descendants(p.pid), 10)
        child = proctree.descendants(p.pid)[0]
        ctx = self.ctx("proc.kill")
        res = HANDLERS["proc.kill"](ctx, schema.validate(catalog.get("proc.kill").params, {"pid": p.pid,
                                                                                            "grace": 2}))
        validate_result("proc.kill", res)
        p.wait(5)
        self.assertEqual(sorted(res["killed"]), sorted([p.pid, child]))
        self.assertEqual(res["method"], "killpg")
        self.assertEqual(ctx.audit_extra["pid"], p.pid)
        q = subprocess.Popen(helpers.py_cmd("import time; time.sleep(60)"))
        self.addCleanup(lambda: q.poll() is None and q.kill())
        res = HANDLERS["proc.kill"](self.ctx("proc.kill"), schema.validate(
            catalog.get("proc.kill").params, {"pid": q.pid, "tree": False, "force": True}))
        q.wait(5)
        self.assertEqual((res["killed"], res["method"]), ([q.pid], "signal"))

    def test_proc_kill_gone(self):
        p = subprocess.Popen(helpers.py_cmd("pass"))
        p.wait()
        with self.assertRaises(HiveError) as cm:
            HANDLERS["proc.kill"](self.ctx("proc.kill"), {"pid": p.pid, "tree": True, "force": False, "grace": 1})
        self.assertEqual(cm.exception.code, "not_found")


class TestProcKillProtections(unittest.TestCase):
    """The real kill guard (WP1 EffectivePolicy.check_kill) with the protected set built by proc.kill."""

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

    def spawn(self, code="import time; time.sleep(60)", argv=None):
        p = subprocess.Popen(argv or helpers.py_cmd(code))
        self.addCleanup(p.wait)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        # right after fork /proc shows the parent's cmdline; wait for the exec
        helpers.wait_until(lambda: "time.sleep(60)" in " ".join((procinfo.process_info(p.pid) or {}).get("cmdline")
                                                                 or []), 5)
        return p

    def assertRefused(self, env, rule):
        self.assertFalse(env["ok"], env)
        self.assertEqual((env["error"]["code"], env["error"]["details"].get("rule")), ("policy_denied", rule))

    def test_node_and_ancestors(self):
        self.assertRefused(self.call(os.getpid()), "protected_process")
        self.assertRefused(self.call(os.getppid()), "protected_process")

    def test_managed_and_keepalive(self):
        p = self.spawn()
        with mock.patch.object(type(self.node), "managed_pids", lambda s: {p.pid: "service:vllm"}):
            env = self.call(p.pid)
        self.assertRefused(env, "protected_process")
        self.assertIn("service_control", env["error"]["hint"])
        with mock.patch.object(type(self.node), "keepalive_pids", lambda s: {p.pid: "wsl-keepalive:Ubuntu"}):
            self.assertRefused(self.call(p.pid), "protected_process")

    def test_hive_processes_and_watchdog(self):
        for word in ("node", "hub", "mcp", "_shim", "_download"):
            p = self.spawn(argv=[sys.executable, "-c", "import time; time.sleep(60)", "-m", "hive", word])
            self.assertRefused(self.call(p.pid), "kill_protect")
        p = self.spawn()
        run = Path(self.node.data_dir) / "run"
        run.mkdir(parents=True, exist_ok=True)
        (run / "watchdog.pid").write_text(f"{p.pid}\n")
        self.assertRefused(self.call(p.pid), "kill_protect")
        (run / "watchdog.pid").unlink()
        if POSIX:
            env = self.call(p.pid)
            self.assertTrue(env["ok"], env)
            self.assertIn(p.pid, env["result"]["killed"])

    def test_low_pids(self):
        env = self.call(1)            # pid 1 is also an ancestor of the node (protected_process)
        self.assertEqual(env["error"]["code"], "policy_denied")


if __name__ == "__main__":
    unittest.main()
