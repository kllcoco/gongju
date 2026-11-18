"""Phase 2 hardening review fixes (whole system): one regression test per confirmed finding."""
from __future__ import annotations

import errno
import hashlib
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List
from unittest import mock

from hive import _download, dist, gateway, httpserver, jobs, presets, services, textlog, util
from hive.errors import HiveError
from tests import helpers, wp1util
from tests import test_files as _tf
from tests import test_gateway as _tg
from tests import test_install as _ti
from tests.wp2util import PY, FakeRt
from tests.wp5util import FAST_RELAY, FakeRelay

LINUX = sys.platform.startswith("linux") and os.path.isdir("/proc/self")
POSIX = os.name != "nt"


# --------------------------------------------------------------------------------------------- 1 file binding
class TestBindNextToShellOperators(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivebind-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.d), True)
        (self.d / "run.sh").write_text("echo benign\n")
        self.pol = helpers.FakeEffectivePolicy(workdir=self.d)

    def bound(self, words):
        files, _lines, _nb = presets.bind_files(words, policy=self.pol, cwd=str(self.d), mask=lambda s: s)
        return [f["path"] for f in files if f["bind"] in ("sha256", "stat")]

    def test_metachar_next_to_script_still_binds(self):
        want = [str(self.d / "run.sh")]
        for cmd in ("bash run.sh;true", "bash run.sh|bash", "bash run.sh&&true", "(bash run.sh)", "bash <run.sh",
                    "bash run.sh>out.txt"):
            self.assertEqual(self.bound(presets.candidate_words(cmd, shell="bash")), want, cmd)
        self.assertEqual(self.bound(presets.candidate_words("cmd /c run.sh&calc", shell="cmd")), want)
        # quoted operators stay inside the word
        self.assertEqual(presets.candidate_words('echo "a;b"', shell="bash"), ["echo", "a;b"])

    def test_hub_split_mirrors_it(self):
        from hive.hub import Hub
        for os_name in ("Linux", None):
            self.assertIn("run.sh", Hub._split(None, "bash run.sh;true", None, os_name)[0])  # type: ignore[arg-type]
        self.assertIn(".\\run.ps1", Hub._split(None, "& .\\run.ps1;calc", None, "Windows")[0])  # type: ignore[arg-type]


# --------------------------------------------------------------------------------------------- 2 fs.search oracle
class TestSearchIsNoSecretOracle(_tf.FilesCase):
    def test_match_runs_on_masked_text(self):
        SECRET = _tf.SECRET
        self.store.set("HF_TOKEN", SECRET)
        self.fh.touch("proj/x.env", f"HF_TOKEN={SECRET}\nother=1\n")
        for pat, lit in ((SECRET[:6], True), (SECRET[:4] + "Q", True), ("hf_Q+", False), (SECRET, True)):
            r = self.call("fs.search", {"pattern": pat, "path": "proj", "literal": lit}, ident=wp1util.READONLY)
            self.assertEqual((r["matches"], r["files_matched"]), ([], 0), pat)
        r = self.call("fs.search", {"pattern": "HF_TOKEN=", "path": "proj", "literal": True}, ident=wp1util.READONLY)
        self.assertEqual(len(r["matches"]), 1)
        self.assertNotIn(SECRET, json.dumps(r))


