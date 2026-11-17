"""Action handlers: job.start, job.status, job.logs, job.list, job.stop (design §9.5, WP2).

Handlers register with ``@handler(name)`` from ``hive.actions`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

from typing import Any, Dict

from ..errors import bad_request, cancelled
from ..jobs import keep_awake_warnings, prepare_command
from . import ActionContext, handler

@handler("job.start")
def job_start(ctx: ActionContext, p: dict) -> dict:
    """Start a background job under the shim."""
    lim = ctx.policy.limits
    if float(lim.jobs_max_runtime or 0) > 0:
        t = ctx.policy.check_timeout(p.get("timeout") or None, lim.jobs_max_runtime, lim.jobs_max_runtime)
    else:
        t = float(p.get("timeout") or 0)
    plan = prepare_command(ctx.policy, p)
    name = p.get("name") or plan.display[:40]
    info = ctx.rt.jobs.start_job(ctx, kind="job", argv_or_cmdline=plan.argv, shell=plan.shell, cwd=plan.cwd,
                                 env=plan.env, stdin=p.get("stdin"), name=name, timeout=t, timeout_grace=10.0,
                                 merge_stderr=bool(p.get("merge_stderr", True)),
                                 log_max_bytes=int(p.get("log_max_mb") or 64) << 20, log_keep=1,
                                 keep_awake=bool(p.get("keep_awake")), tags=p.get("tags") or (),
                                 command_display=plan.display, extra_files=plan.files or None)
    jid = info["job_id"]
    ctx.audit_extra.update(job_id=jid, pid=info.get("pid"))
    if info.get("state") == "failed":
        raise bad_request(f"could not start the job: {info.get('error') or 'unknown error'}",
                          hint="check the program name and the node PATH (system_info which=[…])", job_id=jid)
    rec = ctx.rt.jobs.record(jid)
    logs = rec.get("logs") or {"output": "output.log"}
    log_path = str(ctx.rt.jobs.run_dir(jid) / (logs.get("output") or logs.get("stdout")))
    state = info.get("state")
    out: Dict[str, Any] = {"job_id": jid, "name": info.get("name"), "pid": info.get("pid"),
                           "state": "running" if state in ("starting", "running") else state,
                           "started_at": info.get("started_at") or info.get("created_at"), "log_path": log_path}
    warnings = keep_awake_warnings(bool(p.get("keep_awake")))
    if warnings:
        out["warnings"] = warnings
    return out


def _bounded_wait(ctx: ActionContext, wait: float) -> float:
    return max(0.0, min(float(wait or 0), ctx.remaining() - 1.0))


@handler("job.status")
def job_status(ctx: ActionContext, p: dict) -> dict:
    """JobInfo, optionally waiting for the job to end."""
    jm = ctx.rt.jobs
    wait = _bounded_wait(ctx, p.get("wait") or 0)
    if wait > 0:
        jm.wait_final(p["job_id"], wait, ctx.cancel)
        if ctx.cancel.is_set():
            raise cancelled("cancelled by the caller")
    return jm.get(p["job_id"], tail_lines=int(p.get("tail_lines", 20)))


@handler("job.logs")
def job_logs(ctx: ActionContext, p: dict) -> dict:
    """A range of a job's log (by offset, or the tail)."""
    return ctx.rt.jobs.logs(p["job_id"], offset=p.get("offset"), tail_lines=int(p.get("tail_lines", 100)),
                            max_bytes=int(p.get("max_bytes", 32768)), wait=_bounded_wait(ctx, p.get("wait") or 0),
                            stream=p.get("stream") or "output", raw=bool(p.get("raw")), cancel=ctx.cancel)


@handler("job.list")
def job_list(ctx: ActionContext, p: dict) -> dict:
    """Jobs, newest first."""
    return ctx.rt.jobs.list(state=p.get("state") or "all", limit=int(p.get("limit", 20)),
                            include_shell=bool(p.get("include_shell")))


@handler("job.stop")
def job_stop(ctx: ActionContext, p: dict) -> dict:
    """Stop a job (graceful through the shim, hard after grace)."""
    info = ctx.rt.jobs.stop(p["job_id"], force=bool(p.get("force")), grace=float(p.get("grace", 10)))
    ctx.audit_extra.update(job_id=p["job_id"], exit_code=info.get("exit_code"))
    return info


__all__ = ["job_start", "job_status", "job_logs", "job_list", "job_stop"]
