"""Security audit round 2 (PHASE0_NOTES "External review fixes" > "Audit round 2"): the script operand of a known
interpreter is bound whatever its name looks like (``bash run3`` with run3 missing at review is bound ``absent``),
interpreter and wrapper options with values are skipped to find it, other missing non-path words are listed in the
review header, and the node binds the path as the command names it (a symbolic link retargeted after approval is a
conflict, as on the hub)."""
from __future__ import annotations

import os
import shlex
import unittest

from hive import presets, proctree
from hive.client import connect
from tests.test_hub import _HubCase
from tests.wp2util import CAUTIOUS, PY, Node
from tests.wp5util import FakeRelay, call_async, ok_env

HAS_BASH = proctree.shell_path("bash") is not None
POSIX = os.name != "nt"


def plan(cmd, **kw):
    return presets.bind_plan_ex(cmd, **kw)


class TestScriptOperand(unittest.TestCase):
    """bind_plan_ex finds the script an interpreter runs (finding 1) and only that."""

    BOUND = [
        # (command, the script spelling bound)
        ("bash run3", "./run3"), ("sh run3", "./run3"), ("dash run3", "./run3"), ("zsh run3", "./run3"),
        ("ksh run3", "./run3"), ("fish run3", "./run3"), ("python3 foo2", "./foo2"), ("python foo2", "./foo2"),
        ("python3.11 foo2", "./foo2"), ("/usr/bin/python3 foo2", "./foo2"), ("pypy3 foo2", "./foo2"),
        ("py -3.11 foo2", "./foo2"), ("perl run3", "./run3"), ("ruby run3", "./run3"), ("node run3", "./run3"),
        ("nodejs run3", "./run3"), ("deno run -A run3", "./run3"), ("deno run3", "./run3"), ("bun run run3", "./run3"),
        ("bun run3", "./run3"), ("Rscript run3", "./run3"), ("R CMD BATCH run3", "./run3"), ("R -f run3", "./run3"),
        ("php run3", "./run3"), ("php -f run3", "./run3"), ("lua run3", "./run3"), ("luajit run3", "./run3"),
        ("tclsh run3", "./run3"), ("pwsh run3", "./run3"), ("pwsh -File run3", "./run3"),
        ("pwsh -NoProfile -ExecutionPolicy Bypass -f run3", "./run3"), ("source run3", "./run3"), (". run3", "./run3"),
        ("awk -f prog data", "./prog"), ("sed -f prog data", "./prog"), ("osascript run3", "./run3"),
        # interpreter options that take a value (the attacker's next step)
        ("python -X dev foo2", "./foo2"), ("python -Xdev foo2", "./foo2"), ("python -W ignore foo2", "./foo2"),
        ("python -uB -X importtime foo2", "./foo2"), ("python -E -s -- foo2", "./foo2"),
        ("python --check-hash-based-pycs always foo2", "./foo2"), ("bash -O extglob run3", "./run3"),
        ("bash +O extglob run3", "./run3"), ("bash -eo pipefail run3", "./run3"), ("bash -x -- run3", "./run3"),
        ("bash --norc --noprofile run3", "./run3"), ("zsh -o shwordsplit run3", "./run3"),
        ("node --require x run3", "./run3"), ("node -r x run3", "./run3"), ("node --import x run3", "./run3"),
        ("node --env-file .env run3", "./run3"), ("node --title t --inspect-port 9 run3", "./run3"),
        ("node --max-old-space-size=4096 run3", "./run3"), ("node inspect run3", "./run3"),
        ("perl -I dir run3", "./run3"), ("perl -Idir run3", "./run3"), ("perl -Mstrict -w run3", "./run3"),
        ("perl -i.bak run3", "./run3"), ("ruby -I lib -r json run3", "./run3"), ("ruby -W0 run3", "./run3"),
        ("php -d x=1 -c php.ini run3", "./run3"), ("lua -e 'x=1' run3", "./run3"),
        ("deno run --config c.json --allow-read=. run3", "./run3"), ("bun --preload p.ts run3", "./run3"),
        # wrappers that run the rest of the line
        ("env VAR=1 bash run3", "./run3"), ("env -i -u HOME bash run3", "./run3"), ("env -- bash run3", "./run3"),
        ("env -S 'bash run3'", "./run3"), ("nice bash run3", "./run3"), ("nice -n 5 bash run3", "./run3"),
        ("nice -5 bash run3", "./run3"), ("nice --adjustment=5 bash run3", "./run3"),
        ("timeout 10 bash run3", "./run3"), ("timeout -s KILL -k 5 10 bash run3", "./run3"),
        ("timeout --signal=KILL 10s python3 foo2", "./foo2"), ("sudo bash run3", "./run3"),
        ("sudo -u root -E -H bash run3", "./run3"), ("sudo -u root -- python3 foo2", "./foo2"),
        ("doas -u root bash run3", "./run3"), ("xargs bash run3", "./run3"), ("xargs -n 1 -P 4 bash run3", "./run3"),
        ("xargs -0 -I {} bash run3", "./run3"), ("nohup bash run3", "./run3"), ("setsid -f bash run3", "./run3"),
        ("stdbuf -oL bash run3", "./run3"), ("ionice -c 3 bash run3", "./run3"), ("chrt -f 99 bash run3", "./run3"),
        ("taskset -c 0 bash run3", "./run3"), ("exec -a name bash run3", "./run3"), ("command bash run3", "./run3"),
        ("builtin source run3", "./run3"), ("busybox sh run3", "./run3"), ("wsl -d Ubuntu -- bash run3", "./run3"),
        ("time -p bash run3", "./run3"), ("nice timeout 5 sudo -u x env A=1 python3 -X dev foo2", "./foo2"),
        ("uv run python run3", "./run3"), ("uv run --with rich --python 3.12 python run3", "./run3"),
        ("poetry run python run3", "./run3"), ("pipenv run python run3", "./run3"),
        ("conda run -n base python run3", "./run3"),
        # shell structure
        ("A=1 B=2 bash run3", "./run3"), ("if bash run3; then :; fi", "./run3"), ("echo x | bash run3", "./run3"),
        ("true && python3 foo2 2>&1 >log", "./foo2"), ("bash run3 &> log", "./run3"), ("! bash run3", "./run3"),
        # a command line handed to a shell is read again
        ("bash -c 'bash run3'", "./run3"), ("sh -c \"python3 -X dev foo2 arg\"", "./foo2"),
        ("fish -c 'bash run3'", "./run3"), ("sudo sh -c 'bash run3'", "./run3"),
        ("bash -c \"sh -c 'bash run3'\"", "./run3"),
        # env keys that load a file, set inline
        ("BASH_ENV=evil bash -c true", "./evil"), ("env BASH_ENV=evil bash -c true", "./evil"),
    ]

    def test_operand_bound(self):
        for cmd, want in self.BOUND:
            with self.subTest(cmd=cmd):
                words, moved, _u, _q = plan(cmd)
                self.assertIn(want, words + moved)

    def test_argv_forms(self):
        for argv, want in ((["bash", "run3"], "./run3"), ([PY, "foo2"], "./foo2"), ([PY, "-X", "dev", "foo2"], "./foo2"),
                           (["env", "VAR=1", "bash", "run3"], "./run3"), (["sh", "-c", "bash run3"], "./run3"),
                           (["cmd", "/c", "run3.bat"], "run3.bat"), (["pwsh", "-File", "run3"], "./run3"),
                           (["powershell", "-Command", "python run3"], "./run3"), (["node", "-r", "x", "s"], "./s"),
                           (["C:\\Python311\\python.exe", "foo2"], "./foo2")):
            with self.subTest(argv=argv):
                words, moved, _u, _q = plan(argv)
                self.assertIn(want, words + moved)      # ("-Command" also reads as a -C cwd option: moved)

    def test_not_overbroad(self):
        """Only the script operand is bound as a path: arguments, code, module names and option values are not."""
        for cmd, absent in (("echo run3", "./run3"), ("ls run3", "./run3"), ("make run3", "./run3"),
                            ("git commit -m msg", "./msg"), ("python3 a.py run3 x", "./run3"),
                            ("bash a.sh run3", "./run3"), ("python -c pass run3", "./run3"),
                            ("python -c pass run3", "./pass"), ("perl -e 1 run3", "./run3"),
                            ("node -e 1 run3", "./run3"), ("ruby -e 1 run3", "./run3"),
                            ("php -r 1 run3", "./run3"), ("Rscript -e 1 run3", "./run3"),
                            ("bash -s run3", "./run3"), ("bash - run3", "./run3"), ("python -X dev foo2", "./dev"),
                            ("bash -O extglob run3", "./extglob"), ("timeout 10 bash run3", "./10"),
                            ("sudo -u root bash run3", "./root"), ("awk '{print}' data", "./{print}"),
                            ("awk -f prog data", "./data"), ("sed s/a/b/ data", "./data"),
                            ("command -v bash", "./bash"), ("deno task run3", "./run3"),
                            ("node --run run3", "./run3"), ("sudo -l bash run3", "./run3"),
                            ("bash run3 run4", "./run4"), ("python -m run3", "./run3")):
            with self.subTest(cmd=cmd, absent=absent):
                words, moved, _u, _q = plan(cmd)
                self.assertNotIn(absent, words + moved)

    def test_python_module_files(self):
        words, _m, _u, quiet = plan("python3 -m tool")
        for p in ("./tool.py", "./tool/__init__.py", "./tool/__main__.py"):
            self.assertIn(p, words)
            self.assertIn(p, quiet)        # bound as missing without a header item each
        words = plan("python -m pkg.sub")[0]
        self.assertEqual([w for w in words if w.startswith("./")], ["./pkg.py", "./pkg/__init__.py", "./pkg/sub.py",
                                                                     "./pkg.pyc", "./pkg/__init__.pyc", "./pkg/sub.pyc"])
        for cmd in ("python -I -m tool", "python -P -m tool", "python -Im tool"):   # cwd not on sys.path
            with self.subTest(cmd=cmd):
                self.assertFalse([w for w in plan(cmd)[0] if w.startswith("./")])
        self.assertIn("./tool.py", plan("uv run -m tool")[0])

    def test_cmd_and_powershell(self):
        words, _m, _u, quiet = plan(["cmd", "/c", "run3 arg"])
        for ext in (".com", ".exe", ".bat", ".cmd"):
            self.assertIn("./run3" + ext, words)
            self.assertIn("./run3" + ext, quiet)
        self.assertIn("./run3.bat", plan("call run3", shell="cmd")[0])
        self.assertNotIn("./dir.exe", plan("dir run3", shell="cmd")[0])          # cmd builtins run first
        self.assertIn("./run3", plan("powershell -nop -c \"bash run3\"", shell="powershell")[0])
        self.assertNotIn("./run3", plan("python3 x.py; echo run3", shell="powershell")[0])
        _w, _m, unbound, _q = plan(["powershell", "-EncodedCommand", "ZQBjAGgAbwA="])
        self.assertTrue(any("encoded command" in u for u in unbound), unbound)
        self.assertIn("./run3", plan(["pwsh", "-wd", "sub", "-File", "run3"])[1])   # cwd changes: moved

    def test_cwd_changing_options_move_relative_words(self):
        for cmd in ("sudo -D sub bash run3", "env -C sub bash run3", "ruby -C sub run3", "bun --cwd sub run3",
                    "wsl --cd sub bash run3", "chroot /jail bash run3", "conda run --cwd sub python run3"):
            with self.subTest(cmd=cmd):
                words, moved, _u, _q = plan(cmd)
                self.assertIn("./run3", moved)
                self.assertNotIn("./run3", words)

    def test_quiet_and_listable(self):
        _w, _m, _u, quiet = plan("sudo -u root timeout 10 python3 -X dev foo2 alpha")
        for q in ("sudo", "root", "timeout", "10", "python3", "dev"):
            self.assertIn(q, quiet)
        for q in ("alpha", "./foo2"):
            self.assertNotIn(q, quiet)
        L = presets.listable_missing
        self.assertTrue(L("alpha", quiet))
        for w in ("python3", "10", "5s", "0.0.0.0:8000", "A=1", "fix the bug", "-x", ""):
            with self.subTest(w=w):
                self.assertFalse(L(w, quiet))

    def test_entry_files_redirects_and_start(self):
        """One step further: what else the interpreter runs for an operand, stdin scripts, Start-Process/start."""
        w = plan("python3 run3")[0]
        self.assertIn("./run3/__main__.py", w)                 # a folder operand runs its __main__.py
        w, _m, _u, quiet = plan("node run3")
        for p in ("./run3.js", "./run3.node", "./run3/index.js", "./run3/package.json"):
            self.assertIn(p, w)
            self.assertIn(p, quiet)
        self.assertNotIn("./app.js.js", plan("node app.js")[0])
        self.assertIn("./run3", plan("bash < run3")[0])         # stdin script
        self.assertIn("./in.txt", plan("python3 a.py < in.txt > out")[0])
        self.assertNotIn("./EOF", plan("bash <<EOF\necho hi\nEOF")[0])   # heredoc delimiter
        self.assertNotIn("./3", plan("sort <&3")[0])
        self.assertIn("./r1", plan("diff <(bash r1) f")[0])
        self.assertIn("./run3", plan("timeout -- 5 bash run3")[0])
        self.assertIn("./run3", plan("env -- A=1 bash run3")[0])
        self.assertIn("./run3", plan("Start-Process python -ArgumentList run3", shell="powershell")[0])
        self.assertIn("./run3.bat", plan("saps run3", shell="powershell")[0])
        self.assertIn("./run3.bat", plan('start "" /b run3', shell="cmd")[0])
        self.assertEqual([x for x in plan("call :lbl", shell="cmd")[0] if x.startswith("./")], [])

    def test_unbound_variable_operand_is_listed(self):
        self.assertIn("$S (variables; not bound)", plan("bash $S")[2])

    def test_nesting_is_bounded(self):
        cmd = "bash run3"
        for depth in range(1, 9):
            cmd = "sh -c " + shlex.quote(cmd)
            words, _m, unbound, _q = plan(cmd)
            if depth <= 3:
                self.assertIn("./run3", words, cmd)
            if depth >= 5:
                self.assertTrue(any("nested too deep" in u for u in unbound), (cmd, unbound))