# --------------------------------------------------------------------------------------------- 3 gateway status
class TestGatewayStatusClamp(_tg._GwCase):
    setUp = _tg.TestHubGateway.setUp
    _relay_roundtrip = _tg.TestHubGateway._relay_roundtrip

    def test_upstream_status_values(self):
        ok = gateway.upstream_status
        self.assertEqual([ok(200), ok(400), ok(599), ok("201")], [200, 400, 599, 201])
        for bad in (101, 204, 304, 99999, 199, 600, True, None, "x", 2.5, -1):
            self.assertIsNone(ok(bad), bad)

    def test_relay_bodyless_or_bad_status_is_502(self):
        for st in (204, 101, 304, 99999):
            _t, (status, _h, body) = self._relay_roundtrip({"model": "qwen"}, {"forged": True}, status=st)
            self.assertEqual(status, 502, st)
            self.assertEqual(json.loads(body)["error"]["type"], "upstream_error")

    def test_write_never_sends_a_body_after_204(self):
        sent: List[bytes] = []
        conn = SimpleNamespace(send=sent.append)
        srv = object.__new__(httpserver.ApiServer)
        srv._write(conn, 204, {}, b'{"forged":"reply"}', close=False)
        head, _sep, body = sent[0].partition(b"\r\n\r\n")
        self.assertTrue(head.startswith(b"HTTP/1.1 204 "))
        self.assertEqual(body, b"")
        self.assertIn(b"Connection: close", head)
        self.assertNotIn(b"Content-Length", head)
        sent.clear()
        srv._write(conn, 99999, {}, b"x", close=False)
        self.assertTrue(sent[0].startswith(b"HTTP/1.1 502 "))


# --------------------------------------------------------------------------------------------- 4 relay node_id
class TestRelayNodeIdBinding(unittest.TestCase):
    def setUp(self):
        self.ta, self.tb = util.new_token(), util.new_token()
        nodes = [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(self.ta)},
                 {"name": "pod-b", "mode": "relay", "token_sha256": util.hash_token(self.tb)}]
        self.url, self.toks, self.hub = helpers.start_hub(nodes, relay=dict(FAST_RELAY))
        self.addCleanup(self.hub.test_stop)

    def test_bound_node_id_survives_offline_link(self):
        b = FakeRelay(self.url, self.tb, "pod-b")
        b.register(node_id="n_podbreal1234")
        b.t.close() if hasattr(b.t, "close") else None
        self.assertIs(self.hub.resolve("n_podbreal1234"), self.hub.links["pod-b"])
        self.assertEqual(self.hub.relay_ids.get("pod-b").get("node_id"), "n_podbreal1234")
        self.hub.links["pod-b"]._node_id = ""          # as after a hub restart, before pod-b registers again
        a = FakeRelay(self.url, self.ta, "pod-a")
        a.register(node_id="n_podbreal1234")
        if hasattr(a.t, "close"):
            a.t.close()
        self.assertEqual(self.hub.links["pod-a"].node_id, "")
        self.assertIsNot(self.hub.resolve("n_podbreal1234"), self.hub.links["pod-a"])
        # pod-b's real instance comes back and keeps its id
        b.register(node_id="n_podbreal1234")
        self.assertIs(self.hub.resolve("n_podbreal1234"), self.hub.links["pod-b"])

    def test_foreign_key_cannot_change_bound_node_id(self):
        b = FakeRelay(self.url, self.tb, "pod-b")
        b.register(node_id="n_podbreal1234")
        link = self.hub.links["pod-b"]
        with link.lock:
            link.last_poll_end = time.monotonic() - 3600      # offline: a foreign key is accepted as pending
            link.last_seen_wall = time.time() - 3600
            link.polling = 0
        evil = FakeRelay(self.url, self.tb, "pod-b")
        try:
            evil.register(node_id="n_eviltakeover1")
        except HiveError:
            self.skipTest("the link still counts as online in this build")
        self.assertTrue(link.identity_changed)
        self.assertEqual(link.node_id, "")
        self.assertEqual(self.hub.relay_ids.get("pod-b").get("node_id"), "n_podbreal1234")


