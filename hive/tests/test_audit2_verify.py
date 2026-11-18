"""Verification of audit round 2 (PHASE0_NOTES "External review fixes" > "Audit round 2" > verification): script
text the review could not see (eval/iex, here-strings, text piped into a shell, trap/script/su/flock -c, uv --script,
launchers), names dropped from the header after a cwd change, per-occurrence quiet words, Python bytecode and node
addons beside a bound operand, cmd/PowerShell PATHEXT names, and exec paths written through symbolic links or a
gitfile."""
from __future__ import annotations

import os
import unittest

from hive import presets, proctree
from tests import test_audit2_binding as _a2
from tests.test_hub import _HubCase
from tests.wp1util import FILES, OPERATOR, FakeHome, build_policy
from tests.wp2util import CAUTIOUS, PY, Node

HAS_BASH = proctree.shell_path("bash") is not None
POSIX = os.name != "nt"


def plan(cmd, **kw):
    return presets.bind_plan_ex(cmd, **kw)


def bound_or_moved(cmd, **kw):
    w, m, _u, _q = plan(cmd, **kw)
    return w + m


class TestHiddenScriptText(unittest.TestCase):
    """Findings 1-3: the script reaches the interpreter through text the old scan did not read."""

    BOUND = [
        # eval joins its words and runs them (finding 1a)
        "eval bash run3", "eval 'bash run3'", "builtin eval bash run3", "eval \"nice -n 5 bash run3\"",
        # here-strings and piped text read by a shell with no script operand (finding 2)
        "bash <<< 'bash run3'", "sh <<<\"bash run3\"", "echo 'bash run3' | sh", "echo -n 'bash run3' | bash",
        "printf 'bash run3\\n' | bash -s", "cat run3 | sh", "echo x; echo 'bash run3' | bash -",
        # programs whose arguments are a command line
        "trap 'bash run3' EXIT", "script -qc 'bash run3' /dev/null", "script -q -c 'bash run3' /dev/null",
        "script --command='bash run3' /dev/null", "watch -n 5 'bash run3'", "watch bash run3",
        "su -c 'bash run3' bob", "su bob --command 'bash run3'", "runuser -u bob -- bash run3",
        "runuser -c 'bash run3' bob", "flock /tmp/x -c 'bash run3'", "flock -w 5 /tmp/x bash run3",
        "sg grp -c 'bash run3'", "sg grp 'bash run3'", "alias b='bash run3'",
        # arguments that look like a command line (planned late and quietly)
        "tmux new -d 'bash run3'", "screen -dmS s bash -c 'bash run3'", "xterm -e 'bash run3'",
        # substitutions run here
        "echo \"$(bash run3)\"", "echo `bash run3`", "x=$(bash run3)",
        # uv run --script / -s / --gui-script (finding 3), launchers (finding 9)
        "uv run --no-project --script run3", "uv run --no-project -s run3", "uv run --gui-script run3",
        "uv run --with rich -s run3", "torchrun --nproc-per-node 2 run3", "torchrun --nproc_per_node=2 run3",
        "accelerate launch --num_processes 2 --multi_gpu run3", "deepspeed --num_gpus 2 run3",
        # wrappers seen at home (finding 9c), csh family, env -S with the rest of the line (9d)
        "strace -f -o log bash run3", "nsenter -t 1 -m bash run3", "setpriv --reuid=1000 bash run3",
        "firejail --quiet bash run3", "unbuffer bash run3", "valgrind --tool=memcheck python3 run3",
        "numactl --cpunodebind=0 python3 run3", "xvfb-run -a python3 run3", "tcsh run3", "csh -f run3",
        "env -S'bash -x' run3", "env --split-string='bash -e' run3",
    ]

    def test_operand_bound(self):
        for cmd in self.BOUND:
            with self.subTest(cmd=cmd):
                self.assertIn("./run3", bound_or_moved(cmd))

    def test_bound_not_moved_without_cwd_change(self):
        for cmd in ("eval bash run3", "bash <<< 'bash run3'", "echo 'bash run3' | sh", "bash run3 # source",
                    "python3 run3 eval", "source run3", ". run3", "bash run3; cd sub", "bash run3 && pushd x"):
            with self.subTest(cmd=cmd):
                self.assertIn("./run3", plan(cmd)[0])

    def test_powershell_iex(self):
        for cmd in ('Invoke-Expression "bash run3"', 'iex "bash run3"', "iex -Command 'bash run3'"):
            with self.subTest(cmd=cmd):
                self.assertIn("./run3", bound_or_moved(cmd, shell="powershell"))

    def test_uv_target_and_modules(self):
        w, _m, _u, q = plan("uv run run3")             # an unknown target may be a script or folder uv runs
        for p in ("./run3", "./run3/__main__.py"):
            self.assertIn(p, w)
            self.assertIn(p, q)
        self.assertIn("./tool.pyc", plan("uv run -m tool")[0])
        self.assertNotIn("./pytest", plan("uv run python -m pytest")[0])

    def test_not_overbroad(self):
        for cmd, absent in (("git commit -m 'fix the bug'", "./the"), ("echo run3 | grep x", "./run3"),
                            ("trap - EXIT", "./EXIT"),
                            ("watch -n 5 ls", "./ls"), ("ssh host bash run3", "./run3"),
                            ("python3 -c 'print(1)' <<< 'bash run3'", "./run3"),
                            ("flock /tmp/x make", "./make")):
            with self.subTest(cmd=cmd):
                self.assertNotIn(absent, bound_or_moved(cmd))

    def test_command_text_arguments_are_quiet(self):
        w, _m, u, q = plan("git commit -m 'bash is fun'")
        self.assertIn("./is", w)                      # bound (absent) …
        for x in ("./is", "fun", "bash"):
            self.assertIn(x, q)                       # … without header noise
        self.assertEqual(u, [])
        _w, _m, u, _q = plan("ssh host 'bash run3'")
        self.assertIn("bash run3 (remote command; not read or bound)", u)


