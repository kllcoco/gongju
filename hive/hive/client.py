"""Python client: Target protocol, HttpTarget, HubTarget, Client, NodeHandle, JobHandle, ServiceHandle, connect()
(design §15.2, §14.4, D45, WP5).

``HttpTarget`` wraps ``transport.HttpTransport``; ``Client`` adds request ids, the default node and handles;
``connect()`` resolves a URL, a client.json connection name, ``"local"`` (the ``this-pc`` agent connection, never the
owner token, D45/§5.1) or the client.json default (with the ``HIVE_*`` environment overrides of §14.4).
``HubTarget`` (in-process hub target) arrives with WP5b; transfers (``Client.transfer``, ``NodeHandle.upload`` /
``download``) use ``transfer.py`` (WP5b) through function-level imports.
"""
from __future__ import annotations

import difflib
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Tuple, Union
from urllib.parse import quote

from typing import Protocol, runtime_checkable

from .errors import HiveError, bad_request, internal, not_found, unsupported
from .util import new_id, valid_name

if TYPE_CHECKING:  # pragma: no cover
    from .config import ClientConfig, Connection
    from .tools import Toolkit
    from .transport import HttpTransport

LOCAL_CONNECTION = "this-pc"          # D45: connect("local") uses the agent token connection, never "this-pc-owner"
LOCAL_HINT = "start it: hive node run (or hive service install node)"
KIND_RETRY_SECONDS = 30.0             # resolve_kind(): a failed whoami probe is not repeated for this long
JOB_FINAL = ("exited", "failed", "killed", "timeout", "lost")


