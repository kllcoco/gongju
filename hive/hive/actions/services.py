"""Action handlers: service.define, service.list, service.status, service.start, service.stop, service.restart,
service.remove, service.logs (design §9.6, WP2).

Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

from ..errors import cancelled
from . import ActionContext, handler


def _wait(ctx: ActionContext, wait: float) -> float:
    return max(0.0, min(float(wait or 0), ctx.remaining() - 1.0))


@handler("service.define")
def service_define(ctx: ActionContext, p: dict) -> dict:
    """Define (or update) a supervised service; starts it unless start=false."""
    out = ctx.rt.services.define(ctx, p)
    ctx.audit_extra.update(service=p.get("name"), created=out.get("created"), restarted=out.get("restarted"))
    return out


@handler("service.list")
def service_list(ctx: ActionContext, p: dict) -> dict:
    """All services with their state."""
    return ctx.rt.services.list()


@handler("service.status")
def service_status(ctx: ActionContext, p: dict) -> dict:
    """One service's full status; optionally wait until it settles."""
    out = ctx.rt.services.status(p["name"], tail_lines=int(p.get("tail_lines", 30)), wait=_wait(ctx, p.get("wait")))
    if ctx.cancel.is_set():
        raise cancelled("cancelled by the caller")
    return out


@handler("service.start")
def service_start(ctx: ActionContext, p: dict) -> dict:
    """Start a service (only waits when it is already starting or running)."""
    out = ctx.rt.services.start_service(ctx, p["name"], _wait(ctx, p.get("wait")))
    ctx.audit_extra.update(service=p["name"], pid=out.get("pid"), status=out.get("state"))
    return out


@handler("service.stop")
def service_stop(ctx: ActionContext, p: dict) -> dict:
    """Stop a service (desired stopped)."""
    out = ctx.rt.services.stop_service(ctx, p["name"], _wait(ctx, p.get("wait", 20)))
    ctx.audit_extra.update(service=p["name"], status=out.get("state"))
    return out


@handler("service.restart")
def service_restart(ctx: ActionContext, p: dict) -> dict:
    """Stop and start a service again."""
    out = ctx.rt.services.restart(ctx, p["name"], _wait(ctx, p.get("wait")))
    ctx.audit_extra.update(service=p["name"], pid=out.get("pid"), status=out.get("state"))
    return out


@handler("service.remove")
def service_remove(ctx: ActionContext, p: dict) -> dict:
    """Stop and delete a service definition (and optionally its logs)."""
    out = ctx.rt.services.remove(ctx, p["name"], bool(p.get("delete_logs")), _wait(ctx, p.get("wait", 30)))
    ctx.audit_extra.update(service=p["name"], pending=out.get("pending"))
    return out


@handler("service.logs")
def service_logs(ctx: ActionContext, p: dict) -> dict:
    """A range of a service's log (by offset, or the tail) plus a short status."""
    return ctx.rt.services.logs(p["name"], offset=p.get("offset"), tail_lines=int(p.get("tail_lines", 100)),
                                max_bytes=int(p.get("max_bytes", 32768)), wait=_wait(ctx, p.get("wait")),
                                stream=p.get("stream") or "output", raw=bool(p.get("raw")), cancel=ctx.cancel)


__all__ = ["service_define", "service_list", "service_status", "service_start", "service_stop", "service_restart",
           "service_remove", "service_logs"]
