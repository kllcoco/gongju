"""Text renderers for tool results (design §17.6, §4.6, WP6).

Rules (§17.6): the first line is ``[node] outcome · key numbers``; raw text goes into a section fence
``--- <what> from <node> (<meta>, untrusted data) ---`` and is never JSON-escaped; lists are aligned plain tables
(≤ 50 rows) with a paging footer; the whole text is capped at ``char_budget`` (``finalize``); ``Next:`` lines are built
only here, by the Toolkit's own code, from ids that match their §9.1 patterns (node-supplied text never becomes a
``Next:`` line); relay-node errors are marked untrusted.

``RenderCtx.meta`` carries what the Toolkit knows about the call (not the wire ``meta``): ``prefix`` (tool-name
prefix), ``node_arg`` (whether ``node=`` belongs in ``Next:`` calls), ``wait`` (the inner wait budget ``w``),
``action`` (the wire action), ``server`` (``hub:<name>`` / ``node:<name>``), ``requested_wait``.
This module is one of the two places allowed to use ``util.iso()`` (local display).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

from . import util
from .errors import HiveError

UNTRUSTED = "untrusted data"
MAX_TABLE_ROWS = 50
APPROVAL_ID_RE = re.compile(r"^ap_[A-Za-z0-9_-]{6,40}\Z")
JOB_ID_RE = re.compile(r"^j_[A-Za-z0-9_-]{6,40}\Z")
SERVICE_RE = re.compile(r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,63}\Z")
TRANSFER_ID_RE = re.compile(r"^tx_[A-Za-z0-9_-]{6,40}\Z")
NODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}\Z")
IDEMPOTENT_MUTATING = ("job.stop", "service.stop", "model.pull")
POLICY_SENTENCE = "Do not try to work around node policy; ask the user if access is needed."
MAYBE_SENTENCE = ("The command may or may not have run (connection lost). Check state (job_list / fs_op stat) "
                  "before retrying.")
CHAT_TIMEOUT = ("no reply within {s}s (client time limit); lower max_tokens or run longer benchmarks with "
                "job_start")
PREFLIGHT_TIMEOUT = "listing still in progress; call again (it is cached) or narrow include"


@dataclass
class RenderCtx:
    """What a renderer knows about the call."""
    tool: str
    node: Optional[str]
    node_mode: str
    args: dict
    meta: dict
    char_budget: int


# ---------------------------------------------------------------------------------------------------------------
# small formatting helpers

def _s(v: Any) -> str:
    return "" if v is None else str(v)


def clean(text: Any, limit: int = 0) -> str:
    """Node-supplied single-line text: control/bidi characters escaped, optionally length-capped."""
    s = util.escape_terminal(_s(text)).replace("\n", "\\n").replace("\t", " ")
    if limit and len(s) > limit:
        s = s[:limit] + "…"
    return s


def human_bytes(n: Any) -> str:
    """``23.9 GiB`` (``-`` for None)."""
    if n is None or isinstance(n, bool):
        return "-"
    try:
        return util.human_bytes(int(n))
    except (TypeError, ValueError):
        return "-"


def human_duration(s: Any) -> str:
    """``2d3h``, ``4m12s``, ``1.24s`` (``-`` for None)."""
    if s is None or isinstance(s, bool):
        return "-"
    try:
        v = float(s)
    except (TypeError, ValueError):
        return "-"
    if v < 0:
        v = 0.0
    if v < 10:
        return f"{v:.2f}s"
    if v < 60:
        return f"{int(round(v))}s"
    m, sec = divmod(int(v), 60)
    if m < 60:
        return f"{m}m{sec:02d}s" if sec else f"{m}m"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, h = divmod(h, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def rel_time(ts: Any, now: Optional[float] = None) -> str:
    """ISO UTC timestamp -> ``3m ago`` (``-`` when absent/unparseable)."""
    if not ts:
        return "-"
    try:
        t = util.parse_iso(str(ts))
    except (HiveError, ValueError, TypeError):
        return clean(ts, 40)
    delta = (now if now is not None else time.time()) - t
    if delta < 0:
        return "in " + human_duration(-delta)
    if delta < 5:
        return "just now"
    return human_duration(int(delta)) + " ago"


def local_time(ts: Any) -> str:
    """ISO UTC -> local ISO (display only)."""
    try:
        return util.iso(util.parse_iso(str(ts)))
    except (HiveError, ValueError, TypeError):
        return clean(ts, 40)


def head(ctx: RenderCtx, outcome: str, *nums: Any) -> str:
    """First line ``[node] outcome · n1 · n2``."""
    parts = [p for p in (_s(x) for x in nums) if p]
    node = f"[{ctx.node}] " if ctx.node else ""
    return node + outcome + ("".join(" · " + p for p in parts))


def fence(what: str, ctx: RenderCtx, text: Any, meta: str = "") -> str:
    """Untrusted section ``--- <what> from <node> (<meta>, untrusted data) ---`` followed by the raw text."""
    body = _s(text)
    where = f" from {ctx.node}" if ctx.node else ""
    inner = f"{meta}, {UNTRUSTED}" if meta else UNTRUSTED
    if body and not body.endswith("\n"):
        body += "\n"
    return f"--- {what}{where} ({inner}) ---\n{body}".rstrip("\n")


def table(rows: Sequence[Sequence[Any]], cols: Sequence[str], *, total: Optional[int] = None, offset: int = 0,
          more_hint: Optional[str] = None) -> str:
    """Aligned plain table (≤ 50 rows) with a paging footer ``… N more (use offset=K)``."""
    shown = list(rows)[:MAX_TABLE_ROWS]
    cells = [[clean(c, 120) if c is not None else "-" for c in r] for r in shown]
    widths = [len(c) for c in cols]
    for r in cells:
        for i, c in enumerate(r[:len(cols)]):
            widths[i] = max(widths[i], len(c))
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()]
    for r in cells:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r[:len(cols)])).rstrip())
    count = total if isinstance(total, int) else len(rows)
    rest = count - offset - len(shown)
    if rest > 0:
        lines.append(f"… {rest} more ({more_hint or f'use offset={offset + len(shown)}'})")
    return "\n".join(lines)


def _q(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)


def next_call(ctx: RenderCtx, tool: str, **kw: Any) -> str:
    """``Next: tool(node="x", k=v…)`` built from Toolkit-validated values only ("" if an id fails its pattern)."""
    for key, rx in (("job_id", JOB_ID_RE), ("name", SERVICE_RE), ("transfer_id", TRANSFER_ID_RE)):
        if key in kw and (not isinstance(kw[key], str) or not rx.match(kw[key])):
            return ""
    args: List[str] = []
    if ctx.meta.get("node_arg") and ctx.node and NODE_RE.match(ctx.node):
        args.append(f"node={_q(ctx.node)}")
    for k, v in kw.items():
        if v is None:
            continue
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        args.append(f"{k}={_q(v)}")
    return f"Next: {ctx.meta.get('prefix', '')}{tool}({', '.join(args)})"


def _wait(ctx: RenderCtx) -> int:
    try:
        return max(1, int(float(ctx.meta.get("wait") or 40)))
    except (TypeError, ValueError):
        return 40


def extras(result: dict) -> List[str]:
    """``Warning: …`` lines for ``result.warnings`` (v7)."""
    out = []
    for w in result.get("warnings") or []:
        out.append("Warning: " + clean(w, 500))
    return out


def masked_line(result: dict) -> List[str]:
    """``(secret values were masked)`` when the node masked the result (§13.2 step 10a)."""
    return ["(secret values were masked)"] if isinstance(result, dict) and result.get("masked") is True else []


def join(*parts: Any) -> str:
    """Join non-empty parts/lists with newlines."""
    out: List[str] = []
    for p in parts:
        if isinstance(p, (list, tuple)):
            out.extend(_s(x) for x in p if _s(x))
        elif _s(p):
            out.append(_s(p))
    return "\n".join(out)


def finalize(text: str, char_budget: int) -> str:
    """Cap the whole text at ``char_budget`` (25 % head, 75 % tail), naming the omitted size."""
    if char_budget and len(text) > char_budget:
        cut, _omitted = util.truncate_text(text, char_budget, head_ratio=0.25)
        return cut
    return text


def redact(s: Any) -> str:
    """``audit.redact_text`` for command lines and definitions (§17.9 item 4)."""
    try:
        from .audit import redact_text
        return redact_text(_s(s))
    except Exception:  # noqa: BLE001
        return _s(s)


def _kv(d: Any, keys: Sequence[str]) -> str:
    if not isinstance(d, dict):
        return ""
    return ", ".join(f"{k}={clean(d.get(k), 80)}" for k in keys if d.get(k) is not None)


# ---------------------------------------------------------------------------------------------------------------
# generic + errors

def generic(result: dict, args: dict, ctx: RenderCtx) -> str:
    """Fallback renderer: header + ``key: value`` lines (nested values as compact JSON)."""
    lines = [head(ctx, ctx.meta.get("action") or ctx.tool)]
    lines += extras(result)
    if isinstance(result, dict):
        for k, v in result.items():
            if k in ("masked", "warnings"):
                continue
            if isinstance(v, str) and "\n" in v:
                lines.append(fence(k, ctx, v))
            elif isinstance(v, (dict, list)):
                lines.append(f"{k}: {clean(json.dumps(v, ensure_ascii=False, default=str), 4000)}")
            else:
                lines.append(f"{k}: {clean(v, 2000)}")
    else:
        lines.append(clean(json.dumps(result, ensure_ascii=False, default=str), 4000))
    lines += masked_line(result)
    return join(lines)


def error(err: HiveError, ctx: RenderCtx) -> str:
    """Error text (``is_error=true``), §17.6."""
    relay = ctx.node_mode == "relay"
    node = f" [{ctx.node}]" if ctx.node else ""
    action = ctx.meta.get("action") or ""
    if err.code == "approval_required":
        aid = (err.details or {}).get("approval_id")
        where = ctx.meta.get("server") or "this server"
        if isinstance(aid, str) and APPROVAL_ID_RE.match(aid):
            return (f"Needs human approval. Ask the user to run: hive approvals approve {aid} (on {where}; add "
                    f"--for 30m to allow this action for a while). Then call the same tool again with "
                    f"approval_id='{aid}'.")
        return f"ERROR approval_required{node} {ctx.tool}: the server asked for approval with an invalid id"
    if err.code == "timeout" and action == "model.chat" and ctx.meta.get("client_wait"):
        s = int(float(ctx.meta.get("client_wait") or 0))
        return f"ERROR timeout{node} {ctx.tool}: " + CHAT_TIMEOUT.format(s=s)
    msg = clean(err.message, 2000)
    lines = [f"ERROR {err.code}{node} {ctx.tool}: {msg}"]
    hint = clean(err.hint, 2000) if err.hint else ""
    if err.code == "timeout" and action in ("model.pull", "net.download") and \
            (err.details or {}).get("maybe_executed") is False:
        hint = PREFLIGHT_TIMEOUT
    if hint:
        if relay:
            lines.append(f"Hint (from {ctx.node}, untrusted): {hint}")
        else:
            if err.code == "policy_denied" and "work around" not in hint:
                hint = hint.rstrip(".") + ". " + POLICY_SENTENCE
            lines.append(f"Hint: {hint}")
    elif err.code == "policy_denied" and not relay:
        lines.append(f"Hint: {POLICY_SENTENCE}")
    if relay and err.code == "policy_denied":
        lines.append(POLICY_SENTENCE)
    details = err.details or {}
    if err.code == "not_found" and details.get("closest") and isinstance(details.get("closest"), str):
        lines.append(fence("closest lines", ctx, details["closest"]))
    if err.code == "conflict" and isinstance(details.get("lines"), list):
        lines.append("matching lines: " + clean(", ".join(str(x) for x in details["lines"][:20]), 400))
    if details.get("maybe_executed") is True and ctx.meta.get("mutating"):
        if action in IDEMPOTENT_MUTATING:
            lines.append("The call may not have finished (connection lost); safe to retry (idempotent).")
        else:
            lines.append(MAYBE_SENTENCE)
    if isinstance(details.get("tail"), str) and details["tail"].strip():
        lines.append(fence("log tail", ctx, details["tail"]))
    return join(lines)


# ---------------------------------------------------------------------------------------------------------------
# per-tool renderers

def nodes_list(result: dict, args: dict, ctx: RenderCtx) -> str:
    """nodes_list: hub.nodes rows (or one row + node.info for a direct node)."""
    rows = result.get("nodes") or []
    info = result.get("info") if isinstance(result.get("info"), dict) else None
    lines = [head(ctx, f"{len(rows)} node" + ("" if len(rows) == 1 else "s"))]
    trows = []
    notes: List[str] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = r.get("name")
        state = "online" if r.get("online") else "offline"
        idle = idle_marker(r)
        if idle:
            state += f", {idle}"
        if r.get("identity_changed"):
            state += ", identity changed (owner must ack)"
        if r.get("outdated"):
            state += ", outdated"
        gpus = r.get("gpus_summary") if isinstance(r.get("gpus_summary"), str) and \
            re.match(r"^[\w .,×()+/-]{0,80}\Z", r.get("gpus_summary") or "") else None
        trows.append([name, state, r.get("mode"), r.get("os_label") or r.get("os"), gpus or "-",
                      ",".join(str(t) for t in (r.get("tags") or [])) or "-"])
        sec = [str(s) for s in (r.get("secrets") or []) if isinstance(s, str)]
        desc = r.get("description")
        if sec:
            notes.append(f"{clean(name, 64)} secrets: {clean(', '.join(sec), 400)}")
        if desc:
            notes.append(f"{clean(name, 64)}: {clean(desc, 200)}")
    lines.append(table(trows, ["NAME", "STATE", "MODE", "OS", "GPUS", "TAGS"]))
    lines += notes
    if info:
        lines.append(f"shell: {clean(info.get('default_shell'), 20)} (available: "
                     f"{clean(', '.join(str(s) for s in info.get('shells') or []), 80)})")
        pol = info.get("policy") if isinstance(info.get("policy"), dict) else {}
        roots = ", ".join(f"{r.get('path')}({r.get('mode')})" for r in pol.get("fs_roots") or []
                          if isinstance(r, dict))
        if roots:
            lines.append(f"policy: preset {clean(pol.get('preset'), 20)}; fs roots: {clean(roots, 400)}")
        net = info.get("network") if isinstance(info.get("network"), dict) else {}
        if _kv(net, ("proxy", "hf_endpoint", "models_dir")):
            lines.append("network: " + _kv(net, ("proxy", "hf_endpoint", "models_dir")))
        sec = [s.get("name") for s in info.get("secrets") or [] if isinstance(s, dict)]
        if sec and not any(isinstance(r, dict) and r.get("secrets") for r in rows):
            lines.append("secrets: " + clean(", ".join(str(s) for s in sec), 400))
    return join(lines)


def idle_marker(row: dict) -> str:
    """``idle 3h12m (GPU 0%)`` for a ``cloud`` node with no routed call for ≥ 1 h and GPU util < 5 % (§17.6)."""
    tags = [str(t) for t in row.get("tags") or []]
    if "cloud" not in tags:
        return ""
    idle = row.get("idle_s")
    if not isinstance(idle, (int, float)) or isinstance(idle, bool) or idle < 3600:
        return ""
    st = row.get("status") if isinstance(row.get("status"), dict) else {}
    util_pct = st.get("gpu_util_pct")
    if util_pct is None:
        gl = st.get("gpus") if isinstance(st.get("gpus"), list) else []
        vals = [g.get("utilization_pct") for g in gl if isinstance(g, dict)
                and isinstance(g.get("utilization_pct"), (int, float))]
        util_pct = max(vals) if vals else None
    if isinstance(util_pct, (int, float)) and not isinstance(util_pct, bool) and util_pct >= 5:
        return ""
    gtxt = f" (GPU {int(util_pct)}%)" if isinstance(util_pct, (int, float)) else ""
    m = int(idle) // 60
    return f"idle {m // 60}h{m % 60:02d}m{gtxt}"


def sys_info(result: dict, args: dict, ctx: RenderCtx) -> str:
    """sys_info: one block per section."""
    lines = [head(ctx, "system info")]
    o = result.get("os")
    if isinstance(o, dict):
        lines.append("os: " + _kv(o, ("system", "release", "distro", "machine", "hostname", "python")) +
                     (f", uptime {human_duration(o.get('uptime_s'))}" if o.get("uptime_s") is not None else ""))
        if isinstance(o.get("wsl"), dict):
            lines.append("wsl: " + _kv(o["wsl"], ("distro", "networking", "systemd")))
        cl = o.get("cloud")
        if isinstance(cl, dict) and cl.get("provider"):
            lines.append("cloud: " + _kv(cl, ("provider", "pod_id")))
    c = result.get("cpu")
    if isinstance(c, dict):
        lines.append("cpu: " + _kv(c, ("model", "logical", "physical", "percent")))
    m = result.get("memory")
    if isinstance(m, dict):
        lines.append(f"memory: {human_bytes(m.get('available'))} free of {human_bytes(m.get('total'))}"
                     f" ({clean(m.get('used_pct'), 8)}% used)")
    if isinstance(result.get("disks"), list):
        rows = [[d.get("mount"), d.get("fs"), human_bytes(d.get("free")), human_bytes(d.get("total")),
                 d.get("used_pct")] for d in result["disks"] if isinstance(d, dict)]
        lines.append(table(rows, ["MOUNT", "FS", "FREE", "TOTAL", "USED%"]))
    if isinstance(result.get("gpus"), list):
        if not result["gpus"]:
            lines.append("gpus: none" + (f" ({clean(result.get('gpu_note'), 200)})" if result.get("gpu_note") else ""))
        for g in result["gpus"]:
            if not isinstance(g, dict):
                continue
            used, tot = g.get("memory_used_mb"), g.get("memory_total_mb")
            lines.append(f"gpu {clean(g.get('index'), 4)}: {clean(g.get('name'), 80)} · VRAM "
                         f"{clean(used, 10) if used is not None else '?'}/{clean(tot, 10)} MiB · util "
                         f"{clean(g.get('utilization_pct'), 6) if g.get('utilization_pct') is not None else '?'}%"
                         + (f" · driver {clean(g.get('driver'), 20)}" if g.get("driver") else "")
                         + (f" · CUDA {clean(g.get('cuda'), 10)}" if g.get("cuda") else ""))
            for p in (g.get("processes") or [])[:10]:
                if isinstance(p, dict):
                    lines.append(f"  pid {clean(p.get('pid'), 10)} {clean(p.get('name'), 60)} "
                                 f"{clean(p.get('memory_mb'), 10)} MiB")
        if result.get("gpu_note") and result["gpus"]:
            lines.append("gpu note: " + clean(result["gpu_note"], 200))
    n = result.get("network")
    if isinstance(n, dict):
        lines.append("network: " + _kv(n, ("primary_ip", "tailscale_ip")))
    sw = result.get("software")
    if isinstance(sw, dict):
        lines.append("software:")
        for k, v in sorted(sw.items()):
            lines.append(f"  {clean(k, 30)}: {clean(v, 200) if v else 'not found'}")
    w = result.get("which")
    if isinstance(w, dict):
        for k, v in sorted((w.get("found") or {}).items()):
            v = v if isinstance(v, dict) else {}
            lines.append(f"which {clean(k, 64)}: {clean(v.get('path'), 300)}"
                         + (f" ({clean(v.get('version'), 120)})" if v.get("version") else "")
                         + (f" [{clean(v.get('note'), 80)}]" if v.get("note") else ""))
        if w.get("missing"):
            lines.append("missing: " + clean(", ".join(str(x) for x in w["missing"]), 400))
    lines += masked_line(result)
    return join(lines)


def _job_next(ctx: RenderCtx, job_id: Any) -> str:
    return next_call(ctx, "job_status", job_id=job_id, wait=_wait(ctx))


def shell_run(result: dict, args: dict, ctx: RenderCtx) -> str:
    """shell_run: exit code + output fence; yielded runs point at job_status."""
    state = result.get("state")
    shell = clean(result.get("shell"), 20)
    lines: List[str] = []
    out = result.get("output") if "output" in result else None
    job = result.get("job_id")
    if state == "running":
        ya = ctx.meta.get("yield_after") or result.get("duration_s")
        to = ctx.meta.get("timeout")
        lines.append(head(ctx, f"still running after {int(float(ya or 0))}s → background job {clean(job, 48)} "
                               f"(hard timeout {int(float(to or 0))}s; use job_start for longer work)"))
        lines += extras(result)
        text = out if out is not None else _s(result.get("stdout")) + _s(result.get("stderr"))
        if text:
            lines.append(fence("output so far", ctx, text, "tail"))
        lines.append(_job_next(ctx, job))
        lines += masked_line(result)
        return join(lines)
    if state == "exited":
        code = result.get("exit_code")
        outcome = f"exit {code}" if code is not None else f"ended by signal {clean(result.get('signal'), 12)}"
    elif state == "timeout":
        outcome = f"timeout after {human_duration(result.get('duration_s'))} (killed)"
    elif state == "killed":
        outcome = "killed" + (f" (signal {clean(result.get('signal'), 12)})" if result.get("signal") else "")
    else:
        outcome = clean(state, 20) or "done"
    lines.append(head(ctx, outcome, human_duration(result.get("duration_s")) if state == "exited" else "", shell))
    lines += extras(result)
    meta = human_bytes(result.get("output_bytes"))
    if result.get("truncated"):
        meta += ", truncated: beginning and end kept"
    if out is not None:
        if out:
            lines.append(fence("output", ctx, out, meta))
        else:
            lines.append("(no output)")
    else:
        if result.get("stdout"):
            lines.append(fence("stdout", ctx, result.get("stdout"), meta))
        if result.get("stderr"):
            lines.append(fence("stderr", ctx, result.get("stderr")))
        if not result.get("stdout") and not result.get("stderr"):
            lines.append("(no output)")
    if result.get("truncated") and job:
        lines.append(next_call(ctx, "job_logs", job_id=job, offset=0) + " to read the full log")
    lines += masked_line(result)
    return join(lines)


def job_start(result: dict, args: dict, ctx: RenderCtx) -> str:
    """job_start: id + Next."""
    lines = [head(ctx, f"started job {clean(result.get('job_id'), 48)}",
                  clean(result.get("name"), 64), f"pid {clean(result.get('pid'), 12)}" if result.get("pid") else "")]
    lines += extras(result)
    if result.get("log_path"):
        lines.append("log: " + clean(result.get("log_path"), 300))
    lines.append(_job_next(ctx, result.get("job_id")))
    lines += masked_line(result)
    return join(lines)


def _progress(p: Any) -> List[str]:
    if not isinstance(p, dict):
        return []
    out = []
    if p.get("files_total") is not None:
        out.append(f"{clean(p.get('files_done'), 8)}/{clean(p.get('files_total'), 8)} files")
    if p.get("done") is not None:
        tot = p.get("total")
        txt = human_bytes(p.get("done")) if tot is None else \
            f"{human_bytes(p.get('done')).split(' ')[0]}/{human_bytes(tot)}"
        if p.get("percent") is not None:
            txt += f" ({int(float(p['percent']))}%)"
        out.append(txt)
    if p.get("rate_bps"):
        out.append(f"{float(p['rate_bps']) / 1e6:.0f} MB/s")
    if p.get("eta_s") is not None:
        out.append("ETA " + human_duration(p.get("eta_s")))
    cur = p.get("current")
    if isinstance(cur, list) and cur:
        out.append(clean(", ".join(str(c).replace("\\", "/").rsplit("/", 1)[-1] for c in cur[:4]), 200))
    elif p.get("note"):
        out.append(clean(p.get("note"), 120))
    return out


FINAL_STATES = ("exited", "failed", "killed", "timeout", "lost")


def job_status(result: dict, args: dict, ctx: RenderCtx) -> str:
    """job_status: state, exit code, progress, tail; pull/download results; wait clamping text."""
    jid = result.get("job_id")
    kind = result.get("kind")
    state = result.get("state")
    label = f"job {clean(jid, 48)}" + (f" ({clean(kind, 10)})" if kind and kind != "job" else "")
    nums: List[str] = []
    if state == "exited":
        nums.append(f"exit {clean(result.get('exit_code'), 6)}")
    if state in FINAL_STATES or result.get("duration_s") is not None:
        nums.append(human_duration(result.get("duration_s")))
    nums += _progress(result.get("progress"))
    lines = [head(ctx, f"{label} {clean(state, 12)}", *nums)]
    if result.get("error"):
        lines.append("error: " + clean(result.get("error"), 1000))
    if result.get("command"):
        lines.append("command: " + redact(clean(result.get("command"), 300)))
    res = result.get("result")
    if isinstance(res, dict) and state in FINAL_STATES:
        if kind == "pull":
            lines.append(f"pulled {clean(res.get('repo'), 120)} @ {clean(_s(res.get('commit'))[:7], 10)} → "
                         f"{clean(res.get('path'), 300)} · {clean(res.get('files'), 8)} files · "
                         f"{human_bytes(res.get('bytes'))}")
            for k in ("skipped_invalid", "skipped_unsafe", "stale"):
                if res.get(k):
                    lines.append(f"{k}: {clean(', '.join(str(x) for x in res[k][:10]), 400)}")
        elif kind == "download":
            lines.append(f"saved {clean(res.get('path'), 300)} · {human_bytes(res.get('size'))}"
                         + (f" · sha256 {clean(res.get('sha256'), 64)}" if res.get("sha256") else ""))
        else:
            lines.append("result: " + clean(json.dumps(res, ensure_ascii=False, default=str), 1000))
    tail = result.get("tail")
    if tail:
        lines.append(fence("output", ctx, tail, "tail"))
    if state not in FINAL_STATES:
        req = ctx.meta.get("requested_wait")
        w = ctx.meta.get("used_wait")
        if req:
            if w is not None and float(req) > float(w):
                lines.append(f"waited {int(float(w))}s (client time limit); still {clean(state, 12)}")
            else:
                lines.append(f"waited {int(float(req))}s; still {clean(state, 12)}")
        lines.append(_job_next(ctx, jid))
    if kind == "pull" and state == "exited" and result.get("exit_code") == 0 and isinstance(res, dict) and \
            isinstance(res.get("path"), str) and _safe_path(res["path"]):
        lines.append(next_call(ctx, "service_define", name=_svc_name(res.get("repo")), preset="vllm",
                               preset_args=[f"model={res['path']}"]))
    lines += masked_line(result)
    return join(lines)


def _safe_path(p: str) -> bool:
    return 0 < len(p) <= 400 and not any(ord(c) < 32 or c in "‮⁦⁧⁨⁩" for c in p)


def _svc_name(repo: Any) -> str:
    base = _s(repo).rsplit("/", 1)[-1].lower()
    n = re.sub(r"[^a-z0-9._-]+", "-", base).strip("-._")[:40]
    n = re.sub(r"\.\.+", ".", n)
    return n if n and SERVICE_RE.match(n) else "model"


def _log_lines(result: dict, ctx: RenderCtx, what: str, id_key: str, id_val: Any, tool: str) -> List[str]:
    off, nxt, size = result.get("offset"), result.get("next_offset"), result.get("size")
    meta = f"bytes {clean(off, 14)}–{clean(nxt, 14)} of {clean(size, 14)}"
    if result.get("truncated"):
        meta += ", truncated"
    lines: List[str] = []
    if result.get("skipped_bytes"):
        lines.append(f"note: {human_bytes(result['skipped_bytes'])} were rotated away before this offset")
    text = result.get("text")
    lines.append(fence(what, ctx, text, meta) if text else "(no new output)")
    if isinstance(nxt, int) and not result.get("eof"):
        kw: Dict[str, Any] = {id_key: id_val, "offset": nxt}
        if result.get("state") not in FINAL_STATES:
            kw["wait"] = min(60, _wait(ctx))
        lines.append(next_call(ctx, tool, **kw))
    return lines


def job_logs(result: dict, args: dict, ctx: RenderCtx) -> str:
    """job_logs: text fence + offset paging."""
    jid = result.get("job_id") or args.get("job_id")
    lines = [head(ctx, f"job {clean(jid, 48)} log", clean(result.get("state"), 12),
                  "end of log" if result.get("eof") else "")]
    lines += _log_lines(result, ctx, "output", "job_id", args.get("job_id"), "job_logs")
    lines += masked_line(result)
    return join(lines)


def job_stop(result: dict, args: dict, ctx: RenderCtx) -> str:
    """job_stop: final state."""
    state = result.get("state")
    lines = [head(ctx, f"job {clean(result.get('job_id'), 48)} {clean(state, 12)}",
                  f"exit {clean(result.get('exit_code'), 6)}" if result.get("exit_code") is not None else "",
                  human_duration(result.get("duration_s")))]
    if result.get("error"):
        lines.append("error: " + clean(result["error"], 500))
    lines += masked_line(result)
    return join(lines)


def job_list(result: dict, args: dict, ctx: RenderCtx) -> str:
    """job_list: table."""
    jobs = [j for j in result.get("jobs") or [] if isinstance(j, dict)]
    rows = [[j.get("job_id"), j.get("name"), j.get("kind"), j.get("state"),
             j.get("exit_code") if j.get("exit_code") is not None else "-", rel_time(j.get("started_at")),
             human_duration(j.get("duration_s")), redact(clean(j.get("command"), 80))] for j in jobs]
    lines = [head(ctx, f"{len(jobs)} job" + ("" if len(jobs) == 1 else "s"),
                  f"{result.get('total')} total" if result.get("total") is not None else "")]
    if rows:
        lines.append(table(rows, ["JOB_ID", "NAME", "KIND", "STATE", "EXIT", "STARTED", "DURATION", "COMMAND"],
                           total=len(rows)))
    lines += masked_line(result)
    return join(lines)


def fs_list(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_list: entries table with paging."""
    ents = [e for e in result.get("entries") or [] if isinstance(e, dict)]
    total = result.get("total") if isinstance(result.get("total"), int) else len(ents)
    off = int(args.get("offset") or 0)
    lines = [head(ctx, clean(result.get("path"), 300), f"{total}{'+' if result.get('total_capped') else ''} "
                                                         f"entr{'y' if total == 1 else 'ies'}")]
    rows = [[e.get("type"), human_bytes(e.get("size")) if e.get("type") != "dir" else "-",
             rel_time(e.get("mtime")), e.get("path")] for e in ents]
    if rows:
        lines.append(table(rows, ["TYPE", "SIZE", "MODIFIED", "PATH"]))
    nxt = result.get("next_offset")
    shown_end = off + min(len(rows), MAX_TABLE_ROWS)
    if isinstance(nxt, int) or shown_end < off + len(rows):
        n = nxt if len(rows) <= MAX_TABLE_ROWS and isinstance(nxt, int) else shown_end
        rest = max(0, total - n)
        if rest:
            lines.append(f"… {rest} more (use offset={n})")
    lines += masked_line(result)
    return join(lines)


