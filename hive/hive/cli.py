"""argparse CLI ``hive`` (design §19, WP6).

``init node|hub|client``, ``join``, ``node …`` (run/status/stop/restart/pause/resume/panic/token/secret), ``hub …``
(run/stop/restart/status/list/add-node/add-agent/remove-node/remove-agent/rotate-token/ack-node/restart-node),
``gateway token|url``, ``approvals …``, ``mcp`` (stdio), ``mcp-config``, ``tools export``, ``nodes``, ``actions``,
``call``, ``sh``, ``push``/``pull``, ``logs``, ``service install|uninstall|status``, ``tls-pin``, ``doctor``,
``audit tail``, ``dist build``, ``version``.

Exit codes (§19): 0 ok; 1 action error; 2 usage/config error; 3 auth error (unauthorized/forbidden/policy_denied);
4 unavailable/timeout. ``hive sh`` exits with the remote exit code (0–255), 125 for Hive-level failures, 124 for
timeouts and for a call that yielded to a job. Errors print ``code: message`` and ``Hint: …`` on stderr (JSON with
``--json``). Every agent- or node-supplied text printed here goes through ``util.escape_terminal``.
This module is one of the two places allowed to print and to use ``util.iso()`` (§22.3 rules 5 and 11).
"""
from __future__ import annotations

import argparse
import fnmatch
import getpass
import json
import os
import re
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, util
from .errors import HiveError, conflict

EXIT_OK, EXIT_ACTION, EXIT_USAGE, EXIT_AUTH, EXIT_UNAVAILABLE = 0, 1, 2, 3, 4
SH_FAILURE, SH_TIMEOUT = 125, 124
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
        if code == "not_found" and str(message).startswith("this is node '"):
            hint = _via_hub_hint(hint)
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


def _via_hub_hint(hint: Optional[str]) -> Optional[str]:
    """v8.1 (external review round 2): a node name that is not the local node, on a machine whose client.json has a
    hub connection: say how to reach that node through the hub."""
    try:
        from .config import load_client_config
        conns = list(load_client_config().connections)
    except Exception:  # noqa: BLE001 - only a hint
        return hint
    hubs = _hub_connections(conns)
    if not hubs:
        return hint
    return (hint + "; " if hint else "") + f"other nodes are reached through the hub: --connection {hubs[0]}"


def _conn_flag(args: Any) -> str:
    """`` --connection NAME`` when the command was given one (printed follow-up commands keep it), else ''."""
    c = getattr(args, "connection", None)
    return f" --connection {c}" if c else ""


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

LOCAL_CONNECTIONS = ("this-pc", "this-pc-owner")


def _hub_connections(names: Sequence[str]) -> List[str]:
    """client.json connections that reach a hub: ``hub-local`` first, then names containing "hub"."""
    return [c for c in names if c == "hub-local"] + sorted(c for c in names if c != "hub-local" and "hub" in c.lower())


def _local_node_name() -> Optional[str]:
    """This computer's node name (from its config file), None without a readable node config."""
    from .config import default_paths
    try:
        raw = json.loads(Path(default_paths()["node_config"]).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    name = raw.get("name") if isinstance(raw, dict) else None
    return name.lower() if isinstance(name, str) and name else None


def _auto_hub_connection(args: argparse.Namespace, node: Optional[str], *, owner_default: bool) -> Optional[str]:
    """Final acceptance B2: ``--node X`` (or a NODE argument) without ``--connection``, where the connection in use
    would be this computer's own node (``this-pc``/``this-pc-owner``) and X is another node: the hub connection of
    client.json (``hub-local`` first, then a client.json default that is not this computer's node, then names with
    "hub"), else None. Never with HIVE_URL / HIVE_CONNECTION set."""
    if not node or getattr(args, "connection", None):
        return None
    if os.environ.get("HIVE_URL", "").strip() or os.environ.get("HIVE_CONNECTION", "").strip():
        return None
    n = str(node).strip().lower()
    if not n or n in (".", "-", "local", "self"):
        return None
    try:
        from .config import load_client_config
        cc = load_client_config()
    except (HiveError, OSError, ValueError):
        return None
    current = "this-pc-owner" if owner_default and "this-pc-owner" in cc.connections else cc.default
    if current not in LOCAL_CONNECTIONS:
        return None
    hubs = _hub_connections(list(cc.connections))
    if cc.default and cc.default not in LOCAL_CONNECTIONS and cc.default in cc.connections:
        hubs = [h for h in hubs if h == "hub-local"] + [cc.default] + [h for h in hubs if h not in ("hub-local",
                                                                                                  cc.default)]
    local = _local_node_name()
    if not hubs or local is None or n == local:
        return None
    return hubs[0]


def _connect(args: argparse.Namespace, *, owner_default: bool = False, default_node: Optional[str] = None,
             node: Optional[str] = None) -> Any:
    """Client for --connection / env / ``this-pc-owner`` (CLI admin default, §14.4) / client.json default.

    ``node``: the node the command targets; another node than this computer's own is reached through the hub
    connection of client.json when no connection was chosen (``_auto_hub_connection``, said on stderr)."""
    from .client import connect
    from .config import load_client_config
    hub = _auto_hub_connection(args, node or default_node, owner_default=owner_default)
    if hub:
        sys.stderr.write(util.escape_terminal(
            f"note: '{node or default_node}' is not this computer's node ('{_local_node_name()}'): using the hub "
            f"connection '{hub}' (pass --connection to choose another)") + "\n")
        args.connection = hub
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


def _restart_daemon(kind: str, out: Out, *, no_restart: bool = False) -> bool:
    """Config changes apply through a graceful daemon restart (§8.6); prints what happened (v8.1 wording: "applied
    (restarted)", or "will apply on next start" when nothing runs). True when a running daemon was restarted."""
    from .config import default_paths
    d = default_paths()["node_data" if kind == "node" else "hub_data"]
    if util.FileLock(d / f"{kind}.lock").holder() is None:
        sup = _supervisor(kind)
        if sup:
            # final acceptance B5f: a watchdog / service loop starts it again within seconds with the new config
            out.line(f"the {kind} is not running right now; applied by the {sup}, which starts it again within a "
                     f"few seconds (check: hive {kind} status)")
        else:
            out.line(f"the {kind} is not running: will apply on next start")
        return False
    if no_restart:
        out.line(f"restart to apply: hive {kind} restart")
        return False
    try:
        from . import install
        install.restart(kind)
        out.line(f"applied (restarted the {kind})")
        return True
    except HiveError as e:
        if e.code == "unavailable" and "could not start" in e.message:
            out.line(f"not applied: {e.message}" + (f"; {e.hint}" if e.hint else ""))
        elif e.code == "unavailable" and "foreground" in e.message:
            out.line(f"the {kind} stopped to apply the change (it ran in the foreground without a terminal); "
                     f"start it again: hive {kind} run")
        else:
            out.line(f"not applied yet: {e.message}; run hive {kind} restart" + (f" ({e.hint})" if e.hint else ""))
        return False


def _supervisor(kind: str) -> Optional[str]:
    """install.supervisor: what restarts a stopped daemon by itself now (watchdog, launcher, systemd), else None."""
    try:
        from . import install
        return install.supervisor(kind)
    except Exception:  # noqa: BLE001 - only changes a message
        return None


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

def _hidden_input(prompt: str) -> str:
    """A prompt without echo; Ctrl-D / Ctrl-C end it cleanly (a CliError, nothing saved) instead of a traceback."""
    try:
        return getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt):
        sys.stderr.write("\n")
        raise CliError("cancelled; nothing was saved")


def _token_from_stdin(what: str = "token") -> str:
    """``--token -``: one line from a pipe, or a prompt without echo in a terminal (v8.1)."""
    if sys.stdin is not None and not sys.stdin.isatty():
        try:
            v = sys.stdin.readline().strip()
        except (OSError, ValueError, KeyboardInterrupt):
            v = ""
        if not v:
            raise CliError(f"no {what} on stdin")
    else:
        v = _hidden_input(f"paste the {what} (not shown), then Enter: ").strip()
        if not v:
            raise CliError(f"no {what} given; nothing was saved")
    if any(c.isspace() for c in v):
        raise CliError(f"that does not look like a {what} (it contains spaces)")
    return v


def _read_token_arg(args: argparse.Namespace, out: Out) -> Optional[str]:
    if getattr(args, "token", None) == "-":
        return _token_from_stdin()
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


def _root_warning(out: Out, cfg_path: Path, *, pod: bool = False) -> None:
    """v8.1: say at init (not first at ``node run``) that a node refuses to run as root/Administrator."""
    from . import config
    if pod or not config.is_privileged():
        return
    allowed = False
    if cfg_path.exists():
        raw = util.read_json(cfg_path, default=None)
        allowed = isinstance(raw, dict) and raw.get("allow_root") is True
    if allowed:
        return
    who = "Administrator" if os.name == "nt" else "root"
    out.line(f"warning: you are running as {who}. The node will refuse to start as {who} (an agent would control "
             "the whole machine). Use your normal user account instead.")
    out.line(f"  Only on a machine where {who} is the sole user (a cloud GPU container) set \"allow_root\": true "
             f"in {cfg_path} (hive join / init node --pod do this for pods).")


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
    _root_warning(out, cfg_path, pod=bool(args.pod))
    if args.tailscale or args.listen == "tailscale":
        _tailscale_check(out, "node")
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
            consent_by=_consent_by(), force=args.force, auto_update=args.auto_update)
    except HiveError as e:
        if exists and e.code == "bad_request" and e.message.startswith("config exists"):
            # v8.1 (external review round 2): a bare `hive init node` on a configured node is not an error
            out.line(f"already set up at {cfg_path}; nothing to change")
            out.line("  to change a setting pass it (e.g. --listen, --mirror cn, --root); --force rewrites the "
                     "config with new tokens")
            return EXIT_OK
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    if not toks:
        changed = getattr(cfg, "_changed", [])
        out.line(f"updated {cfg_path}: " + (", ".join(changed) if changed else "no changes"))
        notes = list(getattr(cfg, "_notes", None) or [])
        for n in notes:
            out.line(n)
        _restart_daemon("node", out, no_restart=args.no_restart)
        if any(n.startswith("hub config:") for n in notes):
            _restart_daemon("hub", out, no_restart=args.no_restart)
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
    local_server: Dict[str, Any] = {"command": _console_command()[0], "args": _console_command()[1:] + ["mcp", "--local"]}
    if _mcp_env():
        local_server["env"] = _mcp_env()
    snippet = {"mcpServers": {"hive": local_server}}
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


def _tailscale_check(out: Out, what: str) -> None:
    """v8.1: ``--tailscale`` at init warns at once when Tailscale is not usable here (not a hard failure: it may be
    installed or logged in before the first ``run``)."""
    try:
        from .httpserver import tailscale_info
        tailscale_info()
    except HiveError as e:
        out.line(f"warning: {e.message}; the {what} will not start until Tailscale works here")
        out.line("  install it from https://tailscale.com/download and log in (tailscale up); check with: "
                 "tailscale ip -4")
    except Exception:  # noqa: BLE001 - a check only
        pass


def _apply_local_node(cfg: Any, out: Out) -> None:
    """After ``--with-local-node``: say which name the local node got and make a running node load the new hub token
    (restart via control.json; a foreground terminal run re-execs itself, v8.1)."""
    name = getattr(cfg, "_local_node", None)
    if not name:
        return
    url = next((getattr(n, "url", None) for n in getattr(cfg, "nodes", []) or [] if getattr(n, "name", None) == name),
               None) or "127.0.0.1"
    out.line(f"local node registered with the hub as '{name}' ({url}; a hub token was added to the node config)")
    if name == "hubhost":
        out.line("  (named 'hubhost' because the node and the hub share this computer's name)")
    _restart_daemon("node", out)


def cmd_init_hub(args: argparse.Namespace, out: Out) -> int:
    """``hive init hub`` (an existing config: change only the given settings, v8.1)."""
    from . import config
    paths = config.default_paths()
    exists = paths["hub_config"].exists() and not args.force
    if args.tailscale:
        _tailscale_check(out, "hub")
    public_url = args.public_url
    if args.funnel and not public_url:
        try:
            from .httpserver import tailscale_info
            _ip, names, _note = tailscale_info()
            if names:
                public_url = "https://" + names[0].rstrip(".")
        except HiveError:
            pass
    if exists:
        ignored = [f for f, v in (("--name", bool(args.name)), ("--tls-cert/--tls-key", bool(args.tls_cert or
                                                                                          args.tls_key))) if v]
        if ignored:
            out.line(f"note: {', '.join(ignored)} applies only when creating a config; use --force to rewrite "
                     "(new tokens)")
        try:
            cfg = config.edit_hub_init(listen=args.listen, tailscale=args.tailscale, public_url=public_url,
                                       funnel=args.funnel, local_node=args.with_local_node)
        except HiveError as e:
            if e.code == "bad_request" and e.message.startswith("hub config exists"):
                out.line(f"already set up at {paths['hub_config']}; nothing to change")
                out.line("  to change a setting pass it (e.g. --listen, --tailscale, --with-local-node); --force "
                         "rewrites the config with new tokens")
                return EXIT_OK
            out.error(e.code, e.message, e.hint)
            return EXIT_USAGE
        changed = getattr(cfg, "_changed", [])
        out.line(f"updated {paths['hub_config']}: " + (", ".join(changed) if changed else "no changes")
                 + " (tokens unchanged)")
        for n in getattr(cfg, "_notes", None) or []:
            out.line(n)
        if args.funnel:
            out.line("expose it to pods with: tailscale funnel --bg 7709")
        _apply_local_node(cfg, out)
        _restart_daemon("hub", out)
        return EXIT_OK
    _ask_consent(args, "hub")
    try:
        cfg, _toks = config.init_hub(name=args.name, listen=args.listen or "127.0.0.1:7700", public_url=public_url,
                                     tls_cert=args.tls_cert, tls_key=args.tls_key, tailscale=args.tailscale,
                                     funnel=args.funnel, local_node=args.with_local_node, consent_by=_consent_by(),
                                     force=args.force)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    out.line(f"hub '{cfg.name}' configured at {paths['hub_config']}")
    out.line(f"owner token: {paths['tokens'] / 'hub-owner.token'} (client.json connection hub-local)")
    if args.funnel:
        out.line("expose it to pods with: tailscale funnel --bg 7709")
    if args.with_local_node:
        _apply_local_node(cfg, out)
        out.line("warning: an exec-capable agent on this machine can act as the hub (all nodes, approvals, "
                 "auto-update pods); make the agent token here cautious: hive node token set this-pc --cautious")
    if args.force and util.FileLock(paths["hub_data"] / "hub.lock").holder() is not None:
        _restart_daemon("hub", out)
    else:
        out.line("next: hive hub run   (foreground)   or   hive service install hub   (start at logon)")
    return EXIT_OK


def _tighten_token_file(path: Path, out: Out) -> None:
    """A token file readable by others is made 0600 (POSIX, our own file) instead of only warning later."""
    if os.name == "nt":
        return
    try:
        st = path.stat()
        if st.st_mode & 0o077 and st.st_uid == os.getuid():
            os.chmod(str(path), 0o600)
            out.line(f"note: {path} was readable by other users; set to 0600")
    except OSError:
        pass


CLIENT_TOKEN_RE = re.compile(r"^hv_[A-Za-z0-9_-]{32,256}$")


