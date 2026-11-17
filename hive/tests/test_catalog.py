"""catalog.py (P0, §4.3, §9): 38 specs, flags, patterns, removed names, deadline, results with `masked`."""
from __future__ import annotations

import re
import unittest

from hive import catalog, schema
from hive.actions import HANDLERS, load_all
from hive.catalog import RISKS, SPECS
from hive.errors import HiveError

# §9.1 summary table: action -> (risk, extra, flags, default_timeout, deadline_params)
TABLE = {
    "node.info": ("read", (), "I", 10, ()),
    "system.info": ("read", (), "I", 30, ()),
    "proc.list": ("read", (), "I", 30, ()),
    "proc.kill": ("exec", (), "D", 30, ("grace",)),
    "shell.run": ("exec", (), "DLO", 15, ("timeout",)),
    "job.start": ("exec", (), "O", 30, ()),
    "job.status": ("read", (), "I", 30, ("wait",)),
    "job.logs": ("read", (), "I", 30, ("wait",)),
    "job.list": ("read", (), "I", 30, ()),
    "job.stop": ("exec", (), "DI", 30, ("grace",)),
    "service.define": ("persist", ("exec",), "", 30, ("wait",)),
    "service.list": ("read", (), "I", 30, ()),
    "service.status": ("read", (), "I", 30, ("wait",)),
    "service.start": ("exec", (), "IL", 30, ("wait",)),
    "service.stop": ("exec", (), "DI", 30, ("wait",)),
    "service.restart": ("exec", (), "DL", 30, ("wait",)),
    "service.remove": ("persist", (), "D", 30, ("wait",)),
    "service.logs": ("read", (), "I", 30, ("wait",)),
    "fs.list": ("read", (), "I", 30, ()),
    "fs.stat": ("read", (), "I", 120, ()),
    "fs.read": ("read", (), "I", 30, ()),
    "fs.read_chunk": ("read", (), "I", 60, ()),
    "fs.write": ("write", (), "D", 60, ()),
    "fs.write_chunk": ("write", (), "", 120, ()),
    "fs.edit": ("write", (), "", 60, ()),
    "fs.search": ("read", (), "IL", 60, ()),
    "fs.mkdir": ("write", (), "I", 30, ()),
    "fs.move": ("write", (), "D", 120, ()),
    "fs.copy": ("write", (), "", 600, ()),
    "fs.delete": ("write", (), "D", 300, ()),
    "net.http": ("net", (), "O", 15, ("timeout",)),
    "net.download": ("net", ("write",), "OL", 30, ()),
    "net.ports": ("read", (), "I", 30, ()),
    "model.endpoints": ("read", (), "I", 20, ("timeout",)),
    "model.chat": ("net", (), "L", 15, ("timeout",)),
    "model.pull": ("net", ("write",), "OLI", 60, ()),
    "model.forward": ("net", (), "L", 15, ("timeout",)),
    "hub.nodes": ("read", (), "I", 10, ()),
}

ID_PARAMS = {  # action -> {param: pattern}
    "job.status": {"job_id": catalog.JOB_ID_PATTERN}, "job.logs": {"job_id": catalog.JOB_ID_PATTERN},
    "job.stop": {"job_id": catalog.JOB_ID_PATTERN},
    "model.forward": {"service": catalog.SERVICE_NAME_PATTERN},
}
for _a in ("service.define", "service.status", "service.start", "service.stop", "service.restart", "service.remove",
           "service.logs"):
    ID_PARAMS[_a] = {"name": catalog.SERVICE_NAME_PATTERN}


def walk_props(schema_: dict, path: str = ""):
    for name, ps in (schema_.get("properties") or {}).items():
        yield f"{path}{name}", name, ps
        if ps.get("type") == "array" and isinstance(ps.get("items"), dict) and ps["items"].get("type") == "object":
            yield from walk_props(ps["items"], f"{path}{name}[].")


