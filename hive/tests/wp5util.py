"""Shared fixtures for the WP5b tests (hub, relay, gateway, transfer)."""
from __future__ import annotations

import os
import secrets
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from hive import catalog, config, node, util
from hive.errors import HiveError
from hive.transport import HttpTransport
from tests import helpers

FAST_RELAY = {"poll_wait": 1, "offline_after": 5, "pickup_timeout": 2, "relay_grace": 1, "lost_grace": 1}
ALL_NODE_ACTIONS = [n for n, s in catalog.SPECS.items() if s.scope == "node"]


class FakeRelay:
    """A scripted relay node speaking the §7 protocol over HTTP (register / poll / result)."""

    def __init__(self, hub_url: str, token: str, name: str, *, actions: Optional[Sequence[str]] = None,
                 info: Optional[dict] = None, key: Optional[str] = None, max_parallel: int = 8):
        self.t = HttpTransport(hub_url, token, timeout=30)
        self.name = name
        self.actions = list(actions if actions is not None else ALL_NODE_ACTIONS)
        self.info = info if info is not None else {"name": name, "version": "1.0.0", "os": "Linux",
                                                    "policy": {"preset": "standard", "exec": {"max_timeout": 300}},
                                                    "modes": {"auto_update": False}}
        self.key = key or ("ik_" + secrets.token_urlsafe(32))
        self.max_parallel = max_parallel
        self.session: Optional[str] = None

    def register(self, **over: Any) -> dict:
        body = {"node": self.name, "node_id": "n_fake" + self.name, "instance_key": self.key, "api": 1,
                "version": "1.0.0", "pid": 1, "info": self.info, "actions": self.actions, "catalog_hash": "x",
                "max_parallel": self.max_parallel, "tags": ["ignored"]}
        body.update(over)
        r = self.t.request("POST", "/v1/relay/register", body)
        self.session = r["session_id"]
        return r

    def poll(self, running: Sequence[str] = (), free_slots: int = 8, **over: Any) -> dict:
        body = {"session_id": self.session, "running": list(running), "free_slots": free_slots}
        body.update(over)
        return self.t.request("POST", "/v1/relay/poll", body, timeout=60)

    def result(self, task_id: str, envelope: dict, session: Optional[str] = None) -> dict:
        return self.t.request("POST", "/v1/relay/result", {"session_id": session or self.session,
                                                            "task_id": task_id, "envelope": envelope})

    def poll_until(self, n: int = 1, *, free_slots: int = 8, running: Sequence[str] = (), tries: int = 10
                   ) -> List[dict]:
        tasks: List[dict] = []
        for _ in range(tries):
            tasks += self.poll(running=running, free_slots=free_slots).get("tasks") or []
            if len(tasks) >= n:
                break
        return tasks


def ok_env(result: dict, rid: str = "r_x") -> dict:
    return {"ok": True, "result": result, "meta": {"request_id": rid, "api": 1}}


def call_async(fn: Any, *a: Any, **kw: Any) -> Dict[str, Any]:
    """Run fn in a thread; the returned dict gets "result"/"error" and has a "thread"."""
    box: Dict[str, Any] = {}

    def run() -> None:
        try:
            box["result"] = fn(*a, **kw)
        except BaseException as e:  # noqa: BLE001
            box["error"] = e
    t = threading.Thread(target=run, daemon=True)
    box["thread"] = t
    t.start()
    return box


def start_relay_node(hub_url: str, token: str, *, name: str = "pod-a", start_managers: bool = True,
                     **cfg: Any) -> Any:
    """A real NodeRuntime with a RelayAgent (listen disabled); ``rt.test_stop()`` stops both."""
    from hive import relay
    home = Path(tempfile.mkdtemp(prefix="hiverelay-")).resolve()
    rel = {"enabled": True, "hub_url": hub_url, "token": token}
    rel.update(cfg.pop("relay", {}))
    raw = helpers.make_node_config(cfg.pop("preset", "standard"), data_dir=str(home / "node"), name=name,
                                   node_id="n_" + name.replace("-", "") + "00000000", listen={"enabled": False},
                                   relay=rel, **cfg)
    p = home / "node" / "config.json"
    util.write_json_atomic(p, raw, private=True)
    conf = config.load_node_config(str(p))
    rt = node.NodeRuntime(conf, config_path=p, start_managers=start_managers)
    rt.start()
    agent = relay.RelayAgent(rt, conf.relay)
    agent.start()
    rt.relay = agent

    def stop() -> None:
        agent.stop()
        rt.stop()
    rt.test_stop = stop
    return rt


def hub_token_entry(tok: str, role: str = "operator", **kw: Any) -> dict:
    return dict({"name": "hub-home", "sha256": util.hash_token(tok), "kind": "hub", "role": role}, **kw)


def env_without(name: str) -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k != name}


def raise_for(env: dict) -> dict:
    if env.get("ok"):
        return env["result"]
    raise HiveError.from_dict(env.get("error"))
