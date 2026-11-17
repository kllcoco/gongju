"""Phase 0 cross-WP stubs and real helpers (§22.1): their pinned values, plus the auth P0 truth tables."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from hive import (auth, dist, fsops, gateway, install, netproxy, netutil, presets, procinfo, proctree, services,
                  tools, winapi)
from hive.auth import CAUTIOUS_ACTIONS, ROLE_PRESETS, Identity
from hive.errors import HiveError
from hive.policy import Limits
from hive.secretstore import DEFAULT_SECRET_HOSTS, Secret, SecretStore
from tests import helpers


class TestAuthReal(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(ROLE_PRESETS["files"], {"read", "write"})
        self.assertEqual(len(CAUTIOUS_ACTIONS), 9)
        self.assertNotIn("job.stop", CAUTIOUS_ACTIONS)
        self.assertNotIn("fs.write", CAUTIOUS_ACTIONS)

    def test_identity(self):
        i = Identity(name="claude", kind="agent", classes=frozenset({"read"}), nodes=("desk*",), agent="cd",
                     deny_actions=("fs.*",))
        self.assertTrue(i.read_only)
        self.assertFalse(i.approver)
        self.assertTrue(i.allows_node("DESK-wsl"))
        self.assertFalse(i.allows_node("pod-a"))
        self.assertFalse(i.allows_action("fs.read"))
        self.assertTrue(i.allows_action("shell.run"))
        self.assertEqual(i.display_name, "claude/cd")
        self.assertTrue(Identity(name="o", kind="owner", classes=frozenset()).approver)
        with self.assertRaises(Exception):
            i.name = "x"  # frozen

    def test_exec_gated_truth_table(self):
        all5 = frozenset(ROLE_PRESETS["operator"])
        rows = [
            # classes, deny_actions, approval_set, expected
            (all5, (), (), False),
            (all5, (), ("shell.run",), True),
            (all5, (), ("shell.*",), True),
            (all5, (), ("job.start",), False),
            (all5, ("shell.run",), (), True),
            (all5, ("*",), (), True),
            (all5, ("fs.*",), (), False),
            (frozenset({"read", "write"}), (), (), True),
            (frozenset({"read", "net"}), (), (), True),
            (all5 - {"exec"}, (), (), True),
            (all5, (), CAUTIOUS_ACTIONS, True),
        ]
        for classes, deny, approval, expected in rows:
            ident = Identity(name="a", kind="agent", classes=classes, deny_actions=deny)
            with self.subTest(classes=sorted(classes), deny=deny, approval=approval):
                self.assertIs(auth.exec_gated(ident, approval), expected)

    def test_wp1_real(self):
        # WP1 merged: the former stubs are real (detailed tests in test_auth.py)
        self.assertEqual(auth.parse_bearer("Bearer hv_x"), "hv_x")
        with self.assertRaises(HiveError) as cm:
            auth.TokenStore([], kind_default="agent").authenticate("hv_xxxxxxxxxx", "")
        self.assertEqual(cm.exception.code, "unauthorized")


class TestPolicyAndSecrets(unittest.TestCase):
    def test_limits_defaults(self):
        lim = Limits()
        self.assertEqual((lim.max_read_bytes, lim.max_write_bytes, lim.max_chunk_bytes), (8388608, 16777216, 8388608))
        self.assertEqual((lim.exec_default_timeout, lim.exec_max_timeout), (60, 3600))
        self.assertEqual((lim.max_concurrent_calls, lim.max_call_seconds, lim.max_result_bytes),
                         (32, 3700, 33554432))
        self.assertEqual((lim.gateway_max_body_bytes, lim.gateway_max_response_bytes), (8388608, 67108864))
        self.assertEqual(lim.max_concurrent_pulls, 2)

    def test_secret_repr_hides_value(self):
        s = Secret(name="HF_TOKEN", value="hf_supersecretvalue", hosts=DEFAULT_SECRET_HOSTS["HF_TOKEN"],
                   exec_ok=True, set_at="t", source="store")
        self.assertNotIn("supersecret", repr(s))
        a = netutil.AuthSpec(name="HF_TOKEN", header="Authorization", value="Bearer hf_supersecretvalue",
                             hosts=("huggingface.co",))
        self.assertNotIn("supersecret", repr(a))

    def test_secretstore_stub_is_empty(self):
        st = SecretStore(Path(tempfile.gettempdir()) / "hive-p0-none" / "secrets.json", passthrough=["HF_TOKEN"],
                         env={})
        self.assertEqual(st.names(), [])
        self.assertFalse(st.has_values)
        self.assertEqual(st.mask("abc"), "abc")
        self.assertEqual(st.mask_edges(b"x", b"", b""), (b"x", False))
        for fn in (lambda: st.resolve("HF_TOKEN", purpose="exec"), lambda: st.resolve_auth("HF_TOKEN")):
            with self.assertRaises(HiveError) as cm:
                fn()
            self.assertEqual(cm.exception.code, "not_found")
            self.assertEqual(cm.exception.details["secret"], "HF_TOKEN")


class TestNetStubs(unittest.TestCase):
    def test_auth_host_allowed(self):
        hf = DEFAULT_SECRET_HOSTS["HF_TOKEN"]
        f = netutil.auth_host_allowed
        self.assertTrue(f(hf, "huggingface.co", "https"))
        self.assertTrue(f(hf, "cdn-lfs.huggingface.co", "https"))
        self.assertTrue(f(hf, "HF.CO.", "https"))
        self.assertFalse(f(hf, "huggingface.co", "http"))          # globs require https (C1)
        self.assertFalse(f(hf, "hf-mirror.com", "https"))
        self.assertFalse(f(hf, "evilhuggingface.co", "https"))
        self.assertTrue(f(("loopback",), "127.0.0.1", "http"))
        self.assertTrue(f(("loopback",), "localhost", "http"))
        self.assertTrue(f(("loopback",), "[::1]", "http"))
        self.assertFalse(f(("loopback",), "10.0.0.1", "http"))
        self.assertTrue(f(("private",), "10.0.0.1", "http"))
        self.assertTrue(f(("private",), "100.101.1.7", "http"))
        self.assertFalse(f(("private",), "8.8.8.8", "https"))
        self.assertFalse(f(("private",), "nas.lan", "http"))
        self.assertTrue(f(("*",), "anything.example", "https"))
        self.assertFalse(f((), "huggingface.co", "https"))

    def test_vetted_target(self):
        t = netutil.VettedTarget("https", "h", 443, "/", "1.2.3.4", "direct", None, "public")
        self.assertFalse(t.pin_ip)

    def test_netguard_stub(self):
        g = netutil.NetGuard(http_allow=["loopback"], http_deny=[], download_allow=[], allow_metadata_ip=False,
                             proxies=netproxy.load_proxy_settings(None))
        with self.assertRaises(HiveError) as cm:
            g.check_url("http://127.0.0.1/", purpose="http")
        self.assertEqual(cm.exception.code, "unsupported")
        for fn in (lambda: netutil.guarded_request("GET", "http://x/", guard=g, purpose="http"),
                   lambda: netutil.open_download("http://x/", guard=g), lambda: netutil.tcp_check("127.0.0.1", 1)):
            with self.assertRaises(HiveError):
                fn()

    def test_netproxy_no_proxy_stub(self):
        s = netproxy.load_proxy_settings({"proxy": "http://127.0.0.1:7890"}, {"HTTPS_PROXY": "http://x:1"})
        self.assertEqual(s, netproxy.ProxySettings(None, None, ("localhost", "127.0.0.1", "::1"), "none"))
        self.assertIsNone(netproxy.proxy_for(s, "https", "huggingface.co"))
        self.assertTrue(netproxy.bypass(s, "huggingface.co"))
        self.assertEqual(netproxy.child_proxy_env(s), {})
        self.assertEqual(netproxy.redact_proxy("http://user:pw@127.0.0.1:7890"), "http://***@127.0.0.1:7890")
        self.assertEqual(netproxy.redact_proxy("http://127.0.0.1:7890"), "http://127.0.0.1:7890")
        self.assertEqual(s.describe(), "none")
        p = netproxy.ProxySettings("http://u:p@h:1", "http://u:p@h:1", (), "env")
        self.assertEqual(p.describe(), "http://***@h:1 (env)")
        self.assertEqual(p.to_dict()["https"], "http://u:p@h:1")
        with self.assertRaises(HiveError) as cm:
            netproxy.open_tunnel("http://127.0.0.1:1", "x", 443, timeout=1)
        self.assertEqual(cm.exception.code, "unsupported")

    def test_open_stream_loopback(self):
        with helpers.StubHTTPServer() as srv:
            srv.openai(reply="a b c")
            guard = helpers.FakeNetGuard()
            status, headers, chunks = netutil.open_stream(
                "POST", srv.url + "/v1/chat/completions", guard=guard, purpose="model",
                body=json.dumps({"stream": True}).encode(), headers={"Content-Type": "application/json"})
            body = b"".join(chunks)
            self.assertEqual(status, 200)
            self.assertIn(b"data: [DONE]", body)
            self.assertEqual(guard.calls[0][1:], ("model", "POST", False))
        with self.assertRaises(HiveError) as cm:
            netutil.open_stream("GET", "https://example.com/", guard=None, purpose="model")
        self.assertEqual(cm.exception.code, "unsupported")
        port = helpers.free_port()
        with self.assertRaises(HiveError) as cm:
            netutil.open_stream("GET", f"http://127.0.0.1:{port}/", guard=None, purpose="model")
        self.assertEqual((cm.exception.code, cm.exception.details["reason"],
                          cm.exception.details["maybe_executed"]), ("unavailable", "connect", False))

    def test_open_stream_timeout_after_send(self):
        # §11.2: timeouts become `timeout`; maybe_executed=True once the request was sent
        import socket
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port = lsock.getsockname()[1]
        try:
            with self.assertRaises(HiveError) as cm:
                netutil.open_stream("POST", f"http://127.0.0.1:{port}/v1/chat/completions", guard=None,
                                    purpose="model", body=b"{}", connect_timeout=2, idle_timeout=0.3)
            self.assertEqual((cm.exception.code, cm.exception.details["maybe_executed"]), ("timeout", True))
        finally:
            lsock.close()


class TestWp2Real(unittest.TestCase):
    """WP2 CONTRACT-REQUEST (PHASE0_NOTES): the WP2 stubs are real since the WP2 merge; these assert the neutral
    values that remain off Windows / without a runtime (detailed behaviour is in the WP2 test files)."""

    def test_winapi(self):
        self.assertIn(winapi.spawn_mode(), winapi.SPAWN_MODES)
        if not winapi.IS_WINDOWS:
            self.assertEqual(winapi.spawn_mode(), "free")
            self.assertIsNone(winapi.session_winlogon_ctime())
            with self.assertRaises(HiveError) as cm:
                winapi.relaunch_outside_job(["x"], {})
            self.assertEqual(cm.exception.code, "unsupported")

    def test_proctree(self):
        self.assertIs(proctree.pid_alive, __import__("hive.util", fromlist=["x"]).pid_alive)
        try:
            proctree.set_trusted_path("/opt/x")
            self.assertEqual(proctree.trusted_path(), "/opt/x")
        finally:
            proctree._trusted_path = None
        root = os.path.abspath(os.sep)
        from unittest import mock
        with mock.patch.dict(os.environ, {"PATH": os.pathsep.join([os.path.join(root, "usr", "local", "bin"),
                                                                   os.path.join(root, "opt", "conda", "bin")])}), \
                mock.patch("os.getcwd", return_value=root):
            got = proctree.trusted_path().split(os.pathsep)
        # a filesystem root as cwd (container WORKDIR "/") must not exclude every PATH entry
        self.assertIn(os.path.join(root, "usr", "local", "bin"), got)
        self.assertIn(os.path.join(root, "opt", "conda", "bin"), got)
        self.assertIn(proctree.default_shell(), ("bash", "sh", "pwsh", "powershell"))
        self.assertTrue(any(r["name"] == proctree.default_shell() for r in proctree.available_shells()))
        default = proctree.trusted_path().split(os.pathsep)
        home = str(Path.home())
        self.assertFalse(any(d == home or d.startswith(home + os.sep) for d in default))
        self.assertFalse(any(d.startswith(tempfile.gettempdir()) for d in default))
        with self.assertRaises(HiveError):
            proctree.run_quick(["hive-no-such-program-x"], timeout=1)

    def test_services(self):
        sm = services.ServiceManager(None)
        self.assertEqual(sm.managed_pids(), {})
        self.assertEqual(sm.endpoints(), [])
        with self.assertRaises(HiveError) as cm:
            sm.endpoint_for("qwen")
        self.assertEqual(cm.exception.code, "not_found")
        d = Path(tempfile.mkdtemp(prefix="hivepanic-"))
        try:
            out = services.panic_offline(d, "")
            self.assertEqual(out, {"jobs": [], "services": [], "containers": [], "errors": []})
        finally:
            import shutil
            shutil.rmtree(d, ignore_errors=True)

    def test_jobs(self):
        from hive import jobs
        rt = helpers.FakeRuntime()
        jm = jobs.JobManager(rt)
        jm.start()
        try:
            self.assertEqual(jm.managed_pids(), {})
            with self.assertRaises(HiveError) as cm:
                jm.get("j_abcdef")
            self.assertEqual(cm.exception.code, "not_found")
        finally:
            jm.shutdown()
            rt.cleanup()

    def test_presetctx(self):
        d = Path(tempfile.mkdtemp(prefix="hivepreset-")).resolve()
        (d / "Qwen").mkdir()
        (d / "Qwen" / ".hive-pull.json").write_text(json.dumps({"repo": "Qwen/Qwen2.5-7B-Instruct"}))
        (d / "Bad").mkdir()
        (d / "Bad" / ".hive-pull.json").write_text("[1,2]")
        pol = helpers.FakeEffectivePolicy(workdir=d)
        ctx = presets.PresetCtx(os="linux", node_id="n_x", policy=pol, secret_names=[], docker=False)
        for local in ("/models/x", "~/m", "./m", "C:\\m", "D:/m", "Qwen"):
            self.assertTrue(ctx.is_local_model(local), local)
        for repo in ("Qwen/Qwen2.5-7B-Instruct", "gpt2", ""):
            self.assertFalse(ctx.is_local_model(repo), repo)
        self.assertEqual(ctx.manifest_repo(str(d / "Qwen")), "Qwen/Qwen2.5-7B-Instruct")
        self.assertEqual(ctx.manifest_repo("Qwen"), "Qwen/Qwen2.5-7B-Instruct")
        self.assertIsNone(ctx.manifest_repo(str(d / "Bad")))
        self.assertIsNone(ctx.manifest_repo(str(d / "missing")))
        self.assertEqual(ctx.mask("x"), "x")

    def test_approval_review_shape(self):
        params = {"command": "bash run.sh\necho done", "shell": "bash", "cwd": "/w",
                  "env": ["PATH=/evil:/usr/bin", "API_TOKEN=s3cr3tvalue", "MODE=fast"], "stdin": "line1\n\x1b[2Kx",
                  "secret_env": ["HF_TOKEN"]}
        r = presets.approval_review("shell.run", params, ctx=None)
        self.assertEqual(set(r), {"action", "lines", "changes_what_runs", "files"})
        self.assertEqual(r["action"], "shell.run")
        self.assertEqual(r["files"], [])
        self.assertEqual(r["changes_what_runs"], ["PATH"])
        self.assertEqual(r["lines"][0], "changes what runs: PATH")
        self.assertIn("env PATH=/evil:/usr/bin", r["lines"])
        self.assertIn("env MODE=fast", r["lines"])
        self.assertFalse(any("s3cr3tvalue" in x for x in r["lines"]))
        self.assertIn("stdin:", r["lines"])
        self.assertIn("\\x1b[2Kx", r["lines"])
        self.assertIn("echo done", r["lines"])
        for line in r["lines"]:
            self.assertNotIn("\x1b", line)
        hub = presets.approval_review("service.define", {"name": "qwen", "preset": "vllm"}, ctx=None)
        self.assertEqual(hub["note"], presets.HUB_DEFINE_NOTE)
        self.assertIn("note: " + presets.HUB_DEFINE_NOTE, hub["lines"])
        self.assertEqual(hub["files"], [])
        self.assertLessEqual(set(hub), {"action", "lines", "changes_what_runs", "files", "note"})


class TestWp3Stubs(unittest.TestCase):
    def test_check_regex(self):
        self.assertTrue(fsops.check_regex(r"err(or)?").search("error"))
        for bad in ("x" * 201, "(unclosed"):
            with self.assertRaises(HiveError) as cm:
                fsops.check_regex(bad)
            self.assertEqual(cm.exception.code, "bad_request")
        fsops.check_regex("y" * 300, max_len=500)

    def test_part_tracker_noop(self):
        pt = fsops.PartTracker(Path("parts.json"))
        pt.touch(Path("a.hive-part"))
        pt.done(Path("a.hive-part"))
        self.assertEqual(pt.prune(), 0)


class TestWp4Wp5Wp6Stubs(unittest.TestCase):
    def test_procinfo(self):
        self.assertEqual(procinfo.listeners(), ([], "unavailable"))
        self.assertIsNone(procinfo.process_info(os.getpid()))

    def test_declared_bases(self):
        # §4.6 / §15.2: HubGatewayBackend(GatewayBackend), NodeGatewayBackend(GatewayBackend), HttpTarget(Target),
        # HubTarget(Target), RuntimeTarget(Target)
        from hive import client
        self.assertTrue(issubclass(gateway.HubGatewayBackend, gateway.GatewayBackend))
        self.assertTrue(issubclass(gateway.NodeGatewayBackend, gateway.GatewayBackend))
        for cls in (client.HttpTarget, client.HubTarget, tools.RuntimeTarget):
            self.assertIn(client.Target, cls.__mro__)
            self.assertIsInstance(object.__new__(cls), client.Target)
        self.assertNotIsInstance(object(), client.Target)
        with self.assertRaises(HiveError):
            tools.RuntimeTarget(None, None, "127.0.0.1")

    def test_node_info_probes_neutral(self):
        # node.info and GET /v1/nodes compose these before WP4 merges (§9.2 null fallback)
        from hive import gpu, sysinfo
        self.assertEqual(sysinfo.capabilities(), {})
        self.assertEqual(sysinfo.summary_line(), "")
        self.assertEqual(sysinfo.live_status(), {})
        d = gpu.detect()
        self.assertEqual(set(d), {"backend", "gpus", "processes", "errors", "driver", "cuda"})
        self.assertEqual((d["backend"], d["gpus"], d["processes"]), (None, [], []))
        with self.assertRaises(HiveError):
            sysinfo.os_info()

    def test_install_restart(self):
        with self.assertRaises(HiveError) as cm:
            install.restart("node")
        self.assertEqual((cm.exception.code, cm.exception.message),
                         ("unsupported", "service install not available yet"))

    def test_build_pyz(self):
        with self.assertRaises(HiveError) as cm:
            dist.build_pyz()
        self.assertEqual((cm.exception.code, cm.exception.message), ("unsupported", "pyz build not available yet"))
        self.assertFalse(dist.self_update(None))

    def test_join_codes(self):
        d = {"u": "https://home-hub.tail1.ts.net", "t": "hv_abc123", "p": None, "n": "pod-a"}
        code = dist.encode_join(d)
        self.assertTrue(code.startswith("hvj1."))
        self.assertNotIn("=", code)
        self.assertEqual(dist.decode_join(code), d)
        self.assertEqual(dist.decode_join(dist.encode_join(
            {"hub_url": "http://100.101.1.7:7700", "relay_token": "hv_x", "tls_pin": "sha256:ab", "node_name": "w"})),
            {"u": "http://100.101.1.7:7700", "t": "hv_x", "p": "sha256:ab", "n": "w"})
        for bad in ("hvj2.e30", "hvj1.!!!", "hvj1." + "e30", "nope", 5):
            with self.subTest(code=bad), self.assertRaises(HiveError):
                dist.decode_join(bad)
        with self.assertRaises(HiveError):
            dist.encode_join({"u": "ftp://x", "t": "hv_x", "n": "a"})
        import base64
        extra = "hvj1." + base64.urlsafe_b64encode(json.dumps(dict(d, x=1)).encode()).decode().rstrip("=")
        with self.assertRaises(HiveError):
            dist.decode_join(extra)

    def test_route_registration_noops(self):
        with self.assertLogs("hive.tools", level="WARNING"):
            tools.register_agent_routes(object(), lambda i, r: None, role="node")
        with self.assertRaises(ValueError):
            tools.register_agent_routes(object(), lambda i, r: None, role="node", mcp=True)
        with self.assertLogs("hive.gateway", level="WARNING"):
            gateway.register_gateway_routes(object(), role="node", backend=None, cfg=None)
        with self.assertRaises(HiveError):
            tools.RuntimeTarget(None, None, "")


if __name__ == "__main__":
    unittest.main()
