"""Shared fixtures for the WP2 tests (not a test module): a real NodeRuntime in a fake home, manager-level fakes,
identities and a direct handler-call helper."""
from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock

from hive import catalog, config, schema, util
from hive.actions import HANDLERS, load_all
from hive.auth import Identity
from hive.errors import HiveError
from tests import helpers

logging.getLogger("hive").addHandler(logging.NullHandler())

ALL = ("read", "write", "exec", "net", "persist")
CAUTIOUS_SET = ("shell.run", "job.start", "service.define", "service.start", "service.restart", "service.remove",
                "fs.delete", "fs.move", "proc.kill")
PY = sys.executable


def ident(classes=ALL, **kw: Any) -> Identity:
    """Identity with defaults (agent 'op', direct)."""
    kw.setdefault("name", "op")
    kw.setdefault("kind", "agent")
    kw.setdefault("token_sha256", "sha256:" + "0" * 64)
    return Identity(classes=frozenset(classes), **kw)


OPERATOR = ident()
CAUTIOUS = ident(name="cautious", require_approval=CAUTIOUS_SET)
FILES = ident(("read", "write"), name="files")
OWNER = Identity(name="owner", kind="owner", classes=frozenset(ALL), source_ip="127.0.0.1")


class Node:
    """A started real NodeRuntime (WP1) with the WP2 managers, HOME and HIVE_HOME in a temp dir.

    ``home`` is the fake home (fs root ``~`` rw and the workdir); ``rt`` the runtime.
    """

    def __init__(self, preset: str = "standard", *, start_managers: bool = True, **overrides: Any):
        self.base = Path(tempfile.mkdtemp(prefix="hivewp2-")).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.hive_home = self.base / "hivehome"
        self.data = self.hive_home / "node"
        self.data.mkdir(parents=True)
        self.preset = preset
        self.overrides = overrides
        self.start_managers = start_managers
        self._env = mock.patch.dict(os.environ, {"HOME": str(self.home), "HIVE_HOME": str(self.hive_home),
                                                 "USERPROFILE": str(self.home)})
        self.rt: Any = None

    def __enter__(self) -> "Node":
        self._env.start()
        try:
            self.rt = self.make_runtime()
        except BaseException:
            self._env.stop()
            shutil.rmtree(self.base, ignore_errors=True)
            raise
        return self

    def make_runtime(self) -> Any:
        """Build and start a NodeRuntime on this home (a second one adopts the first one's jobs/services)."""
        from hive import node
        raw = helpers.make_node_config(self.preset, data_dir=str(self.data), **self.overrides)
        path = self.data / "config.json"
        util.write_json_atomic(path, raw, private=True)
        cfg = config.load_node_config(str(path))
        rt = node.NodeRuntime(cfg, config_path=path, start_managers=self.start_managers)
        rt.start()
        return rt

    def stop_runtime(self) -> None:
        """Stop the runtime (jobs and services keep running)."""
        if self.rt is not None:
            self.rt.stop()
            self.rt = None

    def restart_runtime(self) -> Any:
        """Stop the runtime and start a new one (adoption)."""
        self.stop_runtime()
        self.rt = self.make_runtime()
        return self.rt

    def __exit__(self, *exc: Any) -> None:
        try:
            if self.rt is not None:
                try:
                    for name in list(getattr(self.rt.services, "_defs", {})):
                        try:
                            self.rt.services._stop_now(name, desired="stopped", manual_flag=False)
                        except Exception:  # noqa: BLE001
                            pass
                    end = time.monotonic() + 20
                    while time.monotonic() < end and any(self.rt.services._alive(n)
                                                         for n in list(self.rt.services._defs)):
                        time.sleep(0.1)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    for jid, r in list(self.rt.jobs._jobs.items()):
                        if r.get("state") in ("starting", "running"):
                            self.rt.jobs.stop(jid, force=True)
                except Exception:  # noqa: BLE001
                    pass
                self.rt.stop()
        finally:
            self._env.stop()
            shutil.rmtree(self.base, ignore_errors=True)

    # ---------------------------------------------------------------------------------------------- calls
    def call(self, action: str, params: Optional[dict] = None, *, identity: Identity = OPERATOR,
             **kw: Any) -> dict:
        """The full envelope of NodeRuntime.call."""
        return self.rt.call(identity, action, params or {}, **kw)

    def ok(self, action: str, params: Optional[dict] = None, *, identity: Identity = OPERATOR, **kw: Any) -> dict:
        """The result of a successful call (AssertionError with the error otherwise)."""
        env = self.call(action, params, identity=identity, **kw)
        if not env.get("ok"):
            raise AssertionError(f"{action} failed: {env.get('error')}")
        return env["result"]

    def err(self, action: str, params: Optional[dict] = None, *, identity: Identity = OPERATOR,
            **kw: Any) -> dict:
        """The error dict of a failing call (AssertionError when it succeeded)."""
        env = self.call(action, params, identity=identity, **kw)
        if env.get("ok"):
            raise AssertionError(f"{action} unexpectedly succeeded: {env.get('result')}")
        return env["error"]

    def approve(self, approval_id: str, **kw: Any) -> dict:
        """Owner approval of a pending request."""
        return self.rt.approvals.decide(approval_id, True, OWNER, **kw)

    def run_approved(self, action: str, params: dict, *, identity: Identity = CAUTIOUS) -> dict:
        """approval_required -> approve -> retry; returns the retry envelope."""
        e = self.err(action, params, identity=identity)
        if e["code"] != "approval_required":
            raise AssertionError(f"expected approval_required, got {e}")
        self.approve(e["details"]["approval_id"])
        return self.call(action, params, identity=identity, approval_id=e["details"]["approval_id"])


