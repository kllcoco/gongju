"""WP4: doctor.run (design §12.4; §21.2 row "install/doctor")."""
from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hive import config, doctor, proctree, sysinfo, util, wsl
from hive.audit import AuditLog
from tests import helpers

POSIX = os.name != "nt"
LINUX = sys.platform.startswith("linux")
FIX = Path(__file__).resolve().parent / "fixtures" / "wp4"
PROXY_ENV = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy", "NO_PROXY",
             "no_proxy", "PIP_INDEX_URL", "HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HF_ENDPOINT")


def make_tool_dir(name: str, script: str) -> Path:
    d = Path(tempfile.mkdtemp(prefix="hivedoc-tool-")).resolve()
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        (d / f"{name}.py").write_text(script, encoding="utf-8")
        (d / f"{name}.cmd").write_text(f'@"{sys.executable}" "%~dp0{name}.py" %*\r\n', encoding="utf-8")
    else:
        p = d / name
        p.write_text(f"#!{sys.executable}\n{script}", encoding="utf-8")
        p.chmod(0o755)
    return d


class DoctorCase(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="hivedoc-")).resolve()
        self.addCleanup(shutil.rmtree, str(self.base), True)
        self.home = self.base / "home"
        self.home.mkdir()
        env = {k: v for k, v in os.environ.items() if k not in PROXY_ENV}
        env.update(HIVE_HOME=str(self.base / "hh"), HOME=str(self.home), USERPROFILE=str(self.home))
        p = mock.patch.dict(os.environ, env, clear=True)
        p.start()
        self.addCleanup(p.stop)
        old_tp = proctree._trusted_path
        self.addCleanup(lambda: setattr(proctree, "_trusted_path", old_tp))
        self.addCleanup(sysinfo.clear_cache)
        self.cfg, self.tokens = config.init_node(consent_by="tester", proxy="off")
        self.cfg_path = config.default_paths()["node_config"]
        self.data = Path(self.cfg.data_dir)
        if config.is_privileged():
            self.edit(lambda r: r.update(allow_root=True))
        nowsl = mock.patch.object(wsl, "is_wsl", return_value=False)
        nowsl.start()
        self.addCleanup(nowsl.stop)

    def edit(self, fn):
        config.edit_config(self.cfg_path, fn, kind="node")

    def run_doc(self, kind="node", network=False, **kw):
        return doctor.run(kind, network=network, **kw)

    def rows(self, res, check=None, status=None):
        return [r for r in res if (check is None or r["check"] == check) and (status is None or r["status"] == status)]

    def assertRow(self, res, check, status, text):
        hits = [r for r in self.rows(res, check, status) if text in r["detail"] or text in (r["fix"] or "")]
        self.assertTrue(hits, f"no {check}/{status} row with {text!r}: {json.dumps(res, indent=1)}")
        return hits[0]


class TestConfigAndFiles(DoctorCase):
    def test_clean_node(self):
        res = self.run_doc()
        self.assertFalse(self.rows(res, status="fail"), res)
        self.assertTrue(all(set(r) == {"check", "status", "detail", "fix"} for r in res))
        self.assertTrue(all(r["status"] in ("ok", "warn", "fail") for r in res))

    def test_plaintext_bind(self):
        self.edit(lambda r: r["listen"].update(host="0.0.0.0"))
        self.assertRow(self.run_doc(), "config", "fail", "refusing plaintext HTTP on 0.0.0.0")
        self.edit(lambda r: r["listen"].update(allow_insecure_remote=True))
        self.assertRow(self.run_doc(), "config", "warn", "PLAINTEXT HTTP on 0.0.0.0")

    def test_missing_consent(self):
        self.edit(lambda r: r.pop("consent", None))
        self.assertRow(self.run_doc(), "config", "fail", "no owner consent")

    @unittest.skipUnless(POSIX, "POSIX modes")
    def test_loose_permissions(self):
        sec = self.data / "secrets.json"
        sec.write_text("{}")
        os.chmod(sec, 0o644)
        key = self.base / "tls.key"
        key.write_text("-----BEGIN PRIVATE KEY-----\n")
        os.chmod(key, 0o640)
        cert = self.base / "tls.crt"
        cert.write_text("x")
        self.edit(lambda r: r["listen"].setdefault("tls", {}).update(cert=str(cert), key=str(key)))
        os.chmod(self.cfg_path, 0o600)
        res = self.run_doc()
        row = self.assertRow(res, "files", "warn", str(sec))
        self.assertIn("mode 0644", row["detail"])
        self.assertEqual(row["fix"], f"chmod 600 '{sec}'")
        self.assertRow(res, "files", "warn", str(key))

    def test_icacls_parser(self):
        text = (r"C:\Users\alice\.hive\node\secrets.json DESK\alice:(F)" "\n"
                r"                                       BUILTIN\Users:(RX)" "\n"
                r"                                       NT AUTHORITY\Authenticated Users:(I)(M)" "\n"
                r"                                       Everyone:(R)" "\n"
                r"                                       NT AUTHORITY\SYSTEM:(I)(F)" "\n"
                r"                                       VORDEFINIERT\Benutzer:(RX)" "\n"
                "Successfully processed 1 files; Failed processing 0 files\n")
        self.assertEqual(doctor.parse_icacls(text), ["BUILTIN\\Users", "NT AUTHORITY\\Authenticated Users",
                                                     "Everyone", "VORDEFINIERT\\Benutzer"])
        self.assertEqual(doctor.parse_icacls(r"C:\x DESK\alice:(F)" "\n" r"   NT AUTHORITY\SYSTEM:(F)"), [])