@runtime_checkable
class Target(Protocol):
    """Something calls can be sent to (hub or node)."""

    kind: str

    def call(self, action: str, params: Optional[dict], *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        """Result dict (meta is not returned)."""
        ...

    def cancel(self, request_id: str) -> bool:
        """Cancel a call."""
        ...

    def whoami(self) -> dict:
        """GET /v1/whoami."""
        ...

    def nodes(self) -> List[dict]:
        """NodeSummary rows."""
        ...

    def actions(self, node: Optional[str] = None) -> List[dict]:
        """Action rows."""
        ...


class HttpTarget(Target):
    """Target over HttpTransport (§15.2); ``kind`` ("hub" | "node") comes from ``whoami.server.role``.

    ``kind`` is a plain attribute (``"unknown"`` until known) so attribute access and ``isinstance(t, Target)`` (Python
    3.9-3.11 call ``hasattr`` on Protocol data members) never touch the network; ``whoami()`` updates it and
    ``resolve_kind()`` asks the server on purpose.
    """

    kind: str = "unknown"
    _kind_failed_at: Optional[float] = None

    def __init__(self, transport: Union["HttpTransport", str], token: Optional[str] = None, *,
                 kind: Optional[str] = None, connection: Optional[str] = None, **transport_kw: Any):
        from .transport import HttpTransport
        if isinstance(transport, str):
            transport = HttpTransport(transport, token, **transport_kw)
        elif token is not None or transport_kw:
            raise bad_request("token and transport options are given to the HttpTransport, not to HttpTarget")
        self.transport: HttpTransport = transport
        self.connection = connection
        self.kind = kind if kind in ("hub", "node") else "unknown"
        self._whoami: Optional[dict] = None
        self._kind_failed_at = None

    @property
    def url(self) -> str:
        """Base URL of the server."""
        return self.transport.url

    def resolve_kind(self) -> str:
        """``kind``, learned with one ``GET /v1/whoami`` while unknown; ``"unknown"`` when that fails (a failed probe
        is not repeated for KIND_RETRY_SECONDS, so polling callers do not block on an unreachable server)."""
        if self.kind in ("hub", "node"):
            return self.kind
        failed = self._kind_failed_at
        if failed is not None and time.monotonic() - failed < KIND_RETRY_SECONDS:
            return "unknown"
        try:
            self.whoami()
        except HiveError:
            self._kind_failed_at = time.monotonic()
            return "unknown"
        return self.kind

    def call(self, action: str, params: Optional[dict], *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        """POST /v1/call; the result dict (errors raise HiveError)."""
        if not isinstance(action, str) or not action:
            raise bad_request("missing action")
        if params is not None and not isinstance(params, dict):
            raise bad_request("params must be an object")
        body: Dict[str, Any] = {"action": action, "params": params or {}}
        if node is not None:
            body["node"] = node
        if timeout is not None:
            body["timeout"] = timeout
        if request_id is not None:
            body["request_id"] = request_id
        if approval_id is not None:
            body["approval_id"] = approval_id
        result = self.transport.request("POST", "/v1/call", body)
        if not isinstance(result, dict):
            raise internal(f"unexpected result type from {self.url} for {action}")
        return result

    def cancel(self, request_id: str) -> bool:
        """POST /v1/cancel."""
        r = self.transport.request("POST", "/v1/cancel", {"request_id": request_id})
        return bool((r or {}).get("cancelled"))

    def whoami(self) -> dict:
        """GET /v1/whoami (also learns ``kind``)."""
        r = self.transport.request("GET", "/v1/whoami")
        if not isinstance(r, dict):
            raise internal(f"unexpected whoami result from {self.url}")
        role = (r.get("server") or {}).get("role") if isinstance(r.get("server"), dict) else None
        if role in ("hub", "node"):
            self.kind = role
        self._kind_failed_at = None
        self._whoami = r
        return r

    def nodes(self) -> List[dict]:
        """GET /v1/nodes -> NodeSummary rows."""
        r = self.transport.request("GET", "/v1/nodes") or {}
        return list(r.get("nodes") or [])

    def actions_info(self, node: Optional[str] = None) -> dict:
        """The whole ``GET /v1/actions`` result (``node``, ``catalog_hash``, ``implemented``, ``actions``)."""
        path = "/v1/actions" + (f"?node={quote(node, safe='')}" if node else "")
        r = self.transport.request("GET", path)
        return r if isinstance(r, dict) else {}

    def actions(self, node: Optional[str] = None) -> List[dict]:
        """GET /v1/actions[?node=N] -> action rows."""
        return list(self.actions_info(node).get("actions") or [])

    def close(self) -> None:
        """Close the transport's connections."""
        self.transport.close()

    def __repr__(self) -> str:
        return f"HttpTarget({self.transport.url!r})"


class HubTarget(Target):
    """In-process Hub + Identity + request ip (hub-hosted ``/v1/tools`` and ``/mcp``, §15.2). kind ``"hub"``."""

    def __init__(self, hub: Any, identity: Any, ip: str = ""):
        if hub is None or identity is None:
            raise bad_request("HubTarget needs a hub and an identity")
        self.hub = hub
        self.identity = identity
        self.ip = ip or ""
        self.kind = "hub"
        self.connection: Optional[str] = None

    def call(self, action: str, params: Optional[dict], *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        """Hub.call envelope -> result (with its ``masked`` flag) or HiveError."""
        env = self.hub.call(self.identity, action, params or {}, node=node, timeout=timeout, request_id=request_id,
                            approval_id=approval_id, ip=self.ip)
        if env.get("ok"):
            r = env.get("result")
            return r if isinstance(r, dict) else {"result": r}
        raise HiveError.from_dict(env.get("error"))

    def cancel(self, request_id: str) -> bool:
        """Cancel a routed call of this identity."""
        return bool(self.hub.cancel(request_id, self.identity))

    def whoami(self) -> dict:
        """GET /v1/whoami body."""
        return self.hub.whoami(self.identity)

    def nodes(self) -> List[dict]:
        """NodeSummary rows visible to the identity."""
        return self.hub.nodes_for(self.identity)

    def actions_info(self, node: Optional[str] = None) -> dict:
        """The whole /v1/actions body."""
        return self.hub.actions_for(self.identity, node)

    def actions(self, node: Optional[str] = None) -> List[dict]:
        """Action rows."""
        return list(self.actions_info(node).get("actions") or [])

    def close(self) -> None:
        """Nothing to close."""

    def __repr__(self) -> str:
        return f"HubTarget({getattr(self.hub, 'name', '?')!r}, {getattr(self.identity, 'name', '?')!r})"


class Client:
    """High-level client (§15.2)."""

    def __init__(self, target: Target, *, default_node: Optional[str] = None):
        if target is None:
            raise bad_request("Client needs a target")
        self.target = target
        self.default_node = default_node or None
        self.config: Optional["ClientConfig"] = None     # set by from_config/connect (Toolkit reads tools/local_files)
        self.connection: Optional[str] = getattr(target, "connection", None)

    # ------------------------------------------------------------------ construction

    @classmethod
    def from_config(cls, path: Optional[str] = None, connection: Optional[str] = None) -> "Client":
        """Client from client.json (§14.4 resolution: connection → HIVE_URL/HIVE_TOKEN… → HIVE_CONNECTION → default)."""
        return _from_config(path, connection, {})

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"Client({self.target!r}, default_node={self.default_node!r})"

    # ------------------------------------------------------------------ calls

    def call(self, action: str, params: Optional[dict] = None, *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        """Call an action (always with a request id, generated when not given)."""
        rid = request_id or new_request_id()
        return self.target.call(action, params or {}, node=node if node is not None else self.default_node,
                                timeout=timeout, request_id=rid, approval_id=approval_id)

    def nodes(self) -> List[dict]:
        """NodeSummary rows."""
        return self.target.nodes()

    def node(self, name: Optional[str] = None) -> "NodeHandle":
        """Handle for one node."""
        return NodeHandle(self, name if name is not None else self.default_node)

    def whoami(self) -> dict:
        """Identity and server info."""
        return self.target.whoami()

    def actions(self, node: Optional[str] = None) -> List[dict]:
        """Action rows."""
        return self.target.actions(node)

    def cancel(self, request_id: str) -> bool:
        """Cancel a call."""
        return self.target.cancel(request_id)

    def toolkit(self, **kw: Any) -> "Toolkit":
        """Agent Toolkit over this client."""
        from .tools import Toolkit
        return Toolkit(self, **kw)

    def transfer(self, src: str, dst: str, *, overwrite: bool = False, progress: Any = None,
                 cancel: Any = None) -> dict:
        """Copy "node:path" / "local:path" (the copy engine runs in this process, §15.3)."""
        from . import transfer as tr
        s_ep, s_path = self._endpoint(src, tr)
        d_ep, d_path = self._endpoint(dst, tr)
        return tr.copy(s_ep, s_path, d_ep, d_path, overwrite=overwrite, progress=progress, cancel=cancel)

    def _endpoint(self, spec: str, tr: Any) -> Tuple[Any, str]:
        where, path = split_location(spec)
        if where == "local":
            return tr.LocalFS(None), path
        return tr.NodeFS(self.target, where), path

    def close(self) -> None:
        """Close connections."""
        fn = getattr(self.target, "close", None)
        if callable(fn):
            fn()


def new_request_id() -> str:
    """A client-generated request id ``r_…`` (matches ``^[A-Za-z0-9_.:-]{1,80}$``, §6.4)."""
    return "r_" + new_id()


def split_location(spec: str) -> Tuple[str, str]:
    """``"node:path"`` / ``"local:path"`` -> (node name or "local", path); bad_request without a valid prefix."""
    if not isinstance(spec, str) or ":" not in spec:
        raise bad_request(f"location {spec!r} needs a prefix", hint='use "local:PATH" or "NODE:PATH"')
    where, path = spec.split(":", 1)
    w = where.strip().lower()
    if len(where) == 1 and where.isascii() and where.isalpha() and path[:1] in ("\\", "/"):
        # "C:\\data\\f.bin" is a Windows drive path, not node "c"
        raise bad_request(f"location {spec!r} looks like a Windows drive path",
                          hint=f'local paths need the local: prefix, e.g. "local:{spec}"')
    if not path or not (w == "local" or w == "self" or valid_name(w)):
        raise bad_request(f"invalid location {spec!r}", hint='use "local:PATH" or "NODE:PATH"')
    return w, path


# ---------------------------------------------------------------------------------------------------------------
# handles

class NodeHandle:
    """Convenience methods for one node (§15.2); ``name`` None = the client's default node / the direct node."""

    def __init__(self, client: Client, name: Optional[str] = None):
        self.client = client
        self.name = name

    def __repr__(self) -> str:
        return f"NodeHandle({self.name!r})"

    def call(self, action: str, params: Optional[dict] = None, **kw: Any) -> dict:
        """client.call on this node."""
        return self.client.call(action, params or {}, node=self.name, **kw)

    def info(self) -> dict:
        """node.info."""
        return self.call("node.info", {})

    def sys_info(self, sections: Optional[List[str]] = None) -> dict:
        """system.info."""
        return self.call("system.info", {"sections": list(sections)} if sections else {})

    def shell(self, command: str, **kw: Any) -> dict:
        """shell.run."""
        return self.call("shell.run", dict(kw, command=command))

    def sh(self, command: str, **kw: Any) -> str:
        """shell.run with separate stderr; returns stdout; raises internal (exit_code, stderr) on failure."""
        kw.setdefault("merge_stderr", False)
        r = self.shell(command, **kw)
        code = r.get("exit_code")
        if code != 0 or r.get("state") not in (None, "exited"):
            raise HiveError("internal", f"command failed (exit code {code}, state {r.get('state')})",
                            details={"exit_code": code, "stderr": r.get("stderr") or "", "state": r.get("state"),
                                     "job_id": r.get("job_id")})
        out = r.get("stdout")
        return out if isinstance(out, str) else (r.get("output") or "")

    def read_text(self, path: str, **kw: Any) -> str:
        """fs.read, page after page until ``next_start_line`` is None."""
        kw.pop("start_line", None)
        parts: List[str] = []
        start = 1
        for _ in range(1_000_000):
            r = self.call("fs.read", dict(kw, path=path, start_line=start))
            parts.append(r.get("content") or "")
            nxt = r.get("next_start_line")
            if nxt is None:
                break
            if not isinstance(nxt, int) or nxt <= start:
                raise internal("fs.read did not advance", details={"start_line": start, "next_start_line": nxt})
            start = nxt
        return "".join(parts)

    def write_text(self, path: str, text: str, **kw: Any) -> dict:
        """fs.write."""
        return self.call("fs.write", dict(kw, path=path, content=text))

    def edit(self, path: str, old: str, new: str, **kw: Any) -> dict:
        """fs.edit."""
        return self.call("fs.edit", dict(kw, path=path, old_text=old, new_text=new))

    def ls(self, path: str = ".", **kw: Any) -> dict:
        """fs.list."""
        return self.call("fs.list", dict(kw, path=path))

    def search(self, pattern: str, path: str = ".", **kw: Any) -> dict:
        """fs.search."""
        return self.call("fs.search", dict(kw, pattern=pattern, path=path))

    def stat(self, path: str) -> dict:
        """fs.stat."""
        return self.call("fs.stat", {"path": path})

    def upload(self, local: str, remote: str, **kw: Any) -> dict:
        """Copy a local file to this node (transfer.copy, §15.3)."""
        from . import transfer as tr
        return tr.copy(tr.LocalFS(None), local, tr.NodeFS(self.client.target, self._node()), remote, **kw)

    def download(self, remote: str, local: str, **kw: Any) -> dict:
        """Copy a file of this node to a local path (transfer.copy, §15.3)."""
        from . import transfer as tr
        return tr.copy(tr.NodeFS(self.client.target, self._node()), remote, tr.LocalFS(None), local, **kw)

    def _node(self) -> Optional[str]:
        return self.name if self.name is not None else self.client.default_node

    def start_job(self, command: str, **kw: Any) -> "JobHandle":
        """job.start -> JobHandle."""
        r = self.call("job.start", dict(kw, command=command))
        return JobHandle(self.client, self.name, r["job_id"], started=r)

    def job(self, job_id: str) -> "JobHandle":
        """Handle for an existing job."""
        return JobHandle(self.client, self.name, job_id)

    def define_service(self, name: str, **kw: Any) -> dict:
        """service.define."""
        return self.call("service.define", dict(kw, name=name))

    def service(self, name: str) -> "ServiceHandle":
        """Handle for a service."""
        return ServiceHandle(self.client, self.name, name)

    def chat(self, prompt_or_messages: Union[str, List[dict]], **kw: Any) -> str:
        """model.chat; returns the reply text."""
        params = dict(kw)
        if isinstance(prompt_or_messages, str):
            params["prompt"] = prompt_or_messages
        else:
            params["messages"] = list(prompt_or_messages)
        r = self.call("model.chat", params)
        return r.get("content") or ""

    def fetch(self, url: str, dest: str, **kw: Any) -> "JobHandle":
        """net.download -> JobHandle."""
        r = self.call("net.download", dict(kw, url=url, path=dest))
        return JobHandle(self.client, self.name, r["job_id"], started=r)

    def pull_model(self, repo: str, **kw: Any) -> Union["JobHandle", dict]:
        """model.pull -> JobHandle (``dry_run=True`` returns the plan dict instead)."""
        r = self.call("model.pull", dict(kw, repo=repo))
        if kw.get("dry_run") or not r.get("job_id"):
            return r
        return JobHandle(self.client, self.name, r["job_id"], started=r)

    def follow_logs(self, job_id: Optional[str] = None, service: Optional[str] = None,
                    poll: float = 1.0) -> Iterator[str]:
        """Yield new log text (starts with the tail); a job's stream ends when the job is final and fully read."""
        if (job_id is None) == (service is None):
            raise bad_request("give exactly one of job_id, service")
        action, key, ident = ("job.logs", "job_id", job_id) if job_id else ("service.logs", "name", service)
        offset: Optional[int] = None
        poll = max(0.05, float(poll))
        while True:
            params: Dict[str, Any] = {key: ident}
            if offset is not None:
                params["offset"] = offset
            r = self.call(action, params)
            text = r.get("text") or ""
            nxt = r.get("next_offset")
            if isinstance(nxt, int):
                offset = nxt
            if text:
                yield text
            final = job_id is not None and r.get("state") in JOB_FINAL and bool(r.get("eof", True))
            if final and not text:
                return
            if not text:
                time.sleep(poll)


class JobHandle:
    """status() / wait() / logs() / stop() of one job (§15.2)."""

    def __init__(self, client: Client, node: Optional[str], job_id: str, *, started: Optional[dict] = None):
        self.client = client
        self.node = node
        self.job_id = job_id
        self.started = started

    def __repr__(self) -> str:
        return f"JobHandle({self.job_id!r}, node={self.node!r})"

    def _call(self, action: str, params: dict) -> dict:
        return self.client.call(action, dict(params, job_id=self.job_id), node=self.node)

    def status(self, **kw: Any) -> dict:
        """job.status."""
        return self._call("job.status", kw)

    def wait(self, timeout: Optional[float] = None) -> dict:
        """Wait until the job is final (job.status wait=min(30, remaining)); returns the last status."""
        end = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            remaining = 30.0 if end is None else max(0.0, end - time.monotonic())
            st = self._call("job.status", {"wait": min(30.0, remaining)})
            if st.get("state") in JOB_FINAL or (end is not None and time.monotonic() >= end):
                return st

    def logs(self, **kw: Any) -> dict:
        """job.logs."""
        return self._call("job.logs", kw)

    def stop(self, **kw: Any) -> dict:
        """job.stop."""
        return self._call("job.stop", kw)


class ServiceHandle:
    """status() / start() / stop() / restart() / logs() / remove() of one service (§15.2)."""

    def __init__(self, client: Client, node: Optional[str], name: str):
        self.client = client
        self.node = node
        self.name = name

    def __repr__(self) -> str:
        return f"ServiceHandle({self.name!r}, node={self.node!r})"

    def _call(self, action: str, params: dict) -> dict:
        return self.client.call(action, dict(params, name=self.name), node=self.node)

    def status(self, **kw: Any) -> dict:
        """service.status."""
        return self._call("service.status", kw)

    def start(self, wait: float = 0) -> dict:
        """service.start."""
        return self._call("service.start", {"wait": wait})

    def stop(self, **kw: Any) -> dict:
        """service.stop."""
        return self._call("service.stop", kw)

    def restart(self, wait: float = 0) -> dict:
        """service.restart."""
        return self._call("service.restart", {"wait": wait})

    def logs(self, **kw: Any) -> dict:
        """service.logs."""
        return self._call("service.logs", kw)

    def remove(self, **kw: Any) -> dict:
        """service.remove."""
        return self._call("service.remove", kw)


# ---------------------------------------------------------------------------------------------------------------
# connection resolution (§14.4, §15.2)

_TRANSPORT_KW = ("ca_file", "tls_pin", "timeout", "proxy")


def _read_token_file(path: str, base: Optional[Path]) -> str:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute() and base is not None:
        p = base / p
    try:
        tok = p.read_text(encoding="utf-8-sig").strip()
    except FileNotFoundError:
        raise not_found(f"token file {str(p)!r} not found",
                        hint="check token_file in client.json (hive init client), or HIVE_TOKEN_FILE")
    except OSError as e:
        raise bad_request(f"cannot read token file {str(p)!r}: {e.strerror or e}")
    if not tok:
        raise bad_request(f"token file {str(p)!r} is empty")
    return tok.splitlines()[0].strip()


def _conn_token(conn: "Connection", base: Optional[Path]) -> Optional[str]:
    if conn.token:
        return conn.token
    if conn.token_file:
        return _read_token_file(conn.token_file, base)
    return None


def _env_token(env: Dict[str, str]) -> Optional[str]:
    if env.get("HIVE_TOKEN"):
        return env["HIVE_TOKEN"].strip()
    if env.get("HIVE_TOKEN_FILE"):
        return _read_token_file(env["HIVE_TOKEN_FILE"], None)
    return None


def _target_for(conn: "Connection", name: Optional[str], token: Optional[str], base: Optional[Path],
                overrides: Dict[str, Any]) -> HttpTarget:
    from .transport import HttpTransport
    tok = token if token is not None else _conn_token(conn, base)
    kw = {"ca_file": conn.ca_file, "tls_pin": conn.tls_pin, "timeout": conn.timeout, "proxy": conn.proxy}
    kw.update({k: v for k, v in overrides.items() if k in _TRANSPORT_KW and v is not None})
    return HttpTarget(HttpTransport(conn.url, tok, **kw), connection=name)


def _load_cfg(path: Optional[str]) -> "ClientConfig":
    from .config import load_client_config
    return load_client_config(path)


def _cfg_base(cfg: "ClientConfig") -> Optional[Path]:
    p = getattr(cfg, "_path", None)
    return Path(p).parent if p else None


def _default_node(conn_default: Optional[str], overrides: Dict[str, Any]) -> Optional[str]:
    if overrides.get("default_node"):
        return overrides["default_node"]
    return os.environ.get("HIVE_NODE") or conn_default or None


def _named(cfg: "ClientConfig", name: str, token: Optional[str], overrides: Dict[str, Any]) -> Client:
    conn = cfg.connections.get(name)
    if conn is None:
        names = sorted(cfg.connections)
        sugg = difflib.get_close_matches(name, names, n=3, cutoff=0.6)
        hint = (f"did you mean {', '.join(repr(s) for s in sugg)}? " if sugg else "") + \
               (f"known connections: {', '.join(names)}" if names else "add one with: hive init client --url URL")
        if name == LOCAL_CONNECTION:
            hint = "run hive init node on this computer (it creates the this-pc connection), then " + LOCAL_HINT
        raise not_found(f"no connection {name!r} in client.json", hint=hint, suggestions=sugg)
    target = _target_for(conn, name, token, _cfg_base(cfg), overrides)
    client = Client(target, default_node=_default_node(conn.default_node, overrides))
    client.config = cfg
    client.connection = name
    return client


def _from_env(cfg: Optional["ClientConfig"], token: Optional[str], overrides: Dict[str, Any]) -> Client:
    from .config import Connection
    env = dict(os.environ)
    conn = Connection(url=env["HIVE_URL"].strip(), tls_pin=env.get("HIVE_TLS_PIN") or None)
    tok = token if token is not None else _env_token(env)
    target = _target_for(conn, None, tok, None, overrides)
    client = Client(target, default_node=_default_node(None, overrides))
    client.config = cfg
    return client


def _from_config(path: Optional[str], connection: Optional[str], overrides: Dict[str, Any],
                 token: Optional[str] = None) -> Client:
    if not connection and os.environ.get("HIVE_URL", "").strip():
        # HIVE_URL comes before HIVE_CONNECTION/default (§14.4): an unreadable client.json must not break env-only
        # setups (pods, CI); it is loaded tolerantly, like for an explicit URL
        try:
            cfg: Optional["ClientConfig"] = _load_cfg(path)
        except HiveError:
            cfg = None
        return _from_env(cfg, token, overrides)
    cfg = _load_cfg(path)
    if connection:
        return _named(cfg, connection, token, overrides)
    name = os.environ.get("HIVE_CONNECTION", "").strip() or cfg.default
    if not name:
        raise not_found("no Hive connection configured",
                        hint="hive init client --url URL --token-file FILE --default, or set HIVE_URL and "
                             "HIVE_TOKEN_FILE; connect('local') attaches to this computer's node")
    return _named(cfg, name, token, overrides)


def _from_url(url: str, token: Optional[str], overrides: Dict[str, Any], config_path: Optional[str]) -> Client:
    """An explicit URL: token = the argument, else HIVE_TOKEN / HIVE_TOKEN_FILE only when HIVE_URL is unset or names
    this same URL (the env credential stays bound to its host, §5.1/C2); HIVE_TLS_PIN only for HIVE_URL itself."""
    from .config import Connection
    from .transport import HttpTransport
    env = dict(os.environ)
    env_url = env.get("HIVE_URL", "").strip()
    same = False
    if env_url:
        try:
            same = HttpTransport._parse_url(env_url)["url"] == HttpTransport._parse_url(url)["url"]
        except HiveError:
            same = False
    tok = token
    if tok is None and (not env_url or same):
        tok = _env_token(env)
    pin = env["HIVE_TLS_PIN"] if (same and env.get("HIVE_TLS_PIN")) else None
    target = _target_for(Connection(url=url, tls_pin=pin), None, tok, None, overrides)
    client = Client(target, default_node=_default_node(None, overrides))
    try:
        client.config = _load_cfg(config_path)
    except HiveError:
        client.config = None
    return client


def connect(target: Optional[str] = None, token: Optional[str] = None, **kw: Any) -> Client:
    """Client for a URL, a connection name, "local" or the client.json default.

    ``kw``: ``ca_file``, ``tls_pin``, ``timeout``, ``proxy`` (override the connection's values), ``default_node``,
    ``config`` (path of client.json). Nothing is sent until the first call.
    """
    unknown = [k for k in kw if k not in _TRANSPORT_KW + ("default_node", "config")]
    if unknown:
        raise bad_request(f"unknown connect() option(s): {', '.join(sorted(unknown))}",
                          hint="options: " + ", ".join(_TRANSPORT_KW + ("default_node", "config")))
    config_path = kw.pop("config", None)
    if target is not None and not isinstance(target, str):
        raise bad_request("connect() target must be a URL, a connection name or 'local'")
    t = (target or "").strip()
    if not t:
        return _from_config(config_path, None, kw, token)
    if t.lower() == "local":
        client = _from_config(config_path, LOCAL_CONNECTION, kw, token)
        tr = getattr(client.target, "transport", None)
        if tr is not None:
            tr.offline_hint = LOCAL_HINT
        return client
    if "://" in t:
        return _from_url(t, token, kw, config_path)
    return _from_config(config_path, t, kw, token)


__all__ = ["Target", "HttpTarget", "HubTarget", "Client", "NodeHandle", "JobHandle", "ServiceHandle", "connect",
           "new_request_id", "split_location", "LOCAL_CONNECTION"]
