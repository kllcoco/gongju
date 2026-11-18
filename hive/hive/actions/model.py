"""Action handlers: model.endpoints, model.chat (WP3a), model.forward (gateway relay path) and model.pull (WP3b)
(design §9.9).

Every HTTP request goes through ``netutil.guarded_request`` with ``guard=ctx.policy`` (NetGuard + rule 5a/5c for
``purpose="model"``); ``api_key_env`` credentials come only from a service definition or the owner's
``model_endpoints`` (``ServiceManager.endpoint_for``), never from the agent. Handlers register with ``@handler(name)``
and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import fsops, hfhub, netutil, util
from ..errors import (HiveError, bad_request, conflict, denied, forbidden, internal, not_found, rate_limited,
                      timeout, too_large, unavailable, unsupported)
from . import ActionContext, handler

PROBE_PORTS = (11434, 8000, 8080, 30000, 1234, 5000, 7860)
PROBE_TIMEOUT = 0.5
KNOWN_TIMEOUT = 2.0
REASONING_CAP = 2000
FORBIDDEN_EXTRA = ("model", "messages", "stream")
OWNED_BY_SERVER = {"vllm": "vllm", "llamacpp": "llamacpp", "llama.cpp": "llamacpp", "sglang": "sglang"}


# ---------------------------------------------------------------------------------------------------------------
# helpers

def parse_base_url(url: str, what: str = "base_url") -> str:
    """``http(s)://host[:port][/path]`` with no userinfo, query or fragment; returned without a trailing ``/``."""
    if not isinstance(url, str) or not url.strip():
        raise bad_request(f"{what} is empty")
    u = url.strip()
    try:
        parts = urlsplit(u)
        _port = parts.port
    except ValueError:
        raise bad_request(f"{what} is not a valid URL: {u[:200]!r}", hint="e.g. http://127.0.0.1:8000/v1")
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise bad_request(f"{what} must be http(s)://host[:port][/path]", hint="e.g. http://127.0.0.1:8000/v1")
    if "@" in parts.netloc or parts.query or parts.fragment or "?" in u or "#" in u:
        raise bad_request(f"{what} may not contain credentials, a query or a fragment",
                          hint="e.g. http://127.0.0.1:8000/v1; keys go in a node secret (service api_key_env)")
    _ = _port
    return u.rstrip("/")


def _join(base: str, path: str) -> str:
    """Segment-wise join (``…/v1`` + ``chat/completions``)."""
    return base.rstrip("/") + "/" + path.lstrip("/")


def _norm_base(u: str) -> str:
    try:
        p = urlsplit(u)
        port = p.port or (443 if p.scheme == "https" else 80)
        return f"{p.scheme.lower()}://{(p.hostname or '').lower()}:{port}{p.path.rstrip('/')}"
    except ValueError:
        return u


def _port_of(u: str) -> Optional[int]:
    try:
        p = urlsplit(u)
        return p.port or (443 if p.scheme == "https" else 80)
    except ValueError:
        return None


def _host_port(u: str) -> str:
    try:
        p = urlsplit(u)
        return f"{p.hostname}:{p.port or (443 if p.scheme == 'https' else 80)}"
    except ValueError:
        return u


def _services(ctx: ActionContext) -> Any:
    sm = getattr(ctx.rt, "services", None)
    return sm if sm is not None and hasattr(sm, "endpoint_for") else None


def _endpoint_for(ctx: ActionContext, name: str) -> Tuple[str, str, Optional[netutil.AuthSpec]]:
    """(base_url, api, auth) of a managed service, else of a ``model_endpoints`` entry (§10.5)."""
    sm = _services(ctx)
    if sm is not None:
        try:
            return sm.endpoint_for(name)
        except HiveError as e:
            if e.code != "unsupported":
                raise
    for e in getattr(ctx.policy, "model_endpoints", None) or []:
        if e.get("name") == name and e.get("base_url"):
            auth = ctx.policy.auth_spec(str(e["api_key_env"])) if e.get("api_key_env") else None
            return str(e["base_url"]), "openai", auth
    raise not_found(f"model service '{name}' not found",
                    hint="define one with service_define (e.g. preset=vllm) or ask the owner to add model_endpoints")


def _get(ctx: ActionContext, url: str, *, timeout: float, auth: Optional[netutil.AuthSpec] = None,
         max_bytes: int = 4 << 20) -> netutil.HttpResult:
    return netutil.guarded_request("GET", url, guard=ctx.policy, purpose="model", timeout=timeout,
                                   max_bytes=max_bytes, follow_redirects=False, auth=auth, cancel=ctx.cancel)


def _json(body: bytes) -> Any:
    try:
        return json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None


def _models_of(obj: Any) -> List[Dict[str, Any]]:
    """``[{"id","owned_by"}]`` from an OpenAI ``/models`` reply or an Ollama ``/api/tags`` reply."""
    out: List[Dict[str, Any]] = []
    if isinstance(obj, dict) and isinstance(obj.get("data"), list):
        for m in obj["data"]:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                ob = m.get("owned_by")
                out.append({"id": m["id"][:300], "owned_by": ob[:100] if isinstance(ob, str) else None})
    elif isinstance(obj, dict) and isinstance(obj.get("models"), list):
        for m in obj["models"]:
            if isinstance(m, dict):
                mid = m.get("name") or m.get("model")
                if isinstance(mid, str):
                    out.append({"id": mid[:300], "owned_by": None})
    return out[:500]


