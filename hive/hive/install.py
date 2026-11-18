"""Service installation: systemd / schtasks XML / launchd / watchdog from util.launcher_argv, restart per method
(design §20.1, §13.1, D55, D56, WP4).

Every launcher is built from ``util.launcher_argv()`` (never a literal ``-m hive``) and starts in the daemon's data
dir. Exit-code convention (D55): every method restarts the daemon only after a non-zero exit; 0 = stopped on purpose,
75 = restart requested. ``restart()`` follows the §13.1 v6 sequence: holder exit via ``control.json``, then wait for
the method's own relaunch before issuing a start command, so a daemon is never started twice.
"""
from __future__ import annotations

import getpass
import logging
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple
from xml.sax.saxutils import escape as _xml_escape

from . import __version__, util
from .errors import HiveError, bad_request, unavailable, unsupported

log = logging.getLogger("hive.install")

METHODS = ("auto", "systemd", "launchd", "schtasks", "watchdog")
KINDS = ("node", "hub")
TASK_NAMES = {"node": "Hive Node", "hub": "Hive Hub"}
DESCRIPTIONS = {"node": "Hive Node (agent control endpoint)", "hub": "Hive Hub (agent gateway)"}
SYSTEM_PATH_TAIL = "/usr/local/bin:/usr/bin:/bin"
MAC_PATH_HEAD = "/opt/homebrew/bin:/usr/local/bin"
EXIT_WAIT = 30.0          # §13.1: wait <= 30 s for the old holder to exit
HOLDER_WAIT = 20.0        # §13.1: wait <= 20 s for a new holder (method relaunch), then again after a start command
NOT_INSTALLED_HINT = ("service install needs Hive installed: pip install hive-agent, pip install -e <checkout>, or "
                      "hive.pyz; a bare source checkout runs only in the foreground (hive node run)")