# --------------------------------------------------------------------------------------------- 5 id patterns
class TestIdParamNewline(unittest.TestCase):
    def setUp(self):
        self.rtok = util.new_token()
        nodes = [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(self.rtok)}]
        self.url, self.toks, self.hub = helpers.start_hub(
            nodes, relay=dict(FAST_RELAY),
            extra_tokens=[("models", {"name": "app", "kind": "models", "models": ["pod-a/*"]})])
        self.addCleanup(self.hub.test_stop)

    def test_trailing_newline_is_400(self):
        for method, path, tok in (("POST", "/v1/approvals/ap_abcdefgh%0A/approve", "owner"),
                                  ("POST", "/v1/hub/nodes/pod-a%0A/restart", "owner"),
                                  ("GET", "/openai/pod-a/qwen%0A/v1/models", "models")):
            st, _h, body = _tg.req(self.url, method, path, token=self.toks[tok], body={} if method == "POST" else None)
            self.assertEqual(st, 400, (path, body))
        for pat in httpserver.ID_PARAM_PATTERNS.values():
            self.assertNotIn("$", pat)


# --------------------------------------------------------------------------------------------- 6 join codes
class TestNoPlaintextPublicJoinCode(unittest.TestCase):
    def hub_cfg(self, host):
        tls = SimpleNamespace(cert=None, key=None)
        return SimpleNamespace(listen=SimpleNamespace(host=host, port=7700, tls=tls), public_url=None)

    def test_public_ip_refused_private_ok(self):
        with self.assertRaises(HiveError) as cm:
            dist.add_node_instructions(self.hub_cfg("203.0.113.7"), "pod-a", "hv_" + "t" * 40)
        self.assertEqual(cm.exception.code, "bad_request")
        self.assertIn("plaintext", cm.exception.message)
        out = dist.add_node_instructions(self.hub_cfg("192.168.1.20"), "pod-a", "hv_" + "t" * 40)
        self.assertEqual(dist.decode_join(out["codes"]["T"])["u"], "http://192.168.1.20:7700")


class TestStartHivePublicPlaintext(_ti.TestStartHive):
    def test_public_http_refused_without_pinned_sha(self):
        code = dist.encode_join({"u": "http://203.0.113.7:7700", "t": "hv_" + "t" * 40, "p": None, "n": "pod-a"})
        r = self.run_script(HIVE_JOIN=code)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("plain http", r.stderr)
        self.assertFalse((self.ws / ".hive" / "hive.pyz").exists())
        self.assertFalse(self.log.exists())


for _n in [n for n in dir(_ti.TestStartHive) if n.startswith("test_")]:
    setattr(TestStartHivePublicPlaintext, _n, None)        # run only the new test here


