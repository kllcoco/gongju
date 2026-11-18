"""External review, round 2 (PHASE0_NOTES "External review fixes", R2-*): CLI and install findings (listen edits keep
client.json and the hub's local-node entry in step, plaintext listen refused before writing, restart failure reason,
uninstall --stop-services, service define failures and hints, join hints, pause/resume confirmation, init no-op,
token check, broken pipe, presets guide)."""
from __future__ import annotations

import json
import os
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import cli, config, install, node as node_mod
from hive.errors import HiveError
from tests.test_cli import CliCase, run
from tests.test_install import OLD, InstallCase, Seams
from tests.test_walkthrough import _FakeClient

TOKEN = "hv_" + "a1B2c3D4e5" * 4 + "xyz"


class TestListenEdits(CliCase):
    def conns(self):
        return config.load_client_config().connections

    def test_node_port_change_updates_client_json(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        code, out, err = run(["init", "node", "--listen", "127.0.0.1:7703"])
        self.assertEqual(code, 0, err)
        c = self.conns()
        self.assertEqual((c["this-pc"].url, c["this-pc-owner"].url), ("http://127.0.0.1:7703",) * 2)
        self.assertIn("client.json: this-pc-owner -> http://127.0.0.1:7703", out)

    def test_no_listen_drops_local_connections(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        self.assertEqual(run(["init", "client", "--name", "home", "--url", "http://127.0.0.1:7700", "--token-env",
                              "T"], env={"T": TOKEN})[0], 0)
        code, out, err = run(["init", "node", "--no-listen"])
        self.assertEqual(code, 0, err)
        cc = config.load_client_config()
        self.assertNotIn("this-pc", cc.connections)
        self.assertNotIn("this-pc-owner", cc.connections)
        self.assertEqual(cc.default, "home")
        # node status reads the state files instead of asking the client default (another machine)
        with mock.patch.object(cli, "_holder", return_value={"pid": 4242}), \
                mock.patch.object(cli, "_connect", side_effect=AssertionError("must not connect")):
            code, out, err = run(["node", "status"])
        self.assertEqual(code, 0, err)
        self.assertIn("does not listen", out)

    def test_local_node_entry_of_the_hub_follows_the_port(self):
        self.assertEqual(run(["init", "node", "--yes", "--name", "desk"])[0], 0)
        with mock.patch.object(cli, "_restart_daemon", return_value=False):
            self.assertEqual(run(["init", "hub", "--yes", "--name", "home", "--with-local-node"])[0], 0)
        with mock.patch.object(cli, "_restart_daemon", return_value=False) as rd:
            code, out, err = run(["init", "node", "--listen", "127.0.0.1:7703"])
        self.assertEqual(code, 0, err)
        raw = json.loads((self.home / "hub" / "config.json").read_text())
        self.assertEqual([n["url"] for n in raw["nodes"] if n["name"] == "desk"], ["http://127.0.0.1:7703"])
        self.assertEqual(sorted(c.args[0] for c in rd.call_args_list), ["hub", "node"])
        with mock.patch.object(cli, "_restart_daemon", return_value=False):
            code, out, err = run(["init", "node", "--no-listen"])
        self.assertIn("no longer listens", out)

    def test_hub_port_change_updates_hub_local(self):
        self.assertEqual(run(["init", "hub", "--yes"])[0], 0)
        code, out, err = run(["init", "hub", "--listen", "127.0.0.1:7705"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.conns()["hub-local"].url, "http://127.0.0.1:7705")

    def test_plaintext_non_loopback_refused_before_writing(self):
        self.assertEqual(run(["init", "hub", "--yes"])[0], 0)
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        for kind in ("hub", "node"):
            with self.subTest(kind=kind):
                before = (self.home / kind / "config.json").read_text()
                code, out, err = run(["init", kind, "--listen", "192.0.2.2:7700"])
                self.assertEqual(code, 2)
                self.assertIn("refusing plaintext HTTP on 192.0.2.2", err)
                self.assertEqual((self.home / kind / "config.json").read_text(), before)
        self.assertEqual(run(["init", "node", "--listen", "100.101.1.5:7701"])[0], 0)    # tailnet literal is fine

    def test_bare_init_is_a_no_op(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        tok = (self.home / "tokens" / "this-pc-owner.token").read_text()
        code, out, err = run(["init", "node"])
        self.assertEqual(code, 0, err)
        self.assertIn("already set up", out)
        self.assertEqual((self.home / "tokens" / "this-pc-owner.token").read_text(), tok)


class TestRestartFailureReason(InstallCase):
    def test_foreground_reexec_failure_names_the_reason(self):
        Seams(self, method=None, holders=[(0, OLD), (2, None)], alive_until=2)
        logs = self.data / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "node.log").write_text("old: node failed to start: something long ago\n")

        def control(ctx, cmd):
            with open(str(logs / "node.log"), "a") as fh:
                fh.write("2026-11-18 ERROR hive.node: node failed to start: refusing plaintext HTTP on 192.0.2.2 "
                         "(use --tailscale)\n")
        with mock.patch.object(install, "_write_control", side_effect=control):
            with self.assertRaises(HiveError) as cm:
                install.restart("node")
        self.assertIn("restarted but could not start: refusing plaintext HTTP on 192.0.2.2", cm.exception.message)
        self.assertNotIn("foreground", cm.exception.message)
        self.assertNotIn("long ago", cm.exception.message)

    def test_plain_foreground_exit_keeps_its_message(self):
        Seams(self, method=None, holders=[(0, OLD), (2, None)], alive_until=2)
        with self.assertRaises(HiveError) as cm:
            install.restart("node")
        self.assertIn("ran in the foreground", cm.exception.message)


class TestUninstall(InstallCase):
    def test_stop_services_without_installation(self):
        with mock.patch.object(install, "_installed", return_value=(None, False)), \
                mock.patch("hive.services.panic_offline", return_value={"jobs": ["j"], "services": ["s"]}) as po:
            res = install.uninstall("node", stop_services=True)
        po.assert_called_once()
        self.assertIn("stopped 1 jobs and 1 services", res.notes)

    def test_warns_about_what_keeps_running(self):
        with mock.patch.object(install, "_installed", return_value=(None, False)), \
                mock.patch("hive.services.running_offline", return_value=(["j1"], ["s1", "s2"])), \
                mock.patch("hive.services.panic_offline", side_effect=AssertionError("must not stop")):
            res = install.uninstall("node")
        self.assertTrue(any("2 services and 1 jobs keep running" in n and "--stop-services" in n for n in res.notes),
                        res.notes)

    def test_hand_started_node_named_at_install(self):
        with mock.patch.object(install, "_installed", return_value=(None, False)), \
                mock.patch.object(install, "_holder", return_value={"pid": 777}), \
                mock.patch.object(install, "_install_watchdog", return_value=None):
            res = install.install("node", self.config_path, method="watchdog")
        self.assertTrue(any("started by hand is running (pid 777)" in n and "takes over" in n for n in res.notes),
                        res.notes)


class TestServiceDefineCli(CliCase):
    def test_crashed_service_is_a_failure(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        fc = _FakeClient(result={"name": "qwen", "created": True, "state": "backoff", "health": None,
                                 "last_error": "exited with code 1",
                                 "tail": "Traceback\nModuleNotFoundError: No module named 'vllm'\n"})
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, err = run(["service", "define", "qwen", "--preset", "vllm", "--model", "Qwen/X", "--node",
                                  "desk", "--connection", "hub-local"])
        self.assertEqual(code, cli.EXIT_ACTION)
        self.assertIn("it did not start: exited with code 1", err)
        self.assertIn("No module named 'vllm'", err)
        self.assertIn("hive logs desk --service qwen --connection hub-local", err)

    def test_follow_ups_keep_the_connection(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        fc = _FakeClient(result={"name": "stub", "created": True, "state": "running", "health": "healthy",
                                 "endpoint": "http://127.0.0.1:8000/v1"})
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, err = run(["service", "define", "stub", "--argv", "python3 stub.py", "--node", "desk",
                                  "--connection", "hub-local"])
        self.assertEqual(code, 0, err)
        self.assertIn("hive logs desk --service stub -f --connection hub-local", out)
        self.assertIn("wait=120 --node desk --connection hub-local", out)

    def test_not_this_node_hint_names_the_hub(self):
        self.assertEqual(run(["init", "hub", "--yes"])[0], 0)
        fc = _FakeClient(error=HiveError("not_found", "this is node 'vm', not 'desk'", hint="omit node or use 'self'"))
        with mock.patch.object(cli, "_connect", return_value=fc):
            code, out, err = run(["call", "service.status", "name=stub", "--node", "desk"])
        self.assertIn("other nodes are reached through the hub: --connection hub-local", err)


class TestMisc(CliCase):
    def test_init_client_rejects_garbage_token(self):
        for bad in ("x", "hv_short", "hv_" + "a" * 20):
            with self.subTest(bad=bad):
                code, out, err = run(["init", "client", "--name", "vm3", "--url", "http://127.0.0.1:7700", "--token",
                                      "-"], stdin=bad + "\n")
                self.assertEqual(code, 2)
                self.assertIn("does not look like a Hive token", err)
                self.assertFalse((self.home / "tokens" / "vm3.token").exists())
        code, out, err = run(["init", "client", "--name", "vm3", "--url", "http://127.0.0.1:7700", "--token", "-"],
                             stdin=TOKEN + "\n")
        self.assertEqual(code, 0, err)

    def test_broken_pipe_is_quiet(self):
        with mock.patch.object(cli, "_main", side_effect=BrokenPipeError()), \
                mock.patch.object(cli, "_silence_stdout") as sil:
            self.assertEqual(cli.main(["node", "status"]), 0)
        sil.assert_called_once()

    def test_presets_guide_maps_to_cli(self):
        code, out, err = run(["guide", "presets"])
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("From the command line: hive service define"), out[:200])
        self.assertIn("--arg key=value", out)
        self.assertNotIn("From the command line", run(["guide", "overview"])[1])

    def test_join_hint_uses_the_command_the_user_runs(self):
        cfg = mock.Mock()
        cfg.listen.host, cfg.listen.port = "127.0.0.1", 7701
        with mock.patch.dict(os.environ, {"HIVE_HOME": "/workspace/.hive"}), \
                mock.patch.object(cli.util, "_zipapp_path", return_value=Path("/root/hive.pyz")), \
                mock.patch("hive.config.default_paths", return_value={"home": Path("/nonexistent-hive-home")}):
            hint = cli._start_fail_hint(cfg, "OSError: [Errno 98] Address already in use")
        self.assertIn("HIVE_HOME=/workspace/.hive python3 /root/hive.pyz init node --no-listen", hint)

    def test_join_failure_says_the_watchdog_keeps_retrying(self):
        cfg = mock.Mock()
        cfg.name = "pod"
        cfg.listen.host, cfg.listen.port = "127.0.0.1", 7701
        log = self.home / "stdout.log"
        log.write_text("node failed to start: refusing to run as root\n")
        with mock.patch.object(cli, "JOIN_VERIFY_S", 0.5), mock.patch.object(cli, "JOIN_FAIL_GRACE_S", 0.1), \
                mock.patch.object(cli, "_node_log_files", return_value=[log, log]):
            import contextlib
            import io
            buf, ebuf = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(ebuf):
                rc = cli._verify_joined(cfg, {log: 0}, cli.Out())
        self.assertEqual(rc, cli.EXIT_UNAVAILABLE)
        self.assertIn("keeps retrying", buf.getvalue())
        self.assertIn("service uninstall node", buf.getvalue())


class TestPauseAck(CliCase):
    def test_pause_and_resume_confirm_through_the_ack_file(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        self.assertEqual(run(["init", "node", "--no-listen"])[0], 0)
        d = self.home / "node"

        def node_applies():
            # what NodeRuntime._watch_paused does about 1 s later
            time.sleep(0.4)
            raw = json.loads((d / "PAUSED").read_text()) if (d / "PAUSED").exists() else None
            node_mod.write_json_atomic(d / node_mod.PAUSED_ACK, {"paused": raw is not None,
                                                                 "id": (raw or {}).get("id")}, private=True)
        with mock.patch.object(cli, "_holder", return_value={"pid": 1}), \
                mock.patch.object(cli, "_connect", side_effect=AssertionError("no listener: no HTTP")):
            t = threading.Thread(target=node_applies)
            t.start()
            code, out, err = run(["node", "pause"])
            t.join()
            self.assertEqual(code, 0, err)
            self.assertIn("confirmed by the running node", out)
            t = threading.Thread(target=node_applies)
            t.start()
            t0 = time.monotonic()
            code, out, err = run(["node", "resume"])
            t.join()
            self.assertEqual(code, 0, err)
            self.assertGreaterEqual(time.monotonic() - t0, 0.35)          # it waited for the node
            self.assertEqual(out.strip(), "resumed (nothing was restarted)")

    def test_runtime_writes_the_ack(self):
        from tests.wp2util import Node
        with Node() as n:
            nonce = node_mod.pause(n.rt.data_dir)
            self.assertFalse(node_mod.pause_acked(n.rt.data_dir, paused=True, nonce=nonce))
            n.rt._watch_paused()
            self.assertTrue(node_mod.pause_acked(n.rt.data_dir, paused=True, nonce=nonce))
            self.assertTrue(n.rt.is_paused())
            node_mod.resume(n.rt.data_dir)
            n.rt._watch_paused()
            self.assertTrue(node_mod.pause_acked(n.rt.data_dir, paused=False))
            self.assertFalse(n.rt.is_paused())


if __name__ == "__main__":
    unittest.main()