@dataclass
class InstallResult:
    """What an install/uninstall/restart did (or would do with dry_run)."""
    method: str
    files: List[str] = field(default_factory=list)
    commands: List[List[str]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------------------------------------------
# environment seams (tests patch these)

def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _systemd_user_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(_home() / ".config")
    return Path(base) / "systemd" / "user"


_SYSTEMD_SYSTEM_DIR = Path("/etc/systemd/system")


def _launch_agents_dir() -> Path:
    return _home() / "Library" / "LaunchAgents"


def _has_systemd() -> bool:
    """systemd is PID 1 (``/run/systemd/system`` exists) and systemctl is available."""
    return os.path.isdir("/run/systemd/system") and _which("systemctl") is not None


def _which(name: str) -> Optional[str]:
    from . import proctree
    return proctree.which(name, proctree.trusted_path())


def _execute(argv: List[str], timeout: float = 60.0) -> Tuple[int, str]:
    """Run one install command (no shell): (exit code, combined output)."""
    from . import proctree
    try:
        code, out, err = proctree.run_quick(argv, timeout=timeout)
    except HiveError as e:
        return 127, e.message
    return code, (util.decode_text(out) + util.decode_text(err)).strip()


def _sleep(s: float) -> None:
    time.sleep(s)


def _clock() -> float:
    return time.monotonic()


def _is_wsl() -> bool:
    try:
        from . import wsl
        return wsl.is_wsl()
    except Exception:  # noqa: BLE001
        return False


def _win_user() -> str:  # pragma: no cover - Windows only
    dom = os.environ.get("USERDOMAIN") or ""
    user = os.environ.get("USERNAME") or getpass.getuser()
    return f"{dom}\\{user}" if dom else user


# ---------------------------------------------------------------------------------------------------------------
# context

@dataclass
class _Ctx:
    kind: str
    config_path: Path
    data_dir: Path
    home: Path
    allow_root: bool = False
    cfg: Any = None


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise bad_request(f"unknown kind '{kind}'", hint="use node or hub")


def _context(kind: str, config_path: Optional[Path] = None, *, strict: bool = True) -> _Ctx:
    """Resolve config path, data dir and HIVE_HOME; ``strict`` loads (and validates) the config."""
    from . import config
    _check_kind(kind)
    paths = config.default_paths()
    cp = Path(config_path) if config_path else paths[f"{kind}_config"]
    cp = cp.expanduser().resolve()
    data = paths[f"{kind}_data"]
    cfg = None
    allow_root = False
    try:
        cfg = config.load_node_config(str(cp)) if kind == "node" else config.load_hub_config(str(cp))
        if getattr(cfg, "data_dir", None):
            data = Path(cfg.data_dir)
        allow_root = bool(getattr(cfg, "allow_root", False))
    except HiveError:
        if strict:
            raise
        data = cp.parent if cp.parent.exists() else data
    return _Ctx(kind=kind, config_path=cp, data_dir=Path(data).resolve(), home=config.hive_home(), allow_root=allow_root,
                cfg=cfg)


def detect_method() -> str:
    """Auto-detected install method: schtasks (Windows), launchd (macOS), systemd when PID 1 is systemd, else watchdog."""
    if util.IS_WINDOWS:
        return "schtasks"
    if util.IS_MAC:
        return "launchd"
    return "systemd" if _has_systemd() else "watchdog"


def _unit_name(kind: str) -> str:
    return f"hive-{kind}.service"


def _label(kind: str) -> str:
    return f"ai.hive.{kind}"


def _unit_path(kind: str, system: bool) -> Path:
    return (_SYSTEMD_SYSTEM_DIR if system else _systemd_user_dir()) / _unit_name(kind)


def _plist_path(kind: str) -> Path:
    return _launch_agents_dir() / f"{_label(kind)}.plist"


def _task_xml_path(ctx: _Ctx) -> Path:
    return ctx.data_dir / "run" / f"task-{ctx.kind}.xml"


def _watchdog_script(ctx: _Ctx) -> Path:
    return ctx.data_dir / f"run-{ctx.kind}.sh"


def _watchdog_pidfile(ctx: _Ctx) -> Path:
    return ctx.data_dir / "run" / "watchdog.pid"


# ---------------------------------------------------------------------------------------------------------------
# launcher check (v7, cons-5)

def check_launcher(data_dir: Path) -> None:
    """``self_command() + ["version"]`` from the data dir without PYTHONPATH must exit 0 and print this version;
    otherwise ``unsupported`` (a bare source checkout cannot be launched by a service manager)."""
    from . import proctree
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    cwd = Path(data_dir) if Path(data_dir).is_dir() else None
    try:
        code, out, err = proctree.run_quick(util.self_command() + ["version"], timeout=15, cwd=cwd, env=env)
    except HiveError as e:
        raise unsupported(f"cannot run Hive as a service: {e.message}", hint=NOT_INSTALLED_HINT)
    text = util.decode_text(out)
    if code != 0 or __version__ not in text:
        detail = (text.strip() or util.decode_text(err).strip()).splitlines()
        raise unsupported("cannot run Hive as a service: the launcher cannot import this Hive without PYTHONPATH"
                          + (f" ({detail[-1][:200]})" if detail else ""), hint=NOT_INSTALLED_HINT)


# ---------------------------------------------------------------------------------------------------------------
# renderers

def systemd_quote(s: str) -> str:
    """Quote one value for a unit file (v8): ``%`` -> ``%%``; with whitespace, quotes or backslashes the value is
    double-quoted with ``\\`` and ``"`` backslash-escaped."""
    s = str(s).replace("%", "%%")
    if s and not re.search(r'[\s"\'\\;$]', s):
        return s
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _exec_arg(s: str) -> str:
    """ExecStart argument: systemd_quote plus ``$`` -> ``$$`` (ExecStart expands variables)."""
    return systemd_quote(str(s).replace("$", "$$"))


def _env_line(name: str, value: str) -> str:
    """One quoted ``Environment="NAME=value"`` line (v8)."""
    v = f"{name}={value}".replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"')
    return f'Environment="{v}"'


def captured_path(extra_tail: str = SYSTEM_PATH_TAIL) -> str:
    """The current PATH (absolute entries, deduplicated) followed by ``extra_tail``."""
    out: List[str] = []
    for d in (os.environ.get("PATH", "").split(os.pathsep) + extra_tail.split(":")):
        if d and os.path.isabs(d) and d not in out:
            out.append(d)
    return ":".join(out)


def render_systemd(ctx: _Ctx, *, system: bool = False, user: Optional[str] = None, capture_path: bool = True) -> str:
    """The ``hive-<kind>.service`` unit text (§20.1)."""
    argv = util.launcher_argv(ctx.kind, ctx.config_path)
    lines = ["[Unit]", f"Description={DESCRIPTIONS[ctx.kind]}", "After=network-online.target",
             "Wants=network-online.target", "", "[Service]", "Type=simple"]
    if system and user:
        lines.append(f"User={user}")
    lines += [f"WorkingDirectory={systemd_quote(str(ctx.data_dir))}",
              "ExecStart=" + " ".join(_exec_arg(a) for a in argv),
              _env_line("PYTHONUNBUFFERED", "1"), _env_line("HIVE_HOME", str(ctx.home))]
    if capture_path:
        lines.append(_env_line("PATH", captured_path()))
    lines += ["Restart=on-failure", "RestartSec=5", "KillMode=process", "TimeoutStopSec=60", "",
              "[Install]", f"WantedBy={'multi-user.target' if system else 'default.target'}", ""]
    return "\n".join(lines)


def render_plist(ctx: _Ctx, *, capture_path: bool = True) -> bytes:
    """The launchd agent plist (§20.1): RunAtLoad, KeepAlive {SuccessfulExit: false}, WorkingDirectory = data dir."""
    path = MAC_PATH_HEAD + (":" + captured_path() if capture_path else ":" + SYSTEM_PATH_TAIL + ":/usr/sbin:/sbin")
    seen: List[str] = []
    for d in path.split(":"):
        if d and d not in seen:
            seen.append(d)
    logs = ctx.data_dir / "logs"
    d = {"Label": _label(ctx.kind), "ProgramArguments": util.launcher_argv(ctx.kind, ctx.config_path),
         "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
         "EnvironmentVariables": {"HIVE_HOME": str(ctx.home), "PATH": ":".join(seen), "PYTHONUNBUFFERED": "1"},
         "StandardOutPath": str(logs / "launchd.out.log"), "StandardErrorPath": str(logs / "launchd.err.log"),
         "WorkingDirectory": str(ctx.data_dir), "ProcessType": "Interactive"}
    return plistlib.dumps(d)


def render_task_xml(ctx: _Ctx, *, user: Optional[str] = None) -> str:
    """The Scheduled Task XML (§20.1): logon trigger, LeastPrivilege + InteractiveToken, ExecutionTimeLimit PT0S,
    RestartOnFailure PT1M x999 (backstop), Priority 5, Action = supervising windowless launcher in the data dir."""
    argv = util.launcher_argv(ctx.kind, ctx.config_path, windowless=True, supervise=True)
    user = user or (_win_user() if util.IS_WINDOWS else getpass.getuser())
    e = _xml_escape
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{e(DESCRIPTIONS[ctx.kind])}</Description>
    <URI>\\{e(TASK_NAMES[ctx.kind])}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{e(user)}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{e(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>5</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{e(argv[0])}</Command>
      <Arguments>{e(subprocess.list2cmdline(argv[1:]))}</Arguments>
      <WorkingDirectory>{e(str(ctx.data_dir))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _sq(s: str) -> str:
    """POSIX single quoting."""
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def render_watchdog(ctx: _Ctx, *, capture_path: bool = True) -> str:
    """``run-<kind>.sh`` (§20.1): exports HIVE_HOME, HIVE_WATCHDOG=1 [PATH], cd data dir, loop: ``_update`` (node),
    launcher; break on exit 0 (D55), else sleep 5 and relaunch."""
    log_file = _sq(str(ctx.data_dir / "logs" / "stdout.log"))
    lines = ["#!/bin/sh", f"# Hive {ctx.kind} watchdog (written by hive service install {ctx.kind})",
             f"export HIVE_HOME={_sq(str(ctx.home))}", "export HIVE_WATCHDOG=1"]
    if capture_path:
        lines.append(f"export PATH={_sq(captured_path())}")
    lines += [f"cd {_sq(str(ctx.data_dir))} || exit 1", "while true; do"]
    if ctx.kind == "node":
        lines.append("  " + " ".join(_sq(a) for a in util.self_command() + ["_update"])
                     + f" >> {log_file} 2>&1 || true")
    lines += ["  " + " ".join(_sq(a) for a in util.launcher_argv(ctx.kind, ctx.config_path)) + f" >> {log_file} 2>&1",
              '  [ "$?" -eq 0 ] && break', "  sleep 5", "done", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------------------------------
# install

def _systemctl(system: bool, *args: str) -> List[str]:
    exe = _which("systemctl") or "systemctl"
    return [exe] + ([] if system else ["--user"]) + list(args)


def _launchctl(*args: str) -> List[str]:
    return [_which("launchctl") or "/bin/launchctl"] + list(args)


def _schtasks(*args: str) -> List[str]:
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "schtasks.exe")
    return [exe if os.path.exists(exe) else "schtasks"] + list(args)


def _run_all(res: InstallResult, cmds: List[List[str]], *, dry_run: bool, check: bool = True) -> None:
    for c in cmds:
        res.commands.append(c)
        if dry_run:
            continue
        code, out = _execute(c)
        if code != 0 and check:
            raise HiveError("internal", f"{os.path.basename(c[0])} {' '.join(c[1:3])} failed (exit {code}): "
                                        f"{out[:300]}", hint="run the command by hand to see the full error")


def install(kind: str, config_path: Path, *, method: str = "auto", system: bool = False, dry_run: bool = False,
            capture_path: bool = True) -> InstallResult:
    """Install the node/hub as a service (``dry_run``: render and list, change nothing)."""
    if method not in METHODS:
        raise bad_request(f"unknown install method '{method}'", hint="use " + ", ".join(METHODS))
    ctx = _context(kind, config_path)
    m = detect_method() if method == "auto" else method
    if system and m != "systemd":
        raise bad_request("--system is only available for systemd", hint="drop --system")
    check_launcher(ctx.data_dir)
    res = InstallResult(method=m)
    if m == "systemd":
        _install_systemd(ctx, res, system=system, dry_run=dry_run, capture_path=capture_path)
    elif m == "launchd":
        _install_launchd(ctx, res, dry_run=dry_run, capture_path=capture_path)
    elif m == "schtasks":
        _install_schtasks(ctx, res, dry_run=dry_run, capture_path=capture_path)
    else:
        _install_watchdog(ctx, res, dry_run=dry_run, capture_path=capture_path)
    if dry_run:
        res.notes.insert(0, "dry run: nothing was written or started")
    return res


def _install_systemd(ctx: _Ctx, res: InstallResult, *, system: bool, dry_run: bool, capture_path: bool) -> None:
    user: Optional[str] = None
    if system:
        user = os.environ.get("SUDO_USER") or ""
        if not user and not ctx.allow_root:
            raise bad_request("--system needs SUDO_USER (run it with sudo from your own account)",
                              hint="sudo hive service install " + ctx.kind + " --system, or set allow_root:true in "
                                   "the config to run the daemon as root")
        user = user or "root"
    unit = _unit_path(ctx.kind, system)
    text = render_systemd(ctx, system=system, user=user, capture_path=capture_path)
    res.files.append(str(unit))
    if not dry_run:
        unit.parent.mkdir(parents=True, exist_ok=True)
        util.atomic_write_bytes(unit, text.encode("utf-8"))
        os.chmod(unit, 0o644)
        (ctx.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    _run_all(res, [_systemctl(system, "daemon-reload"), _systemctl(system, "enable", "--now", _unit_name(ctx.kind))],
             dry_run=dry_run)
    if not system:
        prefix = "sudo " if _is_wsl() or os.geteuid() != 0 else ""
        res.notes.append(f"start at boot without a login: {prefix}loginctl enable-linger {getpass.getuser()}")
        if _is_wsl():
            res.notes.append("inside WSL the user unit starts with the distro; the Windows node's wsl.keepalive "
                             "keeps the distro running (§20.7)")
    res.notes.append(f"logs: journalctl {'' if system else '--user '}-u {_unit_name(ctx.kind)} -f")


def _install_launchd(ctx: _Ctx, res: InstallResult, *, dry_run: bool, capture_path: bool) -> None:
    plist = _plist_path(ctx.kind)
    data = render_plist(ctx, capture_path=capture_path)
    res.files.append(str(plist))
    uid = os.getuid() if hasattr(os, "getuid") else 0
    if not dry_run:
        plist.parent.mkdir(parents=True, exist_ok=True)
        (ctx.data_dir / "logs").mkdir(parents=True, exist_ok=True)
        util.atomic_write_bytes(plist, data)
        os.chmod(plist, 0o644)
        # an older registration must go first; failure (not loaded) is fine
        res.commands.append(_launchctl("bootout", f"gui/{uid}/{_label(ctx.kind)}"))
        _execute(res.commands[-1])
    else:
        res.commands.append(_launchctl("bootout", f"gui/{uid}/{_label(ctx.kind)}"))
    _run_all(res, [_launchctl("bootstrap", f"gui/{uid}", str(plist))], dry_run=dry_run)


def _install_schtasks(ctx: _Ctx, res: InstallResult, *, dry_run: bool, capture_path: bool) -> None:
    xml_path = _task_xml_path(ctx)
    text = render_task_xml(ctx)
    res.files.append(str(xml_path))
    if ctx.kind == "node" and capture_path:
        res.notes.append("PATH captured into policy.exec.path_prepend (task environments lack user PATH changes)")
        if not dry_run:
            _capture_path_prepend(ctx)
    if not dry_run:
        util.secure_mkdir(xml_path.parent)
        util.atomic_write_bytes(xml_path, b"\xff\xfe" + text.encode("utf-16-le"), private=True)
    name = TASK_NAMES[ctx.kind]
    _run_all(res, [_schtasks("/Create", "/TN", name, "/XML", str(xml_path), "/F"), _schtasks("/Run", "/TN", name)],
             dry_run=dry_run)
    res.notes.append("runs at logon while you are signed in (lock the screen, do not sign out); stop and restart "
                     f"with hive {ctx.kind} stop|restart, not schtasks /End")


def _capture_path_prepend(ctx: _Ctx) -> None:
    from . import config
    cur = [d for d in os.environ.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)]

    def edit(raw: dict) -> None:
        ex = raw.setdefault("policy", {}).setdefault("exec", {})
        old = [d for d in ex.get("path_prepend") or [] if isinstance(d, str)]
        ex["path_prepend"] = old + [d for d in cur if d not in old]
    config.edit_config(ctx.config_path, edit, kind="node")


def _install_watchdog(ctx: _Ctx, res: InstallResult, *, dry_run: bool, capture_path: bool) -> None:
    script = _watchdog_script(ctx)
    text = render_watchdog(ctx, capture_path=capture_path)
    res.files.append(str(script))
    start = ["/bin/sh", str(script)]
    line = f"nohup /bin/sh {_sq(str(script))} >/dev/null 2>&1 &"
    res.notes.append(f"add to the pod/container start command: {line}")
    res.notes.append("pod templates can use deploy/runpod/start-hive.sh instead (it bootstraps hive.pyz and runs "
                     "the same loop)")
    if dry_run:
        res.commands.append(["setsid", "nohup"] + start)
        return
    util.secure_mkdir(ctx.data_dir)
    (ctx.data_dir / "logs").mkdir(parents=True, exist_ok=True)
    util.atomic_write_bytes(script, text.encode("utf-8"))
    os.chmod(script, 0o700)
    pid = _watchdog_alive(ctx)
    if pid:
        res.notes.append(f"a watchdog is already running (pid {pid}); it keeps its loop until it stops")
        return
    res.commands.append(["setsid", "nohup"] + start)
    _start_watchdog(ctx)


def _start_watchdog(ctx: _Ctx) -> int:
    """Start ``run-<kind>.sh`` detached (new session, stdio to /dev/null); records run/watchdog.pid."""
    script = _watchdog_script(ctx)
    p = subprocess.Popen(["/bin/sh", str(script)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, cwd=str(ctx.data_dir), start_new_session=True, close_fds=True)
    _track(p)
    pf = _watchdog_pidfile(ctx)
    util.secure_mkdir(pf.parent)
    util.write_json_atomic(pf.with_suffix(".json"), {"pid": p.pid, "ctime": util.process_create_time(p.pid),
                                                     "script": str(script)}, private=True)
    util.atomic_write_bytes(pf, f"{p.pid}\n".encode("ascii"), private=True)
    return p.pid


_children: List[subprocess.Popen] = []


def _track(p: subprocess.Popen) -> None:
    _children.append(p)
    for c in list(_children):
        if c.poll() is not None:
            _children.remove(c)


def _watchdog_alive(ctx: _Ctx) -> Optional[int]:
    """The live watchdog pid of this kind (pid file + ctime / script check), else None."""
    pf = _watchdog_pidfile(ctx)
    rec = util.read_json(pf.with_suffix(".json"), default=None)
    try:
        pid = int(pf.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    ctime = rec.get("ctime") if isinstance(rec, dict) and rec.get("pid") == pid else None
    if not util.pid_alive(pid, ctime):
        return None
    if ctime is None:
        from . import procinfo
        info = procinfo.process_info(pid) or {}
        if str(_watchdog_script(ctx)) not in " ".join(info.get("cmdline") or []):
            return None
    return pid


# ---------------------------------------------------------------------------------------------------------------
# installed state

def _installed(ctx: _Ctx) -> Tuple[Optional[str], bool]:
    """(method, system) of an existing installation, or (None, False)."""
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        code, _out = _execute(_schtasks("/Query", "/TN", TASK_NAMES[ctx.kind]), timeout=15)
        if code == 0:
            return "schtasks", False
    if util.IS_MAC and _plist_path(ctx.kind).exists():
        return "launchd", False
    if not util.IS_WINDOWS:
        if _unit_path(ctx.kind, False).exists():
            return "systemd", False
        if _unit_path(ctx.kind, True).exists():
            return "systemd", True
        if _watchdog_script(ctx).exists():
            return "watchdog", False
    return None, False


def _holder(ctx: _Ctx) -> Optional[dict]:
    return util.FileLock(ctx.data_dir / f"{ctx.kind}.lock").holder()


def _pid_alive(pid: int, ctime: Optional[float]) -> bool:
    return util.pid_alive(pid, ctime)


def _launcher_alive(ctx: _Ctx) -> bool:
    rec = util.read_json(ctx.data_dir / "run" / "launcher.json", default=None)
    if not isinstance(rec, dict) or not isinstance(rec.get("pid"), int):
        return False
    return _pid_alive(rec["pid"], rec.get("ctime"))


def status(kind: str) -> dict:
    """Installation status: ``{kind, installed, method, system, files, running, pid, data_dir, active}``."""
    ctx = _context(kind, strict=False)
    method, system = _installed(ctx)
    files: List[str] = []
    active: Optional[str] = None
    if method == "systemd":
        files.append(str(_unit_path(kind, system)))
        code, out = _execute(_systemctl(system, "is-active", _unit_name(kind)), timeout=10)
        active = out.strip().splitlines()[0] if out.strip() else ("active" if code == 0 else "unknown")
    elif method == "launchd":
        files.append(str(_plist_path(kind)))
    elif method == "schtasks":  # pragma: no cover - Windows only
        files.append(str(_task_xml_path(ctx)))
    elif method == "watchdog":
        files.append(str(_watchdog_script(ctx)))
        active = "running" if _watchdog_alive(ctx) else "stopped"
    h = _holder(ctx)
    return {"kind": kind, "installed": method is not None, "method": method, "system": system, "files": files,
            "running": h is not None, "pid": h["pid"] if h else None, "data_dir": str(ctx.data_dir),
            "active": active}


# ---------------------------------------------------------------------------------------------------------------
# uninstall

def _stop_daemon(ctx: _Ctx, res: InstallResult, *, dry_run: bool) -> None:
    """Graceful stop via control.json (exit 0: no method restarts it, D55); waits <= 30 s."""
    h = _holder(ctx)
    if h is None:
        return
    res.notes.append(f"stopping the running {ctx.kind} (pid {h['pid']}) via control.json")
    if dry_run:
        return
    _write_control(ctx, "stop")
    end = _clock() + EXIT_WAIT
    while _clock() < end and _pid_alive(h["pid"], h.get("ctime")):
        _sleep(0.2)
    if _pid_alive(h["pid"], h.get("ctime")):
        res.notes.append(f"the {ctx.kind} is still stopping (pid {h['pid']})")


def uninstall(kind: str, *, method: str = "auto", system: bool = False, dry_run: bool = False,
              stop_services: bool = False) -> InstallResult:
    """Remove the service installation (the daemon is stopped gracefully first; ``stop_services`` also stops every
    job and service of the node afterwards)."""
    if method not in METHODS:
        raise bad_request(f"unknown install method '{method}'", hint="use " + ", ".join(METHODS))
    ctx = _context(kind, strict=False)
    inst, inst_system = _installed(ctx)
    m = inst if method == "auto" else method
    system = system or (inst_system if m == inst else False)
    res = InstallResult(method=m or "none")
    if m is None:
        res.notes.append(f"no {kind} service installation found")
        _stop_daemon(ctx, res, dry_run=dry_run)
        return res
    if m == "systemd":
        unit = _unit_path(kind, system)
        _run_all(res, [_systemctl(system, "disable", "--now", _unit_name(kind))], dry_run=dry_run, check=False)
        res.files.append(str(unit))
        if not dry_run:
            try:
                unit.unlink()
            except FileNotFoundError:
                pass
        _run_all(res, [_systemctl(system, "daemon-reload")], dry_run=dry_run, check=False)
    elif m == "launchd":
        uid = os.getuid() if hasattr(os, "getuid") else 0
        plist = _plist_path(kind)
        _run_all(res, [_launchctl("bootout", f"gui/{uid}/{_label(kind)}")], dry_run=dry_run, check=False)
        res.files.append(str(plist))
        if not dry_run:
            try:
                plist.unlink()
            except FileNotFoundError:
                pass
    elif m == "schtasks":
        _stop_daemon(ctx, res, dry_run=dry_run)
        name = TASK_NAMES[kind]
        _run_all(res, [_schtasks("/End", "/TN", name), _schtasks("/Delete", "/TN", name, "/F")], dry_run=dry_run,
                 check=False)
        xml_path = _task_xml_path(ctx)
        res.files.append(str(xml_path))
        if not dry_run:
            try:
                xml_path.unlink()
            except FileNotFoundError:
                pass
    else:
        _stop_daemon(ctx, res, dry_run=dry_run)
        script = _watchdog_script(ctx)
        res.files.append(str(script))
        pid = _watchdog_alive(ctx)
        if pid:
            res.commands.append(["kill", str(pid)])
            if not dry_run:
                # only the watchdog shell: the node already stopped (or stops now with SIGTERM), and jobs/services
                # live in their own sessions
                try:
                    import signal
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        if not dry_run:
            for p in (script, _watchdog_pidfile(ctx), _watchdog_pidfile(ctx).with_suffix(".json")):
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
    if stop_services and kind == "node":
        if dry_run:
            res.notes.append("would stop every job and service of this node")
        else:
            _stop_daemon(ctx, res, dry_run=False)
            try:
                from . import services
                node_id = getattr(ctx.cfg, "node_id", None) or ""
                out = services.panic_offline(ctx.data_dir, node_id)
                res.notes.append(f"stopped {len(out.get('jobs') or [])} jobs and {len(out.get('services') or [])} "
                                 "services")
            except Exception as e:  # noqa: BLE001
                res.notes.append(f"could not stop jobs/services: {e}")
    if dry_run:
        res.notes.insert(0, "dry run: nothing was removed or stopped")
    return res


# ---------------------------------------------------------------------------------------------------------------
# restart (§13.1 v6)

def _write_control(ctx: _Ctx, cmd: str) -> None:
    util.secure_mkdir(ctx.data_dir)
    util.write_json_atomic(ctx.data_dir / "control.json", {"cmd": cmd, "at": util.iso_utc()}, private=True)


def _start_command(ctx: _Ctx, method: str, system: bool) -> List[str]:
    if method == "systemd":
        return _systemctl(system, "start", _unit_name(ctx.kind))
    if method == "launchd":
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return _launchctl("kickstart", f"gui/{uid}/{_label(ctx.kind)}")
    if method == "schtasks":
        return _schtasks("/Run", "/TN", TASK_NAMES[ctx.kind])
    return ["setsid", "nohup", "/bin/sh", str(_watchdog_script(ctx))]


def _manual(ctx: _Ctx, method: Optional[str], system: bool) -> str:
    if method is None:
        return f"hive {ctx.kind} run"
    return " ".join(_start_command(ctx, method, system))


def _wait_new_holder(ctx: _Ctx, old_pid: Optional[int], timeout: float) -> Optional[dict]:
    end = _clock() + timeout
    while True:
        h = _holder(ctx)
        if h is not None and h.get("pid") != old_pid:
            return h
        if _clock() >= end:
            return None
        _sleep(0.25)


def _issue_start(ctx: _Ctx, method: str, system: bool, res: InstallResult) -> None:
    """Run the method's start command once, unless its supervisor is still alive (schtasks launcher, watchdog)."""
    if method == "schtasks" and _launcher_alive(ctx):
        res.notes.append("the Windows launcher is still alive; not running schtasks /Run")
        return
    if method == "watchdog" and _watchdog_alive(ctx):
        res.notes.append("the watchdog is still alive; it relaunches the daemon")
        return
    cmd = _start_command(ctx, method, system)
    res.commands.append(cmd)
    if method == "watchdog":
        _start_watchdog(ctx)
        return
    code, out = _execute(cmd)
    if code != 0:
        res.notes.append(f"{' '.join(cmd)} failed (exit {code}): {out[:200]}")


def restart(kind: str, *, dry_run: bool = False) -> InstallResult:
    """§13.1 restart sequence (holder exit, method restart or start, verify, Windows retry)."""
    ctx = _context(kind, strict=False)
    method, system = _installed(ctx)
    res = InstallResult(method=method or "foreground")
    h = _holder(ctx)
    if dry_run:
        if h is not None:
            res.notes.append(f"would write {ctx.data_dir / 'control.json'} (restart) and wait <= {EXIT_WAIT:g} s "
                             f"for pid {h['pid']} to exit")
        if method is None:
            res.notes.append(f"no service installation: start it again with hive {kind} run")
            return res
        res.notes.append(f"would wait <= {HOLDER_WAIT:g} s for {method} to start the {kind} by itself, else run:")
        res.commands.append(_start_command(ctx, method, system))
        if method == "schtasks":
            res.notes.append("Windows retry when no new holder appears: schtasks /End + /Run once")
            res.commands.append(_schtasks("/End", "/TN", TASK_NAMES[kind]))
            res.commands.append(_schtasks("/Run", "/TN", TASK_NAMES[kind]))
        return res
    old_pid: Optional[int] = None
    if h is None:
        if method is None:
            raise unavailable(f"the {kind} is not running", hint=f"start it: hive {kind} run (or hive service "
                              f"install {kind})")
        _issue_start(ctx, method, system, res)
    else:
        old_pid = h["pid"]
        _write_control(ctx, "restart")
        end = _clock() + EXIT_WAIT
        while _pid_alive(h["pid"], h.get("ctime")):
            if _clock() >= end:
                raise HiveError("timeout", f"the {kind} (pid {h['pid']}) is still running after {EXIT_WAIT:g} s",
                                hint=f"check hive {kind} status; it restarts after in-flight calls finish")
            _sleep(0.2)
        if method is None:
            raise unavailable(f"the {kind} stopped, but it ran in the foreground (no service installation restarts "
                              "it)", hint=f"start it again: hive {kind} run")
        if _wait_new_holder(ctx, old_pid, HOLDER_WAIT) is not None:
            res.notes.append(f"{method} restarted the {kind}")
            return res
        _issue_start(ctx, method, system, res)
    if _wait_new_holder(ctx, old_pid, HOLDER_WAIT) is not None:
        return res
    if method == "schtasks":
        for c in (_schtasks("/End", "/TN", TASK_NAMES[kind]), _schtasks("/Run", "/TN", TASK_NAMES[kind])):
            res.commands.append(c)
            _execute(c)
        if _wait_new_holder(ctx, old_pid, HOLDER_WAIT) is not None:
            return res
    raise unavailable(f"the {kind} did not start within {HOLDER_WAIT:g} s",
                      hint=f"start it by hand: {_manual(ctx, method, system)}; see the {kind} log in "
                           f"{ctx.data_dir / 'logs'}")


__all__ = ["METHODS", "InstallResult", "detect_method", "install", "uninstall", "status", "restart",
           "render_systemd", "render_plist", "render_task_xml", "render_watchdog", "systemd_quote", "check_launcher",
           "captured_path", "TASK_NAMES"]