@unittest.skipUnless(HAS_BASH and POSIX, "needs bash")
class TestNodeOperandBinding(unittest.TestCase):
    def review(self, n, params):
        e = n.err("shell.run", params, identity=CAUTIOUS)
        self.assertEqual(e["code"], "approval_required", e)
        aid = e["details"]["approval_id"]
        return aid, n.rt.approvals.get(aid)["review"]

    def test_missing_script_bound_absent_then_created(self):
        """The repro: ``bash run3`` with run3 absent at review, approved, then created -> conflict, never run."""
        cases = (({"command": "bash run3"}, "run3", "echo EVIL-run3 > evil.txt\n"),
                 ({"command": f"{PY} -X dev foo2"}, "foo2", "open('evil.txt', 'w').write('EVIL')\n"),
                 ({"argv": ["env", "A=1", "bash", "run4"]}, "run4", "echo EVIL > evil.txt\n"),
                 ({"command": "timeout 30 bash -c 'bash run5'"}, "run5", "echo EVIL > evil.txt\n"))
        for params, name, payload in cases:
            with self.subTest(params=params), Node() as n:
                aid, rv = self.review(n, params)
                home = os.path.realpath(str(n.home))
                binds = {f.get("real", f["path"]): f["bind"] for f in rv["files"]}
                self.assertEqual(binds.get(os.path.join(home, name)), "absent", rv["files"])
                self.assertIn(f"./{name} (missing here)", rv["lines"][0])
                n.approve(aid)
                (n.home / name).write_text(payload)
                e = n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)
                self.assertEqual(e["code"], "conflict", e)
                self.assertFalse((n.home / "evil.txt").exists())

    def test_missing_names_listed_in_header(self):
        with Node() as n:
            _aid, rv = self.review(n, {"command": "bash run3 alpha 10 FOO=1 -x"})
            head = rv["lines"][0]
            self.assertTrue(head.startswith("not bound to their content"), head)
            self.assertIn("alpha (no file here, not bound)", head)
            for w in ("bash (", "10 (", "FOO=1 (", "-x (", "run3 (no file"):
                self.assertNotIn(w, head)

    def test_existing_script_still_bound_and_swap_refused(self):
        with Node() as n:
            (n.home / "run3").write_text("echo benign-run3\n")
            params = {"command": "nice -n 5 bash -O extglob run3"}
            aid, rv = self.review(n, params)
            self.assertIn("echo benign-run3", rv["lines"])
            n.approve(aid)
            (n.home / "run3").write_text("echo EVIL > evil.txt\n")
            self.assertEqual(n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)["code"], "conflict")
            self.assertFalse((n.home / "evil.txt").exists())

    def test_symlink_retargeted_after_approval(self):
        """Finding 3: the node binds the named path, so a link retargeted between approve and spawn is refused."""
        with Node() as n:
            (n.home / "a.sh").write_text("echo benign-a\n")
            (n.home / "b.sh").write_text("echo EVIL-b > evil.txt\n")
            os.symlink("a.sh", str(n.home / "link.sh"))
            home = os.path.realpath(str(n.home))
            params = {"command": "bash link.sh"}
            aid, rv = self.review(n, params)
            f = rv["files"][0]
            self.assertEqual((f["path"], f["real"]), (os.path.join(home, "link.sh"), os.path.join(home, "a.sh")))
            self.assertIn(os.path.join(home, "link.sh"), n.rt.approvals.get(aid)["files"])
            n.approve(aid)
            os.unlink(str(n.home / "link.sh"))
            os.symlink("b.sh", str(n.home / "link.sh"))
            self.assertEqual(n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)["code"], "conflict")
            self.assertFalse((n.home / "evil.txt").exists())
            # the same file named twice (real path first, then the link): both paths are bound
            os.unlink(str(n.home / "link.sh"))
            os.symlink("a.sh", str(n.home / "link.sh"))
            params = {"command": "bash a.sh; bash link.sh"}
            aid, rv = self.review(n, params)
            self.assertEqual(sorted(f["path"] for f in rv["files"]),
                             [os.path.join(home, "a.sh"), os.path.join(home, "link.sh")])
            self.assertEqual(sum(1 for ln in rv["lines"] if ln.startswith("--- file ")), 1)
            n.approve(aid)
            os.unlink(str(n.home / "link.sh"))
            os.symlink("b.sh", str(n.home / "link.sh"))
            self.assertEqual(n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)["code"], "conflict")
            self.assertFalse((n.home / "evil.txt").exists())
            # not retargeted: runs
            os.unlink(str(n.home / "link.sh"))
            os.symlink("a.sh", str(n.home / "link.sh"))
            aid, _rv = self.review(n, {"command": "bash link.sh"})
            n.approve(aid)
            out = n.ok("shell.run", {"command": "bash link.sh"}, identity=CAUTIOUS, approval_id=aid)
            self.assertIn("benign-a", out["output"])