def cmd_init_client(args: argparse.Namespace, out: Out) -> int:
    """``hive init client`` (``--token -``: from stdin or a hidden prompt, saved 0600 by Hive itself, v8.1)."""
    from . import config
    name = args.name or "default"
    stdin_token = getattr(args, "token", None) == "-"
    tok = _read_token_arg(args, out)
    if tok is not None:
        tok = tok.strip()
        if not CLIENT_TOKEN_RE.match(tok):
            # v8.1 (external review round 2): a partial or mistaken paste is caught here, not later as "invalid token"
            shown = (tok[:6] + "…") if len(tok) > 6 else tok
            raise CliError(f"that does not look like a Hive token ({shown!r}, {len(tok)} characters); nothing was "
                           "saved", hint="copy the whole hv_… line the hub or node printed (about 46 characters)")
    token_file = None
    if args.token_file and not args.token_env:
        tf = Path(os.path.expanduser(args.token_file)).resolve()
        token_file = str(tf)
        if stdin_token and tok:
            try:
                config._write_token(tf, tok)                # 0600 file (private dir created when missing)
            except (OSError, HiveError) as e:
                raise CliError(f"cannot write token file {tf}: {e}")
            out.line(f"token saved to {tf} (only you can read it)")
        else:
            _tighten_token_file(tf, out)
        tok = None
    try:
        cfg = config.init_client(url=args.url, token=tok, token_file=token_file, tls_pin=args.tls_pin,
                                 name=name, make_default=True if args.default else None)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    if tok:
        out.line(f"token saved to {config.default_paths()['tokens'] / (name + '.token')} (only you can read it)")
    out.line(f"connection '{name}' saved" + (" (default)" if cfg.default == name else ""))
    out.line(f"next: hive nodes --connection {name}   then   hive mcp-config --client claude-desktop --connection "
             f"{name} --write")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# node

_MAIN_ARGV: Optional[List[str]] = None   # the arguments main() was called with (a foreground re-exec repeats them)


def _relaunch_prefix() -> List[str]:
    """The program this process was started as: the pyz, the ``hive`` console script (sys.argv[0]), else
    ``python -m hive`` (also for ``python -m hive``, whose argv[0] is ``__main__.py``)."""
    zp = util._zipapp_path()
    if zp is not None:
        return [sys.executable, str(zp)]
    a0 = sys.argv[0] if sys.argv else ""
    if a0 and os.path.basename(a0) != "__main__.py" and os.path.isfile(a0):
        a0 = os.path.abspath(a0)
        if os.name != "nt" and os.access(a0, os.X_OK):
            return [a0]
        if a0.lower().endswith(".exe"):
            return [a0]
        return [sys.executable, a0]
    return util.self_command()


def _foreground_run(kind: str, args: argparse.Namespace, run: Any) -> int:
    """v8.1: a hand-started ``hive node run`` / ``hive hub run`` in a terminal survives requested restarts (exit 75
    after a config change, ``hive … restart``, an upgrade): it re-execs the same command instead of ending.
    Supervised runs (service manager, watchdog, launcher) keep exit 75 for their supervisor."""
    from . import node as nodemod
    cwd = os.getcwd()
    code = run(args.config, relaunched=args.relaunched, supervise=args.supervise)
    if code != nodemod.EXIT_RESTART or not nodemod.foreground_restart_ok(relaunched=args.relaunched,
                                                                         supervise=args.supervise):
        return code
    argv = list(_MAIN_ARGV) if _MAIN_ARGV is not None else list(sys.argv[1:])
    if not argv:
        argv = [kind, "run"]
    return nodemod.reexec_foreground(_relaunch_prefix() + argv, cwd)


def cmd_node_run(args: argparse.Namespace, out: Out) -> int:
    """``hive node run``."""
    from .node import run_node
    return _foreground_run("node", args, run_node)


def cmd_node_status(args: argparse.Namespace, out: Out) -> int:
    """``hive node status`` (HTTP via this-pc-owner; file-based when the daemon is down or, v8.1 external review
    round 2, does not listen: no this-pc-owner connection, never the client default)."""
    client = None
    try:
        if not _local_owner_conn(args):
            raise HiveError("unavailable", "no local connection")
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
    if holder is not None and not cfg.listen.enabled:
        out.line(f"node '{cfg.name}' is running (pid {holder.get('pid')}); it does not listen, so this is read from "
                 "its state files")
    elif holder is not None:
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
    d = _data_dir(cfg)
    nonce = node.pause(d)
    if _holder(d) is None:
        out.line("paused (the node is not running; it starts paused): non-read calls are refused and nothing is "
                 "spawned; resume with: hive node resume")
    elif _wait_paused(args, PAUSE_CONFIRM_S, data_dir=d, paused=True, nonce=nonce):
        out.line("paused (confirmed by the running node): non-read calls are refused and nothing is spawned; resume "
                 "with: hive node resume")
    else:
        out.line("paused: the running node did not confirm within 3 s (it applies PAUSED within about 1 s of "
                 "reading it); check with: hive node status. Resume with: hive node resume")
    out.line(HUB_REVOKE_LINE)
    for line in _wsl_lines(cfg):
        out.line(line)
    return EXIT_OK


PAUSE_CONFIRM_S = 3.0     # v8.1: node pause/resume wait this long for the running node to apply it


def _local_owner_conn(args: argparse.Namespace) -> bool:
    """An HTTP route to THIS node: --connection/HIVE_URL/HIVE_CONNECTION given, or client.json has this-pc-owner
    (removed when the node stops listening), never the client default, which may be another node or a hub."""
    if getattr(args, "connection", None) or os.environ.get("HIVE_URL") or os.environ.get("HIVE_CONNECTION"):
        return True
    try:
        from .config import load_client_config
        return "this-pc-owner" in load_client_config().connections
    except Exception:  # noqa: BLE001
        return False


def _wait_paused(args: argparse.Namespace, budget: float, *, data_dir: Optional[Path] = None, paused: bool = True,
                 nonce: Optional[str] = None) -> bool:
    """v8.1: ``hive node pause``/``resume`` return once the running node has applied it, at most ``budget``: its
    ``paused.ack`` (works without a listener, external review round 2) or, over this-pc-owner, ``node.info``."""
    from . import node
    end = time.monotonic() + budget
    client = None
    try:
        if _local_owner_conn(args):
            try:
                client = _connect(args, owner_default=True)
            except HiveError:
                client = None
        while True:
            if data_dir is not None and node.pause_acked(data_dir, paused=paused, nonce=nonce):
                return True
            if client is not None:
                try:
                    if bool(client.call("node.info", {}, timeout=2).get("paused")) == paused:
                        return True
                except HiveError:
                    pass
            if time.monotonic() >= end:
                return False
            time.sleep(0.2)
    finally:
        if client is not None:
            client.close()


def cmd_node_resume(args: argparse.Namespace, out: Out) -> int:
    """``hive node resume [--no-relay]`` (v8.1, external review round 2: waits ≤ 3 s until the running node has
    applied it, so the next call is not refused as paused)."""
    from . import node
    cfg = _node_config(args)
    d = _data_dir(cfg)
    node.resume(d, relay=not args.no_relay)
    if _holder(d) is not None and not _wait_paused(args, PAUSE_CONFIRM_S, data_dir=d, paused=False):
        out.line("resumed: the running node did not confirm within 3 s (it applies it within about 1 s); nothing "
                 "was restarted")
        return EXIT_OK
    out.line("resumed (nothing was restarted)")
    return EXIT_OK


def cmd_node_panic(args: argparse.Namespace, out: Out) -> int:
    """``hive node panic [--yes]``."""
    from . import node
    cfg = _node_config(args)
    if not args.yes and (not sys.stdin or not sys.stdin.isatty()):
        out.line("cancelled: not a terminal, so there is no y/N question; run: hive node panic --yes")
        return EXIT_USAGE
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


def _cautious_flag(args: argparse.Namespace) -> bool:
    """--cautious (True) or --no-cautious (False): exactly one of them."""
    if bool(args.cautious) == bool(args.no_cautious):
        raise CliError("give --cautious or --no-cautious")
    return bool(args.cautious)


def cmd_node_token_set(args: argparse.Namespace, out: Out) -> int:
    """``hive node token set NAME --cautious|--no-cautious`` (final acceptance B3): flips the token's approval rule;
    the token value stays the same, so no agent config changes."""
    from . import config
    from .auth import CAUTIOUS_ACTIONS
    cautious = _cautious_flag(args)
    cfg = _node_config(args)
    name = args.name.lower()
    tok = [t for t in cfg.auth.tokens if t.name == name]
    if not tok:
        out.error("not_found", f"no token named {name!r}", "hive node token list")
        return EXIT_ACTION
    if tok[0].kind != "agent":
        raise CliError(f"token {name!r} is a {tok[0].kind} token; approvals apply to agent tokens only")

    def fn(raw: dict) -> None:
        for t in raw.setdefault("auth", {}).setdefault("tokens", []):
            if isinstance(t, dict) and t.get("name") == name:
                if cautious:
                    t["require_approval"] = list(CAUTIOUS_ACTIONS)
                else:
                    t.pop("require_approval", None)
    config.edit_config(Path(cfg._path), fn, kind="node")
    if cautious:
        out.line(f"token {name}: cautious (its shell/service/delete calls wait for your approval: hive approvals "
                 "list); the token itself is unchanged")
    else:
        out.line(f"token {name}: no approvals needed any more; the token itself is unchanged")
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
    a = _hidden_input(f"value for {args.name}: ")
    b = _hidden_input("again: ")
    if a != b:
        raise CliError("the two values differ; nothing was saved")
    if not a:
        raise CliError("empty value; nothing was saved")
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
    client = _connect(args, owner_default=True, node=args.node)
    try:
        res = client.call(args.action, params, node=args.node, timeout=args.timeout, approval_id=args.approval)
    except HiveError as e:
        out.error(e.code, e.message, e.hint, e.details if out.as_json else None)
        _approval_retry_hint(e)
        return exit_code_for(e)
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


def _approval_retry_hint(e: HiveError) -> None:
    """After approval_required: how to retry the very same call once it is approved (v8.1)."""
    if e.code != "approval_required":
        return
    aid = (e.details or {}).get("approval_id") if isinstance(e.details, dict) else None
    if aid:
        sys.stderr.write(util.escape_terminal(
            f"then run the same command again with --approval {aid} (same arguments; e.g. hive sh NODE "
            f"--approval {aid} -- COMMAND)") + "\n")


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
    ap.add_argument("--approval", metavar="AP_ID", help="retry an approved call (the ap_… id it printed)")
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
        client = _connect(gl, owner_default=True, node=node)
        res = client.call("shell.run", params, node=node, approval_id=a.approval)
    except HiveError as e:
        Out(False).error(e.code, e.message, e.hint)
        _approval_retry_hint(e)
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


def _node_state(row: dict) -> str:
    """STATE column: online / checking (the hub's first check of a direct node still runs, B5e) / offline."""
    if row.get("online"):
        return "online"
    return "checking" if row.get("checking") else "offline"


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
    out.line(_table([[r.get("name"), _node_state(r), r.get("mode"), r.get("os_label"),
                      r.get("gpus_summary"), ",".join(r.get("tags") or [])] for r in rows],
                    ["NAME", "STATE", "MODE", "OS", "GPUS", "TAGS"]))
    for r in rows:                         # FA-C3: say why a running direct node shows offline
        note = _hub_token_note(r)
        if note:
            out.line(f"note: {r.get('name')}: {note}")
    hint = _hub_local_hint(args, len(rows))
    if hint:
        out.line(hint)
    return EXIT_OK


def _hub_local_hint(args: argparse.Namespace, n_rows: int) -> Optional[str]:
    """v8.1: ``hive nodes`` on the hub's machine without --connection talks to the local node only; say how to see
    every node through the hub."""
    if getattr(args, "connection", None) or os.environ.get("HIVE_CONNECTION") or os.environ.get("HIVE_URL") \
            or n_rows > 1:
        return None
    try:
        from .config import load_client_config
        cc = load_client_config()
    except (HiveError, OSError, ValueError):
        return None
    if "hub-local" not in cc.connections or cc.default == "hub-local":
        return None
    return (f"hint: this is only this computer (connection '{cc.default}'); every node through the hub on this "
            "machine: hive nodes --connection hub-local")


def cmd_actions(args: argparse.Namespace, out: Out) -> int:
    """``hive actions``."""
    client = _connect(args, node=args.node)
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
# WP6b: join

def _join_code(args: argparse.Namespace) -> str:
    code = args.code
    if code == "-":
        code = (sys.stdin.readline() if sys.stdin is not None else "").strip()
    elif not code:
        code = (os.environ.get("HIVE_JOIN") or "").strip()
    if not code:
        raise CliError("no join code", hint="HIVE_JOIN='hvj1.…' hive join   (or: echo CODE | hive join -)")
    if args.code and args.code != "-":
        sys.stderr.write("warning: a join code on the command line is visible in ps and shell history; prefer "
                         "HIVE_JOIN='…' hive join\n")
    return code


def _detect_pod(args: argparse.Namespace) -> bool:
    if args.no_pod:
        return False
    if args.pod or os.environ.get("HIVE_TOPOLOGY") == "pod":
        return True
    try:
        from . import sysinfo
        return sysinfo.cloud_info().get("provider") in ("runpod", "vast", "autodl")
    except Exception:  # noqa: BLE001
        return False


def _join_hub_check(jd: dict, relay_proxy: Optional[str]) -> Optional[str]:
    """Reach the hub before the relay token is ever sent (system CAs or the join-code pin, §20.4); returns a warning
    text when it cannot be reached (the config is still written: the node retries)."""
    from types import SimpleNamespace
    from . import relay
    from .config import _relay_insecure_ok
    url = str(jd.get("u") or "")
    try:
        cfg = SimpleNamespace(hub_url=url, tls_pin=jd.get("p"), ca_file=None, proxy=relay_proxy or "auto",
                              allow_insecure=_relay_insecure_ok(url))
        tr = relay.relay_transport(cfg, token=None, timeout=15)
        try:
            tr.request("GET", "/v1/health")
        finally:
            tr.close()
    except HiveError as e:
        if e.code == "policy_denied" or (e.details or {}).get("reason") == "tls_pin":
            raise
        return f"{e.code}: {e.message}"
    return None


def cmd_join(args: argparse.Namespace, out: Out) -> int:
    """``hive join [CODE | -] …`` (= init node --join CODE [--pod] [--wsl] + optional service install, §19)."""
    from . import config
    from .dist import decode_join
    code = _join_code(args)
    try:
        jd = decode_join(code)
    except HiveError as e:
        raise CliError(f"invalid join code: {e.message}", hint=e.hint)
    paths = config.default_paths()
    cfg_path = paths["node_config"]
    exists = cfg_path.exists() and not args.force
    pod = _detect_pod(args)
    relay_proxy = args.relay_proxy or os.environ.get("HIVE_RELAY_PROXY") or None
    proxy = args.proxy if args.proxy is not None else (os.environ.get("HIVE_PROXY") or None)
    if not exists and not args.yes and sys.stdin is not None and sys.stdin.isatty():
        _ask_consent(args, "node")
    warn = _join_hub_check(jd, relay_proxy)
    if warn:
        out.line(f"warning: the hub at {jd.get('u')} is not reachable now ({warn}); the node keeps retrying")
    if exists:
        ignored = [f for f, v in (("--preset", args.preset is not None), ("--wsl", args.wsl), ("--pod", args.pod),
                                  ("--mirror", args.mirror is not None), ("--proxy", args.proxy is not None),
                                  ("--models-dir", args.models_dir is not None)) if v]
        if ignored:
            out.line(f"note: {', '.join(ignored)} applies only when creating a config; use --force to rewrite")
    try:
        cfg, toks = config.init_node(
            name=(args.name.lower() if args.name else None) if not exists else None,
            preset=args.preset or "standard", topology="pod" if (pod and not exists) else None,
            relay_role=args.relay_role if exists else (args.relay_role or "operator"), relay_proxy=relay_proxy,
            join=code, proxy=None if exists else proxy, mirror=None if exists else args.mirror,
            models_dir=None if exists else args.models_dir, wsl=bool(args.wsl) and not exists,
            consent_by="join", force=args.force)
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    if toks:
        hf = os.environ.get("HIVE_HF_ENDPOINT")

        def fix(raw: dict) -> None:
            raw["consent"] = {"by": "join", "at": util.iso_utc(), "text": "consent given by join code"}
            if hf:
                raw.setdefault("models", {})["hf_endpoint"] = hf
        config.edit_config(cfg_path, fix, kind="node")
        cfg = config.load_node_config(str(cfg_path))
        out.line(f"node '{cfg.name}' joined {cfg.relay.hub_url} (config {cfg_path})")
    else:
        out.line(f"relay settings of node '{cfg.name}' updated in {cfg_path} (listen, roots, proxy and tokens kept)")
    out.line(f"the hub may do on this machine what role {cfg.relay.role} allows"
             + ("; --relay-role files to narrow" if cfg.relay.role == "operator" else ""))
    if pod and not exists:
        out.line("pod topology applied (data under /workspace survives pod restarts)")
    if exists:
        _restart_daemon("node", out, no_restart=args.no_restart)
    if args.install:
        from . import install
        logs = _node_log_files(cfg)
        marks = {p: _file_size(p) for p in logs}
        try:
            res = install.install("node", cfg_path, method="watchdog" if pod else "auto")
        except HiveError as e:
            out.error(e.code, e.message, e.hint)
            return exit_code_for(e)
        _print_install(res, out)
        if not res.commands and any("already running" in n for n in res.notes):
            return EXIT_OK
        return _verify_joined(cfg, marks, out)
    elif toks:
        out.line("next: hive node run   (foreground)   or   hive service install node")
    return EXIT_OK


