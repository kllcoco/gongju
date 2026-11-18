"""External review, round 2 (PHASE0_NOTES "External review fixes", R2-*): approval file binding of '~' words
(literal paths), cd detection on the whole shell text, read-check binding of protected files, the hub binding the
path as the command names it (symbolic-link cwd), env values that name files."""
from __future__ import annotations

import os
import shutil
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import presets, proctree, services, util
from hive.client import connect
from hive.errors import HiveError
from hive.transport import HttpTransport
from tests import helpers
from tests.test_hub import _HubCase
from tests.wp2util import CAUTIOUS, PY, Node, wait_for
from tests.wp5util import FakeRelay, call_async, hub_token_entry, ok_env

HAS_BASH = proctree.shell_path("bash") is not None
POSIX = os.name != "nt"

CD_SPELLINGS = ("`echo cd` sub && bash setup.sh", "eval 'cd sub'; bash setup.sh", "C=cd; $C sub; bash setup.sh",
                "cd${IFS}sub;bash setup.sh", "f(){ bash setup.sh; }; cd sub; f",
                "for i in 1 2; do bash setup.sh; cd sub; done")


class TestBindPlan(unittest.TestCase):
    def test_tilde_spellings(self):
        P = presets.bind_plan
        self.assertEqual(P("bash ~/lib/q.sh")[0], ["bash", "~/lib/q.sh"])
        for cmd in ("bash '~/lib/q.sh'", 'bash "~/lib/q.sh"', "bash \\~/lib/q.sh", 'bash ~"/lib/q.sh"'):
            with self.subTest(cmd=cmd):
                self.assertEqual(P(cmd)[0], ["bash", "./~/lib/q.sh"])
        # argv: nothing expands '~' (the literal is bound; the expanded one too, programs may expand it themselves)
        self.assertEqual(P(["bash", "~/lib/y.sh"])[0], ["bash", "./~/lib/y.sh", "~/lib/y.sh"])
        # bash expands ~ after '=' only in NAME=… words
        self.assertEqual(P("x --cfg=~/c.yaml")[0], ["x", "./~/c.yaml", "~/c.yaml"])
        self.assertIn("~/b.yaml", P("A=~/b.yaml x")[0])
        self.assertNotIn("./~/b.yaml", P("A=~/b.yaml x")[0])
        # PowerShell / cmd: bound literally and expanded
        # (cmd also binds python.com/.exe/.bat/.cmd in the cwd as missing: it looks there before PATH; audit round 2)
        self.assertEqual(P("python ~/x.py", shell="cmd")[0][:3], ["python", "./~/x.py", "~/x.py"])

    def test_variables(self):
        P = presets.bind_plan
        self.assertEqual(P("bash $HOME/x.sh"), (["bash", "$HOME/x.sh"], [], []))
        self.assertEqual(P("bash ${HOME}/x.sh")[0], ["bash", "${HOME}/x.sh"])
        self.assertEqual(P("bash $D/x.sh"), (["bash"], [], ["$D/x.sh (variables; not bound)"]))
        self.assertEqual(P("bash '$HOME/x.sh'")[2], ["$HOME/x.sh (variables; not bound)"])
        self.assertEqual(P(["bash", "$HOME/x.sh"])[2], ["$HOME/x.sh (variables; not bound)"])
        self.assertEqual(P(["bash", "$OTHER/x.sh"])[0], ["bash", "$OTHER/x.sh"])     # literal, PathGuard keeps it

    def test_whole_text_cd(self):
        for cmd in CD_SPELLINGS:
            with self.subTest(cmd=cmd):
                w, m, _u = presets.bind_plan(cmd)
                self.assertNotIn("setup.sh", w)
                self.assertIn("setup.sh", m)
        for cmd in ("cd/d sub & x.bat", "cd..& x.bat", "cd\\sub & x.bat", "chdir/d sub & x.bat"):
            with self.subTest(cmd=cmd):
                w, m, _u = presets.bind_plan(cmd, shell="cmd")
                self.assertNotIn("x.bat", w)
                self.assertIn("x.bat", m)
        for cmd in ('$ExecutionContext.SessionState.Path.SetLocation("sub"); ./x.ps1', "& {cd sub}; ./x.ps1",
                    "[IO.Directory]::SetCurrentDirectory('sub'); ./x.ps1", "iex $s; ./x.ps1"):
            with self.subTest(cmd=cmd):
                w, m, _u = presets.bind_plan(cmd, shell="powershell")
                self.assertNotIn("./x.ps1", w)
                self.assertIn("./x.ps1", m)
        # nothing that changes the cwd: words stay bound; argv keeps the word-order rule
        for cmd in ("bash setup.sh", "cat cd.txt setup.sh", "bash setup.sh --mode=abcd"):
            with self.subTest(cmd=cmd):
                self.assertIn("setup.sh", presets.bind_plan(cmd)[0])
        self.assertEqual(presets.bind_plan(["bash", "setup.sh", "cd", "x"]), (["bash", "setup.sh", "cd"], ["x"], []))

    def test_env_file_values(self):
        words, unbound = presets._env_words({"BASH_ENV": "~/envx.sh", "ENV": "e.sh", "PYTHONSTARTUP": "/abs/s.py",
                                             "NODE_OPTIONS": "--require ./h.js --import=m.mjs",
                                             "LD_PRELOAD": "a.so:/l/b.so", "PYTHONSTARTUP2": "x",
                                             "BASH_ENV2": "y"}, [], [])
        self.assertEqual(words, ["~/envx.sh", "./~/envx.sh", "./e.sh", "/abs/s.py", "./h.js", "./m.mjs", "./a.so",
                                 "/l/b.so"])
        self.assertEqual(presets._env_words({"BASH_ENV": "$X/e.sh"}, [], [])[1],
                         ["env BASH_ENV=$X/e.sh (variables; not bound)"])


