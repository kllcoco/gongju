"""WP2 ServiceManager + service.* tests (§9.6, §10.5, §21.2 jobs/services row): health (http via the _http_status
seam, tcp, ready_pattern), start_period, unhealthy restarts, backoff, max_restarts, start-only-waits, status wait,
remove pending, port preflight, adoption and the boot rule, exec-disabled and paused spawns, define changes, env
redaction, stop_command (also in panic_offline with the node down), endpoint_for, policy_view, container rules with a
fake docker, approval binding of service.define/restart, result schemas."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import presets, proctree, services, util
from hive.errors import HiveError
from hive.netutil import auth_host_allowed
from tests import helpers
from tests.wp2util import CAUTIOUS, OPERATOR, PY, FakeRt, Node, assert_result_schema, wait_for

POSIX = os.name != "nt"
SLEEPER = [PY, "-c", "import time\nprint('started', flush=True)\nwhile True: time.sleep(1)"]


def server_argv(port: int) -> list:
    code = ("import http.server\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "  def do_GET(self):\n"
            "    self.send_response(200); self.end_headers(); self.wfile.write(b'ok')\n"
            "  def log_message(self, *a): pass\n"
            f"print('serving', flush=True)\nhttp.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n")
    return [PY, "-c", code]


class SvcCase(unittest.TestCase):
    preset = "standard"
    overrides: dict = {}

    def setUp(self):
        for attr, val in (("TICK", 0.2), ("NO_HEALTH_ALIVE_S", 0.5)):
            p = mock.patch.object(services, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.node = Node(self.preset, **self.overrides)
        self.node.__enter__()
        self.addCleanup(self.node.__exit__, None, None, None)
        self.n = self.node

    @property
    def sm(self):
        return self.n.rt.services

    def define(self, **kw):
        kw.setdefault("health_interval", 2)
        r = self.n.ok("service.define", kw)
        assert_result_schema(self, "service.define", r)
        return r

    def state(self, name):
        return self.sm.runtime(name)["state"]

    def wait_state(self, name, *states, timeout=20):
        return wait_for(lambda: self.state(name) in states and self.state(name), timeout)


class TestHealth(SvcCase):
    def test_http_health_via_seam(self):
        answers = iter([503, 503] + [200] * 100)
        with mock.patch.object(services, "_http_status", side_effect=lambda u, t, g: next(answers)) as hs:
            r = self.define(name="web", argv=SLEEPER, health_url="http://127.0.0.1:9/health")
            self.assertEqual(r["state"], "starting")
            self.assertEqual(r["health"], "starting")
            self.wait_state("web", "running")
            self.assertGreaterEqual(hs.call_count, 3)
            url, _timeout, _guard = hs.call_args[0]
            self.assertEqual(url, "http://127.0.0.1:9/health")
        st = self.n.ok("service.status", {"name": "web"})
        assert_result_schema(self, "service.status", st)
        self.assertEqual((st["health"], st["health_detail"]["consecutive_failures"]), ("healthy", 0))
        self.assertTrue(st["resources"]["pids"])

    def test_tcp_health_real_port(self):
        port = helpers.free_port()
        r = self.define(name="tcp", argv=server_argv(port), port=port, wait=15)
        self.assertEqual((r["state"], r["health"]), ("running", "healthy"))
        self.assertEqual(self.sm._health_kind(self.sm._defs["tcp"]), "tcp")

    def test_http_health_real_server_p0_fallback(self):
        port = helpers.free_port()
        r = self.define(name="http", argv=server_argv(port), port=port, endpoint=f"http://127.0.0.1:{port}/v1",
                        wait=15)
        self.assertEqual((r["state"], r["health"]), ("running", "healthy"))

    def test_start_period_then_unhealthy_restart(self):
        with mock.patch.object(services, "_http_status", return_value=None):
            self.define(name="slow", argv=SLEEPER, health_url="http://127.0.0.1:9/", start_period=1,
                        restart_on_unhealthy=True, restart_delay=0, max_restarts=1, restart_window=600)
            wait_for(lambda: self.sm.runtime("slow")["restarts"], 20)
            self.wait_state("slow", "failed", timeout=30)
        rt = self.sm.runtime("slow")
        self.assertIn("giving up", rt["last_error"])

    def test_ready_pattern(self):
        with mock.patch.object(services, "_http_status", return_value=None):
            r = self.define(name="rp", argv=[PY, "-c", "import time\ntime.sleep(0.5)\nprint('Uvicorn READY now', "
                                                       "flush=True)\nwhile True: time.sleep(1)"],
                            health_url="http://127.0.0.1:9/", ready_pattern=r"READY\b", wait=15)
        self.assertEqual((r["state"], r["health"]), ("running", "healthy"))

    def test_running_turns_unhealthy(self):
        flag = {"ok": True}
        with mock.patch.object(services, "_http_status", side_effect=lambda u, t, g: 200 if flag["ok"] else None):
            self.define(name="flaky", argv=SLEEPER, health_url="http://127.0.0.1:9/", unhealthy_threshold=1, wait=15)
            self.assertEqual(self.state("flaky"), "running")
            flag["ok"] = False
            self.wait_state("flaky", "unhealthy", timeout=20)
            flag["ok"] = True
            self.wait_state("flaky", "running", timeout=20)

    def test_no_health_alive_3s(self):
        r = self.define(name="plain", argv=SLEEPER, wait=15)
        self.assertEqual((r["state"], r["health"]), ("running", "none"))


class TestLifecycle(SvcCase):
    def test_stop_start_restart_logs(self):
        self.define(name="s", argv=SLEEPER, wait=10)
        st = self.n.ok("service.stop", {"name": "s"})
        assert_result_schema(self, "service.stop", st)
        self.assertEqual((st["state"], st["desired"]), ("stopped", "stopped"))
        st = self.n.ok("service.start", {"name": "s", "wait": 10})
        assert_result_schema(self, "service.start", st)
        self.assertEqual(st["state"], "running")
        pid = st["pid"]
        st = self.n.ok("service.restart", {"name": "s", "wait": 10})
        self.assertEqual(st["state"], "running")
        self.assertNotEqual(st["pid"], pid)
        lg = self.n.ok("service.logs", {"name": "s"})
        assert_result_schema(self, "service.logs", lg)
        self.assertIn("started", lg["text"])
        self.assertEqual(lg["status"]["state"], "running")
        lst = self.n.ok("service.list", {})
        assert_result_schema(self, "service.list", lst)
        self.assertEqual([x["name"] for x in lst["services"]], ["s"])
        self.assertEqual(self.n.rt.managed_pids().get(st["pid"]), "service:s")

    def test_start_while_starting_only_waits(self):
        with mock.patch.object(services, "_http_status", return_value=None):
            self.define(name="w", argv=SLEEPER, health_url="http://127.0.0.1:9/", start=False)
            self.assertEqual(self.state("w"), "stopped")
            self.n.ok("service.start", {"name": "w"})
            rt1 = self.sm.runtime("w")
            self.assertEqual(rt1["state"], "starting")
            with self.sm._lock:
                self.sm._rs["w"]["restarts"] = [util.iso_utc()]
            self.n.ok("service.start", {"name": "w"})
            rt2 = self.sm.runtime("w")
            self.assertEqual(rt2["shim_pid"], rt1["shim_pid"])     # no spawn
            self.assertEqual(len(rt2["restarts"]), 1)              # no counter reset

    def test_status_wait(self):
        port = helpers.free_port()
        self.define(name="sw", argv=[PY, "-c", f"import time; time.sleep(1.5)\nexec({server_argv(port)[2]!r})"],
                    port=port, health_interval=2)
        t0 = time.monotonic()
        st = self.n.ok("service.status", {"name": "sw", "wait": 20})
        self.assertEqual((st["state"], st["health"]), ("running", "healthy"))
        self.assertLess(time.monotonic() - t0, 15)

    def test_crash_backoff_and_max_restarts(self):
        r = self.define(name="crash", command="echo boom; exit 2" if POSIX else "Write-Output boom; exit 2",
                        restart_delay=0.2, max_restarts=2, restart_window=600)
        self.wait_state("crash", "failed", timeout=30)
        st = self.n.ok("service.status", {"name": "crash"})
        self.assertIn("giving up", st["last_error"])
        self.assertEqual(st["restarts"], 2)
        self.assertEqual(st["last_exit_code"], 2)
        self.assertIn("boom", st["tail"])                          # failed start shows the tail
        _ = r

    def test_restart_no_and_always(self):
        self.define(name="once", argv=[PY, "-c", "print('bye')"], restart="no")
        self.wait_state("once", "stopped")
        self.assertEqual(self.sm.runtime("once")["last_exit_code"], 0)
        self.define(name="loop", argv=[PY, "-c", "print('bye')"], restart="always", restart_delay=0.1,
                    max_restarts=1)
        self.wait_state("loop", "failed", timeout=20)

    def test_remove_pending_then_deleted(self):
        code = ("import signal,time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nprint('up', flush=True)\n"
                "while True: time.sleep(1)")
        self.define(name="stubborn", argv=[PY, "-c", code], stop_timeout=1, wait=10)
        r = self.n.ok("service.remove", {"name": "stubborn", "wait": 0})
        assert_result_schema(self, "service.remove", r)
        self.assertEqual(r, {"removed": "stubborn", "pending": True, "state": "stopping"})
        row = [x for x in self.n.ok("service.list", {})["services"] if x["name"] == "stubborn"][0]
        self.assertEqual(row["desired"], "removed")
        e = self.n.err("service.define", {"name": "stubborn", "argv": SLEEPER})
        self.assertEqual(e["code"], "conflict")
        self.assertIn("being removed", e["message"])
        wait_for(lambda: "stubborn" not in self.sm._defs, 30)
        self.assertEqual(self.n.ok("service.list", {})["services"], [])
        self.assertNotIn("stubborn", json.loads(self.sm.defs_path.read_text())["services"])

    def test_remove_within_wait(self):
        self.define(name="gone", argv=SLEEPER, wait=10)
        self.assertEqual(self.n.ok("service.remove", {"name": "gone", "delete_logs": True}),
                         {"removed": "gone", "pending": False})
        self.assertFalse((self.sm.dir / "gone").exists())

    def test_port_preflight(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        self.addCleanup(s.close)
        port = s.getsockname()[1]
        e = self.n.err("service.define", {"name": "clash", "argv": SLEEPER, "port": port, "health": "none"})
        self.assertEqual(e["code"], "conflict")
        self.assertIn(f"port {port} in use", e["message"])
        self.assertEqual(self.state("clash"), "failed")
        r = self.define(name="clash", argv=SLEEPER, port=port, health="none", force_port=True, wait=10)
        self.assertEqual(r["state"], "running")

    def test_define_change_restarts_and_redacts(self):
        r = self.define(name="cfg", argv=SLEEPER, env=["MODE=a", "MY_API_KEY=supersecretvalue"], wait=10)
        self.assertTrue(r["created"])
        self.assertEqual(r["definition"]["env"], ["MODE", "MY_API_KEY"])
        self.assertNotIn("supersecretvalue", json.dumps(r))
        self.assertIn(services.SECRET_ENV_WARNING.format(k="MY_API_KEY"), r["warnings"])
        stored = json.loads(self.sm.defs_path.read_text())["services"]["cfg"]
        self.assertEqual(stored["env"]["MY_API_KEY"], "supersecretvalue")   # values live in services.json (0600)
        if POSIX:
            self.assertEqual(self.sm.defs_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(stored["defined_by"], OPERATOR.display_name)
        pid = r["pid"]
        r2 = self.define(name="cfg", argv=SLEEPER, env=["MODE=b", "MY_API_KEY=supersecretvalue"], wait=10)
        self.assertEqual((r2["created"], r2["changed"], r2["restarted"]), (False, ["env"], True))
        self.assertNotEqual(r2["pid"], pid)
        e = self.n.err("service.define", {"name": "cfg", "argv": SLEEPER, "replace": False})
        self.assertEqual(e["code"], "conflict")

    def test_stop_command_runs_at_stop(self):
        marker = self.n.home / "stopped.txt"
        self.define(name="sc", argv=SLEEPER, stop_command=[PY, "-c", f"open({str(marker)!r},'w').write('x')"],
                    stop_timeout=1, wait=10)
        self.n.ok("service.stop", {"name": "sc", "wait": 20})
        self.assertTrue(marker.exists())
        self.assertIn("[hive] stop_command exit 0", self.n.ok("service.logs", {"name": "sc"})["text"])

    def test_define_validation(self):
        for params, frag in (({"argv": ["wsl.exe", "-d", "Ubuntu"]}, "wsl.exe"),
                             ({"command": "wsl -d Ubuntu -- vllm serve"}, "wsl.exe"),
                             ({"argv": SLEEPER, "endpoint": "http://127.0.0.1:8000/v1?x=1"}, "query"),
                             ({"argv": SLEEPER, "health_url": "http://user:pw@127.0.0.1:8000/"}, "credentials"),
                             ({"argv": SLEEPER, "ready_pattern": "(unclosed"}, "regex"),
                             ({"argv": SLEEPER, "api_key_env": "NOT_A_SECRET"}, "api_key_env")):
            e = self.n.err("service.define", dict(params, name="bad"))
            self.assertIn(e["code"], ("bad_request", "not_found"), params)
            self.assertIn(frag, e["message"].lower() if frag != "wsl.exe" else e["message"], params)
        e = self.n.err("service.define", {"name": "bad", "argv": ["wsl.exe"]})
        self.assertIn("Hive node inside WSL", e["hint"])

    def test_define_stores_expand_define(self):
        model = self.n.home / "m.gguf"
        model.write_bytes(b"GGUF")
        p = {"name": "ol", "preset": "llamacpp", "preset_args": [f"model_path={model}", f"binary={PY}"],
             "start": False}
        r = self.define(**p)
        stored = dict(self.sm._defs["ol"])
        from hive import catalog, schema
        params = schema.validate(catalog.SPECS["service.define"].params, dict(p, health_interval=2))
        ctx = self.sm._preset_ctx(self.n.rt.effective(OPERATOR))
        expected = presets.expand_define(params, ctx=ctx, given=frozenset(dict(p, health_interval=2)))
        for k, v in expected.items():
            self.assertEqual(stored.get(k), v, k)
        self.assertEqual(r["definition"]["argv"][:3], [PY, "-m", str(model)])

    def test_vllm_defaults_and_keep_awake_warning(self):
        r = self.define(name="qv", preset="vllm", preset_args=["model=Qwen/Q"], start=False)
        self.assertEqual((r["definition"]["start_period"], r["definition"]["stop_timeout"],
                          r["definition"]["keep_awake"]), (1800, 60, True))
        if not util.IS_WINDOWS and not util.IS_MAC:
            from hive.jobs import KEEP_AWAKE_WARNING
            self.assertIn(KEEP_AWAKE_WARNING, r["warnings"])
        r = self.define(name="qv", preset="vllm", preset_args=["model=Qwen/Q"], start_period=300, start=False)
        self.assertEqual(r["definition"]["start_period"], 300)
        e = self.n.err("service.define", {"name": "x", "preset": "vllm", "preset_args": ["model=a/b",
                                                                                        "extra_arg=--host=0.0.0.0"]})
        self.assertIn("gateway", e["message"])


class TestEndpoints(SvcCase):
    def test_endpoint_for_auth(self):
        helpers.set_secret(self.n.rt, "HF_TOKEN", "hf_" + "A" * 30)
        self.define(name="evil", argv=SLEEPER, endpoint="https://evil.example/v1", api_key_env="HF_TOKEN",
                    health="none", start=False)
        base, api, auth = self.sm.endpoint_for("evil")
        self.assertEqual((base, api), ("https://evil.example/v1", "openai"))
        self.assertFalse(auth_host_allowed(auth.hosts, "evil.example", "https"))    # never receives the token
        self.assertTrue(auth_host_allowed(auth.hosts, "huggingface.co", "https"))
        self.define(name="own", argv=SLEEPER, endpoint="http://127.0.0.1:8123/v1", api_key_env="LOCAL_KEY",
                    env=["LOCAL_KEY=abc12345678"], health="none", start=False)
        base, _api, auth = self.sm.endpoint_for("own")
        self.assertEqual((auth.hosts, auth.value), (("loopback",), "Bearer abc12345678"))
        self.assertNotIn("abc12345678", repr(auth))

    def test_model_endpoints_fallback(self):
        with mock.patch.object(self.n.rt.policy, "model_endpoints",
                               [{"name": "ollama", "base_url": "http://127.0.0.1:11434/v1", "api_key_env": None}]):
            self.assertEqual(self.sm.endpoint_for("ollama"), ("http://127.0.0.1:11434/v1", "openai", None))
            self.define(name="ollama", argv=SLEEPER, endpoint="http://127.0.0.1:11500/v1", endpoint_kind="ollama",
                        health="none", start=False)
            self.assertEqual(self.sm.endpoint_for("ollama")[:2], ("http://127.0.0.1:11500/v1", "ollama"))
            with self.assertRaises(HiveError) as cm:
                self.sm.endpoint_for("nope")
            self.assertEqual(cm.exception.code, "not_found")
            self.assertIn("ollama", cm.exception.hint)
        rows = self.sm.endpoints()
        self.assertEqual([(r["name"], r["source"], r["api"]) for r in rows], [("ollama", "service", "ollama")])

    def test_policy_view(self):
        self.assertEqual(self.sm.policy_view(), {"endpoints": [], "code_dirs": []})
        venv = self.n.home / ".venvs" / "vllm" / "bin"
        venv.mkdir(parents=True)
        py = venv / "python"
        py.symlink_to(PY) if POSIX else shutil.copy(PY, py)
        comfy = self.n.home / "ComfyUI"
        comfy.mkdir()
        self.define(name="v", argv=[str(py), "-c", "pass"], endpoint="http://127.0.0.1:8000/v1", cwd=str(comfy),
                    health="none", start=False)
        view = self.sm.policy_view()
        self.assertEqual(view["endpoints"], ["http://127.0.0.1:8000/v1"])
        self.assertIn(str(venv), view["code_dirs"])
        self.assertIn(str(venv.parent), view["code_dirs"])
        self.assertIn(str(comfy), view["code_dirs"])
        # a gated identity cannot write the service's code; an operator on standard may
        with self.assertRaises(HiveError):
            self.n.rt.effective(CAUTIOUS).resolve_path(str(comfy / "custom_nodes" / "a.py"), write=True)
        self.n.rt.effective(OPERATOR).resolve_path(str(comfy / "custom_nodes" / "a.py"), write=True)
        self.n.ok("service.remove", {"name": "v"})
        self.assertEqual(self.sm.policy_view(), {"endpoints": [], "code_dirs": []})

    def test_code_dir_home_warning(self):
        r = self.define(name="h", argv=SLEEPER, cwd=str(self.n.home), health="none", start=False)
        self.assertIn(services.CODE_DIR_WARNING.format(d=str(self.n.home)), r["warnings"])


class TestAdoptionAndBoot(SvcCase):
    def test_adoption_and_boot_rules(self):
        self.define(name="keep", argv=SLEEPER, wait=10)
        pid = self.sm.runtime("keep")["child_pid"]
        self.n.restart_runtime()                       # node crash/upgrade: the service keeps running
        rt = self.sm.runtime("keep")
        self.assertTrue(rt["adopted"])
        self.assertEqual((rt["state"], rt["child_pid"]), ("running", pid))
        # node down + service dead + same boot -> restarted (desired running)
        self.n.stop_runtime()
        proctree.kill_tree(int(rt["shim_pid"]), force=True)
        proctree.kill_tree(int(pid), force=True)
        wait_for(lambda: not util.pid_alive(pid), 10)
        self.n.rt = self.n.make_runtime()
        self.wait_state("keep", "running", "starting")
        self.assertNotEqual(self.sm.runtime("keep")["child_pid"], pid)

    def test_reboot_rule(self):
        self.define(name="noauto", argv=SLEEPER, wait=10)
        self.define(name="auto", argv=SLEEPER, autostart=True, wait=10)
        self.n.stop_runtime()
        for name in ("noauto", "auto"):
            run = Path(self.n.data) / "state" / "services" / name / "run"
            proctree.request_stop(run, 1, force=True)
        for name in ("noauto", "auto"):
            wait_for(lambda name=name: (Path(self.n.data) / "state" / "services" / name / "run" / "exit.json")
                     .exists(), 10)
        with mock.patch.object(util, "boot_key", return_value="other-boot:1"):
            self.n.rt = self.n.make_runtime()
            self.assertEqual(self.state("noauto"), "stopped")
            self.assertEqual(self.sm.runtime("noauto")["last_error"], services.REBOOT_ERROR)
            self.wait_state("auto", "running", "starting")

    def test_t_key_ten_seconds_off_is_same_boot(self):
        self.assertTrue(util.same_boot("t:1000.0", "t:1010.0"))
        self.define(name="tk", argv=SLEEPER, wait=10)
        self.n.stop_runtime()
        run = Path(self.n.data) / "state" / "services" / "tk" / "run"
        proctree.request_stop(run, 1, force=True)
        wait_for(lambda: (run / "exit.json").exists(), 10)
        rtp = Path(self.n.data) / "state" / "services" / "tk" / "runtime.json"
        d = json.loads(rtp.read_text())
        d["boot_key"] = "t:1000.0"
        rtp.write_text(json.dumps(d))
        with mock.patch.object(util, "boot_key", return_value="t:1010.0"):
            self.n.rt = self.n.make_runtime()
            self.wait_state("tk", "running", "starting")

    def test_exec_disabled_autostart_fails_without_spawn(self):
        self.define(name="ad", argv=SLEEPER, autostart=True, wait=10)
        self.n.stop_runtime()
        run = Path(self.n.data) / "state" / "services" / "ad" / "run"
        proctree.request_stop(run, 1, force=True)
        wait_for(lambda: (run / "exit.json").exists(), 10)
        self.n.overrides = {"policy": {"exec": {"enabled": False}}}
        with mock.patch.object(proctree, "spawn_shim") as sp, mock.patch.object(util, "boot_key", return_value="b2"):
            self.n.rt = self.n.make_runtime()
            self.wait_state("ad", "failed")
            sp.assert_not_called()
        self.assertEqual(self.sm.runtime("ad")["last_error"], services.EXEC_DISABLED_ERROR)

    def test_paused(self):
        self.define(name="p", argv=[PY, "-c", "import time; time.sleep(1.5)"], restart="always", restart_delay=0)
        (Path(self.n.data) / "PAUSED").write_text("")
        self.n.rt._paused_cache = (0.0, False)
        self.wait_state("p", "stopped", timeout=20)
        rt = self.sm.runtime("p")
        self.assertEqual((rt["desired"], rt["last_error"]), ("stopped", services.PAUSED_ERROR))
        (Path(self.n.data) / "PAUSED").unlink()
        self.n.rt._paused_cache = (0.0, False)
        time.sleep(1.0)
        self.assertEqual(self.state("p"), "stopped")       # resume restarts nothing

    def test_stop_on_exit(self):
        self.n.overrides = {"policy": {"services": {"stop_on_exit": True}}}
        self.n.restart_runtime()
        self.define(name="soe", argv=SLEEPER, wait=10)
        shim = self.sm.runtime("soe")["shim_pid"]
        self.n.stop_runtime()
        wait_for(lambda: not util.pid_alive(int(shim)), 15)
        self.n.rt = self.n.make_runtime()


class TestPanicOffline(SvcCase):
    def test_panic_with_node_down(self):
        m1 = self.n.home / "m1"
        m2 = self.n.home / "m2"
        self.define(name="alive", argv=SLEEPER, stop_command=[PY, "-c", f"open({str(m1)!r},'w').write('x')"], wait=10)
        self.define(name="deadshim", argv=SLEEPER, stop_command=[PY, "-c", f"open({str(m2)!r},'w').write('x')"],
                    wait=10)
        job = self.n.ok("job.start", {"argv": [PY, "-c", "import time; time.sleep(60)"]})
        dead = self.sm.runtime("deadshim")
        self.n.stop_runtime()
        if POSIX:
            import signal
            os.kill(int(dead["shim_pid"]), signal.SIGKILL)
            proctree.kill_tree(int(dead["child_pid"]), force=True)
        out = services.panic_offline(Path(self.n.data), "n_test0000000000", force_after=3)
        self.assertEqual(out["jobs"], [job["job_id"]])
        self.assertIn("alive", out["services"])
        self.assertTrue(m1.exists())                      # the shim ran stop_argv
        if POSIX:
            self.assertIn("deadshim", out["services"])
            self.assertTrue(m2.exists())                  # run_stop_command for the dead shim
        rt = json.loads((Path(self.n.data) / "state" / "services" / "alive" / "runtime.json").read_text())
        self.assertEqual(rt["desired"], "stopped")
        self.n.rt = self.n.make_runtime()

    def test_run_stop_command(self):
        self.assertIsNone(services.run_stop_command({}))
        self.assertEqual(services.run_stop_command({"stop_command": [PY, "-c", "import sys; sys.exit(3)"]}), 3)


FAKE_DOCKER = r'''
import json, os, sys, time, signal
st = os.environ["FAKE_DOCKER_STATE"]
with open(os.path.join(st, "calls.log"), "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\n")
def read(n, d=""):
    p = os.path.join(st, n)
    return open(p).read().strip() if os.path.exists(p) else d
mode = read("mode", "absent")
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
if cmd in ("inspect", "ps", "version") and mode == "down":
    sys.stderr.write("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?\n")
    sys.exit(1)
if cmd == "inspect":
    if mode == "absent":
        sys.stderr.write("Error: No such object: " + sys.argv[-1] + "\n"); sys.exit(1)
    if "Labels" in " ".join(sys.argv):
        print(read("labels", "{}")); sys.exit(0)
    print(json.dumps({"Id": "abc123def4567890", "State": {"Status": "running"}, "Config": {"Image": "img"}}))
    sys.exit(0)
if cmd == "ps":
    print(read("ps", "")); sys.exit(0)
if cmd == "run":
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
    open(os.path.join(st, "run.pid"), "w").write(str(os.getpid()))
    print("container running", flush=True)
    while True: time.sleep(0.2)
if cmd == "stop" and os.path.exists(os.path.join(st, "run.pid")):
    try:
        os.kill(int(read("run.pid")), signal.SIGTERM)
    except OSError:
        pass
sys.exit(0)
'''


@unittest.skipUnless(POSIX, "fake docker script uses a shebang")
class TestContainers(unittest.TestCase):
    def setUp(self):
        for attr, val in (("TICK", 0.2), ("NO_HEALTH_ALIVE_S", 0.5)):
            p = mock.patch.object(services, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.state = Path(tempfile.mkdtemp(prefix="hivefd-"))
        self.addCleanup(shutil.rmtree, str(self.state), True)
        env = mock.patch.dict(os.environ, {"FAKE_DOCKER_STATE": str(self.state)})
        env.start()
        self.addCleanup(env.stop)
        tool = helpers.fake_tool_on_path("docker", FAKE_DOCKER)
        self.tooldir = tool.__enter__()
        self.addCleanup(tool.__exit__, None, None, None)
        self.node = Node()
        self.node.__enter__()
        self.addCleanup(self.node.__exit__, None, None, None)
        self.n = self.node

    def set(self, name, value):
        (self.state / name).write_text(value)

    def calls(self):
        p = self.state / "calls.log"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def define(self, name="dk", **kw):
        p = {"name": name, "preset": "docker", "preset_args": ["image=img:1", "gpus=none"], "health": "none"}
        p.update(kw)
        return self.n.call("service.define", p)

    def test_absent_starts(self):
        env = self.define(wait=10)
        self.assertTrue(env["ok"], env)
        self.assertEqual(env["result"]["state"], "running")
        self.assertTrue(any(c[0] == "inspect" for c in self.calls()))
        self.set("mode", "present")
        self.n.rt.services._container_cache.clear()
        st = self.n.ok("service.status", {"name": "dk"})
        self.assertEqual(st["container"], {"name": "hive-dk", "id": "abc123def456", "state": "running",
                                           "image": "img"})

    def test_label_match_removed(self):
        self.set("mode", "present")
        self.set("labels", json.dumps({"ai.hive.node": "n_test0000000000", "ai.hive.service": "dk"}))
        env = self.define(wait=10)
        self.assertTrue(env["ok"], env)
        self.assertIn(["rm", "-f", "hive-dk"], self.calls())

    def test_foreign_container_fails(self):
        self.set("mode", "present")
        self.set("labels", json.dumps({"other": "x"}))
        env = self.define()
        self.assertFalse(env["ok"])
        self.assertIn("Hive did not create", env["error"]["message"])
        self.assertNotIn(["rm", "-f", "hive-dk"], self.calls())

    def test_daemon_down_waits_then_starts(self):
        self.set("mode", "down")
        env = self.define(max_restarts=0)
        self.assertTrue(env["ok"], env)
        r = env["result"]
        self.assertEqual((r["state"], r["last_error"]), ("starting", services.DOCKER_WAIT_ERROR))
        rs = self.n.rt.services._rs["dk"]
        with self.n.rt.services._lock:
            rs["_docker_next"] = 0
        self.set("mode", "absent")
        wait_for(lambda: self.n.rt.services.runtime("dk")["state"] == "running", 20)
        self.assertEqual(self.n.rt.services.runtime("dk")["restarts"], [])   # the wait consumed no restarts

    def test_daemon_down_cap(self):
        self.set("mode", "down")
        self.define()
        sm = self.n.rt.services
        base = services._mono()
        with mock.patch.object(services, "_mono", return_value=base + services.DOCKER_WAIT_MAX_S + 5):
            with sm._lock:
                sm._rs["dk"]["_docker_next"] = 0
            wait_for(lambda: sm.runtime("dk")["state"] == "failed", 20)
        self.assertEqual(sm.runtime("dk")["last_error"], services.DOCKER_WAIT_ERROR)

    def test_panic_stops_labelled_containers(self):
        self.set("ps", "cid1\ncid2")
        out = services.panic_offline(Path(self.n.data), "n_test0000000000", force_after=1)
        self.assertEqual(out["containers"], ["cid1", "cid2"])
        self.assertIn(["ps", "-q", "--filter", "label=ai.hive.node=n_test0000000000"], self.calls())
        self.assertIn(["stop", "-t", "10", "cid1", "cid2"], self.calls())


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class TestServiceApprovals(SvcCase):
    def test_define_bound_and_rewrite_conflicts(self):
        run = self.n.home / "run.sh"
        run.write_text("sleep 30\n")
        p = {"name": "b", "command": "bash run.sh", "health": "none"}
        e = self.n.err("service.define", p, identity=CAUTIOUS)
        aid = e["details"]["approval_id"]
        rv = self.n.rt.approvals.get(aid)["review"]
        self.assertTrue(any(f["path"] == str(run) and f["bind"] == "sha256" for f in rv["files"]))
        self.n.approve(aid)
        run.write_text("echo changed\n")
        e = self.n.err("service.define", p, identity=CAUTIOUS, approval_id=aid)
        self.assertEqual(e["code"], "conflict")
        self.assertNotIn("b", self.sm._defs)                       # nothing defined or started

    def test_restart_binds_script_argument(self):
        svc = self.n.home / "svc.sh"
        svc.write_text("sleep 30\n")
        self.define(name="r", argv=["bash", str(svc)], health="none", wait=5)
        e = self.n.err("service.restart", {"name": "r", "wait": 5}, identity=CAUTIOUS)
        rv = self.n.rt.approvals.get(e["details"]["approval_id"])["review"]
        self.assertTrue(any(f["path"] == str(svc) for f in rv["files"]))
        self.assertTrue(any(x.startswith("argv: ") and "svc.sh" in x for x in rv["lines"]))
        self.assertTrue(any(x.startswith("cwd: ") for x in rv["lines"]))
        self.n.approve(e["details"]["approval_id"])
        env = self.n.call("service.restart", {"name": "r", "wait": 5}, identity=CAUTIOUS,
                          approval_id=e["details"]["approval_id"])
        self.assertTrue(env["ok"], env)
        # the started process holds the file against gated writers until it exits
        with self.assertRaises(HiveError):
            self.n.rt.effective(CAUTIOUS).resolve_path(str(svc), write=True)
        self.n.ok("service.stop", {"name": "r", "wait": 20})
        self.n.rt.effective(CAUTIOUS).resolve_path(str(svc), write=True)


class TestBackoffFormula(unittest.TestCase):
    def test_delays_grow_and_reset(self):
        rt = FakeRt()
        self.addCleanup(rt.cleanup)
        sm = services.ServiceManager(rt)
        sm._defs["x"] = {"name": "x", "restart": "on-failure", "restart_delay": 3, "max_restarts": 100,
                         "restart_window": 600}
        rs = sm._new_rs()
        rs.update(desired="running", state="running", shim_pid=None, _gen=1)
        sm._rs["x"] = rs
        delays = []
        for run_s in (1, 1, 1, 120, 1):
            with sm._lock:
                rs.update(state="running", _started_mono=services._mono() - run_s)
            (sm._run_dir("x")).mkdir(parents=True, exist_ok=True)
            util.write_json_atomic(sm._run_dir("x") / "exit.json", {"v": 1, "exit_code": 1, "reason": "exit"})
            sm._on_exit("x")
            self.assertEqual(rs["state"], "backoff")
            delays.append(round(rs["_next_restart_mono"] - services._mono()))
        self.assertEqual(delays, [3, 6, 12, 3, 3])
        with sm._lock:
            rs.update(state="running", _started_mono=services._mono(), _short_runs=20)
        sm._on_exit("x")
        self.assertEqual(round(rs["_next_restart_mono"] - services._mono()), 300)      # capped


if __name__ == "__main__":
    unittest.main()
