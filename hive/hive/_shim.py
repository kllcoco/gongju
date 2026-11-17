"""Process wrapper ``hive _shim --spec PATH``: log pump + rotation, exit.json, stop file, timeouts,
stop_argv/post_exit_argv (design §10.2, WP2).

Eager-import rule (§10.2): every hive.* module the shim can use is imported here at module top; no function-level
``hive.*`` imports. Imports only stdlib, hive.util, hive.errors, hive.proctree, hive.textlog and hive.winapi.
The shim alone writes ``status.json``, ``ready.json`` and ``exit.json`` (all with ``"v": 1``) in its run dir.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import errors, proctree, textlog, util, winapi  # noqa: F401  (eager imports, §10.2)

POLL = 0.5
DEFAULT_FILES = {"status": "status.json", "exit": "exit.json", "stop": "stop", "ready": "ready.json"}
POST_EXIT_TIMEOUT = 30.0


def _signame(signum: int) -> Optional[str]:
    try:
        return signal.Signals(signum).name
    except (ValueError, AttributeError):
        return None


class Shim:
    """One wrapped child process (§10.2 behaviour steps 1–7)."""

    def __init__(self, spec: Dict[str, Any], run_dir: Path):
        self.spec = spec
        self.run_dir = run_dir
        self.files = dict(DEFAULT_FILES)
        self.files.update(spec.get("files") or {})
        self.merge = bool(spec.get("merge_stderr", True))
        self.timeout = float(spec.get("timeout") or 0)
        self.timeout_grace = float(spec.get("timeout_grace") if spec.get("timeout_grace") is not None else 10)
        self.stop_argv: Optional[List[str]] = spec.get("stop_argv") or None
        self.post_exit_argv: Optional[List[str]] = spec.get("post_exit_argv") or None
        self.env: Optional[Dict[str, str]] = spec.get("env") or None
        self.cwd: Optional[str] = spec.get("cwd") or None
        self.child: Optional[subprocess.Popen] = None
        self.child_ctime: Optional[float] = None
        self.job: Optional[Any] = None
        self.writers: Dict[str, textlog.RotatingWriter] = {}
        self.pumps: List[threading.Thread] = []
        self.reason = "exit"
        self.sig_stop = threading.Event()
        self.started_mono = time.monotonic()
        self.status: Dict[str, Any] = {"v": 1, "shim_pid": os.getpid(),
                                       "shim_ctime": util.process_create_time(os.getpid()), "state": "starting"}
        self._snapshot: Dict[int, Optional[float]] = {}
        self._caffeinate: Optional[subprocess.Popen] = None

    # ----------------------------------------------------------------------------------------------- files
    def _path(self, key: str) -> Path:
        return self.run_dir / self.files[key]

    def _write_status(self, **kw: Any) -> None:
        self.status.update(kw)
        try:
            util.write_json_atomic(self._path("status"), self.status, private=True)
        except OSError:
            pass

    def _log_names(self) -> Dict[str, str]:
        logs = self.spec.get("logs") or {}
        if self.merge:
            return {"output": logs.get("output") or "output.log"}
        return {"stdout": logs.get("stdout") or "stdout.log", "stderr": logs.get("stderr") or "stderr.log"}

    def _main_writer(self) -> textlog.RotatingWriter:
        return self.writers.get("output") or self.writers.get("stdout") or next(iter(self.writers.values()))

    def _note(self, text: str) -> None:
        try:
            self._main_writer().write(("[hive] " + text + "\n").encode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            pass

    # ----------------------------------------------------------------------------------------------- run
    def run(self) -> int:
        """Spawn, pump, supervise stop/timeout, write exit.json; returns the shim's exit code."""
        self._write_status()
        if util.IS_WINDOWS and not winapi.has_console():  # pragma: no cover - Windows only
            winapi.alloc_hidden_console()
        max_bytes = int(self.spec.get("log_max_bytes") or (64 << 20))
        keep = int(self.spec.get("log_keep") if self.spec.get("log_keep") is not None else 2)
        for key, name in self._log_names().items():
            self.writers[key] = textlog.RotatingWriter(self.run_dir / name, max_bytes=max_bytes, keep=keep)
        self._install_signals()
        try:
            self._spawn()
        except Exception as e:  # noqa: BLE001 - spawn failure -> exit.json with the error, exit 127
            msg = f"{type(e).__name__}: {e}" if not isinstance(e, OSError) else (e.strerror or str(e))
            if isinstance(e, OSError) and getattr(e, "filename", None):
                msg = f"{msg}: {e.filename}"
            self._note(f"could not start: {msg}")
            self._remove_stdin()
            self._write_exit(None, None, error=msg)
            self._write_status(state="exited")
            self._close_writers()
            return 127
        self._after_spawn()
        rc = self._supervise()
        self._finish(rc)
        return 0

    def _stdin(self) -> Any:
        name = self.spec.get("stdin_file")
        if not name:
            return subprocess.DEVNULL
        return open(self.run_dir / name, "rb")

    def _remove_stdin(self) -> None:
        name = self.spec.get("stdin_file")
        if name:
            try:
                (self.run_dir / name).unlink()
            except OSError:
                pass

    def _spawn(self) -> None:
        argv = self.spec.get("cmdline") or self.spec.get("argv")
        if not argv:
            raise ValueError("spec has neither argv nor cmdline")
        stdin = self._stdin()
        kw: Dict[str, Any] = {"stdin": stdin, "stdout": subprocess.PIPE,
                              "stderr": subprocess.STDOUT if self.merge else subprocess.PIPE,
                              "cwd": self.cwd, "env": self.env, "close_fds": True}
        try:
            if util.IS_WINDOWS:  # pragma: no cover - Windows only
                self._spawn_windows(argv, kw)
            else:
                kw["start_new_session"] = True
                self.child = subprocess.Popen(list(argv), **kw)
        finally:
            if stdin is not subprocess.DEVNULL:
                try:
                    stdin.close()
                except OSError:
                    pass
        if not util.IS_WINDOWS:
            self._remove_stdin()   # POSIX: the child holds it open; unlink right after spawn

    def _spawn_windows(self, argv: Any, kw: Dict[str, Any]) -> None:  # pragma: no cover - Windows only
        try:
            self.job = winapi.JobObject()
        except Exception:  # noqa: BLE001
            self.job = None
        flags = winapi.CREATE_NEW_PROCESS_GROUP
        suspended = self.job is not None
        if suspended:
            flags |= winapi.CREATE_SUSPENDED
        kw["creationflags"] = flags
        self.child = subprocess.Popen(argv, **kw)
        handle = int(getattr(self.child, "_handle", 0) or 0)
        assigned = bool(self.job is not None and handle and self.job.assign(handle))
        if suspended and not winapi.resume_process(handle):
            # NtResumeProcess unavailable: kill the suspended child and spawn without CREATE_SUSPENDED (v1 behaviour)
            try:
                self.child.kill()
                self.child.wait(5)
            except Exception:  # noqa: BLE001
                pass
            kw["creationflags"] = winapi.CREATE_NEW_PROCESS_GROUP
            if kw.get("stdin") not in (None, subprocess.DEVNULL):
                kw["stdin"] = self._stdin()
            self.child = subprocess.Popen(argv, **kw)
            handle = int(getattr(self.child, "_handle", 0) or 0)
            assigned = bool(self.job is not None and handle and self.job.assign(handle))
        if not assigned and self.job is not None:
            self.job.close()
            self.job = None
        self.status["job_object"] = self.job is not None

    def _after_spawn(self) -> None:
        assert self.child is not None
        self.child_ctime = util.process_create_time(self.child.pid)
        started = util.iso_utc()
        util.write_json_atomic(self._path("ready"), {"v": 1, "child_pid": self.child.pid,
                                                     "child_ctime": self.child_ctime, "started_at": started},
                               private=True)
        self.status.setdefault("job_object", bool(util.IS_WINDOWS and self.job is not None))
        self._keep_awake()
        self._write_status(state="running", child_pid=self.child.pid, child_ctime=self.child_ctime)
        if self.merge:
            self._pump(self.child.stdout, self.writers["output"])
        else:
            self._pump(self.child.stdout, self.writers["stdout"])
            self._pump(self.child.stderr, self.writers["stderr"])

    def _pump(self, stream: Any, writer: textlog.RotatingWriter) -> None:
        def loop() -> None:
            fd = stream.fileno()
            while True:
                try:
                    b = os.read(fd, 65536)
                except OSError:
                    break
                if not b:
                    break
                try:
                    writer.write(b)
                except Exception:  # noqa: BLE001 - a full disk must not kill the child
                    pass
        t = threading.Thread(target=loop, name="shim-pump", daemon=True)
        t.start()
        self.pumps.append(t)

    def _keep_awake(self) -> None:
        if not self.spec.get("keep_awake"):
            return
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            self.status["keep_awake"] = bool(winapi.keep_awake(True))
        elif util.IS_MAC:  # pragma: no cover - macOS only
            try:
                self._caffeinate = subprocess.Popen(["/usr/bin/caffeinate", "-i", "-w", str(self.child.pid)],
                                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL, close_fds=True)
                self.status["keep_awake"] = True
            except OSError:
                self.status["keep_awake"] = "unsupported"
        else:
            self.status["keep_awake"] = "unsupported"

    def _install_signals(self) -> None:
        def on_signal(signum: int, frame: Any) -> None:
            self.sig_stop.set()
        for name in ("SIGTERM", "SIGINT", "SIGBREAK", "SIGHUP"):
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, on_signal)
            except (OSError, ValueError):
                pass

    # ----------------------------------------------------------------------------------------------- supervise
    def _read_stop(self) -> Optional[Dict[str, Any]]:
        p = self._path("stop")
        if not p.exists():
            return None
        try:
            d = json.loads(p.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            d = {}
        try:
            p.unlink()
        except OSError:
            pass
        return d if isinstance(d, dict) else {}

    def _wait_child(self, seconds: float) -> bool:
        """Wait up to ``seconds`` for the child; True when it exited."""
        assert self.child is not None
        try:
            self.child.wait(timeout=max(0.0, seconds))
            return True
        except subprocess.TimeoutExpired:
            return False

    def _supervise(self) -> int:
        assert self.child is not None
        while True:
            if self._wait_child(POLL):
                break
            stop = self._read_stop()
            if stop is not None:
                grace = float(stop.get("grace") if stop.get("grace") is not None else self.timeout_grace)
                self._stop_sequence(grace, bool(stop.get("force")), "stop")
                continue
            if self.sig_stop.is_set():
                self.sig_stop.clear()
                self._stop_sequence(self.timeout_grace, False, "stop")
                continue
            if self.timeout > 0 and time.monotonic() - self.started_mono >= self.timeout:
                self._note(f"timeout after {self.timeout:g} s; stopping")
                self._stop_sequence(self.timeout_grace, False, "timeout")
                continue
        return self.child.returncode

    def _escalated(self) -> bool:
        stop = self._read_stop()
        if stop is not None and stop.get("force"):
            return True
        return False

    def _wait_or_escalate(self, seconds: float) -> str:
        """'exited' | 'force' | 'elapsed'."""
        end = time.monotonic() + max(0.0, seconds)
        while True:
            left = end - time.monotonic()
            if self._wait_child(min(POLL, max(0.0, left))):
                return "exited"
            if self._escalated():
                return "force"
            if time.monotonic() >= end:
                return "elapsed"

    def _take_snapshot(self) -> None:
        assert self.child is not None
        snap = {self.child.pid: self.child_ctime}
        try:
            for d in proctree.descendants(self.child.pid):
                snap[d] = util.process_create_time(d)
        except Exception:  # noqa: BLE001
            pass
        self._snapshot.update(snap)

    def _stop_sequence(self, grace: float, force: bool, reason: str) -> None:
        assert self.child is not None
        if self.reason == "exit":
            self.reason = reason
        self._write_status(state="stopping")
        self._take_snapshot()
        t0 = time.monotonic()
        if not force:
            if self.stop_argv:
                self._run_logged(self.stop_argv, grace, "[hive stop] ", "stop_command")
                res = self._wait_or_escalate(max(0.0, grace - (time.monotonic() - t0)))
                if res == "exited":
                    return
                force = res == "force"
            if not force:
                self._graceful_signal()
                res = self._wait_or_escalate(5.0 if self.stop_argv else grace)
                if res == "exited":
                    return
        self._hard_kill()
        self._wait_child(10.0)

    def _graceful_signal(self) -> None:
        assert self.child is not None
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            winapi.send_ctrl_break(self.child.pid)
            return
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
        except OSError:
            try:
                self.child.terminate()
            except OSError:
                pass

    def _hard_kill(self) -> None:
        assert self.child is not None
        self._take_snapshot()
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            if self.job is not None and self.job.terminate(1):
                return
            proctree.kill_tree(self.child.pid, force=True, create_time=self.child_ctime)
            return
        try:
            os.killpg(self.child.pid, signal.SIGKILL)
        except OSError:
            pass
        for pid, ct in list(self._snapshot.items()):
            if pid != self.child.pid and util.pid_alive(pid, ct):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        try:
            self.child.kill()
        except OSError:
            pass

    def _run_logged(self, argv: List[str], limit: float, prefix: str, what: str) -> Optional[int]:
        """Run a helper argv (stop_argv/post_exit_argv), appending its output to the log; returns its exit code."""
        kw: Dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.STDOUT,
                              "cwd": self.cwd, "env": self.env, "close_fds": True}
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            kw["creationflags"] = winapi.CREATE_NO_WINDOW | winapi.CREATE_NEW_PROCESS_GROUP
        else:
            kw["start_new_session"] = True
        try:
            p = subprocess.Popen([str(a) for a in argv], **kw)
        except OSError as e:
            self._note(f"{what} failed to start: {e.strerror or e}")
            return None
        buf = bytearray()

        def reader() -> None:
            try:
                for line in iter(p.stdout.readline, b""):
                    if len(buf) < (1 << 20):
                        buf.extend(line)
            except (OSError, ValueError):
                pass
        t = threading.Thread(target=reader, daemon=True)
        t.start()
        code: Optional[int]
        try:
            code = p.wait(timeout=max(0.1, limit))
        except subprocess.TimeoutExpired:
            try:
                proctree.kill_tree(p.pid, force=True)
            except Exception:  # noqa: BLE001
                pass
            try:
                p.kill()
                p.wait(5)
            except Exception:  # noqa: BLE001
                pass
            code = None
        t.join(2)
        text = bytes(buf)
        writer = self._main_writer()
        for line in text.splitlines():
            writer.write(prefix.encode() + line + b"\n")
        self._note(f"{what} exit {code}" if code is not None else f"{what} timed out after {limit:g} s")
        return code

    # ----------------------------------------------------------------------------------------------- finish
    def _finish(self, rc: int) -> None:
        for t in self.pumps:
            t.join(2.0)
        if self.post_exit_argv:
            try:
                self._run_logged(self.post_exit_argv, POST_EXIT_TIMEOUT, "[hive post-exit] ", "post_exit")
            except Exception:  # noqa: BLE001 - errors ignored
                pass
        exit_code: Optional[int] = rc
        sig: Optional[str] = None
        if rc is not None and rc < 0:
            sig = _signame(-rc)
            exit_code = 128 + (-rc)
        self._write_exit(exit_code, sig)
        self._write_status(state="exited")
        if util.IS_WINDOWS:  # pragma: no cover - Windows only
            self._remove_stdin()
            if self.job is not None:
                self.job.close()
            if self.spec.get("keep_awake"):
                winapi.keep_awake(False)
        if self._caffeinate is not None:  # pragma: no cover - macOS only
            try:
                self._caffeinate.terminate()
            except OSError:
                pass
        self._close_writers()

    def _write_exit(self, exit_code: Optional[int], sig: Optional[str], error: Optional[str] = None) -> None:
        d: Dict[str, Any] = {"v": 1, "exit_code": exit_code, "signal": sig, "ended_at": util.iso_utc(),
                             "reason": self.reason}
        if error is not None:
            d["error"] = error
        util.write_json_atomic(self._path("exit"), d, private=True)

    def _close_writers(self) -> None:
        for w in self.writers.values():
            w.close()