@unittest.skipUnless(HAS_BASH and POSIX, "needs bash")
class TestNodeBinding(unittest.TestCase):
    def review(self, n, action, params):
        e = n.err(action, params, identity=CAUTIOUS)
        self.assertEqual(e["code"], "approval_required", e)
        aid = e["details"]["approval_id"]
        return aid, n.rt.approvals.get(aid)["review"]

    def test_argv_tilde_binds_the_literal_path(self):
        for action in ("shell.run", "job.start"):
            with self.subTest(action=action), Node() as n:
                (n.home / "lib").mkdir()
                (n.home / "lib" / "y.sh").write_text("echo benign-y\n")
                (n.home / "w4").mkdir()
                params = {"argv": ["bash", "~/lib/y.sh"], "cwd": "~/w4"}
                aid, rv = self.review(n, action, params)
                lit = os.path.join(os.path.realpath(str(n.home / "w4")), "~", "lib", "y.sh")
                binds = {f["path"]: f["bind"] for f in rv["files"]}
                self.assertEqual(binds.get(lit), "absent", binds)
                self.assertIn("./~/lib/y.sh (missing here)", rv["lines"][0])
                n.approve(aid)
                os.makedirs(os.path.dirname(lit))
                Path(lit).write_text("echo EVIL-tilde > evil.txt\n")
                e = n.err(action, params, identity=CAUTIOUS, approval_id=aid)
                self.assertEqual(e["code"], "conflict")
                self.assertFalse((n.home / "w4" / "evil.txt").exists())

    def test_quoted_tilde_binds_the_literal_path(self):
        for cmd in ("bash '~/lib/q.sh'", 'bash "~/lib/q.sh"', "bash \\~/lib/q.sh"):
            with self.subTest(cmd=cmd), Node() as n:
                (n.home / "lib").mkdir()
                (n.home / "lib" / "q.sh").write_text("echo benign-q\n")
                aid, rv = self.review(n, "shell.run", {"command": cmd})
                lit = os.path.join(os.path.realpath(str(n.home)), "~", "lib", "q.sh")
                self.assertEqual({f["path"]: f["bind"] for f in rv["files"]}, {lit: "absent"})
                self.assertNotIn("echo benign-q", "\n".join(rv["lines"]))
                n.approve(aid)
                os.makedirs(os.path.dirname(lit))
                Path(lit).write_text("echo EVIL-quoted-tilde > evil.txt\n")
                e = n.err("shell.run", {"command": cmd}, identity=CAUTIOUS, approval_id=aid)
                self.assertEqual(e["code"], "conflict")
                self.assertFalse((n.home / "evil.txt").exists())

    def test_unquoted_tilde_and_home_variable(self):
        with Node() as n:
            (n.home / "lib").mkdir()
            (n.home / "lib" / "q.sh").write_text("echo benign-q\n")
            for cmd in ("bash ~/lib/q.sh", "bash $HOME/lib/q.sh"):
                with self.subTest(cmd=cmd):
                    _aid, rv = self.review(n, "shell.run", {"command": cmd})
                    # audit round 2: path = the path as named (re-resolved at consume), real = where it is now
                    self.assertEqual({f.get("real", f["path"]): f["bind"] for f in rv["files"]},
                                     {os.path.realpath(str(n.home / "lib" / "q.sh")): "sha256"})
                    self.assertIn("echo benign-q", rv["lines"])

    def test_cd_spellings_not_bound(self):
        with Node() as n:
            (n.home / "setup.sh").write_text("echo benign\n")
            (n.home / "sub").mkdir()
            for cmd in CD_SPELLINGS:
                with self.subTest(cmd=cmd):
                    _aid, rv = self.review(n, "shell.run", {"command": cmd})
                    self.assertEqual(rv["files"], [])
                    self.assertNotIn("echo benign", "\n".join(rv["lines"]))
                    self.assertIn("setup.sh (cwd may change)", rv["lines"][0])

    def test_env_file_bound(self):
        with Node() as n:
            (n.home / "envx.sh").write_text("echo benign-env\n")
            params = {"command": "echo hi", "env": {"BASH_ENV": "~/envx.sh"}}
            aid, rv = self.review(n, "shell.run", params)
            self.assertIn("changes what runs: BASH_ENV", rv["lines"][0])
            binds = {f["path"]: f["bind"] for f in rv["files"]}
            self.assertEqual(binds[os.path.realpath(str(n.home / "envx.sh"))], "sha256")
            self.assertIn("echo benign-env", rv["lines"])
            n.approve(aid)
            (n.home / "envx.sh").write_text("echo EVIL-env\n")
            e = n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)
            self.assertEqual(e["code"], "conflict")
            # a relative value is resolved against the cwd and bound even when missing
            _aid, rv = self.review(n, "shell.run", {"command": "echo hi", "env": {"BASH_ENV": "later.sh"}})
            self.assertEqual({f["path"]: f["bind"] for f in rv["files"]},
                             {os.path.realpath(str(n.home)) + "/later.sh": "absent"})


