"""JobManager + reaper: background jobs under the shim, adoption, logs, prune (design §10.4, WP2).

Layout: ``<data>/state/jobs/<job_id>/`` holds ``job.json`` (record, 0600, env KEYS only), the transient ``spec.json``
and ``stdin``, the shim's ``status.json``/``ready.json``/``exit.json``/``stop``, the logs, and for kinds download|pull
``progress.json``/``result.json`` written only by ``_download`` (WP3 convention).
Records are looked up only in the in-memory map; no path is built from a parameter before the lookup succeeds.
Lock rule (§16): one RLock + Condition; never held across subprocess, network or heavy file I/O.
"""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

from . import proctree, textlog, util
from .errors import HiveError, bad_request, cancelled, not_found, rate_limited

if TYPE_CHECKING:  # pragma: no cover
    from .actions import ActionContext
    from .node import NodeRuntime

log = logging.getLogger("hive.jobs")

FINAL = ("exited", "failed", "killed", "timeout", "lost")
RUNNING = ("starting", "running")
KINDS = ("job", "shell", "download", "pull")
REAP_TICK = 0.25
ADOPT_CHECK = 2.0
PROGRESS_EVERY = 1.0
READY_WAIT = 5.0
SHELL_RETENTION_S = 3600.0
KEEP_AWAKE_WARNING = ("keep_awake is not supported on this OS; the machine may sleep "
                      "(WSL: the Windows power plan decides)")


