"""OpenAI-compatible model gateway routes for hub and node (design §8.7, §4.6, D48, WP5).

Routes ``/openai/{node}/{service}/[v1/]{models,chat/completions,completions,embeddings}`` (auth ``models``; a node
also accepts its ``kind:hub`` tokens for the hub's passthrough), OpenAI-shaped errors, CORS for browser apps (Bearer
only, never credentials), a per-token inflight cap, and audit records of kind ``gateway`` that never contain prompts
or replies.

Hub (``HubGatewayBackend``): globs of the ``models`` token are matched against ``<canonical node>/<service>`` after
§8.1 resolution; direct links pass the bytes through (streaming), relay links use the action ``model.forward``
(non-streaming; ``stream:true`` is answered with synthesized SSE chunks, one per upstream choice, then ``[DONE]``).
Node (``NodeGatewayBackend``): ``ServiceManager.endpoint_for(service)`` (or a ``model_endpoints`` entry), vetted with
``check_url(purpose="model")`` and opened with ``netutil.open_stream``; non-2xx and non-SSE bodies are masked.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple

from typing import Protocol, runtime_checkable

from . import catalog
from .errors import HiveError, bad_request, forbidden, not_found, rate_limited, unavailable
from .util import ClosingIterator, glob_match

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .config import GatewayConfig
    from .httpserver import ApiServer, Request
    from .hub import Hub
    from .node import NodeRuntime

log = logging.getLogger("hive.gateway")

PATHS = {"models": "GET", "chat/completions": "POST", "completions": "POST", "embeddings": "POST"}
ROUTE = "/openai/{node}/{service}/{rest*}"
SSE_TYPE = "text/event-stream"
USAGE_SCAN_MAX = 1 << 20     # M2: JSON replies up to 1 MiB passed through in chunks are parsed for the audit usage


@dataclass
class GatewayResponse:
    """Upstream response: exactly one of ``body`` / ``chunks`` is set."""
    status: int
    headers: Dict[str, str]
    body: Optional[bytes]
    chunks: Optional[Iterator[bytes]]


@runtime_checkable
class GatewayBackend(Protocol):
    """Authorize and forward gateway requests."""

    def authorize(self, identity: "Identity", node: str, service: str) -> None:
        """forbidden / not_found."""
        ...

    def forward(self, identity: "Identity", node: str, service: str, method: str, path: str, body: bytes, *,
                stream: bool, request_id: str, cancel: threading.Event) -> GatewayResponse:
        """Forward one request upstream."""
        ...


# ---------------------------------------------------------------------------------------------------------------
# helpers

def upstream_status(value: Any) -> Optional[int]:
    """An upstream (node or pod) status the gateway may pass on: an int in 200..599 other than 204 and 304 (those
    and 1xx carry no body, so a body written after them would be read as the next reply); else None."""
    if isinstance(value, int) and not isinstance(value, bool):
        st = value
    elif isinstance(value, str) and value.strip().isdigit() and len(value.strip()) <= 3:
        st = int(value)
    else:
        return None
    if 200 <= st <= 599 and st not in (204, 304):
        return st
    return None


def _openai_error(status: int, message: str, typ: str, code: str) -> GatewayResponse:
    body = json.dumps({"error": {"message": message, "type": typ, "code": code}}).encode("utf-8")
    return GatewayResponse(status=status, headers={"Content-Type": "application/json"}, body=body, chunks=None)


def _fold_hint(e: HiveError) -> HiveError:
    """OpenAI-shaped errors carry only ``{message,type,code}``: fold the hint into the message so that the §8.7 /
    §7.5 hints ("the hub token on this node needs the net class for the gateway", "the relay role on this pod needs
    the net class …") reach the app."""
    if not e.hint or e.hint in e.message:
        return e
    return HiveError(e.code, f"{e.message} ({e.hint})", e.details or None, hint=e.hint, status=e.status)


def normalize_path(rest: str) -> Optional[str]:
    """``v1/chat/completions`` / ``chat/completions`` → ``chat/completions``; anything else → None."""
    r = (rest or "").strip("/")
    if r.startswith("v1/"):
        r = r[3:]
    return r if r in PATHS else None


def rewrite_stream(body: dict) -> Tuple[dict, bool, bool]:
    """(body with stream false and stream_options removed, stream was requested, include_usage) (§8.7 v5)."""
    b = dict(body)
    stream = b.get("stream") is True
    opts = b.pop("stream_options", None)
    include_usage = bool(isinstance(opts, dict) and opts.get("include_usage"))
    if "stream" in b:
        b["stream"] = False
    return b, stream, include_usage


def synthesize_sse(reply: dict, path: str, include_usage: bool) -> bytes:
    """SSE chunks for a non-streamed upstream reply: one ``data:`` chunk per choice, the optional usage chunk, then
    ``data: [DONE]`` (§8.7 v5 rule)."""
    rid = reply.get("id") or "chatcmpl-hive"
    created = reply.get("created") if isinstance(reply.get("created"), int) else int(time.time())
    model = reply.get("model")
    chat = path == "chat/completions"
    obj = "chat.completion.chunk" if chat else "text_completion"
    out: List[bytes] = []
    for i, ch in enumerate(reply.get("choices") or []):
        if not isinstance(ch, dict):
            continue
        idx = ch.get("index") if isinstance(ch.get("index"), int) else i
        if chat:
            msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
            delta: Dict[str, Any] = {}
            for k in ("role", "content"):
                if k in msg:
                    delta[k] = msg[k]
            if msg.get("tool_calls"):
                delta["tool_calls"] = [dict(tc, index=j) if isinstance(tc, dict) else tc
                                       for j, tc in enumerate(msg["tool_calls"])]
            if msg.get("reasoning_content") is not None:
                delta["reasoning_content"] = msg["reasoning_content"]
            c = {"index": idx, "delta": delta, "finish_reason": ch.get("finish_reason")}
        else:
            c = {"index": idx, "text": ch.get("text", ""), "finish_reason": ch.get("finish_reason")}
            if "logprobs" in ch:
                c["logprobs"] = ch.get("logprobs")
        chunk = {"id": rid, "object": obj, "created": created, "model": model, "choices": [c]}
        out.append(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")
    if include_usage:
        chunk = {"id": rid, "object": obj, "created": created, "model": model, "choices": [],
                 "usage": reply.get("usage")}
        out.append(b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8") + b"\n\n")
    out.append(b"data: [DONE]\n\n")
    return b"".join(out)


def _usage(data: bytes) -> Optional[dict]:
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    u = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(u, dict):
        return None
    return {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens")}


def _sse_usage(tail: bytes) -> Optional[dict]:
    for line in reversed(tail.split(b"\n")):
        line = line.strip()
        if line.startswith(b"data:") and b'"usage"' in line:
            u = _usage(line[5:].strip())
            if u:
                return u
    return None


# ---------------------------------------------------------------------------------------------------------------
# routes

def register_gateway_routes(server: "ApiServer", *, role: str, backend: GatewayBackend,
                            cfg: "GatewayConfig") -> None:
    """Register ``GET/POST /openai/{node}/{service}/{rest*}`` (auth ``models``, OpenAI-style errors, route body limit
    ``cfg.max_body_bytes``, CORS per ``cfg.cors``) and, with CORS, the unauthenticated ``OPTIONS`` preflight."""
    inflight: Dict[str, int] = {}
    lock = threading.Lock()
    cap = int(getattr(cfg, "max_inflight_per_token", 4) or 4)
    audit = getattr(backend, "audit", None)
    where = getattr(backend, "label", role)

    def release(key: str) -> None:
        with lock:
            n = inflight.get(key, 1) - 1
            if n <= 0:
                inflight.pop(key, None)
            else:
                inflight[key] = n

    def record(ident: "Identity", node: str, service: str, path: str, model: Any, stream: bool, status: int,
               bytes_in: int, bytes_out: int, usage: Optional[dict], rid: str, ip: str, ms: int) -> None:
        if audit is None:
            return
        try:
            audit.record(kind="gateway", identity=ident, ip=ip, via=ident.via, node=node, action=None, params=None,
                         ok=200 <= status < 400, code=None if status < 400 else str(status), ms=ms, request_id=rid,
                         extra={"service": service, "path": path, "model": model if isinstance(model, str) else None,
                                "stream": stream, "status": status, "bytes_in": bytes_in, "bytes_out": bytes_out,
                                "usage": usage})
        except HiveError:
            pass

    def handle(req: "Request") -> Tuple[int, Dict[str, str], Any]:
        t0 = time.monotonic()
        ident = req.identity
        node, service = req.query.get("node", ""), req.query.get("service", "")
        path = normalize_path(req.query.get("rest", ""))
        if path is None or PATHS[path] != req.method:
            raise not_found(f"no gateway route {req.method} {req.path}",
                            hint="routes: GET models, POST chat/completions, completions, embeddings")
        body = req.body or b""
        model: Any = None
        stream = False
        if req.method == "POST":
            try:
                obj = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, ValueError, RecursionError):
                obj = None
            if not isinstance(obj, dict):
                raise bad_request("the request body must be a JSON object")
            model = obj.get("model")
            stream = obj.get("stream") is True and path != "embeddings"
        try:
            backend.authorize(ident, node, service)
        except HiveError as e:
            raise _fold_hint(e)
        anode = node
        try:
            anode = getattr(backend, "audit_node", lambda n: n)(node)
        except Exception:  # noqa: BLE001 - audit naming only
            pass
        key = getattr(backend, "inflight_key", lambda i: i.display_name)(ident)
        with lock:
            if inflight.get(key, 0) >= cap:
                raise rate_limited(f"too many gateway requests in flight for this token (max {cap})", retry_after=2,
                                   reason="inflight")
            inflight[key] = inflight.get(key, 0) + 1
        try:
            resp = backend.forward(ident, node, service, req.method, path, body, stream=stream,
                                   request_id=req.request_id, cancel=req.cancel)
        except BaseException as e:
            release(key)
            record(ident, anode, service, path, model, stream, 0, len(body), 0, None, req.request_id, req.client_ip,
                   int((time.monotonic() - t0) * 1000))
            if isinstance(e, HiveError):
                raise _fold_hint(e)
            raise
        headers = {k: v for k, v in (resp.headers or {}).items()}
        if resp.body is not None or resp.chunks is None:
            data = resp.body or b""
            release(key)
            record(ident, anode, service, path, model, stream, resp.status, len(body), len(data),
                   _usage(data) or (_sse_usage(data[-65536:]) if data.startswith(b"data:") else None),
                   req.request_id, req.client_ip, int((time.monotonic() - t0) * 1000))
            return resp.status, headers, data

        # The cleanup lives in ClosingIterator.close(), which also runs when the server closes the stream before the
        # first chunk (client gone while the upstream head was awaited): slot released, upstream closed, audited.
        seen: Dict[str, Any] = {"n": 0, "tail": b"", "head": []}

        def count(src: Iterator[bytes]) -> Iterator[bytes]:
            for chunk in src:
                if not chunk:
                    continue
                seen["n"] += len(chunk)
                seen["tail"] = (seen["tail"] + chunk)[-65536:]
                if seen["head"] is not None:
                    # M2: a JSON reply passed through chunk by chunk (hub direct link) keeps its usage for the audit
                    seen["head"] = seen["head"] + [chunk] if seen["n"] <= USAGE_SCAN_MAX else None
                yield chunk

        def done() -> None:
            release(key)
            usage = _sse_usage(seen["tail"])
            if usage is None and seen["head"] and not seen["head"][0].lstrip().startswith(b"data:"):
                usage = _usage(b"".join(seen["head"]))
            record(ident, anode, service, path, model, stream, resp.status, len(body), seen["n"],
                   usage, req.request_id, req.client_ip, int((time.monotonic() - t0) * 1000))

        upstream = resp.chunks

        def finish() -> None:
            try:
                close = getattr(upstream, "close", None)
                if callable(close):
                    try:
                        close()
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                done()
        return resp.status, headers, ClosingIterator(count(upstream), finish)

    max_body = int(getattr(cfg, "max_body_bytes", 8388608) or 8388608)
    cors = bool(getattr(cfg, "cors", True))
    for method in ("GET", "POST"):
        server.route(method, ROUTE, handle, auth="models", mode="raw", max_body=max_body, error_style="openai",
                     cors=cors)
    if cors:
        server.route("OPTIONS", ROUTE, lambda req: (204, {}, b""), auth="none", mode="raw", error_style="openai",
                     cors=True)
    log.info("model gateway routes registered (%s)", where)


# ---------------------------------------------------------------------------------------------------------------
# node backend

class NodeGatewayBackend(GatewayBackend):
    """Gateway backend of a node (§8.7 "Flow on a node")."""

    def __init__(self, rt: "NodeRuntime"):
        self.rt = rt
        self.cfg = rt.config.gateway
        self.audit = rt.audit
        self.label = f"node:{rt.name}"

    def inflight_key(self, identity: "Identity") -> str:
        """Per gateway token: ``display_name`` (the hub passthrough is ``hub-home/gw/<models token>``)."""
        return f"{identity.display_name}\x00{identity.token_sha256}"

    def audit_node(self, node: str) -> str:
        """M2: audit rows name this node, not the URL segment ("self")."""
        return self.rt.name

    def _own(self, node: str) -> None:
        n = (node or "").lower()
        if n not in ("self", self.rt.name.lower()) and not (self.rt.node_id and n == self.rt.node_id.lower()):
            raise not_found(f"this is node {self.rt.name}, not {node[:64]!r}", hint="use /openai/self/<service>/v1")

    def authorize(self, identity: "Identity", node: str, service: str) -> None:
        """models token with a matching service glob, or the hub token; then the policy check of model.forward."""
        self._own(node)
        if identity.kind == "models":
            if not glob_match(service, identity.models):
                raise forbidden(f"this gateway token may not use service {service!r}")
        elif identity.kind != "hub":
            raise forbidden("use a gateway token: hive gateway token add")
        spec = catalog.get("model.forward")
        try:
            self.rt.effective(identity).check_action(spec)
        except HiveError as e:
            if e.code == "forbidden" and identity.kind == "hub":
                raise forbidden(e.message, hint="the hub token on this node needs the net class for the gateway")
            raise

    def _known(self, identity: "Identity") -> List[str]:
        names: List[str] = []
        try:
            names += [r.get("name") for r in self.rt.services.endpoints() if r.get("name")]
        except Exception:  # noqa: BLE001
            pass
        names += [str(e.get("name")) for e in (self.rt.config.model_endpoints or []) if e.get("name")]
        names = sorted(set(names))
        if identity.kind == "models":
            names = [n for n in names if glob_match(n, identity.models)]
        return names

    def _endpoint(self, service: str, identity: "Identity") -> Tuple[str, str, Any]:
        try:
            return self.rt.services.endpoint_for(service)
        except HiveError as e:
            if e.code == "not_found" and "has no endpoint" in e.message:
                raise not_found(f"service {service} has no OpenAI endpoint",
                                hint="define it with a preset or set endpoint")
            if e.code not in ("not_found", "unsupported"):
                raise
        for ep in self.rt.config.model_endpoints or []:
            if ep.get("name") == service and ep.get("base_url"):
                auth = None
                if ep.get("api_key_env"):
                    auth = self.rt.secrets.resolve_auth(str(ep["api_key_env"]))
                return str(ep["base_url"]), "openai", auth
        known = self._known(identity)
        raise not_found(f"service {service} not found on {self.rt.name}" +
                        (f" (services: {', '.join(known)})" if known else ""))

    def forward(self, identity: "Identity", node: str, service: str, method: str, path: str, body: bytes, *,
                stream: bool, request_id: str, cancel: threading.Event) -> GatewayResponse:
        """Open the upstream model server and stream its answer back."""
        from . import netutil
        base, api, auth = self._endpoint(service, identity)
        base = base.rstrip("/")
        if api == "ollama" and not base.endswith("/v1"):
            base += "/v1"
        url = base + "/" + path
        eff = self.rt.effective(identity)
        headers = {"Accept": "*/*"}
        if method == "POST":
            headers["Content-Type"] = "application/json"
        cfg = self.cfg
        status, rh, chunks = netutil.open_stream(method, url, guard=eff, purpose="model", headers=headers,
                                                 body=body if method == "POST" else None, connect_timeout=10,
                                                 idle_timeout=float(cfg.idle_timeout), auth=auth)
        if upstream_status(status) is None:
            _close(chunks)
            return _openai_error(502, f"the upstream model server answered HTTP {status}", "upstream_error",
                                 "upstream_error")
        ctype = ""
        for k, v in rh.items():
            if k.lower() == "content-type":
                ctype = v
        out_headers = {"Content-Type": ctype or "application/json"}
        limit = int(cfg.max_response_bytes)
        end = time.monotonic() + float(cfg.timeout)

        def bounded() -> Iterator[bytes]:
            n = 0
            for c in chunks:
                if cancel.is_set() or time.monotonic() > end:
                    return
                n += len(c)
                if n > limit:
                    return
                yield c
        # ClosingIterator: the upstream stream is closed even when the stream is closed before its first chunk
        stream_out = ClosingIterator(bounded(), getattr(chunks, "close", None))
        if 200 <= status < 300 and ctype.split(";", 1)[0].strip().lower() == SSE_TYPE:
            return GatewayResponse(status=status, headers=out_headers, body=None, chunks=stream_out)
        try:
            data = b"".join(stream_out)
        finally:
            stream_out.close()
        mask = getattr(self.rt.secrets, "mask", None) if self.rt.secrets is not None else None
        if callable(mask) and getattr(self.rt.secrets, "has_values", False):
            text = data.decode("utf-8", "surrogateescape")
            m = mask(text)
            if m != text:
                data = m.encode("utf-8", "surrogateescape")
        return GatewayResponse(status=status, headers=out_headers, body=data, chunks=None)


# ---------------------------------------------------------------------------------------------------------------
# hub backend

NODE_404_MAX = 65536   # bytes of a direct node's 404 body read to tell its own not-found from an upstream 404


def _close(chunks: Any) -> None:
    close = getattr(chunks, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001
            pass


def _is_node_unknown_service(data: bytes) -> bool:
    """True for the node gateway's own OpenAI-shaped "service X not found on N (services: …)" 404."""
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return False
    err = obj.get("error") if isinstance(obj, dict) else None
    if not isinstance(err, dict) or err.get("code") != "not_found":
        return False
    msg = err.get("message")
    return isinstance(msg, str) and msg.startswith("service ") and " not found on " in msg

class HubGatewayBackend(GatewayBackend):
    """Gateway backend of the hub (§8.7 "Flow on the hub")."""

    def __init__(self, hub: "Hub"):
        self.hub = hub
        self.cfg = hub.cfg.gateway
        self.audit = hub.audit
        self.label = f"hub:{hub.name}"

    def inflight_key(self, identity: "Identity") -> str:
        """Per gateway token."""
        return f"{identity.name}\x00{identity.token_sha256}"

    def audit_node(self, node: str) -> str:
        """M2: audit rows name the canonical node (an alias resolves, §8.1); unknown names as given."""
        link = self.hub.resolve(node)
        return link.name if link is not None else node

    def _pairs(self, identity: "Identity") -> List[str]:
        return [g for g in identity.models]

    def _nodes_matching(self, identity: "Identity") -> List[str]:
        out = []
        for name in self.hub.links:
            if any(glob_match(name.lower(), [g.split("/", 1)[0]]) for g in identity.models if "/" in g):
                out.append(name)
        return sorted(out)

    def authorize(self, identity: "Identity", node: str, service: str) -> None:
        """Resolve the node (§8.1), then match ``<canonical node>/<service>`` against the token's globs."""
        if identity.kind != "models":
            raise forbidden("use a gateway token: hive gateway token add")
        link = self.hub.resolve(node)
        if link is None:
            import difflib
            cands = self._nodes_matching(identity)
            sugg = difflib.get_close_matches((node or "").lower(), [c.lower() for c in cands], n=3, cutoff=0.6)
            raise not_found(f"node {node[:64]!r} not found" + (f" (did you mean {', '.join(sugg)}?)" if sugg else ""))
        if not glob_match(f"{link.name.lower()}/{service}", identity.models):
            raise forbidden(f"this gateway token may not use {node[:64]}/{service}")

    def _not_found(self, identity: "Identity", link_name: str, service: str) -> GatewayResponse:
        pairs = [g for g in identity.models if glob_match(link_name.lower(), [g.split("/", 1)[0]])]
        msg = f"service {service} not found on {link_name}"
        if pairs:
            msg += f" (services: {', '.join(sorted(pairs))})"
        return _openai_error(404, msg, "not_found_error", "not_found")

    def _direct_404(self, identity: "Identity", link_name: str, service: str,
                    resp: GatewayResponse) -> GatewayResponse:
        """§8.7: only the node gateway's own unknown-service 404 (which lists services the app's globs may not cover)
        is replaced by the hub's filtered 404; an upstream model server's 404 (wrong model id, unsupported path) is
        passed through unchanged."""
        if resp.chunks is None:
            data = resp.body or b""
            rest: Optional[Iterator[bytes]] = None
        else:
            buf: List[bytes] = []
            size = 0
            it = iter(resp.chunks)
            ended = False
            try:
                while size <= NODE_404_MAX:
                    try:
                        c = next(it)
                    except StopIteration:
                        ended = True
                        break
                    buf.append(c)
                    size += len(c)
            except BaseException:
                _close(resp.chunks)
                raise
            data = b"".join(buf)
            if ended:
                _close(resp.chunks)
                rest = None
            else:
                rest = it
        if rest is None and _is_node_unknown_service(data):
            return self._not_found(identity, link_name, service)
        if rest is None:
            return GatewayResponse(status=resp.status, headers=resp.headers, body=data, chunks=None)
        src = resp.chunks

        def again() -> Iterator[bytes]:
            yield data
            for c in rest:  # type: ignore[union-attr]
                yield c
        return GatewayResponse(status=resp.status, headers=resp.headers, body=None,
                               chunks=ClosingIterator(again(), getattr(src, "close", None)))

    def forward(self, identity: "Identity", node: str, service: str, method: str, path: str, body: bytes, *,
                stream: bool, request_id: str, cancel: threading.Event) -> GatewayResponse:
        """Direct links: passthrough; relay links: ``model.forward`` (+ SSE synthesis)."""
        from .hub import DirectLink, RelayLink
        link = self.hub.resolve(node)
        assert link is not None
        if isinstance(link, DirectLink):
            if not link.online():
                try:
                    link.ensure_fresh()
                except HiveError:
                    raise unavailable(f"node {link.name} is offline", reason="offline", maybe_executed=False)
            resp = link.open_gateway(service, method, path, body, agent=f"gw/{identity.name}",
                                     timeout=float(self.cfg.timeout), cancel=cancel)
            if resp.status == 404:
                return self._direct_404(identity, link.name, service, resp)
            return resp
        assert isinstance(link, RelayLink)
        params: Dict[str, Any] = {"service": service, "path": path, "timeout": float(self.cfg.timeout)}
        include_usage = False
        want_stream = False
        if method == "POST":
            try:
                obj = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                raise bad_request("the request body must be a JSON object")
            if path != "embeddings":
                obj, want_stream, include_usage = rewrite_stream(obj)
            params["body"] = json.dumps(obj, ensure_ascii=False)
        env = self.hub.call(identity, "model.forward", params, node=link.name, request_id=request_id,
                            cancel=cancel)
        if not env.get("ok"):
            err = HiveError.from_dict(env.get("error"))
            if err.code == "not_found" and "does not implement" not in err.message and "not found" in err.message:
                return self._not_found(identity, link.name, service)
            if err.code == "unavailable" and (err.details or {}).get("reason") == "offline":
                raise unavailable(f"node {link.name} is offline", reason="offline", maybe_executed=False)
            raise err
        r = env.get("result") or {}
        if r.get("truncated"):
            return _openai_error(502, "reply larger than the relay limit; lower max_tokens or use a direct link",
                                 "upstream_error", "relay_truncated")
        status = upstream_status(r.get("status"))
        if status is None:
            # Phase 2 review: the pod chooses this value; a 1xx/204/304 or out-of-range status would make the hub
            # write a body clients do not read (response splitting on keep-alive connections)
            return _openai_error(502, "the upstream model server sent an invalid HTTP status", "upstream_error",
                                 "upstream_error")
        text = r.get("body") if isinstance(r.get("body"), str) else ""
        data = text.encode("utf-8")
        ctype = r.get("content_type") if isinstance(r.get("content_type"), str) else "application/json"
        if status == 404 and path != "models":
            pass
        if 200 <= status < 300 and want_stream and path in ("chat/completions", "completions"):
            try:
                reply = json.loads(text)
            except ValueError:
                reply = None
            if isinstance(reply, dict):
                return GatewayResponse(status=status, headers={"Content-Type": SSE_TYPE},
                                       body=synthesize_sse(reply, path, include_usage), chunks=None)
        return GatewayResponse(status=status, headers={"Content-Type": ctype or "application/json"}, body=data,
                               chunks=None)


__all__ = ["GatewayResponse", "GatewayBackend", "HubGatewayBackend", "NodeGatewayBackend", "register_gateway_routes",
           "synthesize_sse", "rewrite_stream", "normalize_path"]