def _server_guess(base: str, models: Optional[List[Dict[str, Any]]], answered: bool) -> str:
    port = _port_of(base)
    if port == 11434:
        return "ollama"
    if port == 1234:
        return "lmstudio"
    if models:
        ob = str(models[0].get("owned_by") or "").lower()
        if ob in OWNED_BY_SERVER:
            return OWNED_BY_SERVER[ob]
    return "openai" if answered else "unknown"


def _is_loopback_url(u: str) -> bool:
    try:
        return util.is_loopback_host(urlsplit(u).hostname or "")
    except ValueError:
        return False


def _probe(ctx: ActionContext, row: Dict[str, Any], timeout: float, auth: Optional[netutil.AuthSpec]) -> None:
    """Fill ``healthy``/``models``/``server`` of a row from ``GET <base>/models`` (in place; never raises)."""
    answered = False
    models: Optional[List[Dict[str, Any]]] = None
    reported = row.get("healthy")
    try:
        r = _get(ctx, _join(row["base_url"], "models"), timeout=timeout, auth=auth)
        answered = True
        if 200 <= r.status < 300:
            models = _models_of(_json(r.body))
            row["healthy"] = True
        elif r.status in (401, 403) and row.get("source") == "service" and reported is not None:
            row["healthy"] = bool(reported)   # up, but refused the model list: keep ServiceManager's health check
        else:
            row["healthy"] = False
    except HiveError:
        row["healthy"] = False
    row["models"] = [m["id"] for m in models] if models is not None else None
    row["_answered"] = answered and row["healthy"]
    if row.get("server") in (None, "", "unknown", "openai"):
        row["server"] = _server_guess(row["base_url"], models, bool(row["_answered"]))


def list_endpoints(ctx: ActionContext, *, probe: bool, timeout: float) -> List[Dict[str, Any]]:
    """model.endpoints rows: managed services, then config ``model_endpoints``, then loopback port probes
    (deduplicated by base URL; a config name wins over a probe)."""
    rows: List[Dict[str, Any]] = []
    seen = set()
    auths: Dict[int, Optional[netutil.AuthSpec]] = {}

    def add(row: Dict[str, Any], auth: Optional[netutil.AuthSpec] = None) -> None:
        key = _norm_base(row["base_url"])
        if key in seen:
            return
        seen.add(key)
        auths[id(row)] = auth
        rows.append(row)

    sm = _services(ctx)
    if sm is not None:
        try:
            for r in sm.endpoints() or []:
                sauth: Optional[netutil.AuthSpec] = None
                if r.get("name"):
                    try:                          # the service's api_key_env (§10.5): probe like model.chat calls
                        sauth = sm.endpoint_for(str(r["name"]))[2]
                    except HiveError:
                        sauth = None
                add({"name": r.get("name"), "source": "service", "api": r.get("api") or "openai",
                     "server": r.get("server") or "unknown", "base_url": str(r.get("base_url")),
                     "healthy": r.get("healthy"), "models": r.get("models")}, sauth)
        except HiveError:
            pass
    for e in getattr(ctx.policy, "model_endpoints", None) or []:
        if not e.get("base_url"):
            continue
        auth = None
        if e.get("api_key_env"):
            try:
                auth = ctx.policy.auth_spec(str(e["api_key_env"]))
            except HiveError:
                auth = None
        add({"name": e.get("name"), "source": "config", "api": "openai", "server": "unknown",
             "base_url": str(e["base_url"]).rstrip("/"), "healthy": None, "models": None}, auth)
    probes: List[Dict[str, Any]] = []
    if probe:
        for port in PROBE_PORTS:
            row = {"name": None, "source": "probe", "api": "ollama" if port == 11434 else "openai",
                   "server": "unknown", "base_url": f"http://127.0.0.1:{port}/v1", "healthy": None, "models": None}
            if _norm_base(row["base_url"]) not in seen:
                probes.append(row)
        jobs: List[Tuple[Dict[str, Any], float, Optional[netutil.AuthSpec]]] = []
        for row in rows:
            if row["source"] == "service" or _is_loopback_url(row["base_url"]):
                jobs.append((row, min(KNOWN_TIMEOUT, timeout), auths.get(id(row))))
        jobs += [(row, min(PROBE_TIMEOUT, timeout), None) for row in probes]
        threads = [threading.Thread(target=_probe, args=(ctx,) + j, name="hive-model-probe", daemon=True)
                   for j in jobs]
        for t in threads:
            t.start()
        end = time.monotonic() + timeout + 1.0
        for t in threads:
            t.join(max(0.0, end - time.monotonic()))
        for row in probes:
            if row.get("_answered"):
                add(row)
    else:
        for row in rows:
            if row.get("server") in (None, "", "unknown"):
                row["server"] = _server_guess(row["base_url"], None, False)
    for row in rows:
        row.pop("_answered", None)
    return rows


def _audit_route(ctx: ActionContext, r: netutil.HttpResult) -> None:
    """§5.4 NetGuard rule 8: audit ``extra`` records ``route`` and ``dns`` (local|remote)."""
    ctx.audit_extra.update(route=r.route, dns=getattr(r, "dns", "local"))


def _connect_hint(base: str) -> str:
    return f"nothing listening on {_host_port(base)}; check service_list / service_logs"


