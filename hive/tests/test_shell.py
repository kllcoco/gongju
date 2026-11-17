"""WP2 shell.run tests (§9.4, §10.4 shell.run flow, §21.2 shell.run row incl. the v8 approval file-binding cases)."""
from __future__ import annotations

import json
import os
import shutil
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from hive import jobs, presets, proctree, util
from hive.errors import HiveError
from tests import helpers
from tests.wp2util import CAUTIOUS, OPERATOR, PY, FakeRt, Node, assert_result_schema, handler_call, wait_for

POSIX = os.name != "nt"
HAS_BASH = proctree.shell_path("bash") is not None


class ShellCase(unittest.TestCase):
    def setUp(self):
        self.rt = FakeRt()
        self.rt.jobs = jobs.JobManager(self.rt)
        self.rt.jobs.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for jid, r in list(self.rt.jobs._jobs.items()):
            if r.get("state") in ("starting", "running"):
                self.rt.jobs.stop(jid, force=True)
        self.rt.jobs.shutdown()
        self.rt.cleanup()

    def run_(self, params, **kw):
        r = handler_call("shell.run", params, runtime=self.rt, **kw)
        assert_result_schema(self, "shell.run", r)
        return r


class TestShells(ShellCase):
    def test_default_shell_utf8(self):
        r = self.run_({"command": "echo 中文 && echo second"})
        self.assertEqual((r["state"], r["exit_code"]), ("exited", 0))
        self.assertEqual(r["output"].splitlines(), ["中文", "second"])
        self.assertIsNone(r["job_id"])
        self.assertEqual(r["shell"], proctree.default_shell())

    @unittest.skipUnless(POSIX, "POSIX shells")
    def test_each_posix_shell(self):
        for name in ("bash", "sh", "zsh"):
            if proctree.shell_path(name) is None:
                continue
            r = self.run_({"command": "echo $0 中文; exit 5", "shell": name})
            self.assertEqual((r["exit_code"], r["shell"]), (5, name), name)
            self.assertIn("中文", r["output"])

    @unittest.skipUnless(proctree.shell_path("pwsh") or proctree.shell_path("powershell"), "needs PowerShell")
    def test_powershell_exit_semantics(self):  # pragma: no cover - needs PowerShell on the CI runner
        shell = "pwsh" if proctree.shell_path("pwsh") else "powershell"
        self.assertEqual(self.run_({"command": "exit 3", "shell": shell})["exit_code"], 3)
        self.assertEqual(self.run_({"command": "Get-Item C:/does/not/exist/x", "shell": shell})["exit_code"], 1)
        r = self.run_({"command": "Write-Output '中文'", "shell": shell})
        self.assertEqual((r["exit_code"], r["output"].strip()), (0, "中文"))

    @unittest.skipUnless(util.IS_WINDOWS, "cmd is Windows-only")
    def test_cmd_chcp(self):  # pragma: no cover - Windows only
        r = self.run_({"command": "echo 中文", "shell": "cmd"})
        self.assertEqual(r["output"].strip(), "中文")

    def test_argv_and_split_streams(self):
        r = self.run_({"argv": [PY, "-c", "import sys; print('o'); sys.stderr.write('e\\n'); sys.exit(2)"],
                       "merge_stderr": False})
        self.assertEqual((r["stdout"], r["stderr"], r["exit_code"], r["shell"]), ("o\n", "e\n", 2, "none"))
        self.assertNotIn("output", r)

    def test_stdin(self):
        r = self.run_({"argv": [PY, "-c", "import sys; print(sys.stdin.read()[::-1])"], "stdin": "abc"})
        self.assertEqual(r["output"].strip(), "cba")

    def test_cwd(self):
        sub = self.rt.workdir / "sub"
        sub.mkdir()
        r = self.run_({"argv": [PY, "-c", "import os; print(os.getcwd())"], "cwd": str(sub)})
        self.assertEqual((r["output"].strip(), r["cwd"]), (str(sub), str(sub)))


