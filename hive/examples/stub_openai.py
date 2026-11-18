#!/usr/bin/env python3
"""A fake OpenAI-compatible model server for trying Hive's model path end to end (no GPU, no download).

    python3 stub_openai.py [PORT] [--model NAME] [--host HOST]

Serves GET /health, GET /v1/models, POST /v1/chat/completions (with ``"stream": true`` as server-sent events) and
POST /v1/completions. Every answer echoes the last user message ("stub reply: ..."). Standard library only, Python
3.9+. It listens on 127.0.0.1 (this computer only) unless --host says otherwise. Stop it with Ctrl-C (or
``hive service stop`` / ``service_stop`` when Hive runs it as a managed service).

README 6.1 deploys it as a managed service:

    hive push desk examples/stub_openai.py hive-test/stub_openai.py
    hive service define stub --argv 'python3 stub_openai.py 8000' --cwd ~/hive-test --port 8000 \
        --endpoint http://127.0.0.1:8000/v1 --node desk
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

MAX_BODY = 1 << 20     # 1 MiB of request JSON is plenty for a test


def _last_user_text(messages: Any) -> str:
    """Text of the last message (string content, or the text parts of a content list)."""
    if not isinstance(messages, list) or not messages:
        return ""
    content = messages[-1].get("content") if isinstance(messages[-1], dict) else None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""


def make_handler(model: str) -> type:
    """A request handler class serving ``model``."""

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "hive-stub-openai/1"

        # -------------------------------------------------------------------------------------------- helpers
        def _send_json(self, status: int, obj: Dict[str, Any]) -> None:
            data = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, message: str) -> None:
            self._send_json(status, {"error": {"message": message, "type": "invalid_request_error"}})

        def _path(self) -> str:
            return self.path.split("?", 1)[0].rstrip("/") or "/"

        def _body(self) -> Optional[Dict[str, Any]]:
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if n < 0 or n > MAX_BODY:
                self._error(413 if n > MAX_BODY else 400, "bad Content-Length")
                return None
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                self._error(400, "the body is not JSON")
                return None
            if not isinstance(body, dict):
                self._error(400, "the body must be a JSON object")
                return None
            return body

        # -------------------------------------------------------------------------------------------- routes
        def do_GET(self) -> None:  # noqa: N802 - http.server naming
            path = self._path()
            if path == "/health":
                self._send_json(200, {"status": "ok"})
            elif path in ("/v1/models", "/models"):
                self._send_json(200, {"object": "list", "data": [
                    {"id": model, "object": "model", "created": 0, "owned_by": "hive-stub"}]})
            else:
                self._error(404, f"no route {path}")

        def do_POST(self) -> None:  # noqa: N802 - http.server naming
            path = self._path()
            if path not in ("/v1/chat/completions", "/chat/completions", "/v1/completions", "/completions"):
                self._error(404, f"no route {path}")
                return
            body = self._body()
            if body is None:
                return
            chat = path.endswith("/chat/completions")
            prompt = _last_user_text(body.get("messages")) if chat else str(body.get("prompt") or "")
            text = "stub reply: " + (prompt or "(empty)")
            name = body.get("model") if isinstance(body.get("model"), str) and body.get("model") else model
            created = int(time.time())
            usage = {"prompt_tokens": len(prompt.split()), "completion_tokens": len(text.split()),
                     "total_tokens": len(prompt.split()) + len(text.split())}
            if body.get("stream"):
                self._stream(text, name, created, chat, usage if (body.get("stream_options") or {}).get(
                    "include_usage") else None)
                return
            if chat:
                choice: Dict[str, Any] = {"index": 0, "message": {"role": "assistant", "content": text},
                                          "finish_reason": "stop"}
                obj = "chat.completion"
            else:
                choice = {"index": 0, "text": text, "finish_reason": "stop"}
                obj = "text_completion"
            self._send_json(200, {"id": f"stub-{created}", "object": obj, "created": created, "model": name,
                                  "choices": [choice], "usage": usage})

        def _stream(self, text: str, name: str, created: int, chat: bool, usage: Optional[dict]) -> None:
            """Server-sent events, one word per chunk, then ``data: [DONE]`` (chunked transfer encoding)."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            words: List[str] = text.split(" ")
            events: List[Dict[str, Any]] = []
            for i, w in enumerate(words):
                piece = w if i == 0 else " " + w
                if chat:
                    delta: Dict[str, Any] = {"content": piece}
                    if i == 0:
                        delta["role"] = "assistant"
                    choice: Dict[str, Any] = {"index": 0, "delta": delta, "finish_reason": None}
                else:
                    choice = {"index": 0, "text": piece, "finish_reason": None}
                events.append({"id": f"stub-{created}", "object": "chat.completion.chunk" if chat else
                               "text_completion", "created": created, "model": name, "choices": [choice]})
            last = {"index": 0, "finish_reason": "stop"}
            last.update({"delta": {}} if chat else {"text": ""})
            events.append({"id": f"stub-{created}", "object": "chat.completion.chunk" if chat else "text_completion",
                           "created": created, "model": name, "choices": [last]})
            if usage is not None:
                events.append({"id": f"stub-{created}", "object": "chat.completion.chunk" if chat else
                               "text_completion", "created": created, "model": name, "choices": [], "usage": usage})
            try:
                for ev in events:
                    self._chunk(("data: " + json.dumps(ev) + "\n\n").encode("utf-8"))
                    time.sleep(0.05)       # a visible "typing" effect in chat apps
                self._chunk(b"data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass                       # the client went away mid-stream

        def _chunk(self, data: bytes) -> None:
            self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            self.wfile.flush()

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("stub: " + (fmt % args) + "\n")

    return Handler


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="fake OpenAI-compatible server for testing Hive")
    ap.add_argument("port", nargs="?", type=int, default=8000, help="port to listen on (default 8000)")
    ap.add_argument("--model", default="stub-model", help="model id it reports (default stub-model)")
    ap.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    args = ap.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(args.model))
    server.daemon_threads = True
    print(f"stub OpenAI-compatible server on http://{args.host}:{server.server_address[1]}/v1 "
          f"(model {args.model}); Ctrl-C stops it", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