JOIN_VERIFY_S = 30.0      # v8.1: join --install waits this long for "relay registered" in the node log
JOIN_FAIL_GRACE_S = 8.0   # … but stops this long after a start failure (a crash loop) was logged
_START_FAIL = re.compile(r"(address already in use|address in use|only one usage of each socket address|"
                         r"node failed to start|node failed:|refusing to run)", re.I)


def _node_log_files(cfg: Any) -> List[Path]:
    d = _data_dir(cfg)
    f = Path(str((getattr(cfg, "log", None) or {}).get("file") or "logs/node.log"))
    return [f if f.is_absolute() else d / f, d / "logs" / "stdout.log"]


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _read_new(p: Path, start: int) -> str:
    try:
        with open(str(p), "rb") as fh:
            if fh.seek(0, 2) < start:
                start = 0                       # rotated
            fh.seek(start)
            return fh.read(1 << 20).decode("utf-8", "replace")
    except OSError:
        return ""


def _verify_joined(cfg: Any, marks: Dict[Path, int], out: Out) -> int:
    """v8.1: after ``join --install``, success is printed only once the node logs that it registered with the hub
    (≤ JOIN_VERIFY_S); a crash loop (e.g. its listen port is taken) is reported with the fix."""
    out.line(f"waiting up to {JOIN_VERIFY_S:g} s for the node to start and register with the hub…")
    end = time.monotonic() + JOIN_VERIFY_S
    fail = ""
    failed_at: Optional[float] = None
    while True:
        text = "".join(_read_new(p, n) for p, n in marks.items())
        if "relay registered with" in text:
            out.line(f"joined: node '{cfg.name}' is running and registered with {cfg.relay.hub_url}")
            return EXIT_OK
        for line in text.splitlines():
            if _START_FAIL.search(line):
                fail = line.strip()
                failed_at = failed_at or time.monotonic()
        now = time.monotonic()
        if now >= end or (failed_at is not None and now - failed_at > JOIN_FAIL_GRACE_S):
            break
        time.sleep(0.5)
    if fail:
        out.error("unavailable", f"the node did not start: {fail[-300:]}", _start_fail_hint(cfg, fail))
        out.line(f"note: the installed background service keeps retrying every few seconds and picks up the fix by "
                 f"itself; to stop it instead: {_hive_cmd()} service uninstall node")
    elif _holder(_data_dir(cfg)) is not None:
        out.error("unavailable", f"the node runs but has not registered with {cfg.relay.hub_url} within "
                                 f"{JOIN_VERIFY_S:g} s", "it keeps retrying in the background; check that this "
                                                         "machine reaches the hub URL, then: hive node status; the "
                                                         f"log is {_node_log_files(cfg)[0]}")
    else:
        out.error("unavailable", f"the node is not running {JOIN_VERIFY_S:g} s after install",
                  f"see {_node_log_files(cfg)[0]} and {_node_log_files(cfg)[1]}; start it by hand to see the error: "
                  "hive node run")
    return EXIT_UNAVAILABLE


def _hive_cmd() -> str:
    """The command the user runs Hive with here (v8.1, external review round 2): the pod wrapper, ``python3
    <pyz>``, ``hive`` or ``python3 -m hive``, with ``HIVE_HOME=…`` in front when it is set and not the pod wrapper."""
    import shlex
    from .config import default_paths
    try:
        wrapper = default_paths()["home"] / "hive"
        if wrapper.is_file() and os.access(str(wrapper), os.X_OK) and os.name != "nt":
            return shlex.quote(str(wrapper))
    except Exception:  # noqa: BLE001
        pass
    zp = util._zipapp_path()
    if zp is not None:
        base = f"python3 {shlex.quote(str(zp))}"
    elif _which_not_cwd("hive"):
        base = "hive"
    else:
        base = "python3 -m hive"
    hh = os.environ.get("HIVE_HOME")
    return (f"HIVE_HOME={shlex.quote(hh)} " if hh and os.name != "nt" else "") + base


def _start_fail_hint(cfg: Any, line: str) -> str:
    low = line.lower()
    hc = _hive_cmd()
    if "in use" in low or "only one usage" in low:
        lst = cfg.listen
        return (f"its local port {lst.host}:{lst.port} is taken (another Hive node on this machine?). A relay node "
                f"needs no listener: {hc} init node --no-listen   or give it its own port: {hc} init node --listen "
                "127.0.0.1:7721   (the running watchdog restarts it with the new setting)")
    if "refusing to run" in low:
        return "do not run Hive as root; in a container where root is the only user set allow_root: true"
    return f"see {_node_log_files(cfg)[0]}"


# ---------------------------------------------------------------------------------------------------------------
# WP6b: hub

def _hub_config_path(args: argparse.Namespace) -> Path:
    from .config import default_paths
    return Path(os.path.expanduser(getattr(args, "config", None) or str(default_paths()["hub_config"])))


def _hub_config(args: argparse.Namespace) -> Any:
    from .config import load_hub_config
    path = _hub_config_path(args)
    if not path.exists():
        raise CliError(f"no hub config at {path}", hint="run: hive init hub")
    try:
        return load_hub_config(str(path))
    except HiveError as e:
        raise CliError(f"{e.code}: {e.message}", hint=e.hint)


def _hub_edit(args: argparse.Namespace, fn: Any) -> None:
    from . import config
    config.edit_config(_hub_config_path(args), fn, kind="hub")


def _hub_client(args: argparse.Namespace) -> Any:
    """Owner client of the hub on this machine (``hub-local``, §14.4) unless --connection names another."""
    from .client import connect
    return connect(getattr(args, "connection", None) or "hub-local")


def _owner_request(client: Any, method: str, path: str, body: Optional[dict] = None) -> Any:
    tr = getattr(client.target, "transport", None)
    if tr is None:
        raise CliError("this connection cannot reach owner routes")
    return tr.request(method, path, body)


def _hub_holder(cfg: Any) -> Optional[dict]:
    return util.FileLock(Path(cfg.data_dir) / "hub.lock").holder()


def _restart_hub(args: argparse.Namespace, out: Out) -> None:
    _restart_daemon("hub", out, no_restart=getattr(args, "no_restart", False))


def cmd_hub_run(args: argparse.Namespace, out: Out) -> int:
    """``hive hub run``."""
    from .hub import run_hub
    return _foreground_run("hub", args, run_hub)


def cmd_hub_stop(args: argparse.Namespace, out: Out) -> int:
    """``hive hub stop`` (control.json "stop": exit 0, stays stopped, D55)."""
    cfg = _hub_config(args)
    d = Path(cfg.data_dir)
    holder = _hub_holder(cfg)
    if holder is None:
        out.line("hub is not running")
        return EXIT_OK
    _write_control(d, "stop")
    end = time.monotonic() + 30
    while time.monotonic() < end:
        if not util.pid_alive(int(holder.get("pid") or 0), holder.get("ctime")):
            out.line("hub stopped")
            return EXIT_OK
        time.sleep(0.2)
    out.error("timeout", "hub still running after 30 s", "check hive hub status")
    return EXIT_UNAVAILABLE


def cmd_hub_restart(args: argparse.Namespace, out: Out) -> int:
    """``hive hub restart`` (install.restart("hub"), §8.6)."""
    from . import install
    try:
        install.restart("hub")
    except HiveError as e:
        out.error(e.code, e.message, e.hint or "stop it with hive hub stop and start it again with hive hub run")
        return exit_code_for(e)
    out.line("hub restarted")
    return EXIT_OK


def _hub_token_note(r: dict, hub: Optional[str] = None) -> str:
    """FA-C3: NOTE for a direct node that refuses the hub token ("" otherwise)."""
    if not r.get("hub_token_rejected") or r.get("online"):
        return ""
    return f"hub token not accepted: run the printed 'hive node token add hub-{hub or '…'}' on that machine"


def _node_notes(r: dict, hub: Optional[str] = None) -> str:
    notes = []
    tok = _hub_token_note(r, hub)
    if tok:
        notes.append(tok)
    if r.get("identity_changed"):
        notes.append(f"IDENTITY CHANGED (check, then: hive hub ack-node {r.get('name')})")
    if r.get("outdated"):
        notes.append("outdated" + (" (auto-update)" if r.get("auto_update") else " (upgrade by hand)"))
    return "; ".join(notes)


def cmd_hub_status(args: argparse.Namespace, out: Out) -> int:
    """``hive hub status`` (via hub-local; marks outdated nodes and identity_changed)."""
    client = None
    try:
        client = _hub_client(args)
        who = client.whoami()
        rows = client.nodes()
    except HiveError as e:
        if e.code != "unavailable":
            raise
        cfg = _hub_config(args)
        h = _hub_holder(cfg)
        if out.as_json:
            out.data({"running": h is not None, "holder": h})
        elif h is not None:
            out.line(f"hub '{cfg.name}' holds its lock (pid {h.get('pid')}) but does not answer on hub-local")
        else:
            out.line(f"hub '{cfg.name}' is not running (start it: hive hub run, or hive service install hub)")
        return EXIT_OK
    finally:
        if client is not None:
            client.close()
    if out.as_json:
        out.data({"running": True, "server": who.get("server"), "nodes": rows})
        return EXIT_OK
    srv = who.get("server") or {}
    out.foreign(f"hub '{srv.get('name')}' running (version {srv.get('version')}), {len(rows)} node(s)")
    if rows:
        out.line(_table([[r.get("name"), _node_state(r), r.get("mode"),
                          r.get("os_label"), r.get("version"), _node_notes(r, srv.get("name"))] for r in rows],
                        ["NAME", "STATE", "MODE", "OS", "VERSION", "NOTE"]))
    return EXIT_OK


def cmd_hub_list(args: argparse.Namespace, out: Out) -> int:
    """``hive hub list`` (configured nodes and tokens)."""
    cfg = _hub_config(args)
    nodes = [{"name": n.name, "mode": n.mode, "url": n.url, "tags": n.tags, "aliases": n.aliases,
              "description": n.description} for n in cfg.nodes]
    toks = [{"name": t.name, "kind": t.kind, "role": t.role, "classes": t.classes, "nodes": t.nodes,
             "models": t.models, "approval": bool(t.require_approval), "expires": t.expires} for t in cfg.tokens]
    if out.as_json:
        out.data({"nodes": nodes, "tokens": toks})
        return EXIT_OK
    out.line(f"nodes ({len(nodes)}):")
    if nodes:
        out.line(_table([[n["name"], n["mode"], n["url"] or "-", ",".join(n["tags"]), ",".join(n["aliases"])]
                         for n in nodes], ["NAME", "MODE", "URL", "TAGS", "ALIASES"]))
    out.line(f"tokens ({len(toks)}):")
    out.line(_table([[t["name"], t["kind"], ",".join(t["classes"]) if t["classes"] is not None else (t["role"] or "-"),
                      ",".join(t["models"]) if t["kind"] == "models" else ",".join(t["nodes"] or []),
                      "cautious" if t["approval"] else "-", t["expires"] or "-"] for t in toks],
                    ["NAME", "KIND", "ROLE/CLASSES", "NODES/MODELS", "APPROVAL", "EXPIRES"]))
    return EXIT_OK


def _valid_new_name(name: str, what: str) -> str:
    n = (name or "").lower()
    if not util.valid_name(n):
        raise CliError(f"invalid {what} name {name!r}", hint="lowercase letters, digits, . _ - (max 64)")
    return n


def _taken(cfg: Any, name: str) -> bool:
    return any(n.name == name or name in (n.aliases or []) for n in cfg.nodes)


def _pyz_sha() -> Optional[str]:
    try:
        from . import dist
        return dist.pyz_sha256(dist.build_pyz())
    except Exception:  # noqa: BLE001 - the printed block then names `hive dist build`
        return None


def _relay_block(cfg: Any, name: str, token: str, args: argparse.Namespace) -> List[str]:
    from . import dist
    info = dist.add_node_instructions(cfg, name, token, via=getattr(args, "via", None), pyz_sha256_hex=_pyz_sha(),
                                      host=getattr(args, "host", None))
    return list(info["lines"])


def _check_direct_url(url: str, pinned: bool, out: Out) -> None:
    """§14.3 / C1: a direct link over http:// only to loopback and tailnet/private addresses; https elsewhere
    (with a pin or CA: warned when missing). Raises CliError before anything is written."""
    from urllib.parse import urlsplit
    from .transport import plaintext_ok, resolve_host
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        port = u.port or (443 if u.scheme.lower() == "https" else 80)
    except ValueError as e:
        raise CliError(f"invalid --direct URL: {e}")
    if not host:
        raise CliError("--direct URL has no host", hint="e.g. http://desk.tail1234.ts.net:7701")
    ips = resolve_host(host, port)
    if u.scheme.lower() == "http":
        if not plaintext_ok(host, ips):
            raise CliError(f"{url}: plain http only to loopback, LAN or tailnet addresses (the hub token would "
                           f"cross the internet unencrypted)",
                           hint="use the node's tailnet/LAN address, or https with --tls-pin")
    elif not pinned and not plaintext_ok(host, ips):
        out.line("warning: https to a public address without --tls-pin relies on the system CAs (or "
                 "network.ca_file); pin the node's certificate: hive tls-pin CERT on the node")


def cmd_hub_add_node(args: argparse.Namespace, out: Out) -> int:
    """``hive hub add-node NAME --relay [--via] | --direct URL …``."""
    cfg = _hub_config(args)
    name = _valid_new_name(args.name, "node")
    if _taken(cfg, name):
        raise CliError(f"a node named {name!r} exists", hint=f"hive hub remove-node {name} first, or "
                                                            f"hive hub rotate-token {name}")
    if bool(args.relay) == bool(args.direct):
        raise CliError("give --relay or --direct URL")
    tags = _split(args.tags)
    entry: Dict[str, Any] = {"name": name, "tags": tags, "description": args.description or ""}
    lines: List[str] = []
    if args.relay:
        if any(getattr(args, k, None) for k in ("token", "token_file", "token_env", "generate", "tls_pin")):
            raise CliError("--token*/--generate/--tls-pin go with --direct")
        token = util.new_token()
        lines = _relay_block(cfg, name, token, args)        # before writing: a bad_request leaves no entry behind
        entry.update(mode="relay", token_sha256=util.hash_token(token))
    else:
        if args.via:
            raise CliError("--via goes with --relay")
        url = args.direct.rstrip("/")
        if not url.lower().startswith(("http://", "https://")):
            raise CliError("--direct needs an http(s):// URL", hint="e.g. http://desk.tail1234.ts.net:7701")
        _check_direct_url(url, bool(args.tls_pin), out)
        if args.generate:
            token = util.new_token()
            lines = [f"on the node '{name}', run (only the hash crosses machines):",
                     f"  hive node token add hub-{cfg.name} --kind hub --sha256 {util.hash_token(token)}"]
        else:
            token = _read_token_arg(args, out) or ""
            if not token:
                raise CliError("--direct needs the node's hub token", hint="--token-file F, --token-env VAR or "
                                                                           "--generate")
        entry.update(mode="direct", url=url, token=token)
        if args.tls_pin:
            entry["tls_pin"] = args.tls_pin

    def fn(raw: dict) -> None:
        ns = raw.setdefault("nodes", [])
        if any(isinstance(n, dict) and n.get("name") == name for n in ns):
            raise conflict(f"a node named {name!r} exists")
        ns.append(entry)
    _hub_edit(args, fn)
    out.line(f"node '{name}' added ({entry['mode']})")
    for line in lines:
        out.line(line)
    _restart_hub(args, out)
    return EXIT_OK