@unittest.skipUnless(HAS_BASH and POSIX, "needs bash")
class TestServiceBinding(unittest.TestCase):
    def setUp(self):
        for attr, val in (("TICK", 0.2), ("NO_HEALTH_ALIVE_S", 0.5)):
            p = mock.patch.object(services, attr, val)
            p.start()
            self.addCleanup(p.stop)
        self.n = Node()
        self.n.__enter__()
        self.addCleanup(self.n.__exit__, None, None, None)

    def rs(self, name):
        return self.n.rt.services.runtime(name)

    def test_service_argv_tilde_literal_is_approved_and_protected(self):
        n = self.n
        (n.home / "lib5").mkdir()
        (n.home / "lib5" / "x.py").write_text("print('benign decoy')\n")
        p = {"name": "s5", "argv": [PY, "~/lib5/x.py"], "restart": "on-failure", "health": "none",
             "restart_delay": 1}
        self.assertTrue(n.run_approved("service.define", p)["ok"])
        lit = os.path.join(os.path.realpath(str(n.home)), "~", "lib5", "x.py")
        self.assertIn(lit, self.rs("s5")["approved_files"])
        self.assertIsNone(self.rs("s5")["approved_files"][lit])
        # the agent cannot create the literal file (service code while defined)
        e = n.err("fs.write", {"path": "~/~/lib5/x.py", "content": "print('EVIL-s5')", "make_dirs": True},
                  identity=CAUTIOUS)
        self.assertEqual(e["code"], "policy_denied", e)
        # created out of band once the approved run has exited: the crash respawn refuses to run it
        wait_for(lambda: self.rs("s5")["state"] in ("backoff", "failed") or self.rs("s5")["restarts"], 30)
        os.makedirs(os.path.dirname(lit))
        Path(lit).write_text("print('EVIL-s5 never approved', flush=True)\n")
        wait_for(lambda: self.rs("s5")["state"] == "failed", 90)
        self.assertEqual(self.rs("s5")["last_error"], services.APPROVED_CHANGED_ERROR)
        self.assertNotIn("EVIL-s5", str(n.ok("service.logs", {"name": "s5"})))

    def test_start_review_shows_and_binds_changed_service_file(self):
        n = self.n
        (n.home / "lib").mkdir()
        x = n.home / "lib" / "x.py"
        x.write_text("import time\ntime.sleep(60)\n")
        self.assertTrue(n.run_approved("service.define", {"name": "s6", "argv": [PY, str(x)], "health": "none"})["ok"])
        n.ok("service.stop", {"name": "s6", "wait": 20})
        x.write_text("print('EVIL s6 edited while node down')\n")     # out of band
        e = n.err("service.start", {"name": "s6"}, identity=CAUTIOUS)
        self.assertEqual(e["code"], "approval_required", e)
        rv = n.rt.approvals.get(e["details"]["approval_id"])["review"]
        real = os.path.realpath(str(x))
        self.assertEqual([(f["path"], f["bind"]) for f in rv["files"]], [(real, "sha256")])
        self.assertIn("print('EVIL s6 edited while node down')", rv["lines"])
        self.assertNotIn("x.py (not resolvable here)", "\n".join(rv["lines"]))
        self.assertTrue(any(ln.startswith(f"--- file {real}") and "agents cannot change it" in ln
                            for ln in rv["lines"]), rv["lines"])