class TestCwdTracking(unittest.TestCase):
    """Findings 1b/1c and 10: the cwd is followed per command; nothing is dropped from the header."""

    def test_words_before_the_first_cd_stay_bound(self):
        w, m, _u, _q = plan("bash run3; cd sub; bash run4")
        self.assertIn("./run3", w)
        self.assertIn("./run4", m)
        for cmd in ("for i in 1; do bash run3; cd sub; done", "f() { bash run3; }; cd sub; f",
                    "trap 'bash run3' EXIT; cd sub", "alias b='bash run3'; cd sub; b", "sudo -D sub bash run3"):
            with self.subTest(cmd=cmd):
                w, m, _u, _q = plan(cmd)
                self.assertNotIn("./run3", w)
                self.assertIn("./run3", m)

    def test_dynamic_words_only_in_command_position(self):
        for cmd in ("bash run3 # source", "python3 train.py eval", "git log --grep=cd run3.sh",
                    "echo 'cd sub' && bash run3"):
            with self.subTest(cmd=cmd):
                self.assertEqual(plan(cmd)[1], [], cmd)
        for cmd in ("$X sub; bash run3", "`echo cd` sub; bash run3", "$(echo cd) sub; bash run3",
                    "eval \"$S\"; bash run3", "source env.sh; bash run3"):
            with self.subTest(cmd=cmd):
                self.assertIn("./run3", plan(cmd)[1], cmd)

    def test_moved_missing_names_are_listed(self):
        items = presets.moved_items(["run3", "bash", "sub", "run.sh", "10"], lambda w: False, frozenset({"bash"}),
                                    lambda w: w == "sub")
        self.assertEqual(items, ["run3 (no file here, not bound)", "run.sh (cwd may change)"])
        # without quiet (old callers) nothing new is listed
        self.assertEqual(presets.moved_items(["run3"], lambda w: False), [])

    def test_output_redirect_targets_not_bound(self):
        for cmd in ("bash run3 2>/dev/null", "bash run3 > /var/log/x.log", "bash run3 >> out.txt 2>&1"):
            with self.subTest(cmd=cmd):
                w, m, u, _q = plan(cmd)
                self.assertFalse([x for x in w + m if "/dev/null" in x or "x.log" in x or "out.txt" in x], w)
                self.assertEqual(u, [])
        self.assertIn("./in.txt", plan("python3 a.py < in.txt")[0])


