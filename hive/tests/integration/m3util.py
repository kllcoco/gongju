"""Shared helpers for the M3 end-to-end tests (§22.1 step 6: model.pull, netproxy routes, docker preset, WSL
keepalive, the remaining tools).

* ``NodeDaemon``: a real node (``hive init node`` + ``hive node run`` under the restart-on-75 ``Supervisor`` of
  ``m2util``) in a temp ``HIVE_HOME``/``HOME``, with the agent (``this-pc``) and owner (``this-pc-owner``) tokens
  that ``init`` wrote, the CLI, the Python client and MCP stdio sessions bound to it.
* ``McpSession``: ``initialize`` + ``tools/call`` helper around ``tests.test_mcp.McpProc``.
* ``ensure_fake_dns(dir)``: a ``sitecustomize`` that answers ``getaddrinfo`` for the names in env ``HIVE_TEST_FAKE_DNS``
  (JSON ``{name: [ip, …]}``) and leaves every other name to the real resolver. Put on ``PYTHONPATH`` of a node, it
  reaches the node and the ``_download`` children (``self_env()`` keeps ``PYTHONPATH``), emulating a Clash/Mihomo
  fake-ip DNS (``198.18.0.0/15``) or a name that resolves to a LAN address, without touching the system resolver.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from hive import client as hclient, proctree, util

from tests import helpers
from tests.integration import m2util
from tests.integration.m2util import Supervisor, http_json, wait_for
from tests.test_mcp import McpProc

IS_WINDOWS = os.name == "nt"
ROOT = m2util.ROOT

SITECUSTOMIZE = '''\
"""Test-only fake DNS (tests/integration/m3util.py): env HIVE_TEST_FAKE_DNS = JSON {name: [ip, ...]}."""
import json as _json
import os as _os
import socket as _socket

_MAP = {}
try:
    _MAP = {str(k).lower(): list(v) for k, v in _json.loads(_os.environ.get("HIVE_TEST_FAKE_DNS") or "{}").items()}
except ValueError:
    _MAP = {}

if _MAP:
    _real = _socket.getaddrinfo

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode() if isinstance(host, bytes) else host
        ips = _MAP.get(str(name or "").lower().rstrip("."))
        if ips is None:
            return _real(host, port, family, type, proto, flags)
        if isinstance(port, str):
            port = int(port) if port.isdigit() else 0
        out = []
        for ip in ips:
            fam = _socket.AF_INET6 if ":" in ip else _socket.AF_INET
            if family not in (0, fam):
                continue
            addr = (ip, port or 0, 0, 0) if fam == _socket.AF_INET6 else (ip, port or 0)
            out.append((fam, type or _socket.SOCK_STREAM, proto or 6, "", addr))
        if not out:
            raise _socket.gaierror(_socket.EAI_NONAME, "Name or service not known")
        return out

    _socket.getaddrinfo = getaddrinfo
'''


def ensure_fake_dns(directory: Path) -> Path:
    """Write the fake-DNS ``sitecustomize.py`` into ``directory`` (created); returns it (for ``PYTHONPATH``)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sitecustomize.py").write_text(SITECUSTOMIZE, encoding="utf-8")
    return directory


class McpSession:
    """One ``hive mcp`` stdio subprocess; ``tool(name, args)`` returns ``(text, is_error)``."""

    def __init__(self, argv: Sequence[str], env: Dict[str, str]):
        self.p = McpProc(list(argv), env=env)
        self.mid = 0
        self.init = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "m3", "version": "1"}})
        self.p.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def request(self, method: str, params: Optional[dict] = None, timeout: float = 120) -> dict:
        self.mid += 1
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": self.mid, "method": method}
        if params is not None:
            msg["params"] = params
        self.p.send(msg)
        r = self.p.recv(self.mid, timeout=timeout)
        if "error" in r:
            raise AssertionError(f"{method}: {r['error']}")
        return r["result"]

    def tools(self) -> List[dict]:
        return self.request("tools/list", {})["tools"]

    def tool(self, name: str, args: dict, timeout: float = 150) -> "tuple[str, bool]":
        res = self.request("tools/call", {"name": name, "arguments": args}, timeout=timeout)
        text = "\n".join(c.get("text", "") for c in res.get("content", []))
        if os.environ.get("HIVE_TEST_SHOW"):
            print(f"\n>>> {name} {json.dumps(args)}\n{text}")
        return text, bool(res.get("isError"))

    def close(self) -> None:
        try:
            self.p.close()
        except Exception:  # noqa: BLE001 - best effort
            try:
                self.p.p.kill()
            except OSError:
                pass