def _parse_args(argv: List[str]) -> Optional[str]:
    for i, a in enumerate(argv):
        if a == "--spec" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--spec="):
            return a.split("=", 1)[1]
    return None


def main(argv: Optional[List[str]] = None) -> int:
    """Entry point of ``hive _shim --spec PATH``: read and delete the spec, run the wrapped process."""
    util.prune_cwd_path()
    if sys.stdout is None or sys.stderr is None:  # pythonw guard (§13.1)
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    path = _parse_args(list(sys.argv[2:] if argv is None else argv))
    if not path:
        return 64
    spec_path = Path(path).resolve()
    try:
        raw = spec_path.read_text(encoding="utf-8")
    except OSError:
        return 66
    finally:
        try:
            spec_path.unlink()   # the spec may hold secrets (env): deleted right after reading
        except OSError:
            pass
    try:
        spec = json.loads(raw)
    except ValueError:
        return 65
    if not isinstance(spec, dict):
        return 65
    try:
        return Shim(spec, spec_path.parent).run()
    except Exception:  # noqa: BLE001 - last resort: never leave the node without exit.json
        try:
            util.write_json_atomic(spec_path.parent / (spec.get("files") or {}).get("exit", "exit.json"),
                                   {"v": 1, "exit_code": None, "signal": None, "ended_at": util.iso_utc(),
                                    "reason": "exit", "error": "shim failure"}, private=True)
        except Exception:  # noqa: BLE001
            pass
        return 70


__all__ = ["main", "Shim"]