class TestQuietPerOccurrence(unittest.TestCase):
    """Finding 9a/9b: a name hushed as a command is still listed where it is a plain argument; numbers only."""

    def test_quiet_is_per_occurrence(self):
        _w, _m, _u, q = plan("run3 2>/dev/null; flock /tmp/x cat run3")
        self.assertNotIn("run3", q)
        _w, _m, _u, q = plan("make; ls make")
        self.assertNotIn("make", q)
        _w, _m, _u, q = plan("node --title=web app.js web")
        self.assertNotIn("web", q)
        _w, _m, _u, q = plan("node --title=web app.js")
        self.assertIn("web", q)

    def test_numbers(self):
        L = presets.listable_missing
        for w in ("10", "5s", "4GiB", "0.0.0.0:8000", "1e-3", "80%", "3.11", "250ms", "2h"):
            with self.subTest(w=w):
                self.assertFalse(L(w, frozenset()))
        for w in ("1run", "2go", "9x", "0day"):
            with self.subTest(w=w):
                self.assertTrue(L(w, frozenset()))


class TestBytecodeAndAddons(unittest.TestCase):
    """Findings 4, 5 and 8: files the interpreter or Windows runs for an operand besides its name."""

    def test_python_sourceless_siblings(self):
        w, _m, _u, q = plan("python3 -m tool")
        for p in ("./tool.pyc", "./tool/__init__.pyc", "./tool/__main__.pyc"):
            self.assertIn(p, w)
            self.assertIn(p, q)
        w = plan("python3 foo2")[0]
        self.assertIn("./foo2/__main__.pyc", w)

    def test_node_index_node_and_folder_marker(self):
        w, _m, _u, q = plan("node run3")
        self.assertIn("./run3/index.node", w)
        self.assertIn(presets.OP_MARK + "./run3", q)
        self.assertNotIn(presets.OP_MARK + "./run3", plan("bash run3")[3])

    def test_pathext(self):
        for cmd, shell in (("cmd /c run3", "cmd"), ("call run3", "cmd"), ("start run3", "cmd"),
                           ("Start-Process run3", "powershell")):
            with self.subTest(cmd=cmd):
                w = plan(cmd, shell=shell)[0]
                for ext in (".vbs", ".js", ".wsf", ".bat"):
                    self.assertIn("./run3" + ext, w)
        for cmd in (".\\run3", "& .\\run3", "./run3"):
            with self.subTest(cmd=cmd):
                w, _m, _u, q = plan(cmd, shell="powershell")
                base = cmd.split()[-1]
                for ext in (".ps1", ".bat", ".vbs"):
                    self.assertIn(base + ext, w)
                    self.assertIn(base + ext, q)
        self.assertNotIn(".\\run3.ps1.ps1", plan(".\\run3.ps1", shell="powershell")[0])

    def test_absent_entries_do_not_use_the_file_cap(self):
        class Pol:
            def resolve_path(self, p, write=False):
                return p
        import tempfile
        d = tempfile.mkdtemp(prefix="hive-a2v-")
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        words = plan("cmd /c run3", shell="cmd")[0]
        files, _l, nb = presets.bind_files(words, policy=Pol(), cwd=d, mask=lambda s: s,
                                           quiet=plan("cmd /c run3", shell="cmd")[3])
        self.assertEqual({f["bind"] for f in files}, {"absent"})
        self.assertIn(os.path.join(d, "run3.vbs"), [f["path"] for f in files])
        self.assertIn(os.path.join(d, "cmd.wsh"), [f["path"] for f in files])      # 22 absent entries, none capped
        self.assertFalse([x for x in nb if "limit" in x or "run3" in x], nb)