def _hub_urls(cfg: Any, *, here: bool = True) -> List[Tuple[str, str]]:
    """(label, base URL) of the hub for other machines and, ``here``, for this machine (§8.7 URLs)."""
    lst = cfg.listen
    scheme = "https" if lst.tls.cert else "http"
    port = int(lst.port)
    out: List[Tuple[str, str]] = []
    host = str(lst.host)
    if host == "tailscale":
        try:
            from .httpserver import tailscale_info
            ip, names, _note = tailscale_info()
        except Exception:  # noqa: BLE001
            ip, names = None, []
        if ip:
            out.append(("tailnet", f"{scheme}://{ip}:{port}"))
        for n in names[:1]:
            out.append(("magicdns", f"{scheme}://{n.rstrip('.')}:{port}"))
    elif host in ("0.0.0.0", "::", "*"):
        try:
            hn = socket.gethostname()
        except OSError:
            hn = ""
        if hn:
            out.append(("lan", f"{scheme}://{hn}:{port}"))
    elif not util.is_loopback_host(host):
        out.append(("listen", f"{scheme}://{'[' + host + ']' if ':' in host else host}:{port}"))
    if cfg.public_url:
        out.append(("public", str(cfg.public_url).rstrip("/")))
    if here:
        out.append(("local", f"{scheme}://127.0.0.1:{port}"))
    return out


def _hub_local_nodes(cfg: Any) -> List[str]:
    """Hub nodes on the hub's own machine (T5): loopback/own-tailnet/own-hostname direct URLs, `wsl-host:<hub host>`
    tags."""
    from urllib.parse import urlsplit
    host = socket.gethostname().lower()
    own: List[str] = []
    try:
        from .httpserver import tailscale_info
        own = [tailscale_info()[0]]
    except Exception:  # noqa: BLE001
        own = []
    out = []
    for n in cfg.nodes:
        h = ""
        try:
            h = (urlsplit(n.url or "").hostname or "").lower()
        except ValueError:
            h = ""
        local = bool(n.mode == "direct" and h and (util.is_loopback_host(h) or h in own or h == host or
                                                   h.startswith(host + ".")))
        if any(str(t).lower() == f"wsl-host:{host}" for t in (n.tags or [])):
            local = True
        if local:
            out.append(n.name)
    return out


def _role_has_exec(role: Optional[str], classes: Optional[List[str]] = None) -> bool:
    from .auth import ROLE_PRESETS
    if classes is not None:
        return "exec" in classes
    return "exec" in ROLE_PRESETS.get(role or "operator", set())


EXEC_ACTIONS = ("shell.run", "job.start", "service.define", "service.start", "service.restart")


def _exec_ungated(entry: Dict[str, Any]) -> bool:
    """T5 (§19 v5): the token can start commands without approval — it has the exec class and some exec action
    (``EXEC_ACTIONS``, the exec part of CAUTIOUS_ACTIONS) is neither denied nor approval-gated. Covers the §5.2
    ``auth.exec_gated`` rule (shell.run) and also job.start/service.*, which run commands too."""
    if not _role_has_exec(entry.get("role"), entry.get("classes")):
        return False
    deny = list(entry.get("deny_actions") or [])
    appr = list(entry.get("require_approval") or [])
    return any(not util.glob_match(a, deny) and not util.glob_match(a, appr) for a in EXEC_ACTIONS)


def _token_entry_args(args: argparse.Namespace, *, kind: str = "agent") -> Dict[str, Any]:
    from .auth import CAUTIOUS_ACTIONS
    entry: Dict[str, Any] = {"kind": kind, "role": args.role or "operator"}
    nodes = ["*"] if (args.nodes or "*").strip() == "*" else _split(args.nodes)
    if not nodes:
        raise CliError("--nodes is empty", hint="'*' or a comma-separated list of node names/globs")
    entry["nodes"] = nodes
    if args.deny:
        entry["deny_actions"] = _split(args.deny)
    if args.approval and args.cautious:
        raise CliError("give --approval or --cautious, not both")
    if args.approval:
        entry["require_approval"] = _split(args.approval)
    elif args.cautious:
        entry["require_approval"] = list(CAUTIOUS_ACTIONS)
    if args.expires:
        try:
            util.parse_iso(args.expires)
        except HiveError:
            raise CliError(f"invalid --expires {args.expires!r}", hint="YYYY-MM-DD")
        entry["expires"] = args.expires
    return entry


def _mcp_remote_line(base: str, token: str) -> str:
    return (f'claude mcp add --transport http hive "{base}/mcp?wait_budget=110" --header '
            f'"Authorization: Bearer {token}"')


def cmd_hub_add_agent(args: argparse.Namespace, out: Out) -> int:
    """``hive hub add-agent NAME …``: prints the token once, the init client command and MCP snippets."""
    cfg = _hub_config(args)
    name = _valid_new_name(args.name, "token")
    if any(t.name == name for t in cfg.tokens):
        raise CliError(f"a token named {name!r} exists", hint=f"hive hub remove-agent {name} first, or "
                                                             f"hive hub rotate-token {name}")
    entry = _token_entry_args(args)
    token = util.new_token()
    entry.update(name=name, sha256=util.hash_token(token))

    def fn(raw: dict) -> None:
        toks = raw.setdefault("tokens", [])
        if any(isinstance(t, dict) and t.get("name") == name for t in toks):
            raise conflict(f"a token named {name!r} exists")
        toks.append(entry)
    _hub_edit(args, fn)
    urls = [u for lab, u in _hub_urls(cfg, here=False) if lab != "magicdns"] or \
        [u for _lab, u in _hub_urls(cfg, here=True)]
    base = urls[0]
    out.line(f"agent token '{name}' added (role {entry['role']}, nodes {','.join(entry['nodes'])}"
             + (", approvals required" if entry.get("require_approval") else "") + ")")
    out.line("token (shown once; save it in a file on the agent's machine, never in an agent config):")
    out.line(token)
    out.line("")
    out.line("on the agent's machine:")
    out.line(f"  1. hive init client --name {cfg.name} --url {base} --token - --default")
    out.line(f"     (paste the token when asked; it is not shown and is saved to ~/.hive/tokens/{cfg.name}.token, "
             "readable only by you)")
    out.line(f"  2. MCP for Claude Desktop/Cursor: hive mcp-config --client claude-desktop --connection {cfg.name} "
             "--write   (Claude Code: --client claude-code)")
    if len(urls) > 1:
        out.line("  other hub addresses: " + ", ".join(urls[1:]))
    https = [u for u in urls if u.startswith("https://")]
    if https and cfg.listen.mcp_http:
        out.line("remote MCP (Claude Code over HTTP; the only case where a token appears in an agent config):")
        out.line("  " + _mcp_remote_line(https[0], token))
    if _exec_ungated(entry):
        local = [n for n in _hub_local_nodes(cfg) if any(fnmatch.fnmatchcase(n, g) for g in entry["nodes"])]
        if local:
            out.line(f"warning: this token can run commands on {', '.join(local)} (the hub's machine): an "
                     "exec-capable agent on the hub machine can act as the hub (all nodes, approvals, auto-update "
                     "pods); consider --cautious")
    _restart_hub(args, out)
    return EXIT_OK


def cmd_hub_remove_node(args: argparse.Namespace, out: Out) -> int:
    """``hive hub remove-node NAME``."""
    cfg = _hub_config(args)
    name = args.name.lower()
    found = [n for n in cfg.nodes if n.name == name]
    if not found:
        out.error("not_found", f"no node named {name!r}", "hive hub list")
        return EXIT_ACTION

    def fn(raw: dict) -> None:
        raw["nodes"] = [n for n in raw.get("nodes") or [] if not (isinstance(n, dict) and n.get("name") == name)]
        if raw.get("default_node") == name:
            raw["default_node"] = None
    _hub_edit(args, fn)
    if found[0].mode == "relay":
        out.line(f"node '{name}' removed (its relay token no longer works)")
    else:
        # a direct node has no relay token: the hub only stops calling it; the hub token stays valid on that machine
        out.line(f"node '{name}' removed (the hub no longer calls it)")
        out.line(f"to also remove the hub's token on {name}, run there: hive node token revoke hub-{cfg.name}")
    _restart_hub(args, out)
    return EXIT_OK


def cmd_hub_remove_agent(args: argparse.Namespace, out: Out) -> int:
    """``hive hub remove-agent NAME``."""
    cfg = _hub_config(args)
    name = args.name.lower()
    tok = [t for t in cfg.tokens if t.name == name]
    if not tok:
        out.error("not_found", f"no token named {name!r}", "hive hub list")
        return EXIT_ACTION
    if tok[0].kind == "owner":
        out.error("conflict", "this is the hub's owner token", "the local CLI needs it")
        return EXIT_ACTION

    def fn(raw: dict) -> None:
        raw["tokens"] = [t for t in raw.get("tokens") or [] if not (isinstance(t, dict) and t.get("name") == name)]
    _hub_edit(args, fn)
    out.line(f"token '{name}' removed")
    _restart_hub(args, out)
    return EXIT_OK


def cmd_hub_set_agent(args: argparse.Namespace, out: Out) -> int:
    """``hive hub set-agent NAME --cautious|--no-cautious`` (final acceptance B3): flips a hub agent token's
    approval rule; the token value stays the same."""
    from .auth import CAUTIOUS_ACTIONS
    cautious = _cautious_flag(args)
    cfg = _hub_config(args)
    name = args.name.lower()
    tok = [t for t in cfg.tokens if t.name == name]
    if not tok:
        out.error("not_found", f"no token named {name!r}", "hive hub list")
        return EXIT_ACTION
    if tok[0].kind != "agent":
        raise CliError(f"token {name!r} is a {tok[0].kind} token; approvals apply to agent tokens only")

    def fn(raw: dict) -> None:
        for t in raw.get("tokens") or []:
            if isinstance(t, dict) and t.get("name") == name:
                if cautious:
                    t["require_approval"] = list(CAUTIOUS_ACTIONS)
                else:
                    t.pop("require_approval", None)
    _hub_edit(args, fn)
    out.line(f"agent token '{name}': " + ("cautious (its shell/service/delete calls wait for your approval: hive "
                                          "approvals list)" if cautious else "no approvals needed any more")
             + "; the token itself is unchanged")
    _restart_hub(args, out)
    return EXIT_OK


def cmd_hub_rotate_token(args: argparse.Namespace, out: Out) -> int:
    """``hive hub rotate-token NAME``: a relay node gets a fresh join block (the revocation path of a leaked code);
    a direct node a new hub token (only its hash goes to the node); an agent/models token a new token."""
    cfg = _hub_config(args)
    name = args.name.lower()
    node = [n for n in cfg.nodes if n.name == name]
    tok = [t for t in cfg.tokens if t.name == name]
    token = util.new_token()
    lines: List[str] = []
    if node:
        n = node[0]
        if n.mode == "relay":
            lines = _relay_block(cfg, name, token, args)

            def fn(raw: dict) -> None:
                for e in raw.get("nodes") or []:
                    if isinstance(e, dict) and e.get("name") == name:
                        e["token_sha256"] = util.hash_token(token)
        else:
            lines = [f"on the node '{name}', replace the hub token (only the hash crosses machines):",
                     f"  hive node token revoke hub-{cfg.name}",
                     f"  hive node token add hub-{cfg.name} --kind hub --sha256 {util.hash_token(token)}"]

            def fn(raw: dict) -> None:
                for e in raw.get("nodes") or []:
                    if isinstance(e, dict) and e.get("name") == name:
                        e["token"] = token
    elif tok:
        if tok[0].kind == "owner":
            raise CliError("the owner token is rotated with: hive init hub --force")
        lines = ["new token (shown once; replace it in the token file on the agent's/app's machine):", token]

        def fn(raw: dict) -> None:
            for e in raw.get("tokens") or []:
                if isinstance(e, dict) and e.get("name") == name:
                    e["sha256"] = util.hash_token(token)
    else:
        out.error("not_found", f"no node or token named {name!r}", "hive hub list")
        return EXIT_ACTION
    _hub_edit(args, fn)
    out.line(f"'{name}': the old token no longer works")
    for line in lines:
        out.line(line)
    _restart_hub(args, out)
    return EXIT_OK


def _show_pending(out: Out, name: str, pending: dict) -> None:
    out.foreign(f"pending registration for '{name}': from {pending.get('ip')} at {pending.get('at')}, hostname "
                f"{pending.get('hostname') or '-'}, pod {pending.get('pod_id') or '-'}, key "
                f"{str(pending.get('sha256') or '')[:19]}…")


def _ack_offline(cfg: Any, name: str, args: argparse.Namespace, out: Out) -> int:
    """ack-node with the hub down: edit relay_ids.json under hub.lock (§19)."""
    d = Path(cfg.data_dir)
    if not any(n.name == name and n.mode == "relay" for n in cfg.nodes):
        out.error("not_found", f"no relay node named {name!r}", "hive hub list")
        return EXIT_ACTION
    lock = util.FileLock(d / "hub.lock")
    if not lock.acquire(note="ack-node"):
        out.error("unavailable", "the hub holds its lock but does not answer on hub-local", "try again")
        return EXIT_UNAVAILABLE
    try:
        path = d / "state" / "relay_ids.json"
        data = util.read_json(path, {}) or {}
        rec = ((data.get("nodes") or {}).get(name)) if isinstance(data, dict) else None
        if not isinstance(rec, dict) or not rec.get("pending_sha256"):
            out.line(f"no pending registration for '{name}'")
            return EXIT_OK
        pending = dict(rec.get("pending") or {}, sha256=rec["pending_sha256"])
        _show_pending(out, name, pending)
        seen, at = rec.get("bound_seen_at"), pending.get("at")
        if seen and at and str(seen) > str(at):
            out.error("conflict", "the bound instance registered after this attempt",
                      f"rotate the token instead: hive hub rotate-token {name}")
            return EXIT_ACTION
        if not _confirm(f"Bind '{name}' to this new instance?", yes=args.yes):
            out.line("not changed")
            return EXIT_USAGE
        rec.update(key_sha256=rec["pending_sha256"], pending_sha256=None, pending=None, bound_at=util.iso_utc(),
                   bound_seen_at=None)
        util.write_json_atomic(path, data, private=True)
        out.line(f"'{name}' bound to the new instance (takes effect when the hub starts)")
        return EXIT_OK
    finally:
        lock.release()


def cmd_hub_ack_node(args: argparse.Namespace, out: Out) -> int:
    """``hive hub ack-node NAME [--yes]`` (POST /v1/hub/nodes/{name}/ack over hub-local, §7.1)."""
    name = args.name.lower()
    client = None
    try:
        client = _hub_client(args)
        prev = _owner_request(client, "POST", f"/v1/hub/nodes/{name}/ack", {})
        pending = prev.get("pending")
        if not pending:
            out.line(f"no pending registration for '{name}'")
            return EXIT_OK
        _show_pending(out, name, pending)
        if not _confirm(f"Bind '{name}' to this new instance (check the IP/hostname is your pod)?", yes=args.yes):
            out.line("not changed")
            return EXIT_USAGE
        res = _owner_request(client, "POST", f"/v1/hub/nodes/{name}/ack", {"pending_sha256": pending.get("sha256")})
    except HiveError as e:
        if e.code == "unavailable" and (e.details or {}).get("reason") in ("connect", "dns", None):
            return _ack_offline(_hub_config(args), name, args, out)
        raise
    finally:
        if client is not None:
            client.close()
    if res.get("bound"):
        out.line(f"'{name}' bound to the new instance")
        return EXIT_OK
    out.error("conflict", str(res.get("refused") or "not bound"), f"or: hive hub rotate-token {name}")
    return EXIT_ACTION