class TestHubBindingFake(_HubCase):
    def setUp(self):
        e, self.rtok = self.relay_entry()
        self.url, self.toks, self.hub = self.make_hub([e], agent_entry={"require_approval": ["shell.run"]})
        self.fake = FakeRelay(self.url, self.rtok, "pod-a")
        self.fake.info = dict(self.fake.info, os="Linux")
        self.client = connect(self.url, token=self.toks["agent"])
        self.addCleanup(self.client.close)

    def run_call(self, params, answer):
        self.fake.register()
        box = call_async(self.client.call, "shell.run", params, node="pod-a")
        for _ in range(30):
            for t in self.fake.poll().get("tasks") or []:
                self.fake.result(t["task_id"], answer(t))
            if not box["thread"].is_alive():
                break
        box["thread"].join(10)
        self.assertEqual(box["error"].code, "approval_required", box)
        return self.hub.approvals.get(box["error"].details["approval_id"])

    @staticmethod
    def stat(paths, real=None):
        def answer(t):
            p = t["params"]["path"]
            paths.append(p)
            if real and p in real:
                return ok_env({"path": real[p], "exists": True, "type": "file", "size": 5, "sha256": "ab" * 32,
                               "mtime": "2026-11-01T00:00:00.000Z"})
            if t["action"] == "fs.read":
                return ok_env({"content": "echo\n", "next_start_line": None, "long_lines_cut": False,
                               "version": "sha256:" + "ab" * 32})
            return ok_env({"path": p, "exists": False})
        return answer

    def test_literal_tilde_and_variables(self):
        # audit round 2: "./" segments of the named path are dropped (/w/./x -> /w/x; "~" inside a path is literal)
        for params, want in (({"command": "bash '~/lib/q.sh'", "cwd": "/w"}, "/w/~/lib/q.sh"),
                             ({"argv": ["bash", "~/lib/y.sh"], "cwd": "/w"}, "/w/~/lib/y.sh"),
                             ({"command": "bash $HOME/x.sh", "cwd": "/w"}, "$HOME/x.sh"),
                             ({"command": "echo hi", "env": {"BASH_ENV": "envx.sh"}, "cwd": "/w"}, "/w/envx.sh")):
            with self.subTest(params=params):
                paths = []
                rec = self.run_call(params, self.stat(paths))
                self.assertIn(want, paths)
                self.assertIn(want, rec["files"])
                self.assertIsNone(rec["files"][want])
                self.assertNotIn("/w/$HOME/x.sh", paths)

    def test_cd_spellings(self):
        for cmd in CD_SPELLINGS:
            with self.subTest(cmd=cmd):
                rec = self.run_call({"command": cmd, "cwd": "/w"}, self.stat([], {"/w/setup.sh": "/w/setup.sh"}))
                self.assertEqual(rec["files"], {})
                self.assertIn("setup.sh (cwd may change)", rec["review"]["lines"][0])

    def test_bound_path_is_the_named_path(self):
        rec = self.run_call({"command": "bash setup.sh", "cwd": "/w/glink"},
                            self.stat([], {"/w/glink/setup.sh": "/w/ga/setup.sh"}))
        self.assertEqual(list(rec["files"]), ["/w/glink/setup.sh"])
        f = rec["review"]["files"][0]
        self.assertEqual((f["path"], f["real"]), ("/w/glink/setup.sh", "/w/ga/setup.sh"))
        self.assertIn("--- file /w/ga/setup.sh (5 B, sha256 abababababab) ---", rec["review"]["lines"])