class TestTimeouts(ShellCase):
    def test_shim_owned_timeout(self):
        t0 = time.monotonic()
        with mock.patch.object(proctree, "kill_tree", wraps=proctree.kill_tree) as kt:
            r = self.run_({"argv": [PY, "-c", "import time; time.sleep(60)"], "timeout": 1})
        self.assertEqual(r["state"], "timeout")
        self.assertLess(time.monotonic() - t0, 12)
        kt.assert_not_called()          # the node never kills on timeout itself; the shim does
        self.assertIsNone(r["job_id"])

    def test_timeout_above_node_limit(self):
        with self.assertRaises(HiveError) as cm:
            self.run_({"command": "true", "timeout": 7200})
        self.assertIn("max timeout on this node is 3600", cm.exception.hint)

    def test_yield_after_promotes_to_job(self):
        r = self.run_({"argv": [PY, "-c", "import time; print('start', flush=True); time.sleep(2); print('end')"],
                       "yield_after": 1})
        self.assertEqual(r["state"], "running")
        self.assertTrue(r["job_id"])
        self.assertIn("start", r["output"])
        rec = self.rt.jobs.record(r["job_id"])
        self.assertEqual(rec["kind"], "job")
        st = handler_call("job.status", {"job_id": r["job_id"], "wait": 10}, runtime=self.rt)
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0))
        self.assertIn("end", st["tail"])
        self.assertTrue(any(j["job_id"] == r["job_id"] for j in handler_call("job.list", {}, runtime=self.rt)["jobs"]))

    def test_auto_yield_before_call_deadline(self):
        t0 = time.monotonic()
        r = self.run_({"argv": [PY, "-c", "import time; time.sleep(20)"], "timeout": 30}, timeout=8)
        self.assertEqual(r["state"], "running")
        self.assertLess(time.monotonic() - t0, 7)          # yield_after = remaining - 5
        self.assertEqual(self.rt.jobs.record(r["job_id"])["timeout"], 30.0)   # the job keeps its shim timeout

    def test_on_timeout_background(self):
        t0 = time.monotonic()
        r = self.run_({"argv": [PY, "-c", "import time; time.sleep(3); print('late')"], "timeout": 1,
                       "on_timeout": "background"})
        self.assertEqual(r["state"], "running")
        self.assertLess(time.monotonic() - t0, 3)
        self.assertEqual(self.rt.jobs.record(r["job_id"])["timeout"], 0.0)
        st = handler_call("job.status", {"job_id": r["job_id"], "wait": 10}, runtime=self.rt)
        self.assertEqual(st["state"], "exited")

    def test_cancel_stops_tree(self):
        pf = self.rt.workdir / "gc.pid"
        code = ("import subprocess,sys,time\n"
                "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
                f"open({str(pf)!r},'w').write(str(p.pid)); time.sleep(60)")
        ctx = helpers.make_ctx("shell.run", runtime=self.rt, timeout=60)
        from hive import catalog, schema
        from hive.actions import HANDLERS
        p = schema.validate(catalog.SPECS["shell.run"].params, {"argv": [PY, "-c", code]})
        box = {}

        def go():
            try:
                HANDLERS["shell.run"](ctx, p)
            except HiveError as e:
                box["err"] = e
        t = threading.Thread(target=go)
        t.start()
        wait_for(pf.exists)
        gc = int(pf.read_text())
        ctx.cancel.set()
        t.join(20)
        self.assertEqual(box["err"].code, "cancelled")
        wait_for(lambda: not util.pid_alive(gc), 10)


class TestOutput(ShellCase):
    def test_truncation_keeps_tail(self):
        code = "import sys\nfor i in range(1000): sys.stdout.write('line %04d\\n' % i)"
        r = self.run_({"argv": [PY, "-c", code], "max_output": 2048})
        self.assertTrue(r["truncated"])
        self.assertEqual(r["output_bytes"], 10000)
        head, _, tail = r["output"].partition("bytes omitted]…")
        self.assertTrue(head.startswith("line 0000\n"))
        self.assertTrue(r["output"].rstrip().endswith("line 0999"))
        self.assertLess(len(head), len(tail))                 # 25 % head, 75 % tail
        self.assertIn("line 0900", tail)
        jid = r["job_id"]
        self.assertTrue(jid)
        rec = self.rt.jobs.record(jid)
        self.assertEqual((rec["kind"], rec["ephemeral"]), ("shell", True))
        full = handler_call("job.logs", {"job_id": jid, "offset": 0, "max_bytes": 1048576}, runtime=self.rt)
        self.assertEqual(full["size"], 10000)
        lst = handler_call("job.list", {"include_shell": True}, runtime=self.rt)
        self.assertTrue(any(j["job_id"] == jid for j in lst["jobs"]))
        self.assertFalse(any(j["job_id"] == jid for j in handler_call("job.list", {}, runtime=self.rt)["jobs"]))

    def test_untruncated_job_dir_removed(self):
        r = self.run_({"command": "echo hi"})
        self.assertIsNone(r["job_id"])
        self.assertEqual(list(self.rt.jobs.dir.iterdir()), [])