class TestListenAndProxy(DoctorCase):
    def test_forwarded_requests(self):
        (self.data / "logs").mkdir(exist_ok=True)
        (self.data / "logs" / "node.log").write_text(
            "2026-11-18 WARNING hive.httpserver forwarded request on the main port from 127.0.0.1; point the proxy "
            "at listen.proxy_port\n")
        AuditLog(self.data / "audit").record(kind="call", identity=None, ip="proxy:127.0.0.1", via="direct",
                                             node="n", action="node.info", params=None, ok=True, code=None, ms=1,
                                             request_id="r_1")
        res = self.run_doc()
        self.assertRow(res, "listen", "warn", "point the proxy at listen.proxy_port")
        # main-port forwarding records `proxy:<peer>` with or without X-Forwarded-For: no XFF warning then
        # (WP4 review; the XFF case is tests/test_wp4_review.py)
        self.assertFalse([r for r in res if "without X-Forwarded-For" in r["detail"]], res)

    def test_firewall_parser(self):
        text = ("\r\nRule Name:                            Hive Node (tailnet)\r\n"
                "----------------------------------------------------------------------\r\n"
                "Enabled:                              Yes\r\nDirection:                            In\r\n"
                "LocalPort:                            7701\r\nAction:                               Allow\r\n\r\n"
                "Rule Name:                            Other\r\nLocalPort:                            8080\r\n"
                "Action:                               Block\r\n")
        self.assertTrue(doctor.firewall_rule_present(text, 7701))
        self.assertFalse(doctor.firewall_rule_present(text, 8080))
        self.assertFalse(doctor.firewall_rule_present(text, 7702))


