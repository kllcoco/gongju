"""Shared helpers for the M2 end-to-end tests (§21.2 E2E 1-3, gateway; §22.1 step 5).

* ``Supervisor``: runs a real ``hive … run`` daemon in a loop that relaunches it on exit code 75 (a requested
  restart), like the watchdog / ``start-hive.sh`` loop and the Windows launcher (§13.1, D55), so ``hive node
  restart`` / ``hive hub restart`` / a relay ``refresh:"restart"`` work against foreground test daemons.
* ``STUB_OPENAI``: a tiny OpenAI-compatible model server (``/health``, ``/v1/models``, chat with and without SSE
  streaming, completions, embeddings); argv: ``PORT [MODEL]``.
* small HTTP helpers that never use a proxy.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

IS_WINDOWS = os.name == "nt"
ROOT = Path(__file__).resolve().parents[2]

# variables that would point the CLI/MCP at another server or route loopback through a proxy
SCRUB = ("HIVE_URL", "HIVE_TOKEN", "HIVE_TOKEN_FILE", "HIVE_CONNECTION", "HIVE_NODE", "HIVE_PROXY", "HIVE_TLS_PIN",
         "HIVE_MCP_WATCH_INTERVAL", "HIVE_JOIN", "HIVE_RELAY_PROXY", "HIVE_HF_ENDPOINT", "HIVE_WATCHDOG",
         "HIVE_RELAY_ROLE", "HIVE_TOPOLOGY", "HIVE_WAIT_BUDGET",
         "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
         "RUNPOD_POD_ID", "VAST_CONTAINERLABEL", "AutoDLContainerUUID")

STUB_OPENAI = textwrap.dedent("""\
    import json, sys, time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    port = int(sys.argv[1])
    model = sys.argv[2] if len(sys.argv) > 2 else "stub-model"

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _send(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _chunk(self, data):
            self.wfile.write(b"%x\\r\\n%s\\r\\n" % (len(data), data))
            self.wfile.flush()

        def do_GET(self):
            path = self.path.split("?")[0].rstrip("/")
            if path == "/health":
                self._send(200, {"status": "ok"})
            elif path in ("/v1/models", "/models"):
                self._send(200, {"object": "list", "data": [{"id": model, "object": "model", "owned_by": "stub"}]})
            else:
                self._send(404, {"error": {"message": "no route " + path}})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n) or b"{}")
            path = self.path.split("?")[0]
            seen_auth = self.headers.get("Authorization") or ""
            if path.endswith("/embeddings"):
                return self._send(200, {"object": "list", "model": model,
                                        "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2]}],
                                        "usage": {"prompt_tokens": 1, "total_tokens": 1}})
            if "stream_options" in req and not req.get("stream"):    # vLLM behaviour
                return self._send(400, {"error": {"message": "stream_options needs stream", "type": "BadRequest"}})
            msgs = req.get("messages") or [{}]
            last = str(msgs[-1].get("content", req.get("prompt", "")))
            text = "pong: " + last
            if seen_auth:
                text += " [auth:" + seen_auth[:12] + "]"
            if last == "fail400":
                return self._send(400, {"error": {"message": "upstream says no", "type": "invalid_request_error",
                                                  "code": "stub_400"}})
            if last.startswith("slow"):
                time.sleep(3)
            if path.endswith("/completions") and not path.endswith("/chat/completions"):
                return self._send(200, {"id": "t1", "object": "text_completion", "created": 1, "model": model,
                                        "choices": [{"index": 0, "text": text, "finish_reason": "stop"}],
                                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
            if last == "tools" and not req.get("stream"):
                calls = [{"id": "call_%d" % i, "type": "function",
                          "function": {"name": "f%d" % i, "arguments": json.dumps({"i": i})}} for i in range(2)]
                return self._send(200, {"id": "c2", "object": "chat.completion", "created": 1, "model": model,
                                        "choices": [{"index": 0, "finish_reason": "tool_calls",
                                                     "message": {"role": "assistant", "content": None,
                                                                 "reasoning_content": "thinking", "tool_calls": calls}}],
                                        "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})
            n = int(req.get("n") or 1)
            if n > 1 and not req.get("stream"):
                return self._send(200, {"id": "c3", "object": "chat.completion", "created": 1, "model": model,
                                        "choices": [{"index": i, "finish_reason": "stop",
                                                     "message": {"role": "assistant", "content": "%s #%d" % (text, i)}}
                                                    for i in range(n)],
                                        "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7}})
            if req.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                for i, w in enumerate(text.split(" ")):
                    ch = {"id": "c1", "object": "chat.completion.chunk", "created": 1, "model": model,
                          "choices": [{"index": 0, "delta": {"content": w if i == 0 else " " + w},
                                       "finish_reason": None}]}
                    self._chunk(("data: " + json.dumps(ch) + "\\n\\n").encode())
                    time.sleep(0.4)
                self._chunk(b"data: [DONE]\\n\\n")
                self.wfile.write(b"0\\r\\n\\r\\n")
                self.wfile.flush()
                return
            self._send(200, {"id": "c1", "object": "chat.completion", "created": 1, "model": model,
                             "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}})

        def log_message(self, *a):
            pass

    print("stub listening on", port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
""")

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http(method: str, url: str, *, token: Optional[str] = None, body: Any = None, headers: Optional[dict] = None,
         timeout: float = 60) -> Any:
    """urllib request without a proxy; returns the response (or the HTTPError, which has the same API)."""
    h = dict(headers or {})
    data = None
    if token:
        h["Authorization"] = "Bearer " + token
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        return _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        return e


def http_json(method: str, url: str, **kw: Any) -> "tuple[int, Any]":
    r = http(method, url, **kw)
    try:
        raw = r.read()
    finally:
        r.close()
    try:
        return r.status if hasattr(r, "status") else r.code, json.loads(raw.decode("utf-8") or "null")
    except ValueError:
        return r.status if hasattr(r, "status") else r.code, raw


def wait_for(fn: Any, timeout: float = 30, interval: float = 0.2, what: str = "condition") -> Any:
    end = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < end:
        try:
            last = fn()
            if last:
                return last
        except Exception as e:  # noqa: BLE001 - keep polling
            last = e
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


class Supervisor:
    """``argv`` in a loop: relaunch at once on exit 75, stop on any other exit code (the watchdog rule, D55)."""

    def __init__(self, argv: Sequence[str], *, env: Dict[str, str], cwd: str, log: Path, name: str = "daemon"):
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.log_path = log
        self.name = name
        self.exits: List[int] = []
        self.starts = 0
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name=f"sup-{name}", daemon=True)

    def start(self) -> "Supervisor":
        self._thread.start()
        return self

    def _loop(self) -> None:
        with open(str(self.log_path), "ab") as logf:
            while True:
                with self._lock:
                    if self._stop:
                        return
                    self.proc = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
                                                 stdout=logf, stderr=subprocess.STDOUT)
                    self.starts += 1
                rc = self.proc.wait()
                self.exits.append(rc)
                logf.write(f"[supervisor] {self.name} exited {rc}\n".encode())
                logf.flush()
                if rc != 75:
                    return

    @property
    def running(self) -> bool:
        p = self.proc
        return self._thread.is_alive() and p is not None and p.poll() is None

    def pid(self) -> Optional[int]:
        p = self.proc
        return p.pid if p is not None and p.poll() is None else None

    def wait(self, timeout: float = 30) -> bool:
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def kill(self) -> None:
        with self._lock:
            self._stop = True
            p = self.proc
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=15)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(timeout=10)
        self._thread.join(20)

    def output(self, n: int = 4000) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")[-n:]
        except OSError:
            return ""


def run_cli(argv: Sequence[str], *, env: Dict[str, str], cwd: Optional[str] = None, timeout: float = 120,
            prefix: Optional[List[str]] = None, stdin: Optional[str] = None) -> "subprocess.CompletedProcess[str]":
    """Run the real CLI (``python -m hive …`` or ``prefix + argv``) and return text output."""
    cmd = (prefix or [sys.executable, "-m", "hive"]) + list(argv)
    r = subprocess.run(cmd, cwd=cwd or str(ROOT), env=env, input=None if stdin is None else stdin.encode(),
                       stdin=subprocess.DEVNULL if stdin is None else None,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    out = r.stdout.decode("utf-8", "replace")
    err = r.stderr.decode("utf-8", "replace")
    if os.environ.get("HIVE_TEST_SHOW"):
        print(f"\n$ hive {' '.join(argv)}  -> {r.returncode}\n{out}{err}")
    return subprocess.CompletedProcess(r.args, r.returncode, out, err)


def base_env(**over: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB}
    env.update(over)
    return env


def allow_root(cfg_path: Path) -> None:
    """CI containers run as root; a home user's node/hub refuses that unless the owner opts in (§13.1)."""
    if IS_WINDOWS or os.geteuid() != 0:
        return
    raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    raw["allow_root"] = True
    cfg_path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    os.chmod(str(cfg_path), 0o600)


def edit_json(path: Path, fn: Any) -> None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    fn(raw)
    path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    if not IS_WINDOWS:
        os.chmod(str(path), 0o600)


def sse_lines(resp: Any) -> List[tuple]:
    """[(seconds since start, line)] for every non-empty line of a streamed response."""
    t0 = time.monotonic()
    out = []
    for raw in resp:
        line = raw.decode("utf-8").strip()
        if line:
            out.append((time.monotonic() - t0, line))
    return out
