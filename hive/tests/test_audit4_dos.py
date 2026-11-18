"""Audit round 4 (DoS / local users), findings 3, 6 and 7: pre-auth connection pools (one loopback bucket, a separate
proxy_port bucket, eviction of the oldest idle socket, 5 s pre-auth deadline), the authenticated pool with an owner
reserve, the per-identity cap and global byte budget checked before a body is read, relay results refused from
their X-Hive-Task / X-Hive-Session headers, the C-speed JSON depth scan, and the node's per-identity call slots with
an owner reserve."""
from __future__ import annotations

import json
import random
import socket
import threading
import time
import unittest
from unittest import mock

from hive import httpserver
from hive.auth import Identity
from hive.config import HubListenConfig, ListenConfig
from hive.httpserver import _ConnTable, json_depth_ok
from hive.limits import BodyBudget, ConcurrencyGate, KeyedLimit
from tests import helpers
from tests.test_httpserver import T, Server, _free_port
from tests.wp1util import OPERATOR, FakeHome, ident


def _ref_depth_ok(data: bytes, limit: int) -> bool:
    """The old byte loop, stopping where a parser stops (a backslash outside a string, an unmatched closer)."""
    depth, ins, esc = 0, False, False
    for b in data:
        if depth < 0 or (not ins and b == 0x5C):
            return True
        if ins:
            if esc:
                esc = False
            elif b == 0x5C:
                esc = True
            elif b == 0x22:
                ins = False
            continue
        if b == 0x22:
            ins = True
        elif b in (0x5B, 0x7B):
            depth += 1
            if depth > limit:
                return False
        elif b in (0x5D, 0x7D):
            depth -= 1
    return True


def _gen(rnd: random.Random, d: int):
    r = rnd.random()
    if d <= 0 or r < 0.3:
        return rnd.choice([1, "s", 'q"[\\', "[[[", "]]\\", None, "{\"", ""])
    if r < 0.65:
        return [_gen(rnd, d - 1) for _ in range(rnd.randint(0, 4))]
    return {f'{rnd.random():.3f}"[': _gen(rnd, d - 1) for _ in range(rnd.randint(0, 3))}


class TestJsonDepth(unittest.TestCase):
    def test_matches_reference_on_valid_json(self):
        rnd = random.Random(4)
        for _ in range(1500):
            v = json.dumps(_gen(rnd, rnd.randint(0, 12))).encode()
            for lim in (1, 2, 3, 5, 8):
                self.assertEqual(json_depth_ok(v, lim), _ref_depth_ok(v, lim), (v, lim))

    def test_never_true_when_the_parse_nests_deeper(self):
        rnd = random.Random(7)
        atoms = ["[", "]", "{", "}", '"', "\\", "a", ",", "[[", "]]", "\\\\", '\\"', '""']
        for _ in range(6000):
            v = "".join(rnd.choice(atoms) for _ in range(rnd.randint(0, 120))).encode()
            for lim in (1, 2, 3, 5):
                if json_depth_ok(v, lim):
                    self.assertTrue(_ref_depth_ok(v, lim), (v, lim))

    def test_edges(self):
        self.assertTrue(json_depth_ok(b"[" * 64 + b"]" * 64))
        self.assertFalse(json_depth_ok(b"[" * 65 + b"]" * 65))
        self.assertTrue(json_depth_ok(b'["' + b"[" * 100 + b'",' + b"[" * 63 + b"]" * 64))
        self.assertTrue(json_depth_ok(b'["\\"' + b"[" * 100 + b'",' + b"[" * 63 + b"]" * 64))
        self.assertFalse(json_depth_ok(b'["\\\\"' + b"[" * 100 + b'"]'))     # the escape ends before the quote
        self.assertTrue(json_depth_ok(b'["' + b"[" * 70))                    # an unterminated string swallows the rest
        self.assertFalse(json_depth_ok(b"[" * 65 + b'"'))
        self.assertTrue(json_depth_ok(bytearray(b"[1]")))

    def test_fast_on_big_bodies(self):
        # the old pure-Python loop took ~2 s per 48 MiB body (any content); generous bounds
        bodies = [('{"x":"' + "a" * (48 << 20) + '","y":[' + "[1]," * 100000 + "1]}").encode(),
                  ("[" + '"[",' * (4 << 20) + "1]").encode(),
                  ("[" + "[]," * (8 << 20) + "1]").encode()]
        for b in bodies:
            t0 = time.monotonic()
            self.assertTrue(json_depth_ok(b))
            self.assertLess(time.monotonic() - t0, 1.5)