# seams for the --outdated waits (tests use a fake clock)
_now = time.monotonic
_sleep = time.sleep


UPGRADE_BY_HAND = ("upgrade by hand on that machine: pip install -U ./hive (or put a new hive.pyz in place), then "
                   "hive node restart")


def _auto_update_off_lines(name: str, ver: str, why: Any) -> List[str]:
    """``restart-node --outdated`` lines for an outdated relay node without auto-update (final acceptance B5g): why
    it is off (the node's ``auto_update_off``) and how to turn it on, or how to upgrade it by hand."""
    head = f"{name}: outdated (version {ver}), no auto-update"
    why = why if isinstance(why, str) else ""
    if why.startswith("role:"):
        role = why[5:]
        return [f"{head}: its relay role is '{role}' (only a pod with role operator may replace its own program)",
                f"  to turn it on, on that machine: hive init node --relay-role operator --auto-update; or "
                + UPGRADE_BY_HAND]
    if why == "deny_actions":
        return [f"{head}: its relay config denies some actions (relay.deny_actions), so the hub may not update it",
                "  " + UPGRADE_BY_HAND]
    if why == "not_pod":
        return [f"{head}: it is not a cloud pod (auto-update needs the pod's watchdog loop: hive join --install on "
                "a pod, or deploy/runpod/start-hive.sh)", "  " + UPGRADE_BY_HAND]
    if why == "off":
        return [f"{head}: relay.auto_update is off in its config",
                "  to turn it on, on that machine: hive init node --auto-update; or " + UPGRADE_BY_HAND]
    return [f"{head}: it is not a cloud pod run by the watchdog, or its relay role is narrowed (auto-update needs both "
            "a pod and relay role operator)", "  " + UPGRADE_BY_HAND]


def cmd_hub_restart_node(args: argparse.Namespace, out: Out) -> int:
    """``hive hub restart-node NAME | --outdated`` (relay refresh "restart" via the hub owner route, D49)."""
    if bool(args.name) == bool(args.outdated):
        raise CliError("give NAME or --outdated")
    client = _hub_client(args)
    try:
        if args.name:
            res = _owner_request(client, "POST", f"/v1/hub/nodes/{args.name.lower()}/restart", {})
            if res.get("queued"):
                out.line(f"restart queued for '{args.name}' (applies at its next poll)")
                return EXIT_OK
            out.error("unavailable", f"'{args.name}' is offline (or its identity changed); nothing queued")
            return EXIT_UNAVAILABLE
        cfg = _hub_config(args)
        relay_names = [n.name for n in cfg.nodes if n.mode == "relay"]
        offline_after = float(cfg.relay.offline_after or 75)
        end = _now() + offline_after
        rows: List[dict] = []
        while True:
            rows = [r for r in client.nodes() if r.get("name") in relay_names]
            missing = [n for n in relay_names if not any(r.get("name") == n and r.get("online") for r in rows)]
            if not missing or _now() >= end:
                break
            out.line(f"waiting for relay nodes to come online: {', '.join(missing)}")
            _sleep(5)
        # §19 v4: every outdated node is named; only online auto-updating relay nodes get a restart
        queued = []
        for r in client.nodes():
            if not r.get("outdated"):
                continue
            rname = util.escape_terminal(str(r.get("name")))
            ver = util.escape_terminal(str(r.get("version")))
            if r.get("name") not in relay_names:
                out.line(f"{rname}: outdated (version {ver}), direct node: upgrade by hand")
                continue
            if not r.get("online"):
                out.line(f"{rname}: outdated (version {ver}), offline: upgrade by hand when it is back")
                continue
            if not r.get("auto_update"):
                for line in _auto_update_off_lines(rname, ver, r.get("auto_update_off")):
                    out.line(line)
                continue
            res = _owner_request(client, "POST", f"/v1/hub/nodes/{r.get('name')}/restart", {})
            if res.get("queued"):
                queued.append(r.get("name"))
                out.line(f"{rname}: restart queued")
            else:
                out.line(f"{rname}: outdated (version {ver}), restart not queued: upgrade by hand")
        if not queued:
            out.line("no auto-updating outdated relay node to restart")
            return EXIT_OK
        end = _now() + 180
        left = list(queued)
        while left and _now() < end:
            _sleep(5)
            cur = {r.get("name"): r for r in client.nodes()}
            left = [n for n in left if not (cur.get(n, {}).get("online") and not cur.get(n, {}).get("outdated"))]
        if left:
            out.line("still outdated or offline: " + ", ".join(str(n) for n in left))
            return EXIT_UNAVAILABLE
        out.line("all restarted nodes are up to date")
        return EXIT_OK
    finally:
        client.close()


# ---------------------------------------------------------------------------------------------------------------
# WP6b: gateway (§8.7)

def _gateway_side(args: argparse.Namespace) -> str:
    from .config import default_paths
    if args.hub and args.node:
        raise CliError("give --hub or --node, not both")
    if args.node:
        return "node"
    if args.hub or default_paths()["hub_config"].exists():
        return "hub"
    return "node"


def cmd_gateway_token_add(args: argparse.Namespace, out: Out) -> int:
    """``hive gateway token add NAME --models 'node/service,…' [--hub|--node] [--expires DATE]``."""
    from . import config
    side = _gateway_side(args)
    name = _valid_new_name(args.name, "token")
    models = _split(args.models)
    if not models:
        raise CliError("--models is empty", hint="e.g. --models 'desk-wsl/qwen,pod-a/*'" if side == "hub"
                       else "e.g. --models 'qwen,ollama'")
    for m in models:
        if side == "hub" and m.count("/") != 1:
            raise CliError(f"hub gateway globs are node/service: {m!r}", hint="e.g. desk-wsl/qwen or pod-a/*")
        if side == "node" and "/" in m:
            raise CliError(f"node gateway globs are service names: {m!r}", hint="e.g. qwen or *")
    token = util.new_token()
    entry: Dict[str, Any] = {"name": name, "sha256": util.hash_token(token), "kind": "models", "models": models}
    if args.expires:
        try:
            util.parse_iso(args.expires)
        except HiveError:
            raise CliError(f"invalid --expires {args.expires!r}", hint="YYYY-MM-DD")
        entry["expires"] = args.expires
    if side == "hub":
        cfg = _hub_config(args)
        if any(t.name == name for t in cfg.tokens):
            raise CliError(f"a token named {name!r} exists", hint=f"hive gateway token revoke {name}")

        def fn(raw: dict) -> None:
            raw.setdefault("tokens", []).append(entry)
        _hub_edit(args, fn)
    else:
        cfg = _node_config(args)
        if any(t.name == name for t in cfg.auth.tokens):
            raise CliError(f"a token named {name!r} exists", hint=f"hive gateway token revoke {name} --node")

        def fn(raw: dict) -> None:
            raw.setdefault("auth", {}).setdefault("tokens", []).append(entry)
        config.edit_config(Path(cfg._path), fn, kind="node")
    out.line(f"gateway token '{name}' for {', '.join(models)} ({side})")
    out.line("API key (shown once; paste it only into the app's settings, never into a chat):")
    out.line(token)
    first = models[0]
    if not any(ch in first for ch in "*?["):
        # §19/§8.7: the app settings block right away (Model: the id the model server reports; `gateway url
        # --token-file` lists them once the daemon has restarted)
        node, svc = first.split("/", 1) if side == "hub" else ("self", first)
        try:
            bases = _gateway_bases(out, node, svc, own_node=args if side == "node" else None,
                                   hub_args=args if side == "hub" else None)
        except CliError as e:
            bases = []
            out.line(f"note: cannot build the base URLs ({e})")
        if bases:
            out.line("app settings:")
            _gateway_settings(out, bases, "<model name>", "the API key above")
        out.line(f"model ids served: hive gateway url {node} {svc}" + (" --node" if side == "node" else "")
                 + " --token-file FILE (a file holding this key)")
    else:
        out.line("app settings: hive gateway url NODE SERVICE" + (" --node" if side == "node" else ""))
    _restart_daemon(side, out, no_restart=args.no_restart)
    return EXIT_OK


def cmd_gateway_token_list(args: argparse.Namespace, out: Out) -> int:
    """``hive gateway token list [--hub|--node]``."""
    side = _gateway_side(args)
    toks = (_hub_config(args).tokens if side == "hub" else _node_config(args).auth.tokens)
    rows = [{"name": t.name, "models": t.models, "expires": t.expires} for t in toks if t.kind == "models"]
    if out.as_json:
        out.data(rows)
    else:
        out.line(_table([[r["name"], ",".join(r["models"]), r["expires"] or "-"] for r in rows],
                        ["NAME", "MODELS", "EXPIRES"]))
    return EXIT_OK


def cmd_gateway_token_revoke(args: argparse.Namespace, out: Out) -> int:
    """``hive gateway token revoke NAME [--hub|--node]``."""
    from . import config
    side = _gateway_side(args)
    name = args.name.lower()
    if side == "hub":
        cfg = _hub_config(args)
        toks = cfg.tokens
    else:
        cfg = _node_config(args)
        toks = cfg.auth.tokens
    if not any(t.name == name and t.kind == "models" for t in toks):
        out.error("not_found", f"no gateway token named {name!r}", "hive gateway token list")
        return EXIT_ACTION

    def drop(lst: List[Any]) -> List[Any]:
        return [t for t in lst if not (isinstance(t, dict) and t.get("name") == name and t.get("kind") == "models")]
    if side == "hub":
        def fn(raw: dict) -> None:
            raw["tokens"] = drop(raw.get("tokens") or [])
        _hub_edit(args, fn)
    else:
        def fn(raw: dict) -> None:
            a = raw.setdefault("auth", {})
            a["tokens"] = drop(a.get("tokens") or [])
        config.edit_config(Path(cfg._path), fn, kind="node")
    out.line(f"gateway token '{name}' revoked")
    _restart_daemon(side, out, no_restart=args.no_restart)
    return EXIT_OK


def _gateway_models(base: str, token: str) -> List[str]:
    """Model ids from ``GET <base>/v1/models`` (best effort)."""
    from urllib.parse import urlsplit
    from .transport import HttpTransport
    u = urlsplit(base)
    root = f"{u.scheme}://{u.netloc}"
    tr = HttpTransport(root, token, timeout=15, proxy="auto")
    try:
        st, _h, body = tr.request("GET", u.path.rstrip("/") + "/v1/models", raw=True)
    finally:
        tr.close()
    if int(st) != 200:
        raise HiveError("unavailable", f"GET {u.path}/v1/models answered {st}")
    data = json.loads(body.decode("utf-8", "replace"))
    return [str(m.get("id")) for m in (data.get("data") or []) if isinstance(m, dict) and m.get("id")][:10]


def _gateway_bases(out: Out, node: str, svc: str, *, own_node: Optional[argparse.Namespace] = None,
                   connection: Optional[str] = None, hub_args: Optional[argparse.Namespace] = None
                   ) -> List[Tuple[str, str]]:
    """[(label, base URL without /v1)] for NODE/SERVICE (§8.7 URLs): this node's own gateway (``own_node`` = the args
    naming its config), a client.json connection, or the hub config on this machine."""
    from .config import default_paths
    bases: List[Tuple[str, str]] = []
    if own_node is not None:
        cfg = _node_config(own_node)
        scheme = "https" if cfg.listen.tls.cert else "http"
        bases.append(("apps on this PC", f"{scheme}://127.0.0.1:{cfg.listen.port}/openai/self/{svc}"))
    elif connection:
        from .config import load_client_config
        cc = load_client_config()
        conn = cc.connections.get(connection)
        if conn is None:
            raise CliError(f"no connection named {connection!r}", hint="hive init client …")
        bases.append(("via your connection", f"{conn.url.rstrip('/')}/openai/{node}/{svc}"))
    elif hub_args is not None or default_paths()["hub_config"].exists():
        cfg = _hub_config(hub_args if hub_args is not None else argparse.Namespace())
        labels = {"tailnet": "apps on your tailnet (recommended)",
                  "magicdns": "alternative (MagicDNS name; not through a system proxy)",
                  "lan": "apps on your LAN", "listen": "apps that reach the hub",
                  "public": "browser apps on https pages / devices off the tailnet",
                  "local": "apps on this PC"}
        for lab, base in _hub_urls(cfg, here=True):
            bases.append((labels[lab], f"{base}/openai/{node}/{svc}"))
            if lab == "local":
                docker = base.replace("127.0.0.1", "host.docker.internal")
                bases.append(("apps in Docker Desktop on this PC", f"{docker}/openai/{node}/{svc}"))
        if str(cfg.listen.host) == "tailscale" and not any(b[0].startswith("apps on your tailnet") for b in bases):
            out.line("note: cannot read this machine's tailnet IP (is tailscale up?); start it for the tailnet URL")
    else:
        raise CliError("no hub config on this machine", hint="run it on the hub, or pass --connection C (a hub "
                                                             "connection) or --node (this node's own gateway)")
    return bases


def _gateway_settings(out: Out, bases: List[Tuple[str, str]], model: str, api_key: str) -> None:
    """The "Provider: OpenAI-compatible · Base URL · API key · Model" block per base, plus the /v1-less variant."""
    for label, base in bases:
        out.line(f"{label}:")
        out.line(f"  Provider: OpenAI-compatible · Base URL: {base}/v1 · API key: {api_key} · Model: {model}")
        out.line(f"  Apps that add /v1 themselves (e.g. Cherry Studio): Base URL {base}")
    if any(lab.startswith("apps on your tailnet") for lab, _b in bases):
        out.line("if the app uses a system proxy (Clash …), add 100.64.0.0/10 and *.ts.net to its proxy bypass")


def cmd_gateway_url(args: argparse.Namespace, out: Out) -> int:
    """``hive gateway url NODE SERVICE [--connection C] [--token-file F] [--node]`` (§8.7 URLs)."""
    svc = args.service
    if not re.match(r"^[a-z0-9][a-z0-9._-]{0,63}\Z", svc or ""):
        raise CliError(f"invalid service name {svc!r}")
    node = args.node_name.lower()
    conn = getattr(args, "connection", None)
    bases = _gateway_bases(out, node, svc, own_node=args if args.node else None, connection=conn,
                           hub_args=args if getattr(args, "config", None) else None)
    model = "<model name>"
    if args.token_file:
        try:
            tok = Path(os.path.expanduser(args.token_file)).read_text(encoding="utf-8-sig").strip().splitlines()[0]
            probe = [b for lab, b in bases if lab == "apps on this PC"] or [bases[0][1]]
            ids = _gateway_models(probe[0], tok)
            if ids:
                model = util.escape_terminal(ids[0])        # model-server text (§19: node-supplied, escaped)
                out.foreign("served models: " + ", ".join(ids))
        except (OSError, IndexError, HiveError, ValueError) as e:
            out.line(f"note: could not list the served models ({util.escape_terminal(str(getattr(e, 'message', e)))})")
    _gateway_settings(out, bases, model, "<your gateway token>")
    out.line("browser apps on an https:// page need the https (public) URL or a http://127.0.0.1 URL on the same "
             "machine; a model_endpoints name such as ollama or lmstudio works as SERVICE too")
    if not args.token_file:     # final acceptance B5d: with --token-file the user has a token already
        out.line("no token yet? hive gateway token add APP --models '" + (f"{svc}'" if args.node else
                                                                          f"{node}/{svc}'")
                 + (" --node" if args.node else ""))
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# WP6b: approvals (owner routes over this-pc-owner / hub-local)

