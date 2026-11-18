"""Hub: routing to direct/relay NodeLinks, hub.nodes, hub approvals, relay queues, run_hub() (design §8, §7, §4.6,
§9.10, D3, D11, D13, D29, D49, D54, D55, WP5).

``Hub.call`` is the hub's pipeline (§8.2, order binding): per-agent inflight cap → node resolution (§8.1) → hub-scope
actions → ``link.ensure_fresh()`` → hub checks (catalog / implemented / classes / deny / nodes) → param validation
without defaults → hub approvals with file binding over the link → ``link.call(exec_gated=, bound_files=)`` → audit.
The node stays the final policy enforcement point: everything the hub sends runs there as the hub token identity
(direct) or the relay identity (relay), never as the agent (D3).

Links (§8.3): ``RelayLink`` keeps a queue per relay node served by ``/v1/relay/poll`` (at-most-once delivery, §7.4);
``DirectLink`` talks to the node's ``/v1/call`` with the stored hub token and probes ``/v1/nodes`` every 30 s.
"""
from __future__ import annotations

import dataclasses
import difflib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets as _secrets
import shlex
import sys
import threading
import time
import unicodedata
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, AbstractSet, Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from . import __version__, catalog, schema
from .auth import CAUTIOUS_ACTIONS, Identity, TokenStore, exec_gated as auth_exec_gated
from .errors import (HiveError, bad_request, conflict, forbidden, internal, not_found, rate_limited,
                     timeout as timeout_error, unavailable)
from .util import (ClosingIterator, FileLock, IS_WINDOWS, glob_match, human_bytes, iso_utc, new_id, read_json,
                   secure_mkdir, write_json_atomic)

if TYPE_CHECKING:  # pragma: no cover
    from .config import HubConfig, HubNodeEntry
    from .gateway import GatewayResponse
    from .httpserver import ApiServer, Request

log = logging.getLogger("hive.hub")

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,80}\Z")
PROBE_INTERVAL = 30.0           # DirectLink GET /v1/nodes
FAST_PROBE_MAX = 5.0            # B5e: a direct node not reached yet is probed again after 2, 4, then every 5 s
FAST_PROBE_WINDOW = 600.0       # ... during the hub's first 10 minutes
ACTIONS_INTERVAL = 300.0        # DirectLink GET /v1/actions
FRESH_S = 75.0                  # a direct row older than this is probed synchronously (§8.3 v5)
PROBE_TIMEOUT = 5.0
MAX_TASKS_PER_POLL = 16
SANITIZE_MAX = 200
GPUS_SUMMARY_RE = re.compile(r"^[\w .,×()+/-]{0,80}\Z")
OS_LABELS = ("Windows 10", "Windows 11", "macOS", "Linux (WSL2)", "Linux")
FILE_BIND_ACTIONS = ("shell.run", "job.start", "service.start", "service.restart", "service.define")
BIND_MAX_FILES = 8
HASH_MAX = 16 << 20
SHOW_MAX = 256 << 10
DIST_PER_MIN = 6
DIST_CONCURRENT = 4
DIST_CHUNK = 262144             # bytes per write of a hive.pyz download
_BAD_CHARS_RE = re.compile("[\u0000-\u001f\u007f-\u009f​-‏‪-‮⁠-⁩﻿]")


# ---------------------------------------------------------------------------------------------------------------
# helpers

NODE_ID_RE = re.compile(r"^n_[A-Za-z0-9_-]{4,64}\Z")   # the config.py node_id pattern


def _sanitize(v: Any, depth: int = 0) -> Any:
    """T28: strings capped at 200 chars, C0/C1, bidi and zero-width characters stripped (recursively)."""
    if depth > 12:
        return None
    if isinstance(v, str):
        return _BAD_CHARS_RE.sub("", v)[:SANITIZE_MAX]
    if isinstance(v, dict):
        return {(_sanitize(k, depth + 1) if isinstance(k, str) else k): _sanitize(x, depth + 1)
                for k, x in list(v.items())[:500]}
    if isinstance(v, list):
        return [_sanitize(x, depth + 1) for x in v[:500]]
    if isinstance(v, (int, float, bool)) or v is None:
        return v
    return None


def _err_env(err: HiveError, meta: dict) -> dict:
    return {"ok": False, "error": err.to_dict(), "meta": meta}