def _reraise_connect(e: HiveError, base: str) -> HiveError:
    if e.code == "unavailable" and e.details.get("reason") == "connect" and e.details.get("maybe_executed") is False:
        return unavailable(f"cannot connect to the model server at {base}", reason="connect", maybe_executed=False,
                           hint=_connect_hint(base))
    return e


# ---------------------------------------------------------------------------------------------------------------
# model.endpoints

@handler("model.endpoints")
def model_endpoints(ctx: ActionContext, p: dict) -> dict:
    """Known and probed OpenAI-compatible endpoints; with service/port: that endpoint's model list."""
    timeout = float(p.get("timeout", 10))
    timeout = max(0.5, min(timeout, ctx.remaining() - 1.0))
    service, port = p.get("service"), p.get("port")
    if service is None and port is None:
        return {"endpoints": list_endpoints(ctx, probe=bool(p.get("probe", True)), timeout=timeout)}
    if service is not None and port is not None:
        raise bad_request("give only one of service, port")
    auth: Optional[netutil.AuthSpec] = None
    if service is not None:
        base, api, auth = _endpoint_for(ctx, str(service))
        managed = False
        sm = _services(ctx)
        if sm is not None:
            try:
                managed = any(r.get("name") == service for r in sm.endpoints() or [])
            except HiveError:
                managed = False
        row = {"name": service, "source": "service" if managed else "config", "api": api, "server": "unknown",
               "base_url": base.rstrip("/"), "healthy": None, "models": None}
        if managed:
            for r in sm.endpoints() or []:
                if r.get("name") == service and r.get("server"):
                    row["server"] = r["server"]
    else:
        base = f"http://127.0.0.1:{int(port)}/v1"
        api = "ollama" if int(port) == 11434 else "openai"
        row = {"name": None, "source": "probe", "api": api, "server": "unknown", "base_url": base, "healthy": None,
               "models": None}
    models: Optional[List[Dict[str, Any]]] = None
    err: Optional[HiveError] = None
    status: Optional[int] = None
    try:
        r = _get(ctx, _join(row["base_url"], "models"), timeout=timeout, auth=auth)
        status = r.status
        _audit_route(ctx, r)
        if 200 <= r.status < 300:
            models = _models_of(_json(r.body))
    except HiveError as e:
        err = e
    if models is None and api == "ollama":
        root = row["base_url"][:-3] if row["base_url"].endswith("/v1") else row["base_url"]
        try:
            r = _get(ctx, _join(root, "api/tags"), timeout=timeout, auth=auth)
            status = r.status
            if 200 <= r.status < 300:
                models, err = _models_of(_json(r.body)), None
        except HiveError as e:
            err = err or e
    if models is None:
        if err is not None:
            raise _reraise_connect(err, row["base_url"])
        raise unavailable(f"the model server at {row['base_url']} answered HTTP {status}", reason="upstream",
                          maybe_executed=False, status=status, hint="check service_status / service_logs")
    row["healthy"] = True
    row["models"] = [m["id"] for m in models]
    if row["server"] in ("unknown", "openai"):
        row["server"] = _server_guess(row["base_url"], models, True)
    return {"endpoints": [row], "models": models}


# ---------------------------------------------------------------------------------------------------------------
# model.chat

def _messages(p: dict) -> List[Dict[str, str]]:
    msgs: List[Dict[str, str]] = []
    if p.get("system"):
        msgs.append({"role": "system", "content": str(p["system"])})
    for m in p.get("messages") or []:
        msgs.append({"role": str(m["role"]), "content": str(m["content"])})
    if p.get("prompt"):
        msgs.append({"role": "user", "content": str(p["prompt"])})
    if not msgs:
        raise bad_request("give messages or prompt")
    return msgs


def _extra(p: dict) -> Dict[str, Any]:
    raw = p.get("extra_json")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except ValueError as e:
        raise bad_request(f"extra_json is not valid JSON: {e}")
    if not isinstance(obj, dict):
        raise bad_request("extra_json must be a JSON object")
    if not fsops.json_depth_ok(obj, 64):
        raise bad_request("extra_json nests deeper than 64 levels")
    bad = [k for k in FORBIDDEN_EXTRA if k in obj]
    if bad:
        raise bad_request(f"extra_json may not set {', '.join(bad)}", hint="use the model/messages parameters; "
                                                                           "streaming is not supported here")
    return obj


def _resolve_target(ctx: ActionContext, p: dict) -> Tuple[str, str, Optional[netutil.AuthSpec]]:
    given = [k for k in ("service", "port", "base_url") if p.get(k) is not None]
    if len(given) > 1:
        raise bad_request("give only one of service, port, base_url")
    if p.get("service") is not None:
        return _endpoint_for(ctx, str(p["service"]))
    if p.get("port") is not None:
        port = int(p["port"])
        return f"http://127.0.0.1:{port}/v1", "ollama" if port == 11434 else "openai", None
    if p.get("base_url") is not None:
        return parse_base_url(str(p["base_url"])), "openai", None
    rows = [r for r in list_endpoints(ctx, probe=True, timeout=min(5.0, max(0.5, ctx.remaining() - 2.0)))
            if r.get("healthy") is True]
    if len(rows) != 1:
        cands = ", ".join(f"{r.get('name') or r['base_url']}" for r in rows) or "none found"
        raise bad_request(f"cannot pick a model endpoint automatically ({len(rows)} healthy: {cands})",
                          hint="give service, port or base_url (model_endpoints lists them)",
                          candidates=[r["base_url"] for r in rows][:20])
    row = rows[0]
    if row["source"] in ("service", "config") and row.get("name"):
        return _endpoint_for(ctx, str(row["name"]))
    return row["base_url"], row.get("api") or "openai", None


