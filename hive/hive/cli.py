"""argparse CLI ``hive`` (design §19, WP6).

WP6a part: ``init node|hub|client``, ``node run|status|stop|restart|pause|resume|panic|token …|secret …``, ``call``,
``sh``, ``mcp`` (stdio), ``tools export``, ``nodes``, ``actions``, ``version``. The remaining commands (hub, gateway,
approvals, service install, doctor, push/pull, logs, join, mcp-config, tls-pin, audit, dist) arrive with WP6b and
answer "not available yet" (exit 2).

Exit codes (§19): 0 ok; 1 action error; 2 usage/config error; 3 auth error (unauthorized/forbidden/policy_denied);
4 unavailable/timeout. ``hive sh`` exits with the remote exit code (0–255), 125 for Hive-level failures, 124 for
timeouts and for a call that yielded to a job. Errors print ``code: message`` and ``Hint: …`` on stderr (JSON with
``--json``). Every agent- or node-supplied text printed here goes through ``util.escape_terminal``.
This module is one of the two places allowed to print and to use ``util.iso()`` (§22.3 rules 5 and 11).
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, util
from .errors import HiveError, conflict

EXIT_OK, EXIT_ACTION, EXIT_USAGE, EXIT_AUTH, EXIT_UNAVAILABLE = 0, 1, 2, 3, 4
SH_FAILURE, SH_TIMEOUT = 125, 124
WP6B = ("hub", "gateway", "approvals", "service", "doctor", "push", "pull", "logs", "join", "mcp-config",
        "tls-pin", "audit", "dist")
TOKEN_WARNING = "warning: --token is visible in ps and shell history; prefer --token-env/--token-file"
HUB_REVOKE_LINE = "if a hub routes agents here, also run on the hub: hive approvals revoke --all --hub"


class CliError(Exception):
    """A usage/config problem with an exit code (printed as ``message``)."""

    def __init__(self, message: str, code: int = EXIT_USAGE, hint: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.hint = hint


# ---------------------------------------------------------------------------------------------------------------
# output helpers

class Out:
    """Printing with ``--json`` support and terminal escaping of foreign text."""

    def __init__(self, as_json: bool = False):
        self.as_json = as_json

    def line(self, text: str = "") -> None:
        """Print Hive's own text."""
        print(text)

    def foreign(self, text: str, *, err: bool = False) -> None:
        """Print agent/node-supplied text through ``escape_terminal`` (CRLF folded)."""
        s = util.escape_terminal(str(text).replace("\r\n", "\n"))
        if s and not s.endswith("\n"):
            s += "\n"
        _write(sys.stderr if err else sys.stdout, s)

    def data(self, obj: Any) -> None:
        """Machine output (JSON) or a pretty JSON dump through escape_terminal."""
        if self.as_json:
            print(json.dumps(obj, ensure_ascii=True, default=str))
        else:
            self.foreign(json.dumps(obj, ensure_ascii=False, indent=2, default=str))

    def error(self, code: str, message: str, hint: Optional[str] = None, details: Optional[dict] = None) -> None:
        """``code: message`` + ``Hint: …`` on stderr (JSON with --json)."""
        if self.as_json:
            d: Dict[str, Any] = {"code": code, "message": message}
            if hint:
                d["hint"] = hint
            if details:
                d["details"] = details
            sys.stderr.write(json.dumps({"ok": False, "error": d}, ensure_ascii=True, default=str) + "\n")
            return
        sys.stderr.write(util.escape_terminal(f"{code}: {message}") + "\n")
        if hint:
            sys.stderr.write(util.escape_terminal(f"Hint: {hint}") + "\n")


def _write(stream: Any, s: str) -> None:
    """Write text that may hold characters the stream's encoding lacks (a pipe on Windows uses the ANSI code page,
    e.g. cp936): they become backslash escapes instead of a UnicodeEncodeError after the remote command ran."""
    try:
        stream.write(s)
    except UnicodeEncodeError:
        enc = getattr(stream, "encoding", None) or "ascii"
        stream.write(s.encode(enc, "backslashreplace").decode(enc, "replace"))


