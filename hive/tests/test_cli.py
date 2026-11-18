"""WP6a: cli.py smoke tests (§19, §21.2 tools row): init, node token/secret/stop/restart/pause, call and sh exit codes,
tools export, escape_terminal on printed node text."""
from __future__ import annotations

import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from hive import cli, config, util

from tests import helpers


class _Stdin(io.StringIO):
    def isatty(self):
        return False


def run(argv, stdin: str = "", env=None):
    """cli.main in-process -> (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env or {}), mock.patch.object(cli.sys, "stdin", _Stdin(stdin)), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


CLEAN_ENV = ("HIVE_URL", "HIVE_TOKEN", "HIVE_TOKEN_FILE", "HIVE_CONNECTION", "HTTPS_PROXY", "https_proxy",
             "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "HIVE_NODE")


class CliCase(unittest.TestCase):
    def setUp(self):
        self._home = helpers.tmp_home()
        self.home = self._home.__enter__()
        saved = {k: os.environ.pop(k) for k in CLEAN_ENV if k in os.environ}
        self.addCleanup(os.environ.update, saved)
        self.addCleanup(self._home.__exit__, None, None, None)


class TestInit(CliCase):
    def test_init_node_tokens_and_connections(self):
        code, out, err = run(["init", "node", "--yes", "--name", "Desk", "--mirror", "cn"])
        self.assertEqual(code, 0, err)
        cfg = config.load_node_config(str(self.home / "node" / "config.json"))
        self.assertEqual(cfg.name, "desk")
        self.assertEqual(cfg.models.hf_endpoint, config.HF_MIRROR)
        cc = config.load_client_config()
        self.assertEqual(cc.default, "this-pc")
        kinds = {t.sha256: t.kind for t in cfg.auth.tokens}
        for conn, kind in (("this-pc", "agent"), ("this-pc-owner", "owner")):
            tok = Path(cc.connections[conn].token_file).read_text().strip()
            self.assertEqual(kinds[util.hash_token(tok)], kind)
        self.assertIn('"--local"', out)
        self.assertIn("hive node run", out)
        self.assertNotIn("hv_", out)  # tokens are written to files, never printed

    def test_proxy_detection_and_consent(self):
        code, out, err = run(["init", "node"])
        self.assertEqual(code, 2)
        self.assertIn("consent", err)
        self.assertFalse((self.home / "node" / "config.json").exists())
        code, out, err = run(["init", "node", "--yes"], env={"HTTPS_PROXY": "http://127.0.0.1:7890"})
        self.assertEqual(code, 0, err)
        cfg = config.load_node_config(str(self.home / "node" / "config.json"))
        self.assertEqual(cfg.network.proxy, "http://127.0.0.1:7890")
        self.assertIn("proxy: http://127.0.0.1:7890", out)

    def test_init_existing(self):
        self.assertEqual(run(["init", "node", "--yes"])[0], 0)
        code, out, err = run(["init", "node"])
        self.assertEqual(code, 2)
        self.assertIn("config exists", err)
        code, out, err = run(["init", "node", "--listen", "127.0.0.1:7811", "--preset", "full"])
        self.assertEqual(code, 0, err)
        self.assertIn("listen", out)
        self.assertIn("--preset applies only when creating a config", out)
        cfg = config.load_node_config(str(self.home / "node" / "config.json"))
        self.assertEqual((cfg.listen.port, cfg.policy.preset), (7811, "standard"))

    def test_init_client_and_hub(self):
        code, out, err = run(["init", "client", "--url", "http://127.0.0.1:7700", "--token-env", "TOK", "--name",
                              "home"], env={"TOK": "hv_abc"})
        self.assertEqual(code, 0, err)
        cc = config.load_client_config()
        self.assertEqual(cc.default, "home")
        self.assertEqual(Path(cc.connections["home"].token_file).read_text().strip(), "hv_abc")
        code, out, err = run(["init", "client", "--url", "http://127.0.0.1:7700", "--token", "hv_x", "--name", "b"])
        self.assertIn("visible in ps", err)
        code, out, err = run(["init", "hub", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertTrue((self.home / "hub" / "config.json").exists())
        self.assertEqual(run(["init", "hub", "--yes"])[0], 2)


class TestNodeCommands(CliCase):
    def setUp(self):
        super().setUp()
        self.assertEqual(run(["init", "node", "--yes", "--preset", "cautious"])[0], 0)
        self.path = str(self.home / "node" / "config.json")

    def test_token_add_list_revoke(self):
        code, out, err = run(["node", "token", "add", "claude"])
        self.assertEqual(code, 0, err)
        self.assertIn("cautious node: this token's shell/service/delete calls wait for approval", out)
        tok = [l for l in out.splitlines() if l.startswith("hv_")][0]
        cfg = config.load_node_config(self.path)
        e = [t for t in cfg.auth.tokens if t.name == "claude"][0]
        self.assertEqual(e.sha256, util.hash_token(tok))
        self.assertIn("shell.run", e.require_approval)
        code, out, err = run(["node", "token", "add", "bot", "--no-cautious", "--classes", "read,net"])
        self.assertEqual(code, 0, err)
        e = [t for t in config.load_node_config(self.path).auth.tokens if t.name == "bot"][0]
        self.assertEqual((e.require_approval, sorted(e.classes)), ([], ["net", "read"]))
        h = "sha256:" + "a" * 64
        code, out, err = run(["node", "token", "add", "hub-home", "--kind", "hub", "--sha256", h])
        self.assertEqual(code, 0, err)
        self.assertNotIn("hv_", out)
        self.assertEqual(run(["node", "token", "add", "claude"])[0], 2)
        self.assertEqual(run(["node", "token", "add", "x", "--classes", "root"])[0], 2)
        code, out, err = run(["node", "token", "list"])
        self.assertIn("hub-home", out)
        self.assertNotIn("sha256:", out)
        self.assertEqual(run(["node", "token", "revoke", "claude"])[0], 0)
        self.assertEqual(run(["node", "token", "revoke", "claude"])[0], 1)
        self.assertEqual(run(["node", "token", "revoke", "owner"])[0], 1)

    def test_secrets(self):
        code, out, err = run(["node", "secret", "set", "HF_TOKEN"], stdin="hf_secretvalue123\n")
        self.assertEqual(code, 0, err)
        self.assertNotIn("hf_secretvalue123", out + err)
        raw = json.loads((self.home / "node" / "secrets.json").read_text())
        self.assertEqual(raw["secrets"]["HF_TOKEN"]["value"], "hf_secretvalue123")
        if os.name != "nt":
            self.assertEqual(os.stat(self.home / "node" / "secrets.json").st_mode & 0o777, 0o600)
        code, out, err = run(["node", "secret", "set", "MY_KEY", "plaintext-value"])
        self.assertEqual(code, 2)
        self.assertIn("never accepted on the command line", err)
        self.assertNotIn("MY_KEY", json.loads((self.home / "node" / "secrets.json").read_text())["secrets"])
        code, out, err = run(["node", "secret", "set", "MY_KEY", "--from-env", "SRC", "--hosts", "+api.x.com",
                              "--no-exec"], env={"SRC": "v" * 20})
        self.assertEqual(code, 0, err)
        code, out, err = run(["node", "secret", "list"])
        self.assertIn("MY_KEY", out)
        self.assertIn("api.x.com", out)
        self.assertNotIn("v" * 20, out)
        self.assertEqual(run(["node", "secret", "unset", "MY_KEY"])[0], 0)
        self.assertEqual(run(["node", "secret", "unset", "MY_KEY"])[0], 1)
        code, out, err = run(["node", "secret", "set", "OTHER"], stdin="abcdefghij\n")
        self.assertIn("only sent to loopback", out)

    def test_stop_restart_pause(self):
        code, out, err = run(["node", "stop"])
        self.assertEqual((code, out.strip()), (0, "node is not running"))
        code, out, err = run(["node", "restart"])
        self.assertEqual(code, 1)  # install.restart is the P0 stub until WP4
        self.assertIn("unsupported", err)
        self.assertEqual(run(["node", "pause"])[0], 0)
        self.assertTrue((self.home / "node" / "PAUSED").exists())
        self.assertEqual(run(["node", "resume"])[0], 0)
        self.assertFalse((self.home / "node" / "PAUSED").exists())
        self.assertEqual(run(["node", "panic"])[0], 2)          # no tty, no --yes
        code, out, err = run(["node", "panic", "--yes"])
        self.assertEqual(code, 0, err)
        self.assertIn("revoke --all --hub", out)
        self.assertTrue((self.home / "node" / "relay.disabled").exists())
        code, out, err = run(["node", "status"])
        self.assertEqual(code, 0, err)
        self.assertIn("not running", out)
        self.assertIn("paused: yes", out)

    def test_stop_running_holder(self):
        lock = util.FileLock(self.home / "node" / "node.lock")
        self.assertTrue(lock.acquire("test"))
        calls = []

        def alive(pid, ctime=None):
            calls.append(pid)
            return len(calls) < 3
        try:
            with mock.patch.object(cli.util, "pid_alive", side_effect=alive), \
                    mock.patch.object(cli.time, "sleep"):
                code, out, err = run(["node", "stop"])
        finally:
            lock.release()
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads((self.home / "node" / "control.json").read_text())["cmd"], "stop")


class TestCallAndSh(CliCase):
    @classmethod
    def setUpClass(cls):
        cls._h = helpers.tmp_home()
        cls._h.__enter__()
        cls.url, cls.tok, cls.rt = helpers.start_node_server(start_managers=True)

    @classmethod
    def tearDownClass(cls):
        cls.rt.test_stop()
        cls._h.__exit__(None, None, None)

    def env(self, url=None, tok=None):
        return {"HIVE_URL": url or self.url, "HIVE_TOKEN": tok or self.tok}

    def test_call(self):
        code, out, err = run(["call", "node.info"], env=self.env())
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["name"], self.rt.name)
        code, out, err = run(["call", "fs.stat", "path=~", "sha256=false", "--raw"], env=self.env())
        self.assertEqual(code, 0, err)
        self.assertTrue(json.loads(out)["exists"])
        code, out, err = run(["call", "fs.stat", "--json", '{"path": "~"}'], env=self.env())
        self.assertEqual(code, 0, err)
        code, out, err = run(["call", "fs.nothing"], env=self.env())
        self.assertEqual(code, 1)
        self.assertTrue(err.startswith("not_found: "))
        self.assertIn("Hint:", err)
        code, out, err = run(["--json", "call", "fs.nothing"], env=self.env())
        self.assertEqual(json.loads(err)["error"]["code"], "not_found")
        self.assertEqual(run(["call", "node.info"], env=self.env(tok="hv_" + "x" * 40))[0], 3)
        self.assertEqual(run(["call", "node.info"], env=self.env(url=f"http://127.0.0.1:{helpers.free_port()}"))[0], 4)
        self.assertEqual(run(["call", "fs.stat", "nokv"], env=self.env())[0], 2)

    @unittest.skipIf(os.name == "nt", "POSIX printf escapes")
    def test_call_escapes_terminal(self):
        code, out, err = run(["call", "shell.run", "command=printf '\\033[31mred\\342\\200\\256x'"], env=self.env())
        self.assertEqual(code, 0, err)
        self.assertNotIn("\x1b", out)
        self.assertNotIn("\u202e", out)
        self.assertIn("\\u{202E}", out)

    @unittest.skipIf(os.name == "nt", "bash syntax; PowerShell exit semantics are covered by WP2's shell tests")
    def test_sh_exit_codes(self):
        code, out, err = run(["sh", "self", "--", "echo", "hello"], env=self.env())
        self.assertEqual((code, out), (0, "hello\n"))
        code, out, err = run(["sh", "self", "--", "echo out; echo oops >&2; exit 7"], env=self.env())
        self.assertEqual((code, out, err), (7, "out\n", "oops\n"))
        code, out, err = run(["sh", "self", "--timeout", "1", "--", "sleep 5"], env=self.env())
        self.assertEqual(code, 124, err)
        code, out, err = run(["sh", "self", "--", "printf '\\342\\200\\256x'"], env=self.env())
        self.assertEqual(out, "\\u{202E}x\n")
        self.assertEqual(run(["sh", "self", "--", "true"], env=self.env(url=f"http://127.0.0.1:{helpers.free_port()}"))[0],
                         125)
        self.assertEqual(run(["sh", "self"], env=self.env())[0], 125)
        self.assertEqual(run(["sh", "other-node", "--", "true"], env=self.env())[0], 125)


class TestMisc(CliCase):
    def test_tools_export(self):
        for fmt in ("mcp", "anthropic", "openai", "openai-responses"):
            code, out, err = run(["tools", "export", "--format", fmt])
            self.assertEqual(code, 0, err)
            self.assertEqual(len(json.loads(out)), 28)
        target = self.home / "t.json"
        code, out, err = run(["tools", "export", "--format", "anthropic", "--profile", "minimal", "--prefix", "hv_",
                              "-o", str(target)])
        self.assertEqual(code, 0, err)
        data = json.loads(target.read_text())
        self.assertEqual(len(data), 10)
        self.assertTrue(all(t["name"].startswith("hv_") for t in data))
        self.assertEqual(run(["tools", "export", "--format", "gemini"])[0], 2)

    def test_version_and_wp6b(self):
        from hive import __version__
        self.assertEqual(run(["version"])[1].strip(), f"hive {__version__}")
        self.assertEqual(run(["--version"])[1].strip(), f"hive {__version__}")
        code, out, err = run(["gateway", "url", "a", "b"])
        self.assertEqual(code, 2)
        self.assertIn("WP6b", err)
        self.assertEqual(run([])[0], 2)
        self.assertEqual(run(["bogus"])[0], 2)

    def test_exit_code_mapping(self):
        from hive.errors import HiveError
        self.assertEqual([cli.exit_code_for(HiveError(c, "x")) for c in
                          ("unauthorized", "forbidden", "policy_denied", "unavailable", "timeout", "not_found",
                           "conflict")], [3, 3, 3, 4, 4, 1, 1])


if __name__ == "__main__":
    unittest.main()