def _key_hash(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _same(a: Optional[str], b: Optional[str]) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(str(a), str(b))


def _rewrite_approval(env: dict, node: str, audit: Any = None) -> dict:
    """v7 (T28): a node never issues approval_required for via hub|relay calls; a link turns one into internal."""
    if isinstance(env, dict) and not env.get("ok") and (env.get("error") or {}).get("code") == "approval_required":
        log.warning("node %s returned an approval request it may not issue", node)
        if audit is not None:
            try:
                audit.record(kind="warning", identity=None, ip="", via="", node=node, action=None, params=None,
                             ok=False, code="internal", ms=0, request_id="",
                             extra={"warning": "node returned approval_required"})
            except HiveError:
                pass
        env = dict(env, error=internal("node returned an approval request it may not issue").to_dict())
    return env


def _envelope_result(env: dict) -> dict:
    if env.get("ok"):
        r = env.get("result")
        return r if isinstance(r, dict) else {}
    raise HiveError.from_dict(env.get("error"))


def _os_label(info: dict) -> Optional[str]:
    osn = str(info.get("os") or "")
    wsl = info.get("wsl")
    if isinstance(wsl, dict) and wsl.get("distro"):
        return "Linux (WSL2)"
    if osn == "Windows":
        return "Windows 11" if str(info.get("os_release") or "") == "11" else "Windows 10"
    if osn == "Darwin":
        return "macOS"
    if osn == "Linux":
        return "Linux"
    return None


def _auto_update_off(modes: dict) -> Optional[str]:
    """``modes.auto_update_off`` of a node's info when well-formed (final acceptance B5g), else None."""
    v = modes.get("auto_update_off")
    if isinstance(v, str) and re.match(r"^(not_pod|deny_actions|off|role:[a-z]{1,16})\Z", v):
        return v
    return None


def summary_from_info(name: str, mode: str, info: dict, status: Optional[dict], *, entry: Any) -> dict:
    """NodeSummary row (§9.10) from a relay node's register ``info`` (+ poll status); hub config is authoritative for
    name/mode/tags/description."""
    info = info if isinstance(info, dict) else {}
    gpus = [{"name": g.get("name"), "memory_total_mb": g.get("memory_total_mb")}
            for g in (info.get("gpus") or []) if isinstance(g, dict)]
    gs = info.get("gpus_summary")
    wsl = info.get("wsl") if isinstance(info.get("wsl"), dict) else None
    policy = info.get("policy") if isinstance(info.get("policy"), dict) else {}
    ex = policy.get("exec") if isinstance(policy.get("exec"), dict) else {}
    modes = info.get("modes") if isinstance(info.get("modes"), dict) else {}
    version = info.get("version")
    return {"name": name, "online": False, "mode": mode, "os": info.get("os"), "os_label": _os_label(info),
            "arch": info.get("arch"), "version": version, "gpus": gpus,
            "gpus_summary": gs if isinstance(gs, str) and GPUS_SUMMARY_RE.match(gs) else None,
            "tags": list(getattr(entry, "tags", []) or []), "description": getattr(entry, "description", "") or "",
            "wsl": bool(wsl and wsl.get("distro")),
            "wsl_distros": [str(d) for d in (wsl or {}).get("distros") or []] if wsl else [],
            "secrets": [s.get("name") for s in info.get("secrets") or [] if isinstance(s, dict)],
            "last_seen": None, "last_call_at": None, "idle_s": None, "inflight": 0, "queued": 0,
            "identity_changed": False, "outdated": bool(version) and version != __version__,
            "hub_token_rejected": False,
            "preset": policy.get("preset") if isinstance(policy.get("preset"), str) else None,
            "auto_update": bool(modes.get("auto_update")),
            "auto_update_off": _auto_update_off(modes),
            "max_timeout": ex.get("max_timeout") if isinstance(ex.get("max_timeout"), (int, float)) else None,
            "status": status or {}}


def _unknown_action(action: str, link: "NodeLink") -> HiveError:
    """not_found for an action the hub's catalog lacks (final acceptance B5a): "upgrade the hub" only when the node
    implements it (a newer node); otherwise a typo is likelier: the closest action names and ``hive actions``."""
    name = action[:64]
    known = sorted(set(catalog.SPECS) | set(link.actions()))
    if action in link.actions():
        ver = link.summary().get("version") or "?"
        return not_found(f"unknown action {name!r}",
                         hint=f"node '{link.name}' (hive {ver}) has {name!r} but the hub runs hive {__version__}, "
                              "which does not know it; upgrade the hub")
    sugg = difflib.get_close_matches(action, known, n=3, cutoff=0.6)
    hint = ("did you mean " + " or ".join(f"'{x}'" for x in sugg) + "? " if sugg else "") + \
        "list the actions: hive actions"
    ver = link.summary().get("version")
    if ver and ver != __version__:
        hint += f" (the hub runs hive {__version__}, node '{link.name}' hive {ver})"
    return HiveError("not_found", f"unknown action {name!r}", {"suggestions": sugg}, hint=hint)


# ---------------------------------------------------------------------------------------------------------------
# links (§8.3)

class NodeLink(ABC):
    """A hub's link to one node (§8.3)."""

    name: str
    mode: str

    def __init__(self, hub: "Hub", entry: "HubNodeEntry"):
        self.hub = hub
        self.entry = entry
        self.name = entry.name
        self.aliases = [a.lower() for a in (entry.aliases or [])]
        self.last_call_at: Optional[float] = None

    @property
    def node_id(self) -> str:
        """The node's node_id once known ("" before)."""
        return ""

    @abstractmethod
    def online(self) -> bool:
        """Node reachable."""

    @abstractmethod
    def summary(self) -> dict:
        """NodeSummary row."""

    @abstractmethod
    def actions(self) -> List[str]:
        """Implemented actions."""

    @abstractmethod
    def catalog(self) -> List[dict]:
        """Public dicts of the node's actions."""

    @abstractmethod
    def call(self, identity: Identity, action: str, params: dict, *, timeout: float, request_id: str,
             exec_gated: bool, bound_files: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """Envelope; never raises for remote errors."""

    @abstractmethod
    def cancel(self, request_id: str) -> None:
        """Cancel a call."""

    @abstractmethod
    def ensure_fresh(self) -> None:
        """DirectLink synchronous probe; RelayLink no-op."""

    def preset(self) -> Optional[str]:
        """NodeSummary ``preset`` (None = not known yet: the hub treats it as cautious, §8.3 v5)."""
        try:
            p = self.summary().get("preset")
        except Exception:  # noqa: BLE001
            return None
        return p if isinstance(p, str) else None

    def os_name(self) -> Optional[str]:
        """NodeSummary ``os`` (None = unknown)."""
        try:
            o = self.summary().get("os")
        except Exception:  # noqa: BLE001
            return None
        return o if isinstance(o, str) else None

    def stop(self) -> None:
        """Release resources (hub stop)."""

    def _common(self, row: dict) -> dict:
        row = dict(row)
        row["name"] = self.name
        row["mode"] = self.mode
        row["tags"] = list(self.entry.tags or [])
        row["description"] = self.entry.description or ""
        row["last_call_at"] = iso_utc(self.last_call_at) if self.last_call_at else None
        row["idle_s"] = round(time.time() - self.last_call_at, 1) if self.last_call_at else None
        return row


class _Task:
    """One relay task (§7.2)."""

    __slots__ = ("task_id", "request_id", "action", "params", "agent", "exec_gated", "bound_files", "mutating",
                 "deadline", "created", "pickup_deadline", "delivered_at", "timeout_s", "session", "envelope",
                 "done", "absent_since", "forward", "busy_seen", "size")

    def __init__(self, request_id: str, action: str, params: dict, agent: str, exec_gated: bool,
                 bound_files: Optional[dict], mutating: bool, deadline: float, pickup: float):
        now = time.monotonic()
        self.task_id = "t_" + new_id()
        self.request_id = request_id
        self.action = action
        self.params = params
        self.agent = agent
        self.exec_gated = exec_gated
        self.bound_files = bound_files
        self.mutating = mutating
        self.deadline = deadline
        self.created = now
        self.pickup_deadline = now + pickup
        self.delivered_at: Optional[float] = None
        self.timeout_s = 0.0
        self.session: Optional[str] = None
        self.envelope: Optional[dict] = None
        self.done = threading.Event()
        self.absent_since: Optional[float] = None
        self.forward = action == "model.forward"
        self.busy_seen = False
        self.size = 0

    def wire(self) -> dict:
        return {"task_id": self.task_id, "request_id": self.request_id, "action": self.action, "params": self.params,
                "timeout_s": round(self.timeout_s, 3), "agent": self.agent, "exec_gated": self.exec_gated,
                "bound_files": self.bound_files}


class RelayLink(NodeLink):
    """Relay queue/pending per node (§7.4)."""

    mode = "relay"

    def __init__(self, hub: "Hub", entry: "HubNodeEntry"):
        super().__init__(hub, entry)
        self.cfg = hub.cfg.relay
        self.lock = threading.RLock()
        self.cond = threading.Condition(self.lock)
        self.queue: List[_Task] = []
        self.delivered: Dict[str, _Task] = {}
        self.by_rid: Dict[str, _Task] = {}
        self.cancels: List[str] = []
        self.session_id: Optional[str] = None
        self.info: dict = {}
        self.actions_list: List[str] = []
        self._node_id = ""
        self.max_parallel = 8
        self.status_row: dict = {}
        self.last_poll_end: Optional[float] = None    # monotonic
        self.last_seen_wall: Optional[float] = None
        self.polling = 0
        self.poll_gen = 0
        self.refresh: Optional[str] = None
        self.identity_changed = False
        self.gone: Optional[str] = None               # why the node unregistered (B5c); None after a register

    @property
    def node_id(self) -> str:
        """node_id from register (display only)."""
        return self._node_id

    def online(self) -> bool:
        """A poll is held now, or the last one ended within ``offline_after``."""
        with self.lock:
            if self.session_id is None:
                return False
            if self.polling > 0:
                return True
            return self.last_poll_end is not None and time.monotonic() - self.last_poll_end <= self.cfg.offline_after

    def summary(self) -> dict:
        """NodeSummary row from register info + poll status."""
        with self.lock:
            row = summary_from_info(self.name, "relay", self.info, self.status_row, entry=self.entry)
            row["inflight"] = len(self.delivered)
            row["queued"] = len(self.queue)
            row["identity_changed"] = self.identity_changed
            row["last_seen"] = iso_utc(self.last_seen_wall) if self.last_seen_wall else None
            if not self.info:
                row["outdated"] = False
        row["online"] = self.online()
        return self._common(row)

    def actions(self) -> List[str]:
        """Register ``actions``."""
        with self.lock:
            return list(self.actions_list)

    def catalog(self) -> List[dict]:
        """catalog.public_dict of the implemented actions."""
        return [catalog.public_dict(catalog.SPECS[n]) for n in self.actions() if n in catalog.SPECS]

    def ensure_fresh(self) -> None:
        """Register info is current; an offline or identity-changed node is refused here, before the hub checks
        (§7.4: immediate ``unavailable``)."""
        with self.lock:
            if self.identity_changed:
                raise self._identity_error()
            if not self.online():
                raise self._offline_error()

    def _identity_error(self) -> HiveError:
        return unavailable(f"node {self.name} registered from a different machine; nothing is routed to it",
                           reason="identity_changed", maybe_executed=False,
                           hint=f"a different machine registered as {self.name}; if you did not replace it, run "
                                f"hive hub rotate-token {self.name}; if you did, hive hub ack-node {self.name}")

    def _offline_error(self) -> HiveError:
        last = iso_utc(self.last_seen_wall) if self.last_seen_wall else None
        gone = getattr(self, "gone", None)
        if gone:
            hint = {"panic": "the node was stopped with hive node panic; on that machine: hive node resume",
                    "paused": "the node turned its relay off (relay.disabled); on that machine: hive node resume"
                    }.get(gone, "the node stopped; start it again on that machine (hive node run, or its service)")
            return unavailable(f"node {self.name} is offline ({gone})", reason="offline", maybe_executed=False,
                               last_seen=last, hint=hint)
        return unavailable(f"node {self.name} is offline", reason="offline", maybe_executed=False, last_seen=last,
                           hint="the relay node is not polling the hub; check the pod")

    # ------------------------------------------------------------------------------------------- agent side
    def call(self, identity: Identity, action: str, params: dict, *, timeout: float, request_id: str,
             exec_gated: bool, bound_files: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """Queue a task and wait for its result (§7.4)."""
        spec = catalog.SPECS.get(action)
        mutating = bool(spec.mutating) if spec is not None else True
        meta = {"request_id": request_id, "node": self.name, "action": action, "duration_ms": 0, "api": 1}
        with self.lock:
            if self.identity_changed:
                return _err_env(self._identity_error(), meta)
            if not self.online():
                return _err_env(self._offline_error(), meta)
            if len(self.queue) >= int(self.cfg.max_queue_per_node):
                return _err_env(rate_limited(f"the queue for node {self.name} is full", retry_after=5,
                                             reason="queue_full"), meta)
            task = _Task(request_id, action, params, identity.name if identity.kind != "models" else
                         f"gw/{identity.name}", exec_gated, bound_files, mutating,
                         time.monotonic() + float(timeout), float(self.cfg.pickup_timeout))
            agent = identity.name if identity.kind != "models" else f"gw/{identity.name}"
            task.agent = agent[:64]
            self.queue.append(task)
            self.by_rid[request_id] = task
            self.cond.notify_all()
        cap = float(timeout) + float(self.cfg.pickup_timeout) + float(self.cfg.relay_grace) + \
            float(self.cfg.lost_grace) + 30.0
        task.done.wait(cap)
        with self.lock:
            if task.envelope is None:
                self._finish(task, timeout_error(f"no result from {self.name}", maybe_executed=mutating))
            self.by_rid.pop(request_id, None)
            env = task.envelope
        assert env is not None
        return _rewrite_approval(env, self.name, self.hub.audit)

    def cancel(self, request_id: str) -> None:
        """Queued: drop it (cancelled); delivered: queue a relay cancel."""
        with self.lock:
            task = self.by_rid.get(request_id)
            if task is None or task.envelope is not None:
                return
            if task.delivered_at is None:
                if task in self.queue:
                    self.queue.remove(task)
                self._finish(task, HiveError("cancelled", "cancelled before the node picked it up"))
            else:
                self.cancels.append(task.task_id)
                self.cond.notify_all()

    def _finish(self, task: _Task, err: Optional[HiveError] = None, env: Optional[dict] = None) -> None:
        """Complete a task (lock held)."""
        if task.envelope is not None:
            return
        if env is None:
            assert err is not None
            env = _err_env(err, {"request_id": task.request_id, "node": self.name, "action": task.action,
                                 "duration_ms": int((time.monotonic() - task.created) * 1000), "api": 1})
        task.envelope = env
        self.delivered.pop(task.task_id, None)
        if task in self.queue:
            self.queue.remove(task)
        task.done.set()
        self.cond.notify_all()

    # ------------------------------------------------------------------------------------------- node side
    def register(self, body: dict, ip: str) -> dict:
        """Register a session (§7.1); identity binding against relay_ids.json (D54)."""
        key = body.get("instance_key")
        key = key if isinstance(key, str) and key.startswith("ik_") and len(key) <= 200 else None
        kh = _key_hash(key) if key else None
        info = _sanitize(body.get("info") if isinstance(body.get("info"), dict) else {})
        hostname = info.get("hostname") if isinstance(info.get("hostname"), str) else None
        cloud = ((info.get("os") or {}) if isinstance(info.get("os"), dict) else {}).get("cloud")
        pod_id = cloud.get("pod_id") if isinstance(cloud, dict) and isinstance(cloud.get("pod_id"), str) else None
        ids = self.hub.relay_ids
        with self.lock:
            rec = ids.get(self.name)
            changed = False
            key_ok = True
            note: Optional[dict] = None
            if rec.get("key_sha256") is None and kh is not None:
                rec.update(key_sha256=kh, bound_at=iso_utc(), bound_seen_at=iso_utc(), pending_sha256=None,
                           pending=None)
                self.identity_changed = False
                changed = True
            elif kh is not None and _same(rec.get("key_sha256"), kh):
                if rec.get("pending_sha256") or self.identity_changed:
                    note = {"event": "identity recovered", "discarded": rec.get("pending")}
                rec.update(pending_sha256=None, pending=None, bound_seen_at=iso_utc())
                self.identity_changed = False
                changed = True
            else:
                if self.online() and not self.identity_changed:
                    raise conflict(f"node {self.name} is online from another instance",
                                   hint="stop the other instance, or rotate the relay token: hive hub rotate-token "
                                        f"{self.name}")
                rec.update(pending_sha256=kh, pending={"ip": ip, "at": iso_utc(), "hostname": hostname,
                                                       "pod_id": pod_id})
                self.identity_changed = True
                changed = True
                key_ok = False
                note = {"event": "identity changed", "ip": ip, "hostname": hostname}
            # Phase 2 review: the node_id is bound like the key (relay_ids.json "node_id"): only a register with the
            # bound key may set or change it, and an id stored for another node is refused even while that node is
            # offline (node_id resolution would otherwise route another node's calls to this pod)
            new_nid, bad_nid = self._vet_node_id(body.get("node_id"), rec=rec, key_ok=key_ok)
            if new_nid and key_ok and rec.get("node_id") != new_nid:
                rec["node_id"] = new_nid
                changed = True
            if changed:
                ids.save()
            # supersede the old session (§7.1): delivered tasks fail, queued tasks are kept
            old = self.session_id
            for t in list(self.delivered.values()):
                self._finish(t, unavailable(f"node {self.name} re-registered while running the call",
                                            reason="session_replaced", maybe_executed=t.mutating))
            self.cancels = []
            # audit round 3: 192 random bits (was "s_" + new_id(): a millisecond clock + 32 random bits)
            self.session_id = "s_" + _secrets.token_urlsafe(24)
            self.gone = None
            self.info = info
            acts = body.get("actions") if isinstance(body.get("actions"), list) else []
            self.actions_list = [a for a in acts if isinstance(a, str) and len(a) <= 64][:500]
            self._node_id = new_nid
            try:
                self.max_parallel = max(1, min(256, int(body.get("max_parallel") or 8)))
            except (TypeError, ValueError):
                self.max_parallel = 8
            self.last_poll_end = time.monotonic()
            self.last_seen_wall = time.time()
            self.poll_gen += 1
            sid = self.session_id
            self.cond.notify_all()
        if bad_nid:
            self.hub._audit_event("warning", self.name, {"event": "relay node_id refused", "node_id": bad_nid,
                                                         "session": sid})
        if old is not None or note is not None:
            extra = {"event": "relay session replaced" if old else "relay register", "session": sid}
            if note:
                extra.update(note)
            self.hub._audit_event("relay" if note is None else "warning", self.name, extra)
        return {"session_id": sid, "node": self.name, "poll_wait": float(self.cfg.poll_wait),
                "server_time": iso_utc(), "max_result_bytes": int(self.cfg.max_result_bytes)}

    def unregister(self, body: dict) -> dict:
        """Final acceptance B5c: the node says it is going offline (``hive node panic``, pause of the relay, a stop):
        the session ends now, so the node shows offline at once and calls fail fast instead of timing out. Queued
        calls fail (not executed); calls already delivered fail as maybe executed. An unknown or superseded session
        is ignored (a newer instance may hold the node)."""
        sid = body.get("session_id")
        why = body.get("reason") if body.get("reason") in ("panic", "paused", "stop") else "stop"
        with self.lock:
            if not isinstance(sid, str) or not sid or self.session_id is None or \
                    not hmac.compare_digest(sid.encode("utf-8", "replace"), self.session_id.encode("utf-8")):
                return {"unregistered": False}
            self.session_id = None
            self.gone = why
            self.last_seen_wall = time.time()
            for t in list(self.queue):
                self.queue.remove(t)
                self._finish(t, self._offline_error())
            for t in list(self.delivered.values()):
                self._finish(t, unavailable(f"node {self.name} went offline ({why}) while running the call",
                                            reason="offline", maybe_executed=t.mutating))
            self.cancels = []
            self.poll_gen += 1
            self.cond.notify_all()
        self.hub._audit_event("relay", self.name, {"event": "relay unregister", "reason": why})
        return {"unregistered": True}

    def _vet_node_id(self, nid: Any, *, rec: Optional[dict] = None, key_ok: bool = True
                     ) -> Tuple[str, Optional[str]]:
        """(node_id to store, refused value or None). The pod's node_id authenticates nothing (§7.1), so it is kept
        only when well-formed and not equal (case-insensitively) to any configured node name, alias, another
        link's node_id (audit round 3: including the id a direct link learned from its authenticated node.info) or
        a node_id relay_ids.json binds to another node; otherwise a pod could take over routing for another node
        (WP5b review). Phase 2 review: with ``rec`` (this node's relay_ids record) a register without the bound key
        keeps only the bound node_id."""
        if nid is None or nid == "":
            return "", None
        if not isinstance(nid, str) or not NODE_ID_RE.match(nid):
            return "", _sanitize(str(nid))[:80]
        n = nid.lower()
        for l in list(self.hub.links.values()):
            if n == l.name.lower() or n in l.aliases or (l is not self and l.node_id and l.node_id.lower() == n):
                return "", nid
        ids = getattr(self.hub, "relay_ids", None)
        if ids is not None:
            for other, orec in ids.items():
                bound = orec.get("node_id") if isinstance(orec, dict) else None
                if other != self.name and isinstance(bound, str) and bound.lower() == n:
                    return "", nid
        if rec is not None and not key_ok:
            bound = rec.get("node_id")
            if not isinstance(bound, str) or bound.lower() != n:
                return "", (nid if bound else None)
        return nid, None

    def poll(self, body: dict) -> dict:
        """Hold the poll up to ``poll_wait`` until tasks/cancels/refresh exist (§7.2)."""
        sid = body.get("session_id")
        with self.lock:
            if not sid or sid != self.session_id:
                raise conflict("unknown or superseded relay session", hint="register again")
            self.poll_gen += 1
            gen = self.poll_gen
            self.cond.notify_all()          # an older poll returns at once with no tasks
            self.polling += 1
            self.last_seen_wall = time.time()
            st = body.get("status")
            if isinstance(st, dict):
                try:
                    if len(json.dumps(st)) <= 4096:
                        self.status_row = _sanitize(st)
                except (TypeError, ValueError):
                    pass
            rl = body.get("running")
            running = set(x for x in rl if isinstance(x, str)) if isinstance(rl, list) else set()
            now = time.monotonic()
            for t in self.delivered.values():
                if t.session == sid and t.task_id not in running and t.absent_since is None:
                    t.absent_since = now
            try:
                free = max(0, min(MAX_TASKS_PER_POLL, int(body.get("free_slots") or 0)))
            except (TypeError, ValueError):
                free = 0
        end = time.monotonic() + float(self.cfg.poll_wait)
        try:
            with self.lock:
                while True:
                    if gen != self.poll_gen or sid != self.session_id or self.hub.stopping.is_set():
                        return {"tasks": [], "cancel": [], "refresh": False}
                    out = self._take(free, sid)
                    if out is not None:
                        return out
                    left = end - time.monotonic()
                    if left <= 0:
                        return {"tasks": [], "cancel": [], "refresh": False}
                    self.cond.wait(min(left, 1.0))
        finally:
            with self.lock:
                self.polling -= 1
                self.last_poll_end = time.monotonic()
                self.last_seen_wall = time.time()

    def _take(self, free: int, sid: str) -> Optional[dict]:
        """Tasks/cancels/refresh for this poll, or None when there is nothing (lock held)."""
        if self.identity_changed:
            # a foreign instance holds the session (§7.1): it gets no tasks, and neither the restart flag nor
            # cancels meant for the bound instance (they stay for it)
            return None
        refresh = self.refresh
        cancels = list(self.cancels)
        tasks: List[dict] = []
        now = time.monotonic()
        fwd_cap = max(1, self.max_parallel - 2)
        fwd_live = sum(1 for t in self.delivered.values() if t.forward)
        budget = int(self.cfg.max_poll_bytes)
        used = 0
        for t in list(self.queue):
            if len(tasks) >= free:
                t.busy_seen = True
                continue
            if t.forward and fwd_live >= fwd_cap:
                t.busy_seen = True
                continue
            remaining = t.deadline - now
            if remaining <= 0:
                self._finish(t, timeout_error("the call's deadline passed before the node picked it up",
                                              maybe_executed=False))
                continue
            t.timeout_s = remaining
            w = t.wire()
            try:
                size = len(json.dumps(w, ensure_ascii=False, default=str).encode("utf-8"))
            except (TypeError, ValueError):
                size = 0
            if tasks and used + size > budget:
                break       # §7.2: stop adding; this and later tasks stay queued in order (FIFO)
            used += size
            self.queue.remove(t)
            t.delivered_at = now
            t.session = sid
            self.delivered[t.task_id] = t
            if t.forward:
                fwd_live += 1
            tasks.append(w)
        if not tasks and not cancels and not refresh:
            return None
        self.cancels = []
        self.refresh = None
        return {"tasks": tasks, "cancel": cancels, "refresh": refresh or False}

    def expects_result(self, task_id: Any, session_id: Any) -> bool:
        """Audit round 4: True when a result for ``task_id`` in ``session_id`` would be accepted now (checked from
        the X-Hive-Task / X-Hive-Session headers before the body is read)."""
        with self.lock:
            t = self.delivered.get(task_id) if isinstance(task_id, str) else None
            return t is not None and t.session == session_id and t.envelope is None

    def result(self, body: dict) -> dict:
        """Accept a task envelope (§7.3)."""
        sid, tid, env = body.get("session_id"), body.get("task_id"), body.get("envelope")
        with self.lock:
            t = self.delivered.get(tid) if isinstance(tid, str) else None
            if t is None or t.session != sid or t.envelope is not None or not isinstance(env, dict) \
                    or not isinstance(env.get("ok"), bool):
                log.info("relay result for %s not accepted", str(tid)[:40])
                return {"accepted": False}
            meta = env.get("meta") if isinstance(env.get("meta"), dict) else {}
            env = dict(env, meta=dict(meta, node=self.name))
            self._finish(t, env=env)
            self.last_seen_wall = time.time()
        return {"accepted": True}

    def reap(self) -> None:
        """Expiries (§7.4), run every second by the hub reaper."""
        now = time.monotonic()
        with self.lock:
            for t in list(self.queue):
                if now >= t.pickup_deadline:
                    reason = "busy" if (t.busy_seen and self.online()) else "not_polling"
                    self._finish(t, unavailable(f"node {self.name} did not pick up the call in time",
                                                reason=reason, maybe_executed=False,
                                                hint="the node is busy; retry shortly" if reason == "busy" else
                                                "the relay node is not polling; check the pod"))
                elif now >= t.deadline:
                    self._finish(t, timeout_error("the call's deadline passed before the node picked it up",
                                                  maybe_executed=False))
            for t in list(self.delivered.values()):
                assert t.delivered_at is not None
                if now >= t.delivered_at + t.timeout_s + float(self.cfg.relay_grace):
                    self.cancels.append(t.task_id)
                    self._finish(t, timeout_error(f"node {self.name} did not answer in time",
                                                  maybe_executed=t.mutating))
                elif t.absent_since is not None and now >= t.absent_since + float(self.cfg.lost_grace):
                    self._finish(t, unavailable(f"node {self.name} lost the call", reason="lost",
                                                maybe_executed=t.mutating))
            if self.cancels:
                self.cond.notify_all()

    def request_restart(self) -> bool:
        """Set ``refresh:"restart"`` for the next poll; False (nothing queued) when offline or while a foreign
        instance holds the session (``identity_changed``: the bound instance is not the one polling)."""
        with self.lock:
            if not self.online() or self.identity_changed:
                return False
            self.refresh = "restart"
            self.cond.notify_all()
            return True

    def stop(self) -> None:
        """Hub shutdown: fail queued (never executed) and delivered (maybe executed) tasks, wake every poll."""
        with self.lock:
            for t in list(self.queue):
                self._finish(t, unavailable("the hub is shutting down", reason="lost", maybe_executed=False))
            for t in list(self.delivered.values()):
                self._finish(t, unavailable("the hub is shutting down", reason="lost", maybe_executed=t.mutating))
            self.cond.notify_all()


class DirectLink(NodeLink):
    """Transport to a direct node with the stored hub token (§8.3)."""

    mode = "direct"

    def __init__(self, hub: "Hub", entry: "HubNodeEntry"):
        super().__init__(hub, entry)
        from .relay import NetworkTransport
        net = dataclasses.asdict(hub.cfg.network) if dataclasses.is_dataclass(hub.cfg.network) else None
        self.transport = NetworkTransport(str(entry.url), entry.token, ca_file=entry.ca_file, tls_pin=entry.tls_pin,
                                          timeout=30.0, proxy="auto", network=net)
        self.lock = threading.RLock()
        self._row: Optional[dict] = None
        self._row_ok_at: Optional[float] = None      # monotonic of the last successful probe
        self._actions: Optional[List[str]] = None
        self._catalog: List[dict] = []
        self._actions_at: Optional[float] = None
        self._probe_lock = threading.Lock()
        self._probes_done = 0                         # completed probes (success or failure)
        self._probe_error: Optional[HiveError] = None  # error of the last completed probe, None after a success
        self.last_error: Optional[str] = None
        self._node_id = ""

    @property
    def node_id(self) -> str:
        """The node_id the node reported in its authenticated ``node.info`` ("" before the first one, audit round 3);
        a plain attribute read (no lock), so ``RelayLink._vet_node_id`` can read it under a relay link's lock."""
        return self._node_id

    def _learn_node_id(self, timeout: float) -> None:
        """Audit round 3: ``node.info`` over ``/v1/call`` with the hub token (the probe's authenticated channel) for
        the node's node_id; called with the actions refresh (every ``ACTIONS_INTERVAL``). A failed call keeps the id
        already known. A newly learned id is taken away from any relay link that claimed it (``Hub._direct_id``)."""
        with self.lock:
            if "node.info" not in (self._actions or []):
                return
        try:
            status, _h, data = self.transport.request(
                "POST", "/v1/call", {"action": "node.info", "params": {}, "timeout": timeout},
                timeout=timeout + 5, headers={"X-Hive-Agent": "hub-probe"}, raw=True)
            env = json.loads(data.decode("utf-8"))
        except (HiveError, UnicodeDecodeError, ValueError, RecursionError) as e:
            log.info("node.info on %s failed: %s", self.name, getattr(e, "message", e))
            return
        res = env.get("result") if isinstance(env, dict) and env.get("ok") is True else None
        nid = res.get("node_id") if isinstance(res, dict) else None
        if not isinstance(nid, str) or not NODE_ID_RE.match(nid):
            if status == 200 and isinstance(res, dict):
                log.info("node.info on %s carried no valid node_id", self.name)
            return
        if nid == self._node_id:
            return
        self._node_id = nid
        ids = getattr(self.hub, "direct_ids", None)
        if ids is not None:
            ids.put(self.entry, nid)
        self.hub._direct_id(self, nid)

    def _fresh(self) -> bool:
        return self._row_ok_at is not None and time.monotonic() - self._row_ok_at <= FRESH_S

    def online(self) -> bool:
        """Last successful probe within 75 s."""
        with self.lock:
            return self._fresh()

    def probe(self, *, timeout: float = PROBE_TIMEOUT, actions: bool = False) -> None:
        """GET /v1/nodes (and /v1/actions when due or never fetched); raises on failure."""
        with self._probe_lock:
            self._probe_locked(timeout=timeout, actions=actions)

    def _probe_locked(self, *, timeout: float, actions: bool) -> None:
        """``probe`` with ``_probe_lock`` held; records the outcome for callers that waited for it."""
        try:
            r = self.transport.request("GET", "/v1/nodes", timeout=timeout)
            rows = r.get("nodes") if isinstance(r, dict) else None
            if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
                raise internal(f"unexpected /v1/nodes answer from {self.name}")
            row = _sanitize(rows[0])
            need_actions = actions or self._actions is None or self._actions_at is None or \
                time.monotonic() - self._actions_at > ACTIONS_INTERVAL
            if need_actions:
                a = self.transport.request("GET", "/v1/actions", timeout=timeout)
                impl = a.get("implemented") if isinstance(a, dict) else None
                rows_a = a.get("actions") if isinstance(a, dict) else None
                with self.lock:
                    self._actions = [x for x in impl if isinstance(x, str)] if isinstance(impl, list) else \
                        [x.get("name") for x in rows_a or [] if isinstance(x, dict) and x.get("name")]
                    self._catalog = [x for x in rows_a or [] if isinstance(x, dict)]
                    self._actions_at = time.monotonic()
                self._learn_node_id(timeout)
            with self.lock:
                self._row = row
                self._row_ok_at = time.monotonic()
                self.last_error = None
                self._probe_error = None
                self._probes_done += 1
        except HiveError as e:
            with self.lock:
                self.last_error = e.message
                self._probe_error = e
                self._probes_done += 1
            raise

    def ensure_fresh(self) -> None:
        """v5 (fail closed): synchronous probe when the row is missing or older than 75 s."""
        with self.lock:
            if self._row is not None and self._fresh() and self._actions is not None:
                return
            seen = self._probes_done
        # §8.3 v5: one probe in flight per link; callers that waited for it share its outcome
        try:
            with self._probe_lock:
                with self.lock:
                    if self._row is not None and self._fresh() and self._actions is not None:
                        return
                    shared = self._probe_error if self._probes_done != seen else None
                if shared is not None:
                    raise HiveError(shared.code, shared.message)
                self._probe_locked(timeout=PROBE_TIMEOUT, actions=False)
        except HiveError as e:
            raise unavailable(f"node {self.name} is not reachable: {e.message}", reason="connect",
                              maybe_executed=False, hint="is the node running? check its URL and the hub token")

    def summary(self) -> dict:
        """The node's own row with the configured name and mode "direct"."""
        with self.lock:
            row = dict(self._row) if self._row else summary_from_info(self.name, "direct", {}, {}, entry=self.entry)
        row["online"] = self.online()
        with self.lock:
            # final acceptance B5e: before the first check ends the node is "checking", not offline
            row["checking"] = self._probes_done == 0 and not row["online"]
            # FA-C3: the node answers but refuses the hub token (401/403), e.g. "hive node token add hub-…" not run
            # there yet; shown as a NOTE so it does not look like a machine that is switched off
            err = self._probe_error
            row["hub_token_rejected"] = (not row["online"] and err is not None
                                         and err.code in ("unauthorized", "forbidden"))
        row["identity_changed"] = False
        row["outdated"] = bool(row.get("version")) and row.get("version") != __version__
        gs = row.get("gpus_summary")
        row["gpus_summary"] = gs if isinstance(gs, str) and GPUS_SUMMARY_RE.match(gs) else None
        if row.get("os_label") not in OS_LABELS:
            row["os_label"] = None
        row.setdefault("preset", None)
        row.setdefault("max_timeout", None)
        row.setdefault("auto_update", False)
        return self._common(row)

    def actions(self) -> List[str]:
        """The node's ``implemented`` list."""
        with self.lock:
            return list(self._actions or [])

    def catalog(self) -> List[dict]:
        """The node's /v1/actions rows (cached; filtered for the hub token there)."""
        with self.lock:
            return [dict(x) for x in self._catalog]

    def call(self, identity: Identity, action: str, params: dict, *, timeout: float, request_id: str,
             exec_gated: bool, bound_files: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """POST /v1/call with the hub token; ``X-Hive-Agent`` / ``X-Hive-Exec-Gated`` headers."""
        meta = {"request_id": request_id, "node": self.name, "action": action, "duration_ms": 0, "api": 1}
        body: Dict[str, Any] = {"action": action, "params": params, "timeout": timeout, "request_id": request_id}
        if bound_files:
            body["bound_files"] = bound_files
        agent = identity.name if identity.kind != "models" else f"gw/{identity.name}"
        headers = {"X-Hive-Agent": agent[:64]}
        if exec_gated:
            headers["X-Hive-Exec-Gated"] = "1"
        slack = float(self.hub.limits["call_timeout_slack"])
        try:
            status, hdrs, data = self.transport.request("POST", "/v1/call", body, timeout=float(timeout) + slack,
                                                        headers=headers, raw=True)
        except HiveError as e:
            return _err_env(e, meta)
        try:
            env = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            env = None
        if not isinstance(env, dict) or not isinstance(env.get("ok"), bool):
            return _err_env(internal(f"unexpected response from node {self.name} (HTTP {status})"), meta)
        m = env.get("meta") if isinstance(env.get("meta"), dict) else {}
        env["meta"] = dict(m, node=self.name)
        return _rewrite_approval(env, self.name, self.hub.audit)

    def cancel(self, request_id: str) -> None:
        """POST /v1/cancel on the node (background)."""
        def run() -> None:
            try:
                self.transport.request("POST", "/v1/cancel", {"request_id": request_id}, timeout=10)
            except HiveError as e:
                log.info("cancel on %s failed: %s", self.name, e.message)
        threading.Thread(target=run, name=f"cancel-{self.name}", daemon=True).start()

    def open_gateway(self, service: str, method: str, path: str, body: bytes, *, agent: str, timeout: float,
                     cancel: threading.Event) -> "GatewayResponse":
        """Raw GET/POST to ``<node url>/openai/self/<service>/v1/<path>``, streamed back (§8.7)."""
        from .gateway import GatewayResponse, safe_headers
        hdrs = {"X-Hive-Agent": agent[:64], "Accept": "*/*"}
        status, rh, chunks = self.transport.open_stream(method, f"/openai/self/{service}/v1/{path}",
                                                        body if method == "POST" else None, headers=hdrs,
                                                        timeout=timeout, cancel=cancel)
        # audit round 3: the direct node chooses these values; only JSON / SSE / plain-text types and a numeric
        # Retry-After are passed on
        return GatewayResponse(status=status, headers=safe_headers(rh), body=None, chunks=chunks)

    def stop(self) -> None:
        """Close the transport."""
        try:
            self.transport.close()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------------------------------------------
# relay_ids.json (§7.1 v5 shape)

class RelayIds:
    """``<hub data>/state/relay_ids.json``: ``{"v":1,"nodes":{name:{key_sha256, pending_sha256, pending,
    token_sha256, bound_at, bound_seen_at}}}``, written atomically (0600) under the hub's lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.nodes: Dict[str, dict] = {}

    def load(self, entries: Sequence[Any]) -> None:
        """Read the file; reset entries whose token_sha256 differs from the configured one (rotate-token)."""
        try:
            d = read_json(self.path, default=None)
        except HiveError as e:
            log.warning("relay_ids.json unreadable (%s); starting empty", e.message)
            d = None
        nodes = d.get("nodes") if isinstance(d, dict) and isinstance(d.get("nodes"), dict) else {}
        with self.lock:
            self.nodes = {}
            for e in entries:
                rec = nodes.get(e.name) if isinstance(nodes.get(e.name), dict) else None
                if rec is None or rec.get("token_sha256") != e.token_sha256:
                    rec = {"key_sha256": None, "pending_sha256": None, "pending": None,
                           "token_sha256": e.token_sha256, "bound_at": None, "bound_seen_at": None}
                for k in ("key_sha256", "pending_sha256", "pending", "bound_at", "bound_seen_at", "node_id"):
                    rec.setdefault(k, None)
                self.nodes[e.name] = rec
            self.save()

    def items(self) -> List[Tuple[str, dict]]:
        """(name, record) pairs (a snapshot)."""
        with self.lock:
            return list(self.nodes.items())

    def get(self, name: str) -> dict:
        """The (mutable) record of a relay node."""
        with self.lock:
            return self.nodes.setdefault(name, {"key_sha256": None, "pending_sha256": None, "pending": None,
                                                "token_sha256": None, "bound_at": None, "bound_seen_at": None})

    def save(self) -> None:
        """Write the file (0600)."""
        with self.lock:
            try:
                write_json_atomic(self.path, {"v": 1, "nodes": self.nodes}, private=True)
            except OSError as e:
                log.error("cannot write %s: %s", self.path, e)


class DirectIds:
    """``<hub data>/state/direct_ids.json``: ``{"v":1,"nodes":{name:{node_id, url, tls_pin, learned_at}}}``, the
    node_ids direct links learned from an authenticated ``node.info`` (verification fix of audit round 3), so a hub
    restart does not reopen the window in which a relay pod could claim a direct node's id. An entry whose url or
    tls_pin no longer matches the configured node is ignored (the configured node may be another machine)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.lock = threading.RLock()
        self.nodes: Dict[str, dict] = {}

    def load(self, entries: Sequence[Any]) -> Dict[str, str]:
        """Read the file; returns {name: node_id} for the configured direct nodes whose url and pin still match."""
        try:
            d = read_json(self.path, default=None)
        except HiveError as e:
            log.warning("direct_ids.json unreadable (%s); starting empty", e.message)
            d = None
        nodes = d.get("nodes") if isinstance(d, dict) and isinstance(d.get("nodes"), dict) else {}
        out: Dict[str, str] = {}
        with self.lock:
            self.nodes = {}
            for e in entries:
                rec = nodes.get(e.name)
                if not isinstance(rec, dict):
                    continue
                nid = rec.get("node_id")
                if rec.get("url") != str(e.url) or rec.get("tls_pin") != (e.tls_pin or None) or \
                        not isinstance(nid, str) or not NODE_ID_RE.match(nid):
                    continue
                self.nodes[e.name] = rec
                out[e.name] = nid
        return out

    def put(self, entry: Any, nid: str) -> None:
        """Record (and write, 0600) the id a direct link learned."""
        with self.lock:
            self.nodes[entry.name] = {"node_id": nid, "url": str(entry.url), "tls_pin": entry.tls_pin or None,
                                      "learned_at": iso_utc()}
            try:
                write_json_atomic(self.path, {"v": 1, "nodes": self.nodes}, private=True)
            except OSError as e:
                log.error("cannot write %s: %s", self.path, e)


# ---------------------------------------------------------------------------------------------------------------
# the hub

class Hub:
    """The hub (§8, §4.6)."""

    def __init__(self, cfg: "HubConfig", *, config_path: Optional[Path] = None):
        from .approvals import ApprovalStore
        from .audit import AuditLog
        self.cfg = cfg
        self.name = cfg.name
        self.config_path = Path(config_path) if config_path else getattr(cfg, "_path", None)
        self.data_dir = Path(cfg.data_dir or ".").resolve()
        for d in (self.data_dir, self.data_dir / "state", self.data_dir / "audit", self.data_dir / "logs",
                  self.data_dir / "run"):
            secure_mkdir(d, required=(d == self.data_dir))
        self.tokens = TokenStore([t.to_dict() for t in cfg.tokens], kind_default="agent")
        relay_entries = [{"name": n.name, "sha256": n.token_sha256, "kind": "node"} for n in cfg.nodes
                         if n.mode == "relay" and n.token_sha256]
        self.relay_tokens = TokenStore(relay_entries, kind_default="node")
        aud = cfg.audit or {}
        self.audit = AuditLog(self.data_dir / "audit", enabled=bool(aud.get("enabled", True)),
                              retention_days=int(aud.get("retention_days", 90)),
                              required=bool(aud.get("required", False)))
        try:
            self.audit.hub_name = self.name
        except Exception:  # noqa: BLE001
            pass
        appr = cfg.approvals or {}
        self.approvals = ApprovalStore(self.data_dir / "approvals.json", ttl=int(appr.get("ttl", 600)),
                                       max_pending=int(appr.get("max_pending", 50)), where=f"hub:{self.name}",
                                       decide_from=appr.get("decide_from") or ("loopback",),
                                       max_grant_minutes=int(appr.get("max_grant_minutes", 240)))
        lim = {"max_call_seconds": 3700, "max_inflight_per_agent": 32, "call_timeout_slack": 15}
        lim.update(cfg.limits or {})
        self.limits = lim
        self.relay_ids = RelayIds(self.data_dir / "state" / "relay_ids.json")
        self.links: Dict[str, NodeLink] = {}
        for e in cfg.nodes:
            self.links[e.name] = RelayLink(self, e) if e.mode == "relay" else DirectLink(self, e)
        self.stopping = threading.Event()
        self.shutdown_event = threading.Event()
        self.exit_code = 0
        self.server: Any = None
        self._lock = threading.Lock()
        self._inflight: Dict[str, Tuple[Identity, NodeLink, threading.Event]] = {}
        self._agent_inflight: Dict[str, int] = {}
        self._threads: List[threading.Thread] = []
        self._lockfile: Optional[FileLock] = None
        self._pyz: Optional[Tuple[bytes, str]] = None
        self._pyz_lock = threading.Lock()
        self._dist_hits: Dict[str, List[float]] = {}
        self._dist_sem = threading.BoundedSemaphore(DIST_CONCURRENT)
        self._started = False
        self.relay_ids.load([e for e in cfg.nodes if e.mode == "relay"])
        for e in cfg.nodes:
            if e.mode == "relay":
                link = self.links[e.name]
                assert isinstance(link, RelayLink)
                link.identity_changed = bool(self.relay_ids.get(e.name).get("pending_sha256"))
        # verification fix (audit round 3): direct node_ids learned before the restart are known from the start
        # (before the API server or the prober run), so a pod cannot claim one while the direct node is offline;
        # a relay_ids.json binding to such an id is dropped now
        self.direct_ids = DirectIds(self.data_dir / "state" / "direct_ids.json")
        for name, nid in self.direct_ids.load([e for e in cfg.nodes if e.mode != "relay"]).items():
            dl = self.links.get(name)
            if isinstance(dl, DirectLink):
                dl._node_id = nid
                self._direct_id(dl, nid)

    # ------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Lock, reaper (1 s), direct-link prober (first round now, then every 30 s), pyz build."""
        if self._started:
            return
        lock = FileLock(self.data_dir / "hub.lock")
        if not lock.acquire(note=f"hub {os.urandom(4).hex()}"):   # per start: a re-exec keeps pid+ctime (v8.1)
            holder = lock.holder_pid()
            raise conflict(f"another hub is running (pid {holder})" if holder else "another hub is running",
                           hint="stop it first: hive hub stop")
        self._lockfile = lock
        self._started = True
        for name, fn in (("hub-reaper", self._reaper), ("hub-prober", self._prober)):
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self._threads.append(t)
        if (self.cfg.dist or {}).get("enabled", True):
            threading.Thread(target=self._build_pyz, name="hub-pyz", daemon=True).start()
        self._audit_event("lifecycle", self.name, {"event": "start", "version": __version__, "pid": os.getpid()})

    def stop(self) -> None:
        """Cancel in-flight calls, wake polls, stop threads, release the lock."""
        self.stopping.set()
        with self._lock:
            items = list(self._inflight.values())
        for _ident, link, ev in items:
            ev.set()
        for link in self.links.values():
            link.stop()
        end = time.monotonic() + 10
        while time.monotonic() < end:
            with self._lock:
                if not self._inflight:
                    break
            time.sleep(0.05)
        # Phase 2 review: a probe or call still running when the links were first stopped may have opened a new
        # keep-alive connection; join the hub threads (prober, reaper) and close the transports once more
        for t in list(getattr(self, "_threads", [])):
            if t is not threading.current_thread():
                t.join(5)
        self._threads = [t for t in getattr(self, "_threads", []) if t.is_alive()]
        for link in self.links.values():
            link.stop()
        if self._started:
            self._audit_event("lifecycle", self.name, {"event": "stop", "exit_code": self.exit_code})
        self._started = False
        if self._lockfile is not None:
            self._lockfile.release()
            self._lockfile = None

    def request_stop(self, code: int = 0) -> None:
        """Ask run_hub to shut down gracefully (0 stop, 75 restart)."""
        self.exit_code = code
        self.shutdown_event.set()

    def _reaper(self) -> None:
        while not self.stopping.wait(1.0):
            for link in list(self.links.values()):
                if isinstance(link, RelayLink):
                    try:
                        link.reap()
                    except Exception:  # noqa: BLE001
                        log.warning("relay reaper failed for %s", link.name, exc_info=True)

    def _prober(self) -> None:
        """Probe every direct node at start and every ``PROBE_INTERVAL``; final acceptance B5e: a node not reached
        yet since the hub started (just added with add-node, or its ``hive node token add --kind hub`` not done
        yet) is probed again after 2 and 4 s, then every 5 s during the hub's first 10 minutes, so it shows online
        within seconds of accepting the hub token."""
        nxt = 0.0
        fast: Dict[str, Tuple[float, float]] = {}      # name -> (next fast probe, its delay)
        started = time.monotonic()
        while not self.stopping.is_set():
            now = time.monotonic()
            due_all = now >= nxt
            for link in list(self.links.values()):
                if not isinstance(link, DirectLink) or self.stopping.is_set():
                    continue
                f = fast.get(link.name)
                if not due_all and (f is None or now < f[0]):
                    continue
                try:
                    link.probe()
                    fast.pop(link.name, None)
                except HiveError as e:
                    log.info("probe of %s failed: %s", link.name, e.message)
                    reached = link._row_ok_at is not None
                    delay = 2.0 if f is None else min(FAST_PROBE_MAX, f[1] * 2)
                    if not reached and now - started < FAST_PROBE_WINDOW:
                        fast[link.name] = (time.monotonic() + delay, delay)
                    else:
                        fast.pop(link.name, None)
                except Exception:  # noqa: BLE001
                    log.warning("probe of %s failed", link.name, exc_info=True)
            if due_all:
                nxt = time.monotonic() + PROBE_INTERVAL
            self.stopping.wait(1.0)

    def _build_pyz(self) -> None:
        try:
            self.pyz()
        except HiveError as e:
            log.warning("hive.pyz not available: %s", e.message)

    def pyz(self) -> Tuple[bytes, str]:
        """(bytes, sha256 hex) of the served zipapp, built once and cached."""
        with self._pyz_lock:
            if self._pyz is None:
                from . import dist
                data = dist.build_pyz()
                self._pyz = (data, dist.pyz_sha256(data))
            return self._pyz

    def _audit_event(self, kind: str, node: str, extra: dict) -> None:
        try:
            self.audit.record(kind=kind, identity=None, ip="", via="", node=node, action=None, params=None, ok=True,
                              code=None, ms=0, request_id="", extra=extra)
        except HiveError:
            pass

    # ------------------------------------------------------------------------------------------- resolution
    def _visible(self, identity: Identity) -> List[NodeLink]:
        return [l for l in self.links.values() if identity.allows_node(l.name)]

    def resolve(self, node: str) -> Optional[NodeLink]:
        """Case-insensitive match on name, then node_id, then aliases (§8.1), no access check.

        Audit round 3: node_ids reported by direct links (authenticated ``node.info``) are matched before the ones
        relay pods report at register (which authenticate nothing), so a pod can never take a direct node's id; an
        id that more than one link of the same kind reports is refused (``not_found`` "ambiguous")."""
        n = str(node).lower()
        links = list(self.links.values())
        for l in links:
            if l.name.lower() == n:
                return l
        for direct in (True, False):
            hits = [l for l in links if isinstance(l, DirectLink) == direct and l.node_id and l.node_id.lower() == n]
            if len(hits) > 1:
                raise not_found(f"node_id {str(node)[:64]!r} is ambiguous (reported by more than one node)",
                                hint="address the node by name")
            if hits:
                return hits[0]
        for l in links:
            if n in l.aliases:
                return l
        return None

    def _direct_id(self, link: "DirectLink", nid: str) -> None:
        """Audit round 3: a direct probe learned ``nid``; a relay link (live session or relay_ids.json binding) that
        claims the same id loses the claim (warning audit record). Direct node ids come from an authenticated probe,
        relay node ids are self-reported."""
        n = nid.lower()
        for other in list(self.links.values()):
            if not isinstance(other, RelayLink):
                continue
            dropped = None
            with other.lock:
                if other._node_id and other._node_id.lower() == n:
                    dropped = other._node_id
                    other._node_id = ""
                rec = self.relay_ids.get(other.name)
                bound = rec.get("node_id")
                if isinstance(bound, str) and bound.lower() == n:
                    dropped = dropped or bound
                    rec["node_id"] = None
                    self.relay_ids.save()
            if dropped:
                log.warning("relay node %s claimed node_id %s, which direct node %s reports; claim dropped",
                            other.name, dropped, link.name)
                self._audit_event("warning", other.name, {"event": "relay node_id claim dropped", "node_id": dropped,
                                                          "direct_node": link.name})

    def link_for(self, identity: Identity, node: Optional[str]) -> NodeLink:
        """§8.1 resolution + access check."""
        visible = self._visible(identity)
        names = sorted(l.name for l in visible)
        if node is None or node == "":
            if len(visible) == 1:
                return visible[0]
            dn = self.cfg.default_node
            if dn:
                l = self.resolve(dn)
                if l is not None and identity.allows_node(l.name):
                    return l
            raise bad_request("specify node", hint="specify node; available: " + (", ".join(names) or "(none)"))
        if not isinstance(node, str) or len(node) > 128:
            raise bad_request("invalid node name")
        link = self.resolve(node)
        if link is None:
            sugg = difflib.get_close_matches(node.lower(), [x.lower() for x in names], n=3, cutoff=0.6)
            raise not_found(f"node {node[:64]!r} not found",
                            hint=(f"did you mean {', '.join(sugg)}? " if sugg else "") +
                                 "available: " + (", ".join(names) or "(none)"),
                            suggestions=sugg)
        if not identity.allows_node(link.name):
            raise forbidden(f"this token may not use node {node[:64]!r}",
                            hint="allowed nodes: " + (", ".join(names) or "(none)"))
        return link

    # ------------------------------------------------------------------------------------------- views
    def nodes_for(self, identity: Identity, include_offline: bool = True) -> List[dict]:
        """NodeSummary rows of the nodes this identity may see."""
        rows = []
        for l in sorted(self._visible(identity), key=lambda x: x.name):
            r = l.summary()
            if include_offline or r.get("online"):
                rows.append(r)
        return rows

    def approval_set(self, identity: Identity, link: Optional[NodeLink]) -> Tuple[str, ...]:
        """§8.2 step 4: require_approval (+ CAUTIOUS_ACTIONS for a cautious or unknown-preset node)."""
        if identity.kind != "agent":
            return ()
        s = tuple(identity.require_approval)
        if link is not None:
            p = link.preset()
            if p is None or p == "cautious":
                s = s + tuple(a for a in CAUTIOUS_ACTIONS if a not in s)
        return s

    def _allowed(self, identity: Identity, spec: Any) -> bool:
        return set(spec.classes_required) <= set(identity.classes) and identity.allows_action(spec.name)

    def actions_for(self, identity: Identity, node: Optional[str] = None) -> dict:
        """/v1/actions body: one node's rows, or the union over visible nodes with per-action ``nodes``."""
        if node:
            link = self.link_for(identity, node)
            aset = self.approval_set(identity, link)
            rows = []
            impl = set(link.actions())
            for row in link.catalog() or [catalog.public_dict(catalog.SPECS[n]) for n in impl
                                          if n in catalog.SPECS]:
                spec = catalog.SPECS.get(row.get("name"))
                if spec is None or (impl and spec.name not in impl) or not self._allowed(identity, spec):
                    continue
                r = catalog.public_dict(spec)
                r["approval"] = glob_match(spec.name, aset)
                rows.append(r)
            return {"node": link.name, "catalog_hash": catalog.catalog_hash(), "actions": rows}
        union: Dict[str, dict] = {}
        for link in sorted(self._visible(identity), key=lambda x: x.name):
            aset = self.approval_set(identity, link)
            for n in link.actions():
                spec = catalog.SPECS.get(n)
                if spec is None or spec.scope != "node" or not self._allowed(identity, spec):
                    continue
                r = union.get(n)
                if r is None:
                    r = catalog.public_dict(spec)
                    r["approval"] = False
                    r["nodes"] = []
                    union[n] = r
                r["nodes"].append(link.name)
                r["approval"] = bool(r["approval"] or glob_match(n, aset))
        for name in HUB_HANDLERS:
            spec = catalog.SPECS.get(name)
            if spec is not None and self._allowed(identity, spec):
                r = catalog.public_dict(spec)
                r["approval"] = False
                r["nodes"] = []
                union[name] = r
        return {"node": None, "catalog_hash": catalog.catalog_hash(),
                "actions": [union[k] for k in sorted(union)]}

    def whoami(self, identity: Identity) -> dict:
        """GET /v1/whoami body."""
        return {"identity": {"name": identity.name, "kind": identity.kind, "classes": sorted(identity.classes),
                             "read_only": identity.read_only, "nodes": list(identity.nodes),
                             "approver": identity.approver, "via": identity.via, "agent": identity.agent},
                "server": {"role": "hub", "name": self.name, "version": __version__, "api": 1},
                "approvals": bool(identity.kind == "agent" and (identity.require_approval or any(
                    (l.preset() in (None, "cautious")) for l in self._visible(identity))))}

    def _act_nodes(self, identity: Identity, params: dict) -> dict:
        """hub.nodes."""
        return {"nodes": self.nodes_for(identity, include_offline=bool(params.get("include_offline", True)))}

    # ------------------------------------------------------------------------------------------- call
    def call(self, identity: Identity, action: str, params: Optional[dict] = None, *, node: Optional[str] = None,
             timeout: Optional[float] = None, request_id: Optional[str] = None, approval_id: Optional[str] = None,
             cancel: Optional[threading.Event] = None, ip: str = "") -> dict:
        """The hub pipeline (§8.2); returns the envelope and never raises."""
        t0 = time.monotonic()
        rid = request_id if isinstance(request_id, str) and REQUEST_ID_RE.match(request_id) else new_id("r_")
        via = f"hub:{self.name}"
        meta: Dict[str, Any] = {"request_id": rid, "node": None, "action": action, "duration_ms": 0, "via": via,
                                "api": 1}
        akey = f"{identity.name}\x00{identity.token_sha256}"
        audit_params = params if isinstance(params, dict) else None
        extra: Dict[str, Any] = {}
        link: Optional[NodeLink] = None
        env: Optional[dict] = None
        counted = False
        ev = cancel or threading.Event()
        try:
            if request_id is not None and not (isinstance(request_id, str) and REQUEST_ID_RE.match(request_id)):
                raise bad_request("invalid request_id", hint="use ^[A-Za-z0-9_.:-]{1,80}$")
            if params is not None and not isinstance(params, dict):
                raise bad_request("params must be an object")
            if not isinstance(action, str) or not action:
                raise bad_request("missing action")
            # 1. per-agent inflight cap
            cap = int(self.limits.get("max_inflight_per_agent", 32))
            with self._lock:
                if self._agent_inflight.get(akey, 0) >= cap:
                    raise rate_limited(f"too many calls in flight for {identity.name} (max {cap})", retry_after=2,
                                       reason="inflight")
                self._agent_inflight[akey] = self._agent_inflight.get(akey, 0) + 1
                counted = True
                self._inflight[rid] = (identity, None, ev)  # type: ignore[assignment]
            # 2a. hub-scope actions
            if action in HUB_HANDLERS:
                spec = catalog.get(action)
                if not self._allowed(identity, spec):
                    raise forbidden(f"this token may not call {action}")
                p = schema.validate(spec.params, params or {})
                audit_params = p
                meta["node"] = self.name
                result = HUB_HANDLERS[action](self, identity, p)
                env = {"ok": True, "result": result, "meta": meta}
                return env
            # 2. node
            link = self.link_for(identity, node)
            meta["node"] = link.name
            with self._lock:
                self._inflight[rid] = (identity, link, ev)
            # 3. hub checks (preceded by ensure_fresh)
            link.ensure_fresh()
            spec = catalog.SPECS.get(action)
            if spec is None:
                try:
                    catalog.get(action)       # removed v1 names keep their replacement hint
                except HiveError as e:
                    if (e.details or {}).get("removed"):
                        raise
                raise _unknown_action(action, link)
            if spec.scope == "hub":
                raise bad_request(f"{action} is a hub action", hint="call it without node")
            if action not in link.actions():
                ver = link.summary().get("version") or "?"
                raise not_found(f"node {link.name!r} does not implement {action}",
                                hint=f"node '{link.name}' runs hive {ver} which lacks '{action}'; upgrade it")
            if not set(spec.classes_required) <= set(identity.classes):
                missing = sorted(set(spec.classes_required) - set(identity.classes))
                raise forbidden(f"this token may not call {action} (needs {', '.join(missing)})")
            if not identity.allows_action(action):
                raise forbidden(f"{action} is denied for this token")
            if not identity.allows_node(link.name):
                raise forbidden(f"this token may not use node {link.name}")
            # 3a. validate without defaults; forwarded as normalized
            p = schema.validate(spec.params, params or {}, coerce=True, defaults=False)
            audit_params = p
            maxs = float(self.limits.get("max_call_seconds", 3700))
            deadline = catalog.deadline(spec, p, timeout, max_seconds=maxs)
            end = time.monotonic() + deadline
            # 4. hub approvals
            aset = self.approval_set(identity, link)
            gated = auth_exec_gated(identity, aset)
            bound: Optional[Dict[str, Optional[str]]] = None
            if identity.kind == "agent" and glob_match(action, aset):
                gid = self.approvals.granted(identity, link.name, action)
                if gid:
                    extra["grant"] = gid
                else:
                    review: Optional[dict] = None
                    if not approval_id:
                        review = self._review(identity, link, action, p, rid, end, gated)
                    files = self.approvals.require(identity, link.name, action, p, review or {}, approval_id)
                    extra["approval"] = approval_id
                    bound = files or None
            if ev.is_set():
                raise HiveError("cancelled", "cancelled")
            # 5. forward
            remaining = max(1.0, end - time.monotonic())
            env = link.call(identity, action, p, timeout=remaining, request_id=rid, exec_gated=gated,
                            bound_files=bound)
            link.last_call_at = time.time()
            return env
        except HiveError as e:
            env = _err_env(e, meta)
            return env
        except Exception as e:  # noqa: BLE001 - call() never raises
            log.error("hub pipeline failure for %s", action, exc_info=True)
            env = _err_env(internal(type(e).__name__), meta)
            return env
        finally:
            with self._lock:
                self._inflight.pop(rid, None)
                if counted:
                    n = self._agent_inflight.get(akey, 1) - 1
                    if n <= 0:
                        self._agent_inflight.pop(akey, None)
                    else:
                        self._agent_inflight[akey] = n
            if env is not None:
                m = env.get("meta") if isinstance(env.get("meta"), dict) else {}
                m = dict(m)
                m.update(request_id=rid, via=via, api=1, action=action)
                m.setdefault("node", meta.get("node"))
                m["duration_ms"] = int((time.monotonic() - t0) * 1000)
                env["meta"] = m
                ok = bool(env.get("ok"))
                code = None if ok else (env.get("error") or {}).get("code")
                try:
                    self.audit.record(kind="call", identity=identity, ip=ip, via="direct",
                                      node=str(meta.get("node") or ""), action=action, params=audit_params, ok=ok,
                                      code=code, ms=m["duration_ms"], request_id=rid, extra=extra or None)
                except HiveError as e:
                    if ok:
                        env.clear()
                        env.update(_err_env(e, m))

    def cancel(self, request_id: str, identity: Identity) -> bool:
        """Cancel a routed call (own requests only; owner: any)."""
        with self._lock:
            item = self._inflight.get(request_id)
        if item is None:
            return False
        owner, link, ev = item
        if identity.kind != "owner" and (owner.name != identity.name or owner.token_sha256 != identity.token_sha256):
            return False
        ev.set()
        if link is not None:
            link.cancel(request_id)
        return True

    # ------------------------------------------------------------------------------------------- approval review
    def _review(self, identity: Identity, link: NodeLink, action: str, params: dict, rid: str, end: float,
                gated: bool) -> dict:
        """presets.approval_review(ctx=None) + the v7/v8 file binding over the link (§8.2 step 4)."""
        from . import presets
        review = presets.approval_review(action, params, ctx=None)
        if action not in FILE_BIND_ACTIONS:
            return review
        words, cwd, moved, unbound, quiet = self._bind_words(identity, link, action, params, rid, end, gated)
        if not words and not moved and not unbound:
            return review
        files, lines, not_bound = self._bind_files(identity, link, words, cwd, rid, end, gated, quiet)
        if moved:   # v8.1: relative words after cd / -C are not bound; named in the header (presets.moved_items)
            kinds: Dict[str, Optional[str]] = {}

            def kind(w: str) -> Optional[str]:
                if w not in kinds:
                    kinds[w] = self._stat_type(identity, link, self._join(cwd, w), rid, end, gated)
                return kinds[w]
            not_bound += presets.moved_items(moved, lambda w: kind(w) == "file", quiet,
                                             lambda w: kind(w) is not None)
        not_bound += unbound
        from .util import escape_terminal
        review = dict(review)
        rl = list(review.get("lines") or [])
        if not_bound:
            hdr = escape_terminal("not bound to their content (may change after approval): " + ", ".join(not_bound))
            pos = 1 if rl and rl[0].startswith("changes what runs:") else 0
            rl.insert(pos, hdr)
        rl += [escape_terminal(x) for x in lines]
        review["lines"] = rl
        review["files"] = list(review.get("files") or []) + files
        return review

    def _sub(self, identity: Identity, link: NodeLink, action: str, params: dict, rid: str, i: int, end: float,
             gated: bool) -> dict:
        left = end - time.monotonic()
        if left <= 0.5:
            raise timeout_error("the approval file check ran out of time", maybe_executed=False)
        env = link.call(identity, action, params, timeout=max(1.0, left), request_id=f"{rid[:72]}.b{i}",
                        exec_gated=gated)
        return _envelope_result(env)

    def _split(self, text: str, shell: Optional[str], os_name: Optional[str]
               ) -> Tuple[List[str], List[str], List[str], AbstractSet[str]]:
        """(words to bind, relative words whose cwd may change, unbindable header items, quiet words) via
        ``presets.bind_plan_ex`` (v8.1); with the node's OS unknown, both the POSIX and the PowerShell reading (a
        word either one moves is not bound; a non-path word is quiet only when both readings say so)."""
        from . import presets
        if shell not in (None, "", "auto"):
            return presets.bind_plan_ex(text, shell=shell, os_name=None)
        if os_name == "Windows":
            return presets.bind_plan_ex(text, shell="powershell")
        if os_name:
            return presets.bind_plan_ex(text, shell="bash")
        out, moved, unbound, q1 = presets.bind_plan_ex(text, shell="bash")
        w2, m2, u2, q2 = presets.bind_plan_ex(text, shell="powershell")
        out += [w for w in w2 if w not in out]
        moved += [w for w in m2 if w not in moved]
        unbound += [w for w in u2 if w not in unbound]
        quiet = (set(q1) & set(q2)) | {w for w in set(q1) | set(q2) if presets._pathlike(w)}
        return [w for w in out if w not in moved], moved, unbound, frozenset(quiet)

    def _is_file(self, identity: Identity, link: NodeLink, path: str, rid: str, end: float, gated: bool) -> bool:
        return self._stat_type(identity, link, path, rid, end, gated) == "file"

    def _stat_type(self, identity: Identity, link: NodeLink, path: str, rid: str, end: float, gated: bool
                   ) -> Optional[str]:
        """"file"/"dir"/… of an existing path on the node, None when missing or not checkable."""
        try:
            st = self._sub(identity, link, "fs.stat", {"path": path}, rid, 900, end, gated)
        except HiveError:
            return None
        return str(st.get("type") or "other") if st.get("exists") else None

    def _bind_words(self, identity: Identity, link: NodeLink, action: str, params: dict, rid: str, end: float,
                    gated: bool) -> Tuple[List[str], Optional[str], List[str], List[str], AbstractSet[str]]:
        from . import presets
        os_name = link.os_name()
        words: List[str] = []
        moved: List[str] = []
        unbound: List[str] = []
        quiet: set = set()

        def add(plan: Tuple[List[str], List[str], List[str], AbstractSet[str]]) -> None:
            words.extend(w for w in plan[0] if w not in words)
            moved.extend(w for w in plan[1] if w not in moved)
            unbound.extend(w for w in plan[2] if w not in unbound)
            quiet.update(plan[3])

        cwd = params.get("cwd") if isinstance(params.get("cwd"), str) else None
        env: Any = params.get("env")
        if action in ("shell.run", "job.start"):
            if params.get("argv"):
                add(presets.bind_plan_ex([str(x) for x in params["argv"]]))
            else:
                add(self._split(str(params.get("command") or ""), params.get("shell"), os_name))
        elif action == "service.define":
            if params.get("argv"):
                add(presets.bind_plan_ex([str(x) for x in params["argv"]]))
            elif params.get("command"):
                add(self._split(str(params.get("command") or ""), params.get("shell"), os_name))
            sc = params.get("stop_command")
            if sc:
                add(presets.bind_plan_ex([str(x) for x in sc]) if isinstance(sc, list) else
                    self._split(str(sc), params.get("shell"), os_name))
            for item in params.get("preset_args") or []:
                s = str(item)
                if "=" in s:
                    v = s.split("=", 1)[1]
                    if v and not v.startswith("-") and v not in words:
                        words.append(v)
        else:   # service.start / service.restart: the stored definition, read first with service.status
            try:
                st = self._sub(identity, link, "service.status", {"name": params.get("name")}, rid, 0, end, gated)
            except HiveError as e:
                if e.code in ("policy_denied", "forbidden", "not_found"):
                    return [], None, [], [], frozenset()
                raise
            d = st.get("definition") if isinstance(st.get("definition"), dict) else {}
            if d.get("argv"):
                add(presets.bind_plan_ex([str(x) for x in d["argv"]]))
            elif d.get("command"):
                add(self._split(str(d.get("command")), d.get("shell"), os_name))
            cwd = d.get("cwd") if isinstance(d.get("cwd"), str) else None
            env = d.get("env")
        w2, u2 = presets._env_words(env, words, unbound)   # v8.1: files named by BASH_ENV, LD_PRELOAD, …
        return w2, cwd, moved, u2, frozenset(quiet)

    @staticmethod
    def _join(cwd: Optional[str], w: str) -> str:
        if w.startswith("~") or w.startswith("$") or w.startswith("/") or w.startswith("\\") or re.match(r"^[A-Za-z]:[\\/]", w):
            return w
        if not cwd:
            return w
        sep = "\\" if ("\\" in cwd and "/" not in cwd) else "/"
        return cwd.rstrip("/\\") + sep + w

    def _bind_files(self, identity: Identity, link: NodeLink, words: List[str], cwd: Optional[str], rid: str,
                    end: float, gated: bool, quiet: AbstractSet[str] = frozenset()
                    ) -> Tuple[List[dict], List[str], List[str]]:
        """``presets.bind_files`` over the link (fs.stat/fs.read on the node). Audit round 2: a missing non-path word
        is listed as "no file here, not bound" unless quiet; a second path to an already bound file adds an entry
        for that path."""
        from .presets import (BIND_MAX_ABSENT, FOLDER_OPERAND, MISSING_UNBOUND, NOT_SHOWN, OP_MARK, _pathlike,
                              _strip_dot_segments, listable_missing)

        def entry(**kw: Any) -> dict:
            # v8.1 (external review): the node re-resolves the bound path at consume (FileHolds.acquire), so it is
            # the path as the command names it (cwd + word, not the real path seen now): a symbolic link in the cwd
            # or the word retargeted after approval resolves to another file and the consume is refused
            e = dict(kw)
            if path != rp:
                e["path"] = path
                e["real"] = rp
            seen[rp] = e
            return e
        files: List[dict] = []
        lines: List[str] = []
        not_bound: List[str] = []
        seen: Dict[str, dict] = {}
        named_seen: set = set()
        bound = 0
        absent = 0
        shown_left = SHOW_MAX
        i = 1
        for w in words:
            if "\x00" in w:
                continue
            path = _strip_dot_segments(self._join(cwd, w))
            try:
                st = self._sub(identity, link, "fs.stat", {"path": path}, rid, i, end, gated)
                i += 1
            except HiveError as e:
                i += 1
                if e.code in ("policy_denied", "forbidden", "not_found", "bad_request"):
                    if _pathlike(w) and w not in quiet:
                        not_bound.append(f"{w} (not resolvable here)")
                    continue
                raise
            rp = st.get("path") if isinstance(st.get("path"), str) else path
            if rp in seen:
                first = seen[rp]
                if path != rp and path not in named_seen and first.get("bind") != "none":
                    named_seen.add(path)
                    files.append(dict(first, path=path, real=rp, shown=False))
                continue
            exists = bool(st.get("exists"))
            if exists and st.get("type") != "file":
                if OP_MARK + w in quiet and st.get("type") == "dir":
                    not_bound.append(f"{w.rstrip('/')}/ {FOLDER_OPERAND}")
                continue
            if not exists and not _pathlike(w):
                if listable_missing(w, quiet):
                    not_bound.append(f"{w} {MISSING_UNBOUND}")
                continue
            seen[rp] = {"bind": "none"}
            named_seen.add(path)
            size = st.get("size") if isinstance(st.get("size"), int) else None
            if (bound >= BIND_MAX_FILES) if exists else (absent >= BIND_MAX_ABSENT):
                files.append(entry(path=rp, bind="none", sha256=None, size=size, shown=False))
                if w not in quiet:
                    not_bound.append(f"{w} (over the {BIND_MAX_FILES if exists else BIND_MAX_ABSENT}-file limit)")
                continue
            if not exists:
                absent += 1
                files.append(entry(path=rp, bind="absent", sha256=None, size=None, shown=False))
                if w not in quiet:
                    not_bound.append(f"{w} (missing here)")
                continue
            bound += 1
            size = int(size or 0)
            mtime = st.get("mtime")
            if size > HASH_MAX:
                fp = f"stat:{size}:{mtime}"
                files.append(entry(path=rp, bind="stat", sha256=None, size=size, shown=False, fingerprint=fp,
                                   mtime=mtime))
                lines.append(f"--- file {rp} ({human_bytes(size)}, bound by size and mtime) ---")
                lines.append(NOT_SHOWN)
                continue
            content: Optional[str] = None
            digest: Optional[str] = None
            if size <= shown_left and shown_left >= 1024:
                try:
                    r = self._sub(identity, link, "fs.read",
                                  {"path": rp, "max_lines": 20000, "max_bytes": max(1024, min(shown_left, 8 << 20))},
                                  rid, i, end, gated)
                    i += 1
                    if r.get("next_start_line") is None and not r.get("long_lines_cut") and r.get("version"):
                        digest = str(r["version"]).split(":", 1)[-1]
                        content = str(r.get("content") or "")
                except HiveError as e:
                    i += 1
                    if e.code in ("policy_denied", "forbidden", "not_found"):
                        if _pathlike(w):
                            not_bound.append(f"{w} (not resolvable here)")
                        bound -= 1
                        seen.pop(rp, None)
                        named_seen.discard(path)
                        continue
                    if e.code not in ("unsupported", "too_large", "bad_request"):
                        raise
            if digest is None:
                r = self._sub(identity, link, "fs.stat", {"path": rp, "sha256": True}, rid, i, end, gated)
                i += 1
                digest = str(r.get("sha256") or "").split(":", 1)[-1] or None
                if digest is None:
                    bound -= 1
                    seen.pop(rp, None)
                    named_seen.discard(path)
                    continue
            shown = content is not None
            if shown:
                shown_left -= size
            files.append(entry(path=rp, bind="sha256", sha256=digest, size=size, shown=shown))
            lines.append(f"--- file {rp} ({human_bytes(size)}, sha256 {digest[:12]}) ---")
            if content is not None:
                lines.extend(content.split("\n"))
            else:
                lines.append(NOT_SHOWN)
        return files, lines, not_bound

    # ------------------------------------------------------------------------------------------- dist
    def dist_allowed(self, ip: str) -> Optional[float]:
        """None when a pyz download may start now, else seconds to wait (≤ 6/min per client IP)."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._dist_hits.get(ip, []) if now - t < 60.0]
            if len(hits) >= DIST_PER_MIN:
                self._dist_hits[ip] = hits
                return max(1.0, 60.0 - (now - hits[0]))
            hits.append(now)
            self._dist_hits[ip] = hits
            if len(self._dist_hits) > 10000:
                self._dist_hits.pop(next(iter(self._dist_hits)))
        return None


HUB_HANDLERS: Dict[str, Callable[["Hub", "Identity", dict], dict]] = {"hub.nodes": Hub._act_nodes}


# ---------------------------------------------------------------------------------------------------------------
# HTTP routes (§6.3, §7)

_CALL_KEYS = ("action", "params", "node", "timeout", "request_id", "approval_id")


def build_server(hub: Hub) -> "ApiServer":
    """The hub's ApiServer with every hub route registered (not started)."""
    from .client import HubTarget
    from .httpserver import ApiServer
    from .limits import AuthThrottle
    cfg = hub.cfg
    th = cfg.throttle or {}
    server = ApiServer(cfg.listen, role="hub", tokens=lambda: hub.tokens, relay_tokens=lambda: hub.relay_tokens,
                       throttle=AuthThrottle(int(th.get("max_failures", 10)), float(th.get("window", 60)),
                                             float(th.get("block", 300))),
                       logger=logging.getLogger("hive.httpserver"), audit=hub.audit,
                       owner_check=hub.approvals.may_decide)
    server.node_name = hub.name
    # audit round 4: the transport's per-identity cap on requests with a body (checked before the body is read)
    # leaves room above the per-agent call cap (cancels, MCP requests) so that cap keeps answering first
    from .limits import KeyedLimit
    server.inflight = KeyedLimit(int(hub.limits.get("max_inflight_per_agent", 32)) + 8)

    def health(req: "Request") -> dict:
        return {"role": "hub", "api": 1}

    def whoami(req: "Request") -> dict:
        return hub.whoami(req.identity)

    def actions(req: "Request") -> dict:
        return hub.actions_for(req.identity, req.query.get("node") or None)

    def nodes(req: "Request") -> dict:
        return {"nodes": hub.nodes_for(req.identity)}

    def call(req: "Request") -> dict:
        try:
            body = req.json()
            if not isinstance(body, dict):
                raise bad_request("body must be a JSON object", hint='{"action": "...", "params": {...}}')
            if "bound_files" in body:
                raise bad_request("bound_files is only accepted from a hub")
            unknown = [k for k in body if k not in _CALL_KEYS]
            if unknown:
                raise bad_request(f"unknown field(s) in call body: {', '.join(unknown[:5])}",
                                  hint="fields: " + ", ".join(_CALL_KEYS))
            timeout = body.get("timeout")
            if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
                raise bad_request("timeout must be a number of seconds")
            approval_id = body.get("approval_id")
            if approval_id is not None and not isinstance(approval_id, str):
                raise bad_request("approval_id must be a string")
            node = body.get("node")
            if node is not None and not isinstance(node, str):
                raise bad_request("node must be a string")
        except HiveError as e:
            return _err_env(e, {"request_id": req.request_id, "node": None, "action": None, "duration_ms": 0,
                                "via": f"hub:{hub.name}", "api": 1})
        return hub.call(req.identity, body.get("action"), body.get("params"), node=node, timeout=timeout,
                        request_id=body.get("request_id"), approval_id=approval_id, cancel=req.cancel,
                        ip=req.client_ip)

    def cancel(req: "Request") -> dict:
        body = req.json() or {}
        rid = body.get("request_id") if isinstance(body, dict) else None
        if not isinstance(rid, str) or not REQUEST_ID_RE.match(rid):
            raise bad_request("request_id required")
        return {"cancelled": hub.cancel(rid, req.identity)}

    def approvals_list(req: "Request") -> dict:
        inc = str(req.query.get("include_done", "")).lower() in ("1", "true", "yes")
        return {"approvals": hub.approvals.list(include_done=inc)}

    def _decide(req: "Request", approve: bool) -> dict:
        body = req.json() or {}
        if not isinstance(body, dict):
            raise bad_request("body must be an object")
        extra = [k for k in body if k not in ("note", "grant_minutes")]
        if extra:
            raise bad_request(f"unknown field(s): {', '.join(extra)}")
        rec = hub.approvals.decide(req.query["id"], approve, req.identity, note=str(body.get("note") or ""),
                                   grant_minutes=body.get("grant_minutes"))
        hub.audit.record(kind="approval", identity=req.identity, ip=req.client_ip, via="direct",
                         node=str(rec.get("node") or ""), action=rec.get("action"), params=None, ok=True, code=None,
                         ms=0, request_id=req.request_id,
                         extra={"approval": rec["id"], "decision": rec["status"], "requester": rec.get("requester"),
                                "grant": (rec.get("grant") or {}).get("id")})
        return rec

    def grants(req: "Request") -> dict:
        return {"grants": hub.approvals.grants()}

    def revoke(req: "Request") -> dict:
        body = req.json() or {}
        if not isinstance(body, dict):
            raise bad_request("body must be an object")
        if body.get("all"):
            n = hub.approvals.revoke(None)
        elif isinstance(body.get("id"), str):
            n = hub.approvals.revoke(body["id"])
        else:
            raise bad_request("give id or all:true")
        hub.audit.record(kind="approval", identity=req.identity, ip=req.client_ip, via="direct", node="",
                         action=None, params=None, ok=True, code=None, ms=0, request_id=req.request_id,
                         extra={"revoked": n})
        return {"revoked": n}

    def _relay_link(name: str) -> RelayLink:
        link = hub.links.get(name)
        if link is None:
            raise not_found(f"node {name!r} not found", hint="hive hub status lists the nodes")
        if not isinstance(link, RelayLink):
            raise bad_request(f"{name} is a direct node", hint="run hive node restart on that machine")
        return link

    def node_restart(req: "Request") -> dict:
        link = _relay_link(req.query["name"])
        queued = link.request_restart()
        hub._audit_event("lifecycle", link.name, {"event": "restart-node requested", "queued": queued})
        return {"queued": queued}

    def node_ack(req: "Request") -> dict:
        link = _relay_link(req.query["name"])
        body = req.json() or {}
        if not isinstance(body, dict) or any(k != "pending_sha256" for k in body):
            raise bad_request("body: {\"pending_sha256\"?: str}")
        want = body.get("pending_sha256")
        with link.lock:
            rec = hub.relay_ids.get(link.name)
            pending = dict(rec.get("pending") or {}) if rec.get("pending_sha256") else None
            if pending is not None:
                pending["sha256"] = rec.get("pending_sha256")
            if want is None or pending is None:
                return {"bound": False, "pending": pending, "refused": None}
            if not isinstance(want, str) or not _same(want, rec.get("pending_sha256")):
                return {"bound": False, "pending": pending,
                        "refused": "the pending registration changed; run hive hub ack-node again"}
            seen, at = rec.get("bound_seen_at"), (rec.get("pending") or {}).get("at")
            if seen and at and str(seen) > str(at):
                return {"bound": False, "pending": pending,
                        "refused": "the bound instance registered after this attempt; rotate the token instead"}
            rec.update(key_sha256=rec["pending_sha256"], pending_sha256=None, pending=None, bound_at=iso_utc(),
                       bound_seen_at=None)
            link.identity_changed = False
            hub.relay_ids.save()
        hub._audit_event("warning", link.name, {"event": "ack-node: instance bound", "pending": pending,
                                               "by": req.identity.name})
        return {"bound": True, "pending": pending, "refused": None}

    def _relay_body(req: "Request") -> Tuple[RelayLink, dict]:
        from .httpserver import MAX_RELAY_JSON_CONTAINERS
        body = req.json(max_containers=MAX_RELAY_JSON_CONTAINERS)   # verification round 4: capped before parsing
        if not isinstance(body, dict):
            raise bad_request("body must be a JSON object")
        ident = req.identity
        link = hub.links.get(ident.node_name)
        if not isinstance(link, RelayLink):
            raise forbidden("this relay token belongs to no relay node")
        if "node" in body and body.get("node") != link.name:
            raise forbidden(f"this relay token belongs to node {link.name}")
        return link, body

    # audit round 3: polls naming an unknown or superseded session (409) count per (relay node, source IP), as auth
    # failures do; the token check's own throttle forgets an IP at every successful auth, so this one is separate.
    # Verification fixes: keyed by node too (a relay token can poll only its own node's session, so guesses matter
    # only to that node, and pods sharing one NAT/proxy address cannot block each other); the block is consulted
    # only for a poll whose session does not match (a poll with the live session always proceeds, §6 v4); a
    # successful register clears the (node, IP) count and block (the token holder can register anyway)
    session_throttle = AuthThrottle(int(th.get("max_failures", 10)), float(th.get("window", 60)),
                                    float(th.get("block", 300)))

    def _session_key(link: RelayLink, ip: str) -> str:
        return f"{link.name.lower()}\n{ip}"

    def relay_register(req: "Request") -> dict:
        link, body = _relay_body(req)
        if body.get("node") != link.name:
            raise forbidden(f"this relay token belongs to node {link.name}")
        raw = req.body or b""
        if len(raw) > (2 << 20) + 65536:
            raise HiveError("too_large", "register body larger than 2 MiB", status=413)
        out = link.register(body, req.client_ip)
        key = _session_key(link, req.client_ip)
        session_throttle.ok(key)
        with session_throttle._lock:
            session_throttle._blocked.pop(key)
        return out

    def relay_poll(req: "Request") -> dict:
        link, body = _relay_body(req)
        key = _session_key(link, req.client_ip)
        sid = body.get("session_id")
        live = link.session_id
        matches = (isinstance(sid, str) and bool(sid) and live is not None
                   and hmac.compare_digest(sid.encode("utf-8", "replace"), live.encode("utf-8")))
        if not matches:
            blocked = session_throttle.check(key)
            if blocked is not None:
                raise rate_limited("too many polls with an unknown relay session from your address",
                                   retry_after=blocked, reason="auth_throttle", hint="register again later")
        try:
            return link.poll(body)
        except HiveError as e:
            if e.code == "conflict":
                session_throttle.fail(key)
                if session_throttle.check(key) is not None:
                    hub._audit_event("warning", link.name, {"event": "relay session guesses blocked",
                                                            "ip": req.client_ip})
            raise

    def relay_unregister(req: "Request") -> dict:
        link, body = _relay_body(req)
        return link.unregister(body)

    def relay_result_pre(headers: Any, ident: Any, length: int) -> Optional[dict]:
        # audit round 4 (finding 6): X-Hive-Task / X-Hive-Session name the task before the body is read; a result
        # for a task this relay node does not owe (unknown, finished, other session) is refused unread
        tid = headers.get("x-hive-task")
        if tid is None:
            return None                       # an older relay build: the body decides, as before
        link = hub.links.get(ident.node_name)
        if not isinstance(link, RelayLink):
            raise forbidden("this relay token belongs to no relay node")
        if not link.expects_result(tid, headers.get("x-hive-session")):
            log.info("relay result for %s not accepted (refused before the body)", str(tid)[:40])
            return {"accepted": False}
        return None

    def relay_result(req: "Request") -> dict:
        link, body = _relay_body(req)
        tid = req.headers.get("x-hive-task")
        if tid is not None and (body.get("task_id") != tid or
                                body.get("session_id") != req.headers.get("x-hive-session")):
            raise bad_request("X-Hive-Task / X-Hive-Session do not match the body")
        return link.result(body)

    def dist_pyz(req: "Request") -> Tuple[int, Dict[str, str], Any]:
        if not (cfg.dist or {}).get("enabled", True):
            raise not_found("pyz distribution is disabled on this hub")
        wait = hub.dist_allowed(req.client_ip)
        if wait is not None:
            raise rate_limited("too many hive.pyz downloads; retry later", retry_after=wait, reason="dist")
        if not hub._dist_sem.acquire(blocking=False):
            raise rate_limited("too many concurrent hive.pyz downloads; retry later", retry_after=5, reason="dist")
        try:
            data, sha = hub.pyz()
        except BaseException:
            hub._dist_sem.release()
            raise

        # §5.7 "at most 4 concurrent downloads": the slot is held until the body is sent (or the client is gone);
        # ClosingIterator releases it even when the stream is closed before the first chunk
        def body() -> Iterator[bytes]:
            view = memoryview(data)
            for i in range(0, len(data), DIST_CHUNK):
                yield bytes(view[i:i + DIST_CHUNK])
        return 200, {"Content-Type": "application/octet-stream", "X-Hive-Sha256": sha,
                     "Content-Length": str(len(data)),
                     "Content-Disposition": "attachment; filename=hive.pyz"}, \
            ClosingIterator(body(), hub._dist_sem.release)

    server.route("GET", "/v1/health", health, auth="none")
    server.route("GET", "/v1/whoami", whoami)
    server.route("GET", "/v1/actions", actions)
    server.route("GET", "/v1/nodes", nodes)
    server.route("POST", "/v1/call", call, mode="envelope")
    server.route("POST", "/v1/cancel", cancel)
    server.route("GET", "/v1/approvals", approvals_list, auth="owner")
    server.route("POST", "/v1/approvals/{id}/approve", lambda r: _decide(r, True), auth="owner")
    server.route("POST", "/v1/approvals/{id}/deny", lambda r: _decide(r, False), auth="owner")
    server.route("GET", "/v1/approvals/grants", grants, auth="owner")
    server.route("POST", "/v1/approvals/grants/revoke", revoke, auth="owner")
    server.route("POST", "/v1/hub/nodes/{name}/restart", node_restart, auth="owner")
    server.route("POST", "/v1/hub/nodes/{name}/ack", node_ack, auth="owner")
    server.route("POST", "/v1/relay/register", relay_register, auth="relay", max_body=(2 << 20) + 65536)
    server.route("POST", "/v1/relay/poll", relay_poll, auth="relay")
    server.route("POST", "/v1/relay/unregister", relay_unregister, auth="relay")
    server.route("POST", "/v1/relay/result", relay_result, auth="relay",
                 max_body=int(cfg.relay.max_result_bytes) + 65536, pre_body=relay_result_pre)
    server.route("GET", "/v1/dist/hive.pyz", dist_pyz, auth="none", mode="raw")
    try:
        from . import tools
        tools.register_agent_routes(server, lambda ident, req: HubTarget(hub, ident, req.client_ip), role="hub",
                                    profile=cfg.listen.mcp_profile, mcp=bool(cfg.listen.mcp_http))
    except Exception as e:  # noqa: BLE001 - optional (WP6)
        log.warning("agent tool routes not registered: %s", e)
    if cfg.gateway.enabled:
        try:
            from . import gateway
            gateway.register_gateway_routes(server, role="hub", backend=gateway.HubGatewayBackend(hub),
                                            cfg=cfg.gateway)
        except Exception as e:  # noqa: BLE001
            log.warning("model gateway routes not registered: %s", e)
    return server


# ---------------------------------------------------------------------------------------------------------------
# run_hub

def write_hub_code(data_dir: Path) -> None:
    """``<hub data>/run/hub-code.json`` (§5.4 step 7, D56): the hub process's code paths, hard-denied on the node."""
    from . import util
    try:
        pkg = str(util._zipapp_path() or util._package_dir())
    except Exception:  # noqa: BLE001
        pkg = ""
    prefixes = sorted({p for p in (sys.prefix, sys.base_prefix, sys.exec_prefix) if p})
    sp = [p for p in sys.path if p and os.path.isabs(p)]
    write_json_atomic(Path(data_dir) / "run" / "hub-code.json",
                      {"v": 1, "package_dir": pkg, "executable": sys.executable, "prefixes": prefixes,
                       "sys_path": sp}, private=True)


def _check_control(hub: Hub) -> None:
    p = hub.data_dir / "control.json"
    if not p.exists():
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        d = {}
    try:
        p.unlink()
    except OSError:
        pass
    cmd = d.get("cmd") if isinstance(d, dict) else None
    if cmd in ("stop", "restart"):
        log.warning("control.json: %s requested", cmd)
        hub._audit_event("lifecycle", hub.name, {"event": f"{cmd} requested"})
        hub.request_stop(75 if cmd == "restart" else 0)


def run_hub(config_path: Optional[str] = None, *, relaunched: bool = False, supervise: bool = False) -> int:
    """Run the hub daemon; exit 0 after stop, 75 after a requested restart, non-zero on failure (D55)."""
    from . import config as cfgmod, node as nodemod, util
    nodemod._stdio_guard()
    try:
        cfg = cfgmod.load_hub_config(config_path)
    except HiveError as e:
        logging.getLogger("hive").error("config: %s%s", e.message, f" ({e.hint})" if e.hint else "")
        return 2
    data_dir = Path(cfg.data_dir or ".")
    if IS_WINDOWS and supervise and not relaunched:  # pragma: no cover - Windows only
        cp = Path(config_path or getattr(cfg, "_path", None) or cfgmod.default_paths()["hub_config"])
        return nodemod.supervise("hub", util.launcher_argv("hub", cp, windowless=True), data_dir)
    nodemod.setup_logging(data_dir, cfg.log or {}, "hub")
    try:
        os.chdir(str(data_dir))
    except OSError as e:
        log.warning("cannot chdir to the data dir %s: %s", data_dir, e)
    try:
        for w in cfgmod.startup_safety_checks(cfg, "hub"):
            log.warning("%s", w)
        hub = Hub(cfg, config_path=Path(config_path) if config_path else None)
        hub.start()
    except HiveError as e:
        log.error("hub failed to start: %s%s", e.message, f" ({e.hint})" if e.hint else "")
        return 1
    server = None
    try:
        try:
            write_hub_code(hub.data_dir)
        except OSError as e:
            log.warning("cannot write hub-code.json: %s", e)
        server = build_server(hub)
        bound = server.start()
        hub.server = server
        scheme = "https" if cfg.listen.tls.cert else "http"
        log.warning("Hive hub '%s' ACTIVE — %d nodes, %d tokens. Listen: %s", hub.name, len(hub.links),
                    len(hub.tokens), ", ".join(f"{scheme}://{h}:{p}" for h, p in bound))
        _install_signals(hub)
        while not hub.shutdown_event.wait(1.0):
            _check_control(hub)
    except HiveError as e:
        log.error("hub failed: %s%s", e.message, f" ({e.hint})" if e.hint else "")
        hub.exit_code = 1
    finally:
        if server is not None:
            server.stop()
        hub.stop()
    return hub.exit_code


def _install_signals(hub: Hub) -> None:
    if threading.current_thread() is not threading.main_thread():
        return
    import signal

    def handler(signum: int, frame: Any) -> None:
        hub.request_stop(0)
    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                pass


__all__ = ["Hub", "NodeLink", "RelayLink", "DirectLink", "RelayIds", "run_hub", "build_server", "HUB_HANDLERS",
           "summary_from_info", "write_hub_code"]
