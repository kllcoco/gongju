"""WP2 presets tests (§10.6, §9.6, §5.6; §21.2 jobs/services row preset items): builders, args table errors,
platforms, the v6 given-key override rule, docker golden argv and volume rules, approval_review shapes."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from hive import catalog, presets, schema
from hive.errors import HiveError
from tests import helpers
from tests.wp2util import OPERATOR, Node

POSIX = os.name != "nt"


def pctx(policy, *, os_name="linux", secrets=(), docker=True, node_id="n_test0000000000"):
    return presets.PresetCtx(os=os_name, node_id=node_id, policy=policy, secret_names=list(secrets), docker=docker)


def define_params(**kw):
    """Validated service.define params (defaults applied) and the given set."""
    given = frozenset(k for k, v in kw.items() if v is not None)
    return schema.validate(catalog.SPECS["service.define"].params, kw), given


def as_wire(definition: dict) -> dict:
    """A stored definition expressed as service.define params (for schema validation)."""
    d = {k: v for k, v in definition.items() if k in presets.DEF_KEYS and k not in ("preset", "preset_args")}
    d["env"] = [f"{k}={v}" for k, v in (definition.get("env") or {}).items()]
    return d


class TestBuilders(unittest.TestCase):
    def setUp(self):
        self.pol = helpers.FakeEffectivePolicy()

    def test_every_preset_builds_a_valid_definition(self):
        args = {"vllm": [("model", "Qwen/Qwen2.5-7B-Instruct")], "sglang": [("model", "Qwen/Qwen2.5-7B")],
                "ollama": [], "llamacpp": [("model_path", __file__)], "comfyui": [("dir", os.path.dirname(__file__))],
                "docker": [("image", "vllm/vllm-openai:v0.6.0")]}
        for name, a in args.items():
            p, given = define_params(name=f"svc-{name}", preset=name,
                                     preset_args=[f"{k}={v}" for k, v in a])
            d = presets.expand_define(p, ctx=pctx(self.pol), given=given)
            schema.validate(catalog.SPECS["service.define"].params, as_wire(d))
            self.assertTrue(d["argv"], name)
            self.assertEqual(d["preset"], name)
            if name != "comfyui":
                self.assertTrue(d["endpoint"].startswith("http://127.0.0.1:"), name)
            if name != "docker":
                self.assertTrue("127.0.0.1" in " ".join(d["argv"]) or d["env"].get("OLLAMA_HOST", "").startswith(
                    "127.0.0.1"), name)

    def test_vllm_golden(self):
        p, given = define_params(name="qwen", preset="vllm",
                                 preset_args=["model=Qwen/Qwen2.5-7B-Instruct", "max_model_len=32768",
                                              "extra_arg=--enable-prefix-caching", "extra_arg=--max-num-seqs=8",
                                              "gpus=0,1", "tp=2"])
        d = presets.expand_define(p, ctx=pctx(self.pol, secrets=["HF_TOKEN"]), given=given)
        self.assertEqual(d["argv"], ["python3", "-m", "vllm.entrypoints.openai.api_server", "--model",
                                     "Qwen/Qwen2.5-7B-Instruct", "--served-model-name", "Qwen/Qwen2.5-7B-Instruct",
                                     "--host", "127.0.0.1", "--port", "8000", "-tp", "2", "--max-model-len", "32768",
                                     "--enable-prefix-caching", "--max-num-seqs=8"])
        self.assertEqual((d["start_period"], d["stop_timeout"], d["keep_awake"]), (1800, 60, True))
        self.assertEqual((d["health_url"], d["endpoint"]), ("http://127.0.0.1:8000/health", "http://127.0.0.1:8000/v1"))
        self.assertEqual(d["secret_env"], ["HF_TOKEN"])       # repo id + existing exec secret
        self.assertEqual(d["gpus"], "0,1")

    def test_given_keys_override_defaults_do_not(self):
        p, given = define_params(name="q", preset="vllm", preset_args=["model=Qwen/Q"])
        d = presets.expand_define(p, ctx=pctx(self.pol), given=given)
        self.assertEqual((d["start_period"], d["stop_timeout"], d["keep_awake"]), (1800, 60, True))
        p, given = define_params(name="q", preset="vllm", preset_args=["model=Qwen/Q"], start_period=300,
                                 env=["A=1"], secret_env=["MY_KEY"], health_url="http://127.0.0.1:8000/v1/models")
        d = presets.expand_define(p, ctx=pctx(self.pol, secrets=["HF_TOKEN"]), given=given)
        self.assertEqual(d["start_period"], 300)
        self.assertEqual(d["health_url"], "http://127.0.0.1:8000/v1/models")
        self.assertEqual(d["env"], {"A": "1"})
        self.assertEqual(d["secret_env"], ["HF_TOKEN", "MY_KEY"])   # union incl. the auto-added name
        # a forwarder (hub) sends params validated with defaults=False: same result
        raw = {"name": "q", "preset": "vllm", "preset_args": ["model=Qwen/Q"]}
        fw = schema.validate(catalog.SPECS["service.define"].params, raw, defaults=False)
        fw = schema.validate(catalog.SPECS["service.define"].params, fw)
        self.assertEqual(presets.expand_define(fw, ctx=pctx(self.pol), given=frozenset(raw))["start_period"], 1800)

    def test_top_level_port(self):
        p, given = define_params(name="o", preset="ollama", port=11500)
        d = presets.expand_define(p, ctx=pctx(self.pol), given=given)
        self.assertEqual((d["port"], d["env"]["OLLAMA_HOST"]), (11500, "127.0.0.1:11500"))
        p, given = define_params(name="o", preset="ollama", port=11500, preset_args=["port=11500"])
        presets.expand_define(p, ctx=pctx(self.pol), given=given)
        p, given = define_params(name="o", preset="ollama", port=11500, preset_args=["port=12000"])
        with self.assertRaises(HiveError) as cm:
            presets.expand_define(p, ctx=pctx(self.pol), given=given)
        self.assertEqual(cm.exception.code, "bad_request")

    def test_repeated_and_multi_args(self):
        args = [("model", "a/b"), ("model", "c/d"), ("extra_arg", "--x=1"), ("extra_arg", "--y")]
        d = presets.build("sglang", args, "s", ctx=pctx(self.pol))
        self.assertIn("c/d", d["argv"])
        self.assertNotIn("a/b", d["argv"])
        self.assertEqual(d["argv"][-2:], ["--x=1", "--y"])

    def test_bad_args(self):
        for args, frag in (([("model", "x/y"), ("bogus", "1")], "unknown arg"), ([], "missing required"),
                           ([("model", "x/y"), ("port", "99999")], "port must be between"),
                           ([("model", "x/y"), ("tp", "two")], "tp must be an integer")):
            with self.assertRaises(HiveError) as cm:
                presets.build("vllm", args, "v", ctx=pctx(self.pol))
            e = cm.exception
            self.assertEqual(e.code, "bad_request")
            self.assertIn(frag, e.message)
            self.assertEqual([a["name"] for a in e.details["args"]], [a["name"] for a in presets.PRESETS["vllm"].args])
            self.assertEqual(e.hint, presets.PRESETS["vllm"].example)
        p, given = define_params(name="v", preset="vllm", preset_args=["novalue"])
        with self.assertRaises(HiveError) as cm:
            presets.expand_define(p, ctx=pctx(self.pol), given=given)
        self.assertIn("args", cm.exception.details)

    def test_platform_refusal(self):
        for os_name, hint in (("windows", "WSL node"), ("darwin", "ollama or llamacpp")):
            for name in ("vllm", "sglang"):
                with self.assertRaises(HiveError) as cm:
                    presets.build(name, [("model", "x/y")], "v", ctx=pctx(self.pol, os_name=os_name))
                self.assertEqual(cm.exception.code, "unsupported")
                self.assertIn(hint, cm.exception.hint)
        self.assertIn("vLLM needs Linux", str(cm.exception) + cm.exception.message.replace("SGLang", "vLLM"))

    def test_python_default_per_os(self):
        d = presets.build("comfyui", [("dir", os.path.dirname(__file__))], "c", ctx=pctx(self.pol))
        self.assertEqual(d["argv"][0], "python3")
        d = presets.build("comfyui", [("dir", os.path.dirname(__file__))], "c", ctx=pctx(self.pol, os_name="windows"))
        self.assertEqual(d["argv"][0], "python")
        d = presets.build("vllm", [("model", "a/b"), ("python", "~/.venvs/vllm/bin/python")], "v", ctx=pctx(self.pol))
        self.assertEqual(d["argv"][0], os.path.expanduser("~/.venvs/vllm/bin/python"))

    def test_bind_rules(self):
        with self.assertRaises(HiveError) as cm:
            presets.build("vllm", [("model", "a/b"), ("extra_arg", "--host=0.0.0.0")], "v", ctx=pctx(self.pol))
        self.assertIn("model gateway", cm.exception.message)
        # abbreviations argparse accepts and values merely containing a bind flag are refused too
        for bad in ("--hos=0.0.0.0", "--ho", "--listen0.0.0.0", "--bind-address=0.0.0.0", "--LISTEN", "--li",
                    "--b=0.0.0.0", "x--host"):
            with self.assertRaises(HiveError, msg=bad):
                presets.build("vllm", [("model", "a/b"), ("extra_arg", bad)], "v", ctx=pctx(self.pol))
        for ok in ("--max-model-len=4096", "--hf-overrides={}", "--load-format", "-tp", "--lora-modules"):
            presets.build("vllm", [("model", "a/b"), ("extra_arg", ok)], "v", ctx=pctx(self.pol))
        p, given = define_params(name="o", preset="ollama", env=["OLLAMA_HOST=0.0.0.0:11434"])
        with self.assertRaises(HiveError):
            presets.expand_define(p, ctx=pctx(self.pol), given=given)
        # the docker preset is exempt: the server inside the container listens on 0.0.0.0
        p, given = define_params(name="dk", preset="docker", env=["OLLAMA_HOST=0.0.0.0"],
                                 preset_args=["image=ollama/ollama", "args=serve --host 0.0.0.0"])
        d = presets.expand_define(p, ctx=pctx(self.pol), given=given)
        self.assertEqual(d["argv"][-3:], ["serve", "--host", "0.0.0.0"])

    def test_local_model_rules(self):
        work = Path(helpers.FakeRuntime().workdir)
        (work / "models" / "Qwen").mkdir(parents=True)
        (work / "models" / "Qwen" / ".hive-pull.json").write_text(json.dumps({"repo": "Qwen/Qwen2.5-7B-Instruct"}))
        pol = helpers.FakeEffectivePolicy(workdir=work)
        d = presets.build("vllm", [("model", "models/Qwen")], "v", ctx=pctx(pol, secrets=["HF_TOKEN"]))
        self.assertEqual(d["secret_env"], [])
        self.assertIn(str(work / "models" / "Qwen"), d["argv"])
        self.assertEqual(d["argv"][d["argv"].index("--served-model-name") + 1], "Qwen/Qwen2.5-7B-Instruct")
        d = presets.build("vllm", [("model", "Qwen/Qwen2.5-7B")], "v", ctx=pctx(pol, secrets=["HF_TOKEN"]))
        self.assertEqual(d["secret_env"], ["HF_TOKEN"])
        d = presets.build("vllm", [("model", "Qwen/Qwen2.5-7B")], "v", ctx=pctx(pol))
        self.assertEqual(d["secret_env"], [])                     # no such secret -> nothing added
        d = presets.build("vllm", [("model", "/data/x")], "v", ctx=pctx(pol, secrets=["HF_TOKEN"]))
        self.assertEqual((d["secret_env"], d["argv"][d["argv"].index("--served-model-name") + 1]), ([], "x"))

    def test_describe(self):
        rows = presets.describe()
        self.assertEqual([r["name"] for r in rows], ["vllm", "sglang", "ollama", "llamacpp", "comfyui", "docker"])
        self.assertEqual(rows[0]["platforms"], ["linux"])
        self.assertTrue(all(r["example"] and r["args"] for r in rows if r["name"] != "ollama"))


class TestDocker(unittest.TestCase):
    def setUp(self):
        self.pol = helpers.FakeEffectivePolicy()

    def build(self, *args, name="vl", pol=None, docker=True):
        return presets.build("docker", [tuple(a.split("=", 1)) for a in args], name,
                             ctx=pctx(pol or self.pol, docker=docker))

    def test_golden_argv(self):
        d = self.build("image=vllm/vllm-openai:v1", "port=8001", "container_port=8000", "shm_size=8g",
                       "args=--model /models/q --served-model-name q --host 0.0.0.0", "extra_arg=--ipc=host")
        self.assertEqual(d["argv"], ["docker", "run", "--rm", "--init", "--name", "hive-vl", "--label",
                                     "ai.hive.node=n_test0000000000", "--label", "ai.hive.service=vl",
                                     "--pull=missing", "--shm-size=8g", "--gpus", "all", "-p", "127.0.0.1:8001:8000",
                                     "--ipc=host", "vllm/vllm-openai:v1", "--model", "/models/q",
                                     "--served-model-name", "q", "--host", "0.0.0.0"])
        self.assertEqual(d["stop_command"], ["docker", "stop", "-t", "30", "hive-vl"])
        self.assertEqual(d["post_exit"], ["docker", "rm", "-f", "hive-vl"])
        self.assertEqual((d["container"], d["stop_timeout"], d["start_period"]), ("hive-vl", 40.0, 1800))
        self.assertEqual(d["endpoint"], "http://127.0.0.1:8001/v1")
        self.assertNotIn("endpoint", self.build("image=x", "openai=false"))

    def test_gpus(self):
        def gpu_part(v):
            a = self.build("image=x", f"gpus={v}")["argv"]
            return a[a.index("--shm-size=16g") + 1:a.index("-p")]
        self.assertEqual(gpu_part("all"), ["--gpus", "all"])
        self.assertEqual(gpu_part("0,1"), ["--gpus", '"device=0,1"'])
        self.assertEqual(gpu_part("none"), [])
        self.assertEqual(gpu_part("amd"), ["--device=/dev/kfd", "--device=/dev/dri", "--group-add=video"])

    def test_refusals(self):
        for bad in ("--privileged", "--pid=host", "--network=host", "--net=host", "--userns=host", "--volume=/:/x",
                    "--name=x", "--rm", "--gpus=all", "-v", "--env=A=1", "positional", "-it"):
            with self.assertRaises(HiveError, msg=bad) as cm:
                self.build("image=x", f"extra_arg={bad}")
            self.assertEqual(cm.exception.code, "bad_request", bad)
        for img in ("-bad", "bad image", ""):
            with self.assertRaises(HiveError):
                self.build(f"image={img}")
        with self.assertRaises(HiveError) as cm:
            self.build("image=x", docker=False)
        self.assertEqual(cm.exception.code, "unsupported")

    def test_env_names_only(self):
        p, given = define_params(name="dk", preset="docker", preset_args=["image=x"], env=["A=secretvalue", "B=2"],
                                 secret_env=["HF_TOKEN"])
        d = presets.expand_define(p, ctx=pctx(self.pol), given=given)
        argv = d["argv"]
        img = argv.index("x")
        self.assertEqual(argv[img - 6:img], ["-e", "A", "-e", "B", "-e", "HF_TOKEN"])
        self.assertNotIn("secretvalue", " ".join(argv))
        self.assertEqual(d["secret_env"], ["HF_TOKEN"])            # explicit only; the docker preset adds none
        p, given = define_params(name="dk", preset="docker", preset_args=["image=x"])
        self.assertEqual(presets.expand_define(p, ctx=pctx(self.pol, secrets=["HF_TOKEN"]),
                                               given=given)["secret_env"], [])

    def test_container_name_pattern(self):
        name = "a" * 64
        d = self.build("image=x", name=name)
        import re
        self.assertRegex(d["container"], re.compile(r"^hive-[a-z0-9][a-z0-9._-]{0,63}$"))

    def test_volume_split(self):
        self.assertEqual(presets._split_volume(r"D:\models:/models:ro"), (r"D:\models", "/models", "ro"))
        self.assertEqual(presets._split_volume("/srv/x:/x"), ("/srv/x", "/x", None))
        for bad in ("name:/x", "/srv/x", "/srv:relative"):
            with self.assertRaises(presets._ArgError):
                presets._split_volume(bad)
        with self.assertRaises(HiveError):
            self.build("image=x", "volume=named:/data")

    def test_volumes_with_real_policy(self):
        with Node() as n:
            eff = n.rt.effective(OPERATOR)
            models = n.home / "models"
            models.mkdir()
            (n.home / ".ssh").mkdir()
            (n.home / ".ssh" / "id_rsa").write_text("k")
            link = n.home / "mlink"
            link.symlink_to(models) if POSIX else None
            d = self.build(f"image=x", "volume=~/models:/models", pol=eff)
            self.assertIn(f"{models}:/models", d["argv"])
            if POSIX:
                d = self.build("image=x", "volume=~/mlink:/m:ro", pol=eff)
                self.assertIn(f"{models}:/m:ro", d["argv"])      # -v carries the resolved realpath
                outside = n.base / "outside"
                outside.mkdir()
                (n.home / "escape").symlink_to(outside)
                with self.assertRaises(HiveError):
                    self.build("image=x", "volume=~/escape:/e", pol=eff)
            with self.assertRaises(HiveError) as cm:
                self.build("image=x", "volume=~:/home", pol=eff)       # ~ contains ~/.ssh
            self.assertEqual(cm.exception.details.get("rule"), "contains_denied")
            self.assertIn("subfolder", cm.exception.hint)

    def test_ro_root_forces_ro(self):
        with Node(policy={"fs": {"roots": [{"path": "~", "mode": "rw"}, {"path": "~/ro", "mode": "ro"}]}}) as n:
            (n.home / "ro").mkdir()
            eff = n.rt.effective(OPERATOR)
            d = self.build("image=x", "volume=~/ro:/data", pol=eff)
            self.assertIn(f"{n.home / 'ro'}:/data:ro", d["argv"])
            with self.assertRaises(HiveError) as cm:
                self.build("image=x", "volume=~/ro:/data:rw", pol=eff)
            self.assertEqual(cm.exception.code, "policy_denied")


class TestApprovalReview(unittest.TestCase):
    KEYS = {"action", "lines", "changes_what_runs", "files"}

    def test_hub_forms(self):
        r = presets.approval_review("shell.run", {"command": "ls", "env": ["PATH=/x"]}, ctx=None)
        self.assertEqual(set(r), self.KEYS)
        self.assertEqual(r["files"], [])
        d = presets.approval_review("service.define", {"name": "q", "preset": "vllm"}, ctx=None)
        self.assertEqual(d["note"], presets.HUB_DEFINE_NOTE)
        self.assertEqual(set(d), self.KEYS | {"note"})
        s = presets.approval_review("service.restart", {"name": "q"}, ctx=None)
        self.assertEqual(s["note"], presets.HUB_START_NOTE)
        self.assertIn("name: q", s["lines"])

    def test_other_actions_redacted(self):
        r = presets.approval_review("fs.delete", {"path": "/x", "recursive": True}, ctx=None)
        self.assertEqual(r["lines"][0], "action: fs.delete")
        self.assertIn('path: "/x"', r["lines"])
        w = presets.approval_review("fs.write", {"path": "/x", "content": "very secret text"}, ctx=None)
        self.assertNotIn("very secret text", "\n".join(w["lines"]))

    def test_node_define_shows_built_definition(self):
        pol = helpers.FakeEffectivePolicy()
        r = presets.approval_review("service.define", {"name": "q", "preset": "vllm",
                                                       "preset_args": ["model=Qwen/Q"], "env": ["X_TOKEN=abc123456789"]},
                                    ctx=pctx(pol, secrets=["HF_TOKEN"]),
                                    given=frozenset({"name", "preset", "preset_args", "env"}))
        text = "\n".join(r["lines"])
        self.assertIn("vllm.entrypoints.openai.api_server", text)
        self.assertIn("secret_env: HF_TOKEN (added by the preset: HF_TOKEN)", text)
        self.assertNotIn("abc123456789", text)
        self.assertNotIn("note", r)

    def test_node_restart_shows_stored_definition(self):
        pol = helpers.FakeEffectivePolicy()
        svc = {"name": "q", "argv": ["python3", "-m", "http.server"], "cwd": None, "defined_by": "op/claude"}
        r = presets.approval_review("service.restart", {"name": "q"}, ctx=pctx(pol), service=svc)
        self.assertIn('argv: ["python3", "-m", "http.server"]', r["lines"])
        self.assertIn("defined_by: op/claude", r["lines"])
        self.assertTrue(any(x.startswith("argv[0] modified: ") for x in r["lines"]))
        self.assertTrue(any(x.startswith("cwd: ") for x in r["lines"]))

    def test_too_large(self):
        with self.assertRaises(HiveError) as cm:
            presets.approval_review("shell.run", {"command": "cat", "stdin": "x" * (300 << 10)}, ctx=None)
        self.assertEqual((cm.exception.code, cm.exception.hint), ("too_large", "split the input or ask the owner"))

    def test_hundred_kb_command_shown_in_full(self):
        cmd = "echo " + "a" * 100_000 + " TAIL_MARK"
        r = presets.approval_review("shell.run", {"command": cmd}, ctx=None)
        self.assertTrue(any("TAIL_MARK" in x for x in r["lines"]))

    def test_candidate_words(self):
        self.assertEqual(presets.candidate_words("python3 run.py --cfg=conf.yaml -v X=1"),
                         ["python3", "run.py", "conf.yaml", "X=1", "1"])
        self.assertEqual(presets.candidate_words('& "C:\\a b.ps1" -x', shell="powershell"), ["C:\\a", "b.ps1"])
        self.assertEqual(presets.candidate_words(["bash", "s.sh"]), ["bash", "s.sh"])


if __name__ == "__main__":
    unittest.main()
