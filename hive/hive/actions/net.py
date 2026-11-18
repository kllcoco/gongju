"""Action handlers: net.http, net.download, net.ports (listener lookup from procinfo, WP4) (design §9.8, WP3).

net.http goes through ``netutil.guarded_request`` with ``guard=ctx.policy`` (NetGuard + rules 5a/5c), secrets only
by name (``auth_env`` -> ``EffectivePolicy.auth_spec(name, method=)``, rule 5b). net.download is a job of kind
``download`` running ``hive _download`` through the shim; the URL, headers, proxy settings and the resolved secret
travel only in the 0600 spec file (§11.3). net.ports checks 127.0.0.1 only. Handlers register with
``@handler(name)`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

import base64
import binascii
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import netutil, util
from ..errors import HiveError, bad_request, conflict, denied, internal, not_found
from . import ActionContext, handler

TEXT_TYPES = ("text/", "application/json", "application/xml", "application/javascript", "application/x-ndjson",
              "+json", "+xml", "application/x-www-form-urlencoded", "application/yaml", "application/toml")
MAX_HEADERS = 50
MAX_HEADER_VALUE = 4096
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}\Z")
DOWNLOAD_LOG_BYTES = 4 << 20


def _headers(p: dict) -> Dict[str, str]:
    """``["Name: value", …]`` -> dict (later duplicates win)."""
    return util.parse_kv(p.get("headers") or [], sep=":")


def auth_spec(ctx: ActionContext, name: str, method: str = "GET") -> netutil.AuthSpec:
    """``EffectivePolicy.auth_spec(name, method=)`` (rule 5b); test fakes without ``method`` are tolerated."""
    try:
        return ctx.policy.auth_spec(name, method=method)
    except TypeError:
        return ctx.policy.auth_spec(name)


def first_hop_auth(auth: netutil.AuthSpec, target: Any) -> None:
    """The first hop of a request carrying a secret must be a host the secret is bound to (C2) and not plain
    http:// to a public target (C1)."""
    if target.scheme == "http" and target.category not in ("loopback", "private"):
        raise denied(f"secret '{auth.name}' would be sent over plain http:// to {target.host}",
                     rule="plaintext_credentials", hint="use https:// for requests that carry a secret")
    if not netutil.auth_host_allowed(auth.hosts, target.host, target.scheme):
        hosts = ", ".join(auth.hosts) or "(no hosts)"
        raise denied(f"secret '{auth.name}' may not be sent to {target.host}", rule="secret_host",
                     hint=f"secret {auth.name} may only be sent to {hosts}; the owner can widen it: hive node secret "
                          f"set {auth.name} --hosts +{target.host}")


def _is_text(content_type: str, body: bytes) -> bool:
    ct = (content_type or "").lower()
    if any(t in ct for t in TEXT_TYPES):
        try:
            body.decode("utf-8")
            return True
        except UnicodeDecodeError:
            return "charset=" in ct
    if b"\x00" in body[:8192]:
        return False
    try:
        body.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _decode(content_type: str, body: bytes) -> str:
    m = re.search(r"charset=([A-Za-z0-9._-]+)", content_type or "", re.IGNORECASE)
    if m:
        try:
            return body.decode(m.group(1), "replace")
        except LookupError:
            pass
    return body.decode("utf-8", "replace")


def _ct(headers: Dict[str, str]) -> str:
    for k, v in (headers or {}).items():
        if k.lower() == "content-type":
            return str(v)
    return ""


# ---------------------------------------------------------------------------------------------------------------
# net.http

@handler("net.http")
def net_http(ctx: ActionContext, p: dict) -> dict:
    """One vetted HTTP(S) request; 4xx/5xx are results."""
    method = str(p.get("method") or "GET").upper()
    headers = _headers(p)
    body: Optional[bytes] = None
    if p.get("body") is not None:
        if p.get("body_base64"):
            try:
                body = base64.b64decode(str(p["body"]), validate=True)
            except (binascii.Error, ValueError) as e:
                raise bad_request(f"body is not valid base64: {e}", hint="send text with body_base64=false")
        else:
            body = str(p["body"]).encode("utf-8")
    if p.get("content_type"):
        if any(k.lower() == "content-type" for k in headers):
            raise bad_request("give the content type either in headers or as content_type, not both")
        headers["Content-Type"] = str(p["content_type"])
    auth = auth_spec(ctx, str(p["auth_env"]), method) if p.get("auth_env") else None
    limit = int(getattr(ctx.policy.limits, "max_response_bytes", 8388608))
    max_bytes = min(int(p.get("max_bytes", 65536)), limit)
    budget = max(1.0, min(float(p.get("timeout", 30)), ctx.remaining() - 1.0))
    r = netutil.guarded_request(method, str(p["url"]), guard=ctx.policy, purpose="http", headers=headers, body=body,
                                timeout=budget, max_bytes=max_bytes,
                                follow_redirects=bool(p.get("follow_redirects", True)),
                                verify_tls=bool(p.get("verify_tls", True)), auth=auth, cancel=ctx.cancel)
    ctx.audit_extra.update(status=r.status, route=r.route, dns=getattr(r, "dns", "local"), bytes=len(r.body))
    out_headers: Dict[str, str] = {}
    for k, v in list(r.headers.items())[:MAX_HEADERS]:
        out_headers[str(k)] = str(v)[:MAX_HEADER_VALUE]
    result: Dict[str, Any] = {"status": r.status, "reason": r.reason, "url": r.url, "headers": out_headers,
                              "truncated": bool(r.truncated), "bytes": len(r.body), "elapsed_s": r.elapsed_s,
                              "route": r.route}
    ct = _ct(r.headers)
    if not r.body or _is_text(ct, r.body):
        result["body"] = _decode(ct, r.body)
    else:
        result["body_base64"] = base64.b64encode(r.body).decode("ascii")
    return result