class TestCatalogShape(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(len(SPECS), 38)
        self.assertEqual(sum(1 for s in SPECS.values() if s.scope == "node"), 37)
        self.assertEqual([s.name for s in SPECS.values() if s.scope == "hub"], ["hub.nodes"])
        self.assertEqual(set(SPECS), set(TABLE))
        self.assertEqual(list(SPECS), list(TABLE))  # catalog order = §9.1 order

    def test_table(self):
        for name, (risk, extra, flags, dt, dp) in TABLE.items():
            s = SPECS[name]
            with self.subTest(action=name):
                self.assertEqual(s.risk, risk)
                self.assertEqual(s.extra_classes, extra)
                self.assertEqual(s.destructive, "D" in flags)
                self.assertEqual(s.idempotent, "I" in flags)
                self.assertEqual(s.long_running, "L" in flags)
                self.assertEqual(s.open_world, "O" in flags)
                self.assertEqual(s.default_timeout, dt)
                self.assertEqual(s.deadline_params, dp)

    def test_every_spec(self):
        for name, s in SPECS.items():
            with self.subTest(action=name):
                self.assertEqual(s.name, name)
                self.assertRegex(name, r"^[a-z]+\.[a-z_]+$")
                self.assertTrue(s.title)
                self.assertTrue(0 < len(s.description) <= 1000)
                self.assertIn(s.risk, RISKS)
                for c in s.extra_classes:
                    self.assertIn(c, RISKS)
                    self.assertNotEqual(c, s.risk)
                if s.destructive:
                    self.assertTrue(s.mutating)
                self.assertIn(s.scope, ("node", "hub"))
                self.assertEqual(s.classes_required, frozenset((s.risk,) + s.extra_classes))
                self.assertEqual(s.mutating, s.risk != "read")
                self.assertEqual(s.params.get("type"), "object")
                self.assertIs(s.params.get("additionalProperties"), False)
                for p in s.deadline_params:
                    self.assertIn(p, s.params["properties"])
                for path, pname, ps in walk_props(s.params):
                    self.assertTrue(ps.get("description"), path)
                    if pname in ("offset", "length", "size", "total_size"):
                        self.assertGreaterEqual(ps.get("minimum", -1), 0, path)

    def test_extra_classes(self):
        self.assertEqual(SPECS["net.download"].classes_required, frozenset({"net", "write"}))
        self.assertEqual(SPECS["model.pull"].classes_required, frozenset({"net", "write"}))
        self.assertEqual(SPECS["service.define"].classes_required, frozenset({"persist", "exec"}))

    def test_id_patterns(self):
        for action, params in ID_PARAMS.items():
            for p, pat in params.items():
                with self.subTest(action=action, param=p):
                    self.assertEqual(SPECS[action].params["properties"][p]["pattern"], pat)
        jp = re.compile(catalog.JOB_ID_PATTERN)
        self.assertTrue(jp.search("j_18f3a2c1d4e5"))
        self.assertFalse(jp.search("j_../../x"))
        sp = re.compile(catalog.SERVICE_NAME_PATTERN)
        for ok in ("qwen", "vllm-7b", "a.b_c", "x" * 64):
            self.assertTrue(sp.search(ok), ok)
        for bad in ("Qwen", "-a", "a..b", "..", "x" * 65, "a/b"):
            self.assertFalse(sp.search(bad), bad)

    def test_specific_params(self):
        p = lambda a: SPECS[a].params["properties"]  # noqa: E731
        self.assertNotIn("wsl", p("shell.run")["shell"]["enum"])
        self.assertNotIn("default", p("shell.run")["timeout"])
        self.assertEqual(SPECS["shell.run"].params["x-one-of-required"], ["command", "argv"])
        self.assertEqual(SPECS["service.define"].params["x-one-of-required"], ["command", "argv", "preset"])
        self.assertEqual(SPECS["model.chat"].params["x-any-of-required"], ["messages", "prompt"])
        self.assertIn("wait", p("service.status"))
        self.assertIn("wait", p("service.remove"))
        self.assertEqual(p("model.forward")["path"]["enum"], ["models", "chat/completions", "completions",
                                                              "embeddings"])
        self.assertEqual(p("model.forward")["body"]["maxLength"], 8388608)
        self.assertEqual(p("model.pull")["auth_env"]["default"], "auto")
        self.assertEqual(p("service.define")["health_expect"]["default"], [200, 399])
        self.assertEqual(p("net.http")["headers"]["x-kv"], ":")
        self.assertEqual(p("shell.run")["env"]["x-kv"], "=")
        self.assertEqual(p("shell.run")["secret_env"]["maxItems"], 20)
        for name in SPECS:
            self.assertNotIn("fanout", p(name))

    def test_lint_every_param_schema(self):
        for name, s in SPECS.items():
            with self.subTest(action=name):
                self.assertEqual(schema.lint_schema(s.params), [])


class TestResults(unittest.TestCase):
    def test_closed_with_masked(self):
        for name, s in SPECS.items():
            with self.subTest(action=name):
                self.assertEqual(s.result.get("type"), "object")
                self.assertIs(s.result.get("additionalProperties"), False)
                self.assertEqual(s.result["properties"]["masked"]["type"], "boolean")
                self.assertNotIn("masked", s.result.get("required", []))
                for r in s.result.get("required", []):
                    self.assertIn(r, s.result["properties"])

    def test_masked_shell_run_validates(self):
        res = {"state": "exited", "exit_code": 0, "signal": None, "duration_s": 0.2, "shell": "bash", "cwd": "/root",
               "pid": 4242, "output": "token=«secret:HF_TOKEN»\n", "output_bytes": 30, "truncated": False,
               "job_id": None, "masked": True}
        schema.validate(SPECS["shell.run"].result, res, coerce=False, defaults=False, path="result")
        with self.assertRaises(HiveError):
            schema.validate(SPECS["shell.run"].result, dict(res, bogus=1), coerce=False, path="result")

    def test_masked_fs_read_validates(self):
        res = {"path": "/w/.env", "content": "HF_TOKEN=«secret:HF_TOKEN»\n", "start_line": 1, "end_line": 1,
               "total_lines": 1, "next_start_line": None, "size": 40, "version": "ab" * 32, "encoding": "utf-8",
               "long_lines_cut": 0, "masked": True}
        schema.validate(SPECS["fs.read"].result, res, coerce=False, defaults=False, path="result")

    def test_open_maps_only_where_keys_are_data(self):
        # §22.3 rule 11 "every schema additionalProperties:false": the only exceptions are result maps whose KEYS are
        # data (PHASE0_NOTES); fixed-shape sections (system.info os/cpu/memory/disks/gpus/network) are closed
        def walk(s, path, out):
            if not isinstance(s, dict):
                return
            if s.get("additionalProperties", False) is not False:
                out.append(path)
            for k, v in (s.get("properties") or {}).items():
                walk(v, f"{path}.{k}", out)
            if isinstance(s.get("items"), dict):
                walk(s["items"], path + "[]", out)
        found = []
        for name, s in SPECS.items():
            walk(s.params, name + "(params)", found)
            walk(s.result, name, found)
        allowed = {"hub.nodes.nodes[].status", "net.http.headers", "system.info.software", "system.info.which.found"}
        allowed |= {f"service.{a}.definition" for a in ("define", "start", "stop", "restart", "status")}
        self.assertEqual(set(found), allowed)

    def test_system_info_sections_closed(self):
        res = {
            "os": {"system": "Linux", "release": "6.8", "version": "#1", "machine": "x86_64", "hostname": "pod",
                   "distro": "Ubuntu 22.04", "python": "3.11.9", "boot_time": "2026-11-17T08:00:00.000Z",
                   "uptime_s": 4000, "timezone": "UTC", "cloud": {"provider": "runpod", "pod_id": "abc"},
                   "wsl": None},
            "cpu": {"model": "EPYC", "logical": 32, "physical": 16, "load_avg": [1, 2, 3], "percent": None},
            "memory": {"total": 1 << 36, "available": 1 << 35, "used_pct": 50.0, "swap_total": None,
                       "swap_used": None, "container_limit": None},
            "disks": [{"mount": "/", "fs": "overlay", "total": 100, "free": 50, "used_pct": 50.0}],
            "gpus": [{"index": 0, "vendor": "nvidia", "name": "RTX 4090", "uuid": None, "memory_total_mb": 24564,
                      "memory_used_mb": 100, "utilization_pct": 0, "temperature_c": None, "power_w": None,
                      "driver": "550.1", "cuda": "12.4", "unified_memory": False,
                      "processes": [{"pid": 1, "name": "python", "memory_mb": 90}]}],
            "gpu_backend": "nvidia", "gpu_note": None,
            "network": {"primary_ip": "10.0.0.2", "addresses": ["10.0.0.2"], "tailscale_ip": None},
        }
        r = SPECS["system.info"].result
        schema.validate(r, res, coerce=False, defaults=False, path="result")
        for section, bad in (("os", dict(res["os"], hostnmae="x")), ("cpu", dict(res["cpu"], cores=1)),
                             ("memory", {k: v for k, v in res["memory"].items() if k != "total"}),
                             ("network", dict(res["network"], ip="x"))):
            with self.subTest(section=section), self.assertRaises(HiveError):
                schema.validate(r, dict(res, **{section: bad}), coerce=False, defaults=False, path="result")
        with self.assertRaises(HiveError):
            schema.validate(r, dict(res, gpus=[dict(res["gpus"][0], vram=1)]), coerce=False, defaults=False,
                            path="result")

    def test_result_examples(self):
        examples = {
            "job.start": {"job_id": "j_18f3a2c1d4e5", "name": "train", "pid": 1, "state": "running",
                          "started_at": "2026-11-17T09:12:03.120Z", "log_path": "/x/output.log",
                          "warnings": ["keep_awake is not supported on this OS"]},
            "service.remove": {"removed": "qwen", "pending": True, "state": "stopping"},
            "proc.kill": {"killed": [1, 2], "still_alive": [], "method": "killpg"},
            "hub.nodes": {"nodes": [{"name": "desk", "online": True, "mode": "direct", "os": "Windows",
                                     "os_label": "Windows 11", "gpus": [{"name": "RTX", "memory_total_mb": 24564}],
                                     "max_timeout": None, "status": {"cpu_pct": 3}}]},
            "net.ports": {"listeners": [{"proto": "tcp", "address": "127.0.0.1", "port": 8000, "pid": None,
                                         "process": None}], "method": "proc"},
        }
        for action, res in examples.items():
            with self.subTest(action=action):
                schema.validate(SPECS[action].result, res, coerce=False, defaults=False, path="result")


class TestLookup(unittest.TestCase):
    def test_get(self):
        self.assertIs(catalog.get("shell.run"), SPECS["shell.run"])

    def test_did_you_mean(self):
        with self.assertRaises(HiveError) as cm:
            catalog.get("shell.runn")
        e = cm.exception
        self.assertEqual(e.code, "not_found")
        self.assertIn("shell.run", e.details["suggestions"])
        self.assertIn("shell.run", e.hint)

    def test_removed_names(self):
        removed = ("system.which", "system.env", "job.remove", "service.from_preset", "service.presets",
                   "model.list", "model.embed", "hub.transfer", "hub.transfer_status")
        self.assertEqual(set(catalog.REMOVED), set(removed))
        for n in removed:
            with self.subTest(name=n), self.assertRaises(HiveError) as cm:
                catalog.get(n)
            self.assertEqual(cm.exception.code, "not_found")
            self.assertEqual(cm.exception.hint, catalog.REMOVED[n])
        self.assertIn("system.info", catalog.REMOVED["system.which"])
        self.assertIn("model.endpoints", catalog.REMOVED["model.list"])

    def test_public_dict_and_hash(self):
        d = catalog.public_dict(SPECS["service.define"])
        self.assertEqual(list(d), ["name", "title", "description", "params", "result", "risk", "classes_required",
                                   "mutating", "destructive", "idempotent", "open_world", "long_running",
                                   "default_timeout", "scope", "since"])
        self.assertEqual(d["classes_required"], ["exec", "persist"])
        self.assertTrue(d["mutating"])
        d["params"]["properties"].clear()
        self.assertTrue(SPECS["service.define"].params["properties"])  # deep copy
        h = catalog.catalog_hash()
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(h, catalog.catalog_hash())

    def test_spec_hashable_by_identity(self):
        self.assertEqual(len({SPECS["fs.read"], SPECS["fs.read"]}), 1)


class TestDeadline(unittest.TestCase):
    def test_v5_cases(self):
        d = catalog.deadline
        self.assertEqual(d(SPECS["model.chat"], {}), 135)
        self.assertEqual(d(SPECS["model.chat"], None), 135)
        self.assertEqual(d(SPECS["shell.run"], {}), 75)
        self.assertEqual(d(SPECS["shell.run"], {"timeout": 100}), 115)
        self.assertEqual(d(SPECS["shell.run"], {"timeout": 100}, 42), 42)
        self.assertEqual(d(SPECS["shell.run"], {"timeout": 86400}), 3700)
        self.assertEqual(d(SPECS["shell.run"], {"timeout": 86400}, max_seconds=600), 600)
        self.assertEqual(d(SPECS["shell.run"], {}, 0.2), 1)
        self.assertEqual(d(SPECS["job.status"], {"wait": "abc"}), 30)
        self.assertEqual(d(SPECS["job.status"], {"wait": True}), 30)
        self.assertEqual(d(SPECS["job.status"], {"wait": 60}), 90)
        self.assertEqual(d(SPECS["job.status"], {"wait": "60"}), 90)
        self.assertEqual(d(SPECS["service.stop"], {}), 50)        # wait default 20
        self.assertEqual(d(SPECS["service.remove"], {}), 60)      # wait default 30
        self.assertEqual(d(SPECS["proc.kill"], {}), 35)           # grace default 5
        self.assertEqual(d(SPECS["model.forward"], {}), 315)      # timeout default 300
        self.assertEqual(d(SPECS["net.http"], {"timeout": "x"}), 45)
        self.assertEqual(d(SPECS["fs.copy"], {}), 600)
        self.assertEqual(d(SPECS["hub.nodes"], {}), 10)
        self.assertEqual(d(SPECS["shell.run"], {}, "abc"), 75)


class TestHandlersGate(unittest.TestCase):
    def test_every_node_spec_has_handler(self):
        load_all()
        missing = [n for n, s in SPECS.items() if s.scope == "node" and n not in HANDLERS]
        if missing and len(missing) == 37:
            self.skipTest("integration gate: no WP handlers merged yet (Phase 0)")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