class _Sock:
    def __init__(self) -> None:
        self.a, self.b = socket.socketpair()

    def close(self) -> None:
        self.a.close()
        self.b.close()


class TestConnTable(unittest.TestCase):
    def test_split_and_owner_reserve(self):
        t = _ConnTable(64)
        self.assertEqual((t.pre_cap, t.auth_cap, t.owner_reserve), (32, 32, 4))
        t = _ConnTable(8)
        self.assertEqual((t.pre_cap, t.auth_cap, t.owner_reserve), (4, 4, 2))
        slots = [t.admit(object(), f"10.0.0.{i}") for i in range(4)]
        self.assertTrue(t.authenticate(slots[0], False))
        self.assertTrue(t.authenticate(slots[1], False))
        self.assertFalse(t.authenticate(slots[2], False))         # non-owner share (auth 4 - reserve 2) used up
        self.assertTrue(t.authenticate(slots[2], True))
        self.assertTrue(t.authenticate(slots[3], True))
        extra = t.admit(object(), "10.0.0.9")
        self.assertFalse(t.authenticate(extra, True))             # the authenticated pool is full
        t.release(slots[0])
        self.assertTrue(t.authenticate(extra, True))
        snap = t.snapshot()
        self.assertEqual((snap["pre"], snap["auth"], snap["owner"]), (0, 4, 3))

    def test_eviction_oldest_waiting_same_bucket(self):
        socks = [_Sock() for _ in range(20)]
        self.addCleanup(lambda: [s.close() for s in socks])
        t = _ConnTable(64)
        slots = [t.admit(socks[i].a, "loopback") for i in range(16)]
        t.waiting(slots[0], False)                                # busy (head read): never evicted
        new = t.admit(socks[16].a, "loopback")
        self.assertIsNotNone(new)
        self.assertTrue(slots[1].evicted)
        self.assertFalse(slots[0].evicted)
        socks[1].b.settimeout(2)
        self.assertEqual(socks[1].b.recv(10), b"")                # the victim's socket was shut down
        self.assertFalse(t.waiting(slots[1], True))
        self.assertEqual(t.snapshot()["buckets"], {"loopback": 16})
        t.release(slots[1])                                       # its thread ends: no double count
        self.assertEqual(t.snapshot()["pre"], 16)
        other = t.admit(socks[17].a, "proxy")                     # another bucket never evicts loopback
        self.assertIsNotNone(other)
        self.assertEqual(t.snapshot()["buckets"], {"loopback": 16, "proxy": 1})

    def test_pool_full_evicts_from_largest_bucket_or_refuses(self):
        t = _ConnTable(8)                                         # pre-auth pool 4
        a = [t.admit(object(), "proxy") for _ in range(3)]
        b = t.admit(object(), "loopback")
        with mock.patch.object(httpserver.socket.socket, "shutdown"):
            c = t.admit(object(), "10.1.1.1")
        self.assertIsNotNone(c)
        self.assertTrue(a[0].evicted)
        self.assertFalse(b.evicted)
        for s in a[1:] + [b, c]:
            t.waiting(s, False)
        self.assertIsNone(t.admit(object(), "10.2.2.2"))          # nobody idle to evict: 503


