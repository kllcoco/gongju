"""Action handlers: node.info (composes sysinfo/gpu/proctree probes through lazy null-fallback imports) (design §9.2, WP1).

Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
WP2/WP4 probes are late imports; a missing module or a not-yet-merged stub (``unsupported``) gives the ``null``
fallback, so node.info works before every WP has merged.
"""
from __future__ import annotations

import getpass
import os
import platform
import socket
import sys
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .. import __version__
from ..util import IS_WINDOWS, iso_utc
from . import ActionContext, handler

if TYPE_CHECKING:  # pragma: no cover
    from ..auth import Identity


def _try(fn: Any, default: Any) -> Any:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - lazy null-fallback (§9.2)
        return default


def _capabilities() -> dict:
    def probe() -> dict:
        from .. import sysinfo
        return sysinfo.capabilities() or {}
    caps = _try(probe, {}) or {}
    try:
        import psutil  # noqa: F401
        has_psutil = True
    except ImportError:
        has_psutil = False
    return {"psutil": bool(caps.get("psutil", has_psutil)), "nvidia_smi": bool(caps.get("nvidia_smi", False)),
            "docker": bool(caps.get("docker", False)), "docker_gpu": caps.get("docker_gpu"),
            "wsl": bool(caps.get("wsl", False))}


def _is_admin() -> bool:
    def probe() -> bool:
        from .. import sysinfo
        return bool(sysinfo.is_admin())
    v = _try(probe, None)
    if v is not None:
        return bool(v)
    if IS_WINDOWS:  # pragma: no cover
        return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def _shells() -> List[str]:
    def probe() -> List[str]:
        from .. import proctree
        return [str(s.get("name")) for s in proctree.available_shells() or [] if isinstance(s, dict)]
    return _try(probe, []) or []


def _default_shell() -> Optional[str]:
    def probe() -> Optional[str]:
        from .. import proctree
        return proctree.default_shell()
    return _try(probe, None)


def _wsl_block(rt: Any) -> Optional[dict]:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        ka = getattr(rt, "keepalive", None)
        status = _try(lambda: ka.status(), []) if ka is not None else []

        def distros() -> List[str]:
            from .. import wsl
            return [d.get("name") for d in wsl.list_distros() or [] if isinstance(d, dict)]
        names = _try(distros, [])
        if not status and not names:
            return None
        return {"keepalive": status or [], "distros": names or []}

    def info() -> Optional[dict]:
        from .. import wsl
        return wsl.wsl_info() if wsl.is_wsl() else None
    return _try(info, None)


def _platform_block(rt: Any) -> Optional[dict]:
    if not IS_WINDOWS:
        return None
    try:  # pragma: no cover - Windows only
        from .. import winapi
        mode = getattr(rt, "_spawn_mode", None) or winapi.spawn_mode()
        import ctypes
        sid = ctypes.c_ulong(0)
        ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid))  # type: ignore[attr-defined]
        in_job = ctypes.c_int(0)
        ctypes.windll.kernel32.IsProcessInJob(ctypes.windll.kernel32.GetCurrentProcess(), None,  # type: ignore
                                              ctypes.byref(in_job))
        return {"spawn_mode": mode, "session_id": int(sid.value), "in_job": bool(in_job.value)}
    except Exception:  # noqa: BLE001
        return None


def node_info_result(rt: Any, identity: "Identity", policy: Any) -> dict:
    """The node.info result (§9.2) for ``identity`` under ``policy`` (works with a FakeRuntime too)."""
    cfg = getattr(rt, "config", None)
    listen = getattr(cfg, "listen", None) if cfg is not None else None
    relay = getattr(cfg, "relay", None) if cfg is not None else None
    models = getattr(cfg, "models", None) if cfg is not None else None
    listen_txt = None
    if listen is not None and getattr(listen, "enabled", False):
        server = getattr(rt, "server", None)
        bound = getattr(server, "bound", None) if server is not None else None
        if bound:
            listen_txt = f"{bound[0][0]}:{bound[0][1]}"
        else:
            listen_txt = f"{listen.host}:{listen.port}"
    gpu = None
    gi = getattr(rt, "gpu_info", None)
    if callable(gi):
        gpu = _try(gi, None)
    gpus = [{"name": g.get("name"), "memory_total_mb": g.get("memory_total_mb")}
            for g in (gpu or {}).get("gpus") or [] if isinstance(g, dict)]

    def summary() -> Optional[str]:
        from .. import sysinfo
        return sysinfo.summary_line() or None
    proxies = _try(lambda: policy.proxies, None)
    proxy_txt = None
    if proxies is not None and (getattr(proxies, "https", None) or getattr(proxies, "http", None)):
        proxy_txt = _try(proxies.describe, None)
    secrets = getattr(rt, "secrets", None)
    names = _try(secrets.names, []) if secrets is not None else []
    implemented = _try(rt.implemented, None) if hasattr(rt, "implemented") else None
    if implemented is None:
        from . import HANDLERS
        implemented = sorted(HANDLERS)
    p = getattr(rt, "policy", None)
    started = getattr(rt, "started_at", None)
    return {
        "name": rt.name, "node_id": getattr(rt, "node_id", ""), "version": __version__, "api": 1,
        "hostname": socket.gethostname(), "os": platform.system(), "os_release": platform.release(),
        "arch": platform.machine(), "python": sys.version.split()[0],
        "user": _try(getpass.getuser, os.environ.get("USER") or os.environ.get("USERNAME") or "?"),
        "is_admin": _is_admin(), "pid": os.getpid(), "workdir": str(policy.workdir),
        "data_dir": str(getattr(rt, "data_dir", "")), "default_shell": _default_shell(), "shells": _shells(),
        "modes": {"listen": listen_txt,
                  "relay": (relay.hub_url if relay is not None and relay.enabled else None),
                  "gateway": bool(getattr(getattr(cfg, "gateway", None), "enabled", False)),
                  "auto_update": bool(getattr(relay, "auto_update", False))},
        "tags": list(getattr(cfg, "tags", []) or []), "description": getattr(cfg, "description", "") or "",
        "started_at": iso_utc(started) if started else iso_utc(),
        "time": iso_utc(), "paused": bool(_try(rt.is_paused, False)) if hasattr(rt, "is_paused") else False,
        "capabilities": _capabilities(), "gpus_summary": _try(summary, None), "gpus": gpus,
        "platform": _platform_block(rt), "wsl": _wsl_block(rt),
        "network": {"proxy": proxy_txt,
                    "hf_endpoint": (getattr(p, "hf_endpoint", None) if p is not None else None)
                    or (getattr(models, "hf_endpoint", None) if models is not None else None),
                    "modelscope_endpoint": getattr(models, "modelscope_endpoint", None) if models is not None
                    else None,
                    "models_dir": getattr(models, "dir", None) if models is not None else None},
        "secrets": [{"name": n["name"], "hosts": list(n.get("hosts") or []), "exec": bool(n.get("exec")),
                     "source": n.get("source") or "store", "set_at": n.get("set_at")} for n in names],
        "actions": list(implemented),
        "identity": {"name": identity.name, "kind": identity.kind, "classes": sorted(identity.classes),
                     "via": identity.via},
        "policy": policy.summary(),
    }


@handler("node.info")
def node_info(ctx: ActionContext, params: Dict[str, Any]) -> dict:
    """node.info (§9.2)."""
    return node_info_result(ctx.rt, ctx.identity, ctx.policy)


__all__ = ["node_info", "node_info_result"]