@handler("model.chat")
def model_chat(ctx: ActionContext, p: dict) -> dict:
    """One non-streaming chat completion against a local or allowed OpenAI-compatible server."""
    extra = _extra(p)
    msgs = _messages(p)
    base, api, auth = _resolve_target(ctx, p)
    if api == "ollama" and not urlsplit(base).path.rstrip("/").endswith("/v1"):
        base = _join(base, "v1")
    base = base.rstrip("/")
    budget = max(1.0, min(float(p.get("timeout", 120)), ctx.remaining() - 1.0))
    t_start = time.monotonic()
    model = p.get("model")
    if not model:
        try:
            r = _get(ctx, _join(base, "models"), timeout=min(budget, 30.0), auth=auth)
        except HiveError as e:
            raise _reraise_connect(e, base)
        ids = _models_of(_json(r.body)) if 200 <= r.status < 300 else []
        if not ids:
            raise bad_request(f"no model id given and {base}/models listed none", hint="pass model=…",
                              status=r.status)
        model = ids[0]["id"]
    body: Dict[str, Any] = {"model": model, "messages": msgs, "max_tokens": int(p.get("max_tokens", 512)),
                            "stream": False}
    for k in ("temperature", "top_p", "stop"):
        if p.get(k) is not None:
            body[k] = p[k]
    body.update(extra)
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
    left = max(1.0, budget - (time.monotonic() - t_start))
    try:
        r = netutil.guarded_request("POST", _join(base, "chat/completions"), guard=ctx.policy, purpose="model",
                                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                                    body=payload, timeout=left, max_bytes=int(ctx.policy.limits.max_response_bytes),
                                    follow_redirects=False, auth=auth, cancel=ctx.cancel)
    except HiveError as e:
        raise _reraise_connect(e, base)
    latency = round(time.monotonic() - t_start, 3)
    ctx.audit_extra.update(model=str(model)[:200], base_url=base, status=r.status)
    _audit_route(ctx, r)
    if r.status == 404:
        known: List[str] = []
        try:
            lr = _get(ctx, _join(base, "models"), timeout=min(5.0, max(0.5, ctx.remaining() - 1.0)), auth=auth)
            known = [m["id"] for m in _models_of(_json(lr.body))] if 200 <= lr.status < 300 else []
        except HiveError:
            known = []
        raise not_found(f"model '{model}' was not found on {base}", models=known[:50],
                        hint=("available models: " + ", ".join(known[:20])) if known else
                        "check the model id with model_endpoints")
    if r.status >= 400:
        excerpt = r.body[:500].decode("utf-8", "replace")
        raise unavailable(f"the model server answered HTTP {r.status}", reason="upstream", maybe_executed=True,
                          status=r.status, body=excerpt, hint="check service_logs on the node")
    data = _json(r.body)
    if not isinstance(data, dict) or not isinstance(data.get("choices"), list) or not data["choices"]:
        raise unavailable("the model server sent an invalid reply" + (" (truncated)" if r.truncated else ""),
                          reason="upstream", maybe_executed=True, status=r.status,
                          body=r.body[:300].decode("utf-8", "replace"))
    choice = data["choices"][0] if isinstance(data["choices"][0], dict) else {}
    msg = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, list):   # content parts
        content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
    if content is None:
        content = choice.get("text") if isinstance(choice.get("text"), str) else ""
    content = str(content)
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    reasoning = util.truncate_text(str(reasoning), REASONING_CAP, head_ratio=0.75)[0] if reasoning else None
    max_chars = int(p.get("max_chars", 8000))
    if len(content) > max_chars:
        content = util.truncate_text(content, max_chars, head_ratio=0.75)[0]
    usage = None
    u = data.get("usage")
    if isinstance(u, dict):
        usage = {"prompt_tokens": u.get("prompt_tokens"), "completion_tokens": u.get("completion_tokens")}
    tps = None
    if usage and isinstance(usage.get("completion_tokens"), (int, float)) and latency > 0:
        tps = round(float(usage["completion_tokens"]) / latency, 2)
    if usage:
        ctx.audit_extra["usage"] = usage
    fr = choice.get("finish_reason")
    return {"content": content, "reasoning": reasoning, "finish_reason": fr if isinstance(fr, str) else None,
            "model": str(data.get("model") or model), "usage": usage, "latency_s": latency, "tokens_per_s": tps,
            "base_url": base}


# ---------------------------------------------------------------------------------------------------------------
# model.forward (gateway relay path, §8.7 / §9.9)

FORWARD_PATHS = ("models", "chat/completions", "completions", "embeddings")