class TestLimits(unittest.TestCase):
    def test_gate_owner_reserve_and_per_identity(self):
        g = ConcurrencyGate(8, owner_reserve=4)
        self.assertEqual((g.shared, g.per_identity), (4, 4))
        for _ in range(4):
            self.assertTrue(g.acquire(0, key="a"))
        self.assertFalse(g.acquire(0.05, key="b"))                # the rest is reserved for owners
        for _ in range(4):
            self.assertTrue(g.acquire(0, owner=True))
        self.assertFalse(g.acquire(0.05, owner=True))
        g.release(owner=True)
        self.assertFalse(g.acquire(0.05, key="b"))
        g.release(key="a")
        self.assertTrue(g.acquire(0, key="b"))
        g = ConcurrencyGate(6, owner_reserve=2, per_identity=2)
        self.assertTrue(g.acquire(0, key="a") and g.acquire(0, key="a"))
        self.assertFalse(g.acquire(0.05, key="a"))
        self.assertEqual(g.held_by("a"), 2)
        self.assertTrue(g.acquire(0, key="b") and g.acquire(0, key="b"))
        self.assertFalse(g.acquire(0.05, key="c"))                # shared 4 used
        self.assertEqual(ConcurrencyGate(1, owner_reserve=4).owner_reserve, 0)
        w = ConcurrencyGate(1)
        w.acquire(0, key="x")
        threading.Timer(0.1, lambda: w.release(key="x")).start()
        self.assertTrue(w.acquire(2, key="y"))                    # a waiter is woken by a release

    def test_body_budget(self):
        b = BodyBudget(1000, reserve=250)
        self.assertTrue(b.acquire(700))
        self.assertFalse(b.acquire(100, timeout=0.05))            # 800 > 750 for non-owners
        self.assertTrue(b.acquire(300, owner=True))
        b.release(700)
        b.release(300)
        self.assertTrue(b.acquire(5000))                          # larger than the budget: only when idle
        self.assertFalse(b.acquire(1, owner=True, timeout=0.05))
        threading.Timer(0.1, lambda: b.release(5000)).start()
        self.assertTrue(b.acquire(10, timeout=2))

    def test_keyed_limit(self):
        k = KeyedLimit(2)
        self.assertTrue(k.acquire("a") and k.acquire("a"))
        self.assertFalse(k.acquire("a"))
        self.assertTrue(k.acquire("b"))
        k.release("a")
        self.assertTrue(k.acquire("a"))

    def test_config_defaults(self):
        self.assertEqual((ListenConfig().max_buffered_bytes, HubListenConfig().max_buffered_bytes),
                         (128 << 20, 256 << 20))


def _get(port: int, token: str, *, host: str = "127.0.0.1", src: str = "127.0.0.1") -> bytes:
    s = socket.socket()
    s.bind((src, 0))
    s.settimeout(10)
    s.connect(("127.0.0.1", port))
    try:
        s.sendall(f"GET /v1/echo HTTP/1.1\r\nHost: {host}\r\nAuthorization: Bearer {token}\r\nConnection: close"
                  f"\r\n\r\n".encode())
        out = b""
        while True:
            c = s.recv(65536)
            if not c:
                return out
            out += c
    finally:
        s.close()


def _idle(n: int, port: int, src: str = "127.0.0.1"):
    out = []
    for _ in range(n):
        s = socket.socket()
        s.bind((src, 0))
        s.settimeout(10)
        s.connect(("127.0.0.1", port))
        out.append(s)
    return out


def _post_head(port: int, token: str, path: str, length: int) -> socket.socket:
    s = socket.create_connection(("127.0.0.1", port), timeout=10)
    s.sendall(f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {token}\r\nContent-Type: "
              f"application/json\r\nContent-Length: {length}\r\n\r\n".encode())
    return s


def _read_response(s: socket.socket) -> bytes:
    out = b""
    while b"\r\n\r\n" not in out:
        c = s.recv(65536)
        if not c:
            return out
        out += c
    head, _, rest = out.partition(b"\r\n\r\n")
    n = int([ln.split(b":", 1)[1] for ln in head.split(b"\r\n") if ln.lower().startswith(b"content-length")][0])
    while len(rest) < n:
        c = s.recv(65536)
        if not c:
            break
        rest += c
    return head + b"\r\n\r\n" + rest