class FakeRt(helpers.FakeRuntime):
    """FakeRuntime + ``effective()`` (FakeEffectivePolicy) so system spawns work in manager unit tests."""

    def __init__(self, **kw: Any):
        super().__init__(**kw)
        self.paused = False

    def effective(self, identity: Identity) -> Any:
        return helpers.FakeEffectivePolicy(identity, workdir=self.workdir, data_dir=self.data_dir)

    def is_paused(self) -> bool:
        return self.paused


def handler_call(action: str, params: dict, *, runtime: Any, identity: Optional[Identity] = None,
                 timeout: float = 30, policy: Any = None) -> dict:
    """Call a handler directly with validated params (FakeEffectivePolicy unless ``policy``)."""
    load_all()
    ctx = helpers.make_ctx(action, runtime=runtime, identity=identity, policy=policy, timeout=timeout)
    ctx.given = frozenset(k for k, v in params.items() if v is not None)
    p = schema.validate(catalog.SPECS[action].params, params)
    return HANDLERS[action](ctx, p)


def assert_result_schema(tc: Any, action: str, result: dict) -> None:
    """The result validates against the action's (closed) result schema."""
    try:
        schema.validate(catalog.SPECS[action].result, result, coerce=False, defaults=False)
    except HiveError as e:
        tc.fail(f"{action} result does not match its schema: {e.details}")


def wait_for(fn: Any, timeout: float = 15, interval: float = 0.05) -> Any:
    """Poll until fn() is truthy; returns its value (AssertionError on timeout)."""
    end = time.monotonic() + timeout
    while True:
        v = fn()
        if v:
            return v
        if time.monotonic() >= end:
            raise AssertionError(f"condition not met within {timeout} s")
        time.sleep(interval)


def py(code: str) -> list:
    """argv running Python code."""
    return [PY, "-c", code]


def threads_quiet() -> None:
    """Give daemon threads a moment (used between restarts)."""
    time.sleep(0.05)
    _ = threading.active_count()


__all__ = ["Node", "FakeRt", "ident", "OPERATOR", "CAUTIOUS", "FILES", "OWNER", "handler_call",
           "assert_result_schema", "wait_for", "py", "PY", "Dict"]
