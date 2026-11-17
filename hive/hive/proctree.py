"""Shells, argv building, shim/detached spawning (spawn modes), kill_tree, pid identity, descendants, trusted_path
(design §10.1, WP2).

Phase 0: ``set_trusted_path``/``trusted_path`` are REAL (module-level value + fail-safe default; WP2 keeps them) and
``pid_alive``/``process_create_time`` are re-exported from util unchanged (v5). ``default_shell``/``available_shells``
are neutral P0 stubs (``None``/``[]``, the node.info null fallback). ``spawn_shim``, ``kill_tree``, ``run_quick`` and
the other names are stubs until WP2 merges.
Module rule: imports only stdlib, ``hive.util``, ``hive.errors`` and ``hive.winapi`` at module level.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from . import util, winapi  # noqa: F401  (winapi: Windows spawn flags, WP2)
from .errors import unsupported

pid_alive = util.pid_alive                       # v5: defined in util (P0, §4.2) and re-exported here unchanged
process_create_time = util.process_create_time

# fixed tool locations of §12.2 (always trusted for internal probes)
FIXED_TOOL_DIRS_POSIX = ("/usr/lib/wsl/lib", "/usr/bin")
FIXED_TOOL_DIRS_WINDOWS = (r"C:\Windows\System32", r"C:\Program Files\NVIDIA Corporation\NVSMI")

_trusted_path: Optional[str] = None


def _na(what: str):
    return unsupported(f"proctree.{what} not available yet (WP2)")


def set_trusted_path(path: str) -> None:
    """Set the PATH used for internal probes (NodeRuntime.start passes Policy.trusted_path)."""
    global _trusted_path
    _trusted_path = path


def _is_under(child: str, parent: str) -> bool:
    try:
        c = os.path.normcase(os.path.realpath(child))
        p = os.path.normcase(os.path.realpath(parent))
    except OSError:
        return False
    if not p:
        return False
    return c == p or c.startswith(p.rstrip(os.sep) + os.sep)


def _is_root(p: str) -> bool:
    try:
        r = os.path.realpath(p)
    except OSError:
        r = p
    return os.path.dirname(r) == r


def _default_trusted_path() -> str:
    excluded = []
    try:
        excluded.append(str(Path.home()))
    except (RuntimeError, KeyError):
        pass
    try:
        excluded.append(os.getcwd())
    except OSError:
        pass
    excluded.append(tempfile.gettempdir())
    # a filesystem root as home/cwd/temp (cwd "/" in containers, "C:\\", HOME=/) would exclude every PATH entry;
    # such a base is skipped (it is not "a writable folder the agent chose", PHASE0_NOTES)
    excluded = [e for e in excluded if e and not _is_root(e)]
    out: List[str] = []
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or not os.path.isabs(d):
            continue
        if any(_is_under(d, e) for e in excluded):
            continue
        if d not in out:
            out.append(d)
    fixed = FIXED_TOOL_DIRS_WINDOWS if util.IS_WINDOWS else FIXED_TOOL_DIRS_POSIX
    for d in fixed:
        if d not in out:
            out.append(d)
    return os.pathsep.join(out)


def trusted_path() -> str:
    """The value set by set_trusted_path(), else the fail-safe default (PATH minus home/cwd/temp + fixed tool dirs)."""
    if _trusted_path is not None:
        return _trusted_path
    return _default_trusted_path()


def default_shell() -> str:
    """POSIX: bash if found else sh; Windows: pwsh if found else powershell (WP2; P0 stub: ``None``, the node.info
    null fallback of §9.2)."""
    return None  # type: ignore[return-value]


def available_shells() -> List[dict]:
    """``[{"name","path","version"|None}]`` (WP2; P0 stub: ``[]``)."""
    return []


@dataclass
class ShellPlan:
    """Result of build_shell_argv."""
    argv: Union[List[str], str]
    shell: str
    env_extra: Dict[str, str]


def build_shell_argv(command: str, shell: str = "auto", login: bool = False) -> ShellPlan:
    """Shell argv per §10.1 table (WP2)."""
    raise _na("build_shell_argv")


def build_argv(argv: List[str], node_path: str) -> List[str]:
    """Resolve argv[0] with the node PATH only (WP2)."""
    raise _na("build_argv")


@dataclass
class ShimHandle:
    """A started shim."""
    shim_pid: int
    shim_ctime: Optional[float]
    popen: Optional[subprocess.Popen]
    run_dir: Path


def spawn_shim(spec: dict, run_dir: Path) -> ShimHandle:
    """Write spec.json and start ``hive _shim`` detached (WP2)."""
    raise _na("spawn_shim")


def spawn_detached(argv: List[str], *, env: Dict[str, str], cwd: Optional[Path] = None) -> Tuple[int, Optional[float]]:
    """Detached spawn without a shim (WSL keepalive; WP2)."""
    raise _na("spawn_detached")


def request_stop(run_dir: Path, grace: float, force: bool = False) -> None:
    """Write ``<run_dir>/stop`` (WP2)."""
    raise _na("request_stop")


def kill_tree(pid: int, *, grace: float = 5.0, force: bool = False,
              create_time: Optional[float] = None) -> Dict[str, Any]:
    """``{"killed","still_alive","method"}`` (WP2)."""
    raise _na("kill_tree")


def descendants(pid: int) -> List[int]:
    """Descendant pids (WP2)."""
    raise _na("descendants")


def run_quick(argv: List[str], *, timeout: float, cwd: Any = None, env: Any = None,
              max_output: int = 1 << 20) -> Tuple[int, bytes, bytes]:
    """Direct subprocess for internal short probes only (WP2)."""
    raise _na("run_quick")


__all__ = ["pid_alive", "process_create_time", "set_trusted_path", "trusted_path", "default_shell",
           "available_shells", "ShellPlan", "build_shell_argv", "build_argv", "ShimHandle", "spawn_shim",
           "spawn_detached", "request_stop", "kill_tree", "descendants", "run_quick"]