class TestEnvAndPath(unittest.TestCase):
    def test_env_secret_env_scrub(self):
        with Node() as n:
            helpers.set_secret(n.rt, "MY_API_KEY", "sk-secretvalue-123456789")
            os.environ["HIVE_SCRUB_ME"] = "nope"
            try:
                code = ("import os; print(os.environ.get('A'), os.environ.get('HIVE_SCRUB_ME'), "
                        "os.environ.get('MY_API_KEY'), os.environ.get('PYTHONUNBUFFERED'), "
                        "'HOME' in os.environ)")
                r = n.ok("shell.run", {"argv": [PY, "-c", code], "env": ["A=1"], "secret_env": ["MY_API_KEY"]})
            finally:
                os.environ.pop("HIVE_SCRUB_ME", None)
            self.assertEqual(r["output"].split(), ["1", "None", "«secret:MY_API_KEY»", "1", "True"])
            self.assertTrue(r["masked"])
            # env_unset and explicit env win over child_env defaults
            r = n.ok("shell.run", {"argv": [PY, "-c", "import os; print(os.environ.get('PYTHONUNBUFFERED'))"],
                                   "env": ["PYTHONUNBUFFERED=0"]})
            self.assertEqual(r["output"].strip(), "0")
            e = n.err("shell.run", {"argv": [PY, "-c", "pass"], "secret_env": ["NOT_SET_HERE"]})
            self.assertEqual(e["code"], "not_found")

    @unittest.skipUnless(POSIX, "POSIX executable bits")
    def test_argv0_resolved_with_node_path_not_agent_env(self):
        with Node() as n:
            planted = n.base / "evilbin"
            planted.mkdir()
            tool = planted / "hive-planted-tool"
            tool.write_text("#!/bin/sh\necho planted\n")
            tool.chmod(0o755)
            e = n.err("shell.run", {"argv": ["hive-planted-tool"], "env": [f"PATH={planted}"]})
            self.assertEqual(e["code"], "not_found")
            r = n.ok("shell.run", {"argv": [str(tool)]})
            self.assertEqual(r["output"].strip(), "planted")


# ---------------------------------------------------------------------------------------------------------------
# v8 approval file binding (needs the real WP1 pipeline)

