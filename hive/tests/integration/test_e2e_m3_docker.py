"""M3 (§22.1 step 6): the ``docker`` preset end to end with a fake ``docker`` binary on the node's trusted PATH.

The fake CLI keeps a small container table in a state folder: ``run`` (foreground, like the preset's ``docker run
--rm``) records the container and serves an OpenAI-compatible stub on the published ``127.0.0.1:<port>``; ``stop``,
``rm -f``, ``inspect``, ``ps -q --filter label=…``, ``top``, ``version`` and ``info`` behave like the real CLI for what
Hive uses (§10.5, §10.6). Every call is logged with its argv and the values of the ``-e NAME`` variables it saw.

Through ``hive mcp --local`` (agent) and the Python client / CLI (owner):
* capabilities report docker; ``service_define preset=docker`` builds the §10.6 argv (``--rm --init --name hive-<s>``,
  node/service labels, ``-p 127.0.0.1:P:C``, the resolved ``-v`` with ``:ro``, ``-e NAME`` without values, image then
  args) and the service becomes healthy; ``service_status`` shows the container; ``model_list`` / ``model_chat`` reach
  the server inside; ``service_logs`` shows its output;
* a node restart leaves the container running and the service is adopted;
* ``service_control stop`` runs ``docker stop -t 30 hive-<s>`` then ``docker rm -f``;
* a container name taken by a container Hive did not create → the start fails without removing it; a leftover
  container with this node's labels is removed and replaced;
* refusals: ``--privileged`` / ``--network=host`` extra args, a ``~`` volume, a named volume;
* ``hive node panic`` stops the service's container and a labelled container that no service owns.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path
from typing import Any, List

from hive.errors import HiveError

from tests import helpers
from tests.integration import m2util
from tests.integration.m3util import IS_WINDOWS, NodeDaemon, wait_for

FAKE_DOCKER = r'''
import json, os, signal, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
ST = __STATE__
TABLE = os.path.join(ST, "containers.json")

def log(extra=None):
    rec = {"argv": sys.argv[1:], "t": time.time()}
    if extra:
        rec.update(extra)
    with open(os.path.join(ST, "calls.log"), "a") as f:
        f.write(json.dumps(rec) + "\n")

def load():
    try:
        with open(TABLE) as f:
            t = json.load(f)
    except (OSError, ValueError):
        t = {}
    out = {}
    for k, v in t.items():
        try:
            os.kill(v["pid"], 0)
            out[k] = v
        except OSError:
            pass
    return out

def save(t):
    tmp = TABLE + ".tmp%d" % os.getpid()
    with open(tmp, "w") as f:
        json.dump(t, f)
    os.replace(tmp, TABLE)

def find(t, ref):
    for k, v in t.items():
        if ref in (k, v["id"], v["id"][:12]):
            return k
    return None

args = sys.argv[1:]
cmd = args[0] if args else ""
if cmd == "version":
    log(); print("27.0.0-fake"); sys.exit(0)
if cmd == "info":
    log(); print('{"runc":{"path":"runc"}}'); sys.exit(0)
if cmd == "inspect":
    log()
    t = load()
    k = find(t, args[-1])
    if k is None:
        sys.stderr.write("Error: No such object: " + args[-1] + "\n"); sys.exit(1)
    c = t[k]
    if "Labels" in " ".join(args):
        print(json.dumps(c["labels"])); sys.exit(0)
    print(json.dumps({"Id": c["id"], "Name": "/" + k, "State": {"Status": "running", "Pid": c["pid"]},
                      "Config": {"Image": c["image"], "Labels": c["labels"]}}))
    sys.exit(0)
if cmd == "ps":
    log()
    t = load()
    want = [a.split("=", 1)[1] for a in args if a.startswith("label=")]
    for k, v in t.items():
        if all(("%s=%s" % tuple(w.split("=", 1))) in ["%s=%s" % kv for kv in v["labels"].items()] for w in want):
            print(v["id"][:12])
    sys.exit(0)
if cmd == "top":
    log()
    t = load()
    k = find(t, args[1])
    print("PID")
    if k:
        print(t[k]["pid"])
    sys.exit(0)
if cmd in ("stop", "rm"):
    log()
    t = load()
    refs = [a for a in args[1:] if not a.startswith("-")]
    if cmd == "stop" and "-t" in args:
        refs = [a for a in refs if a != args[args.index("-t") + 1]]
    for ref in refs:
        k = find(t, ref)
        if k is None:
            continue
        try:
            os.kill(t[k]["pid"], signal.SIGTERM)
        except OSError:
            pass
        end = time.time() + 5
        while time.time() < end:
            try:
                os.kill(t[k]["pid"], 0)
            except OSError:
                break
            time.sleep(0.05)
        print(ref)
    sys.exit(0)
if cmd == "run":
    opts = {"labels": {}, "env": {}, "volumes": [], "publish": None, "name": None}
    i = 1
    takes = {"--name", "--label", "-p", "-e", "-v", "--gpus"}
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a in takes:
            v = args[i + 1]
            if a == "--name": opts["name"] = v
            elif a == "--label": opts["labels"][v.split("=", 1)[0]] = v.split("=", 1)[1]
            elif a == "-p": opts["publish"] = v
            elif a == "-e": opts["env"][v] = os.environ.get(v)
            elif a == "-v": opts["volumes"].append(v)
            i += 2
        else:
            i += 1
    image, cargs = args[i], args[i + 1:]
    log({"env_seen": opts["env"]})
    t = load()
    if opts["name"] in t:
        sys.stderr.write("docker: Error response from daemon: Conflict. The container name is already in use.\n")
        sys.exit(125)
    host, hport, cport = opts["publish"].split(":")
    model = cargs[cargs.index("--served-model-name") + 1] if "--served-model-name" in cargs else image
    cid = ("%016x" % (abs(hash((opts["name"], time.time()))) & (2 ** 64 - 1))) * 4
    t[opts["name"]] = {"id": cid, "pid": os.getpid(), "labels": opts["labels"], "image": image, "args": cargs,
                       "port": int(hport), "volumes": opts["volumes"]}
    save(t)

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            p = self.path.split("?")[0].rstrip("/")
            if p == "/health":
                self._send(200, {"ok": True})
            elif p == "/v1/models":
                self._send(200, {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "vllm"}]})
            else:
                self._send(404, {"error": p})
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            text = "from container %s: %s" % (image, req["messages"][-1]["content"])
            self._send(200, {"id": "x", "object": "chat.completion", "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 1, "completion_tokens": 3}})
        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, int(hport)), H)

    def term(*a):
        t2 = load()
        t2.pop(opts["name"], None)
        save(t2)
        print("container %s stopped" % opts["name"], flush=True)
        os._exit(0)
    signal.signal(signal.SIGTERM, term)
    signal.signal(signal.SIGINT, term)
    print("container %s started image=%s args=%s" % (opts["name"], image, " ".join(cargs)), flush=True)
    srv.serve_forever()
log()
sys.exit(0)
'''


@unittest.skipIf(IS_WINDOWS, "the fake docker is a POSIX script (Windows nodes run Docker Desktop; lead's CI)")
class TestE2EM3Docker(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.node = None
        cls.extra: List[subprocess.Popen] = []
        cls.state = Path(__import__("tempfile").mkdtemp(prefix="hivem3dk-")).resolve()
        cls.sysbin = cls.state / "sysbin"             # outside every fs root: on the trusted PATH
        cls.sysbin.mkdir()
        docker = cls.sysbin / "docker"
        docker.write_text(f"#!{sys.executable}\n" + FAKE_DOCKER.replace("__STATE__", repr(str(cls.state))),
                          encoding="utf-8")
        docker.chmod(0o755)
        try:
            cls.node = NodeDaemon("m3docker", init_args=("--proxy", "off", "--mirror", "none"),
                                  path_prepend=(str(cls.sysbin),))
            cls.node.start()
            (cls.node.work / "models" / "qwen").mkdir(parents=True)
            (cls.node.work / "models" / "qwen" / "config.json").write_text("{}", encoding="utf-8")
            cls.mcp = cls.node.mcp("--profile", "full")
            cls.node_id = cls.node.call("node.info")["node_id"]
        except BaseException:
            cls.tearDownClass()
            raise

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.node is not None:
            cls.node.stop()
        for p in cls.extra:
            if p.poll() is None:
                p.kill()
                p.wait(10)
        for c in cls.containers().values():           # anything a failed test left behind
            try:
                os.kill(c["pid"], 15)
            except OSError:
                pass
        shutil.rmtree(str(cls.state), ignore_errors=True)

    # -------------------------------------------------------------------------------------------------------------
    @classmethod
    def containers(cls) -> dict:
        try:
            t = json.loads((cls.state / "containers.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        out = {}
        for k, v in t.items():
            try:
                os.kill(v["pid"], 0)
                out[k] = v
            except OSError:
                pass
        return out

    def calls(self) -> List[dict]:
        p = self.state / "calls.log"
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []

    def argvs(self) -> List[List[str]]:
        return [c["argv"] for c in self.calls()]

    def tool(self, name: str, args: dict, *, error: bool = False) -> str:
        text, is_err = self.mcp.tool(name, args)
        self.assertEqual(is_err, error, f"{name}: {text}")
        return text

    def define_err(self, preset_args: List[str], name: str = "bad") -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.node.call("service.define", {"name": name, "preset": "docker", "preset_args": preset_args,
                                              "start": False})
        return cm.exception

    def define_args(self, port: int) -> dict:
        return {"name": "vl", "preset": "docker",
                "preset_args": ["image=vllm/vllm-openai:v0.6.3", f"port={port}", "gpus=none",
                                f"volume={self.node.work / 'models'}:/models:ro",
                                "args=--model /models/qwen --served-model-name qwen-m3 --host 0.0.0.0",
                                "stop_timeout=30"],
                "env": ["VLLM_LOGGING_LEVEL=DEBUG"], "options": ["health_interval=2"], "wait": 60}

    # -------------------------------------------------------------------------------------------------------------
    def test_01_define_chat_restart_stop(self) -> None:
        info = self.node.call("node.info")
        self.assertTrue(info["capabilities"]["docker"], info["capabilities"])
        port = helpers.free_port()
        text = self.tool("service_define", self.define_args(port))
        self.assertIn("vl", text)
        st = self.node.call("service.status", {"name": "vl"})
        self.assertEqual((st["state"], st["health"]), ("running", "healthy"), st)
        self.assertEqual(st["container"]["name"], "hive-vl")
        self.assertEqual(st["container"]["state"], "running")
        self.assertEqual(st["container"]["image"], "vllm/vllm-openai:v0.6.3")
        self.assertEqual(st["endpoint"], f"http://127.0.0.1:{port}/v1")
        run = [c for c in self.calls() if c["argv"][0] == "run"][-1]
        argv = run["argv"]
        self.assertEqual(argv[:5], ["run", "--rm", "--init", "--name", "hive-vl"])
        self.assertIn(f"ai.hive.node={self.node_id}", argv)
        self.assertIn("ai.hive.service=vl", argv)
        self.assertEqual(argv[argv.index("-p") + 1], f"127.0.0.1:{port}:{port}")
        vol = argv[argv.index("-v") + 1]
        self.assertEqual(vol, f"{os.path.realpath(str(self.node.work / 'models'))}:/models:ro")
        self.assertEqual(argv[argv.index("-e") + 1], "VLLM_LOGGING_LEVEL")
        self.assertNotIn("DEBUG", " ".join(argv))                  # the value lives in the CLI's env only
        self.assertEqual(run["env_seen"], {"VLLM_LOGGING_LEVEL": "DEBUG"})
        img = argv.index("vllm/vllm-openai:v0.6.3")
        self.assertEqual(argv[img + 1:], ["--model", "/models/qwen", "--served-model-name", "qwen-m3", "--host",
                                          "0.0.0.0"])
        self.assertEqual(set(self.containers()), {"hive-vl"})

        text = self.tool("model_list", {"service": "vl"})
        self.assertIn("qwen-m3", text)
        text = self.tool("model_chat", {"service": "vl", "prompt": "ping"})
        self.assertIn("from container vllm/vllm-openai:v0.6.3: ping", text)
        text = self.tool("service_logs", {"name": "vl"})
        self.assertIn("container hive-vl started", text)
        self.assertIn("vl", self.tool("service_list", {}))

        # a node restart keeps the container and adopts the service
        pid = self.containers()["hive-vl"]["pid"]
        self.node.restart_daemon()
        st = self.node.call("service.status", {"name": "vl", "wait": 20})
        self.assertEqual(st["state"], "running", st)
        self.assertEqual(self.containers()["hive-vl"]["pid"], pid)
        self.assertIn("ping", self.node.call("model.chat", {"service": "vl", "prompt": "ping"})["content"])

        # stop: the preset's stop_command, then the post-exit rm
        n = len(self.calls())
        self.tool("service_control", {"op": "stop", "name": "vl", "wait": 30})
        after = [c["argv"] for c in self.calls()[n:]]
        self.assertIn(["stop", "-t", "30", "hive-vl"], after)
        wait_for(lambda: ["rm", "-f", "hive-vl"] in [c["argv"] for c in self.calls()[n:]], 20, what="docker rm -f")
        self.assertEqual(self.containers(), {})
        self.assertEqual(self.node.call("service.status", {"name": "vl"})["state"], "stopped")
        self.tool("service_control", {"op": "remove", "name": "vl", "wait": 20})

    def test_02_name_conflicts(self) -> None:
        port = helpers.free_port()
        # a container Hive did not create holds the name: refused, left alone
        foreign = self.start_container("hive-taken", {"owner": "someone"}, helpers.free_port())
        with self.assertRaises(HiveError):
            self.node.call("service.define", {"name": "taken", "preset": "docker",
                                              "preset_args": ["image=alpine:3", f"port={port}", "gpus=none"],
                                              "wait": 15})
        st = self.node.call("service.status", {"name": "taken"})
        self.assertEqual(st["state"], "failed", st)
        self.assertIn("Hive did not create", st["last_error"])
        self.assertIn("hive-taken", self.containers())
        self.assertNotIn(["rm", "-f", "hive-taken"], self.argvs())
        self.node.call("service.remove", {"name": "taken", "wait": 10})
        foreign.terminate()
        foreign.wait(10)
        # a leftover container with this node's labels (e.g. after a crash) is removed and replaced
        self.start_container("hive-left", {"ai.hive.node": self.node_id, "ai.hive.service": "left"},
                             helpers.free_port())
        port = helpers.free_port()
        self.node.call("service.define", {"name": "left", "preset": "docker",
                                          "preset_args": ["image=alpine:3", f"port={port}", "gpus=none"],
                                          "wait": 30})
        self.assertIn(["rm", "-f", "hive-left"], self.argvs())
        self.assertEqual(self.containers()["hive-left"]["port"], port)
        self.node.call("service.remove", {"name": "left", "wait": 30})
        wait_for(lambda: "hive-left" not in self.containers(), 20, what="container removed")

    def test_03_refusals(self) -> None:
        for extra in ("extra_arg=--privileged", "extra_arg=--network=host", "extra_arg=--pid=host"):
            e = self.define_err(["image=alpine:3", extra])
            self.assertEqual(e.code, "bad_request", (extra, e.to_dict()))
        e = self.define_err(["image=alpine:3", "volume=~:/home"])
        self.assertEqual(e.code, "policy_denied", e.to_dict())
        e = self.define_err(["image=alpine:3", "volume=models:/models"])
        self.assertEqual(e.code, "bad_request", e.to_dict())
        e = self.define_err(["image=alpine:3", "port=8000", "gpus=bogus!"])
        self.assertEqual(e.code, "bad_request", e.to_dict())
        self.assertFalse(any(a[0] == "run" and "hive-bad" in a for a in self.argvs()))

    def test_09_panic_stops_containers(self) -> None:
        port = helpers.free_port()
        self.node.call("service.define", {"name": "pv", "preset": "docker",
                                          "preset_args": ["image=alpine:3", f"port={port}", "gpus=none"],
                                          "wait": 30})
        stray = self.start_container("stray", {"ai.hive.node": self.node_id}, helpers.free_port())
        other = self.start_container("not-ours", {"ai.hive.node": "n_someoneelse0000"}, helpers.free_port())
        self.assertEqual(set(self.containers()), {"hive-pv", "stray", "not-ours"})
        # the owner's shell has the same PATH as the node (the fake docker); panic resolves docker on the node
        # policy's trusted PATH (temp dirs included, unlike the CLI's fail-safe default)
        r = m2util.run_cli(["node", "panic", "--yes"], env=self.node.node_env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        wait_for(lambda: set(self.containers()) == {"not-ours"}, 30,
                 what=f"labelled containers stopped ({r.stdout} {self.containers()} {self.argvs()[-8:]})")
        self.assertIsNotNone(stray.wait(10))
        self.assertIsNone(other.poll())
        r = self.node.cli("node", "resume")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        st = self.node.call("service.status", {"name": "pv"})
        self.assertEqual(st["state"], "stopped", st)              # not restarted by the supervisor
        wait_for(lambda: self.node.call("service.remove", {"name": "pv", "wait": 20}), 10,
                 what="remove after resume (the daemon re-reads PAUSED within 1 s)")

    # -------------------------------------------------------------------------------------------------------------
    def start_container(self, name: str, labels: dict, port: int) -> subprocess.Popen:
        argv = [str(self.sysbin / "docker"), "run", "--rm", "--name", name]
        for k, v in labels.items():
            argv += ["--label", f"{k}={v}"]
        argv += ["-p", f"127.0.0.1:{port}:80", "alpine:3"]
        p = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.extra.append(p)
        wait_for(lambda: name in self.containers(), 20, what=f"container {name}")
        return p


if __name__ == "__main__":
    unittest.main()