class TestNetwork(DoctorCase):
    def setUp(self):
        super().setUp()
        self.stub = helpers.StubHTTPServer().start()
        self.addCleanup(self.stub.stop)
        self.stub.add("GET", "/hf/api/models", 503, b"down")
        self.stub.add("GET", "/mirror/api/models", 200, b"[]")
        self.stub.add("GET", "/ms/api/v1/models", 200, b"{}")
        self.stub.add("GET", "/pypi/simple/pip/", 200, b"<html/>")
        ep = {"huggingface": self.stub.url + "/hf/api/models?limit=1",
              "hf_mirror": self.stub.url + "/mirror/api/models?limit=1",
              "modelscope": self.stub.url + "/ms/api/v1/models?PageSize=1",
              "pypi": self.stub.url + "/pypi/simple/pip/"}
        p = mock.patch.dict(doctor.ENDPOINTS, ep)
        p.start()
        self.addCleanup(p.stop)

    def test_verdict_against_stub_endpoints(self):
        res = self.run_doc(network=True)
        self.assertRow(res, "network", "ok", "no proxy in effect")
        self.assertRow(res, "network", "warn", "huggingface.co: unreachable (HTTP 503)")
        self.assertRow(res, "network", "ok", "modelscope: reachable")
        self.assertRow(res, "network", "ok", "pip index: reachable")
        row = self.assertRow(res, "network", "warn", "huggingface.co unreachable, hf-mirror.com OK")
        self.assertIn("--mirror cn", row["fix"])

    def test_socks_only_proxy(self):
        self.edit(lambda r: r["network"].update(proxy=None))
        with mock.patch.dict(os.environ, {"ALL_PROXY": "socks5://127.0.0.1:7891"}):
            res = self.run_doc(network=True)
        self.assertRow(res, "network", "warn", "only a SOCKS proxy is set (ALL_PROXY)")

    def test_mirror_without_bound_token(self):
        self.edit(lambda r: r.setdefault("models", {}).update(hf_endpoint=self.stub.url + "/mirror"))
        with mock.patch.dict(os.environ, {"HF_TOKEN": "hf_" + "x" * 30}):
            res = self.run_doc(network=True)
        row = self.assertRow(res, "network", "warn", "HF_TOKEN is not bound to it")
        self.assertIn("--hosts +127.0.0.1", row["fix"])
        self.assertNotIn("x" * 30, json.dumps(res))                       # never a value
        self.assertRow(res, "network", "ok", "127.0.0.1: reachable")

    def test_fakeip_ts_net(self):
        self.edit(lambda r: r.setdefault("relay", {}).update(hub_url="https://hub.tail1234.ts.net"))
        with mock.patch.object(doctor, "_resolve", return_value=["198.18.0.7"]):
            res = self.run_doc(network=False)
        row = self.assertRow(res, "network", "warn", "hub.tail1234.ts.net resolves to 198.18.0.7")
        self.assertIn("fake-ip-filter", row["fix"])
        with mock.patch.object(doctor, "_resolve", return_value=["100.101.1.7"]):
            self.assertFalse(self.rows(self.run_doc(), "network", "warn"))