class TestSlowloris(unittest.TestCase):
    """Finding 3 with real servers on loopback (127.0.0.2+ are loopback aliases on Linux)."""

    def setUp(self):
        self.socks = []
        self.addCleanup(lambda: [s.close() for s in self.socks])

    def test_local_flood_cannot_lock_out_owner_or_agents(self):
        s = Server()
        self.addCleanup(s.stop)
        for src in ("127.0.0.1", "127.0.0.2", "127.0.0.3"):
            try:
                self.socks += _idle(30, s.port, src)
            except OSError:                                       # no 127/8 aliases on this platform
                pass
        time.sleep(0.3)
        snap = s.api._conns.snapshot()
        self.assertEqual(snap["buckets"].get("loopback"), 16)     # ONE bucket for all of 127/8
        self.assertLessEqual(snap["pre"], snap["pre_cap"])
        self.assertIn(b"200 OK", _get(s.port, T["owner"]))
        self.assertIn(b"200 OK", _get(s.port, T["agent"]))
        self.assertGreater(s.api._conns.snapshot()["evictions"], 0)

    def test_proxy_port_flood_is_its_own_bucket(self):
        s = Server(proxy_port=_free_port(), allowed_hosts=["hub.example.ts.net"])
        self.addCleanup(s.stop)
        proxy = s.bound[-1][1]
        self.socks += _idle(40, proxy)
        self.socks += _idle(10, s.port)                           # some local idle sockets too
        time.sleep(0.3)
        snap = s.api._conns.snapshot()
        self.assertEqual((snap["buckets"].get("proxy"), snap["buckets"].get("loopback")), (16, 10))
        self.assertIn(b"200 OK", _get(s.port, T["owner"]))
        self.assertIn(b"200 OK", _get(proxy, T["agent"], host="hub.example.ts.net"))
        self.assertEqual(s.api._conns.snapshot()["buckets"].get("loopback"), 10)   # never evicted by proxy

    def test_preauth_deadline_is_total(self):
        with mock.patch.object(httpserver, "PREAUTH_DEADLINE", 1.0):
            s = Server()
            self.addCleanup(s.stop)
            c = _idle(1, s.port)[0]
            self.socks.append(c)
            t0 = time.monotonic()
            self.assertEqual(c.recv(10), b"")
            self.assertLess(time.monotonic() - t0, 3)
            d = _idle(1, s.port)[0]                               # a dribbling head: the deadline is absolute
            self.socks.append(d)
            t0 = time.monotonic()
            closed = False
            for ch in b"GET /v1/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Pad: " + b"a" * 40:
                try:
                    d.sendall(bytes([ch]))
                except OSError:
                    closed = True
                    break
                time.sleep(0.1)
            if not closed:
                try:
                    closed = d.recv(10) == b""
                except OSError:
                    closed = True
            self.assertTrue(closed)
            self.assertLess(time.monotonic() - t0, 4.5)
            # after authentication the keep-alive idle timeout is the normal one
            k = socket.create_connection(("127.0.0.1", s.port), timeout=10)
            self.socks.append(k)
            k.sendall(f"GET /v1/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {T['agent']}\r\n\r\n"
                      .encode())
            self.assertIn(b"200 OK", _read_response(k))
            time.sleep(1.5)
            k.sendall(f"GET /v1/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {T['agent']}\r\n\r\n"
                      .encode())
            self.assertIn(b"200 OK", _read_response(k))

    def test_owner_reserve_when_authenticated_pool_is_busy(self):
        s = Server(max_connections=8)                             # pre 4, auth 4, owner reserve 2
        self.addCleanup(s.stop)
        for _ in range(2):
            k = socket.create_connection(("127.0.0.1", s.port), timeout=10)
            self.socks.append(k)
            k.sendall(f"GET /v1/echo HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {T['agent']}\r\n\r\n"
                      .encode())
            self.assertIn(b"200 OK", _read_response(k))
        out = _get(s.port, T["hub"])
        self.assertIn(b" 503 ", out.split(b"\r\n")[0])
        self.assertIn(b"Retry-After: 1", out)
        self.assertIn(b"200 OK", _get(s.port, T["owner"]))