@unittest.skipUnless(HAS_BASH and POSIX, "needs bash")
class TestNodeRepros(unittest.TestCase):
    """The repros on a real node: absent at review, created after approval -> conflict, the payload never runs."""

    def review(self, n, params):
        e = n.err("shell.run", params, identity=CAUTIOUS)
        self.assertEqual(e["code"], "approval_required", e)
        aid = e["details"]["approval_id"]
        return aid, n.rt.approvals.get(aid)["review"]

    def attack(self, params, rel, payload, prep=None):
        with Node() as n:
            if prep:
                prep(n)
            aid, rv = self.review(n, params)
            n.approve(aid)
            p = n.home / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payload)
            e = n.err("shell.run", params, identity=CAUTIOUS, approval_id=aid)
            self.assertEqual(e["code"], "conflict", (params, e, rv["files"]))
            self.assertFalse((n.home / "evil.txt").exists())
            return rv

    def test_hidden_script_text(self):
        sh = b"echo EVIL > evil.txt\n"
        for cmd in ("eval bash run3", 'bash -c "$(cat run3)"', 'eval "$(cat run3)"', "bash <<< 'bash run3'",
                    "echo 'bash run3' | sh", "cat run3 | bash", "trap 'bash run3' EXIT",
                    "script -qc 'bash run3' /dev/null", "env -S'bash -x' run3", "flock /tmp/x bash run3",
                    "watch -n 1 bash run3"):
            with self.subTest(cmd=cmd):
                self.attack({"command": cmd}, "run3", sh)
        # after a cd nothing relative can be bound: the operand is named in the header instead of dropped
        with Node() as n:
            _aid, rv = self.review(n, {"command": "cd .; flock /tmp/x bash run3"})
            self.assertIn("./run3 (cwd may change)", rv["lines"][0])
            self.assertNotIn("/tmp/x", rv["lines"][0])

    def test_uv_script_and_python_entry_files(self):
        py = b"open('evil.txt', 'w').write('EVIL')\n"
        for cmd, rel in (("uv run --no-project --script run3", "run3"), ("uv run --no-project -s run3", "run3"),
                         (f"{PY} -m tool", "tool.pyc"), (f"{PY} -m tool", "tool/__main__.pyc"),
                         ("node run3", "run3/index.node"), (f"{PY} foo2", "foo2/__main__.pyc")):
            with self.subTest(cmd=cmd, rel=rel):
                self.attack({"command": cmd}, rel, py)

    def test_existing_folder_operand_named_in_header(self):
        def prep(n):
            (n.home / "run3").mkdir()
        for cmd in ("node run3", f"{PY} run3"):
            with self.subTest(cmd=cmd):
                rv = self.attack({"command": cmd}, "run3/index.node" if cmd.startswith("node") else
                                 "run3/__main__.pyc", b"\x7fELF", prep)
                self.assertIn("./run3/ (folder; only its entry files are bound)", rv["lines"][0])

    def test_missing_name_after_cd_listed(self):
        with Node() as n:
            (n.home / "sub").mkdir()
            _aid, rv = self.review(n, {"command": "cd sub; flock /tmp/x make run9"})
            self.assertIn("run9 (no file here, not bound)", rv["lines"][0])
            self.assertNotIn("sub (", rv["lines"][0])
            self.assertNotIn("make (", rv["lines"][0])

    def test_quiet_name_still_listed_as_argument(self):
        with Node() as n:
            _aid, rv = self.review(n, {"command": "run3 2>/dev/null; flock /tmp/x cat run3; tcsh 1run"})
            head = rv["lines"][0]
            self.assertIn("run3 (no file here, not bound)", head)
            self.assertIn("./1run (missing here)", head)
            self.assertNotIn("/dev/null", head)

    def test_source_operand_bound_and_comment_ignored(self):
        with Node() as n:
            (n.home / "run3").write_text("echo benign-run3\n")
            for cmd in ("source run3", ". run3", "bash run3 # source"):
                with self.subTest(cmd=cmd):
                    aid, rv = self.review(n, {"command": cmd})
                    self.assertIn("echo benign-run3", rv["lines"])
                    self.assertNotIn("cwd may change", rv["lines"][0] if rv["lines"] else "")


class TestHubVerification(_HubCase):
    """The hub shares the plan: eval text and a uv --script operand are bound; a missing name after cd is listed."""

    setUp = _a2.TestHubOperandBinding.setUp
    run_call = _a2.TestHubOperandBinding.run_call
    stat = staticmethod(_a2.TestHubOperandBinding.stat)

    def test_hub_binds_hidden_operands(self):
        for params in ({"command": "eval bash run3", "cwd": "/w"}, {"command": "echo 'bash run3' | sh", "cwd": "/w"},
                       {"command": "uv run --no-project -s run3", "cwd": "/w"}):
            with self.subTest(params=params):
                paths = []
                rec = self.run_call(params, self.stat(paths))
                self.assertIn("/w/run3", rec["files"])
                self.assertIsNone(rec["files"]["/w/run3"])

    def test_hub_lists_missing_name_after_cd(self):
        rec = self.run_call({"command": "cd sub; flock /tmp/x make run9", "cwd": "/w"}, self.stat([]))
        self.assertIn("run9 (no file here, not bound)", rec["review"]["lines"][0])


