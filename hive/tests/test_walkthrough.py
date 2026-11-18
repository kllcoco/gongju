"""v8.1 external review (a beginner following the Chinese README step by step): regression tests for the fixes.

A foreground ``hive node run`` / ``hive hub run`` in a terminal re-execs itself after exit 75; B ``init hub
--with-local-node`` restarts the local node; D ``hive service define`` / ``hive guide``; E ``init client --token -``;
F the small items (root warning at init, Tailscale check, ``init hub`` edits in place, mcp-config HIVE_HOME, hints,
``sh --approval``, ``secret set`` Ctrl-D, ``pause`` confirmation, ``join --install`` verification, doctor both)."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hive import cli, config, install, node as node_mod, util
from hive.errors import HiveError

from tests.test_cli import CliCase, run
from tests.test_install import InstallCase, OLD, Seams

PKG_ROOT = Path(__file__).resolve().parents[1]


class _Tty(io.StringIO):
    def isatty(self):
        return True


class _FakeClient:
    """Records calls; ``result`` or ``error`` answers them."""

    def __init__(self, result=None, error=None, rows=None):
        self.calls = []
        self.result = result if result is not None else {}
        self.error = error
        self.rows = rows or []

    def call(self, action, params, **kw):
        self.calls.append((action, dict(params), kw))
        if self.error is not None:
            raise self.error
        return self.result

    def nodes(self):
        return self.rows

    def close(self):
        pass


# ---------------------------------------------------------------------------------------------------------------- A

class TestForegroundRestart(unittest.TestCase):
    def test_terminal_detection(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("HIVE_WATCHDOG", "INVOCATION_ID", "HIVE_FOREGROUND_CHILD"):
                os.environ.pop(k, None)
            with mock.patch.object(node_mod.sys, "stdin", _Tty()):
                self.assertTrue(node_mod.foreground_restart_ok())
                self.assertFalse(node_mod.foreground_restart_ok(relaunched=True))
                self.assertFalse(node_mod.foreground_restart_ok(supervise=True))
                for k, v in (("HIVE_WATCHDOG", "1"), ("INVOCATION_ID", "abc"), ("HIVE_FOREGROUND_CHILD", "1")):
                    with mock.patch.dict(os.environ, {k: v}):
                        self.assertFalse(node_mod.foreground_restart_ok(), k)
            with mock.patch.object(node_mod.sys, "stdin", io.StringIO()), \
                    mock.patch.object(node_mod.sys, "stderr", io.StringIO()):
                self.assertFalse(node_mod.foreground_restart_ok())          # no terminal: a supervisor's child

    def _args(self, **kw):
        return SimpleNamespace(config=None, relaunched=False, supervise=False, **kw)

    def test_exit_75_reexecs_same_command(self):
        seen = {}

        def reexec(argv, cwd):
            seen["argv"], seen["cwd"] = argv, cwd
            return 0
        with mock.patch.object(node_mod, "foreground_restart_ok", return_value=True), \
                mock.patch.object(node_mod, "reexec_foreground", side_effect=reexec), \
                mock.patch.object(cli, "_MAIN_ARGV", ["node", "run", "--config", "c.json"]):
            code = cli._foreground_run("node", self._args(), lambda *a, **k: 75)
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"], cli._relaunch_prefix() + ["node", "run", "--config", "c.json"])
        with mock.patch.object(cli.sys, "argv", [os.path.join("x", "hive", "__main__.py"), "node", "run"]):
            self.assertEqual(cli._relaunch_prefix(), util.self_command())     # python -m hive
        self.assertEqual(seen["cwd"], os.getcwd())

    def test_supervised_or_other_codes_return(self):
        with mock.patch.object(node_mod, "reexec_foreground", side_effect=AssertionError("no re-exec")):
            with mock.patch.object(node_mod, "foreground_restart_ok", return_value=False):
                self.assertEqual(cli._foreground_run("hub", self._args(), lambda *a, **k: 75), 75)
            with mock.patch.object(node_mod, "foreground_restart_ok", return_value=True):
                self.assertEqual(cli._foreground_run("hub", self._args(), lambda *a, **k: 0), 0)
                self.assertEqual(cli._foreground_run("hub", self._args(), lambda *a, **k: 1), 1)

    @unittest.skipIf(os.name == "nt", "POSIX execv")
    def test_reexec_prints_line_and_execs(self):
        err = io.StringIO()
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        with mock.patch.object(node_mod.os, "execv", side_effect=OSError("boom")) as ex, \
                mock.patch.object(node_mod.sys, "stderr", err):
            code = node_mod.reexec_foreground(["/bin/true", "x"], d)
        self.assertEqual(code, 75)                                       # exec failed: the old exit code stays
        ex.assert_called_once_with("/bin/true", ["/bin/true", "x"])
        self.assertIn("restarting to apply new settings", err.getvalue())
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(d))


class TestRestartSeesReexec(InstallCase):
    """install.restart: a re-exec keeps pid and ctime; the per-start lock note tells the new start apart."""

    def setUp(self):
        super().setUp()
        install._execute.side_effect = None

    def test_same_pid_new_note_is_a_restart(self):
        again = dict(OLD, note="node 2")
        s = Seams(self, method=None, holders=[(0, OLD), (1, None), (2, again)], alive_until=10 ** 6)
        res = install.restart("node")
        self.assertEqual(s.controls, ["restart"])
        self.assertIn("restarted", " ".join(res.notes))
        self.assertLess(s.t, 10)

    def test_lock_note_is_per_start(self):
        from hive.util import FileLock
        lk = FileLock(self.data / "x.lock")
        # what NodeRuntime/Hub write: a kind plus a nonce, so two starts of one process differ
        self.assertTrue(lk.acquire(note=f"node {os.urandom(4).hex()}"))
        self.addCleanup(lk.release)
        self.assertTrue(lk.holder()["note"].startswith("node "))


@unittest.skipUnless(os.name == "posix" and sys.platform.startswith("linux"), "pty + execv (Linux)")
class TestForegroundNodeSurvivesConfigChange(unittest.TestCase):
    """Real process: ``hive node run`` in a pseudo-terminal, then ``hive node token add`` -> same pid, new start."""

    def test_token_add_restarts_in_place(self):
        import pty
        import select
        home = Path(tempfile.mkdtemp(prefix="hivefg-")).resolve()
        self.addCleanup(shutil.rmtree, str(home), True)
        env = dict(os.environ, HIVE_HOME=str(home), PYTHONPATH=str(PKG_ROOT))
        for k in ("HIVE_WATCHDOG", "INVOCATION_ID", "HIVE_URL", "HIVE_CONNECTION"):
            env.pop(k, None)
        hive = [sys.executable, "-m", "hive"]
        port = __import__("tests.helpers", fromlist=["free_port"]).free_port()
        r = subprocess.run(hive + ["init", "node", "--yes", "--listen", f"127.0.0.1:{port}"], env=env,
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        cp = home / "node" / "config.json"
        raw = json.loads(cp.read_text())
        raw["allow_root"] = True
        cp.write_text(json.dumps(raw))
        pid, fd = pty.fork()
        if pid == 0:  # pragma: no cover - child
            os.chdir(str(home))
            os.execvpe(hive[0], hive + ["node", "run"], env)
        buf = bytearray()

        def pump(cond, timeout):
            end = time.monotonic() + timeout
            while time.monotonic() < end and not cond():
                rr, _, _ = select.select([fd], [], [], 0.2)
                if rr:
                    try:
                        buf.extend(os.read(fd, 4096))
                    except OSError:
                        return

        def holder():
            return util.FileLock(home / "node" / "node.lock").holder()
        try:
            pump(lambda: holder() is not None and b"ACTIVE" in buf, 30)
            first = holder()
            self.assertIsNotNone(first, buf.decode(errors="replace"))
            r = subprocess.run(hive + ["node", "token", "add", "x1"], env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=90)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn("applied (restarted", r.stdout)
            pump(lambda: buf.count(b"ACTIVE") >= 2, 30)
            self.assertIn(b"restarting to apply new settings", bytes(buf))
            second = holder()
            self.assertEqual(second["pid"], first["pid"])               # the same process, re-exec'd
            self.assertNotEqual(second["note"], first["note"])
            r = subprocess.run(hive + ["node", "stop"], env=env, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            end = time.monotonic() + 20
            status = None
            while time.monotonic() < end:
                p, st = os.waitpid(pid, os.WNOHANG)
                if p:
                    status = st
                    break
                pump(lambda: False, 0.2)
            self.assertIsNotNone(status)
            self.assertEqual(os.WEXITSTATUS(status), 0)                 # stop still ends it with 0
        finally:
            try:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            except OSError:
                pass
            os.close(fd)


class TestRestartWording(CliCase):
    def test_not_running_and_applied_and_foreground(self):
        out = cli.Out()
        with mock.patch("sys.stdout", new_callable=io.StringIO) as so:
            self.assertFalse(cli._restart_daemon("node", out))
        self.assertIn("will apply on next start", so.getvalue())
        with mock.patch.object(util.FileLock, "holder", return_value={"pid": 1}), \
                mock.patch.object(install, "restart", return_value=None), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as so:
            self.assertTrue(cli._restart_daemon("hub", out))
        self.assertIn("applied (restarted", so.getvalue())
        err = HiveError("unavailable", "the hub stopped, but it ran in the foreground (no service installation "
                                       "restarts it)")
        with mock.patch.object(util.FileLock, "holder", return_value={"pid": 1}), \
                mock.patch.object(install, "restart", side_effect=err), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as so:
            self.assertFalse(cli._restart_daemon("hub", out))
        self.assertIn("start it again: hive hub run", so.getvalue())


# ---------------------------------------------------------------------------------------------------------------- B + F (init)

class TestInitHub(CliCase):
    def test_with_local_node_restarts_the_running_node(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        with mock.patch.object(cli, "_restart_daemon", return_value=True) as rd:
            code, out, err = run(["init", "hub", "--yes", "--with-local-node"])
        self.assertEqual(code, 0, err)
        self.assertEqual([c.args[0] for c in rd.call_args_list], ["node"])
        self.assertIn("registered with the hub as 'hubhost'", out)          # node and hub share the host name
        self.assertIn("share this computer's name", out)
        raw = json.loads((self.home / "node" / "config.json").read_text())
        self.assertTrue(any(t.get("kind") == "hub" for t in raw["auth"]["tokens"]))

    def test_rerun_changes_only_listen_and_keeps_tokens(self):
        self.assertEqual(run(["init", "hub", "--yes"])[0], 0)
        tok = (self.home / "tokens" / "hub-owner.token").read_text()
        before = json.loads((self.home / "hub" / "config.json").read_text())["tokens"]
        code, out, err = run(["init", "hub", "--listen", "127.0.0.1:7790"])
        self.assertEqual(code, 0, err)
        self.assertIn("listen", out)
        self.assertIn("will apply on next start", out)
        raw = json.loads((self.home / "hub" / "config.json").read_text())
        self.assertEqual(raw["listen"]["port"], 7790)
        self.assertEqual(raw["tokens"], before)
        self.assertEqual((self.home / "tokens" / "hub-owner.token").read_text(), tok)
        code, out, err = run(["init", "hub"])                               # nothing to change
        self.assertEqual(code, 2)
        self.assertIn("--force", err)

    def test_rerun_with_local_node_adds_it(self):
        self.assertEqual(run(["init", "node", "--yes", "--name", "desk"])[0], 0)
        self.assertEqual(run(["init", "hub", "--yes", "--name", "home"])[0], 0)
        with mock.patch.object(cli, "_restart_daemon", return_value=False) as rd:
            code, out, err = run(["init", "hub", "--with-local-node"])
        self.assertEqual(code, 0, err)
        self.assertEqual(sorted(c.args[0] for c in rd.call_args_list), ["hub", "node"])
        raw = json.loads((self.home / "hub" / "config.json").read_text())
        self.assertEqual([n["name"] for n in raw["nodes"]], ["desk"])
        code, out, err = run(["init", "hub", "--with-local-node"])         # again: replaced, not duplicated
        self.assertEqual(code, 0, err)
        raw = json.loads((self.home / "hub" / "config.json").read_text())
        self.assertEqual([n["name"] for n in raw["nodes"]], ["desk"])

    def test_tailscale_checked_at_init(self):
        with mock.patch("hive.httpserver.tailscale_info",
                        side_effect=HiveError("unavailable", "Tailscale is not available")):
            code, out, err = run(["init", "hub", "--yes", "--tailscale"])
        self.assertEqual(code, 0, err)                                       # a warning, not a failure
        self.assertIn("warning: Tailscale is not available", out)
        self.assertIn("tailscale up", out)

    def test_root_warned_at_init_node(self):
        with mock.patch.object(config, "is_privileged", return_value=True):
            code, out, err = run(["init", "node", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("refuse to start as", out)
        self.assertIn("allow_root", out)
        with mock.patch.object(config, "is_privileged", return_value=False):
            code, out, err = run(["init", "node", "--mirror", "cn"])
        self.assertNotIn("allow_root", out)


# ---------------------------------------------------------------------------------------------------------------- E

class TestInitClientToken(CliCase):
    def test_token_from_stdin_saved_private(self):
        code, out, err = run(["init", "client", "--name", "home", "--url", "http://127.0.0.1:7700", "--token", "-",
                              "--default"], stdin="hv_secret123\n")
        self.assertEqual(code, 0, err)
        p = self.home / "tokens" / "home.token"
        self.assertEqual(p.read_text().strip(), "hv_secret123")
        if os.name == "posix":
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertNotIn("hv_secret123", out + err)
        self.assertNotIn("visible in ps", err)
        cc = config.load_client_config()
        self.assertEqual(cc.default, "home")
        self.assertEqual(Path(cc.connections["home"].token_file), p)

    def test_token_to_named_file_and_prompt(self):
        target = self.home / "elsewhere" / "h.token"
        with mock.patch.object(cli.sys, "stdin", _Tty()), \
                mock.patch.object(cli.getpass, "getpass", return_value="hv_prompted") as gp, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            code = cli.main(["init", "client", "--name", "h", "--url", "http://127.0.0.1:7700", "--token", "-",
                             "--token-file", str(target)])
        self.assertEqual(code, 0)
        gp.assert_called_once()
        self.assertEqual(target.read_text().strip(), "hv_prompted")
        if os.name == "posix":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_prompt_ctrl_d_is_clean(self):
        with mock.patch.object(cli.sys, "stdin", _Tty()), \
                mock.patch.object(cli.getpass, "getpass", side_effect=EOFError), \
                mock.patch("sys.stderr", new_callable=io.StringIO) as se:
            code = cli.main(["init", "client", "--name", "h", "--url", "http://127.0.0.1:7700", "--token", "-"])
        self.assertEqual(code, 2)
        self.assertIn("cancelled", se.getvalue())
        self.assertNotIn("Traceback", se.getvalue())

    @unittest.skipUnless(os.name == "posix", "POSIX modes")
    def test_existing_token_file_is_tightened(self):
        tf = self.home / "t.token"
        tf.write_text("hv_x\n")
        os.chmod(tf, 0o644)
        code, out, err = run(["init", "client", "--name", "h", "--url", "http://127.0.0.1:7700", "--token-file",
                              str(tf)])
        self.assertEqual(code, 0, err)
        self.assertEqual(tf.stat().st_mode & 0o777, 0o600)
        self.assertIn("0600", out)

    def test_add_agent_prints_the_one_line_command(self):
        self.assertEqual(run(["init", "hub", "--yes", "--name", "home"])[0], 0)
        code, out, err = run(["hub", "add-agent", "claude-desktop"])
        self.assertEqual(code, 0, err)
        self.assertIn("hive init client --name home --url http://127.0.0.1:7700 --token - --default", out)


# ---------------------------------------------------------------------------------------------------------------- D

class TestServiceDefineAndGuide(CliCase):
    def define(self, *argv, result=None):
        fc = _FakeClient(result=result or {"name": "qwen", "state": "running", "health": "healthy", "created": True,
                                           "port": 8000, "endpoint": "http://127.0.0.1:8000/v1"})
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, err = run(["service", "define"] + list(argv))
        return code, out, err, fc

    def test_preset_model_maps_to_the_preset_arg(self):
        code, out, err, fc = self.define("qwen", "--preset", "vllm", "--model", "Qwen/Qwen2.5-7B-Instruct",
                                         "--arg", "max_model_len=8192", "--node", "desk")
        self.assertEqual(code, 0, err)
        action, params, kw = fc.calls[0]
        self.assertEqual(action, "service.define")
        self.assertEqual(params["preset"], "vllm")
        self.assertEqual(params["preset_args"], ["max_model_len=8192", "model=Qwen/Qwen2.5-7B-Instruct"])
        self.assertEqual(kw["node"], "desk")
        self.assertIn("service 'qwen' defined on desk", out)
        self.assertIn("hive gateway url desk qwen", out)
        code, out, err, fc = self.define("q", "--preset", "llamacpp", "--model", "~/m.gguf")
        self.assertEqual(fc.calls[0][1]["preset_args"], ["model_path=~/m.gguf"])
        code, out, err, fc = self.define("o", "--preset", "ollama", "--model", "x")
        self.assertEqual(code, 2)
        self.assertEqual(fc.calls, [])

    def test_argv_stub(self):
        code, out, err, fc = self.define("stub", "--argv", "python3 stub.py 8000", "--port", "8000", "--endpoint",
                                         "http://127.0.0.1:8000/v1", "--param", "health=http")
        self.assertEqual(code, 0, err)
        p = fc.calls[0][1]
        self.assertEqual(p["argv"], ["python3", "stub.py", "8000"])
        self.assertEqual((p["port"], p["endpoint"], p["health"], p["wait"]),
                         (8000, "http://127.0.0.1:8000/v1", "http", 30.0))
        code, out, err, fc = self.define("x")                              # nothing to run
        self.assertEqual(code, 2)
        code, out, err, fc = self.define("x", "--argv", "a", "--command", "b")
        self.assertEqual(code, 2)

    def test_guide_prints_the_agent_topics(self):
        from hive import guides
        code, out, err = run(["guide", "gateway"])
        self.assertEqual(code, 0, err)
        self.assertIn(guides.topic_text("gateway").strip().splitlines()[0], out)
        code, out, err = run(["guide"])
        self.assertIn(guides.topic_text("overview").strip().splitlines()[0], out)
        code, out, err = run(["guide", "--list"])
        self.assertIn("model-serving", out)
        code, out, err = run(["guide", "nope"])
        self.assertEqual(code, 2)


# ---------------------------------------------------------------------------------------------------------------- F

class TestSmallItems(CliCase):
    def test_mcp_config_passes_a_custom_hive_home(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        with mock.patch.object(cli, "_console_command", return_value=["/usr/bin/hive"]), \
                mock.patch("hive.client.connect", side_effect=HiveError("unavailable", "down")):
            code, out, err = run(["mcp-config", "--local", "--client", "generic"])
            self.assertEqual(code, 0, err)
            srv = json.loads(out[out.index("{"):out.rindex("}") + 1])["mcpServers"]["hive"]
            self.assertEqual(srv["env"], {"HIVE_HOME": str(self.home)})
            code, out, err = run(["mcp-config", "--local", "--client", "claude-code"])
            self.assertIn(f"-e HIVE_HOME={self.home} --", out)
        self.assertEqual(cli._mcp_env(), {"HIVE_HOME": str(self.home)})
        with mock.patch.dict(os.environ, {"HIVE_HOME": os.path.join(os.path.expanduser("~"), ".hive")}):
            self.assertEqual(cli._mcp_env(), {})                             # the default needs no env

    def test_sh_approval_retry(self):
        err = HiveError("approval_required", "this call needs the owner's approval (node)",
                        {"approval_id": "ap_abc", "expires_at": "x", "where": "node", "how": "hive approvals approve "
                                                                                             "ap_abc"},
                        hint="hive approvals approve ap_abc")
        fc = _FakeClient(error=err)
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, e = run(["sh", "desk", "--", "echo", "hi"])
        self.assertEqual(code, cli.SH_FAILURE)
        self.assertIn("--approval ap_abc", e)
        fc = _FakeClient(result={"state": "exited", "exit_code": 0, "stdout": "hi\n"})
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, e = run(["sh", "desk", "--approval", "ap_abc", "--", "echo", "hi"])
        self.assertEqual(code, 0, e)
        self.assertEqual(fc.calls[0][2]["approval_id"], "ap_abc")
        self.assertEqual(fc.calls[0][1]["command"], "echo hi")

    def test_secret_prompt_ctrl_d_and_ctrl_c(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        for exc in (EOFError, KeyboardInterrupt):
            with mock.patch.object(cli.sys, "stdin", _Tty()), \
                    mock.patch.object(cli.getpass, "getpass", side_effect=exc), \
                    mock.patch("sys.stderr", new_callable=io.StringIO) as se:
                code = cli.main(["node", "secret", "set", "HF_TOKEN"])
            self.assertEqual(code, 2, exc)
            self.assertIn("nothing was saved", se.getvalue())
            self.assertNotIn("Traceback", se.getvalue())
        self.assertFalse((self.home / "node" / "secrets.json").exists())

    def test_panic_without_terminal_names_yes(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        code, out, err = run(["node", "panic"])
        self.assertEqual(code, 2)
        self.assertIn("hive node panic --yes", out)

    def test_nodes_hint_on_the_hub_machine(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        self.assertEqual(run(["init", "hub", "--yes"])[0], 0)
        fc = _FakeClient(rows=[{"name": "vm", "online": True, "mode": "self"}])
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, err = run(["nodes"])
            self.assertIn("hive nodes --connection hub-local", out)
            code, out, err = run(["--connection", "hub-local", "nodes"])
            self.assertNotIn("hint:", out)

    def test_pause_waits_for_confirmation(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        code, out, err = run(["node", "pause"])
        self.assertIn("not running", out)
        fc = _FakeClient(result={"paused": True})
        with mock.patch.object(cli, "_holder", return_value={"pid": 1}), \
                mock.patch.object(cli, "_connect", return_value=fc):
            code, out, err = run(["node", "pause"])
        self.assertEqual(code, 0, err)
        self.assertIn("confirmed by the running node", out)
        self.assertEqual(fc.calls[0][0], "node.info")
        fc = _FakeClient(result={"paused": False})
        with mock.patch.object(cli, "_holder", return_value={"pid": 1}), \
                mock.patch.object(cli, "_connect", return_value=fc), \
                mock.patch.object(cli.time, "sleep"), \
                mock.patch.object(cli.time, "monotonic", side_effect=[0.0] + [10.0] * 20):
            code, out, err = run(["node", "pause"])
        self.assertIn("did not confirm within 3 s", out)

    def test_doctor_checks_node_and_hub(self):
        (self.home / "node").mkdir()
        (self.home / "node" / "config.json").write_text("{}")
        (self.home / "hub").mkdir()
        (self.home / "hub" / "config.json").write_text("{}")
        seen = []

        def fake(kind, network=True, deep=False):
            seen.append(kind)
            return [{"check": "x", "status": "ok", "detail": kind}]
        with mock.patch("hive.doctor.run", side_effect=fake):
            code, out, err = run(["doctor", "--no-network"])
        self.assertEqual(seen, ["node", "hub"])
        self.assertIn("== hub ==", out)
        with mock.patch("hive.doctor.run", side_effect=fake):
            code, out, err = run(["doctor", "hub"])
        self.assertEqual(seen[-1:], ["hub"])


class TestJoinVerify(CliCase):
    def cfg(self):
        self.assertEqual(run(["init", "node", "--yes", "--listen", "127.0.0.1:7701"])[0], 0)
        return config.load_node_config(str(self.home / "node" / "config.json"))

    def test_registered(self):
        cfg = self.cfg()
        log = cli._node_log_files(cfg)[0]
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("old line: relay registered with http://old\n")
        marks = {p: cli._file_size(p) for p in cli._node_log_files(cfg)}
        with open(str(log), "a") as fh:
            fh.write("INFO hive.relay: relay registered with http://hub:7700 (session s1)\n")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(cli._verify_joined(cfg, marks, cli.Out()), 0)
        self.assertIn("joined:", buf.getvalue())

    def test_port_conflict_explained(self):
        cfg = self.cfg()
        log = cli._node_log_files(cfg)[0]
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("relay registered with http://old\n")                 # before the install: ignored
        marks = {p: cli._file_size(p) for p in cli._node_log_files(cfg)}
        with open(str(log), "a") as fh:
            fh.write("ERROR hive.node: node failed: cannot listen on 127.0.0.1:7701: [Errno 98] Address already in "
                     "use\n")
        se = io.StringIO()
        with mock.patch.object(cli, "JOIN_VERIFY_S", 0.5), mock.patch("sys.stderr", se), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli._verify_joined(cfg, marks, cli.Out()), cli.EXIT_UNAVAILABLE)
        self.assertIn("Address already in use", se.getvalue())
        self.assertIn("--no-listen", se.getvalue())


class TestWatchdogWording(InstallCase):
    def test_desktop_friendly_notes(self):
        res = install.install("node", self.config_path, method="watchdog", dry_run=True)
        text = " ".join(res.notes)
        self.assertIn("no systemd user session here", text)
        self.assertIn("login script", text)
        self.assertIn("after a reboot", text)


if __name__ == "__main__":
    unittest.main()