@unittest.skipUnless(HAS_BASH and POSIX, "needs bash")
class TestHubBindingRealNode(_HubCase):
    def setUp(self):
        self.hub_tok = util.new_token()
        self.nurl, _a, self.nrt = helpers.start_node_server(start_managers=True,
                                                            auth={"tokens": [hub_token_entry(self.hub_tok)]})
        self.addCleanup(self.nrt.test_stop)
        self.url, self.toks, self.hub = self.make_hub(
            [{"name": "desk", "mode": "direct", "url": self.nurl, "token": self.hub_tok}],
            agent_entry={"require_approval": ["shell.run"]})
        self.c = connect(self.url, token=self.toks["agent"])
        self.addCleanup(self.c.close)
        self.wd = Path(self.nrt.workdir) / f"r2-{os.getpid()}-{time.monotonic_ns()}"
        self.wd.mkdir()
        self.addCleanup(shutil.rmtree, str(self.wd), True)

    def request(self, params):
        with self.assertRaises(HiveError) as cm:
            self.c.call("shell.run", params, node="desk")
        self.assertEqual(cm.exception.code, "approval_required")
        aid = cm.exception.details["approval_id"]
        HttpTransport(self.url, self.toks["owner"]).request("POST", f"/v1/approvals/{aid}/approve", {})
        return aid

    def consume_conflicts(self, params, aid):
        with self.assertRaises(HiveError) as cm:
            self.c.call("shell.run", params, node="desk", approval_id=aid)
        self.assertEqual(cm.exception.code, "conflict")

    def test_quoted_tilde_and_argv_tilde(self):
        for params in ({"command": "bash '~/lib/q.sh'", "cwd": str(self.wd)},
                       {"argv": ["bash", "~/lib/q.sh"], "cwd": str(self.wd)}):
            with self.subTest(params=params):
                aid = self.request(params)
                (self.wd / "~" / "lib").mkdir(parents=True, exist_ok=True)
                (self.wd / "~" / "lib" / "q.sh").write_text("echo EVIL > evil.txt\n")
                self.consume_conflicts(params, aid)
                self.assertFalse((self.wd / "evil.txt").exists())
                shutil.rmtree(str(self.wd / "~"))

    def test_symlinked_cwd_retargeted_after_approval(self):
        for d, text in (("ga", "echo benign\n"), ("gb", "echo EVIL-g > evil.txt\n")):
            (self.wd / d).mkdir()
            (self.wd / d / "setup.sh").write_text(text)
        link = self.wd / "glink"
        os.symlink("ga", str(link))
        params = {"command": "bash setup.sh", "cwd": str(link)}
        aid = self.request(params)
        link.unlink()
        os.symlink("gb", str(link))
        self.consume_conflicts(params, aid)
        self.assertFalse((self.wd / "gb" / "evil.txt").exists())
        # not retargeted: the approved command runs
        link.unlink()
        os.symlink("ga", str(link))
        aid = self.request(params)
        out = self.c.call("shell.run", params, node="desk", approval_id=aid)
        self.assertIn("benign", out["output"])


if __name__ == "__main__":
    unittest.main()