class TestBodiesBeforeRead(unittest.TestCase):
    """Finding 6: per-identity cap and byte budget are checked before the body is read; pre_body hooks."""

    def setUp(self):
        self.s = Server()
        self.addCleanup(self.s.stop)
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.s.api.route("POST", "/v1/slow", lambda r: self.release.wait(10) and {"n": len(r.body)})
        self.socks = []
        self.addCleanup(lambda: [x.close() for x in self.socks])

    def hold(self, token: str, length: int) -> socket.socket:
        c = _post_head(self.s.port, token, "/v1/slow", length)
        c.sendall(b'"' + b"a" * (length - 2) + b'"')
        self.socks.append(c)
        return c

    def test_per_identity_cap_before_body(self):
        self.s.api.inflight = KeyedLimit(1)
        self.hold(T["agent"], 100)
        time.sleep(0.3)
        c = _post_head(self.s.port, T["agent"], "/v1/slow", 30 << 20)   # body never sent: answered anyway
        self.socks.append(c)
        out = _read_response(c)
        self.assertIn(b" 429 ", out.split(b"\r\n")[0])
        self.assertIn(b'"reason":"inflight"', out)
        self.assertIn(b"Connection: close", out)
        o = self.hold(T["owner"], 100)                            # owners are not capped
        h = self.hold(T["hub"], 100)                              # another identity is not affected
        self.release.set()
        self.assertIn(b"200 OK", _read_response(o))
        self.assertIn(b"200 OK", _read_response(h))

    def test_byte_budget_before_body(self):
        self.s.api.body_budget = BodyBudget(1000, reserve=250)
        with mock.patch.object(httpserver, "BODY_WAIT", 0.3):
            first = self.hold(T["agent"], 700)
            time.sleep(0.3)
            c = _post_head(self.s.port, T["hub"], "/v1/slow", 200)
            self.socks.append(c)
            out = _read_response(c)
            self.assertIn(b" 503 ", out.split(b"\r\n")[0])
            self.assertIn(b"Retry-After: 2", out)
            o = self.hold(T["owner"], 200)                        # the owner reserve
            self.release.set()
            self.assertIn(b"200 OK", _read_response(first))
            self.assertIn(b"200 OK", _read_response(o))
        for _ in range(100):         # the server thread releases its share just after writing the response
            if self.s.api.body_budget.used == 0:
                break
            time.sleep(0.02)
        self.assertEqual(self.s.api.body_budget.used, 0)

    def test_pre_body_short_circuit(self):
        seen = []

        def pre(headers, ident_, length):
            seen.append((headers.get("x-hive-task"), ident_.name, length))
            if headers.get("x-hive-task") == "bad":
                return {"accepted": False}
            return None
        self.s.api.route("POST", "/v1/pre", lambda r: {"n": len(r.body)}, pre_body=pre)
        c = socket.create_connection(("127.0.0.1", self.s.port), timeout=10)
        self.socks.append(c)
        c.sendall(f"POST /v1/pre HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {T['agent']}\r\n"
                  f"X-Hive-Task: bad\r\nContent-Type: application/json\r\nContent-Length: {40 << 20}\r\n\r\n"
                  .encode())
        out = _read_response(c)
        self.assertIn(b"200 OK", out)
        self.assertIn(b'"accepted":false', out)
        self.assertIn(b"Connection: close", out)
        self.assertEqual(seen, [("bad", "agent", 40 << 20)])
        c2 = _post_head(self.s.port, T["agent"], "/v1/pre", 4)
        self.socks.append(c2)
        c2.sendall(b"[12]")
        self.assertIn(b'"n":4', _read_response(c2))