# ---------------------------------------------------------------------------------------------------------------
# net.download

def _dns_side(target: Any) -> str:
    """§5.4 NetGuard rule 8 audit value (local|remote) of a vetted target."""
    try:
        return netutil._dns_side(target)
    except Exception:  # noqa: BLE001 - test fakes
        return "local"


def _guard_dict(ctx: ActionContext) -> dict:
    ng = getattr(ctx.policy, "net_guard", None)
    to_dict = getattr(ng, "to_dict", None)
    if not callable(to_dict):
        raise internal("the node's network guard cannot be handed to the downloader")
    return to_dict()


def _jobs(ctx: ActionContext) -> Any:
    jm = getattr(ctx.rt, "jobs", None)
    if jm is None or not hasattr(jm, "start_job"):
        raise internal("the job manager is not available on this node")
    return jm


def _casefold(ctx: ActionContext, directory: Path) -> bool:
    try:
        return ctx.policy.path_key(str(directory) + "/AbC", follow=False).endswith("/abc")
    except Exception:  # noqa: BLE001
        return os.name == "nt"


def _deny_patterns(ctx: ActionContext) -> List[str]:
    fn = getattr(ctx.policy, "deny_patterns", None)
    if callable(fn):
        return [str(x) for x in fn(write=True)]
    return []


def start_download_job(ctx: ActionContext, *, kind: str, spec: dict, name: str, tags: Any = ()) -> dict:
    """Start ``hive _download`` as a job of ``kind`` (download|pull) with ``spec`` in the 0600 extra file."""
    jm = _jobs(ctx)
    argv = util.self_command() + ["_download", "--spec", "{run_dir}/download.json"]
    try:
        env = ctx.policy.child_env({})
    except HiveError:
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith("HIVE_")}
    for k in [k for k in env if k.upper().startswith("HIVE_")]:
        env.pop(k, None)
    info = jm.start_job(ctx, kind=kind, argv_or_cmdline=argv, shell="none", cwd=Path(str(ctx.policy.workdir)),
                        env=env, stdin=None, name=name[:64], timeout=0, merge_stderr=True,
                        log_max_bytes=DOWNLOAD_LOG_BYTES, log_keep=1, tags=tags,
                        command_display=f"hive _download ({kind})",
                        extra_files={"download.json": util.canonical_json(spec).encode("utf-8")})
    if info.get("state") == "failed":
        raise internal(f"the downloader could not start: {info.get('error') or 'unknown error'}",
                       job_id=info.get("job_id"))
    ctx.audit_extra.update(job_id=info.get("job_id"))
    return info