@unittest.skipUnless(HAS_BASH, "needs bash")
class TestApprovalBinding(unittest.TestCase):
    def review(self, n, action, params):
        e = n.err(action, params, identity=CAUTIOUS)
        self.assertEqual(e["code"], "approval_required", e)
        aid = e["details"]["approval_id"]
        return aid, n.rt.approvals.get(aid)["review"]

    def test_rewrite_after_approval_conflicts(self):
        with Node() as n:
            run = n.home / "run.sh"
            run.write_text("echo approved > out.txt\n")
            aid, rv = self.review(n, "shell.run", {"command": "bash run.sh"})
            self.assertEqual(set(rv), {"action", "lines", "changes_what_runs", "files"})
            f = [x for x in rv["files"] if x["path"] == str(run)][0]
            self.assertEqual((f["bind"], f["shown"]), ("sha256", True))
            self.assertIn("echo approved > out.txt", rv["lines"])
            self.assertTrue(any(x.startswith(f"--- file {run} (") for x in rv["lines"]))
            n.approve(aid)
            run.write_text("echo evil > out.txt\n")
            e = n.err("shell.run", {"command": "bash run.sh"}, identity=CAUTIOUS, approval_id=aid)
            self.assertEqual(e["code"], "conflict")
            self.assertIn("a file used by the approved command changed; request approval again", e["message"])
            self.assertFalse((n.home / "out.txt").exists())
            # unchanged file -> runs
            env = n.run_approved("shell.run", {"command": "bash run.sh"})
            self.assertTrue(env["ok"], env)
            self.assertEqual((n.home / "out.txt").read_text().strip(), "evil")

    def test_nine_files_cap_and_header(self):
        with Node() as n:
            names = [f"f{i}.py" for i in range(9)]
            for nm in names:
                (n.home / nm).write_text("print(1)\n")
            _aid, rv = self.review(n, "shell.run", {"command": "cat " + " ".join(names)})
            binds = [f["bind"] for f in rv["files"]]
            self.assertEqual(binds.count("sha256"), 8)
            self.assertEqual(binds[-1], "none")
            self.assertTrue(rv["lines"][0].startswith("not bound to their content (may change after approval): "))
            self.assertIn("f8.py (over the 8-file limit)", rv["lines"][0])

    def test_large_scripts(self):
        with Node() as n:
            big = n.home / "big.sh"
            big.write_text("echo x\n" * (150 * 1024))          # ~1 MiB text: hashed, not shown
            huge = n.home / "model.gguf"
            with open(huge, "wb") as fh:
                fh.truncate(20 << 20)                            # 20 MiB sparse: stat-bound
            hashed = []
            real = presets.hashlib.sha256

            def spy(data=b""):
                hashed.append(len(data))
                return real(data)
            with mock.patch.object(presets.hashlib, "sha256", spy):
                aid, rv = self.review(n, "shell.run", {"command": f"bash big.sh {huge.name}"})
            by = {Path(f["path"]).name: f for f in rv["files"]}
            self.assertEqual((by["big.sh"]["bind"], by["big.sh"]["shown"]), ("sha256", False))
            self.assertEqual((by["model.gguf"]["bind"], by["model.gguf"]["shown"]), ("stat", False))
            self.assertTrue(by["model.gguf"]["fingerprint"].startswith("stat:"))
            self.assertFalse(any(x >= (16 << 20) for x in hashed))
            self.assertIn(presets.NOT_SHOWN, rv["lines"])
            self.assertNotIn("echo x", rv["lines"])
            n.approve(aid)
            env = n.call("shell.run", {"command": f"bash big.sh {huge.name} >/dev/null"}, identity=CAUTIOUS,
                         approval_id=aid)
            self.assertEqual(env["error"]["code"], "forbidden")     # different params -> other approval

    def test_absent_then_created_conflicts(self):
        with Node() as n:
            aid, rv = self.review(n, "shell.run", {"command": "bash setup.sh"})
            f = [x for x in rv["files"] if x["path"].endswith("setup.sh")][0]
            self.assertEqual(f["bind"], "absent")
            self.assertIn("setup.sh (missing here)", rv["lines"][0])
            n.approve(aid)
            (n.home / "setup.sh").write_text("echo surprise\n")
            e = n.err("shell.run", {"command": "bash setup.sh"}, identity=CAUTIOUS, approval_id=aid)
            self.assertEqual(e["code"], "conflict")

    def test_cd_sub_header(self):
        with Node() as n:
            (n.home / "sub").mkdir()
            (n.home / "sub" / "run.sh").write_text("echo hi\n")
            _aid, rv = self.review(n, "shell.run", {"command": "cd sub && bash run.sh"})
            self.assertIn("run.sh (missing here)", rv["lines"][0])

    def test_outside_roots_and_exec_paths_not_bound(self):
        with Node() as n:
            outside = n.base / "outside.sh"
            outside.write_text("echo out\n")
            (n.home / ".bashrc").write_text("alias x=y\n")
            _aid, rv = self.review(n, "shell.run", {"command": f"bash {outside} ~/.bashrc"})
            paths = [f["path"] for f in rv["files"]]
            self.assertNotIn(str(outside), paths)
            self.assertNotIn(str(n.home / ".bashrc"), paths)
            self.assertIn("(not resolvable here)", rv["lines"][0])

    def test_secret_in_script_masked(self):
        with Node() as n:
            secret = "hf_" + "Q" * 30
            helpers.set_secret(n.rt, "HF_TOKEN", secret)
            (n.home / "push.sh").write_text(f"curl -H 'Authorization: Bearer {secret}' https://x\n")
            _aid, rv = self.review(n, "shell.run", {"command": "bash push.sh"})
            text = "\n".join(rv["lines"])
            self.assertIn("«secret:HF_TOKEN»", text)
            self.assertNotIn(secret, text)

    def test_env_values_and_stdin_in_review(self):
        with Node() as n:
            _aid, rv = self.review(n, "shell.run", {"command": "ls", "env": ["BASH_ENV=/home/me/tmp/x.sh",
                                                                           "PATH=/home/me/tmp:/usr/bin",
                                                                           "API_TOKEN=abcdefgh12345678"]})
            self.assertEqual(rv["lines"][0], "changes what runs: BASH_ENV, PATH")
            self.assertEqual(rv["changes_what_runs"], ["BASH_ENV", "PATH"])
            self.assertIn("env BASH_ENV=/home/me/tmp/x.sh", rv["lines"])
            self.assertNotIn("abcdefgh12345678", "\n".join(rv["lines"]))
            _aid, rv = self.review(n, "shell.run", {"command": "python3 -", "stdin": "import os\nprint('hi')"})
            self.assertIn("stdin:", rv["lines"])
            self.assertIn("print('hi')", rv["lines"])

    def test_hold_lasts_until_yielded_job_exits(self):
        with Node() as n:
            run = n.home / "slow.sh"
            run.write_text("sleep 2\necho done\n")
            env = n.run_approved("shell.run", {"command": "bash slow.sh", "yield_after": 1})
            self.assertTrue(env["ok"], env)
            self.assertEqual(env["result"]["state"], "running")
            gated = n.rt.effective(CAUTIOUS)
            with self.assertRaises(HiveError) as cm:
                gated.resolve_path(str(run), write=True)
            self.assertEqual((cm.exception.code, cm.exception.details.get("rule")), ("conflict", "approved_input"))
            n.rt.effective(OPERATOR).resolve_path(str(run), write=True)     # operators on standard are not gated
            self.assertTrue(n.rt.jobs.wait_final(env["result"]["job_id"], 15))
            gated.resolve_path(str(run), write=True)                        # released when the job exited


if __name__ == "__main__":
    unittest.main()