class TestNodeCallSlots(unittest.TestCase):
    """Finding 7: one identity cannot take every node call slot; owners keep a reserve."""

    def setUp(self):
        self.fh = FakeHome().__enter__()
        self.addCleanup(self.fh.__exit__, None, None, None)
        self.rt = helpers.make_runtime(start_managers=False, limits={"max_concurrent_calls": 6,
                                                                     "max_calls_per_identity": 2,
                                                                     "owner_reserved_calls": 2})
        self.addCleanup(self.rt.stop)

    def test_per_identity_and_owner_reserve(self):
        from hive.actions import HANDLERS
        from hive import node as node_mod
        gate = threading.Event()
        started = []

        def slow(ctx, p):
            started.append(1)
            gate.wait(10)
            return {}
        self.addCleanup(gate.set)
        threads = []
        with mock.patch.dict(HANDLERS, {"node.info": slow}), mock.patch.object(node_mod, "GATE_WAIT", 0.2):
            def run(who):
                t = threading.Thread(target=self.rt.call, args=(who, "node.info", {}), daemon=True)
                t.start()
                threads.append(t)
            a, b = ident(name="a"), ident(name="b")
            run(a)
            run(a)
            helpers.wait_until(lambda: len(started) == 2, 5)
            e = self.rt.call(a, "node.info", {})["error"]
            self.assertEqual((e["code"], e["details"]["reason"]), ("rate_limited", "inflight"))
            run(b)
            run(b)
            helpers.wait_until(lambda: len(started) == 4, 5)
            e = self.rt.call(ident(name="c"), "node.info", {})["error"]
            self.assertEqual((e["code"], e["details"]["reason"]), ("unavailable", "busy"))
            owner = Identity(name="owner", kind="owner", classes=frozenset(OPERATOR.classes), source_ip="127.0.0.1")
            run(owner)
            run(owner)
            helpers.wait_until(lambda: len(started) == 6, 5)
            gate.set()
            for t in threads:
                t.join(10)
        self.assertEqual(self.rt.gate.inflight, 0)


class TestRelayResultHeaders(unittest.TestCase):
    """Finding 6: the hub refuses a relay result for a task it does not expect before reading the body."""

    def test_headers_checked_before_body(self):
        from hive import util
        from hive.client import connect
        from tests.wp5util import FAST_RELAY, FakeRelay, call_async, ok_env
        rtok = util.new_token()
        e = {"name": "pod-a", "mode": "relay", "token_sha256": util.hash_token(rtok)}
        url, toks, hub = helpers.start_hub([e], relay=dict(FAST_RELAY))
        self.addCleanup(hub.test_stop)
        fake = FakeRelay(url, rtok, "pod-a")
        fake.register()
        client = connect(url, token=toks["agent"])
        self.addCleanup(client.close)
        box = call_async(client.call, "fs.stat", {"path": "/x"}, node="pod-a")
        tasks = fake.poll_until(1)
        tid = tasks[0]["task_id"]
        port = int(url.rsplit(":", 1)[1])

        def post(headers: str, body: bytes = b"", length: int = 0) -> bytes:
            s = socket.create_connection(("127.0.0.1", port), timeout=10)
            try:
                s.sendall(f"POST /v1/relay/result HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer {rtok}\r\n"
                          f"Content-Type: application/json\r\nContent-Length: {length or len(body)}\r\n{headers}\r\n"
                          .encode() + body)
                return _read_response(s)
            finally:
                s.close()
        # unknown task / wrong session: answered without the (never sent) 30 MiB body
        out = post("X-Hive-Task: t_unknown\r\nX-Hive-Session: x\r\n", length=30 << 20)
        self.assertIn(b'"accepted":false', out)
        out = post(f"X-Hive-Task: {tid}\r\nX-Hive-Session: wrong\r\n", length=30 << 20)
        self.assertIn(b'"accepted":false', out)
        # headers and body must agree
        env = {"session_id": fake.session, "task_id": "t_other", "envelope": ok_env({"exists": False})}
        out = post(f"X-Hive-Task: {tid}\r\nX-Hive-Session: {fake.session}\r\n", json.dumps(env).encode())
        self.assertIn(b" 400 ", out.split(b"\r\n")[0])
        env["task_id"] = tid
        out = post(f"X-Hive-Task: {tid}\r\nX-Hive-Session: {fake.session}\r\n", json.dumps(env).encode())
        self.assertIn(b'"accepted":true', out)
        box["thread"].join(10)
        self.assertEqual(box["result"], {"exists": False})

    def test_relay_agent_sends_headers(self):
        from hive.relay import RelayAgent
        agent = RelayAgent.__new__(RelayAgent)
        agent.max_result_bytes = 1 << 20
        agent._stop = threading.Event()
        agent.transport = mock.Mock()
        agent.transport.request.return_value = {"accepted": True}
        agent._post_result("s_1", "t_1", {"ok": True, "result": {}})
        kw = agent.transport.request.call_args.kwargs
        self.assertEqual(kw["headers"], {"X-Hive-Task": "t_1", "X-Hive-Session": "s_1"})


if __name__ == "__main__":
    unittest.main()
