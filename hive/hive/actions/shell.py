"""Action handler: shell.run (design §9.4, §10.4 "shell.run flow", WP2).

The command runs under the shim as an ephemeral job of kind ``shell``; the shim owns the hard timeout. The handler
waits for the exit, the yield point (``yield_after`` or the call deadline), the timeout, or a cancel. Handlers register
with ``@handler(name)`` and never import other action modules (§22.3 rule 2).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import textlog, util
from ..errors import bad_request, cancelled
from ..jobs import prepare_command
from . import ActionContext, handler

SHELL_LOG_MAX = 64 << 20
TIMEOUT_GRACE = 3.0
CANCEL_WAIT = 8.0
EXIT_SLACK = 8.0


def _stream_text(path: Path, max_output: int) -> Tuple[str, int, bool]:
    """(cleaned text, logical bytes, truncated): whole log when it fits, else 25 % head + 75 % tail (§9.4)."""
    size = textlog.log_size(path)
    head_raw, earliest = textlog.head_bytes(path, max_output + 1)
    if earliest == 0 and size <= max_output:
        data = head_raw[:size]
        return textlog.clean_terminal_text(util.decode_text(data)), size, False
    head_n = int(max_output * 0.25)
    tail_n = max_output - head_n
    head = head_raw[:head_n] if earliest == 0 else b""
    tail = textlog.read_at(path, max(earliest, size - tail_n), tail_n)
    omitted = size - len(head) - len(tail)
    head_text = textlog.decode_chunk(head)[0] if head else ""
    text = (textlog.clean_terminal_text(head_text) + f"\n…[{omitted} bytes omitted]…\n"
            + textlog.clean_terminal_text(util.decode_text(tail)))
    return text, size, True


def collect_output(jm: Any, job_id: str, max_output: int, merge: bool) -> Dict[str, Any]:
    """output (or stdout/stderr), output_bytes and truncated for a shell job."""
    rec = jm.record(job_id)
    rd = jm.run_dir(job_id)
    logs = rec.get("logs") or {"output": "output.log"}
    out: Dict[str, Any] = {}
    if "output" in logs:
        text, n, trunc = _stream_text(rd / logs["output"], max_output)
        out.update(output=text, output_bytes=n, truncated=trunc)
    else:
        so, n1, t1 = _stream_text(rd / logs["stdout"], max_output)
        se, n2, t2 = _stream_text(rd / logs["stderr"], max_output)
        out.update(stdout=so, stderr=se, output_bytes=n1 + n2, truncated=t1 or t2)
    return out


_STATE = {"exited": "exited", "timeout": "timeout", "killed": "killed", "lost": "killed"}


@handler("shell.run")
def shell_run(ctx: ActionContext, p: dict) -> dict:
    """Run a command (or argv) and return its exit code and output; long commands yield to a job."""
    jm = ctx.rt.jobs
    lim = ctx.policy.limits
    t = ctx.policy.check_timeout(p.get("timeout"), lim.exec_default_timeout, lim.exec_max_timeout)
    plan = prepare_command(ctx.policy, p)
    yield_after: Optional[float] = p.get("yield_after")
    on_timeout = p.get("on_timeout") or "kill"
    if yield_after is not None and on_timeout == "kill" and float(yield_after) >= t:
        # M1 integration: the Toolkit sends yield_after = min(timeout, w) (§17.4), i.e. yield_after == timeout for every
        # timeout <= the wait budget. Yielding when the shim kills the command anyway reported a killed command as
        # "still running → background job"; wait for the shim's timeout result instead, at most TIMEOUT_GRACE + 1 s
        # past the timeout (then yield as before, so the call stays inside the client's wait budget).
        yield_after = t + TIMEOUT_GRACE + 1.0
    if yield_after is None and t > ctx.remaining() - 5:
        yield_after = max(1.0, ctx.remaining() - 5)   # auto-yield instead of failing at the call deadline
    max_output = max(1024, min(int(p.get("max_output") or 65536), int(lim.max_output_bytes)))
    merge = bool(p.get("merge_stderr", True))
    info = jm.start_job(ctx, kind="shell", argv_or_cmdline=plan.argv, shell=plan.shell, cwd=plan.cwd, env=plan.env,
                        stdin=p.get("stdin"), name=plan.display[:40], timeout=t if on_timeout == "kill" else 0,
                        timeout_grace=TIMEOUT_GRACE, merge_stderr=merge, log_max_bytes=SHELL_LOG_MAX, log_keep=1,
                        command_display=plan.display, extra_files=plan.files or None)
    jid = info["job_id"]
    ctx.audit_extra["job_id"] = jid
    t0 = time.monotonic()
    yield_at = t0 + float(yield_after) if yield_after else float("inf")
    outcome = "exited"
    while True:
        if jm.wait_final(jid, 0.05, ctx.cancel):
            outcome = "exited"
            break
        now = time.monotonic()
        if ctx.cancel.is_set():
            jm.request_stop(jid, TIMEOUT_GRACE, False)
            jm.wait_final(jid, CANCEL_WAIT)
            raise cancelled("shell.run cancelled; the command was stopped")
        if on_timeout == "background" and now - t0 >= t:
            outcome = "background"
            break
        if on_timeout == "kill" and now - t0 >= t + TIMEOUT_GRACE + EXIT_SLACK:
            outcome = "yield"   # the shim has not written exit.json yet: hand over the job id
            break
        if now >= yield_at or ctx.remaining() <= 2:
            outcome = "yield"
            break
    rec = jm.record(jid)
    info = jm.info(jid, tail_lines=0)
    res: Dict[str, Any] = {"shell": plan.shell, "cwd": str(plan.cwd), "pid": info.get("pid"),
                           "duration_s": info.get("duration_s") or round(time.monotonic() - t0, 3)}
    if outcome in ("yield", "background"):
        jm.promote(jid, name=plan.display[:40])
        res.update(collect_output(jm, jid, max_output, merge))
        res.update(state="running", exit_code=None, signal=None, job_id=jid)
        ctx.audit_extra["yielded"] = True
        return _order(res)
    state = rec.get("state")
    if state == "failed":
        jm.remove(jid)
        raise bad_request(f"could not start the command: {rec.get('error') or 'unknown error'}",
                          hint="check the program name and the node PATH (system_info which=[…])")
    res.update(collect_output(jm, jid, max_output, merge))
    res.update(state=_STATE.get(state, "killed"), exit_code=rec.get("exit_code"), signal=rec.get("signal"))
    if res["truncated"]:
        jm.mark_ephemeral(jid)    # the full log stays readable for 1 h with job_logs
        res["job_id"] = jid
    else:
        jm.remove(jid)
        res["job_id"] = None
    ctx.audit_extra["exit_code"] = rec.get("exit_code")
    return _order(res)


_ORDER: List[str] = ["state", "exit_code", "signal", "duration_s", "shell", "cwd", "pid", "output", "stdout",
                     "stderr", "output_bytes", "truncated", "job_id"]


def _order(res: Dict[str, Any]) -> Dict[str, Any]:
    return {k: res[k] for k in _ORDER if k in res}


__all__ = ["shell_run", "collect_output"]
