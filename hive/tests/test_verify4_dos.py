"""Verification round 4 (DoS / local users): JSON container cap and sliced pre-scan, listen backlog, bounded regex
repeats, hfhub page pre-count. fs.edit / literal search: tests/test_fsops.py; audit records: tests/test_audit.py.
Performance bounds are generous (the old code took seconds to minutes)."""
from __future__ import annotations

import json
import random
import socket
import threading
import time
import unittest
import urllib.error
import urllib.request
from typing import Any, Callable, Tuple
from unittest import mock

from hive import httpserver
from hive.errors import HiveError
from hive.httpserver import (MAX_JSON_CONTAINERS, MAX_RELAY_JSON_CONTAINERS, Request, json_body_problem,
                             json_depth_ok)
from tests.test_audit4_dos import _gen, _ref_depth_ok
from tests.test_httpserver import T, Server
from tests.test_mcp_http import _HubMcp, post

N = 48 << 20


def _max_gil_gap(fn: Callable[[], Any]) -> Tuple[Any, float, float]:
    """(result, seconds, longest gap seen by a 1 ms ticker thread while ``fn`` ran)."""
    stop = threading.Event()
    gap = [0.0]

    def tick() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.001)
            now = time.perf_counter()
            gap[0] = max(gap[0], now - last)
            last = now

    t = threading.Thread(target=tick, daemon=True)
    t.start()
    time.sleep(0.02)
    gap[0] = 0.0
    t0 = time.monotonic()
    try:
        out = fn()
    finally:
        took = time.monotonic() - t0
        stop.set()
        t.join()
    return out, took, gap[0]


def _req(body: bytes) -> Request:
    return Request(method="POST", path="/v1/call", query={}, headers={}, client_ip="127.0.0.1", body=body,
                   identity=None)


class TestJsonContainerCap(unittest.TestCase):
    def test_refused_before_json_loads(self):
        body = b"[" + b"[]," * (N // 3) + b"[]]"
        with mock.patch.object(httpserver.json, "loads", side_effect=AssertionError("parsed")):
            t0 = time.monotonic()
            with self.assertRaises(HiveError) as cm:
                _req(body).json()
            self.assertLess(time.monotonic() - t0, 1.0)
        self.assertEqual((cm.exception.code, cm.exception.http_status), ("too_large", 413))
        self.assertEqual(json_body_problem(body, max_containers=MAX_RELAY_JSON_CONTAINERS), "containers")
        self.assertEqual(json_body_problem(b"{" + b'"k":{},' * (MAX_JSON_CONTAINERS + 1) + b'"z":1}'),
                         "containers")

    def test_limits(self):
        ok = b"[" + b"[]," * (MAX_JSON_CONTAINERS - 2) + b"1]"
        self.assertIsNone(json_body_problem(ok))
        self.assertEqual(len(_req(ok).json()), MAX_JSON_CONTAINERS - 1)
        over = b"[" + b"[]," * MAX_JSON_CONTAINERS + b"1]"
        self.assertEqual(json_body_problem(over), "containers")
        self.assertIsNone(json_body_problem(over, max_containers=MAX_RELAY_JSON_CONTAINERS))
        self.assertIsNone(json_body_problem(over, max_containers=None))
        # brackets inside strings never count: a big file's text in a string parameter
        text = json.dumps({"content": "{[" * (4 << 20)}).encode()
        self.assertIsNone(json_body_problem(text))
        self.assertEqual(json_body_problem(b"[" * 65 + b"]" * 65), "deep")
        with self.assertRaises(HiveError) as cm:
            _req(b"[" * 65 + b"]" * 65).json()
        self.assertEqual((cm.exception.code, cm.exception.message), ("bad_request", "JSON nested too deeply"))

    def test_no_long_gil_hold(self):
        # one 48 MiB '"[' body froze the server for 1.6 s (single C calls over the whole body)
        for name, body in (('"[', b'"[' * (N // 2)), ('"[]', b'"[]' * (N // 3)), ('[]"', b'[]"' * (N // 3)),
                           ("[] soup", b"[]" * (N // 2)), ('\\"[', b'["' + b'\\"[' * (N // 3) + b'"]')):
            with self.subTest(body=name):
                problem, took, gap = _max_gil_gap(lambda: json_body_problem(body))
                self.assertIn(problem, ("containers", None))
                self.assertLess(took, 2.0)
                self.assertLess(gap, 0.5)

    def test_slices_match_reference(self):
        # tiny slices: string / escape state carried across every boundary
        rnd = random.Random(11)
        atoms = ["[", "]", "{", "}", '"', "\\", "a", ",", "[[", "]]", "\\\\", '\\"', '""']
        with mock.patch.object(httpserver, "JSON_SLICE", 3):
            for _ in range(1500):
                v = json.dumps(_gen(rnd, rnd.randint(0, 10))).encode()
                for lim in (1, 2, 3, 5):
                    self.assertEqual(json_depth_ok(v, lim), _ref_depth_ok(v, lim), (v, lim))
                    expect = None if _ref_depth_ok(v, lim) else "deep"
                    self.assertEqual(json_body_problem(v, lim, None), expect)
            for _ in range(3000):
                v = "".join(rnd.choice(atoms) for _ in range(rnd.randint(0, 80))).encode()
                for lim in (1, 2, 3):
                    if json_depth_ok(v, lim):
                        self.assertTrue(_ref_depth_ok(v, lim), (v, lim))

    def test_server_mcp_and_relay(self):
        s = Server()
        self.addCleanup(s.stop)
        body = b"[" + b"[]," * (1 << 20) + b"[]]"
        rq = urllib.request.Request(f"http://127.0.0.1:{s.port}/v1/post", data=body, method="POST",
                                    headers={"Authorization": f"Bearer {T['agent']}",
                                             "Content-Type": "application/json"})
        t0 = time.monotonic()
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(rq, timeout=30)
        self.assertEqual(cm.exception.code, 413)
        self.assertIn(b"arrays/objects", cm.exception.read())
        self.assertLess(time.monotonic() - t0, 5.0)



class TestHubBodies(_HubMcp):
    def test_mcp_and_call_refuse_container_soup(self):
        body = b"[" + b"[]," * (1 << 20) + b"[]]"
        t0 = time.monotonic()
        st, _h, out = post(self.url, self.toks["agent"], None, raw=body)
        self.assertEqual(st, 413, out)
        self.assertIn("arrays/objects", json.dumps(out))
        st, _h, out = post(self.url, self.toks["agent"], None, path="/v1/call",
                           raw=b'{"action":"node.info","node":"desk","params":{"x":' + body + b"}}")
        self.assertEqual((st, out["error"]["code"]), (413, "too_large"))
        self.assertLess(time.monotonic() - t0, 10.0)
        st, _h, out = post(self.url, self.toks["agent"], {"action": "node.info", "node": "desk", "params": {}},
                           path="/v1/call")
        self.assertEqual(st, 200, out)


if __name__ == "__main__":
    unittest.main()
