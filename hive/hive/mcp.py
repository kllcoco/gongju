"""MCP server: McpCore (stdlib JSON-RPC 2.0), stdio loop, Streamable HTTP handler for the hub (design §18, WP6).

``McpCore.handle`` serves one JSON-RPC message: ``initialize`` (3 protocol versions), ``ping``, ``tools/list``,
``tools/call`` (on a pool of daemon threads, so a hung call never keeps the process alive after EOF; the response is
delivered through ``send``), ``notifications/cancelled`` (the ``POST /v1/cancel`` runs off the reader thread),
``logging/setLevel``, empty ``resources/list``/``prompts/list``; batches → -32600, unknown methods → -32601.
``run_stdio`` is ``hive mcp …`` (§18.3): stdout is reserved for the protocol (``sys.stdout`` is pointed at stderr), a
watcher refreshes the Toolkit every 30 s and emits ``notifications/tools/list_changed``.
``http_handler`` (hub ``/mcp``, §18.5) arrives with WP6b.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Dict, List, Optional, Union

from . import __version__
from .errors import HiveError, unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .tools import Toolkit

log = logging.getLogger("hive.mcp")

SUPPORTED_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26"]
PARSE_ERROR, INVALID_REQUEST, METHOD_NOT_FOUND, INVALID_PARAMS, INTERNAL_ERROR = -32700, -32600, -32601, -32602, -32603
STRUCTURED_MAX = 1 << 20
WATCH_INTERVAL = 30.0
WATCH_INTERVAL_ENV = "HIVE_MCP_WATCH_INTERVAL"   # tests only: a shorter watcher interval for `hive mcp`
EOF_WAIT = 2.0
LOG_LEVELS = ("debug", "info", "notice", "warning", "error", "critical", "alert", "emergency")


def _na(what: str) -> HiveError:
    return unsupported(f"mcp.{what} not available yet (WP6b)")


@dataclass
class McpSession:
    """Per-connection MCP state."""
    protocol_version: str = SUPPORTED_VERSIONS[0]
    client_info: dict = field(default_factory=dict)
    initialized: bool = False
    inflight: Dict[Any, Any] = field(default_factory=dict)
    log_level: str = "info"


class CallHandle:
    """One in-flight ``tools/call``: the wire request id and whether the client cancelled it."""

    def __init__(self, request_id: str):
        self.request_id = request_id
        self.cancel = threading.Event()
        self.cancelled = False


class _DaemonPool:
    """A small bounded pool of daemon worker threads (started on demand, at most ``max_workers``).

    Unlike ``ThreadPoolExecutor`` (whose workers concurrent.futures joins at interpreter exit) a call stuck on a
    stalled hub cannot keep ``hive mcp`` alive after stdin EOF (§18.3: wait ≤ 2 s, exit 0)."""

    def __init__(self, max_workers: int, name: str):
        self._max = max(1, int(max_workers))
        self._name = name
        self._q: "collections.deque[Callable[[], None]]" = collections.deque()
        self._cv = threading.Condition()
        self._workers = 0
        self._idle = 0
        self._busy = 0
        self._closed = False

    def submit(self, fn: Callable[[], None]) -> None:
        """Queue ``fn``; RuntimeError after ``shutdown``."""
        with self._cv:
            if self._closed:
                raise RuntimeError("pool is shut down")
            self._q.append(fn)
            if len(self._q) > self._idle and self._workers < self._max:
                self._workers += 1
                threading.Thread(target=self._run, name=f"{self._name}-{self._workers}", daemon=True).start()
            else:
                self._cv.notify()

    def _run(self) -> None:
        while True:
            with self._cv:
                while not self._q and not self._closed:
                    self._idle += 1
                    self._cv.wait()
                    self._idle -= 1
                if not self._q:
                    self._workers -= 1
                    self._cv.notify_all()
                    return
                fn = self._q.popleft()
                self._busy += 1
            try:
                fn()
            except Exception:  # noqa: BLE001 - work() handles its own errors
                log.exception("mcp worker failed")
            finally:
                with self._cv:
                    self._busy -= 1
                    self._cv.notify_all()

    def shutdown(self, wait: float = 0.0) -> bool:
        """Refuse new work and wait ≤ ``wait`` s for queued and running work; True when everything finished."""
        end = time.monotonic() + max(0.0, wait)
        with self._cv:
            self._closed = True
            self._cv.notify_all()
            while self._q or self._busy:
                left = end - time.monotonic()
                if left <= 0:
                    return False
                self._cv.wait(left)
        return True


def _bg(fn: Callable[[], None], name: str) -> None:
    """Run ``fn`` on a daemon thread (for calls that may block on the network)."""
    def run() -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log.debug("%s failed: %s", name, e)
    threading.Thread(target=run, name=name, daemon=True).start()


def _resp(mid: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _err(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


class McpCore:
    """JSON-RPC method handling over a Toolkit (§18.1, §18.2)."""

    def __init__(self, toolkit: "Toolkit", *, server_name: str = "hive", server_title: str = "Hive 蜂巢",
                 version: str = __version__, structured_content: bool = False, max_workers: int = 8):
        self.toolkit = toolkit
        self.server_name = server_name
        self.server_title = server_title
        self.version = version
        self.structured_content = bool(structured_content)
        self.watch = True                       # stdio: refresh + list_changed (HTTP sets False, §18.5)
        self.watch_interval = WATCH_INTERVAL
        self._pool = _DaemonPool(max_workers, "mcp-call")
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher: Optional[threading.Thread] = None
        self._sessions: List[McpSession] = []

    # ------------------------------------------------------------------ dispatch
    def handle(self, msg: Union[dict, list], session: McpSession,
               send: Callable[[dict], None]) -> Optional[Union[dict, list]]:
        """Handle one message; returns the response (None for notifications, responses and pooled tool calls)."""
        if isinstance(msg, list):
            return _err(None, INVALID_REQUEST, "batches not supported")
        if not isinstance(msg, dict):
            return _err(None, INVALID_REQUEST, "invalid request: expected a JSON object")
        method = msg.get("method")
        has_id = "id" in msg
        mid = msg.get("id")
        if method is None:
            if "result" in msg or "error" in msg:
                return None                             # a response from the client (we send no requests)
            if not has_id:
                return None                             # nothing to answer (no id)
            ok_id = mid is None or (isinstance(mid, (str, int, float)) and not isinstance(mid, bool))
            return _err(mid if ok_id else None, INVALID_REQUEST, "invalid request: missing method")
        if not isinstance(method, str) or (has_id and not isinstance(mid, (str, int, float)) and mid is not None):
            return _err(mid if has_id else None, INVALID_REQUEST, "invalid request")
        params = msg.get("params")
        if params is not None and not isinstance(params, dict):
            return _err(mid, INVALID_PARAMS, "params must be an object") if has_id else None
        params = params or {}
        if not has_id:
            self._notification(method, params, session, send)
            return None
        try:
            if method == "initialize":
                return _resp(mid, self._initialize(params, session))
            if method == "ping":
                return _resp(mid, {})
            if method == "tools/list":
                return _resp(mid, {"tools": self.toolkit.mcp_tools(session.protocol_version)})
            if method == "tools/call":
                return self._tools_call(mid, params, session, send)
            if method == "logging/setLevel":
                lvl = str(params.get("level") or "").lower()
                if lvl not in LOG_LEVELS:
                    return _err(mid, INVALID_PARAMS, "unknown log level")
                session.log_level = lvl
                return _resp(mid, {})
            if method == "resources/list":
                return _resp(mid, {"resources": []})
            if method == "prompts/list":
                return _resp(mid, {"prompts": []})
            return _err(mid, METHOD_NOT_FOUND, f"Method not found: {method[:100]}")
        except Exception as e:  # noqa: BLE001
            log.exception("mcp %s failed", method)
            return _err(mid, INTERNAL_ERROR, f"internal error: {type(e).__name__}")

    def _initialize(self, params: dict, session: McpSession) -> dict:
        req = params.get("protocolVersion")
        ver = req if req in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        session.protocol_version = ver
        ci = params.get("clientInfo")
        session.client_info = ci if isinstance(ci, dict) else {}
        info: Dict[str, Any] = {"name": self.server_name, "version": self.version}
        if ver >= "2025-06-18":
            info["title"] = self.server_title
        out: Dict[str, Any] = {"protocolVersion": ver,
                               "capabilities": {"tools": {"listChanged": True}, "logging": {}},
                               "serverInfo": info}
        if ver >= "2025-03-26":
            out["instructions"] = self.toolkit.instructions()
        return out

    def _notification(self, method: str, params: dict, session: McpSession, send: Callable[[dict], None]) -> None:
        if method == "notifications/initialized":
            session.initialized = True
            if self.watch:
                self.start_watcher(send)
        elif method == "notifications/cancelled":
            rid = params.get("requestId")
            with self._lock:
                h = session.inflight.get(rid) if isinstance(rid, (str, int, float)) else None
            if h is not None:
                h.cancelled = True
                h.cancel.set()
                # POST /v1/cancel can take as long as a connect timeout: never on the stdin reader thread
                _bg(lambda: self.toolkit.cancel(h.request_id), "mcp-cancel")

    def _tools_call(self, mid: Any, params: dict, session: McpSession, send: Callable[[dict], None]) -> Optional[dict]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            return _err(mid, INVALID_PARAMS, "missing tool name")
        if self.toolkit.get_tool(name) is None:
            return _err(mid, INVALID_PARAMS, f"Unknown tool: {name[:100]}")
        args = params.get("arguments")
        if args is None:
            args = {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        token = meta.get("progressToken")
        from .client import new_request_id
        h = CallHandle(new_request_id())
        with self._lock:
            session.inflight[mid] = h
        version = session.protocol_version

        def progress(done: float, total: Optional[float], message: str) -> None:
            note: Dict[str, Any] = {"progressToken": token, "progress": done}
            if total is not None:
                note["total"] = total
            if message:
                note["message"] = message
            try:
                send({"jsonrpc": "2.0", "method": "notifications/progress", "params": note})
            except Exception:  # noqa: BLE001
                pass

        use_progress = token is not None and isinstance(token, (str, int)) and \
            name.endswith("fs_transfer")

        def work() -> None:
            before = self._sig()
            try:
                r = self.toolkit.call(name, args, request_id=h.request_id, cancel=h.cancel,
                                      progress=progress if use_progress else None)
                result: Dict[str, Any] = {"content": [{"type": "text", "text": r.text}], "isError": bool(r.is_error)}
                if self.structured_content and version >= "2025-06-18" and isinstance(r.data, dict) and \
                        not r.is_error:
                    try:
                        if len(json.dumps(r.data, ensure_ascii=False, default=str)) <= STRUCTURED_MAX:
                            result["structuredContent"] = r.data
                    except (TypeError, ValueError):
                        pass
                out: dict = _resp(mid, result)
            except Exception as e:  # noqa: BLE001 - Toolkit.call never raises; belt and braces
                log.exception("tool call failed")
                out = _resp(mid, {"content": [{"type": "text", "text": f"ERROR internal: {type(e).__name__}"}],
                                  "isError": True})
            with self._lock:
                session.inflight.pop(mid, None)
            if h.cancelled:
                return
            try:
                send(out)
            except Exception as e:  # noqa: BLE001
                log.debug("send failed: %s", e)
            if self.watch and self._sig() != before:
                try:
                    send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                except Exception:  # noqa: BLE001
                    pass

        try:
            self._pool.submit(work)
        except RuntimeError:
            with self._lock:
                session.inflight.pop(mid, None)
            return _err(mid, INTERNAL_ERROR, "server is shutting down")
        return None

    def _sig(self) -> str:
        return str(getattr(self.toolkit, "_sig", ""))

    # ------------------------------------------------------------------ watcher / lifecycle
    def start_watcher(self, send: Callable[[dict], None]) -> None:
        """Start the node watcher: every ``watch_interval`` s ``refresh()`` → ``notifications/tools/list_changed``."""
        with self._lock:
            if self._watcher is not None:
                return

            def loop() -> None:
                while not self._stop.wait(self.watch_interval):
                    try:
                        changed = self.toolkit.refresh()
                    except Exception as e:  # noqa: BLE001
                        log.debug("refresh failed: %s", e)
                        continue
                    if changed:
                        try:
                            send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
                        except Exception:  # noqa: BLE001
                            pass
            self._watcher = threading.Thread(target=loop, name="mcp-watcher", daemon=True)
            self._watcher.start()

    def close(self, session: Optional[McpSession] = None, wait: float = EOF_WAIT) -> bool:
        """Cancel in-flight calls (the cancel requests run on daemon threads), stop the watcher and wait ≤ ``wait`` s
        for the pool; True when every call finished. Workers are daemon threads, so the caller can exit either way."""
        self._stop.set()
        if session is not None:
            with self._lock:
                handles = list(session.inflight.values())
            for h in handles:
                h.cancelled = True
                h.cancel.set()
                _bg(lambda rid=h.request_id: self.toolkit.cancel(rid), "mcp-cancel")
        return self._pool.shutdown(wait)


# ---------------------------------------------------------------------------------------------------------------
# stdio transport (§18.3)

def serve_stdio(core: McpCore, inp: BinaryIO, out: BinaryIO, *, session: Optional[McpSession] = None) -> int:
    """Read newline-delimited JSON-RPC from ``inp`` and write responses to ``out`` until EOF; returns 0."""
    sess = session or McpSession()
    wlock = threading.Lock()

    def send(msg: dict) -> None:
        data = (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with wlock:
            try:
                out.write(data)
                out.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    while True:
        try:
            line = inp.readline()
        except (OSError, ValueError):
            break
        if not line:
            break
        text = line.strip()
        if not text:
            continue
        try:
            msg = json.loads(text.decode("utf-8") if isinstance(text, bytes) else text)
        except (ValueError, UnicodeDecodeError):
            send(_err(None, PARSE_ERROR, "Parse error"))
            continue
        resp = core.handle(msg, sess, send)
        if resp is not None:
            send(resp)  # type: ignore[arg-type]
    core.close(sess, wait=EOF_WAIT)
    return 0


def _split_globs(values: Optional[List[str]]) -> List[str]:
    out: List[str] = []
    for v in values or []:
        out += [x.strip() for x in str(v).split(",") if x.strip()]
    return out


def build_parser() -> argparse.ArgumentParser:
    """Argument parser of ``hive mcp`` (§18.3)."""
    ap = argparse.ArgumentParser(prog="hive mcp", description="MCP server over stdio (newline-delimited JSON-RPC).")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--connection", help="client.json connection name")
    g.add_argument("--url", help="hub or node URL (with --token-file)")
    g.add_argument("--local", action="store_true", help="attach to this computer's node (this-pc agent token)")
    ap.add_argument("--token-file", help="token file for --url")
    ap.add_argument("--profile", choices=["minimal", "standard", "full"])
    ap.add_argument("--node", help="default node")
    ap.add_argument("--include", action="append", help="tool name globs to include (comma-separated)")
    ap.add_argument("--exclude", action="append", help="tool name globs to exclude (comma-separated)")
    ap.add_argument("--prefix", help="tool name prefix, e.g. hv_")
    ap.add_argument("--wait-budget", type=float, help="seconds one tool call may take (10..280)")
    ap.add_argument("--structured", action="store_true", help="also return structuredContent (2025-06-18+)")
    ap.add_argument("--log", help="log file (default: warnings on stderr)")
    return ap


def _client_from_args(a: argparse.Namespace) -> Any:
    from .client import connect
    if a.local:
        return connect("local")
    if a.url:
        tok = None
        if a.token_file:
            from .client import _read_token_file
            tok = _read_token_file(a.token_file, None)
        return connect(a.url, token=tok)
    if a.token_file:
        raise HiveError("bad_request", "--token-file needs --url")
    return connect(a.connection or None)


def run_stdio(argv: List[str]) -> int:
    """``hive mcp …``."""
    a = build_parser().parse_args(argv)
    proto_out = sys.stdout.buffer
    _tolerant(sys.stderr)              # node text in log lines must not raise UnicodeEncodeError (cp936/cp1252)
    sys.stdout = sys.stderr            # stray prints cannot corrupt the protocol (§18.3)
    if os.name == "nt":  # pragma: no cover - Windows only
        try:
            import msvcrt
            msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)  # type: ignore[attr-defined]
            msvcrt.setmode(proto_out.fileno(), os.O_BINARY)  # type: ignore[attr-defined]
        except (OSError, ValueError, AttributeError):
            pass
    root = logging.getLogger("hive")
    if a.log:
        h: logging.Handler = logging.FileHandler(a.log, encoding="utf-8")
        root.setLevel(logging.INFO)
    else:
        h = logging.StreamHandler(sys.stderr)
        root.setLevel(logging.WARNING)
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(h)
    from .tools import Toolkit, resolve_wait_budget
    try:
        client = _client_from_args(a)
    except HiveError as e:
        sys.stderr.write(f"hive mcp: {e.code}: {e.message}\n" + (f"Hint: {e.hint}\n" if e.hint else ""))
        return 2
    cfg = getattr(client, "config", None)
    tcfg = getattr(cfg, "tools", None)
    profile = a.profile or os.environ.get("HIVE_PROFILE") or getattr(tcfg, "profile", None) or "standard"
    include = _split_globs(a.include) or list(getattr(tcfg, "include", None) or [])
    exclude = _split_globs(a.exclude) or list(getattr(tcfg, "exclude", None) or [])
    prefix = a.prefix if a.prefix is not None else (getattr(tcfg, "prefix", "") or "")
    try:
        tk = Toolkit(client, profile=profile, include=include, exclude=exclude, prefix=prefix,
                     wait_budget=resolve_wait_budget(a.wait_budget, config=cfg, stdio=True),
                     char_budget=int(getattr(tcfg, "char_budget", 20000) or 20000), default_node=a.node)
    except HiveError as e:
        sys.stderr.write(f"hive mcp: {e.code}: {e.message}\n")
        return 2
    core = McpCore(tk, structured_content=bool(a.structured or getattr(tcfg, "structured_content", False)))
    try:
        iv = float(os.environ.get(WATCH_INTERVAL_ENV) or 0)
        if 0 < iv < WATCH_INTERVAL:
            core.watch_interval = iv
    except ValueError:
        pass
    rc = serve_stdio(core, sys.stdin.buffer, proto_out)
    # §18.3 EOF: exit after ≤ 2 s even if some non-daemon thread (e.g. a library's) still runs a stuck request
    if any(t.is_alive() and not t.daemon for t in threading.enumerate() if t is not threading.main_thread()):
        for stream in (proto_out, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        logging.shutdown()
        os._exit(rc)
    return rc


def _tolerant(stream: Any) -> None:
    """``errors='backslashreplace'`` on a text stream when it supports ``reconfigure`` (3.7+)."""
    try:
        stream.reconfigure(errors="backslashreplace")
    except (AttributeError, ValueError, OSError):
        pass


def http_handler(core_factory: Callable[..., McpCore]) -> Callable[..., Any]:
    """Raw route for /mcp (hub only; WP6b)."""
    raise _na("http_handler")


__all__ = ["SUPPORTED_VERSIONS", "McpSession", "McpCore", "CallHandle", "run_stdio", "serve_stdio", "http_handler",
           "build_parser"]