class TestPolicyChecks(DoctorCase):
    def add_hub(self, nodes=(), tokens=()):
        hcfg, _ = config.init_hub(consent_by="tester")
        path = config.default_paths()["hub_config"]

        def fn(r):
            r["nodes"] = list(nodes)
            r["tokens"] = r.get("tokens", []) + list(tokens)
        config.edit_config(path, fn, kind="hub")

    def test_exec_agent_on_hub_machine(self):
        res = self.run_doc()
        self.assertFalse([r for r in self.rows(res, "policy", "warn") if "T5" in r["detail"]])
        self.add_hub()
        res = self.run_doc()
        row = self.assertRow(res, "policy", "warn", "on the hub's machine without approvals")
        self.assertRegex(row["fix"], r"^hive node token set \S+ --cautious$")      # final acceptance B3: exact

    def test_hub_agent_reaching_local_nodes(self):
        tok = {"name": "claude", "sha256": util.hash_token("hv_" + "a" * 40), "kind": "agent", "role": "operator",
               "nodes": ["*"]}
        host = socket.gethostname().lower()
        self.add_hub(nodes=[{"name": "desk", "mode": "direct", "url": "http://127.0.0.1:7701",
                             "token": "hv_" + "b" * 40},
                            {"name": "desk-wsl", "mode": "relay", "token_sha256": util.hash_token("hv_" + "c" * 40),
                             "tags": ["wsl", f"wsl-host:{host}"]},
                            {"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token("hv_" + "d" * 40)}],
                     tokens=[tok])
        row = self.assertRow(self.run_doc(kind="hub"), "policy", "warn", "hub agent token 'claude' reaches")
        self.assertIn("desk", row["detail"])
        self.assertIn("desk-wsl", row["detail"])                          # wsl-host:<hub hostname> counts
        self.assertNotIn("pod-a", row["detail"])
        self.assertEqual(row["fix"], "hive hub set-agent claude --cautious, or narrow its nodes")

    def test_cautious_node_with_open_agent(self):
        self.edit(lambda r: r["policy"].update(preset="cautious"))
        row = self.assertRow(self.run_doc(), "policy", "warn", "on this cautious node without approvals")
        self.assertRegex(row["fix"], r"^hive node token set \S+ --cautious$")


@unittest.skipUnless(POSIX, "fake tools are POSIX scripts")
class TestDockerAndWsl(DoctorCase):
    def tool(self, name, script):
        d = make_tool_dir(name, script)
        self.addCleanup(shutil.rmtree, str(d), True)
        p = mock.patch.dict(os.environ, {"PATH": str(d) + os.pathsep + os.environ.get("PATH", "")})
        p.start()
        self.addCleanup(p.stop)
        return d

    def test_docker_info_and_gpu_runtime(self):
        self.tool("docker", "import sys\na=' '.join(sys.argv[1:])\nprint('{\"runc\":{}}' if 'Runtimes' in a "
                            "else '27.1.1')")
        self.tool("nvidia-smi", "print('NVIDIA-SMI version : 560.94')")
        sysinfo.clear_cache()
        with mock.patch("os.path.exists", side_effect=lambda p: False if p == "/etc/cdi/nvidia.yaml"
                        else os.path.lexists(p)):
            res = self.run_doc()
        self.assertRow(res, "docker", "ok", "docker daemon reachable")
        self.assertRow(res, "docker", "ok", "INFO: user")
        self.assertRow(res, "docker", "ok", "controls Docker (root-equivalent)")
        if LINUX:
            self.assertRow(res, "docker", "warn", "nvidia-ctk runtime configure")

    def test_docker_daemon_down(self):
        self.tool("docker", "import sys\nsys.exit(1)")
        sysinfo.clear_cache()
        self.assertRow(self.run_doc(), "docker", "warn", "daemon is not reachable")

    def wsl_node(self, interop: bool):
        self.edit(lambda r: r.update(tags=["wsl", "wsl-host:desk"]))
        patches = [mock.patch.object(wsl, "is_wsl", return_value=True),
                   mock.patch.object(wsl, "wsl_info", return_value={"distro": "Ubuntu-24.04", "networking": "nat",
                                                                    "systemd": True}),
                   mock.patch.object(wsl, "interop_enabled", return_value=interop)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_wsl_host_power_plan(self):
        self.wsl_node(interop=True)
        self.tool("powercfg.exe", "import sys\nsys.stdout.write(open(%r).read())" % str(FIX / "powercfg_standby.txt"))
        res = self.run_doc()
        row = self.assertRow(res, "wsl", "warn", "Windows sleeps after 30 min")
        self.assertIn("never sleep", row["fix"])
        self.assertRow(res, "wsl", "warn", "Windows interop is on")

    def test_wsl_host_interop_off_info_only(self):
        self.wsl_node(interop=False)
        res = self.run_doc()
        self.assertRow(res, "wsl", "ok", "INFO: Windows power plan not checked")
        self.assertFalse([r for r in self.rows(res, "wsl", "warn") if "sleeps" in r["detail"]])

    def test_wsl_nat_fixed_proxy_and_models_on_mnt(self):
        self.wsl_node(interop=False)
        self.edit(lambda r: (r["network"].update(proxy="http://172.20.48.1:7890"),
                             r.setdefault("models", {}).update(dir="/mnt/d/models")))
        res = self.run_doc()
        self.assertRow(res, "wsl", "warn", "http://wsl-host:7890")
        self.assertRow(res, "wsl", "warn", "models.dir /mnt/d/models")

    def test_parse_powercfg(self):
        self.assertEqual(doctor.parse_powercfg((FIX / "powercfg_standby.txt").read_text()), (1800, 900))
        self.assertEqual(doctor.parse_powercfg("nothing"), (None, None))


class TestRelayAndClient(DoctorCase):
    def test_relay_narrowed_auto_update(self):
        self.edit(lambda r: r.setdefault("relay", {}).update(enabled=True, hub_url="https://hub.example.com", token="hv_" + "r" * 40,
                                              role="files", auto_update=True))
        res = self.run_doc()
        self.assertRow(res, "relay", "ok", "role files, auto_update on")
        self.assertRow(res, "relay", "warn", "voids the narrowed relay role")

    def test_client_and_hub_targets(self):
        res = self.run_doc(kind="client")
        self.assertRow(res, "client", "ok", "connection(s)")
        res = self.run_doc(kind="hub")
        self.assertRow(res, "config", "fail", "no hub config")
        with self.assertRaises(Exception):
            doctor.run("bogus")


if __name__ == "__main__":
    unittest.main()
