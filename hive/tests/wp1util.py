"""Shared fixtures for the WP1 tests (not a test module): an isolated fake home, policy builders, HTTP helpers."""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from unittest import mock

from hive import config, util
from hive.auth import Identity
from hive.policy import Policy
from hive.secretstore import SecretStore
from tests import helpers

# keep WARNING lines of the code under test (root user, tool routes not merged…) out of the test output
import logging as _logging
_logging.getLogger("hive").addHandler(_logging.NullHandler())

ALL = ("read", "write", "exec", "net", "persist")


def ident(classes=ALL, **kw: Any) -> Identity:
    """Identity with defaults (agent 'a', direct)."""
    kw.setdefault("name", "a")
    kw.setdefault("kind", "agent")
    return Identity(classes=frozenset(classes), **kw)


OPERATOR = ident(name="op")
FILES = ident(("read", "write"), name="files")
READNET = ident(("read", "net"), name="readnet")
READONLY = ident(("read",), name="ro")
CAUTIOUS = ident(name="cautious", require_approval=("shell.run", "job.start", "service.define", "service.start",
                                                    "service.restart", "service.remove", "fs.delete", "fs.move",
                                                    "proc.kill"))


class FakeHome:
    """A temp dir used as HOME (and HIVE_HOME below it is NOT used: hive home is a sibling)."""

    def __init__(self) -> None:
        self.base = Path(tempfile.mkdtemp(prefix="hivewp1-")).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.hive_home = self.base / "hivehome"
        self.data = self.hive_home / "node"
        self.data.mkdir(parents=True)
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home), "HIVE_HOME": str(self.hive_home)})

    def __enter__(self) -> "FakeHome":
        self._env.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._env.stop()
        shutil.rmtree(self.base, ignore_errors=True)

    def p(self, rel: str) -> Path:
        """Path below the fake home."""
        return self.home / rel

    def touch(self, rel: str, data: str = "x") -> Path:
        """Create a file below the fake home."""
        path = self.home / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        return path


def node_config(fh: FakeHome, preset: str = "standard", **overrides: Any) -> config.NodeConfig:
    """Parsed node config (placeholders expanded) with data_dir in the fake hive home."""
    raw = helpers.make_node_config(preset, data_dir=str(fh.data), **overrides)
    return config._parse_node(raw, fh.data / "config.json")


def build_policy(fh: FakeHome, preset: str = "standard", *, policy: Optional[dict] = None, guard: Any = None,
                 is_wsl: bool = False, protected: Tuple[list, list] = ([], []), secrets: Optional[SecretStore] = None,
                 **overrides: Any) -> Policy:
    """A real Policy for the fake home; NetGuard replaced by helpers.FakeNetGuard (or ``guard``)."""
    cfg = node_config(fh, preset, policy=policy or {}, **overrides)
    st = secrets or SecretStore(fh.data / "secrets.json", passthrough=(), env={})
    with mock.patch("hive.netutil.NetGuard", guard or helpers.FakeNetGuard):
        return Policy(cfg.policy, workdir=Path(cfg.workdir), data_dir=fh.data, hive_home=fh.hive_home,
                      protected=protected, network=cfg.network, models=cfg.models, secrets=st, gateway=cfg.gateway,
                      is_wsl=is_wsl, model_endpoints=cfg.model_endpoints)


def http(url: str, method: str = "GET", body: Any = None, token: Optional[str] = None,
         headers: Optional[Dict[str, str]] = None, timeout: float = 10) -> Tuple[int, Dict[str, str], Any]:
    """(status, headers, parsed JSON or raw bytes) of one request (urllib)."""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        hdrs.setdefault("Content-Type", "application/json")
    if token:
        hdrs["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status, h = r.status, dict(r.headers.items())
    except urllib.error.HTTPError as e:
        raw = e.read()
        status, h = e.code, dict(e.headers.items())
        e.close()
    try:
        return status, h, json.loads(raw.decode()) if raw else None
    except ValueError:
        return status, h, raw


@contextlib.contextmanager
def node_server(**cfg: Any) -> Iterator[Tuple[str, str, Any]]:
    """helpers.start_node_server in a temp HIVE_HOME; stopped afterwards."""
    with helpers.tmp_home():
        url, token, rt = helpers.start_node_server(**cfg)
        try:
            yield url, token, rt
        finally:
            rt.test_stop()


def raw_request(port: int, data: bytes, *, host: str = "127.0.0.1", read: bool = True,
                timeout: float = 5) -> bytes:
    """Send raw bytes on a fresh TCP connection and return what the server answered."""
    import socket
    s = socket.create_connection((host, port), timeout=timeout)
    try:
        s.sendall(data)
        if not read:
            return b""
        out = b""
        while True:
            try:
                chunk = s.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
            if b"\r\n\r\n" in out:
                head, _, rest = out.partition(b"\r\n\r\n")
                cl = [line for line in head.split(b"\r\n") if line.lower().startswith(b"content-length:")]
                if cl and len(rest) >= int(cl[0].split(b":")[1]):
                    break
        return out
    finally:
        s.close()


__all__ = ["ident", "OPERATOR", "FILES", "READNET", "READONLY", "CAUTIOUS", "FakeHome", "node_config",
           "build_policy", "http", "node_server", "raw_request", "util"]