@handler("model.forward")
def model_forward(ctx: ActionContext, p: dict) -> dict:
    """Forward one OpenAI-compatible call to a model service on this node (never streams)."""
    service, path = str(p["service"]), str(p["path"])
    if path not in FORWARD_PATHS:
        raise bad_request(f"path must be one of {', '.join(FORWARD_PATHS)}")
    lim = ctx.policy.limits
    payload: Optional[bytes] = None
    obj: Optional[Dict[str, Any]] = None
    wanted_stream = False
    raw = p.get("body")
    if path != "models":
        if raw is None or not str(raw).strip():
            raise bad_request(f"body is required for {path}", hint="send the request object as JSON text")
        data = str(raw).encode("utf-8")
        cap = int(getattr(lim, "gateway_max_body_bytes", 8388608))
        if len(data) > cap:
            raise too_large(f"request body is {len(data)} bytes; the limit is {cap}",
                            hint="shorten the request (gateway.max_body_bytes)", limit=cap)
        try:
            obj = json.loads(data.decode("utf-8"))
        except ValueError as e:
            raise bad_request(f"body is not valid JSON: {e}")
        if not isinstance(obj, dict):
            raise bad_request("body must be a JSON object")
        if not fsops.json_depth_ok(obj, 64):
            raise bad_request("body nests deeper than 64 levels")
        wanted_stream = bool(obj.get("stream"))
        if wanted_stream:
            obj["stream"] = False            # SSE synthesis is the gateway's job (§8.7)
        obj.pop("stream_options", None)      # vLLM/SGLang reject it without streaming (v5)
        payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    try:
        base, api, auth = _endpoint_for(ctx, service)
    except HiveError as e:
        if e.code == "not_found":
            raise not_found(e.message, hint="define the service with a preset or set endpoint",
                            **{k: v for k, v in e.details.items() if k != "hint"})
        raise
    if api == "ollama" and not urlsplit(base).path.rstrip("/").endswith("/v1"):
        base = _join(base, "v1")
    base = base.rstrip("/")
    url = _join(base, path)
    max_bytes = max(1024, min(int(getattr(lim, "gateway_max_response_bytes", 67108864)),
                              int(getattr(lim, "max_result_bytes", 33554432)) - 65536))
    budget = max(1.0, min(float(p.get("timeout", 300)), ctx.remaining() - 1.0))
    method = "GET" if path == "models" else "POST"
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    try:
        r = netutil.guarded_request(method, url, guard=ctx.policy, purpose="model", headers=headers, body=payload,
                                    timeout=budget, max_bytes=max_bytes, follow_redirects=False, auth=auth,
                                    cancel=ctx.cancel)
    except HiveError as e:
        if e.code == "unavailable" and e.details.get("reason") == "connect" and e.details.get("maybe_executed") \
                is False:
            raise unavailable(f"cannot connect to the model server at {base}", reason="connect",
                              maybe_executed=False, hint=f"nothing listening on {base}; check service_status")
        raise
    _audit_route(ctx, r)
    ctx.audit_extra.update(service=service, path=path, status=r.status, bytes_out=len(r.body))
    if obj is not None:
        if obj.get("model") is not None:
            ctx.audit_extra["model"] = str(obj.get("model"))[:200]
        ctx.audit_extra["stream"] = wanted_stream
    if not r.truncated and 200 <= r.status < 300 and path != "models":
        reply = _json(r.body)
        if isinstance(reply, dict) and isinstance(reply.get("usage"), dict):
            u = reply["usage"]
            ctx.audit_extra["usage"] = {k: u.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                        if k in u}
    ct = None
    for k, v in r.headers.items():
        if k.lower() == "content-type":
            ct = str(v)
    return {"status": r.status, "content_type": ct, "body": r.body.decode("utf-8", "replace"),
            "truncated": bool(r.truncated), "elapsed_s": r.elapsed_s}


# ---------------------------------------------------------------------------------------------------------------
# model.pull (D36, §9.9 / §11.3 pull mode / §11.5)