def _approval_places(args: argparse.Namespace) -> List[Tuple[str, str]]:
    """[(where, connection)] for --node/--hub or every place on this machine."""
    from .config import load_client_config
    try:
        cc = load_client_config()
        conns = cc.connections
    except HiveError:
        conns = {}
    places = []
    if not getattr(args, "hub", False) and "this-pc-owner" in conns:
        places.append(("node", "this-pc-owner"))
    if not getattr(args, "node", False) and "hub-local" in conns:
        places.append(("hub", "hub-local"))
    if not places:
        want = "node" if getattr(args, "node", False) else "hub" if getattr(args, "hub", False) else "node or hub"
        raise CliError(f"no {want} on this machine (client.json has no this-pc-owner/hub-local connection)",
                       hint="run this on the machine where the node or hub runs")
    return places


def _place_client(conn: str) -> Any:
    from .client import connect
    return connect(conn)


def _not_running(where: str) -> str:
    return f"{where} not running"


def cmd_approvals_list(args: argparse.Namespace, out: Out) -> int:
    """``hive approvals list [--node|--hub]``."""
    rows: List[dict] = []
    errors = 0
    last = HiveError("unavailable", "not running")
    places = _approval_places(args)
    for where, conn in places:
        c = _place_client(conn)
        try:
            res = _owner_request(c, "GET", "/v1/approvals")
            for r in res.get("approvals") or []:
                rows.append(dict(r, _where=where))
        except HiveError as e:
            errors += 1
            last = e
            out.line(f"{where}: {_not_running(where) if e.code == 'unavailable' else e.code + ': ' + e.message}")
        finally:
            c.close()
    if errors and errors == len(places):
        # nothing could be asked: not "no pending approvals" (like approvals revoke)
        if out.as_json:
            out.data(rows)
        return exit_code_for(last)
    if out.as_json:
        out.data(rows)
        return EXIT_OK
    if not rows:
        out.line("no pending approvals" + (" (where it could be checked)" if errors else ""))
        return EXIT_OK
    for r in rows:
        exp = r.get("expires")
        out.foreign(f"{r.get('id')}  [{r.get('_where')}] {r.get('requester')} → {r.get('action')} on "
                    f"{r.get('node') or '-'}  ({r.get('status')}, expires {exp})")
        summ = r.get("summary")
        if summ:
            s = summ if isinstance(summ, str) else json.dumps(summ, ensure_ascii=False)
            if len(s) > 300:
                s = s[:300] + f" [TRUNCATED, {len(s) - 300} bytes hidden]"
            out.foreign("    " + s)
    out.line("decide: hive approvals approve ID [--for 30m]   |   hive approvals deny ID")
    return EXIT_OK


def _find_approval(args: argparse.Namespace, aid: str) -> Tuple[str, str, dict]:
    if not re.match(r"^ap_[A-Za-z0-9_-]{6,40}\Z", aid or ""):
        raise CliError(f"invalid approval id {aid!r}", hint="ids look like ap_…")
    down = []
    for where, conn in _approval_places(args):
        c = _place_client(conn)
        try:
            res = _owner_request(c, "GET", "/v1/approvals?include_done=1")
        except HiveError as e:
            if e.code == "unavailable":
                down.append(where)
                continue
            raise
        finally:
            c.close()
        for r in res.get("approvals") or []:
            if r.get("id") == aid:
                return where, conn, r
    if down and len(down) == len(_approval_places(args)):
        raise HiveError("unavailable", " and ".join(_not_running(w) for w in down),
                        hint="the approval lives in the running daemon")
    raise HiveError("not_found", f"approval {aid} not found", hint="hive approvals list")


def _page(out: Out, lines: List[str]) -> None:
    """Print review lines (escaped), paged by 40 when interactive."""
    tty = sys.stdin is not None and sys.stdin.isatty() and sys.stdout.isatty()
    for i, line in enumerate(lines):
        out.foreign(str(line))
        if tty and i and i % 40 == 39 and i + 1 < len(lines):
            try:
                if input("-- more (Enter, q to stop) --").strip().lower() == "q":
                    out.line(f"[{len(lines) - i - 1} more lines not shown]")
                    return
            except EOFError:
                return


def _parse_for(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    m = re.match(r"^(\d+)\s*([mh]?)\Z", v.strip().lower())
    if not m:
        raise CliError(f"invalid --for {v!r}", hint="e.g. 30m or 2h")
    n = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
    if n < 1:
        raise CliError("--for must be at least 1m")
    return n


def _decide(args: argparse.Namespace, out: Out, approve: bool) -> int:
    grant = _parse_for(getattr(args, "for_", None)) if approve else None
    where, conn, rec = _find_approval(args, args.id)
    if rec.get("status") != "pending":
        out.error("conflict", f"approval {args.id} is {rec.get('status')}")
        return EXIT_ACTION
    out.foreign(f"{rec.get('id')} on {where}: {rec.get('requester')} wants {rec.get('action')} on "
                f"{rec.get('node') or '-'}")
    review = rec.get("review") if isinstance(rec.get("review"), dict) else {}
    lines = review.get("lines") if isinstance(review.get("lines"), list) else None
    if lines is None:
        summ = rec.get("summary")
        lines = (summ if isinstance(summ, str) else json.dumps(summ, ensure_ascii=False, indent=2)).splitlines()
    _page(out, lines)
    verb = "Approve" + (f" (and allow {rec.get('action')} for {grant} min)" if grant else "") if approve else "Deny"
    if not args.yes and (not sys.stdin or not sys.stdin.isatty()):
        out.line(f"nothing decided: not a terminal, so there is no y/N question; after reading the review above "
                 f"add --yes: hive approvals {'approve' if approve else 'deny'} {args.id} --yes")
        return EXIT_USAGE
    if not _confirm(f"{verb} this call?", yes=args.yes):
        out.line("nothing decided")
        return EXIT_USAGE
    body: Dict[str, Any] = {}
    if grant:
        body["grant_minutes"] = grant
    c = _place_client(conn)
    try:
        res = _owner_request(c, "POST", f"/v1/approvals/{args.id}/{'approve' if approve else 'deny'}", body)
    finally:
        c.close()
    out.line(f"{args.id}: {res.get('status')}" + (f"; grant {(res.get('grant') or {}).get('id')}" if grant else ""))
    if approve:
        out.line("the agent can now retry the same call with this approval_id")
    return EXIT_OK


def cmd_approvals_approve(args: argparse.Namespace, out: Out) -> int:
    """``hive approvals approve ID [--for 30m] [--yes]`` (prints the full review before y/N)."""
    return _decide(args, out, True)


def cmd_approvals_deny(args: argparse.Namespace, out: Out) -> int:
    """``hive approvals deny ID [--yes]``."""
    return _decide(args, out, False)


def cmd_approvals_grants(args: argparse.Namespace, out: Out) -> int:
    """``hive approvals grants [--node|--hub]``."""
    rows = []
    for where, conn in _approval_places(args):
        c = _place_client(conn)
        try:
            for g in _owner_request(c, "GET", "/v1/approvals/grants").get("grants") or []:
                rows.append(dict(g, _where=where))
        except HiveError as e:
            if e.code != "unavailable":
                raise
            out.line(_not_running(where))
        finally:
            c.close()
    if out.as_json:
        out.data(rows)
        return EXIT_OK
    out.line(_table([[g.get("id"), g.get("_where"), g.get("identity"), g.get("action"), g.get("node"), g.get("until")]
                     for g in rows], ["GRANT", "WHERE", "TOKEN", "ACTION", "NODE", "UNTIL"]))
    return EXIT_OK


def cmd_approvals_revoke(args: argparse.Namespace, out: Out) -> int:
    """``hive approvals revoke [ID | --all] [--node|--hub]``."""
    if bool(args.id) == bool(args.all):
        raise CliError("give a grant ID or --all")
    body: Dict[str, Any] = {"all": True} if args.all else {"id": args.id}
    total = 0
    code = EXIT_OK
    for where, conn in _approval_places(args):
        c = _place_client(conn)
        try:
            total += int(_owner_request(c, "POST", "/v1/approvals/grants/revoke", body).get("revoked") or 0)
        except HiveError as e:
            if e.code != "unavailable":
                raise
            out.line(_not_running(where))
            code = EXIT_UNAVAILABLE
        finally:
            c.close()
    out.line(f"revoked {total} grant(s)")
    return code if total == 0 else EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# WP6b: mcp-config (§18.4)

def _desktop_config_path() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or Path.home()) / "Claude" / "claude_desktop_config.json"
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"


def _mcp_env() -> Dict[str, str]:
    """v8.1: ``{"HIVE_HOME": …}`` for MCP snippets when this shell uses a non-default HIVE_HOME (the MCP client
    starts ``hive mcp`` without it and would look in ~/.hive)."""
    h = os.environ.get("HIVE_HOME")
    if not h:
        return {}
    try:
        cur = Path(os.path.expanduser(h)).resolve()
        default = (Path(os.path.expanduser("~")) / ".hive").resolve()
    except OSError:
        return {}
    return {} if cur == default else {"HIVE_HOME": str(cur)}


