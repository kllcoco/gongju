"""WP6b: the hub's Streamable HTTP ``/mcp`` (§18.5, §21.2 tools/render/mcp/cli row): session header, session bound to
its token, per-token session cap, 202 for notifications, GET 405, DELETE, version header, ``wait_budget``/``profile``
query, ``fs_transfer`` with ``local:`` → bad_request, a real tool call through a direct node; the node serves no
``/mcp``; ``listen.mcp_http:false`` → 404."""
from __future__ import annotations

import http.client
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from hive import mcp, util

from tests import helpers
from tests.wp5util import hub_token_entry


def post(url: str, token: Optional[str], body: Any, *, sid: Optional[str] = None, path: str = "/mcp",
         method: str = "POST", headers: Optional[Dict[str, str]] = None, raw: Optional[bytes] = None
         ) -> Tuple[int, Dict[str, str], Any]:
    """One HTTP request -> (status, lower-cased headers, parsed JSON body or None)."""
    u = urlparse(url)
    conn = http.client.HTTPConnection(u.hostname, u.port, timeout=60)
    h = {"Accept": "application/json, text/event-stream"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Type"] = "application/json"
    if sid:
        h["Mcp-Session-Id"] = sid
    h.update(headers or {})
    conn.request(method, path, body=data, headers=h)
    r = conn.getresponse()
    text = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    try:
        return r.status, hdrs, json.loads(text) if text else None
    except ValueError:
        return r.status, hdrs, text


def init_msg(version: str = "2025-06-18", mid: Any = 1) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


class _HubMcp(unittest.TestCase):
    hub_extra: Dict[str, Any] = {}

    @classmethod
    def setUpClass(cls):
        cls._h = helpers.tmp_home()
        cls.home = cls._h.__enter__()
        cls.root = Path(tempfile.mkdtemp(prefix="hivemcp-")).resolve()
        cls.addClassCleanup(shutil.rmtree, cls.root, True)
        cls.hub_tok = util.new_token()
        cls.nurl, _a, cls.nrt = helpers.start_node_server(
            start_managers=True, auth={"tokens": [hub_token_entry(cls.hub_tok)]}, workdir=str(cls.root),
            policy={"fs": {"roots": [{"path": str(cls.root), "mode": "rw"}]}})
        cls.url, cls.toks, cls.hub = helpers.start_hub(
            [{"name": "desk", "mode": "direct", "url": cls.nurl, "token": cls.hub_tok}],
            extra_tokens=[("other", {"name": "other-agent", "kind": "agent", "role": "operator"}),
                          ("reader", {"name": "reader", "kind": "agent", "role": "readonly"})], **cls.hub_extra)
        helpers.wait_until(lambda: cls.hub.links["desk"].summary().get("online"), 15)

    @classmethod
    def tearDownClass(cls):
        cls.hub.test_stop()
        cls.nrt.test_stop()
        cls._h.__exit__(None, None, None)

    def initialize(self, token_key: str = "agent", *, query: str = "", version: str = "2025-06-18"
                   ) -> Tuple[str, dict]:
        st, h, body = post(self.url, self.toks[token_key], init_msg(version), path="/mcp" + query)
        self.assertEqual(st, 200, body)
        self.assertIn("mcp-session-id", h)
        self.assertTrue(h["content-type"].startswith("application/json"))
        return h["mcp-session-id"], body


class TestHubMcp(_HubMcp):
    def test_initialize_and_session_header(self):
        for v in mcp.SUPPORTED_VERSIONS:
            sid, body = self.initialize(version=v)
            self.assertEqual(body["result"]["protocolVersion"], v)
            self.assertGreaterEqual(len(sid), 40)
            if v >= "2025-03-26":
                self.assertIn("instructions", body["result"])
            st, h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "method": "notifications/initialized"},
                            sid=sid)
            self.assertEqual((st, b), (202, None))
            st, h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sid,
                            headers={"MCP-Protocol-Version": v})
            self.assertEqual((st, b["result"]), (200, {}))

    def test_tools_list_and_call_through_direct_node(self):
        sid, _ = self.initialize()
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid=sid)
        self.assertEqual(st, 200)
        names = [t["name"] for t in b["result"]["tools"]]
        self.assertIn("shell_run", names)
        self.assertEqual(len(names), 28)
        p = self.root / "hello.txt"
        st, _h, b = post(self.url, self.toks["agent"],
                         {"jsonrpc": "2.0", "id": "c1", "method": "tools/call",
                          "params": {"name": "fs_write", "arguments": {"path": str(p), "content": "hi from mcp"}}},
                         sid=sid)
        self.assertEqual(st, 200, b)
        self.assertEqual(b["id"], "c1")
        self.assertFalse(b["result"]["isError"], b)
        self.assertEqual(p.read_text(), "hi from mcp")
        st, _h, b = post(self.url, self.toks["agent"],
                         {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "fs_read", "arguments": {"path": str(p)}}}, sid=sid)
        self.assertIn("hi from mcp", b["result"]["content"][0]["text"])
        # validation errors are tool results; unknown tools are -32602
        st, _h, b = post(self.url, self.toks["agent"],
                         {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                          "params": {"name": "fs_read", "arguments": {"bogus": 1}}}, sid=sid)
        self.assertTrue(b["result"]["isError"])
        st, _h, b = post(self.url, self.toks["agent"],
                         {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope"}}, sid=sid)
        self.assertEqual(b["error"]["code"], -32602)

    @unittest.skipIf(os.name == "nt", "POSIX sleep")
    def test_cancellation(self):
        import threading
        sid, _ = self.initialize()
        box = {}

        def call():
            box["r"] = post(self.url, self.toks["agent"],
                            {"jsonrpc": "2.0", "id": "long", "method": "tools/call",
                             "params": {"name": "shell_run", "arguments": {"command": "sleep 20", "timeout": 60}}},
                            sid=sid)
        t = threading.Thread(target=call, daemon=True)
        t0 = time.monotonic()
        t.start()
        time.sleep(1.0)
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "method": "notifications/cancelled",
                                                        "params": {"requestId": "long"}}, sid=sid)
        self.assertEqual(st, 202)
        t.join(15)
        self.assertFalse(t.is_alive())
        st, _h, b = box["r"]
        self.assertEqual((st, b["id"], b["error"]["code"]), (200, "long", -32800))
        self.assertLess(time.monotonic() - t0, 15)

    def test_fs_transfer_local_refused(self):
        sid, _ = self.initialize()
        st, _h, b = post(self.url, self.toks["agent"],
                         {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                          "params": {"name": "fs_transfer", "arguments": {"src": "local:/etc/hosts",
                                                                          "dst": f"desk:{self.root}/x"}}}, sid=sid)
        self.assertEqual(st, 200)
        self.assertTrue(b["result"]["isError"])
        text = b["result"]["content"][0]["text"]
        self.assertIn("bad_request", text)
        self.assertIn("local:", text)
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid=sid)
        desc = [t for t in b["result"]["tools"] if t["name"] == "fs_transfer"][0]["description"]
        self.assertIn("local: paths are not available over remote MCP", desc)

    def test_session_bound_to_token(self):
        sid, _ = self.initialize("agent")
        st, _h, b = post(self.url, self.toks["other"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sid)
        self.assertEqual(st, 404)
        self.assertEqual(b["error"]["code"], -32001)
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "ping"},
                         sid="x" * 43)
        self.assertEqual(st, 404)
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "ping"})
        self.assertEqual(st, 400)
        st, _h, b = post(self.url, None, {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sid)
        self.assertEqual(st, 401)
        # the token is never taken from the query
        st, _h, b = post(self.url, None, init_msg(), path="/mcp?token=" + self.toks["agent"])
        self.assertEqual(st, 401)

    def test_identity_rebound_per_request(self):
        """A readonly token's session lists only read tools; another token cannot borrow it."""
        sid, _ = self.initialize("reader")
        st, _h, b = post(self.url, self.toks["reader"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid=sid)
        names = [t["name"] for t in b["result"]["tools"]]
        self.assertNotIn("shell_run", names)
        self.assertIn("fs_read", names)
        sessions = self.hub.server.mcp_sessions
        rec = sessions.acquire(sid, "reader")
        try:
            self.assertEqual(rec.core.toolkit.client.target.identity.name, "reader")
        finally:
            sessions.release(rec)

    def test_per_token_cap_and_delete(self):
        sids = [self.initialize("other")[0] for _ in range(mcp.SESSIONS_PER_TOKEN + 1)]
        # the least recently used one was evicted
        st, _h, b = post(self.url, self.toks["other"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sids[0])
        self.assertEqual(st, 404)
        st, _h, b = post(self.url, self.toks["other"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sids[-1])
        self.assertEqual(st, 200)
        mine = [s for s in self.hub.server.mcp_sessions._s.values() if s.token_name == "other-agent"]
        self.assertEqual(len(mine), mcp.SESSIONS_PER_TOKEN)
        st, _h, b = post(self.url, self.toks["other"], None, sid=sids[-1], method="DELETE")
        self.assertEqual(st, 200)
        st, _h, b = post(self.url, self.toks["other"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sids[-1])
        self.assertEqual(st, 404)
        st, _h, b = post(self.url, self.toks["other"], None, sid=sids[-1], method="DELETE")
        self.assertEqual(st, 404)

    def test_get_405_version_header_and_bad_bodies(self):
        st, _h, _b = post(self.url, self.toks["agent"], None, method="GET")
        self.assertEqual(st, 405)
        sid, _ = self.initialize()
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "ping"}, sid=sid,
                         headers={"MCP-Protocol-Version": "2024-11-05"})
        self.assertEqual(st, 400)
        st, _h, b = post(self.url, self.toks["agent"], None, sid=sid, raw=b"{not json")
        self.assertEqual((st, b["error"]["code"]), (400, -32700))
        st, _h, b = post(self.url, self.toks["agent"], [{"jsonrpc": "2.0", "id": 1, "method": "ping"}], sid=sid)
        self.assertEqual((st, b["error"]["code"]), (400, -32600))
        # a response from the client: accepted, nothing to answer
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 77, "result": {}}, sid=sid)
        self.assertEqual(st, 202)
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 3, "method": "nope"}, sid=sid)
        self.assertEqual(b["error"]["code"], -32601)

    def test_query_profile_and_wait_budget(self):
        sid, _ = self.initialize(query="?profile=minimal&wait_budget=999")
        st, _h, b = post(self.url, self.toks["agent"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, sid=sid)
        self.assertEqual(len(b["result"]["tools"]), 10)
        rec = self.hub.server.mcp_sessions.acquire(sid, "test-agent")
        try:
            self.assertEqual(rec.core.toolkit.wait_budget, 280.0)
            self.assertTrue(rec.core.toolkit.server_hosted)
            self.assertFalse(rec.core.watch)
        finally:
            self.hub.server.mcp_sessions.release(rec)
        sid, _ = self.initialize(query="?profile=full&wait_budget=1")
        rec = self.hub.server.mcp_sessions.acquire(sid, "test-agent")
        try:
            self.assertEqual(rec.core.toolkit.wait_budget, 10.0)
            self.assertEqual(rec.core.toolkit.profile, "full")
        finally:
            self.hub.server.mcp_sessions.release(rec)
        st, _h, b = post(self.url, self.toks["agent"], init_msg(), path="/mcp?profile=huge")
        self.assertEqual(st, 400)
        st, _h, b = post(self.url, self.toks["agent"], init_msg(), path="/mcp?wait_budget=abc")
        self.assertEqual(st, 400)

    def test_origin_rejected(self):
        st, _h, b = post(self.url, self.toks["agent"], init_msg(), headers={"Origin": "https://evil.example"})
        self.assertEqual(st, 403)


class TestHubMcpDisabled(_HubMcp):
    hub_extra = {"listen": {"mcp_http": False}}

    def test_404_when_disabled(self):
        st, _h, _b = post(self.url, self.toks["agent"], init_msg())
        self.assertEqual(st, 404)
        # GET /v1/tools stays
        st, _h, b = post(self.url, self.toks["agent"], None, path="/v1/tools", method="GET")
        self.assertEqual(st, 200)


class TestNodeServesNoMcp(unittest.TestCase):
    def test_node_has_no_mcp(self):
        with helpers.tmp_home():
            url, tok, rt = helpers.start_node_server()
            try:
                st, _h, _b = post(url, tok, init_msg())
                self.assertEqual(st, 404)
            finally:
                rt.test_stop()


class TestSessionTable(unittest.TestCase):
    class _Core:
        def __init__(self):
            self.closed = 0

        def close(self, session=None, wait=0):
            self.closed += 1
            return True

    def test_idle_expiry_and_total_cap(self):
        now = [1000.0]
        t = mcp.HttpSessions(idle=3600, per_token=3, total=4, clock=lambda: now[0])
        cores = []
        recs = []
        for i in range(4):
            c = self._Core()
            cores.append(c)
            recs.append(t.create(f"tok{i % 2}", c, mcp.McpSession()))
            now[0] += 1
        self.assertEqual(len(t), 4)
        c = self._Core()
        t.create("tok9", c, mcp.McpSession())          # total cap: the LRU session goes
        self.assertEqual(len(t), 4)
        self.assertEqual(cores[0].closed, 1)
        self.assertIsNone(t.acquire(recs[0].id, "tok0"))
        r = t.acquire(recs[1].id, "tok1")
        self.assertIsNotNone(r)
        self.assertIsNone(t.acquire(recs[1].id, "tok0"))   # bound to its token
        t.release(r)
        now[0] += 3601
        self.assertIsNone(t.acquire(recs[1].id, "tok1"))  # idle expiry
        self.assertEqual(len(t), 0)
        t.close_all()


if __name__ == "__main__":
    unittest.main()