def tolerant_streams() -> None:
    """``errors='backslashreplace'`` on stdout/stderr (3.7+ ``reconfigure``), so ``print`` of node text never raises
    UnicodeEncodeError when the output is a pipe or file in a legacy Windows code page (§19 exit codes)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass


def exit_code_for(err: HiveError) -> int:
    """§19 exit code of a HiveError from an action or connection."""
    if err.code in ("unauthorized", "forbidden", "policy_denied"):
        return EXIT_AUTH
    if err.code in ("unavailable", "timeout"):
        return EXIT_UNAVAILABLE
    return EXIT_ACTION


def _table(rows: Sequence[Sequence[Any]], cols: Sequence[str]) -> str:
    cells = [[util.escape_terminal("-" if c is None else str(c)) for c in r] for r in rows]
    widths = [len(c) for c in cols]
    for r in cells:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))
    lines = ["  ".join(c.ljust(widths[i]) for i, c in enumerate(cols)).rstrip()]
    for r in cells:
        lines.append("  ".join(c.ljust(widths[i]) for i, c in enumerate(r)).rstrip())
    return "\n".join(lines)


def _confirm(question: str, *, yes: bool = False) -> bool:
    if yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        return False
    try:
        return input(question + " [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


# ---------------------------------------------------------------------------------------------------------------
# connections

def _connect(args: argparse.Namespace, *, owner_default: bool = False, default_node: Optional[str] = None) -> Any:
    """Client for --connection / env / ``this-pc-owner`` (CLI admin default, §14.4) / client.json default."""
    from .client import connect
    from .config import load_client_config
    name = getattr(args, "connection", None)
    kw: Dict[str, Any] = {}
    if default_node:
        kw["default_node"] = default_node
    if name:
        return connect(name, **kw)
    if owner_default and not os.environ.get("HIVE_URL") and not os.environ.get("HIVE_CONNECTION"):
        try:
            cfg = load_client_config()
        except HiveError:
            cfg = None
        if cfg is not None and "this-pc-owner" in cfg.connections:
            return connect("this-pc-owner", **kw)
    return connect(None, **kw)


def _node_config(args: argparse.Namespace) -> Any:
    from .config import default_paths, load_node_config
    path = getattr(args, "config", None) or str(default_paths()["node_config"])
    if not Path(path).exists():
        raise CliError(f"no node config at {path}", hint="run: hive init node")
    try:
        return load_node_config(path)
    except HiveError as e:
        raise CliError(f"{e.code}: {e.message}", hint=e.hint)


def _data_dir(cfg: Any) -> Path:
    return Path(cfg.data_dir or Path(getattr(cfg, "_path", ".")).parent)


def _holder(data_dir: Path) -> Optional[dict]:
    return util.FileLock(data_dir / "node.lock").holder()


def _restart_daemon(kind: str, out: Out, *, no_restart: bool = False) -> None:
    """Config changes apply through a graceful daemon restart (§8.6); prints what happened."""
    from .config import default_paths
    d = default_paths()["node_data" if kind == "node" else "hub_data"]
    if util.FileLock(d / f"{kind}.lock").holder() is None:
        return
    if no_restart:
        out.line(f"restart to apply: hive {kind} restart")
        return
    try:
        from . import install
        install.restart(kind)
        out.line(f"restarted the running {kind}")
    except HiveError as e:
        out.line(f"restart to apply: hive {kind} restart ({e.message})")


def _console_command() -> List[str]:
    """argv that starts this Hive (console script preferred, D56)."""
    zp = util._zipapp_path()
    if zp is not None:
        return [sys.executable, str(zp)]
    name = "hive.exe" if os.name == "nt" else "hive"
    exe_dir = Path(sys.executable).parent
    dirs: List[Path] = [exe_dir]
    try:
        import sysconfig
        sp = sysconfig.get_path("scripts")
        if sp:
            dirs.append(Path(sp))
    except (KeyError, ValueError, OSError):
        pass
    dirs.append(exe_dir / "Scripts")          # non-venv Windows install: <prefix>\Scripts\hive.exe
    for d in dirs:
        cand = d / name
        if cand.is_file():
            return [os.path.abspath(str(cand))]
    exe = _which_not_cwd("hive")
    if exe:
        return [exe]
    return [sys.executable] + (["-P"] if sys.version_info >= (3, 11) else []) + ["-m", "hive"]


def _which_not_cwd(cmd: str) -> Optional[str]:
    """``which`` over PATH without the current directory (Windows Python ≤ 3.11 searches the cwd first, so a
    ``hive.bat`` in the folder where init runs could end up in the MCP snippet; D56)."""
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
    except OSError:
        cwd = ""
    keep = []
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d or not os.path.isabs(d) or os.path.normcase(os.path.abspath(d)) == cwd:  # relative = cwd-based
            continue
        keep.append(d)
    # searched by hand: shutil.which itself puts the cwd first on Windows (Python < 3.12) even with path=
    exts = [""]
    if os.name == "nt":  # pragma: no cover - Windows only
        exts = [e for e in (os.environ.get("PATHEXT") or ".COM;.EXE;.BAT;.CMD").split(os.pathsep) if e]
    for d in keep:
        for ext in exts:
            cand = os.path.join(d, cmd + ext)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return os.path.abspath(cand)
    return None


# ---------------------------------------------------------------------------------------------------------------
# init

def _read_token_arg(args: argparse.Namespace, out: Out) -> Optional[str]:
    if getattr(args, "token_env", None):
        v = os.environ.get(args.token_env, "").strip()
        if not v:
            raise CliError(f"environment variable {args.token_env} is empty")
        return v
    if getattr(args, "token_file", None):
        try:
            return Path(os.path.expanduser(args.token_file)).read_text(encoding="utf-8-sig").strip().splitlines()[0]
        except (OSError, IndexError) as e:
            raise CliError(f"cannot read token file {args.token_file}: {e}")
    if getattr(args, "token", None):
        sys.stderr.write(TOKEN_WARNING + "\n")
        return args.token
    return None


def _consent_by() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = os.environ.get("USER") or os.environ.get("USERNAME") or "user"
    return f"{user}@{socket.gethostname()}"


def _ask_consent(args: argparse.Namespace, what: str) -> None:
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = "this user"
    text = (f"Remote AI agents will be able to run commands and change files on this computer as user '{user}' "
            f"within this policy.")
    if args.yes:
        return
    print(text)
    if not sys.stdin or not sys.stdin.isatty():
        raise CliError(f"consent needed to create the {what} config: run in a terminal and type yes, or pass --yes")
    try:
        ans = input("Type yes to continue: ").strip().lower()
    except EOFError:
        ans = ""
    if ans != "yes":
        raise CliError("consent not given; nothing written")


def cmd_init_node(args: argparse.Namespace, out: Out) -> int:
    """``hive init node``."""
    from . import config
    paths = config.default_paths()
    cfg_path = paths["node_config"]
    if args.config and Path(args.config).expanduser().resolve() != cfg_path.resolve():
        raise CliError("init writes the default node config location", hint=f"set HIVE_HOME instead ({cfg_path})")
    exists = cfg_path.exists() and not args.force
    if args.insecure and not args.lan:
        raise CliError("--insecure only goes with --lan")
    topology = None
    if args.tailscale:
        topology = "tailscale"
    elif args.lan:
        topology = "lan-insecure" if args.insecure else "lan"
    elif args.pod:
        topology = "pod"
    listen = "off" if args.no_listen else args.listen
    relay_token = _read_token_arg(args, out)
    if args.join and args.relay:
        raise CliError("give --join or --relay, not both")
    if not exists:
        _ask_consent(args, "node")
    else:
        ignored = [f for f, v in (("--preset", args.preset is not None), ("--wsl", args.wsl), ("--pod", args.pod),
                                  ("--name", bool(args.name))) if v]
        if ignored:
            out.line(f"note: {', '.join(ignored)} applies only when creating a config; use --force to rewrite")
    try:
        cfg, toks = config.init_node(
            name=args.name if not exists else None, preset=args.preset or "standard", topology=topology if not
            (exists and topology == "pod") else None, listen=listen, roots=args.root or (), relay_url=args.relay,
            # None = not given: on an existing config only an explicit --relay-role (even "operator") changes it
            relay_token=relay_token, relay_role=args.relay_role if exists else (args.relay_role or "operator"),
            relay_proxy=args.relay_proxy, tls_pin=args.tls_pin,
            join=args.join, tls_cert=args.tls_cert, tls_key=args.tls_key, proxy=args.proxy, mirror=args.mirror,
            models_dir=args.models_dir, wsl=bool(args.wsl) and not exists, wsl_keepalive=args.wsl_keepalive or (),
            consent_by=_consent_by(), force=args.force)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    if not toks:
        changed = getattr(cfg, "_changed", [])
        out.line(f"updated {cfg_path}: " + (", ".join(changed) if changed else "no changes"))
        _restart_daemon("node", out, no_restart=args.no_restart)
        return EXIT_OK
    lst = cfg.listen
    out.line(f"node '{cfg.name}' configured at {cfg_path} (preset {cfg.policy.preset})")
    if lst.enabled:
        scheme = "https" if lst.tls.cert else "http"
        out.line(f"listen: {scheme}://{lst.host}:{lst.port}")
    if cfg.relay.enabled:
        out.line(f"relay: {cfg.relay.hub_url} (role {cfg.relay.role})")
    out.line(f"tokens: {paths['tokens'] / 'this-pc.token'} (agent), {paths['tokens'] / 'this-pc-owner.token'} (owner)")
    out.line("client.json connections: this-pc (agent), this-pc-owner (owner)")
    if cfg.network.proxy:
        out.line(f"proxy: {_redact_url(cfg.network.proxy)} (child processes and downloads use it)")
    elif not args.mirror:
        out.line("tip: in mainland China add --mirror cn (Hugging Face mirror + pip mirror) and --proxy if needed")
    if cfg.policy.preset != "cautious":
        out.line("tip: if agents will browse the web, consider --preset cautious (approve risky calls once or "
                 "--for 30m)")
    snippet = {"mcpServers": {"hive": {"command": _console_command()[0],
                                        "args": _console_command()[1:] + ["mcp", "--local"]}}}
    out.line("MCP config for an agent on this computer:")
    out.line(json.dumps(snippet, indent=2))
    out.line("next: hive node run   (foreground)   or   hive service install node   (start at logon)")
    return EXIT_OK


def _redact_url(u: str) -> str:
    try:
        from .audit import redact_text
        return redact_text(u)
    except Exception:  # noqa: BLE001
        return u


def cmd_init_hub(args: argparse.Namespace, out: Out) -> int:
    """``hive init hub``."""
    from . import config
    if config.default_paths()["hub_config"].exists() and not args.force:
        out.error("conflict", "hub config exists", "use --force to rewrite it (new tokens)")
        return EXIT_USAGE
    _ask_consent(args, "hub")
    public_url = args.public_url
    if args.funnel and not public_url:
        try:
            from .httpserver import tailscale_info
            _ip, names, _note = tailscale_info()
            if names:
                public_url = "https://" + names[0].rstrip(".")
        except HiveError:
            pass
    try:
        cfg, _toks = config.init_hub(name=args.name, listen=args.listen or "127.0.0.1:7700", public_url=public_url,
                                     tls_cert=args.tls_cert, tls_key=args.tls_key, tailscale=args.tailscale,
                                     funnel=args.funnel, local_node=args.with_local_node, consent_by=_consent_by(),
                                     force=args.force)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    paths = config.default_paths()
    out.line(f"hub '{cfg.name}' configured at {paths['hub_config']}")
    out.line(f"owner token: {paths['tokens'] / 'hub-owner.token'} (client.json connection hub-local)")
    if args.funnel:
        out.line("expose it to pods with: tailscale funnel --bg 7709")
    if args.with_local_node:
        out.line("warning: an exec-capable agent on this machine can act as the hub (all nodes, approvals, "
                 "auto-update pods); give agents here --cautious tokens")
    out.line("next: hive hub run")
    return EXIT_OK


def cmd_init_client(args: argparse.Namespace, out: Out) -> int:
    """``hive init client``."""
    from . import config
    tok = _read_token_arg(args, out)
    token_file = None
    if args.token_file and not args.token_env:
        token_file = str(Path(os.path.expanduser(args.token_file)).resolve())
        tok = None
    try:
        cfg = config.init_client(url=args.url, token=tok, token_file=token_file, tls_pin=args.tls_pin,
                                 name=args.name or "default", make_default=True if args.default else None)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    out.line(f"connection '{args.name or 'default'}' saved" + (" (default)" if cfg.default == (args.name or "default")
                                                                 else ""))
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# node

def cmd_node_run(args: argparse.Namespace, out: Out) -> int:
    """``hive node run``."""
    from .node import run_node
    return run_node(args.config, relaunched=args.relaunched, supervise=args.supervise)


def cmd_node_status(args: argparse.Namespace, out: Out) -> int:
    """``hive node status`` (HTTP via this-pc-owner; file-based when the daemon is down)."""
    client = None
    try:
        client = _connect(args, owner_default=True)
        info = client.call("node.info", {})
        jobs: Any = None
        svcs: Any = None
        try:
            jobs = client.call("job.list", {"state": "running"})
        except HiveError:
            pass
        try:
            svcs = client.call("service.list", {})
        except HiveError:
            pass
        if out.as_json:
            out.data({"running": True, "info": info, "jobs": jobs, "services": svcs})
            return EXIT_OK
        out.foreign(f"node '{info.get('name')}' running (pid {info.get('pid')}, version {info.get('version')}, "
                    f"paused: {'yes' if info.get('paused') else 'no'})")
        modes = info.get("modes") or {}
        out.foreign(f"listen: {modes.get('listen') or 'off'}  relay: {modes.get('relay') or 'off'}")
        pol = info.get("policy") or {}
        out.foreign(f"preset: {pol.get('preset')}  started: {util.iso(util.parse_iso(info['started_at']))}"
                    if info.get("started_at") else f"preset: {pol.get('preset')}")
        if isinstance(jobs, dict):
            rows = [[j.get("job_id"), j.get("name"), j.get("state"), j.get("command")] for j in jobs.get("jobs") or []]
            out.line(f"running jobs: {len(rows)}")
            if rows:
                out.line(_table(rows, ["JOB_ID", "NAME", "STATE", "COMMAND"]))
        if isinstance(svcs, dict):
            rows = [[s.get("name"), s.get("state"), s.get("health"), s.get("pid"), s.get("port")]
                    for s in svcs.get("services") or []]
            out.line(f"services: {len(rows)}")
            if rows:
                out.line(_table(rows, ["NAME", "STATE", "HEALTH", "PID", "PORT"]))
        return EXIT_OK
    except HiveError as e:
        if e.code not in ("unavailable", "not_found"):
            out.error(e.code, e.message, e.hint)
            return exit_code_for(e)
    finally:
        if client is not None:
            client.close()
    cfg = _node_config(args)
    d = _data_dir(cfg)
    holder = _holder(d)
    st: Dict[str, Any] = {"running": holder is not None, "holder": holder, "paused": (d / "PAUSED").exists(),
                          "relay_disabled": (d / "relay.disabled").exists(), "jobs": [], "services": []}
    jd = d / "state" / "jobs"
    if jd.is_dir():
        for p in sorted(jd.iterdir()):
            rec = util.read_json(p / "job.json", None) if (p / "job.json").exists() else None
            if isinstance(rec, dict) and rec.get("state") in ("starting", "running"):
                st["jobs"].append({"job_id": rec.get("job_id"), "name": rec.get("name"), "state": rec.get("state")})
    raw = util.read_json(d / "state" / "services.json", {}) or {}
    for name in sorted((raw.get("services") or {}) if isinstance(raw, dict) else {}):
        st["services"].append(name)
    if out.as_json:
        out.data(st)
        return EXIT_OK
    if holder is not None:
        out.line(f"node '{cfg.name}' holds its lock (pid {holder.get('pid')}) but does not answer on this-pc-owner")
    else:
        out.line(f"node '{cfg.name}' is not running (start it: hive node run, or hive service install node)")
    out.line(f"paused: {'yes' if st['paused'] else 'no'}  relay disabled: {'yes' if st['relay_disabled'] else 'no'}")
    out.line(f"jobs marked running: {len(st['jobs'])}  defined services: {', '.join(st['services']) or 'none'}")
    return EXIT_OK


def _write_control(d: Path, cmd: str) -> None:
    util.write_json_atomic(d / "control.json", {"cmd": cmd, "at": util.iso_utc()}, private=True)


def cmd_node_stop(args: argparse.Namespace, out: Out) -> int:
    """``hive node stop`` (control.json "stop"; the daemon exits 0 and stays stopped, D55)."""
    cfg = _node_config(args)
    d = _data_dir(cfg)
    holder = _holder(d)
    if holder is None:
        out.line("node is not running")
        return EXIT_OK
    _write_control(d, "stop")
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if not util.pid_alive(int(holder.get("pid") or 0), holder.get("ctime")):
            out.line("node stopped (jobs and services keep running)")
            return EXIT_OK
        time.sleep(0.2)
    out.error("timeout", "node still running after 30 s", "check hive node status; it stops after in-flight calls")
    return EXIT_UNAVAILABLE


def cmd_node_restart(args: argparse.Namespace, out: Out) -> int:
    """``hive node restart`` (install.restart, §13.1)."""
    from . import install
    try:
        install.restart("node")
    except HiveError as e:
        out.error(e.code, e.message, e.hint or "stop it with hive node stop and start it again with hive node run")
        return exit_code_for(e)
    out.line("node restarted")
    if os.name == "nt":
        out.line("note: quit MCP clients first when upgrading (their running hive.exe is locked)")
    return EXIT_OK


def _wsl_lines(cfg: Any) -> List[str]:
    if os.name != "nt":
        return []
    return [f"this PC also runs a WSL node; stop it too: wsl -d {e.get('distro')} -- ~/.venvs/hive/bin/hive node "
            f"panic --yes (or the hive path you installed there)" for e in cfg.wsl.keepalive or []
            if isinstance(e, dict) and e.get("distro")]


def cmd_node_pause(args: argparse.Namespace, out: Out) -> int:
    """``hive node pause``."""
    from . import node
    cfg = _node_config(args)
    node.pause(_data_dir(cfg))
    out.line("paused: non-read calls are refused and nothing is spawned; resume with: hive node resume")
    out.line(HUB_REVOKE_LINE)
    for line in _wsl_lines(cfg):
        out.line(line)
    return EXIT_OK


def cmd_node_resume(args: argparse.Namespace, out: Out) -> int:
    """``hive node resume [--no-relay]``."""
    from . import node
    cfg = _node_config(args)
    node.resume(_data_dir(cfg), relay=not args.no_relay)
    out.line("resumed (nothing was restarted)")
    return EXIT_OK


def cmd_node_panic(args: argparse.Namespace, out: Out) -> int:
    """``hive node panic [--yes]``."""
    from . import node
    cfg = _node_config(args)
    if not _confirm("Stop every job, service and container of this node and pause it?", yes=args.yes):
        out.line("cancelled (pass --yes to skip the question)")
        return EXIT_USAGE
    res = node.panic(cfg, _data_dir(cfg))
    if out.as_json:
        out.data(res)
    else:
        out.line(f"paused; relay disabled; stopped jobs: {len(res.get('jobs') or [])}, services: "
                 f"{len(res.get('services') or [])}, containers: {len(res.get('containers') or [])}")
        for e in res.get("errors") or []:
            out.foreign(f"error: {e}", err=True)
        out.line(HUB_REVOKE_LINE)
        for line in _wsl_lines(cfg):
            out.line(line)
    return EXIT_OK


def _split(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


def cmd_node_token_add(args: argparse.Namespace, out: Out) -> int:
    """``hive node token add NAME …``."""
    from . import config
    from .auth import CAUTIOUS_ACTIONS, CLASSES
    cfg = _node_config(args)
    name = args.name.lower()
    if not util.valid_name(name):
        raise CliError(f"invalid token name {args.name!r}", hint="lowercase letters, digits, . _ - (max 64)")
    if any(t.name == name for t in cfg.auth.tokens):
        raise CliError(f"a token named {name!r} exists", hint=f"revoke it first: hive node token revoke {name}")
    if args.role and args.classes:
        raise CliError("give --role or --classes, not both")
    entry: Dict[str, Any] = {"name": name, "kind": args.kind}
    if args.classes:
        cl = _split(args.classes)
        bad = [c for c in cl if c not in CLASSES]
        if bad:
            raise CliError(f"unknown class(es): {', '.join(bad)}", hint="classes: " + ", ".join(CLASSES))
        entry["classes"] = cl
    else:
        entry["role"] = args.role or "operator"
    if args.deny:
        entry["deny_actions"] = _split(args.deny)
    if args.expires:
        try:
            util.parse_iso(args.expires)
        except HiveError:
            raise CliError(f"invalid --expires {args.expires!r}", hint="YYYY-MM-DD")
        entry["expires"] = args.expires
    cautious_default = False
    if args.kind == "agent":
        if args.approval:
            entry["require_approval"] = _split(args.approval)
        elif args.cautious or (cfg.policy.preset == "cautious" and not args.no_cautious):
            entry["require_approval"] = list(CAUTIOUS_ACTIONS)
            cautious_default = not args.cautious
    elif args.approval or args.cautious:
        raise CliError("approvals apply to --kind agent tokens only")
    token = None
    if args.sha256:
        h = args.sha256.strip().lower()
        if not h.startswith("sha256:") or len(h) != 71 or any(c not in "0123456789abcdef" for c in h[7:]):
            raise CliError("--sha256 must be sha256:<64 hex>")
        entry["sha256"] = h
    else:
        token = util.new_token()
        entry["sha256"] = util.hash_token(token)

    def fn(raw: dict) -> None:
        toks = raw.setdefault("auth", {}).setdefault("tokens", [])
        if any(isinstance(t, dict) and t.get("name") == name for t in toks):
            raise conflict(f"a token named {name!r} exists")
        toks.append(entry)
    try:
        config.edit_config(Path(cfg._path), fn, kind="node")
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    if cautious_default:
        out.line("cautious node: this token's shell/service/delete calls wait for approval; --no-cautious to opt out")
    if token:
        out.line(f"token for {name} (shown once; store it in a token file, never in agent configs):")
        out.line(token)
    else:
        out.line(f"token {name} added (hash only)")
    _restart_daemon("node", out, no_restart=args.no_restart)
    return EXIT_OK


def cmd_node_token_list(args: argparse.Namespace, out: Out) -> int:
    """``hive node token list``."""
    cfg = _node_config(args)
    rows = []
    for t in cfg.auth.tokens:
        rows.append([t.name, t.kind, ",".join(t.classes) if t.classes is not None else (t.role or "-"),
                     "cautious" if t.require_approval else "-", t.expires or "-", t.note or ""])
    if out.as_json:
        out.data([{"name": r[0], "kind": r[1], "access": r[2], "approval": r[3], "expires": r[4], "note": r[5]}
                  for r in rows])
    else:
        out.line(_table(rows, ["NAME", "KIND", "ROLE/CLASSES", "APPROVAL", "EXPIRES", "NOTE"]))
    return EXIT_OK


def cmd_node_token_revoke(args: argparse.Namespace, out: Out) -> int:
    """``hive node token revoke NAME``."""
    from . import config
    cfg = _node_config(args)
    name = args.name.lower()
    tok = [t for t in cfg.auth.tokens if t.name == name]
    if not tok:
        out.error("not_found", f"no token named {name!r}", "hive node token list")
        return EXIT_ACTION
    if tok[0].kind == "owner" and sum(1 for t in cfg.auth.tokens if t.kind == "owner") == 1:
        out.error("conflict", "this is the only owner token (the local CLI needs it)", "add another owner first")
        return EXIT_ACTION

    def fn(raw: dict) -> None:
        toks = raw.setdefault("auth", {}).setdefault("tokens", [])
        toks[:] = [t for t in toks if not (isinstance(t, dict) and t.get("name") == name)]
    config.edit_config(Path(cfg._path), fn, kind="node")
    out.line(f"token {name} revoked")
    _restart_daemon("node", out, no_restart=args.no_restart)
    return EXIT_OK


def _secret_store(cfg: Any) -> Any:
    from .secretstore import SecretStore
    return SecretStore(_data_dir(cfg) / "secrets.json", passthrough=cfg.secrets.env_passthrough, env={})


def _read_secret_value(args: argparse.Namespace) -> str:
    if args.from_env:
        v = os.environ.get(args.from_env)
        if not v:
            raise CliError(f"environment variable {args.from_env} is empty or not set")
        return v
    if sys.stdin is not None and not sys.stdin.isatty():
        line = sys.stdin.readline()
        v = line.rstrip("\r\n")
        if not v:
            raise CliError("no value on stdin")
        return v
    a = getpass.getpass(f"value for {args.name}: ")
    b = getpass.getpass("again: ")
    if a != b:
        raise CliError("the two values differ")
    return a


def cmd_node_secret_set(args: argparse.Namespace, out: Out) -> int:
    """``hive node secret set NAME`` (value from --from-env, stdin or a prompt; never argv)."""
    from .secretstore import DEFAULT_SECRET_HOSTS
    if args.value:
        raise CliError("secret values are never accepted on the command line (visible in ps and shell history)",
                       hint=f"echo VALUE | hive node secret set {args.name}   or   --from-env VAR   or the prompt")
    if args.hosts and args.any_host:
        raise CliError("give --hosts or --any-host, not both")
    cfg = _node_config(args)
    value = _read_secret_value(args)
    hosts = ["*"] if args.any_host else args.hosts
    st = _secret_store(cfg)
    try:
        st.set(args.name, value, hosts=hosts, exec_ok=not args.no_exec)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    row = [r for r in st.names() if r.get("name") == args.name]
    hl = ", ".join(row[0].get("hosts") or []) if row else ""
    out.line(f"secret {args.name} set (hosts: {hl}; exec: {'no' if args.no_exec else 'yes'}); the running node "
             f"uses it at its next call")
    if args.name not in DEFAULT_SECRET_HOSTS and not args.hosts and not args.any_host:
        out.line(f"note: {args.name} is only sent to loopback; bind hosts with --hosts h1,h2 (replace) or "
                 f"--hosts +host (add)")
    return EXIT_OK


def cmd_node_secret_list(args: argparse.Namespace, out: Out) -> int:
    """``hive node secret list``."""
    cfg = _node_config(args)
    rows = _secret_store(cfg).names()
    if out.as_json:
        out.data(rows)
        return EXIT_OK
    out.line(_table([[r.get("name"), r.get("source"), ",".join(r.get("hosts") or []),
                      "yes" if r.get("exec") else "no", r.get("set_at") or "-"] for r in rows],
                    ["NAME", "SOURCE", "HOSTS", "EXEC", "SET_AT"]))
    return EXIT_OK


def cmd_node_secret_unset(args: argparse.Namespace, out: Out) -> int:
    """``hive node secret unset NAME``."""
    cfg = _node_config(args)
    if _secret_store(cfg).unset(args.name):
        out.line(f"secret {args.name} removed")
        return EXIT_OK
    out.error("not_found", f"no stored secret {args.name!r}", "hive node secret list")
    return EXIT_ACTION


# ---------------------------------------------------------------------------------------------------------------
# call / sh / nodes / actions / tools / mcp

def _parse_value(v: str) -> Any:
    try:
        return json.loads(v)
    except ValueError:
        return v


def cmd_call(args: argparse.Namespace, out: Out) -> int:
    """``hive call ACTION [key=value …] [--json '{…}'] [--node N] [--timeout S] [--approval ID] [--raw]``."""
    params: Dict[str, Any] = {}
    if args.params_json:
        try:
            p = json.loads(args.params_json)
        except ValueError as e:
            raise CliError(f"--json is not valid JSON: {e}")
        if not isinstance(p, dict):
            raise CliError("--json must be a JSON object")
        params.update(p)
    for kv in args.kv or []:
        if "=" not in kv:
            raise CliError(f"expected key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        params[k.strip()] = _parse_value(v)
    client = _connect(args, owner_default=True)
    try:
        res = client.call(args.action, params, node=args.node, timeout=args.timeout, approval_id=args.approval)
    finally:
        client.close()
    if args.raw or out.as_json:
        print(json.dumps(res, ensure_ascii=True, default=str))
    else:
        out.data(res)
    return EXIT_OK


def _split_sh_argv(argv: List[str]) -> Tuple[List[str], List[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def cmd_sh(argv: List[str], gl: argparse.Namespace) -> int:
    """``hive sh NODE -- COMMAND… [--shell S] [--cwd D] [--timeout S]`` (exit = remote exit code)."""
    head, cmd = _split_sh_argv(argv)
    ap = argparse.ArgumentParser(prog="hive sh")
    ap.add_argument("node")
    ap.add_argument("words", nargs="*")
    ap.add_argument("--shell")
    ap.add_argument("--cwd")
    ap.add_argument("--timeout", type=float)
    ap.add_argument("--connection")
    try:
        a = ap.parse_args(head)
    except SystemExit as e:
        return SH_FAILURE if e.code else 0
    words = list(a.words) + cmd
    if not words:
        sys.stderr.write("hive sh: no command (hive sh NODE -- COMMAND…)\n")
        return SH_FAILURE
    if a.connection:
        gl.connection = a.connection
    # the direct node itself: an explicit "self", so a connection's default_node is not sent to it (note 86)
    node = "self" if a.node in (".", "-", "local", "self") else a.node
    params: Dict[str, Any] = {"command": words[0] if len(words) == 1 else " ".join(words), "merge_stderr": False}
    if a.shell:
        params["shell"] = a.shell
    if a.cwd:
        params["cwd"] = a.cwd
    if a.timeout:
        params["timeout"] = a.timeout
    client = None
    try:
        client = _connect(gl, owner_default=True)
        res = client.call("shell.run", params, node=node)
    except HiveError as e:
        Out(False).error(e.code, e.message, e.hint)
        return SH_TIMEOUT if e.code == "timeout" else SH_FAILURE
    finally:
        if client is not None:
            client.close()
    out = Out(False)
    if res.get("stdout"):
        out.foreign(res["stdout"])
    elif res.get("output"):
        out.foreign(res["output"])
    if res.get("stderr"):
        out.foreign(res["stderr"], err=True)
    state = res.get("state")
    if state == "running":
        sys.stderr.write(util.escape_terminal(f"hive sh: still running as job {res.get('job_id')} "
                                              f"(follow: hive logs {a.node} {res.get('job_id')} -f)") + "\n")
        return SH_TIMEOUT
    if state == "timeout":
        sys.stderr.write("hive sh: timeout\n")
        return SH_TIMEOUT
    code = res.get("exit_code")
    if state == "exited" and isinstance(code, int) and not isinstance(code, bool):
        return code & 0xFF if code >= 0 else SH_FAILURE
    sys.stderr.write(util.escape_terminal(f"hive sh: command ended with state {state} "
                                          f"(signal {res.get('signal')})") + "\n")
    return SH_FAILURE


def cmd_nodes(args: argparse.Namespace, out: Out) -> int:
    """``hive nodes``."""
    client = _connect(args)
    try:
        rows = client.nodes()
    finally:
        client.close()
    if out.as_json:
        out.data(rows)
        return EXIT_OK
    out.line(_table([[r.get("name"), "online" if r.get("online") else "offline", r.get("mode"), r.get("os_label"),
                      r.get("gpus_summary"), ",".join(r.get("tags") or [])] for r in rows],
                    ["NAME", "STATE", "MODE", "OS", "GPUS", "TAGS"]))
    return EXIT_OK


def cmd_actions(args: argparse.Namespace, out: Out) -> int:
    """``hive actions``."""
    client = _connect(args)
    try:
        rows = client.actions(args.node)
    finally:
        client.close()
    if out.as_json:
        out.data(rows)
        return EXIT_OK
    out.line(_table([[r.get("name"), r.get("risk"), "yes" if r.get("approval") else "-", r.get("title")]
                     for r in rows], ["ACTION", "RISK", "APPROVAL", "TITLE"]))
    return EXIT_OK


def cmd_tools_export(args: argparse.Namespace, out: Out) -> int:
    """``hive tools export --format F`` (filtered through a reachable connection, else the static catalog)."""
    from .tools import Toolkit
    client = None
    if args.connection or args.node:
        client = _connect(args, default_node=args.node)
    try:
        tk = Toolkit(client, profile=args.profile, prefix=args.prefix or "", default_node=args.node)
        data = tk.export(args.format)
    finally:
        if client is not None:
            client.close()
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
        out.line(f"{len(data)} tools written to {args.output}")
    else:
        print(text)
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# parser

def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--connection", default=argparse.SUPPRESS, help="client.json connection")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)


def _jsonflag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS,
                   help="machine output (errors as JSON on stderr)")


def build_parser() -> argparse.ArgumentParser:
    """The ``hive`` argument parser (WP6a commands)."""
    ap = argparse.ArgumentParser(prog="hive", description="Hive (蜂巢): let AI agents operate your machines.")
    ap.add_argument("--connection", help="client.json connection")
    ap.add_argument("--json", dest="as_json", action="store_true", help="machine output")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", "-V", action="store_true", help="print the version")
    sub = ap.add_subparsers(dest="cmd")

    def leaf(parent: Any, name: str, fn: Any, *, json_flag: bool = True, **kw: Any) -> argparse.ArgumentParser:
        p = parent.add_parser(name, **kw)
        _common(p)
        if json_flag:
            _jsonflag(p)
        p.set_defaults(fn=fn)
        return p

    leaf(sub, "version", lambda a, o: (print(f"hive {__version__}"), EXIT_OK)[1], help="print the version")

    init = sub.add_parser("init", help="create configs").add_subparsers(dest="what")
    p = leaf(init, "node", cmd_init_node, help="configure this computer as a node")
    p.add_argument("--name")
    p.add_argument("--preset", choices=["readonly", "standard", "cautious", "full"],
                   help="policy preset for a new config (default standard)")
    p.add_argument("--root", action="append", help="extra fs root PATH[:ro] (repeatable)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--listen", help="HOST:PORT")
    g.add_argument("--tailscale", action="store_true", help="listen on this machine's tailnet address")
    g.add_argument("--no-listen", action="store_true")
    p.add_argument("--lan", action="store_true", help="listen on all interfaces (needs TLS or --insecure)")
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    p.add_argument("--insecure", action="store_true")
    p.add_argument("--relay", help="hub URL (relay mode)")
    p.add_argument("--token-env")
    p.add_argument("--token-file")
    p.add_argument("--token")
    p.add_argument("--tls-pin")
    p.add_argument("--join", help="join code")
    p.add_argument("--relay-role", choices=["readonly", "files", "operator"],
                   help="what the hub may do here (default operator); on an existing config it changes relay.role")
    p.add_argument("--relay-proxy")
    p.add_argument("--pod", action="store_true")
    p.add_argument("--proxy", help="URL or off")
    p.add_argument("--mirror", choices=["cn", "none"])
    p.add_argument("--models-dir")
    p.add_argument("--wsl", action="store_true")
    p.add_argument("--wsl-keepalive", action="append", metavar="DISTRO")
    p.add_argument("--config")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-restart", action="store_true")
    p = leaf(init, "hub", cmd_init_hub, help="configure this computer as the hub")
    p.add_argument("--name")
    p.add_argument("--listen")
    p.add_argument("--tailscale", action="store_true")
    p.add_argument("--funnel", action="store_true")
    p.add_argument("--public-url")
    p.add_argument("--tls-cert")
    p.add_argument("--tls-key")
    p.add_argument("--with-local-node", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--force", action="store_true")
    p = leaf(init, "client", cmd_init_client, help="add a connection to client.json")
    p.add_argument("--url")
    p.add_argument("--token-file")
    p.add_argument("--token-env")
    p.add_argument("--token")
    p.add_argument("--tls-pin")
    p.add_argument("--name")
    p.add_argument("--default", action="store_true")

    node = sub.add_parser("node", help="this computer's node").add_subparsers(dest="what")
    p = leaf(node, "run", cmd_node_run, help="run the node in the foreground")
    p.add_argument("--config")
    p.add_argument("--relaunched", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    for name, fn in (("status", cmd_node_status), ("stop", cmd_node_stop), ("restart", cmd_node_restart),
                     ("pause", cmd_node_pause)):
        p = leaf(node, name, fn)
        p.add_argument("--config")
    p = leaf(node, "resume", cmd_node_resume)
    p.add_argument("--config")
    p.add_argument("--no-relay", action="store_true")
    p = leaf(node, "panic", cmd_node_panic)
    p.add_argument("--config")
    p.add_argument("--yes", action="store_true")
    tok = node.add_parser("token", help="node tokens").add_subparsers(dest="what2")
    p = leaf(tok, "add", cmd_node_token_add)
    p.add_argument("name")
    p.add_argument("--role", choices=["readonly", "files", "operator"])
    p.add_argument("--classes")
    p.add_argument("--kind", choices=["agent", "hub"], default="agent")
    p.add_argument("--deny")
    p.add_argument("--approval")
    p.add_argument("--cautious", action="store_true")
    p.add_argument("--no-cautious", action="store_true")
    p.add_argument("--expires")
    p.add_argument("--sha256")
    p.add_argument("--config")
    p.add_argument("--no-restart", action="store_true")
    p = leaf(tok, "list", cmd_node_token_list)
    p.add_argument("--config")
    p = leaf(tok, "revoke", cmd_node_token_revoke)
    p.add_argument("name")
    p.add_argument("--config")
    p.add_argument("--no-restart", action="store_true")
    sec = node.add_parser("secret", help="node secrets").add_subparsers(dest="what2")
    p = leaf(sec, "set", cmd_node_secret_set)
    p.add_argument("name")
    p.add_argument("value", nargs="?", help=argparse.SUPPRESS)
    p.add_argument("--hosts", action="append")
    p.add_argument("--any-host", action="store_true")
    p.add_argument("--no-exec", action="store_true")
    p.add_argument("--from-env")
    p.add_argument("--config")
    p = leaf(sec, "list", cmd_node_secret_list)
    p.add_argument("--config")
    p = leaf(sec, "unset", cmd_node_secret_unset)
    p.add_argument("name")
    p.add_argument("--config")

    p = leaf(sub, "call", cmd_call, json_flag=False, help="call one action")
    p.add_argument("action")
    p.add_argument("kv", nargs="*", metavar="key=value")
    p.add_argument("--json", dest="params_json", help="params as a JSON object")
    p.add_argument("--node")
    p.add_argument("--timeout", type=float)
    p.add_argument("--approval")
    p.add_argument("--raw", action="store_true", help="print the result as compact ASCII JSON")
    p = leaf(sub, "nodes", cmd_nodes)
    p = leaf(sub, "actions", cmd_actions)
    p.add_argument("--node")
    tools = sub.add_parser("tools", help="tool definitions").add_subparsers(dest="what")
    p = leaf(tools, "export", cmd_tools_export, json_flag=False)
    p.add_argument("--format", required=True, choices=["mcp", "anthropic", "openai", "openai-responses"])
    p.add_argument("--profile", choices=["minimal", "standard", "full"], default="standard")
    p.add_argument("--prefix")
    p.add_argument("--node")
    p.add_argument("-o", "--output")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (``hive = hive.cli:main``)."""
    util.prune_cwd_path()
    tolerant_streams()
    args_list = list(sys.argv[1:] if argv is None else argv)
    if args_list[:1] in (["--version"], ["-V"]):
        print(f"hive {__version__}")
        return 0
    # global flags before the command: --connection C, --json, -v
    gl = argparse.Namespace(connection=None, as_json=False, verbose=False)
    i = 0
    while i < len(args_list) and args_list[i].startswith("-"):
        a = args_list[i]
        if a == "--connection" and i + 1 < len(args_list):
            gl.connection = args_list[i + 1]
            i += 2
            continue
        if a.startswith("--connection="):
            gl.connection = a.split("=", 1)[1]
        elif a == "--json":
            gl.as_json = True
        elif a in ("-v", "--verbose"):
            gl.verbose = True
        else:
            break
        i += 1
    rest = args_list[i:]
    cmd = rest[0] if rest else ""
    if cmd == "mcp":
        from .mcp import run_stdio
        return run_stdio(rest[1:])
    if cmd == "sh":
        return cmd_sh(rest[1:], gl)
    if cmd in ("_shim", "_download", "_update"):
        sys.stderr.write(f"hive: {cmd} is internal; run it as python -m hive {cmd}\n")
        return EXIT_USAGE
    if cmd in WP6B:
        sys.stderr.write(f"hive {cmd}: not available yet in this build (WP6b)\n")
        return EXIT_USAGE
    ap = build_parser()
    try:
        args = ap.parse_args(rest)
    except SystemExit as e:
        return int(e.code or 0) if isinstance(e.code, int) else EXIT_USAGE
    for k in ("connection", "as_json", "verbose"):
        if not hasattr(args, k) or getattr(args, k) in (None, False):
            setattr(args, k, getattr(gl, k))
    if getattr(args, "verbose", False):
        import logging
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    out = Out(bool(getattr(args, "as_json", False)))
    fn = getattr(args, "fn", None)
    if fn is None:
        if getattr(args, "version", False):
            print(f"hive {__version__}")
            return EXIT_OK
        ap.print_help(sys.stderr)
        return EXIT_USAGE
    try:
        return int(fn(args, out))
    except CliError as e:
        out.error("usage" if e.code == EXIT_USAGE else "error", e.message, e.hint)
        return e.code
    except HiveError as e:
        out.error(e.code, e.message, e.hint, e.details if out.as_json else None)
        return exit_code_for(e)
    except KeyboardInterrupt:
        sys.stderr.write("interrupted\n")
        return 130


__all__ = ["main", "build_parser", "exit_code_for", "Out", "CliError"]