_PULL_LOCK = threading.Lock()        # v5: busy check + start_job in one step
REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}\Z")
PICKLE_EXT = (".bin", ".pt", ".pth", ".ckpt", ".pkl", ".pickle")
GIB = 1 << 30
AUTO_TOKENS = {"huggingface": ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"), "modelscope": ("MODELSCOPE_API_TOKEN",)}
INCLUDE_HINT = "use include, e.g. ['*.safetensors','*.json','tokenizer*']"
MANIFEST = ".hive-pull.json"
LOCK_NAME = ".hive-pull.lock"
PART_SUFFIX = ".hive-part"
LISTING_HINT = "listing still in progress; call again (cached) or narrow include"


def _node_name(ctx: ActionContext) -> str:
    return str(getattr(ctx.rt, "name", None) or "the node")


def _models_cfg(ctx: ActionContext) -> Any:
    cfg = getattr(getattr(ctx.rt, "config", None), "models", None)
    if cfg is not None:
        return cfg
    return {"hf_endpoint": getattr(getattr(ctx.policy, "p", None), "hf_endpoint", None)}


def _unbound_hint(ctx: ActionContext, name: str, host: str) -> str:
    return (f"{name} exists on {_node_name(ctx)} but is not bound to {host}: hive node secret set {name} --hosts "
            f"+{host} (pods: HIVE_SECRET_HOSTS_{name}=+{host})")


def _pull_auth(ctx: ActionContext, source: str, endpoint: str, auth_env: str
               ) -> Tuple[Optional[netutil.AuthSpec], Optional[str]]:
    """(token AuthSpec or None, unbound note) per the ``auth_env`` rules of §9.9."""
    u = urlsplit(endpoint)
    host, scheme = (u.hostname or "").lower(), (u.scheme or "https").lower()
    if auth_env == "none":
        return None, None
    if auth_env == "auto":
        note = None
        for name in AUTO_TOKENS[source]:
            try:
                spec = ctx.policy.auth_spec(name)
            except HiveError:
                continue
            if netutil.auth_host_allowed(spec.hosts, host, scheme):
                return spec, None
            note = note or _unbound_hint(ctx, name, host)
        return None, note
    spec = ctx.policy.auth_spec(auth_env)
    if not netutil.auth_host_allowed(spec.hosts, host, scheme):
        raise denied(f"secret '{auth_env}' is not bound to {host}", rule="secret_host",
                     hint=f"hive node secret set {auth_env} --hosts +{host}")
    return spec, None


def _pkey(ctx: ActionContext, path: Any) -> str:
    try:
        return ctx.policy.path_key(str(path))
    except Exception:  # noqa: BLE001
        return util.path_key(os.path.realpath(str(path)), casefold=os.name == "nt")


def _under(key: str, base: str) -> bool:
    return key == base or key.startswith(base.rstrip("/") + "/")


def _refuse_target(ctx: ActionContext, target: Path) -> None:
    """Target folder refused when it is a filesystem root, home, the workdir, an fs root, the data dir, or an
    ancestor of any of these (§9.9 step 1)."""
    real = os.path.realpath(str(target))
    if os.path.dirname(real) == real:
        raise denied("cannot pull into a filesystem or drive root", rule="protected_path",
                     hint="pick a folder below the root, e.g. ~/models/<name>")
    key = _pkey(ctx, real)
    prot: List[Tuple[str, str]] = [(os.path.expanduser("~"), "the home folder")]
    if getattr(ctx.policy, "workdir", None) is not None:
        prot.append((str(ctx.policy.workdir), "the node workdir"))
    if getattr(ctx.rt, "data_dir", None) is not None:
        prot.append((str(ctx.rt.data_dir), "the node data folder"))
    try:
        for r in ctx.policy.roots():
            if r.get("path") not in (None, "*"):
                prot.append((os.path.expanduser(str(r["path"])), "an fs root"))
    except Exception:  # noqa: BLE001
        pass
    for path, label in prot:
        if _under(_pkey(ctx, path), key):
            raise denied(f"cannot pull into '{real}': it is (or contains) {label}", rule="protected_path",
                         hint="pick a dedicated folder, e.g. ~/models/<Org>--<Name>")


def _busy(ctx: ActionContext, target: Path, key: str) -> Optional[str]:
    """Job id of a running pull into ``target`` (folder lock holder or job tag), else None."""
    h = util.FileLock(str(target / LOCK_NAME)).holder()
    if h is not None:
        return str(h.get("note") or "") or "unknown"
    jm = getattr(ctx.rt, "jobs", None)
    if jm is not None and hasattr(jm, "list"):
        try:
            rows = jm.list(state="running", limit=10000).get("jobs") or []
        except Exception:  # noqa: BLE001
            rows = []
        for row in rows:
            if row.get("kind") != "pull":
                continue
            try:
                tags = jm.record(row["job_id"]).get("tags") or []
            except Exception:  # noqa: BLE001
                tags = []
            if f"target:{key}" in tags:
                return str(row["job_id"])
    return None


def _check_busy(ctx: ActionContext, target: Path, key: str) -> None:
    jid = _busy(ctx, target, key)
    if jid:
        raise conflict("another pull into this folder is running", job_id=jid,
                       hint=f"follow it with job_status({jid!r}) or stop it with job_stop")


def _check_cap(ctx: ActionContext) -> None:
    jm = getattr(ctx.rt, "jobs", None)
    cap = int(getattr(ctx.policy.limits, "max_concurrent_pulls", 2) or 2)
    if jm is not None and hasattr(jm, "running_count") and jm.running_count("pull") >= cap:
        raise rate_limited(f"{cap} model pulls are already running on this node", retry_after=30.0,
                           reason="pulls_running", hint="wait for a running pull to finish (job_list)")


def _glob(path: str, pats: List[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, g) for g in pats)


def _free(path: Path) -> Tuple[int, str]:
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return int(shutil.disk_usage(str(p)).free), str(p)
    except OSError:
        return 0, str(p)


def _is_wsl() -> bool:
    from .. import netproxy
    return netproxy._is_wsl()


def _plan_files(ctx: ActionContext, p: dict, files: List[hfhub.RepoFile]
                ) -> Tuple[List[hfhub.RepoFile], List[str], List[str]]:
    """Name check, include/exclude, prefer_safetensors (§9.9 step 4)."""
    include = [str(g) for g in p.get("include") or []]
    exclude = [str(g) for g in p.get("exclude") or []]
    invalid: List[str] = []
    kept: List[hfhub.RepoFile] = []
    for f in files:
        if util.safe_remote_name(f.path) != f.path:
            invalid.append(f.path[:300])
            continue
        if include and not _glob(f.path, include):
            continue
        if exclude and _glob(f.path, exclude):
            continue
        kept.append(f)
    pickle: List[str] = []
    if p.get("prefer_safetensors", True) and any(f.path.lower().endswith(".safetensors") for f in kept):
        out = []
        for f in kept:
            if f.path.lower().endswith(PICKLE_EXT) and not (include and _glob(f.path, include)
                                                             and any(g != "*" for g in include)):
                pickle.append(f.path)
                continue
            out.append(f)
        kept = out
    return kept, invalid[:1000], pickle[:1000]


def _expected(f: hfhub.RepoFile) -> Optional[str]:
    if f.sha256:
        return "sha256:" + f.sha256
    if f.git_sha1:
        return "sha1:" + f.git_sha1
    return None


def _start_pull_job(ctx: ActionContext, spec: dict, name: str, tags: List[str]) -> dict:
    jm = getattr(ctx.rt, "jobs", None)
    if jm is None or not hasattr(jm, "start_job"):
        raise internal("the job manager is not available on this node")
    argv = util.self_command() + ["_download", "--spec", "{run_dir}/download.json"]
    try:
        env = ctx.policy.child_env({})
    except HiveError:
        env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith("HIVE_")]:
        env.pop(k, None)
    info = jm.start_job(ctx, kind="pull", argv_or_cmdline=argv, shell="none", cwd=Path(str(ctx.policy.workdir)),
                        env=env, stdin=None, name=name[:64], timeout=0, merge_stderr=True, log_max_bytes=4 << 20,
                        log_keep=1, tags=tags, command_display="hive _download (pull)",
                        extra_files={"download.json": util.canonical_json(spec).encode("utf-8")})
    if info.get("state") == "failed":
        raise internal(f"the downloader could not start: {info.get('error') or 'unknown error'}",
                       job_id=info.get("job_id"))
    return info


