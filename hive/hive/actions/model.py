"""Action handlers: model.endpoints, model.chat (design §9.9, WP3a). model.pull and model.forward (gateway relay
path) arrive with WP3b (``PENDING_ACTIONS``).

Every HTTP request goes through ``netutil.guarded_request`` with ``guard=ctx.policy`` (NetGuard + rule 5a/5c for
``purpose="model"``); ``api_key_env`` credentials come only from a service definition or the owner's
``model_endpoints`` (``ServiceManager.endpoint_for``), never from the agent. Handlers register with ``@handler(name)``
and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from .. import fsops, netutil, util
from ..errors import HiveError, bad_request, not_found, unavailable
from . import ActionContext, handler

PENDING_ACTIONS = ("model.pull", "model.forward")      # WP3b (§22.1); the handlers gate test skips them until then
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
    try:
        r = _get(ctx, _join(row["base_url"], "models"), timeout=timeout, auth=auth)
        answered = True
        if 200 <= r.status < 300:
            models = _models_of(_json(r.body))
            row["healthy"] = True
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
                add({"name": r.get("name"), "source": "service", "api": r.get("api") or "openai",
                     "server": r.get("server") or "unknown", "base_url": str(r.get("base_url")),
                     "healthy": r.get("healthy"), "models": r.get("models")})
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


__all__ = ["model_endpoints", "model_chat", "list_endpoints", "parse_base_url", "PENDING_ACTIONS"]