class NodeDaemon:
    """A real node daemon in a temp tree. ``start()`` runs ``hive init node`` then ``hive node run``.

    ``cfg_edit(raw)`` edits the written config before the first start; ``path_prepend`` dirs go first on the
    daemon's PATH (fake tools; they must be outside every fs root to be on the trusted PATH); ``env_extra`` is added
    to the daemon's environment only (not to the CLI's)."""

    def __init__(self, name: str = "m3", *, preset: str = "standard", init_args: Sequence[str] = (),
                 cfg_edit: Any = None, path_prepend: Sequence[str] = (), env_extra: Optional[Dict[str, str]] = None,
                 pythonpath_extra: Sequence[str] = ()):
        self.name = name
        self.base = Path(tempfile.mkdtemp(prefix=f"hive{name}-")).resolve()
        self.home = self.base / "home"       # HIVE_HOME
        self.user = self.base / "user"       # HOME (fs root "~")
        self.work = self.base / "work"       # extra rw root
        for d in (self.home, self.user, self.work):
            d.mkdir()
        self.preset = preset
        self.init_args = list(init_args)
        self.cfg_edit = cfg_edit
        pp = [str(ROOT)] + [str(x) for x in pythonpath_extra]
        if os.environ.get("PYTHONPATH"):
            pp.append(os.environ["PYTHONPATH"])
        self.env = m2util.base_env(HIVE_HOME=str(self.home), HOME=str(self.user), PYTHONPATH=os.pathsep.join(pp))
        if IS_WINDOWS:  # pragma: no cover - Windows only
            self.env["USERPROFILE"] = str(self.user)
        self.node_env = dict(self.env)
        if path_prepend:
            self.node_env["PATH"] = os.pathsep.join([str(x) for x in path_prepend] + [os.environ.get("PATH", "")])
        self.node_env.update(env_extra or {})
        self.port = 0
        self.url = ""
        self.sup: Optional[Supervisor] = None
        self.agent = self.owner = ""
        self._clients: List[Any] = []
        self._sessions: List[McpSession] = []
        self.extra_pids: List[int] = []
        self.cleanup_panic = True

    @property
    def cfg_path(self) -> Path:
        return self.home / "node" / "config.json"

    # ------------------------------------------------------------------------------------------------ lifecycle
    def start(self) -> "NodeDaemon":
        try:
            self.port = helpers.free_port()
            self.url = f"http://127.0.0.1:{self.port}"
            self.ok(self.cli("init", "node", "--yes", "--name", self.name, "--preset", self.preset, "--listen",
                             f"127.0.0.1:{self.port}", "--root", str(self.work), *self.init_args), "hive init node")
            m2util.allow_root(self.cfg_path)
            if self.cfg_edit is not None:
                m2util.edit_json(self.cfg_path, self.cfg_edit)
            self.agent = (self.home / "tokens" / "this-pc.token").read_text(encoding="utf-8").strip()
            self.owner = (self.home / "tokens" / "this-pc-owner.token").read_text(encoding="utf-8").strip()
            self.run()
        except BaseException:
            self.stop()
            raise
        return self

    def run(self) -> None:
        """(Re)start the daemon and wait until it answers."""
        self.sup = Supervisor([sys.executable, "-m", "hive", "node", "run"], env=self.node_env, cwd=str(ROOT),
                              log=self.base / "node.log", name=self.name).start()
        wait_for(self.healthy, 60, what=f"node {self.name} health: {self.output()}")

    def healthy(self) -> bool:
        if self.sup is not None and not self.sup.running and self.sup.wait(0):
            raise AssertionError(f"node exited {self.sup.exits}: {self.output()}")
        st, res = http_json("GET", self.url + "/v1/health", timeout=2)
        return st == 200 and res["result"]["role"] == "node"

    def output(self, n: int = 4000) -> str:
        return self.sup.output(n) if self.sup is not None else ""

    def stop_daemon(self) -> None:
        if self.sup is not None and self.sup.running:
            r = self.cli("node", "stop")
            if not self.sup.wait(30):
                self.sup.kill()
            if r.returncode != 0 and self.sup.running:
                self.sup.kill()
        elif self.sup is not None:
            self.sup.kill()

    def stop(self) -> None:
        for s in self._sessions:
            s.close()
        self._sessions = []
        for c in self._clients:
            try:
                c.close()
            except Exception:  # noqa: BLE001
                pass
        self._clients = []
        try:
            if self.sup is not None and self.sup.running and self.cleanup_panic:
                self.cli("node", "panic", "--yes", timeout=60)       # stops every job, service and container
        except Exception:  # noqa: BLE001 - best effort
            pass
        if self.sup is not None:
            try:
                self.stop_daemon()
            except Exception:  # noqa: BLE001
                self.sup.kill()
        for pid in self.extra_pids:
            if pid and util.pid_alive(pid):
                try:
                    proctree.kill_tree(pid, force=True, grace=1)
                except Exception:  # noqa: BLE001
                    pass
        if os.environ.get("HIVE_TEST_KEEP"):
            print("kept", self.base)
        else:
            shutil.rmtree(str(self.base), ignore_errors=True)

    # ------------------------------------------------------------------------------------------------ access
    def cli(self, *argv: str, stdin: Optional[str] = None, timeout: float = 120) -> Any:
        return m2util.run_cli(argv, env=self.env, stdin=stdin, timeout=timeout)

    @staticmethod
    def ok(r: Any, what: str) -> str:
        if r.returncode != 0:
            raise AssertionError(f"{what} failed ({r.returncode}): {r.stdout}{r.stderr}")
        return r.stdout

    def client(self, token: Optional[str] = None) -> Any:
        c = hclient.connect(self.url, token=token or self.owner)
        self._clients.append(c)
        return c

    def call(self, action: str, params: Optional[dict] = None, *, token: Optional[str] = None) -> dict:
        """Owner (default) call through the Python client; raises HiveError."""
        return self.client(token).call(action, params or {})

    def mcp(self, *extra: str) -> McpSession:
        s = McpSession(["--local", *extra], env=self.env)
        self._sessions.append(s)
        return s

    def secret_set(self, name: str, value: str, *hosts_args: str) -> None:
        self.ok(self.cli("node", "secret", "set", name, *hosts_args, stdin=value + "\n"), f"secret set {name}")

    def restart_daemon(self) -> None:
        """Stop (owner ``hive node stop``) and start again: config edits take effect (§8.6)."""
        self.stop_daemon()
        self.run()


def job_id_of(text: str) -> str:
    m = re.search(r"\b(j_[A-Za-z0-9_-]{6,40})\b", text)
    if not m:
        raise AssertionError(f"no job id in: {text}")
    return m.group(1)


def wait_job(node: NodeDaemon, job_id: str, timeout: float = 90) -> dict:
    """Poll ``job.status`` (owner) until the job leaves starting/running."""
    end = time.monotonic() + timeout
    st: dict = {}
    while time.monotonic() < end:
        st = node.call("job.status", {"job_id": job_id, "wait": 5})
        if st["state"] not in ("starting", "running"):
            return st
    raise AssertionError(f"job {job_id} still {st.get('state')}: {st}")


__all__ = ["NodeDaemon", "McpSession", "ensure_fake_dns", "job_id_of", "wait_job", "http_json", "wait_for",
           "IS_WINDOWS", "ROOT"]