class TestHubOperandBinding(_HubCase):
    """The hub shares the planning (bind_plan_ex) and lists missing names the same way (fake relay node, as in
    test_ext_review2.TestHubBindingFake)."""

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

    def test_hub_binds_missing_operand(self):
        for params, want in (({"command": "bash run3 alpha", "cwd": "/w"}, "/w/run3"),
                             ({"command": "python3 -X dev foo2", "cwd": "/w"}, "/w/foo2"),
                             ({"argv": ["env", "A=1", "bash", "run4"], "cwd": "/w"}, "/w/run4")):
            with self.subTest(params=params):
                paths = []
                rec = self.run_call(params, self.stat(paths))
                self.assertIn(want, paths)
                self.assertIn(want, rec["files"])
                self.assertIsNone(rec["files"][want])
                head = rec["review"]["lines"][0]
                self.assertIn("(missing here)", head)
                self.assertNotIn("bash (", head)
                if "alpha" in str(params):
                    self.assertIn("alpha (no file here, not bound)", head)

    def test_hub_binds_second_path_to_same_file(self):
        rec = self.run_call({"command": "bash a.sh; bash link.sh", "cwd": "/w"},
                            self.stat([], {"/w/a.sh": "/w/a.sh", "/w/link.sh": "/w/a.sh"}))
        self.assertEqual(sorted(rec["files"]), ["/w/a.sh", "/w/link.sh"])


if __name__ == "__main__":
    unittest.main()