def _parse_ts(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    try:
        return util.parse_iso(s)
    except HiveError:
        return None


def _redact(text: str) -> str:
    try:
        from .audit import redact_text
        return redact_text(text)
    except Exception:  # noqa: BLE001
        return text


def display_command(argv_or_cmdline: Union[List[str], str], limit: int = 200) -> str:
    """Redacted one-line command text (job records show at most 200 chars)."""
    if isinstance(argv_or_cmdline, str):
        text = argv_or_cmdline
    else:
        text = " ".join(str(a) for a in argv_or_cmdline)
    text = _redact(text.replace("\n", " ").replace("\r", " "))
    return text[:limit]


def keep_awake_warnings(keep_awake: bool) -> List[str]:
    """The v7 keep_awake warning on Linux (incl. WSL) for job.start / service.define results, else []."""
    if keep_awake and not util.IS_WINDOWS and not util.IS_MAC:
        return [KEEP_AWAKE_WARNING]
    return []


@dataclass
class ExecPlan:
    """A policy-checked, fully built command (shell.run, job.start, service spawns)."""
    argv: Union[List[str], str]          # argv list, or the Windows cmd command-line string
    shell: str                           # resolved shell name, or "none" for argv
    cwd: Path
    env: Dict[str, str]                  # the full child environment (EffectivePolicy.child_env)
    files: Dict[str, bytes]              # run-dir files referenced as {run_dir}/… (PowerShell -File fallback)
    display: str                         # redacted command text for records


def _first_word(command: str) -> str:
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    return words[0] if words else ""


def is_wsl_exe(word: str) -> bool:
    """argv[0] / first command word naming wsl.exe (services cannot be supervised across it, §9.6)."""
    base = os.path.basename(str(word).replace("\\", "/")).lower()
    return base in ("wsl", "wsl.exe")


def prepare_command(policy: Any, p: Dict[str, Any], *, cwd: Optional[Path] = None) -> ExecPlan:
    """CommandGuard + argv building + child env for shell.run/job.start-style params (command|argv, shell, cwd,
    env, env_unset, secret_env, login, gpus). argv[0] is resolved with ``policy.node_path`` only (§5.4)."""
    command = p.get("command")
    argv = p.get("argv")
    if (command is None) == (not argv):
        raise bad_request("give exactly one of command or argv")
    wd = cwd if cwd is not None else policy.check_cwd(p.get("cwd"))
    overrides = util.parse_kv(p.get("env") or [])
    policy.check_env(overrides)
    files: Dict[str, bytes] = {}
    env_extra: Dict[str, str] = {}
    if argv:
        argv = [str(a) for a in argv]
        policy.check_command(list(argv), "none")
        built: Union[List[str], str] = proctree.build_argv(argv, policy.node_path, cwd=wd)
        shell = "none"
        display = display_command(argv)
    else:
        shell = proctree.resolve_shell(p.get("shell") or "auto")
        policy.check_shell(shell)
        policy.check_command(str(command), shell)
        plan = proctree.build_shell_argv(str(command), shell, login=bool(p.get("login")))
        built, shell, files, env_extra = plan.argv, plan.shell, dict(plan.files), dict(plan.env_extra)
        display = display_command(str(command))
    env = policy.child_env(overrides, unset=p.get("env_unset") or (), gpus=p.get("gpus"),
                           secret_env=p.get("secret_env") or ())
    for k, v in env_extra.items():
        if k not in overrides:
            env[k] = v
    return ExecPlan(argv=built, shell=shell, cwd=Path(wd), env=env, files=files, display=display)


def read_log(run_dir: Path, logs: Dict[str, str], finished: Any, secrets: Any = None, *, offset: Optional[int] = None,
             tail_lines: int = 100, max_bytes: int = 32768, wait: float = 0, stream: str = "output",
             raw: bool = False, cancel: Optional[threading.Event] = None, merged_hint: str = "job") -> dict:
    """Shared log reader of job.logs and service.logs (stream choice, wait for data, mask_edges, cleaning)."""
    if "output" in logs:
        if stream in ("stdout", "stderr"):
            raise bad_request(f"this {merged_hint} merged stderr; use stream='output'",
                              hint=f"this {merged_hint} merged stderr; use stream='output'")
        log_path = run_dir / logs["output"]
    else:
        log_path = run_dir / logs["stderr" if stream == "stderr" else "stdout"]
    if offset is not None and wait and wait > 0:
        end = time.monotonic() + float(wait)
        while time.monotonic() < end:
            if textlog.log_size(log_path) > offset or finished():
                break
            if cancel is not None and cancel.is_set():
                raise cancelled("cancelled by the caller")
            time.sleep(min(0.1, max(0.0, end - time.monotonic())))
    rr = textlog.read_range(log_path, offset, max_bytes, tail_lines if offset is None else None)
    data: bytes = rr["data"]
    _text, consumed = textlog.decode_chunk(data)
    data = data[:consumed]
    start = rr["offset"]
    nxt = start + consumed
    masked = False
    sec = secrets
    if sec is not None and data and getattr(sec, "has_values", False):
        n = max(0, int(getattr(sec, "max_value_bytes", 0) or 0) - 1)
        before = textlog.read_at(log_path, start - n, n if start >= n else start) if n else b""
        after = textlog.read_at(log_path, nxt, n) if n else b""
        try:
            data, masked = sec.mask_edges(data, before, after)
        except Exception:  # noqa: BLE001
            masked = False
    text = util.decode_text(data)
    if not raw:
        text = textlog.clean_terminal_text(text)
    done = bool(finished())
    out: Dict[str, Any] = {"text": text, "offset": start, "next_offset": nxt, "size": rr["size"],
                           "eof": done and nxt >= rr["size"], "skipped_bytes": rr["skipped_bytes"],
                           "truncated": nxt < rr["size"] if offset is not None else bool(rr["truncated"])}
    if masked:
        out["masked"] = True
    return out


class JobManager:
    """Background jobs under the shim (§10.4)."""

    def __init__(self, rt: "NodeRuntime"):
        self.rt = rt
        self.dir = Path(getattr(rt, "state_dir", None) or Path(rt.data_dir) / "state") / "jobs"
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._jobs: Dict[str, Dict[str, Any]] = {}
        self._popen: Dict[str, Any] = {}
        self._holds: Dict[str, str] = {}
        self._finalizing: set = set()
        self._orphans: List[Any] = []
        self._last_check: Dict[str, float] = {}
        self._last_progress: Dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Load records, adopt running jobs (exit.json -> finalize; live shim -> adopted; else lost), start reaper."""
        util.secure_mkdir(self.dir)
        for d in sorted(self.dir.iterdir()) if self.dir.exists() else []:
            if not d.is_dir():
                continue
            rec = util.read_json(d / "job.json", None) if (d / "job.json").exists() else None
            if not isinstance(rec, dict) or rec.get("job_id") != d.name:
                continue
            with self._lock:
                self._jobs[d.name] = rec
        for jid, rec in list(self._jobs.items()):
            if rec.get("state") in FINAL:
                continue
            rd = self.dir / jid
            if (rd / "exit.json").exists():
                self._finalize(jid)
            elif rec.get("shim_pid") and util.pid_alive(int(rec["shim_pid"]), rec.get("shim_ctime")):
                with self._lock:
                    rec["adopted"] = True
                    self._read_ready(jid, rec)
                self._rehold(jid, rec)
                self._save(rec)
            else:
                self._finalize(jid)
        self._stop.clear()
        self._thread = threading.Thread(target=self._reaper, name="job-reaper", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        """Stop the reaper; never kills jobs."""
        self._stop.set()
        with self._lock:
            # shims keep running after a node stop: hand their Popen objects to proctree's detached list, which
            # polls them (no zombie, no "still running" warning when this manager is dropped)
            proctree.adopt_popens(list(self._orphans) + [p for p in self._popen.values() if p is not None])
            self._orphans.clear()
        t = self._thread
        if t is not None:
            t.join(2)
        self._thread = None

    # ------------------------------------------------------------------------------------------- persistence
    def _run_dir(self, jid: str) -> Path:
        return self.dir / jid

    def _save(self, rec: Dict[str, Any]) -> None:
        with self._lock:
            snap = dict(rec)
        try:
            util.write_json_atomic(self._run_dir(snap["job_id"]) / "job.json", snap, private=True)
        except OSError as e:
            log.warning("cannot write job record %s: %s", snap.get("job_id"), e)

    def _rehold(self, jid: str, rec: Dict[str, Any]) -> None:
        paths = rec.get("held_paths") or []
        holds = getattr(self.rt, "holds", None)
        if not paths or holds is None:
            return
        try:
            from .approvals import file_fingerprint
            from .auth import system_identity
            eff = self.rt.effective(system_identity("node:autostart"))
            hid = holds.acquire({p: file_fingerprint(p) for p in paths}, policy=eff)
            with self._lock:
                self._holds[jid] = hid
        except Exception as e:  # noqa: BLE001
            log.warning("job %s: could not hold its approved files again: %s", jid, e)

    def _release_hold(self, jid: str) -> None:
        with self._lock:
            hid = self._holds.pop(jid, None)
        holds = getattr(self.rt, "holds", None)
        if hid and holds is not None:
            try:
                holds.release(hid)
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------------------------------- start
    def _limits(self, ctx: Any) -> Any:
        pol = getattr(ctx, "policy", None)
        lim = getattr(pol, "limits", None)
        if lim is None:
            lim = getattr(getattr(self.rt, "policy", None), "limits", None)
        return lim

    def start_job(self, ctx: "ActionContext", *, kind: str = "job", argv_or_cmdline: Any, shell: str, cwd: Path,
                  env: Dict[str, str], stdin: Optional[str], name: Optional[str], timeout: float,
                  timeout_grace: float = 10.0, merge_stderr: bool, log_max_bytes: int, log_keep: int = 1,
                  keep_awake: bool = False, tags: Any = (), command_display: str = "",
                  extra_files: Optional[Dict[str, bytes]] = None) -> dict:
        """Start a job under the shim and return its JobInfo (waits <= 5 s for ready.json/exit.json).

        ``extra_files`` are written 0600 into the run dir before the spawn; every argv element containing
        ``{run_dir}`` is substituted with the run dir path. A pending ``ctx.hold`` is taken over (released when the
        job ends) and ``ctx.hold`` is set to None (§13.2 step 8a hand-over).
        """
        if kind not in KINDS:
            raise bad_request(f"unknown job kind {kind!r}")
        lim = self._limits(ctx)
        max_running = int(getattr(lim, "jobs_max_running", 32) or 32)
        if self.running_count(None) >= max_running:
            raise rate_limited("too many running jobs", retry_after=5.0, reason="jobs_running",
                               hint="too many running jobs; stop some (job_list)")
        jid = util.new_id("j_")
        rd = self._run_dir(jid)
        util.secure_mkdir(rd, required=True)
        env = dict(env or {})
        if isinstance(argv_or_cmdline, str):
            argv: Optional[List[str]] = None
            cmdline: Optional[str] = argv_or_cmdline.replace("{run_dir}", str(rd))
            display_src: Union[List[str], str] = cmdline
        else:
            argv = [str(a).replace("{run_dir}", str(rd)) for a in argv_or_cmdline]
            cmdline = None
            display_src = argv
            selfcmd = util.self_command()
            if argv[:len(selfcmd)] == selfcmd:
                env.update(util.self_env())   # v5: helper targets (_download) import hive from a checkout too
        for fname, data in (extra_files or {}).items():
            if os.path.basename(fname) != fname or fname in ("", ".", ".."):
                raise bad_request(f"invalid extra file name {fname!r}")
            util.atomic_write_bytes(rd / fname, data, private=True)
        stdin_file = None
        if stdin is not None:
            util.atomic_write_bytes(rd / "stdin", stdin.encode("utf-8"), private=True)
            stdin_file = "stdin"
        logs = {"output": "output.log"} if merge_stderr else {"stdout": "stdout.log", "stderr": "stderr.log"}
        disp = command_display or display_command(display_src)
        now = util.iso_utc()
        ident = getattr(ctx, "identity", None)
        rec: Dict[str, Any] = {
            "v": 1, "job_id": jid, "name": (name or disp or kind)[:64], "kind": kind, "state": "starting",
            "exit_code": None, "signal": None, "pid": None, "child_ctime": None, "shim_pid": None,
            "shim_ctime": None, "adopted": False, "created_at": now, "started_at": None, "ended_at": None,
            "command": disp[:200], "cwd": str(cwd), "shell": shell,
            "owner": getattr(ident, "display_name", None) or getattr(ident, "name", None),
            "tags": [str(t) for t in (tags or ())], "env_keys": sorted(env), "merge_stderr": bool(merge_stderr),
            "logs": logs, "timeout": float(timeout or 0), "error": None, "result": None, "progress": None,
            "held_paths": [], "ephemeral": False, "reason": None}
        hold = getattr(ctx, "hold", None) if ctx is not None else None
        holds = getattr(self.rt, "holds", None)
        if hold and holds is not None:
            try:
                rec["held_paths"] = holds.paths(hold)
            except Exception:  # noqa: BLE001
                rec["held_paths"] = []
        spec = {"v": 1, "argv": argv, "cmdline": cmdline, "cwd": str(cwd), "env": env, "stdin_file": stdin_file,
                "merge_stderr": bool(merge_stderr), "logs": logs, "log_max_bytes": int(log_max_bytes),
                "log_keep": int(log_keep), "timeout": float(timeout or 0), "timeout_grace": float(timeout_grace),
                "keep_awake": bool(keep_awake), "stop_argv": None, "post_exit_argv": None,
                "files": {"status": "status.json", "exit": "exit.json", "stop": "stop", "ready": "ready.json"}}
        with self._lock:
            self._jobs[jid] = rec
            if hold and holds is not None:
                self._holds[jid] = hold
                ctx.hold = None
        self._save(rec)
        try:
            h = proctree.spawn_shim(spec, rd)
        except HiveError as e:
            with self._lock:
                rec.update(state="failed", error=e.message, ended_at=util.iso_utc())
                self._cond.notify_all()
            self._save(rec)
            self._release_hold(jid)
            raise
        with self._lock:
            rec.update(shim_pid=h.shim_pid, shim_ctime=h.shim_ctime)
            self._popen[jid] = h.popen
        self._save(rec)
        end = time.monotonic() + READY_WAIT
        while time.monotonic() < end:
            if (rd / "ready.json").exists() or (rd / "exit.json").exists():
                break
            if h.popen is not None and h.popen.poll() is not None:
                break
            time.sleep(0.01)
        self._poll_job(jid)
        return self.info(jid, tail_lines=0)

    # ------------------------------------------------------------------------------------------- reaper
    def _reaper(self) -> None:
        while not self._stop.wait(REAP_TICK):
            with self._lock:
                active = [j for j, r in self._jobs.items() if r.get("state") not in FINAL]
                self._orphans[:] = [p for p in self._orphans if p.poll() is None]
            proctree.reap_detached()
            for jid in active:
                try:
                    self._poll_job(jid)
                except Exception:  # noqa: BLE001
                    log.warning("job %s: reaper check failed", jid, exc_info=True)

    def _read_ready(self, jid: str, rec: Dict[str, Any]) -> None:
        ready = util.read_json(self._run_dir(jid) / "ready.json", None) \
            if (self._run_dir(jid) / "ready.json").exists() else None
        if isinstance(ready, dict) and ready.get("child_pid"):
            rec["pid"] = int(ready["child_pid"])
            rec["child_ctime"] = ready.get("child_ctime")
            rec["started_at"] = ready.get("started_at") or rec.get("started_at") or util.iso_utc()
            if rec.get("state") == "starting":
                rec["state"] = "running"

    def _poll_job(self, jid: str) -> None:
        with self._lock:
            rec = self._jobs.get(jid)
            if rec is None or rec.get("state") in FINAL:
                return
            popen = self._popen.get(jid)
            kind = rec.get("kind")
        rd = self._run_dir(jid)
        if rec.get("state") == "starting":
            ready = util.read_json(rd / "ready.json", None) if (rd / "ready.json").exists() else None
            if isinstance(ready, dict):
                with self._lock:
                    self._read_ready(jid, rec)
                    self._cond.notify_all()
                self._save(rec)
        if kind in ("download", "pull"):
            now = time.monotonic()
            if now - self._last_progress.get(jid, 0.0) >= PROGRESS_EVERY:
                self._last_progress[jid] = now
                self._read_progress(jid, rec)
        if popen is not None:
            if popen.poll() is not None:
                self._finalize(jid)
            return
        now = time.monotonic()
        if now - self._last_check.get(jid, 0.0) < ADOPT_CHECK:
            return
        self._last_check[jid] = now
        if (rd / "exit.json").exists():
            self._finalize(jid)
        elif not rec.get("shim_pid") or not util.pid_alive(int(rec["shim_pid"]), rec.get("shim_ctime")):
            self._finalize(jid)

    def _read_progress(self, jid: str, rec: Dict[str, Any]) -> None:
        p = self._run_dir(jid) / "progress.json"
        if not p.exists():
            return
        try:
            d = util.read_json(p, None)
        except HiveError:
            return
        if isinstance(d, dict):
            d = {k: v for k, v in d.items() if k != "v"}
            with self._lock:
                rec["progress"] = d

    def _finalize(self, jid: str) -> None:
        with self._lock:
            rec = self._jobs.get(jid)
            if rec is None or rec.get("state") in FINAL or jid in self._finalizing:
                return
            self._finalizing.add(jid)
        try:
            rd = self._run_dir(jid)
            ex = None
            if (rd / "exit.json").exists():
                try:
                    ex = util.read_json(rd / "exit.json", None)
                except HiveError:
                    ex = None
            upd: Dict[str, Any] = {}
            if isinstance(ex, dict):
                reason = ex.get("reason") or "exit"
                upd["exit_code"] = ex.get("exit_code")
                upd["signal"] = ex.get("signal")
                upd["ended_at"] = ex.get("ended_at") or util.iso_utc()
                upd["reason"] = reason
                if ex.get("error") and ex.get("exit_code") is None:
                    upd.update(state="failed", error=str(ex["error"])[:500])
                elif reason == "timeout":
                    upd["state"] = "timeout"
                elif reason == "stop":
                    upd["state"] = "killed"
                else:
                    upd["state"] = "exited"
            else:
                upd.update(state="lost", ended_at=util.iso_utc(),
                           error="the process wrapper vanished without writing exit.json")
                self._kill_orphan(jid, rec)
            if rec.get("pid") is None:
                with self._lock:
                    self._read_ready(jid, rec)
            if rec.get("kind") in ("download", "pull"):
                self._read_progress(jid, rec)
                res = util.read_json(rd / "result.json", None) if (rd / "result.json").exists() else None
                if isinstance(res, dict):
                    upd["result"] = {k: v for k, v in res.items() if k != "v"}
                elif upd.get("state") != "killed" and not upd.get("error"):
                    if upd.get("exit_code") not in (0, None) or upd.get("state") == "lost":
                        upd["error"] = textlog.last_line(self._main_log(jid, rec)) or "download failed"
            with self._lock:
                if rec.get("state") == "starting" and upd.get("state") != "failed" and rec.get("started_at") is None:
                    rec["started_at"] = rec.get("created_at")
                rec.update(upd)
                popen = self._popen.pop(jid, None)
                if popen is not None and popen.poll() is None:
                    self._orphans.append(popen)    # the shim is finishing after exit.json: reaped by the reaper
                self._cond.notify_all()
            self._save(rec)
            self._release_hold(jid)
        finally:
            with self._lock:
                self._finalizing.discard(jid)
                self._cond.notify_all()

    def _kill_orphan(self, jid: str, rec: Dict[str, Any]) -> None:
        ready = util.read_json(self._run_dir(jid) / "ready.json", None) \
            if (self._run_dir(jid) / "ready.json").exists() else None
        if isinstance(ready, dict) and ready.get("child_pid"):
            pid, ct = int(ready["child_pid"]), ready.get("child_ctime")
            if util.pid_alive(pid, ct):
                try:
                    proctree.kill_tree(pid, force=True, create_time=ct)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------------------------------- views
    def _rec(self, job_id: str) -> Dict[str, Any]:
        with self._lock:
            rec = self._jobs.get(job_id)
            if rec is None:
                recent = sorted(self._jobs.values(), key=lambda r: r.get("created_at") or "", reverse=True)[:5]
                ids = [r["job_id"] for r in recent]
        if rec is None:
            raise not_found(f"job '{job_id}' not found",
                            hint=("recent jobs: " + ", ".join(ids)) if ids else "no jobs on this node (job_list)")
        return rec

    def _main_log(self, jid: str, rec: Dict[str, Any]) -> Path:
        logs = rec.get("logs") or {"output": "output.log"}
        return self._run_dir(jid) / (logs.get("output") or logs.get("stdout") or "output.log")

    def _log_paths(self, jid: str, rec: Dict[str, Any]) -> List[Path]:
        logs = rec.get("logs") or {"output": "output.log"}
        return [self._run_dir(jid) / n for n in logs.values()]

    def _tail_text(self, jid: str, rec: Dict[str, Any], n: int) -> str:
        if n <= 0:
            return ""
        logs = rec.get("logs") or {"output": "output.log"}
        rd = self._run_dir(jid)
        if "output" in logs:
            return textlog.clean_terminal_text(util.decode_text(textlog.tail_lines(rd / logs["output"], n)))
        out = textlog.clean_terminal_text(util.decode_text(textlog.tail_lines(rd / logs["stdout"], n)))
        err = textlog.clean_terminal_text(util.decode_text(textlog.tail_lines(rd / logs["stderr"], n)))
        if err.strip():
            out = (out.rstrip("\n") + "\n" if out else "") + "[stderr]\n" + err
        return out

    def info(self, job_id: str, tail_lines: int = 20, *, with_tail: bool = True) -> dict:
        """JobInfo (§9.5); ``with_tail=False`` omits ``tail`` (job.stop)."""
        rec = self._rec(job_id)
        with self._lock:
            r = dict(rec)
        started = _parse_ts(r.get("started_at"))
        ended = _parse_ts(r.get("ended_at"))
        dur = None
        if started is not None:
            dur = round(max(0.0, (ended if ended is not None else time.time()) - started), 3)
        out_bytes = 0
        rot = 0
        for p in self._log_paths(job_id, r):
            out_bytes += textlog.log_size(p)
            rot += textlog.rotated_bytes(p)
        info: Dict[str, Any] = {
            "job_id": r["job_id"], "name": r.get("name"), "kind": r.get("kind"), "state": r.get("state"),
            "exit_code": r.get("exit_code"), "signal": r.get("signal"),
            "pid": r.get("pid") if r.get("pid") is not None else r.get("shim_pid"),
            "adopted": bool(r.get("adopted")), "created_at": r.get("created_at"), "started_at": r.get("started_at"),
            "ended_at": r.get("ended_at"), "duration_s": dur, "command": r.get("command"), "cwd": r.get("cwd"),
            "owner": r.get("owner"), "tags": list(r.get("tags") or []), "output_bytes": out_bytes,
            "rotated_bytes": rot, "progress": r.get("progress"), "result": r.get("result"), "error": r.get("error")}
        if with_tail:
            info["tail"] = self._tail_text(job_id, r, int(tail_lines))
        return info

    def get(self, job_id: str, tail_lines: int = 20) -> dict:
        """JobInfo with the last ``tail_lines`` cleaned log lines; not_found with a hint listing recent ids."""
        self._rec(job_id)
        self._poll_job(job_id)
        return self.info(job_id, tail_lines)

    def list(self, state: str = "all", limit: int = 20, include_shell: bool = False) -> dict:
        """job.list rows, newest first."""
        with self._lock:
            recs = [dict(r) for r in self._jobs.values()]
        rows = []
        for r in sorted(recs, key=lambda x: x.get("created_at") or "", reverse=True):
            if r.get("kind") == "shell" and not include_shell:
                continue
            st = r.get("state")
            if state == "running" and st not in RUNNING:
                continue
            if state == "finished" and st in RUNNING:
                continue
            started = _parse_ts(r.get("started_at"))
            ended = _parse_ts(r.get("ended_at"))
            dur = round(max(0.0, (ended or time.time()) - started), 3) if started is not None else None
            rows.append({"job_id": r["job_id"], "name": r.get("name"), "kind": r.get("kind"), "state": st,
                         "exit_code": r.get("exit_code"), "started_at": r.get("started_at"), "duration_s": dur,
                         "command": (r.get("command") or "")[:80]})
        return {"jobs": rows[:max(1, int(limit))], "total": len(rows)}

    def is_final(self, job_id: str) -> bool:
        """The job reached a final state."""
        return self._rec(job_id).get("state") in FINAL

    def wait_final(self, job_id: str, timeout: float, cancel: Optional[threading.Event] = None) -> bool:
        """Block (cheaply) until the job is final, ``timeout`` elapses or ``cancel`` is set; True when final."""
        self._rec(job_id)
        end = time.monotonic() + max(0.0, float(timeout))
        while True:
            self._poll_job(job_id)
            with self._lock:
                if self._jobs[job_id].get("state") in FINAL:
                    return True
                left = end - time.monotonic()
                if left <= 0 or (cancel is not None and cancel.is_set()):
                    return False
                self._cond.wait(min(0.05, left))

    def record(self, job_id: str) -> Dict[str, Any]:
        """A copy of the job record (internal: handlers and tests)."""
        rec = self._rec(job_id)
        with self._lock:
            return dict(rec)

    def run_dir(self, job_id: str) -> Path:
        """The run dir of a known job."""
        self._rec(job_id)
        return self._run_dir(job_id)

    def wait(self, job_id: str, timeout: float, cancel: threading.Event) -> dict:
        """Block until the job ends, ``timeout`` elapses or ``cancel`` is set; returns JobInfo (tail 20)."""
        self._rec(job_id)
        end = time.monotonic() + max(0.0, float(timeout))
        while True:
            self._poll_job(job_id)
            with self._lock:
                if self._jobs[job_id].get("state") in FINAL:
                    break
                left = end - time.monotonic()
                if left <= 0 or (cancel is not None and cancel.is_set()):
                    break
                self._cond.wait(min(0.05, left))
        return self.info(job_id)

    def logs(self, job_id: str, **job_logs_params: Any) -> dict:
        """job.logs: ``offset``/``tail_lines``/``max_bytes``/``wait``/``stream``/``raw`` (+ ``cancel`` Event).

        Range reads mask secret parts cut by their edges (``SecretStore.mask_edges``) and set ``masked``."""
        rec = self._rec(job_id)
        r = read_log(self._run_dir(job_id), rec.get("logs") or {"output": "output.log"},
                     lambda: self._rec(job_id).get("state") in FINAL, getattr(self.rt, "secrets", None),
                     **job_logs_params)
        with self._lock:
            r["state"] = rec.get("state")
        out = {"job_id": job_id}
        out.update(r)
        return out

    # ------------------------------------------------------------------------------------------- control
    def stop(self, job_id: str, force: bool = False, grace: float = 10.0) -> dict:
        """job.stop: stop file for the shim (graceful, then hard after ``grace``); JobInfo without tail."""
        rec = self._rec(job_id)
        self._poll_job(job_id)
        if rec.get("state") in FINAL:
            return self.info(job_id, with_tail=False)
        rd = self._run_dir(job_id)
        shim_alive = bool(rec.get("shim_pid")) and util.pid_alive(int(rec["shim_pid"]), rec.get("shim_ctime"))
        if shim_alive:
            proctree.request_stop(rd, grace, force)
        elif rec.get("pid") and util.pid_alive(int(rec["pid"]), rec.get("child_ctime")):
            proctree.kill_tree(int(rec["pid"]), grace=grace, force=force, create_time=rec.get("child_ctime"))
        end = time.monotonic() + (0 if force else float(grace)) + 8.0
        while time.monotonic() < end:
            self._poll_job(job_id)
            with self._lock:
                if rec.get("state") in FINAL:
                    break
                self._cond.wait(0.05)
            if (rd / "exit.json").exists():
                self._finalize(job_id)
        if rec.get("state") not in FINAL and shim_alive:
            proctree.kill_tree(int(rec["shim_pid"]), force=True, create_time=rec.get("shim_ctime"))
            if rec.get("pid"):
                proctree.kill_tree(int(rec["pid"]), force=True, create_time=rec.get("child_ctime"))
            time.sleep(0.2)
            self._finalize(job_id)
        return self.info(job_id, with_tail=False)

    def request_stop(self, job_id: str, grace: float, force: bool = False) -> None:
        """Write the stop file without waiting (shell.run cancel)."""
        rec = self._rec(job_id)
        if rec.get("state") not in FINAL:
            proctree.request_stop(self._run_dir(job_id), grace, force)

    def promote(self, job_id: str, *, name: str) -> None:
        """shell.run yield -> kind "job" (retention, job_list, reaper)."""
        rec = self._rec(job_id)
        with self._lock:
            rec["kind"] = "job"
            rec["name"] = (name or rec.get("name") or "")[:64]
            rec["ephemeral"] = False
        self._save(rec)

    def mark_ephemeral(self, job_id: str) -> None:
        """Keep a finished shell job's full log for 1 h (truncated shell.run output)."""
        rec = self._rec(job_id)
        with self._lock:
            rec["ephemeral"] = True
        self._save(rec)

    def remove(self, job_id: Optional[str] = None, older_than_hours: Optional[float] = None) -> dict:
        """Remove finished jobs (one id, or all finished older than ``older_than_hours``); internal (prune, tests)."""
        now = time.time()
        with self._lock:
            if job_id is not None:
                rec = self._jobs.get(job_id)
                if rec is None:
                    raise not_found(f"job '{job_id}' not found")
                cands = [job_id]
            else:
                cands = list(self._jobs)
            victims = []
            for jid in cands:
                r = self._jobs[jid]
                if r.get("state") not in FINAL:
                    if job_id is not None:
                        raise bad_request(f"job '{jid}' is still running", hint="stop it first (job_stop)")
                    continue
                if older_than_hours is not None:
                    ended = _parse_ts(r.get("ended_at")) or _parse_ts(r.get("created_at")) or now
                    if now - ended < older_than_hours * 3600:
                        continue
                victims.append(jid)
            for jid in victims:
                self._jobs.pop(jid, None)
                self._popen.pop(jid, None)
        for jid in victims:
            shutil.rmtree(self._run_dir(jid), ignore_errors=True)
            self._release_hold(jid)
        return {"removed": victims}

    def set_progress(self, job_id: str, progress: dict) -> None:
        """Store the progress of a download/pull job (normally read from progress.json by the reaper)."""
        rec = self._rec(job_id)
        with self._lock:
            rec["progress"] = dict(progress or {})

    def running_count(self, kind: Optional[str]) -> int:
        """Running jobs of ``kind`` (all kinds when None); model.pull uses it for its concurrency cap."""
        with self._lock:
            return sum(1 for r in self._jobs.values()
                       if r.get("state") in RUNNING and (kind is None or r.get("kind") == kind))

    def managed_pids(self) -> Dict[int, str]:
        """shim/child pid -> "job:<id>" for proc.list and the kill guard."""
        out: Dict[int, str] = {}
        with self._lock:
            for jid, r in self._jobs.items():
                if r.get("state") in FINAL:
                    continue
                for k in ("shim_pid", "pid"):
                    if r.get(k):
                        out[int(r[k])] = f"job:{jid}"
        return out

    def _retention(self) -> Dict[str, Any]:
        cfg = getattr(getattr(getattr(self.rt, "config", None), "policy", None), "jobs", None) or {}
        return {"retention_days": float(cfg.get("retention_days", 7)), "max_records": int(cfg.get("max_records", 200))}

    def prune(self) -> None:
        """Remove finished jobs older than retention_days or beyond max_records, and shell jobs ended > 1 h ago."""
        ret = self._retention()
        now = time.time()
        with self._lock:
            recs = [dict(r) for r in self._jobs.values() if r.get("state") in FINAL]
        victims = set()
        for r in recs:
            ended = _parse_ts(r.get("ended_at")) or _parse_ts(r.get("created_at")) or now
            if r.get("kind") == "shell" and now - ended > SHELL_RETENTION_S:
                victims.add(r["job_id"])
            elif now - ended > ret["retention_days"] * 86400:
                victims.add(r["job_id"])
        keep = [r for r in sorted(recs, key=lambda x: x.get("ended_at") or "", reverse=True)
                if r["job_id"] not in victims]
        for r in keep[ret["max_records"]:]:
            victims.add(r["job_id"])
        for jid in victims:
            try:
                self.remove(jid)
            except HiveError:
                pass


__all__ = ["JobManager", "display_command", "read_log", "FINAL", "ExecPlan", "prepare_command", "is_wsl_exe",
           "keep_awake_warnings", "KEEP_AWAKE_WARNING"]