def fs_read(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_read: file fence + continuation."""
    sl, el, tot = result.get("start_line"), result.get("end_line"), result.get("total_lines")
    ver = _s(result.get("version"))
    lines = [head(ctx, clean(result.get("path"), 300),
                  f"lines {clean(sl, 10)}–{clean(el, 10)} of {clean(tot, 10) if tot is not None else '?'}",
                  human_bytes(result.get("size")), clean(result.get("encoding"), 20),
                  f"version {ver}" if ver else "")]
    if result.get("long_lines_cut"):
        lines.append(f"note: {clean(result['long_lines_cut'], 8)} long lines were cut")
    lines.append(fence("file", ctx, result.get("content"), clean(args.get("path") or result.get("path"), 200)))
    nxt = result.get("next_start_line")
    if isinstance(nxt, int) and isinstance(args.get("path"), str):
        lines.append(next_call(ctx, "fs_read", path=args["path"], start_line=nxt))
    lines += masked_line(result)
    return join(lines)


def fs_write(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_write."""
    lines = [head(ctx, f"wrote {human_bytes(result.get('bytes'))} to {clean(result.get('path'), 300)}",
                  "created" if result.get("created") else "", f"version {clean(result.get('version'), 64)}")]
    lines += masked_line(result)
    return join(lines)


def fs_edit(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_edit: replacements + snippet."""
    lines = [head(ctx, f"edited {clean(result.get('path'), 300)}", f"{clean(result.get('replacements'), 6)} "
                                                                    "replacement(s)",
                  f"version {clean(result.get('version'), 64)}")]
    if result.get("snippet"):
        lines.append(fence("edited region", ctx, result["snippet"]))
    lines += masked_line(result)
    return join(lines)


def fs_search(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_search: file:line: text."""
    ms = [m for m in result.get("matches") or [] if isinstance(m, dict)]
    lines = [head(ctx, f"{len(ms)} match" + ("" if len(ms) == 1 else "es"),
                  f"in {clean(result.get('files_matched'), 8)} files", f"scanned {clean(result.get('files_scanned'), 8)}",
                  human_duration(result.get("elapsed_s")))]
    if result.get("truncated"):
        lines.append("truncated" + (f" ({clean(result.get('reason'), 40)})" if result.get("reason") else "")
                     + "; narrow with path/glob or raise max_results")
    body: List[str] = []
    for m in ms:
        for b in m.get("before") or []:
            body.append(f"{_s(m.get('file'))}-{_s(b)}")
        if args.get("names_only"):
            body.append(_s(m.get("file")))
        else:
            body.append(f"{_s(m.get('file'))}:{_s(m.get('line'))}: {_s(m.get('text'))}")
        for a in m.get("after") or []:
            body.append(f"{_s(m.get('file'))}-{_s(a)}")
    if body:
        lines.append(fence("matches", ctx, "\n".join(body)))
    lines += masked_line(result)
    return join(lines)


def fs_op(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_op: one line per op."""
    op = args.get("op")
    if op == "stat":
        if not result.get("exists"):
            return head(ctx, f"{clean(result.get('path'), 300)} does not exist")
        nums = [clean(result.get("type"), 8), human_bytes(result.get("size")) if result.get("type") != "dir" else "",
                f"modified {rel_time(result.get('mtime'))}", clean(result.get("mode"), 8)]
        if result.get("sha256"):
            nums.append("sha256 " + clean(result["sha256"], 64))
        if result.get("link_target"):
            nums.append("→ " + clean(result["link_target"], 300))
        return join(head(ctx, clean(result.get("path"), 300), *nums), masked_line(result))
    if op == "mkdir":
        return head(ctx, ("created " if result.get("created") else "exists: ") + clean(result.get("path"), 300))
    if op == "move":
        return head(ctx, f"moved {clean(result.get('src'), 300)} → {clean(result.get('dst'), 300)}")
    if op == "copy":
        return head(ctx, f"copied {clean(result.get('src'), 300)} → {clean(result.get('dst'), 300)}",
                    f"{clean(result.get('files'), 8)} file(s)", human_bytes(result.get("bytes")))
    if op == "delete":
        return head(ctx, f"deleted {clean(result.get('path'), 300)}",
                    f"{clean(result.get('deleted_files'), 8)} file(s)", f"{clean(result.get('deleted_dirs'), 8)} dir(s)")
    return generic(result, args, ctx)


def fs_transfer(result: dict, args: dict, ctx: RenderCtx) -> str:
    """fs_transfer: TransferManager status."""
    st = result.get("state")
    tid = result.get("transfer_id")
    done, tot = result.get("bytes_done"), result.get("bytes_total")
    nums = [f"{human_bytes(done)} of {human_bytes(tot)}" if tot else human_bytes(done)]
    if result.get("rate_bps"):
        nums.append(f"{float(result['rate_bps']) / 1e6:.0f} MB/s")
    if result.get("eta_s") is not None and st == "running":
        nums.append("ETA " + human_duration(result.get("eta_s")))
    lines = [head(ctx, f"transfer {clean(tid, 48)} {clean(st, 12)}: {clean(result.get('src'), 300)} → "
                       f"{clean(result.get('dst'), 300)}", *nums)]
    if result.get("sha256"):
        lines.append("sha256 " + clean(result["sha256"], 64) + " (verified)")
    if result.get("error"):
        err = clean(result["error"], 500)
        lines.append("error: " + err)
        if "source changed" in err or "changed" in err:
            lines.append("source changed; run again")
    if st == "running":
        lines.append(next_call(ctx, "fs_transfer", transfer_id=tid))
    return join(lines)


def proc_list(result: dict, args: dict, ctx: RenderCtx) -> str:
    """proc_list: table."""
    ps = [p for p in result.get("processes") or [] if isinstance(p, dict)]
    rows = [[p.get("pid"), p.get("ppid"), p.get("user"), p.get("cpu_pct"), human_bytes(p.get("mem_bytes")),
             rel_time(p.get("started")), p.get("managed") or "-", redact(clean(p.get("cmd") or p.get("name"), 100))]
            for p in ps]
    lines = [head(ctx, f"{len(ps)} of {clean(result.get('total'), 8)} processes", clean(result.get("method"), 12))]
    lines.append(table(rows, ["PID", "PPID", "USER", "CPU%", "MEM", "STARTED", "HIVE", "COMMAND"]))
    lines += masked_line(result)
    return join(lines)


def proc_kill(result: dict, args: dict, ctx: RenderCtx) -> str:
    """proc_kill."""
    k = ", ".join(str(x) for x in result.get("killed") or [])
    a = ", ".join(str(x) for x in result.get("still_alive") or [])
    lines = [head(ctx, f"killed {k or 'nothing'}", f"still alive: {a}" if a else "", clean(result.get("method"), 20))]
    return join(lines)


def _svc_row(s: dict) -> List[Any]:
    return [s.get("name"), s.get("state"), s.get("health"), s.get("pid") or "-", human_duration(s.get("uptime_s"))
            if s.get("uptime_s") is not None else "-", s.get("restarts"), s.get("port") or "-",
            s.get("endpoint") or "-"]


def service_list(result: dict, args: dict, ctx: RenderCtx) -> str:
    """service_list: table + Next for crashlooping services."""
    svcs = [s for s in result.get("services") or [] if isinstance(s, dict)]
    lines = [head(ctx, f"{len(svcs)} service" + ("" if len(svcs) == 1 else "s"))]
    if svcs:
        lines.append(table([_svc_row(s) for s in svcs],
                           ["NAME", "STATE", "HEALTH", "PID", "UPTIME", "RESTARTS", "PORT", "ENDPOINT"]))
    for s in svcs:
        if s.get("state") in ("backoff", "failed"):
            nxt = next_call(ctx, "service_logs", name=s.get("name"))
            if nxt:
                lines.append(nxt + (" to see why it keeps restarting" if s.get("state") == "backoff"
                                    else " to see why it failed"))
    lines += masked_line(result)
    return join(lines)


def _gateway_hint(result: dict, ctx: RenderCtx) -> List[str]:
    ep = result.get("endpoint") or ((result.get("definition") or {}).get("endpoint")
                                    if isinstance(result.get("definition"), dict) else None)
    if result.get("health") == "healthy" and isinstance(ep, str) and ep and \
            isinstance(result.get("name"), str) and SERVICE_RE.match(result["name"]):
        node = ctx.node if ctx.node and NODE_RE.match(ctx.node) else "<node>"
        return [f"The user can use it from chat apps: hive gateway url {node} {result['name']} (guide('gateway'))"]
    return []


def _status_lines(result: dict, ctx: RenderCtx) -> List[str]:
    lines: List[str] = []
    if result.get("last_error"):
        lines.append("last error: " + clean(result["last_error"], 1000))
    res = result.get("resources") if isinstance(result.get("resources"), dict) else {}
    if res:
        parts = [f"RAM {human_bytes(res.get('rss_total'))}" if res.get("rss_total") is not None else "",
                 f"CPU {clean(res.get('cpu_pct'), 6)}%" if res.get("cpu_pct") is not None else "",
                 f"GPU {clean(res.get('gpu_memory_mb'), 8)} MiB" if res.get("gpu_memory_mb") is not None else ""]
        parts = [p for p in parts if p]
        if parts:
            lines.append("resources: " + ", ".join(parts))
    c = result.get("container")
    if isinstance(c, dict) and c.get("name"):
        lines.append("container: " + _kv(c, ("name", "state", "image")))
    if result.get("next_restart_at"):
        lines.append("next restart " + rel_time(result["next_restart_at"]))
    return lines


def _service_head(result: dict, ctx: RenderCtx, verb: str) -> str:
    return head(ctx, f"service {clean(result.get('name'), 64)} {verb}".rstrip(), clean(result.get("state"), 12),
                clean(result.get("health"), 12), f"pid {clean(result.get('pid'), 10)}" if result.get("pid") else "",
                f"up {human_duration(result.get('uptime_s'))}" if result.get("uptime_s") else "",
                f"{clean(result.get('restarts'), 6)} restarts" if result.get("restarts") else "")


def _service_tail_and_next(result: dict, ctx: RenderCtx) -> List[str]:
    lines: List[str] = []
    name = result.get("name")
    state = result.get("state")
    tail = result.get("tail")
    if state in ("failed", "backoff", "unhealthy", "stopped") and tail:
        lines.append(fence("log tail", ctx, "\n".join(_s(tail).splitlines()[-40:]), "last 40 lines"))
        lines.append(next_call(ctx, "service_logs", name=name))
    elif state in ("starting",) or (state == "running" and result.get("health") in ("starting", "unknown")):
        lines.append(next_call(ctx, "service_status", name=name, wait=_wait(ctx)))
    elif tail and state not in ("running",):
        lines.append(fence("log tail", ctx, tail))
    return lines


def _definition_lines(d: Any) -> List[str]:
    if not isinstance(d, dict):
        return []
    out: List[str] = []
    argv = d.get("argv")
    if isinstance(argv, list) and argv:
        out.append("argv: " + redact(" ".join(_shq(str(a)) for a in argv)))
    elif d.get("command"):
        out.append("command: " + redact(clean(d["command"], 2000)))
    env = d.get("env")
    if isinstance(env, dict) and env:
        out.append("env: " + ", ".join(f"{clean(k, 64)}=***" for k in env))
    elif isinstance(env, list) and env:
        out.append("env: " + ", ".join(f"{clean(str(k).split('=', 1)[0], 64)}=***" for k in env))
    sec = d.get("secret_env")
    if isinstance(sec, list) and sec:
        out.append("secret_env: " + ", ".join(f"«secret:{clean(n, 64)}»" for n in sec))
    for k in ("cwd", "port", "endpoint", "health", "health_url", "start_period", "restart", "autostart", "container"):
        if d.get(k) not in (None, "", []):
            out.append(f"{k}: {clean(d[k], 300)}")
    return out


def _shq(a: str) -> str:
    a = clean(a, 500)
    if a and not re.search(r"[\s'\"\\$`]", a):
        return a
    return "'" + a.replace("'", "'\\''") + "'"


def service_define(result: dict, args: dict, ctx: RenderCtx) -> str:
    """service_define: created/updated, changed fields, effective definition, warnings."""
    verb = "created" if result.get("created") else "updated"
    if result.get("restarted"):
        verb += " and restarted"
    lines = [_service_head(result, ctx, verb)]
    lines += extras(result)
    ch = result.get("changed")
    if isinstance(ch, list) and ch and not result.get("created"):
        lines.append("changed: " + clean(", ".join(str(c) for c in ch), 400))
    lines += _definition_lines(result.get("definition"))
    lines += _status_lines(result, ctx)
    lines += _service_tail_and_next(result, ctx)
    if result.get("state") == "stopped" and not result.get("tail"):
        lines.append(next_call(ctx, "service_control", op="start", name=result.get("name"), wait=_wait(ctx)))
    lines += _gateway_hint(result, ctx)
    lines += masked_line(result)
    return join(lines)


def service_control(result: dict, args: dict, ctx: RenderCtx) -> str:
    """service_control: start/stop/restart/remove."""
    op = args.get("op")
    if op == "remove":
        if result.get("pending"):
            return head(ctx, f"service {clean(result.get('removed'), 64)} is being removed",
                        f"state {clean(result.get('state'), 12)}", "the definition is deleted when it has stopped")
        return head(ctx, f"service {clean(result.get('removed'), 64)} removed")
    verb = {"start": "start", "stop": "stop", "restart": "restart"}.get(op or "", "")
    lines = [_service_head(result, ctx, f"({verb})" if verb else "")]
    lines += extras(result)
    lines += _status_lines(result, ctx)
    lines += _service_tail_and_next(result, ctx)
    lines += _gateway_hint(result, ctx)
    lines += masked_line(result)
    return join(lines)


def service_status(result: dict, args: dict, ctx: RenderCtx) -> str:
    """service_status: state, health, resources, tail."""
    lines = [_service_head(result, ctx, "")]
    if result.get("defined_by"):
        lines.append("defined by " + clean(result["defined_by"], 80))
    hd = result.get("health_detail") if isinstance(result.get("health_detail"), dict) else {}
    if hd.get("last_error"):
        lines.append("health check: " + clean(hd["last_error"], 300))
    lines += _status_lines(result, ctx)
    req = ctx.meta.get("requested_wait")
    w = ctx.meta.get("used_wait")
    if req and result.get("state") in ("starting",) or (req and result.get("health") == "starting"):
        if w is not None and float(req) > float(w):
            lines.append(f"waited {int(float(w))}s (client time limit); still {clean(result.get('state'), 12)}")
        else:
            lines.append(f"waited {int(float(req))}s; still {clean(result.get('state'), 12)}")
    lines += _service_tail_and_next(result, ctx)
    if result.get("tail") and result.get("state") == "running" and result.get("health") in ("healthy", "none"):
        lines.append(fence("log tail", ctx, result["tail"]))
    lines += _gateway_hint(result, ctx)
    lines += masked_line(result)
    return join(lines)


def service_logs(result: dict, args: dict, ctx: RenderCtx) -> str:
    """service_logs: status + log fence + paging."""
    st = result.get("status") if isinstance(result.get("status"), dict) else {}
    lines = [head(ctx, f"service {clean(args.get('name'), 64)} log", clean(st.get("state"), 12),
                  clean(st.get("health"), 12), f"{clean(st.get('restarts'), 6)} restarts" if st.get("restarts") else "")]
    r = dict(result)
    r.setdefault("state", st.get("state"))
    r["state"] = "running"  # a service log never ends; keep offering the follow-up
    lines += _log_lines(r, ctx, "log", "name", args.get("name"), "service_logs")
    lines += masked_line(result)
    return join(lines)


def model_chat(result: dict, args: dict, ctx: RenderCtx) -> str:
    """model_chat: reply fence + usage/speed."""
    u = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    usage = f"{clean(u.get('prompt_tokens'), 8)}+{clean(u.get('completion_tokens'), 8)} tokens" if u else ""
    tps = result.get("tokens_per_s")
    lines = [head(ctx, clean(result.get("model"), 120) or "reply", human_duration(result.get("latency_s")),
                  f"{float(tps):.1f} tok/s" if isinstance(tps, (int, float)) and not isinstance(tps, bool) else "",
                  usage, f"finish {clean(result.get('finish_reason'), 20)}" if result.get("finish_reason") else "")]
    if result.get("reasoning"):
        lines.append(fence("reasoning", ctx, result["reasoning"]))
    lines.append(fence("reply", ctx, result.get("content"), clean(result.get("base_url"), 200)))
    lines += masked_line(result)
    return join(lines)


def model_pull(result: dict, args: dict, ctx: RenderCtx) -> str:
    """model_pull: dry-run plan or started job."""
    repo = clean(result.get("repo") or args.get("repo"), 200)
    commit = clean(_s(result.get("commit"))[:7], 10)
    if result.get("job_id"):
        lines = [head(ctx, f"pulling {repo} @ {commit} → {clean(result.get('path'), 300)} as job "
                           f"{clean(result.get('job_id'), 48)}")]
        lines.append(f"{clean(result.get('file_count'), 8)} files · {human_bytes(result.get('to_download_bytes'))} "
                     f"to fetch")
        lines += extras(result)
        lines.append(_job_next(ctx, result.get("job_id")))
        return join(lines, masked_line(result))
    fits = "fits" if result.get("fits") else "does NOT fit"
    lines = [head(ctx, f"{repo} @ {commit}", f"{clean(result.get('file_count'), 8)} files",
                  f"{human_bytes(result.get('to_download_bytes'))} to download",
                  f"{human_bytes(result.get('disk_free'))} free → {fits}")]
    files = [f for f in result.get("files") or [] if isinstance(f, dict)]
    rows = [[f.get("path"), human_bytes(f.get("size")), "done" if f.get("done") else ""] for f in files]
    if rows:
        lines.append(table(rows, ["FILE", "SIZE", ""], total=result.get("file_count")
                           if isinstance(result.get("file_count"), int) else len(rows),
                           more_hint="use include/exclude to narrow"))
    tail = f"{clean(result.get('source'), 20)} via {clean(result.get('endpoint'), 200)}"
    sk = result.get("skipped_pickle") or []
    if sk:
        tail += f" · skipped {len(sk)} pickle files (safetensors present)"
    lines.append(tail)
    if result.get("skipped_invalid"):
        lines.append("skipped invalid names: " + clean(", ".join(str(x) for x in result["skipped_invalid"][:10]), 400))
    if result.get("note"):
        lines.append("note: " + clean(result["note"], 400))
    lines.append(f"target: {clean(result.get('path'), 300)}")
    lines += masked_line(result)
    return join(lines)


def http_request(result: dict, args: dict, ctx: RenderCtx) -> str:
    """http_request: status line + response fence."""
    lines = [head(ctx, f"{clean(result.get('status'), 4)} {clean(result.get('reason'), 60)}",
                  human_bytes(result.get("bytes")), human_duration(result.get("elapsed_s")),
                  clean(result.get("route"), 8))]
    if result.get("url") and result.get("url") != args.get("url"):
        lines.append("final url: " + clean(result["url"], 400))
    hd = result.get("headers")
    if isinstance(hd, dict):
        ct = hd.get("content-type") or hd.get("Content-Type")
        if ct:
            lines.append("content-type: " + clean(ct, 120))
    meta = "truncated" if result.get("truncated") else ""
    if result.get("body_base64") is not None:
        lines.append(f"(binary body, {human_bytes(result.get('bytes'))}, base64 in the data)")
    elif result.get("body"):
        lines.append(fence("response", ctx, result.get("body"), meta))
    lines += masked_line(result)
    return join(lines)


def net_download(result: dict, args: dict, ctx: RenderCtx) -> str:
    """net_download: started job."""
    lines = [head(ctx, f"downloading to {clean(result.get('path'), 300)} as job {clean(result.get('job_id'), 48)}")]
    lines.append(_job_next(ctx, result.get("job_id")))
    return join(lines)


def port_check(result: dict, args: dict, ctx: RenderCtx) -> str:
    """port_check: ports or listeners."""
    if isinstance(result.get("ports"), list):
        ps = [p for p in result["ports"] if isinstance(p, dict)]
        rows = [[p.get("port"), "open" if p.get("open") else "closed", p.get("pid") or "-",
                 p.get("process") or "-"] for p in ps]
        lines = [head(ctx, f"{sum(1 for p in ps if p.get('open'))} of {len(ps)} ports open",
                      clean(result.get("method"), 12)), table(rows, ["PORT", "STATE", "PID", "PROCESS"])]
    else:
        ls = [p for p in result.get("listeners") or [] if isinstance(p, dict)]
        rows = [[p.get("proto"), p.get("address"), p.get("port"), p.get("pid") or "-", p.get("process") or "-"]
                for p in ls]
        lines = [head(ctx, f"{len(ls)} listening ports", clean(result.get("method"), 12)),
                 table(rows, ["PROTO", "ADDRESS", "PORT", "PID", "PROCESS"])]
    return join(lines)


def guide(result: dict, args: dict, ctx: RenderCtx) -> str:
    """guide: the topic text as is (Hive's own text)."""
    return _s(result.get("text"))


def model_list(result: dict, args: dict, ctx: RenderCtx) -> str:
    """model_list: endpoints (+ models of one endpoint)."""
    eps = [e for e in result.get("endpoints") or [] if isinstance(e, dict)]
    rows = [[e.get("name") or "-", e.get("source"), e.get("api"), e.get("server"), e.get("base_url"),
             {True: "yes", False: "no"}.get(e.get("healthy"), "-"),
             ", ".join(str(m) for m in (e.get("models") or [])[:5]) or "-"] for e in eps]
    lines = [head(ctx, f"{len(eps)} model endpoint" + ("" if len(eps) == 1 else "s"))]
    if rows:
        lines.append(table(rows, ["NAME", "SOURCE", "API", "SERVER", "BASE_URL", "HEALTHY", "MODELS"]))
    if isinstance(result.get("models"), list):
        mrows = [[m.get("id"), m.get("owned_by") or "-"] for m in result["models"] if isinstance(m, dict)]
        lines.append(table(mrows, ["MODEL", "OWNED_BY"]))
    return join(lines)


def list_actions(result: dict, args: dict, ctx: RenderCtx) -> str:
    """list_actions: names, risk, approval, nodes."""
    acts = [a for a in result.get("actions") or [] if isinstance(a, dict)]
    rows = [[a.get("name"), a.get("risk"), "yes" if a.get("approval") else "-",
             ",".join(str(n) for n in a.get("nodes") or []) if "nodes" in a else "-",
             _s(a.get("title"))] for a in acts]
    lines = [head(ctx, f"{len(acts)} actions"), table(rows, ["ACTION", "RISK", "APPROVAL", "NODES", "TITLE"],
                                                     total=len(rows))]
    lines.append("Call any of them with call_action(action=…, params_json='{…}'); schemas: tools export or the "
                 "catalog")
    return join(lines)


__all__ = ["RenderCtx", "generic", "error", "finalize", "head", "fence", "table", "next_call", "idle_marker",
           "human_bytes", "human_duration", "rel_time", "local_time", "clean", "redact"]