@handler("model.pull")
def model_pull(ctx: ActionContext, p: dict) -> dict:
    """Plan (and unless dry_run, start) a commit-pinned repository snapshot download."""
    source = str(p.get("source") or "huggingface")
    repo_type = str(p.get("repo_type") or "model")
    if source == "modelscope" and repo_type == "dataset":
        raise unsupported("datasets cannot be pulled from ModelScope", hint="use source='huggingface'")
    repo, url_rev = hfhub.normalize_repo(str(p["repo"]), source)
    revision = p.get("revision") or url_rev or ("master" if source == "modelscope" else "main")
    revision = str(revision)
    if not REVISION_RE.match(revision) or ".." in revision or "//" in revision:
        raise bad_request(f"invalid revision {revision[:100]!r}", hint="a branch, tag or commit, e.g. main")
    dry_run = bool(p.get("dry_run"))
    endpoint = hfhub.endpoint_for(source, _models_cfg(ctx), os.environ)
    ep_host = (urlsplit(endpoint).hostname or "").lower()
    auth, note = _pull_auth(ctx, source, endpoint, str(p.get("auth_env") or "auto"))
    if not dry_run:
        _check_cap(ctx)
    # target folder
    if p.get("path"):
        raw_target = str(p["path"])
    else:
        mdir = getattr(_models_cfg(ctx), "dir", None) if not isinstance(_models_cfg(ctx), dict) else None
        raw_target = os.path.join(os.path.expanduser(str(mdir or "~/models")), repo.replace("/", "--"))
    try:
        target = ctx.policy.resolve_path(raw_target, write=True)
    except HiveError as e:
        if not p.get("path") and e.code == "policy_denied":
            raise denied(f"the default models folder is not writable here: {e.message}", rule=e.details.get("rule"),
                         hint="give path=… inside a writable root (models.dir must lie inside an rw root)")
        raise
    if target.exists() and not target.is_dir():
        raise conflict(f"'{target}' exists and is not a folder", hint="pick another path")
    _refuse_target(ctx, target)
    tkey = _pkey(ctx, target)
    _check_busy(ctx, target, tkey)
    # 2. listing (bounded by the call deadline; cached 10 min)
    deadline = time.monotonic() + max(0.5, ctx.remaining() - 3.0)
    try:
        commit, listed = hfhub.list_repo(source, endpoint, repo, repo_type, revision, guard=ctx.policy, auth=auth,
                                         deadline=deadline)
    except HiveError as e:
        if e.code == "timeout":
            raise timeout(f"listing {repo} did not finish in time", maybe_executed=False, hint=LISTING_HINT)
        details = {k: v for k, v in e.details.items() if k != "hint"}
        if e.code == "forbidden" and e.details.get("gated"):
            raise forbidden(e.message, hint=note or (
                f"gated model: accept the license at https://huggingface.co/{repo} with the account whose token is "
                f"on the node, then ask the owner to run `hive node secret set HF_TOKEN` on {_node_name(ctx)}"),
                **details)
        if e.code in ("forbidden", "not_found") and note and auth is None:
            raise HiveError(e.code, e.message, details, hint=note)
        raise
    ctx.audit_extra.update(repo=repo, source=source, commit=commit, endpoint_host=ep_host)
    # 4. names and filters
    planned, invalid, pickle = _plan_files(ctx, p, listed)
    if len(planned) > int(p.get("max_files", 1000)):
        raise too_large(f"{len(planned)} files match (max_files {int(p.get('max_files', 1000))})", hint=INCLUDE_HINT,
                        files=len(planned))
    # 5. existing target
    manifest: Dict[str, Any] = {}
    mpath = target / MANIFEST
    if mpath.is_file():
        try:
            m = util.read_json(mpath, None)
            manifest = m if isinstance(m, dict) else {}
        except HiveError:
            manifest = {}
    same = manifest.get("source") == source and manifest.get("repo") == repo
    mfiles = manifest.get("files") if same and isinstance(manifest.get("files"), dict) else {}
    overwrite = bool(p.get("overwrite"))
    if target.is_dir() and not same and not overwrite:
        try:
            entries = [e for e in os.listdir(str(target)) if e not in (LOCK_NAME,)]
        except OSError:
            entries = []
        if entries:
            raise conflict(f"'{target}' is not empty and holds no pull of {repo}",
                           hint="pick an empty folder, or pass overwrite=true", entries=sorted(entries)[:10])
    real_target = os.path.realpath(str(target))
    unsafe: List[str] = []
    differ: List[str] = []
    rows: List[Dict[str, Any]] = []
    total = 0
    to_download = 0
    plan: List[hfhub.RepoFile] = []
    for f in planned:
        total += f.size
        dest = Path(real_target).joinpath(*f.path.split("/"))
        try:
            parent_real = os.path.realpath(str(dest.parent))
            inside = os.path.commonpath([os.path.normcase(parent_real), os.path.normcase(real_target)]) \
                == os.path.normcase(real_target)
        except ValueError:
            inside = False
        unsafe_why = not inside or dest.is_symlink()
        if not unsafe_why:
            try:
                ctx.policy.resolve_path(str(dest), write=True)
            except HiveError:
                unsafe_why = True
        if unsafe_why:
            unsafe.append(f.path)
            continue
        done = False
        part = Path(str(dest) + PART_SUFFIX)
        if dest.exists():
            size = dest.stat().st_size
            rec = mfiles.get(f.path) if isinstance(mfiles, dict) else None
            if size == f.size and isinstance(rec, dict) and rec.get("hash") == _expected(f) \
                    and int(rec.get("size", -1)) == size:
                done = True
            elif size != f.size and not isinstance(rec, dict) and not overwrite:
                differ.append(f.path)
        if not done:
            have = part.stat().st_size if part.is_file() and not part.is_symlink() else 0
            to_download += max(0, f.size - min(have, f.size))
        plan.append(f)
        rows.append({"path": f.path, "size": f.size, "done": done})
    if differ:
        raise conflict(f"{len(differ)} existing file(s) in '{target}' differ from the repository",
                       hint="pass overwrite=true to replace them, or pick another path", paths=differ[:10])
    free, free_at = _free(target)
    need = int(to_download * 1.02) + GIB
    fits = free >= need
    if dry_run:
        rows.sort(key=lambda r: -int(r["size"]))
        out: Dict[str, Any] = {"repo": repo, "source": source, "endpoint": endpoint, "revision": revision,
                               "commit": commit, "path": str(target), "file_count": len(plan), "total_bytes": total,
                               "to_download_bytes": to_download, "disk_free": free, "fits": fits,
                               "files": rows[:200], "skipped_invalid": invalid, "skipped_pickle": pickle}
        notes = []
        if note:
            notes.append(note)
        if unsafe:
            notes.append(f"{len(unsafe)} file(s) would be skipped (unsafe destination): {', '.join(unsafe[:5])}")
        if notes:
            out["note"] = "; ".join(notes)
        return out
    if not fits:
        details: Dict[str, Any] = {"need": need, "free": free, "path": free_at}
        if _is_wsl():
            details["note"] = "free space is the WSL virtual disk; check the Windows drive too"
        raise too_large(f"not enough disk space: need {util.human_bytes(need)}, {util.human_bytes(free)} free",
                        hint="exclude files (e.g. include=['*.json','*.safetensors','tokenizer*']) or pick a path on "
                             "a bigger disk", **details)
    spec = {"v": 1, "mode": "pull", "net": _pull_guard_dict(ctx),
            "auth": ({"name": auth.name, "header": auth.header, "value": auth.value, "hosts": list(auth.hosts)}
                     if auth is not None else None),
            "source": source, "endpoint": endpoint, "repo": repo, "repo_type": repo_type, "revision": revision,
            "commit": commit, "target": str(target),
            "files": [{"path": f.path, "size": f.size, "sha256": f.sha256, "git_sha1": f.git_sha1} for f in plan],
            "max_parallel": int(p.get("max_parallel", 4)), "verify": str(p.get("verify") or "fast"),
            "overwrite": overwrite, "lock_file": str(target / LOCK_NAME),
            "skipped_invalid": invalid, "skipped_pickle": pickle, "skipped_unsafe": unsafe}
    with _PULL_LOCK:
        _check_cap(ctx)
        _check_busy(ctx, target, tkey)
        target.mkdir(parents=True, exist_ok=True)
        info = _start_pull_job(ctx, spec, f"pull {repo}", [f"target:{tkey}"])
    ctx.audit_extra.update(job_id=info.get("job_id"), path=str(target))
    return {"job_id": info["job_id"], "path": str(target), "commit": commit, "file_count": len(plan),
            "total_bytes": total, "to_download_bytes": to_download, "state": "running"}


def _pull_guard_dict(ctx: ActionContext) -> dict:
    ng = getattr(ctx.policy, "net_guard", None)
    to_dict = getattr(ng, "to_dict", None)
    if not callable(to_dict):
        raise internal("the node's network guard cannot be handed to the downloader")
    return to_dict()


__all__ = ["model_endpoints", "model_chat", "model_forward", "model_pull", "list_endpoints", "parse_base_url"]