@handler("net.download")
def net_download(ctx: ActionContext, p: dict) -> dict:
    """Download one URL in the background (job of kind download)."""
    url = str(p["url"])
    sha = p.get("sha256")
    if sha is not None and not SHA256_RE.match(str(sha).strip()):
        raise bad_request("sha256 must be 64 hex characters")
    headers = _headers(p)
    netutil._check_headers(headers, None)            # framing headers etc. fail here, not in the child
    if any(k.lower() == "authorization" for k in headers) and p.get("auth_env"):
        raise bad_request("give either an Authorization header or auth_env, not both",
                          hint="auth_env sends the secret as 'Authorization: Bearer …'")
    target = ctx.policy.check_url(url, purpose="download", method="GET")
    auth = None
    if p.get("auth_env"):
        auth = auth_spec(ctx, str(p["auth_env"]), "GET")
        first_hop_auth(auth, target)
    raw_path = str(p["path"])
    dest: Optional[Path] = None
    directory: Optional[Path] = None
    try:
        cand = ctx.policy.resolve_path(raw_path, write=True)
    except HiveError:
        raise
    if cand.is_dir():
        directory = ctx.policy.resolve_path(raw_path, write=True, must_exist=True)
        # the server picks the name: vet a file *inside* the folder too, so the rules that look at a file's parents
        # (v7 venv bin/Scripts, PATH and service code dirs) apply to the folder itself (§5.4 PathGuard step 7)
        ctx.policy.resolve_path(str(directory / "download.bin"), write=True)
    else:
        dest = cand
        if dest.is_symlink():
            raise bad_request(f"'{dest}' is a symbolic link", hint="download to a regular file path")
        if dest.exists() and not p.get("overwrite"):
            raise conflict(f"'{dest}' already exists", hint="use overwrite=true to replace it")
        if not dest.parent.is_dir():
            if not p.get("make_dirs", True):
                raise not_found(f"folder '{dest.parent}' does not exist", hint="create it or use make_dirs=true")
            ctx.policy.resolve_path(str(dest.parent), write=True)
        ctx.policy.resolve_path(str(dest) + ".hive-part", write=True)
        ctx.policy.resolve_path(str(dest) + ".hive-part.json", write=True)     # the resume validator sidecar
    spec: Dict[str, Any] = {
        "v": 1, "mode": "file", "net": _guard_dict(ctx),
        "auth": ({"name": auth.name, "header": auth.header, "value": auth.value, "hosts": list(auth.hosts)}
                 if auth is not None else None),
        "url": url, "path": str(dest) if dest is not None else None,
        "dir": str(directory) if directory is not None else None,
        "deny": _deny_patterns(ctx) if directory is not None else [],
        "casefold": _casefold(ctx, directory or dest.parent),   # type: ignore[union-attr]
        "headers": headers, "sha256": str(sha).strip().lower() if sha else None,
        "overwrite": bool(p.get("overwrite")), "resume": bool(p.get("resume", True)),
        "make_dirs": bool(p.get("make_dirs", True))}
    shown = (dest or directory)
    info = start_download_job(ctx, kind="download", spec=spec,
                              name="download " + (url.rsplit("/", 1)[-1][:40] or target.host))
    ctx.audit_extra.update(path=str(shown), host=target.host, route=getattr(target, "route", None),
                           dns=_dns_side(target))
    return {"job_id": info["job_id"], "path": str(shown), "state": "running"}


# ---------------------------------------------------------------------------------------------------------------
# net.ports

def _listeners() -> Any:
    from .. import procinfo   # function-level: WP4 module (§22.3 rule 3)
    try:
        rows, method = procinfo.listeners()
    except HiveError:
        rows, method = [], "unavailable"
    return list(rows or []), str(method or "unavailable")


def _row(r: dict) -> dict:
    port = r.get("port")
    return {"proto": str(r.get("proto") or "tcp"), "address": str(r.get("address") or ""),
            "port": int(port) if isinstance(port, int) else 0,
            "pid": r.get("pid") if isinstance(r.get("pid"), int) else None,
            "process": r.get("process") if r.get("process") is not None else r.get("name")}


@handler("net.ports")
def net_ports(ctx: ActionContext, p: dict) -> dict:
    """Open loopback ports (with owner process) or all listening sockets."""
    rows, method = _listeners()
    ports = p.get("ports")
    if not ports:
        return {"listeners": [_row(r) for r in rows if isinstance(r, dict)][:2000], "method": method}
    timeout = float(p.get("timeout", 1.0))
    by_port: Dict[int, dict] = {}
    for r in rows:
        if isinstance(r, dict) and isinstance(r.get("port"), int) and r["port"] not in by_port:
            by_port[r["port"]] = r
    out: List[Optional[dict]] = [None] * len(ports)

    def check(i: int, port: int) -> None:
        ok, ms, _err = netutil.tcp_check("127.0.0.1", int(port), timeout)
        lr = by_port.get(int(port))
        out[i] = {"port": int(port), "open": bool(ok), "latency_ms": ms,
                  "pid": lr.get("pid") if lr and isinstance(lr.get("pid"), int) else None,
                  "process": (lr.get("process") or lr.get("name")) if lr else None,
                  "address": lr.get("address") if lr else None}

    threads = [threading.Thread(target=check, args=(i, port), name="hive-port", daemon=True)
               for i, port in enumerate(ports)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout + 2.0)
    res = [r if r is not None else {"port": int(ports[i]), "open": False, "latency_ms": None, "pid": None,
                                    "process": None, "address": None} for i, r in enumerate(out)]
    return {"ports": res, "method": method}


__all__ = ["net_http", "net_download", "net_ports", "auth_spec", "first_hop_auth", "start_download_job"]