class TestExecPathsVerification(unittest.TestCase):
    """Findings 4, 6, 7: bytecode, symbolic-link dotfiles and git redirects are exec paths for gated tokens."""

    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)

    def denied(self, eff, p):
        from hive.errors import HiveError
        with self.assertRaises(HiveError) as cm:
            eff.resolve_path(p, write=True)
        self.assertEqual(cm.exception.details.get("rule"), "exec_path", cm.exception.message)

    def test_bytecode(self):
        e = build_policy(self.fh).effective(FILES, paused=False)
        for p in ("~/proj/tool.pyc", "~/proj/__pycache__/tool.cpython-312.pyc", "~/proj/foo2/__main__.pyc",
                  "~/proj/__pycache__/notes.txt"):
            with self.subTest(p=p):
                self.denied(e, p)
        for p in ("~/proj/tool.py", "~/proj/pycache.txt", "~/proj/x.pyc.txt"):
            with self.subTest(p=p):
                e.resolve_path(p, write=True)
        build_policy(self.fh).effective(OPERATOR, paused=False).resolve_path("~/proj/tool.pyc", write=True)

    def test_dotfile_links_made_after_start(self):
        h = self.fh.home
        pol = build_policy(self.fh)            # built BEFORE the links exist (a dotfile manager run later)
        e = pol.effective(FILES, paused=False)
        (h / "dotfiles" / "config" / "systemd" / "user").mkdir(parents=True)
        (h / "dotfiles" / "bashrc").write_text("x")
        (h / ".config").mkdir()
        os.symlink("dotfiles/bashrc", str(h / ".bashrc"))
        os.symlink("../dotfiles/config/systemd", str(h / ".config" / "systemd"))
        pol._live_alts = (-1e9, [])            # the 2 s cache
        for p in ("~/.bashrc", "~/dotfiles/bashrc", "~/.config/systemd/user/evil.service",
                  "~/.config/systemd/user/../user/x.service", "~/dotfiles/config/systemd/user/y.service"):
            with self.subTest(p=p):
                self.denied(e, p)
        for p in ("~/dotfiles/vimrc-notes", "~/dotfiles/config/other/x"):
            with self.subTest(p=p):
                e.resolve_path(p, write=True)
        pol.effective(OPERATOR, paused=False).resolve_path("~/.bashrc", write=True)

    def test_link_into_protected_folder(self):
        h = self.fh.home
        (h / "proj" / ".git" / "hooks").mkdir(parents=True)
        os.symlink("proj/.git/hooks", str(h / "hk"))
        e = build_policy(self.fh).effective(FILES, paused=False)
        self.denied(e, "~/hk/pre-commit")
        os.symlink("proj/.git", str(h / "gd"))
        self.denied(e, "~/gd/config")

    def test_gitfile_and_bare_repo_shape(self):
        h = self.fh.home
        (h / "proj2").mkdir()
        e = build_policy(self.fh).effective(FILES, paused=False)
        self.denied(e, "~/proj2/.git")
        e.resolve_path("~/proj2/evilgit/HEAD", write=True)
        (h / "proj2" / "evilgit").mkdir()
        (h / "proj2" / "evilgit" / "HEAD").write_text("ref: refs/heads/main\n")
        self.denied(e, "~/proj2/evilgit/config")
        (h / "proj3").mkdir()
        (h / "proj3" / "config").write_text("[core]\n")
        self.denied(e, "~/proj3/HEAD")
        e.resolve_path("~/proj3/config.yaml", write=True)
        e.resolve_path("~/proj2/head.txt", write=True)
        build_policy(self.fh).effective(OPERATOR, paused=False).resolve_path("~/proj2/evilgit/config", write=True)


if __name__ == "__main__":
    unittest.main()