def _write_mcp_json(path: Path, server: dict, out: Out) -> None:
    data: Dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, ValueError) as e:
            raise CliError(f"cannot read {path}: {e}", hint="fix or remove it, then run again")
        if not isinstance(data, dict):
            raise CliError(f"{path} is not a JSON object")
        try:
            # the config may hold other MCP servers' API keys (env): the backup is private like the original
            util.atomic_write_bytes(Path(str(path) + ".bak"), path.read_bytes(), private=True)
        except OSError:
            pass
    data.setdefault("mcpServers", {})["hive"] = server
    path.parent.mkdir(parents=True, exist_ok=True)
    util.atomic_write_bytes(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out.line(f"wrote {path} (restart the app to load it)")


def _mcp_remove(client: str, out: Out) -> int:
    """``hive mcp-config --client X --remove`` (final acceptance B4): take the "hive" server out of the app's MCP
    config (a .bak copy is kept), or print the command that does it (Claude Code)."""
    if client == "claude-code":
        out.line("run: claude mcp remove hive   (if you added it with -s user: claude mcp remove -s user hive)")
        return EXIT_OK
    if client == "generic":
        raise CliError("--remove works with --client claude-desktop, cursor or claude-code",
                       hint="for another MCP client, delete its \"hive\" server entry by hand")
    path = Path.home() / ".cursor" / "mcp.json" if client == "cursor" else _desktop_config_path()
    data: Any = None
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        except (OSError, ValueError) as e:
            raise CliError(f"cannot read {path}: {e}", hint="fix it, or delete its \"hive\" entry by hand")
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict) or "hive" not in servers:
        out.line(f"nothing to remove: no \"hive\" entry in {path}")
        if client == "cursor":
            out.line("(a project's own .cursor/mcp.json is not touched; delete its \"hive\" entry by hand)")
        return EXIT_OK
    try:
        util.atomic_write_bytes(Path(str(path) + ".bak"), path.read_bytes(), private=True)
    except OSError:
        pass
    del servers["hive"]
    util.atomic_write_bytes(path, (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
    out.line(f"removed \"hive\" from {path} (restart the app; the old file is kept as {path.name}.bak)")
    return EXIT_OK


def _local_token_kind(token: Optional[str]) -> Optional[str]:
    """Kind of ``token`` from the configs on this machine (node ``auth.tokens``, hub ``tokens``), None if unknown."""
    if not token:
        return None
    from .config import default_paths, load_hub_config, load_node_config
    h = util.hash_token(token)
    paths = default_paths()
    found: Optional[str] = None
    for path, load, attr in ((paths["node_config"], load_node_config, "auth"),
                             (paths["hub_config"], load_hub_config, None)):
        try:
            if not Path(path).exists():
                continue
            cfg = load(str(path))
            toks = cfg.auth.tokens if attr else cfg.tokens
        except Exception:  # noqa: BLE001 - an unreadable config just tells us nothing
            continue
        for t in toks:
            if getattr(t, "sha256", None) == h:
                if t.kind == "owner":
                    return "owner"
                found = found or t.kind
    return found


def _mcp_probe(c: Any) -> Optional[int]:
    """HTTP status of a session-less ``POST /mcp`` (400 = the hub serves /mcp, 404 = not served); None on failure."""
    tr = getattr(c.target, "transport", None)
    if tr is None:
        return None
    try:
        st, _h, _b = tr.request("POST", "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, raw=True,
                                headers={"Accept": "application/json, text/event-stream"})
        return int(st)
    except Exception:  # noqa: BLE001
        return None


def cmd_mcp_config(args: argparse.Namespace, out: Out) -> int:
    """``hive mcp-config [--client …] [--connection NAME | --local] [--remote] [--write] [--allow-owner]``.

    Fails closed (§5.1, §18.4): the token kind comes from ``whoami``; with the server unreachable, from the configs on
    this machine; a kind that stays unknown is refused like an owner token unless ``--allow-owner``."""
    from .client import _cfg_base, _conn_token, connect
    from .config import load_client_config
    if getattr(args, "remove", False):
        return _mcp_remove(args.client or "claude-desktop", out)
    cc = load_client_config()
    if args.local:
        conn = "this-pc"
    elif args.connection:
        conn = args.connection
    else:
        conn = cc.default or ""
        if not conn:
            raise CliError("client.json has no default connection", hint="pass --connection NAME or --local")
        out.line(f"using the default connection '{conn}' (pass --connection NAME for another)")
    if conn not in cc.connections:
        raise CliError(f"no connection named {conn!r} in client.json",
                       hint="hive init node (this-pc) or hive init client --name … --url …")
    conn_obj = cc.connections[conn]
    try:
        tok: Optional[str] = _conn_token(conn_obj, _cfg_base(cc))
    except HiveError:
        tok = None
    client = args.client or "claude-desktop"
    kind = role = None
    mcp_status: Optional[int] = None
    unreachable: Optional[HiveError] = None
    try:
        c = connect(conn)
        try:
            w = c.whoami() or {}
            kind = (w.get("identity") or {}).get("kind")
            role = (w.get("server") or {}).get("role")
            if args.remote and role == "hub":
                mcp_status = _mcp_probe(c)
        finally:
            c.close()
    except HiveError as e:
        unreachable = e
        kind = _local_token_kind(tok)
        if kind is None and conn in ("this-pc-owner", "hub-local"):
            kind = "owner"
    if kind == "owner" and not args.allow_owner:
        out.error("forbidden", f"connection '{conn}' uses an owner token",
                  "agents should use an agent token; see this-pc (hive mcp-config --local), or pass --allow-owner")
        return EXIT_AUTH
    if kind is None and not args.allow_owner:
        e = unreachable
        why = f"{e.code}: {e.message}" if e is not None else "whoami named no kind"
        out.error("unavailable", f"cannot check the token of connection '{conn}' ({why})",
                  "start the hub/node and run this again (owner tokens are refused), or pass --allow-owner to print "
                  "the snippet anyway")
        return EXIT_UNAVAILABLE
    if unreachable is not None:
        out.line(f"note: could not reach the server ({unreachable.code}); the token kind was checked locally"
                 if kind else f"note: could not check the token ({unreachable.code}); printed because of "
                              f"--allow-owner")
    if args.remote:
        # §18.3/§18.5: only a hub serves /mcp; §18.4: the token goes into an agent config only for that hub
        if unreachable is not None:
            out.error("unavailable", f"cannot check that connection '{conn}' is a hub serving /mcp "
                                     f"({unreachable.code})", "start the hub and run this again, or use the stdio "
                                                              "snippet (drop --remote)")
            return EXIT_UNAVAILABLE
        if role != "hub" or mcp_status == 404:
            what = "a node" if role == "node" else ("a hub with listen.mcp_http off" if role == "hub" else
                                                   "not a hub")
            out.error("bad_request", f"remote MCP is served by a hub only (connection '{conn}' is {what})",
                      "use the stdio snippet (drop --remote)")
            return EXIT_USAGE
        from urllib.parse import urlsplit
        base = conn_obj.url.rstrip("/")
        try:
            u = urlsplit(base)
            host, port = (u.hostname or ""), (u.port or (443 if u.scheme == "https" else 80))
        except ValueError:
            host, port = "", 0
        if base.lower().startswith("http://"):
            from .transport import plaintext_ok, resolve_host
            if not host or not plaintext_ok(host, resolve_host(host, port)):
                out.error("bad_request", f"{base} is plain http to a public address; the token would cross the "
                                         f"internet unencrypted", "use the hub's https URL (public_url) or its "
                                                                  "tailnet/LAN address")
                return EXIT_USAGE
        out.line("warning: this puts the token into the agent's config (the only case where Hive does that); "
                 "prefer stdio (drop --remote)")
        if client == "claude-code":
            out.line(_mcp_remote_line(base, tok or "<token>"))
        else:
            out.line(json.dumps({"mcpServers": {"hive": {"type": "http", "url": f"{base}/mcp?wait_budget=45",
                                                         "headers": {"Authorization": f"Bearer {tok or '<token>'}"}}}},
                                indent=2))
        return EXIT_OK
    cmd = _console_command()
    if "-m" in cmd and sys.version_info < (3, 11):
        out.line("warning: Hive is not installed as a command; the snippet uses python -m hive, which puts the MCP "
                 "client's folder on sys.path. Install Hive with pip (pip install ./hive) for a safer snippet.")
    margs = cmd[1:] + ["mcp", "--connection", conn]
    env = _mcp_env()
    if client == "claude-code":
        line = "claude mcp add hive " + "".join(f"-e {_shq(k + '=' + v)} " for k, v in env.items()) + "-- " + \
            " ".join(_shq(a) for a in [cmd[0]] + margs + ["--wait-budget", "110"])
        out.line(line)
        return EXIT_OK
    server: Dict[str, Any] = {"command": cmd[0], "args": margs}
    if env:
        server["env"] = env
    if args.write:
        if client == "cursor":
            _write_mcp_json(Path.home() / ".cursor" / "mcp.json", server, out)
        elif client == "claude-desktop":
            _write_mcp_json(_desktop_config_path(), server, out)
        else:
            raise CliError("--write works with --client claude-desktop or cursor")
        return EXIT_OK
    out.line(json.dumps({"mcpServers": {"hive": server}}, indent=2))
    if client == "cursor":
        out.line("(put this into .cursor/mcp.json, or pass --write for ~/.cursor/mcp.json)")
    elif client == "claude-desktop":
        out.line(f"(Claude Desktop: {_desktop_config_path()}; or pass --write)")
    return EXIT_OK


def _shq(s: str) -> str:
    if os.name == "nt":
        return f'"{s}"' if (" " in s or not s) else s
    import shlex
    return shlex.quote(s)


# ---------------------------------------------------------------------------------------------------------------
# WP6b: service install / uninstall / status, tls-pin, doctor, audit, dist

def _print_install(res: Any, out: Out) -> None:
    if out.as_json:
        out.data({"method": res.method, "files": res.files, "commands": res.commands, "notes": res.notes})
        return
    out.line(f"method: {res.method}")
    for f in res.files:
        out.line(f"file: {f}")
    for c in res.commands:
        out.line("run: " + " ".join(_shq(str(a)) for a in c))
    for n in res.notes:
        out.line(n)


def cmd_service_install(args: argparse.Namespace, out: Out) -> int:
    """``hive service install node|hub [--method …] [--system] [--dry-run] [--no-capture-path]``."""
    from . import install
    from .config import default_paths
    path = Path(os.path.expanduser(args.config)) if args.config else \
        default_paths()["node_config" if args.kind == "node" else "hub_config"]
    if not path.exists():
        raise CliError(f"no {args.kind} config at {path}", hint=f"run: hive init {args.kind}")
    res = install.install(args.kind, path, method=args.method, system=args.system, dry_run=args.dry_run,
                          capture_path=not args.no_capture_path)
    _print_install(res, out)
    return EXIT_OK


def cmd_service_uninstall(args: argparse.Namespace, out: Out) -> int:
    """``hive service uninstall node|hub [--stop-services]``."""
    from . import install
    res = install.uninstall(args.kind, method=args.method, system=args.system, dry_run=args.dry_run,
                            stop_services=args.stop_services)
    _print_install(res, out)
    return EXIT_OK


def cmd_service_status(args: argparse.Namespace, out: Out) -> int:
    """``hive service status node|hub``."""
    from . import install
    st = install.status(args.kind)
    if out.as_json:
        out.data(st)
        return EXIT_OK
    out.line(f"{args.kind}: " + (f"installed ({st.get('method')}{', system' if st.get('system') else ''})"
                                 if st.get("installed") else "not installed")
             + f"; {'running (pid ' + str(st.get('pid')) + ')' if st.get('running') else 'not running'}"
             + (f"; {st.get('active')}" if st.get("active") else ""))
    for f in st.get("files") or []:
        out.line(f"file: {f}")
    return EXIT_OK


def cmd_service_define(args: argparse.Namespace, out: Out) -> int:
    """``hive service define NAME --preset P [--model M] [--arg k=v …] | --argv '…' | --command '…' [--port N]
    [--endpoint URL] [--node N]``: a thin wrapper over the ``service.define`` action (v8.1)."""
    import shlex
    from . import presets
    params: Dict[str, Any] = {"name": args.name}
    pargs = list(args.arg or [])
    if args.preset:
        params["preset"] = args.preset
    if args.model:
        pre = presets.PRESETS.get(args.preset or "")
        names = [a["name"] for a in (pre.args if pre is not None else [])]
        key = next((k for k in ("model", "model_path") if k in names), None)
        if key is None:
            raise CliError("--model goes with --preset vllm, sglang or llamacpp",
                           hint="other presets: --arg key=value (hive guide presets lists them)")
        pargs.append(f"{key}={args.model}")
    if pargs:
        if not args.preset:
            raise CliError("--arg goes with --preset (preset arguments)", hint="hive guide presets")
        params["preset_args"] = pargs
    if args.argv:
        try:
            params["argv"] = shlex.split(args.argv, posix=os.name != "nt")
        except ValueError as e:
            raise CliError(f"--argv: {e}")
    if args.command:
        params["command"] = args.command
    if sum(1 for k in ("preset", "argv", "command") if k in params) != 1:
        raise CliError("give exactly one of --preset, --argv or --command",
                       hint="e.g. hive service define qwen --preset vllm --model Qwen/Qwen2.5-7B-Instruct")
    for k, v in (("port", args.port), ("endpoint", args.endpoint), ("cwd", args.cwd)):
        if v is not None:
            params[k] = v
    if args.autostart:
        params["autostart"] = True
    if args.no_start:
        params["start"] = False
    elif args.wait:
        params["wait"] = args.wait
    for kv in args.param or []:
        if "=" not in kv:
            raise CliError(f"--param expects key=value, got {kv!r}")
        k, v = kv.split("=", 1)
        params[k.strip()] = _parse_value(v)
    client = _connect(args, owner_default=True, node=args.node)
    try:
        res = client.call("service.define", params, node=args.node, approval_id=args.approval)
    except HiveError as e:
        out.error(e.code, e.message, e.hint, e.details if out.as_json else None)
        _approval_retry_hint(e)
        return exit_code_for(e)
    finally:
        client.close()
    if out.as_json:
        out.data(res)
        return EXIT_OK
    node = args.node or "self"
    cf = _conn_flag(args)
    verb = "defined" if res.get("created") else "updated"
    state = str(res.get("state") or "")
    out.foreign(f"service '{res.get('name')}' {verb} on {node}: state {state}, health "
                f"{res.get('health') or '-'}" + (f", port {res.get('port')}" if res.get("port") else ""))
    if res.get("endpoint"):
        out.foreign(f"endpoint (on that machine): {res.get('endpoint')}")
    for w in res.get("warnings") or []:
        out.foreign(f"warning: {w}")
    if state in ("backoff", "failed", "exited") and not args.no_start:
        # v8.1 (external review round 2): a service that crashed at once is a failure, not "defined"
        out.foreign(f"it did not start: {res.get('last_error') or 'the process exited'}", err=True)
        tail = [x for x in str(res.get("tail") or "").splitlines() if x.strip()][-8:]
        if tail:
            out.foreign("last output:\n" + "\n".join("  " + x for x in tail), err=True)
        sys.stderr.write(f"Hint: full log: hive logs {node} --service {args.name}{cf}; fix it and run the same "
                         "define again\n")
        return EXIT_ACTION
    out.line(f"watch it: hive logs {node} --service {args.name} -f{cf}   status: hive call service.status "
             f"name={args.name} wait=120" + (f" --node {args.node}" if args.node else "") + cf)
    if res.get("endpoint") or args.preset not in (None, "comfyui"):
        out.line(f"chat apps: hive gateway url {node} {args.name}" + ("" if args.node else " --node")
                 + f"{cf}   (README section 6.2)")
    return EXIT_OK


def cmd_guide(args: argparse.Namespace, out: Out) -> int:
    """``hive guide [TOPIC]``: the same how-to topics the agents' ``guide`` tool serves (v8.1)."""
    from . import guides
    if args.list_topics:
        out.line("topics: " + ", ".join(guides.TOPICS))
        return EXIT_OK
    try:
        text = guides.topic_text(args.topic or "overview")
    except HiveError as e:
        out.error(e.code, e.message, e.hint)
        return EXIT_USAGE
    if out.as_json:
        out.data({"topic": args.topic or "overview", "text": text})
        return EXIT_OK
    if (args.topic or "").strip().lower() == "presets":
        # v8.1 (external review round 2): the topic is written for agents' tool calls; map it to the CLI
        out.line(PRESETS_CLI_LINE)
        out.line("")
    out.line(text.rstrip("\n"))
    out.line("")
    out.line("topics: " + ", ".join(guides.TOPICS) + "   (hive guide TOPIC)")
    return EXIT_OK


PRESETS_CLI_LINE = ("From the command line: hive service define NAME --preset P [--model M] [--arg key=value …] "
                    "[--node N]; each preset_args entry 'key=value' below is one --arg key=value, and model=… is "
                    "--model … (e.g. hive service define qwen --preset vllm --model Qwen/Qwen2.5-7B-Instruct --arg "
                    "python=~/.venvs/vllm/bin/python).")


def cmd_tls_pin(args: argparse.Namespace, out: Out) -> int:
    """``hive tls-pin CERT.pem``: the ``sha256:…`` pin of a certificate (D51)."""
    import ssl
    from .transport import cert_pin
    try:
        text = Path(os.path.expanduser(args.cert)).read_text(encoding="ascii", errors="replace")
        der = ssl.PEM_cert_to_DER_cert(text)
    except (OSError, ValueError) as e:
        raise CliError(f"cannot read a PEM certificate from {args.cert}: {e}")
    print(cert_pin(der))
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace, out: Out) -> int:
    """``hive doctor [node|hub|client] [--no-network] [--deep]`` (exit 1 when a check fails). Without a kind every
    role configured here is checked: node and hub on a machine that has both (v8.1)."""
    from . import doctor
    from .config import default_paths
    if args.kind is not None:
        kinds = [args.kind]
    else:
        p = default_paths()
        kinds = [k for k in ("node", "hub") if p[f"{k}_config"].exists()] or ["client"]
    all_rows: List[dict] = []
    for kind in kinds:
        rows = doctor.run(kind, network=not args.no_network, deep=args.deep)
        if len(kinds) > 1:
            rows = [dict(r, target=kind) for r in rows]
        all_rows += rows
        if out.as_json:
            continue
        if len(kinds) > 1:
            out.line(f"== {kind} ==")
        for r in rows:
            mark = {"ok": "ok  ", "warn": "WARN", "fail": "FAIL"}.get(str(r.get("status")), "?   ")
            out.foreign(f"[{mark}] {r.get('check')}: {r.get('detail')}")
            if r.get("fix") and r.get("status") != "ok":
                out.foreign(f"       fix: {r.get('fix')}")
        n = {s: sum(1 for r in rows if r.get("status") == s) for s in ("ok", "warn", "fail")}
        out.line(f"{kind}: {n['ok']} ok, {n['warn']} warnings, {n['fail']} failures")
    if out.as_json:
        out.data(all_rows)
    return EXIT_ACTION if any(r.get("status") == "fail" for r in all_rows) else EXIT_OK


def _audit_dir(args: argparse.Namespace) -> Path:
    from .config import default_paths
    p = default_paths()
    if args.hub or (not args.node and not p["node_config"].exists() and p["hub_config"].exists()):
        cfg = _hub_config(args)
        return Path(cfg.data_dir) / "audit"
    cfg = _node_config(args)
    return _data_dir(cfg) / "audit"


def _audit_line(r: dict) -> str:
    ts = r.get("ts") or ""
    try:
        ts = util.iso(util.parse_iso(ts))
    except Exception:  # noqa: BLE001
        pass
    who = r.get("identity") or "-"
    if r.get("agent"):
        who += f"({r.get('agent')})"
    res = "ok" if r.get("ok") else (r.get("code") or "error")
    what = r.get("action") or ""
    extra = r.get("extra") if isinstance(r.get("extra"), dict) else {}
    tail = ""
    if extra:
        tail = " " + json.dumps(extra, ensure_ascii=False, separators=(",", ":"), default=str)[:300]
    return (f"{ts} {r.get('kind')} {who} {r.get('via') or '-'} {r.get('ip') or '-'} {r.get('node') or '-'} "
            f"{what} {res} {r.get('ms') or 0}ms{tail}")


def cmd_audit_tail(args: argparse.Namespace, out: Out) -> int:
    """``hive audit tail [-n 50] [--node|--hub] [--follow]`` (read locally; no remote action reads audit, D16)."""
    from .audit import AuditLog
    d = _audit_dir(args)
    log_ = AuditLog(d, enabled=False)
    rows = log_.tail(max(1, int(args.n)))
    seen = set()
    for r in rows:
        seen.add(r.get("id"))
        if out.as_json:
            print(json.dumps(r, ensure_ascii=True, default=str))
        else:
            out.foreign(_audit_line(r))
    if not args.follow:
        return EXIT_OK
    try:
        while True:
            _sleep(1.0)
            for r in log_.tail(200):
                if r.get("id") in seen:
                    continue
                seen.add(r.get("id"))
                if out.as_json:
                    print(json.dumps(r, ensure_ascii=True, default=str), flush=True)
                else:
                    out.foreign(_audit_line(r))
            if len(seen) > 100000:
                seen = set(list(seen)[-1000:])
    except KeyboardInterrupt:
        return EXIT_OK


def cmd_dist_build(args: argparse.Namespace, out: Out) -> int:
    """``hive dist build [--out hive.pyz]``."""
    from . import dist
    data = dist.build_pyz()
    path = Path(os.path.expanduser(args.out))
    util.atomic_write_bytes(path, data)
    try:
        os.chmod(str(path), 0o755)
    except OSError:
        pass
    out.line(f"{path} ({util.human_bytes(len(data))})")
    out.line(f"sha256 {dist.pyz_sha256(data)}")
    return EXIT_OK


# ---------------------------------------------------------------------------------------------------------------
# WP6b: push / pull / logs

class _Bar:
    """A one-line progress bar on stderr (≤ 5 updates/s)."""

    def __init__(self, label: str):
        self.label = label
        self.last = 0.0
        self.tty = sys.stderr.isatty()

    def __call__(self, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self.last < 0.2 and done < total:
            return
        self.last = now
        pct = (100.0 * done / total) if total else 100.0
        text = f"{self.label}: {util.human_bytes(done)}/{util.human_bytes(total)} ({pct:.0f}%)"
        sys.stderr.write(("\r" + text) if self.tty else (text + "\n"))
        sys.stderr.flush()

    def end(self) -> None:
        if self.tty:
            sys.stderr.write("\n")


def _transfer(args: argparse.Namespace, out: Out, src: str, dst: str) -> int:
    client = _connect(args, owner_default=True, node=_node_spec(args.node))
    bar = _Bar(f"{args.node}")
    try:
        res = client.transfer(src, dst, overwrite=args.overwrite, progress=bar)
    finally:
        bar.end()
        client.close()
    if out.as_json:
        out.data(res)
    else:
        out.line(f"{util.human_bytes(int(res.get('bytes') or 0))} copied in {float(res.get('duration_s') or 0):.1f}s"
                 + (f" (resumed from {util.human_bytes(int(res['resumed_from']))})" if res.get("resumed_from")
                    else "") + f", sha256 {res.get('sha256')}")
    return EXIT_OK


def _node_spec(n: str) -> str:
    return "self" if n in (".", "-", "local", "self") else n.lower()


def cmd_push(args: argparse.Namespace, out: Out) -> int:
    """``hive push NODE LOCAL REMOTE [--overwrite]`` (resumable; LocalFS unconfined)."""
    return _transfer(args, out, "local:" + os.path.abspath(os.path.expanduser(args.local)),
                     f"{_node_spec(args.node)}:{args.remote}")


def cmd_pull(args: argparse.Namespace, out: Out) -> int:
    """``hive pull NODE REMOTE LOCAL [--overwrite]``."""
    return _transfer(args, out, f"{_node_spec(args.node)}:{args.remote}",
                     "local:" + os.path.abspath(os.path.expanduser(args.local)))


def cmd_logs(args: argparse.Namespace, out: Out) -> int:
    """``hive logs NODE (JOB_ID | --service NAME) [-f] [--tail N] [--raw]``."""
    from .textlog import clean_terminal_text
    if bool(args.job_id) == bool(args.service):
        raise CliError("give a JOB_ID or --service NAME")
    node = _node_spec(args.node)
    client = _connect(args, owner_default=True, node=node)

    def show(text: str) -> None:
        if not text:
            return
        t = text if args.raw else clean_terminal_text(text)
        _write(sys.stdout, util.escape_terminal(t))
        sys.stdout.flush()
    try:
        action, key, ident = ("job.logs", "job_id", args.job_id) if args.job_id else \
            ("service.logs", "name", args.service)
        params: Dict[str, Any] = {key: ident, "tail_lines": max(1, min(5000, int(args.tail)))}
        if args.raw:
            params["raw"] = True
        r = client.call(action, params, node=node)
        show(r.get("text") or "")
        if not args.follow:
            return EXIT_OK
        offset = r.get("next_offset")
        while True:
            if args.job_id and r.get("state") in ("exited", "killed", "timeout", "failed", "lost") and \
                    not r.get("text"):
                return EXIT_OK
            p = {key: ident, "wait": 10}
            if isinstance(offset, int):
                p["offset"] = offset
            if args.raw:
                p["raw"] = True
            r = client.call(action, p, node=node)
            show(r.get("text") or "")
            if isinstance(r.get("next_offset"), int):
                offset = r["next_offset"]
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        client.close()


# ---------------------------------------------------------------------------------------------------------------
# parser

def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--connection", default=argparse.SUPPRESS, help="client.json connection")
    p.add_argument("-v", "--verbose", action="store_true", default=argparse.SUPPRESS)


def _jsonflag(p: argparse.ArgumentParser) -> None:
    p.add_argument("--json", dest="as_json", action="store_true", default=argparse.SUPPRESS,
                   help="machine output (errors as JSON on stderr)")


def build_parser() -> argparse.ArgumentParser:
    """The ``hive`` argument parser (§19)."""
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
    au = p.add_mutually_exclusive_group()
    au.add_argument("--auto-update", dest="auto_update", action="store_const", const=True, default=None,
                    help="pods: the hub may update this node (hive hub restart-node --outdated)")
    au.add_argument("--no-auto-update", dest="auto_update", action="store_const", const=False)
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
    p = leaf(tok, "set", cmd_node_token_set, help="change a token's approval rule (the token stays the same)")
    p.add_argument("name")
    p.add_argument("--cautious", action="store_true", help="its risky calls wait for approval")
    p.add_argument("--no-cautious", action="store_true", help="no approvals")
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

    # ---- WP6b
    p = leaf(sub, "join", cmd_join, help="join a hub as a relay node (join code from HIVE_JOIN, an argument or -)")
    p.add_argument("code", nargs="?", help="join code (default: env HIVE_JOIN; - reads stdin)")
    p.add_argument("--name")
    p.add_argument("--preset", choices=["readonly", "standard", "cautious", "full"])
    p.add_argument("--relay-role", choices=["readonly", "files", "operator"])
    p.add_argument("--relay-proxy", help="URL or off (default: env HIVE_RELAY_PROXY, else auto)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--pod", action="store_true")
    g.add_argument("--no-pod", action="store_true")
    p.add_argument("--wsl", action="store_true")
    p.add_argument("--mirror", choices=["cn", "none"])
    p.add_argument("--proxy", help="URL or off (default: env HIVE_PROXY)")
    p.add_argument("--models-dir")
    p.add_argument("--install", action="store_true", help="also install the node as a service")
    p.add_argument("--force", action="store_true")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--no-restart", action="store_true")

    hub = sub.add_parser("hub", help="the hub on this computer").add_subparsers(dest="what")
    p = leaf(hub, "run", cmd_hub_run, help="run the hub in the foreground")
    p.add_argument("--config")
    p.add_argument("--relaunched", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--supervise", action="store_true", help=argparse.SUPPRESS)
    for name, fn in (("stop", cmd_hub_stop), ("restart", cmd_hub_restart), ("status", cmd_hub_status),
                     ("list", cmd_hub_list)):
        p = leaf(hub, name, fn)
        p.add_argument("--config")
    p = leaf(hub, "add-node", cmd_hub_add_node, help="add a relay or direct node")
    p.add_argument("name")
    p.add_argument("--relay", action="store_true")
    p.add_argument("--via", choices=["tailnet", "public"])
    p.add_argument("--host", help="the hub address remote machines use (when it listens on a wildcard/loopback)")
    p.add_argument("--direct", metavar="URL")
    p.add_argument("--token-file")
    p.add_argument("--token-env")
    p.add_argument("--token")
    p.add_argument("--generate", action="store_true", help="create the hub token here; print its hash for the node")
    p.add_argument("--tls-pin")
    p.add_argument("--tags")
    p.add_argument("--description")
    p.add_argument("--config")
    p.add_argument("--no-restart", action="store_true")
    p = leaf(hub, "add-agent", cmd_hub_add_agent, help="add an agent token")
    p.add_argument("name")
    p.add_argument("--role", choices=["readonly", "files", "operator"])
    p.add_argument("--nodes", help="'*' or a comma-separated list")
    p.add_argument("--deny")
    p.add_argument("--approval")
    p.add_argument("--cautious", action="store_true")
    p.add_argument("--expires")
    p.add_argument("--config")
    p.add_argument("--no-restart", action="store_true")
    p = leaf(hub, "set-agent", cmd_hub_set_agent, help="change an agent token's approval rule")
    p.add_argument("name")
    p.add_argument("--cautious", action="store_true", help="its risky calls wait for approval")
    p.add_argument("--no-cautious", action="store_true", help="no approvals")
    p.add_argument("--config")
    p.add_argument("--no-restart", action="store_true")
    for name, fn in (("remove-node", cmd_hub_remove_node), ("remove-agent", cmd_hub_remove_agent),
                     ("rotate-token", cmd_hub_rotate_token)):
        p = leaf(hub, name, fn)
        p.add_argument("name")
        p.add_argument("--config")
        p.add_argument("--no-restart", action="store_true")
        if name == "rotate-token":
            p.add_argument("--via", choices=["tailnet", "public"])
            p.add_argument("--host")
    p = leaf(hub, "ack-node", cmd_hub_ack_node, help="accept a relay node's new instance key")
    p.add_argument("name")
    p.add_argument("--yes", action="store_true")
    p.add_argument("--config")
    p = leaf(hub, "restart-node", cmd_hub_restart_node, help="restart (and auto-update) relay nodes")
    p.add_argument("name", nargs="?")
    p.add_argument("--outdated", action="store_true")
    p.add_argument("--config")

    gw = sub.add_parser("gateway", help="the OpenAI-compatible model gateway").add_subparsers(dest="what")
    gtok = gw.add_parser("token", help="gateway (models) tokens").add_subparsers(dest="what2")
    p = leaf(gtok, "add", cmd_gateway_token_add)
    p.add_argument("name")
    p.add_argument("--models", required=True, help="'node/service,…' on a hub; 'service,…' on a node")
    p.add_argument("--expires")
    for q in (p,):
        q.add_argument("--hub", action="store_true")
        q.add_argument("--node", action="store_true")
        q.add_argument("--config")
        q.add_argument("--no-restart", action="store_true")
    p = leaf(gtok, "list", cmd_gateway_token_list)
    p.add_argument("--hub", action="store_true")
    p.add_argument("--node", action="store_true")
    p.add_argument("--config")
    p = leaf(gtok, "revoke", cmd_gateway_token_revoke)
    p.add_argument("name")
    p.add_argument("--hub", action="store_true")
    p.add_argument("--node", action="store_true")
    p.add_argument("--config")
    p.add_argument("--no-restart", action="store_true")
    p = leaf(gw, "url", cmd_gateway_url, help="print the Base URLs for an app")
    p.add_argument("node_name", metavar="NODE")
    p.add_argument("service", metavar="SERVICE")
    p.add_argument("--token-file", help="a gateway token file: also list the served model ids")
    p.add_argument("--node", action="store_true", help="this node's own gateway (http://127.0.0.1:<port>)")
    p.add_argument("--config")

    ap_ = sub.add_parser("approvals", help="approve or deny gated calls").add_subparsers(dest="what")
    for name, fn in (("list", cmd_approvals_list), ("grants", cmd_approvals_grants)):
        p = leaf(ap_, name, fn)
        g = p.add_mutually_exclusive_group()
        g.add_argument("--node", action="store_true")
        g.add_argument("--hub", action="store_true")
    for name, fn in (("approve", cmd_approvals_approve), ("deny", cmd_approvals_deny)):
        p = leaf(ap_, name, fn)
        p.add_argument("id")
        if name == "approve":
            p.add_argument("--for", dest="for_", metavar="DURATION", help="also allow this action for a while, "
                                                                          "e.g. 30m")
        p.add_argument("--yes", action="store_true")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--node", action="store_true")
        g.add_argument("--hub", action="store_true")
    p = leaf(ap_, "revoke", cmd_approvals_revoke)
    p.add_argument("id", nargs="?")
    p.add_argument("--all", action="store_true")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--node", action="store_true")
    g.add_argument("--hub", action="store_true")

    p = leaf(sub, "mcp-config", cmd_mcp_config, help="print (or write) an MCP client config snippet")
    p.add_argument("--client", choices=["claude-desktop", "claude-code", "cursor", "generic"])
    p.add_argument("--local", action="store_true")
    p.add_argument("--remote", action="store_true")
    p.add_argument("--write", action="store_true")
    p.add_argument("--allow-owner", action="store_true")
    p.add_argument("--remove", action="store_true", help="take Hive out of the app's MCP config (uninstall)")

    svc = sub.add_parser("service", help="start the node/hub with the OS").add_subparsers(dest="what")
    p = leaf(svc, "install", cmd_service_install)
    p.add_argument("kind", choices=["node", "hub"])
    p.add_argument("--method", choices=["auto", "systemd", "launchd", "schtasks", "watchdog"], default="auto")
    p.add_argument("--system", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--no-capture-path", action="store_true")
    p.add_argument("--config")
    p = leaf(svc, "uninstall", cmd_service_uninstall)
    p.add_argument("kind", choices=["node", "hub"])
    p.add_argument("--stop-services", action="store_true")
    p.add_argument("--method", choices=["auto", "systemd", "launchd", "schtasks", "watchdog"], default="auto")
    p.add_argument("--system", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = leaf(svc, "status", cmd_service_status)
    p.add_argument("kind", choices=["node", "hub"])
    p = leaf(svc, "define", cmd_service_define, help="define (and start) a model server or other long-running "
                                                      "service on a node (the service.define action)")
    p.add_argument("name", help="service name, e.g. qwen")
    p.add_argument("--preset", choices=["vllm", "sglang", "ollama", "llamacpp", "comfyui", "docker"],
                   help="built-in recipe (hive guide presets)")
    p.add_argument("--model", help="model folder / HF repo id (vllm, sglang) or GGUF file (llamacpp)")
    p.add_argument("--arg", action="append", metavar="KEY=VALUE", help="preset argument (repeatable)")
    p.add_argument("--argv", help="the program and its arguments as one string, e.g. 'python3 server.py 8000'")
    p.add_argument("--command", help="a shell command line instead of --argv")
    p.add_argument("--port", type=int)
    p.add_argument("--endpoint", help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1")
    p.add_argument("--cwd")
    p.add_argument("--autostart", action="store_true", help="start again after a reboot")
    p.add_argument("--no-start", action="store_true")
    p.add_argument("--wait", type=float, default=30.0, help="seconds to wait for it to come up (default 30)")
    p.add_argument("--param", action="append", metavar="KEY=VALUE", help="any other service.define parameter")
    p.add_argument("--node", help="target node (through a hub connection)")
    p.add_argument("--approval", metavar="AP_ID", help="retry an approved call")
    p = leaf(sub, "guide", cmd_guide, help="how-to topics (the same text agents get from the guide tool)")
    p.add_argument("topic", nargs="?", help="overview (default), model-serving, presets, gateway, windows, …")
    p.add_argument("--list", dest="list_topics", action="store_true")

    p = leaf(sub, "tls-pin", cmd_tls_pin, json_flag=False, help="print the sha256 pin of a PEM certificate")
    p.add_argument("cert")
    p = leaf(sub, "doctor", cmd_doctor, help="check this machine's setup")
    p.add_argument("kind", nargs="?", choices=["node", "hub", "client"])
    p.add_argument("--no-network", action="store_true")
    p.add_argument("--deep", action="store_true")
    aud = sub.add_parser("audit", help="read the local audit log").add_subparsers(dest="what")
    p = leaf(aud, "tail", cmd_audit_tail)
    p.add_argument("-n", type=int, default=50)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--node", action="store_true")
    g.add_argument("--hub", action="store_true")
    p.add_argument("--follow", "-f", action="store_true")
    p.add_argument("--config")
    dst = sub.add_parser("dist", help="distribution").add_subparsers(dest="what")
    p = leaf(dst, "build", cmd_dist_build)
    p.add_argument("--out", default="hive.pyz")

    p = leaf(sub, "push", cmd_push, help="copy a local file to a node (resumable)")
    p.add_argument("node")
    p.add_argument("local")
    p.add_argument("remote")
    p.add_argument("--overwrite", action="store_true")
    p = leaf(sub, "pull", cmd_pull, help="copy a file from a node (resumable)")
    p.add_argument("node")
    p.add_argument("remote")
    p.add_argument("local")
    p.add_argument("--overwrite", action="store_true")
    p = leaf(sub, "logs", cmd_logs, json_flag=False, help="show or follow a job's or service's log")
    p.add_argument("node")
    p.add_argument("job_id", nargs="?")
    p.add_argument("--service")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--tail", type=int, default=100)
    p.add_argument("--raw", action="store_true")
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point (``hive = hive.cli:main``). v8.1 (external review round 2): output piped into a reader that
    stops early (``hive node status | head -1``) ends quietly instead of a BrokenPipeError traceback."""
    try:
        rc = _main(argv)
        sys.stdout.flush()
        return rc
    except BrokenPipeError:
        _silence_stdout()
        return EXIT_OK


def _silence_stdout() -> None:
    """Point stdout at devnull so the interpreter's final flush does not raise again (Python docs, SIGPIPE)."""
    try:
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, sys.stdout.fileno())
        os.close(fd)
    except (OSError, ValueError, AttributeError):
        pass


def _main(argv: Optional[List[str]] = None) -> int:
    util.prune_cwd_path()
    tolerant_streams()
    args_list = list(sys.argv[1:] if argv is None else argv)
    global _MAIN_ARGV
    _MAIN_ARGV = list(args_list)
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