# --------------------------------------------------------------------------------------------- 7 clock steps
@unittest.skipUnless(LINUX, "Linux /proc create times")
class TestCreateTimeIgnoresClockSteps(unittest.TestCase):
    def test_same_ctime_after_btime_moves(self):
        me = os.getpid()
        ct = util.process_create_time(me)
        key = util.boot_key()
        real = util._linux_btime()
        with mock.patch.object(util, "_linux_btime", return_value=real + 5):
            self.assertAlmostEqual(util.process_create_time(me), ct, places=3)
            self.assertTrue(util.pid_alive(me, ct))
            self.assertEqual(util.boot_key(), key)
            with mock.patch.object(util, "_BTIME_CACHE", None):      # a fresh process of the same boot
                self.assertAlmostEqual(util.process_create_time(me), ct, places=3)

    def test_other_process_after_clock_step_agrees(self):
        ct = util.process_create_time(os.getpid())
        code = ("import sys; from hive import util; real = util._linux_btime(); "
                "util._linux_btime = lambda: real + 100; print(repr(util.process_create_time(int(sys.argv[1]))))")
        env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
        out = subprocess.run([sys.executable, "-c", code, str(os.getpid())], capture_output=True, text=True,
                             env=env, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertAlmostEqual(float(out.stdout.strip()), ct, places=2)

    def test_job_stop_after_clock_step(self):
        rt = FakeRt()
        rt.jobs = jobs.JobManager(rt)
        rt.jobs.start()
        self.addCleanup(rt.cleanup)
        self.addCleanup(rt.jobs.shutdown)
        ctx = helpers.make_ctx("job.start", runtime=rt)
        info = rt.jobs.start_job(ctx, kind="job", argv_or_cmdline=[PY, "-c", "import time; time.sleep(60)"],
                                 shell="none", cwd=rt.workdir, env=dict(os.environ), stdin=None, name=None,
                                 timeout=0, merge_stderr=True, log_max_bytes=1 << 20)
        jid = info["job_id"]
        helpers.wait_until(lambda: rt.jobs.record(jid).get("state") == "running", 10)
        real = util._linux_btime()
        with mock.patch.object(util, "_linux_btime", return_value=real + 50), \
                mock.patch.object(util, "_BTIME_CACHE", None):
            rec = rt.jobs.record(jid)
            self.assertTrue(util.pid_alive(int(rec["shim_pid"]), rec.get("shim_ctime")))
            rt.jobs.stop(jid, force=True)
            helpers.wait_until(lambda: rt.jobs.record(jid).get("state") not in ("starting", "running"), 20)


# --------------------------------------------------------------------------------------------- 8 rotation seqlock
class TestRotationSeqlock(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivelog-"))
        self.addCleanup(shutil.rmtree, str(self.d), True)
        self.log = self.d / "x.log"

    def test_reader_during_rotation_never_goes_back(self):
        w = textlog.RotatingWriter(self.log, max_bytes=4096 * 3, keep=2)
        self.addCleanup(w.close)
        line = b"0123456789" * 9 + b"\n"
        total = 0
        while total + len(line) <= w.limit:
            w.write(line)
            total += len(line)
        seen: dict = {}
        real = util.replace_with_retry

        def slow_replace(src, dst, *a, **kw):
            real(src, dst, *a, **kw)
            if str(dst).endswith(".log.1"):          # x.log is gone, the new state is not written yet
                t = threading.Thread(target=lambda: seen.update(r=textlog.read_range(self.log, total, 4096, None)))
                t.start()
                time.sleep(0.3)
                seen["t"] = t
        with mock.patch.object(util, "replace_with_retry", slow_replace):
            w.write(line)                             # rotates
        seen["t"].join(10)
        r = seen["r"]
        self.assertGreaterEqual(r["next_offset"], total)
        self.assertGreaterEqual(r["size"], total)
        self.assertEqual(textlog.log_size(self.log), total + len(line))

    def test_writer_crash_mid_rotation_is_settled(self):
        w = textlog.RotatingWriter(self.log, max_bytes=8192, keep=1)
        w.write(b"a" * 100)
        w.close()
        os.replace(self.log, self.d / "x.log.1")
        util.write_json_atomic(Path(str(self.log) + ".state"), {"v": 1, "rotated_bytes": 0, "seq": 1,
                                                                 "rotating": 100})
        with mock.patch.object(textlog, "ROTATION_WAIT_S", 0.05):
            self.assertEqual(textlog.log_size(self.log), 100)
        w2 = textlog.RotatingWriter(self.log, max_bytes=8192, keep=1)
        w2.write(b"b")
        w2.close()
        st = json.loads(Path(str(self.log) + ".state").read_text())
        self.assertEqual((st["rotated_bytes"], st["seq"] % 2), (100, 0))
        self.assertEqual(textlog.read_range(self.log, 100, 10, None)["data"], b"b")

    def test_offset_beyond_size_keeps_offset(self):
        self.log.write_bytes(b"abc")
        r = textlog.read_range(self.log, 10, 100, None)
        self.assertEqual((r["data"], r["next_offset"]), (b"", 10))


# --------------------------------------------------------------------------------------------- 9 GBK split
class TestGbkSplitBoundaries(unittest.TestCase):
    TEXT = "".join(f"中文输出测试第{i}行，编译完成。\n" for i in range(60))

    def test_decode_chunk_holds_back_lead_byte(self):
        data = self.TEXT.encode("gbk")
        with mock.patch.object(util, "IS_WINDOWS", True):
            pos, out = 0, []
            while pos < len(data):
                text, used = textlog.decode_chunk(data[pos:pos + 33])
                out.append(text)
                pos += used if used else 33
            self.assertEqual("".join(out), self.TEXT)
            self.assertEqual(util.decode_text(data[1:]).count("�"), 1)

    def test_shell_truncation_and_job_logs_clean_at_every_alignment(self):
        from hive.actions import shell
        d = Path(tempfile.mkdtemp(prefix="hivegbk-"))
        self.addCleanup(shutil.rmtree, str(d), True)
        p = d / "output.log"
        p.write_bytes(self.TEXT.encode("gbk"))
        with mock.patch.object(util, "IS_WINDOWS", True):
            for mo in range(300, 340):
                text, _n, trunc = shell._stream_text(p, mo)
                self.assertTrue(trunc)
                self.assertNotIn("�", text, mo)
            for off in range(1, 40):
                r = jobs.read_log(d, {"output": "output.log"}, lambda: True, offset=off, max_bytes=64)
                self.assertLessEqual(r["text"].count("�"), 0, (off, r["text"]))


# --------------------------------------------------------------------------------------------- 10 corrupt records
class TestCorruptRecords(unittest.TestCase):
    def test_bad_job_json_is_skipped(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        bad = Path(rt.state_dir) / "jobs" / "j_badbad01"
        bad.mkdir(parents=True)
        (bad / "job.json").write_bytes(b"\0" * 64)
        jm = jobs.JobManager(rt)
        jm.start()
        self.addCleanup(jm.shutdown)
        self.assertTrue((bad / "job.json.corrupt").exists())
        self.assertNotIn("j_badbad01", jm._jobs)

    def test_bad_services_json_is_kept_aside(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        rt.warnings = []
        sdir = Path(rt.state_dir)
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "services.json").write_bytes(b"")
        sm = services.ServiceManager(rt)
        sm.start()
        self.addCleanup(sm.shutdown)
        self.assertFalse((sdir / "services.json").exists())
        self.assertTrue(list(sdir.glob("services.json.corrupt-*")))
        self.assertTrue(any("services.json was unreadable" in w for w in rt.warnings))


# --------------------------------------------------------------------------------------------- 11 save after remove
class TestSaveAfterRemove(unittest.TestCase):
    def test_removed_job_dir_not_recreated(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        jm = jobs.JobManager(rt)
        rd = jm.dir / "j_racerace1"
        rd.mkdir(parents=True)
        rec = {"job_id": "j_racerace1", "state": "exited"}
        jm._jobs["j_racerace1"] = rec
        real = util.write_json_atomic

        def racing(path, obj, *a, **kw):
            with jm._lock:
                jm._jobs.pop("j_racerace1", None)
            shutil.rmtree(str(rd), ignore_errors=True)
            return real(path, obj, *a, **kw)
        with mock.patch.object(util, "write_json_atomic", racing):
            jm._save(rec)
        self.assertFalse(rd.exists())


# --------------------------------------------------------------------------------------------- 12 future restarts
class TestFutureRestartStamps(unittest.TestCase):
    def test_future_stamps_do_not_count(self):
        now = time.time()
        future = [util.iso_utc(now + 8 * 3600 + i) for i in range(5)]
        recent = [util.iso_utc(now - 10)]
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        sm = services.ServiceManager(rt)
        self.assertEqual(sm._restarts_in_window({"restart_window": 600}, {"restarts": future + recent}), 1)
        self.assertEqual(services._recent_restarts(future + recent, float("inf")), recent)


# --------------------------------------------------------------------------------------------- 13/14 downloads
class _Prog:
    def add(self, n, downloaded=True):
        pass


class TestDownloadRetry(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="hivedl2-"))
        self.addCleanup(shutil.rmtree, str(self.d), True)

    def test_retry_resumes_without_rehashing(self):
        data = os.urandom(100_000)
        calls = {"n": 0}

        def fake_open(url, guard=None, headers=None, auth=None, range_start=0, cancel=None):
            calls["n"] += 1
            if calls["n"] == 1:
                def broken():
                    yield data[:40_000]
                    raise ConnectionResetError("reset by peer")
                return 200, {"Content-Length": str(len(data))}, url, broken()
            self.assertEqual(range_start, 40_000)
            return 206, {"Content-Range": f"bytes 40000-{len(data) - 1}/{len(data)}"}, url, iter([data[40_000:]])
        part = self.d / "f.bin.hive-part"
        cp: dict = {}
        kw = dict(guard=None, auth=None, headers={}, max_size=len(data), hasher_factory=hashlib.sha256,
                  progress=_Prog(), stop=threading.Event(), checkpoint=cp)
        with mock.patch.object(_download.netutil, "open_download", fake_open):
            with self.assertRaises(OSError):
                _download._fetch("http://x/f.bin", part, resume=False, **kw)
            with mock.patch.object(_download, "_hash_file", side_effect=AssertionError("re-hashed")):
                h, size, _rh, _f = _download._fetch("http://x/f.bin", part, resume=True, **kw)
        self.assertEqual((size, h.hexdigest()), (len(data), hashlib.sha256(data).hexdigest()))
        # a part changed on disk since the checkpoint is hashed again
        cp2: dict = {"h": hashlib.sha256(b"zz"), "size": 40_000, "sig": (40_000, 1)}
        self.assertIsNone(_download._checkpoint_hasher(cp2, part, 40_000))

    def test_disk_full_is_not_transient(self):
        e = OSError(errno.ENOSPC, "No space left on device")
        self.assertFalse(_download._transient(e))
        self.assertTrue(_download._transient(ConnectionResetError("x")))
        self.assertTrue(util.is_disk_full(e))

    def test_pull_disk_full_is_exit_disk(self):
        p = object.__new__(_download.Puller)
        p.spec = {"source": "huggingface", "endpoint": "http://x", "repo": "a/b", "repo_type": "model",
                  "commit": "c" * 40}
        p.target = self.d
        p.guard = p.auth = None
        p.stop = threading.Event()
        p.lock = threading.Lock()
        p.progress = _download.Progress(0)
        p.failures = []
        with mock.patch.object(_download, "_fetch", side_effect=OSError(errno.ENOSPC, "No space left")), \
                mock.patch.object(_download, "_sleep", side_effect=AssertionError("retried")):
            with self.assertRaises(_download.Failure) as cm:
                p._download_one({"path": "m.safetensors", "size": 10, "sha256": "0" * 64}, self.d / "m.safetensors")
        self.assertEqual(cm.exception.code, _download.EXIT_DISK)

    def test_start_job_disk_full_is_too_large(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        jm = jobs.JobManager(rt)
        ctx = helpers.make_ctx("job.start", runtime=rt)
        with mock.patch.object(util, "atomic_write_bytes", side_effect=OSError(errno.ENOSPC, "No space left")):
            with self.assertRaises(HiveError) as cm:
                jm.start_job(ctx, kind="job", argv_or_cmdline=[PY, "-c", "pass"], shell="none", cwd=rt.workdir,
                             env={}, stdin="x", name=None, timeout=0, merge_stderr=True, log_max_bytes=1 << 20)
        self.assertEqual((cm.exception.code, cm.exception.hint), ("too_large", "disk full"))


# --------------------------------------------------------------------------------------------- 15 autostart vs stop
class TestAutostartAfterShutdown(unittest.TestCase):
    def test_no_spawn_after_shutdown(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        sm = services.ServiceManager(rt)
        spawned: List[str] = []
        gate = threading.Event()

        def slow_autostart(names):
            gate.wait(5)
            for n in names:
                if not sm._stop.is_set():
                    spawned.append(n)
        sm._autostart_thread = threading.Thread(target=slow_autostart, args=(["a"],), daemon=True)
        sm._autostart_thread.start()
        threading.Timer(0.2, gate.set).start()
        sm.shutdown()
        self.assertFalse(sm._autostart_thread is not None and sm._autostart_thread.is_alive())
        self.assertEqual(spawned, [])


if __name__ == "__main__":
    unittest.main()
