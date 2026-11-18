"""Audit round 3 (multi-machine) regression tests: node_id takeover by a relay pod, relay session ids and their
throttle, Content-Type of gateway replies chosen by a pod or a direct node, control characters in response headers."""
from __future__ import annotations

import json
import threading
import unittest

from hive import gateway, httpserver, util
from hive.client import connect
from hive.errors import HiveError
from hive.transport import HttpTransport
from tests import helpers
from tests.test_gateway import req
from tests.wp5util import FAST_RELAY, FakeRelay, call_async, hub_token_entry, ok_env


def _hub_audit(hub) -> str:
    return "".join(p.read_text() for p in (hub.data_dir / "audit").glob("*.jsonl"))


class _Case(unittest.TestCase):
    def make_hub(self, nodes, **cfg):
        cfg.setdefault("relay", dict(FAST_RELAY))
        url, toks, hub = helpers.start_hub(nodes=nodes, **cfg)
        self.addCleanup(hub.test_stop)
        return url, toks, hub


class TestRelayCannotTakeDirectNodeId(_Case):
    """Finding 1 end to end: a real hub, a real direct node and a fake relay pod speaking the §7 protocol."""

    def setUp(self):
        self.hub_tok = util.new_token()
        self.nurl, _a, self.nrt = helpers.start_node_server(auth={"tokens": [hub_token_entry(self.hub_tok)]},
                                                            gateway={"enabled": True})
        self.addCleanup(self.nrt.test_stop)
        self.rtok = util.new_token()
        nodes = [{"name": "desk", "mode": "direct", "url": self.nurl, "token": self.hub_tok},
                 {"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(self.rtok)}]
        self.url, self.toks, self.hub = self.make_hub(
            nodes, extra_tokens=[("podapp", {"name": "podapp", "kind": "models", "models": ["pod-a/*"]}),
                                 ("deskapp", {"name": "deskapp", "kind": "models", "models": ["desk/*"]})])
        self.client = connect(self.url, token=self.toks["agent"])
        self.addCleanup(self.client.close)
        self.desk = self.hub.links["desk"]
        self.pod = self.hub.links["pod-a"]
        # the hub learns desk's node_id from its authenticated probe (node.info with the hub token)
        helpers.wait_until(lambda: bool(self.desk.node_id), 15)
        self.desk_id = self.client.call("node.info", {}, node="desk")["node_id"]
        self.assertEqual(self.desk.node_id, self.desk_id)
        self.fake = FakeRelay(self.url, self.rtok, "pod-a")

    def call_meta_node(self, node: str) -> str:
        env = HttpTransport(self.url, self.toks["agent"]).request(
            "POST", "/v1/call", {"action": "node.info", "params": {}, "node": node}, raw=True)
        return json.loads(env[2])["meta"]["node"]

    def poll_in_background(self):
        """Keep the fake pod polling (and answering any task) so a call misrouted to it would show."""
        stop = threading.Event()
        got = []

        def run():
            while not stop.is_set():
                try:
                    for t in self.fake.poll(free_slots=8).get("tasks") or []:
                        got.append(t)
                        self.fake.result(t["task_id"], ok_env({"name": "pod-a", "node_id": self.desk_id}))
                except HiveError:
                    return
        th = threading.Thread(target=run, daemon=True)
        th.start()

        def finish():
            stop.set()
            th.join(10)
        self.addCleanup(finish)
        return got

    def test_register_with_direct_node_id_is_refused(self):
        self.fake.register(node_id=self.desk_id)
        self.assertEqual(self.pod.node_id, "")
        self.assertIsNone(self.hub.relay_ids.get("pod-a").get("node_id"))
        self.assertIn('"event":"relay node_id refused"', _hub_audit(self.hub))
        got = self.poll_in_background()
        # calls addressed by the id go to the direct node (meta.node = desk), case-insensitively
        self.assertEqual(self.call_meta_node(self.desk_id), "desk")
        self.assertEqual(self.call_meta_node(self.desk_id.upper()), "desk")
        # gateway requests addressed by the id resolve to desk: the pod's app token gets 403, desk's app the
        # node's own 404 (no such service on desk)
        st, _h, _b = req(self.url, "GET", f"/openai/{self.desk_id}/qwen/v1/models", token=self.toks["podapp"])
        self.assertEqual(st, 403)
        st, _h, body = req(self.url, "GET", f"/openai/{self.desk_id}/qwen/v1/models", token=self.toks["deskapp"])
        self.assertEqual(st, 404)
        self.assertIn("not found on desk", json.loads(body)["error"]["message"])
        self.assertEqual(got, [])

    def test_claim_made_before_the_probe_is_dropped(self):
        # the pod registers first (hub just started, desk not probed yet): its claim is kept for now
        self.desk._node_id = ""
        self.fake.register(node_id=self.desk_id)
        self.assertEqual(self.pod.node_id, self.desk_id)
        self.assertEqual(self.hub.relay_ids.get("pod-a").get("node_id"), self.desk_id)
        # the next probe that fetches node.info takes the id back from the pod (live session and relay_ids.json)
        self.desk.probe(actions=True)
        self.assertEqual(self.desk.node_id, self.desk_id)
        self.assertEqual(self.pod.node_id, "")
        self.assertIsNone(self.hub.relay_ids.get("pod-a").get("node_id"))
        stored = json.loads((self.hub.data_dir / "state" / "relay_ids.json").read_text())
        self.assertIsNone(stored["nodes"]["pod-a"].get("node_id"))
        audit = _hub_audit(self.hub)
        self.assertIn('"event":"relay node_id claim dropped"', audit)
        self.assertIn('"direct_node":"desk"', audit)
        got = self.poll_in_background()
        self.assertEqual(self.call_meta_node(self.desk_id), "desk")
        self.assertEqual(got, [])
        # registering again with the id is refused now
        self.fake.register(node_id=self.desk_id)
        self.assertEqual(self.pod.node_id, "")

    def test_direct_id_wins_even_if_both_hold_it(self):
        # defence in depth: should a relay link still hold the id, resolution prefers the direct link
        self.fake.register()
        with self.pod.lock:
            self.pod._node_id = self.desk_id
        self.assertIs(self.hub.resolve(self.desk_id), self.desk)

    def test_pod_keeps_its_own_id(self):
        self.fake.register(node_id="n_podaown1234")
        self.assertEqual(self.pod.node_id, "n_podaown1234")
        self.assertIs(self.hub.resolve("n_podaown1234"), self.pod)


class TestAmbiguousNodeId(_Case):
    def test_two_direct_links_with_one_id(self):
        nodes = [{"name": "d1", "mode": "direct", "url": "http://127.0.0.1:9", "token": util.new_token()},
                 {"name": "d2", "mode": "direct", "url": "http://127.0.0.1:9", "token": util.new_token()}]
        url, toks, hub = self.make_hub(nodes)
        hub.links["d1"]._node_id = "n_cloned1234"
        hub.links["d2"]._node_id = "n_CLONED1234"
        with self.assertRaises(HiveError) as cm:
            hub.resolve("n_cloned1234")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("address the node by name", cm.exception.hint)
        c = connect(url, token=toks["agent"])
        self.addCleanup(c.close)
        with self.assertRaises(HiveError) as cm:
            c.call("fs.stat", {"path": "/"}, node="n_cloned1234")
        self.assertEqual(cm.exception.code, "not_found")
        self.assertIn("ambiguous", cm.exception.message)
        # names still resolve
        self.assertIs(hub.resolve("d2"), hub.links["d2"])


class TestRelaySession(_Case):
    def setUp(self):
        self.rtok = util.new_token()
        self.url, self.toks, self.hub = self.make_hub(
            [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(self.rtok)}],
            throttle={"max_failures": 3, "window": 60, "block": 300})
        self.fake = FakeRelay(self.url, self.rtok, "pod-a")

    def test_session_id_has_192_random_bits(self):
        ids = set()
        for _ in range(3):
            sid = self.fake.register()["session_id"]
            self.assertRegex(sid, r"^s_[A-Za-z0-9_-]{32}\Z")
            ids.add(sid)
        self.assertEqual(len(ids), 3)

    def bad_poll(self) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.fake.poll(session_id="s_" + "A" * 32)
        return cm.exception

    def test_session_mismatches_are_throttled_per_ip(self):
        self.fake.register()
        for _ in range(3):
            self.assertEqual(self.bad_poll().code, "conflict")
        # blocked now, even with the right session (the token itself is valid)
        with self.assertRaises(HiveError) as cm:
            self.fake.poll()
        self.assertEqual((cm.exception.code, cm.exception.details["reason"]), ("rate_limited", "auth_throttle"))
        self.assertIn('"event":"relay session guesses blocked"', _hub_audit(self.hub))

    def test_register_clears_the_count(self):
        # what a real node does after a hub restart: 409 on the old session, then register
        for _ in range(4):
            self.assertEqual(self.bad_poll().code, "conflict")
            self.fake.register()
        self.assertEqual(self.fake.poll()["tasks"], [])

    def test_relay_agent_waits_out_the_block(self):
        from hive import relay

        class Agent:
            failures = 0
            session_id = "s_x"
            hub_url = "http://hub"
            slept = []

            def _backoff(self):
                return 1.0

            def _sleep(self, s):
                self.slept.append(s)
        a = Agent()
        with self.assertLogs("hive.relay", "WARNING"):
            relay.RelayAgent._on_error(a, HiveError("rate_limited", "blocked", {"retry_after": 250.0,
                                                                                "reason": "auth_throttle"}))
        self.assertEqual(a.slept, [250.0])


class TestGatewayContentType(unittest.TestCase):
    def test_safe_content_type(self):
        f = gateway.safe_content_type
        for v, want in (("application/json", "application/json"),
                        ("text/event-stream; charset=UTF-8", "text/event-stream; charset=utf-8"),
                        ("Text/Plain;charset=utf-8", "text/plain; charset=utf-8"),
                        ("text/html", "application/json"),
                        ("text/html\x0bX-Evil: 1", "application/json"),
                        ("application/json\x0bX-Evil: 1", "application/json"),
                        ("application/json; charset=utf-8\x0b", "application/json"),
                        ("text/plain; foo=bar", "application/json"),
                        ("text/plain; charset=utf-8; a=b", "application/json"),
                        ("text/plain; charset=\"utf-8\"", "application/json"),
                        ("", "application/json"), (None, "application/json"), (5, "application/json")):
            with self.subTest(v=v):
                self.assertEqual(f(v), want)
        self.assertEqual(gateway.safe_headers({"content-type": "text/html", "Retry-After": "5\x0b", "X-A": "b"}),
                         {"Content-Type": "application/json"})
        self.assertEqual(gateway.safe_headers({"Retry-After": "x5"}), {"Content-Type": "application/json"})
        self.assertEqual(gateway.safe_headers({"Retry-After": "7"}),
                         {"Retry-After": "7", "Content-Type": "application/json"})

    def test_direct_link_open_gateway_filters_headers(self):
        url, _toks, hub = helpers.start_hub(
            [{"name": "desk", "mode": "direct", "url": "http://127.0.0.1:9", "token": util.new_token()}])
        self.addCleanup(hub.test_stop)
        link = hub.links["desk"]
        link.transport.open_stream = lambda *a, **kw: (200, {"Content-Type": "text/html\x0bSet-Cookie: a=b",
                                                             "Retry-After": "x\x0b", "Set-Cookie": "a=b"},
                                                       iter([b"{}"]))
        r = link.open_gateway("qwen", "GET", "models", b"", agent="gw/x", timeout=5, cancel=threading.Event())
        self.assertEqual(r.headers, {"Content-Type": "application/json"})
        link.transport.open_stream = lambda *a, **kw: (200, {"content-type": "text/event-stream"}, iter([b""]))
        r = link.open_gateway("qwen", "GET", "models", b"", agent="gw/x", timeout=5, cancel=threading.Event())
        self.assertEqual(r.headers, {"Content-Type": "text/event-stream"})


class TestRelayGatewayContentType(_Case):
    """The pod's ``model.forward`` result chooses ``content_type``; the hub writes only an allowed type."""

    def test_pod_content_type(self):
        rtok = util.new_token()
        url, toks, hub = self.make_hub(
            [{"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(rtok)}],
            extra_tokens=[("models", {"name": "chatbox", "kind": "models", "models": ["pod-a/*"]})])
        fake = FakeRelay(url, rtok, "pod-a")
        fake.register()
        for ctype, want in (("text/html\x0bX-Evil: 1", "application/json"),
                            ("text/html", "application/json"),
                            ("application/json; charset=utf-8", "application/json; charset=utf-8"),
                            ("text/plain\x00", "application/json")):
            with self.subTest(ctype=ctype):
                box = call_async(req, url, "POST", "/openai/pod-a/qwen/v1/chat/completions", token=toks["models"],
                                 body={"model": "qwen"})
                t = fake.poll_until(1)[0]
                fake.result(t["task_id"], ok_env({"status": 200, "content_type": ctype, "body": "{}",
                                                  "truncated": False, "elapsed_s": 0.1}))
                box["thread"].join(15)
                st, h, _body = box["result"]
                self.assertEqual(st, 200)
                self.assertEqual(h["content-type"], want)
                self.assertNotIn("x-evil", h)


class _Conn:
    def __init__(self):
        self.data = b""

    def send(self, b):
        self.data += b


class TestHeaderControlChars(unittest.TestCase):
    def test_header_ok(self):
        ok = httpserver._header_ok
        self.assertTrue(ok("Content-Type", "application/json"))
        self.assertTrue(ok("X-A", "a\tb"))
        for k, v in (("X-A", "a\x0bb"), ("X-A", "a\x00"), ("X-A", "a\x7f"), ("X-A", "a\rb"), ("X-A", "a\nb"),
                     ("X-A", "\x1f"), ("X A", "v"), ("X-A:", "v"), ("", "v"), ("X-A\x0b", "v")):
            with self.subTest(k=k, v=v):
                self.assertFalse(ok(k, v))

    def test_write_drops_bad_headers(self):
        srv = httpserver.ApiServer.__new__(httpserver.ApiServer)
        conn = _Conn()
        srv._write(conn, 200, {"Content-Type": "text/html\x0bX-Evil: 1", "X-Good": "1", "X-Bad": "a\x00b"}, b"{}",
                   close=True)
        head = conn.data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        self.assertNotIn("X-Evil", head)
        self.assertNotIn("X-Bad", head)
        self.assertNotIn("\x0b", head)
        self.assertIn("X-Good: 1", head)
        # the dropped Content-Type is replaced by the default one
        self.assertIn("Content-Type: application/json; charset=utf-8", head)
        conn = _Conn()
        srv._write_stream(conn, 200, {"Content-Type": "text/plain\x0bX-Evil: 1", "X-Good": "1"}, iter([b"x"]),
                          threading.Event())
        head = conn.data.split(b"\r\n\r\n", 1)[0].decode("latin-1")
        self.assertNotIn("X-Evil", head)
        self.assertIn("X-Good: 1", head)


if __name__ == "__main__":
    unittest.main()
