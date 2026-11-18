"""Shared fakes for the WP6 tests (tools/render/mcp/cli): a scripted Target and Client."""
from __future__ import annotations

import threading
from typing import Any, Callable, Dict, List, Optional

from hive import catalog
from hive.client import Client
from hive.errors import HiveError


def row(name: str, *, os_label: str = "Linux", mode: str = "direct", online: bool = True, tags=(), gpus=None,
        max_timeout=None, secrets=(), idle_s=None, status=None, description: str = "") -> dict:
    """A NodeSummary row."""
    return {"name": name, "online": online, "mode": mode, "os": "Linux", "os_label": os_label, "arch": "x86_64",
            "version": "1.0.0", "gpus": [], "gpus_summary": gpus, "tags": list(tags), "description": description,
            "wsl": False, "wsl_distros": [], "secrets": list(secrets), "last_seen": None, "last_call_at": None,
            "idle_s": idle_s, "inflight": 0, "queued": 0, "identity_changed": False, "outdated": False,
            "preset": "standard", "auto_update": False, "max_timeout": max_timeout, "status": status or {}}


class FakeTarget:
    """Scripted Target: ``kind`` hub|node, NodeSummary rows, allowed actions, a call handler."""

    def __init__(self, kind: str = "hub", nodes: Optional[List[dict]] = None, actions: Optional[List[str]] = None,
                 approval: tuple = (), approvals: bool = False, name: str = "home",
                 handler: Optional[Callable[..., Any]] = None):
        self.kind = kind
        self.name = name
        self._nodes = nodes if nodes is not None else ([row("desk")] if kind == "node" else [row("desk"),
                                                                                           row("pod-a", mode="relay")])
        self._actions = actions if actions is not None else [n for n, s in catalog.SPECS.items()
                                                             if s.scope == "node"]
        self._approval = set(approval)
        self.approvals = approvals
        self.handler = handler
        self.calls: List[Dict[str, Any]] = []
        self.cancelled: List[str] = []
        self.whoami_calls = 0
        self.lock = threading.Lock()
        self.fail_whoami: Optional[HiveError] = None

    def whoami(self) -> dict:
        self.whoami_calls += 1
        if self.fail_whoami is not None:
            raise self.fail_whoami
        return {"identity": {"name": "agent", "kind": "agent", "classes": ["read"], "read_only": False,
                             "nodes": ["*"], "approver": False, "via": "direct", "agent": ""},
                "server": {"role": self.kind, "name": self.name, "version": "1.0.0", "api": 1},
                "approvals": self.approvals}

    def nodes(self) -> List[dict]:
        return list(self._nodes)

    def actions(self, node: Optional[str] = None) -> List[dict]:
        out = []
        for n in self._actions:
            d = {"name": n, "risk": catalog.SPECS[n].risk, "title": catalog.SPECS[n].title,
                 "approval": n in self._approval}
            if self.kind == "hub":
                d["nodes"] = [r["name"] for r in self._nodes]
            out.append(d)
        return out

    def call(self, action: str, params: Optional[dict], *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None,
             approval_id: Optional[str] = None) -> dict:
        with self.lock:
            self.calls.append({"action": action, "params": dict(params or {}), "node": node, "timeout": timeout,
                               "request_id": request_id, "approval_id": approval_id})
        if self.handler is not None:
            return self.handler(action, params or {}, node=node, timeout=timeout, approval_id=approval_id)
        return {}

    def cancel(self, request_id: str) -> bool:
        self.cancelled.append(request_id)
        return True


def fake_client(**kw: Any) -> Client:
    """Client over a FakeTarget."""
    return Client(FakeTarget(**kw))
