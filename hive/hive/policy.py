"""Node policy: Policy, EffectivePolicy (PathGuard, CommandGuard, kill guard) and Limits (design §5.3–§5.5, WP1).

``Policy`` is built once per config load (immutable; config changes restart the daemon, D16). ``Policy.effective()``
returns the per-identity ``EffectivePolicy`` that every handler consults: the §5.3 authorization formula, approvals,
``resolve_path`` ("PathGuard", the binding §5.4 step order incl. hard denies, ``deny_write`` exec paths and file
holds), ``check_command`` ("CommandGuard"), the child environment, the identity-dependent URL rules 5a/5c on top of
``netutil.NetGuard`` (WP3, reached through a function-level import), rule 5b (``auth_spec(…, method=)``) and the kill
guard. NetGuard lives in ``netutil.py``.
"""
from __future__ import annotations

import dataclasses
import logging
import os
import re
import shlex
import shutil
import site
import sys
import sysconfig
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union
from urllib.parse import urlsplit

from . import auth as _auth
from .errors import HiveError, bad_request, conflict, denied, forbidden, internal, not_found, too_large
from .util import IS_MAC, IS_WINDOWS, glob_match, match_deny, path_key

if TYPE_CHECKING:  # pragma: no cover
    from .approvals import ApprovalStore, FileHolds
    from .auth import Identity
    from .catalog import ActionSpec
    from .config import GatewayConfig, ModelsConfig, NetworkConfig, PolicyConfig
    from .secretstore import SecretStore

log = logging.getLogger("hive.policy")


@dataclass(frozen=True)
class Limits:
    """The only source of limits for handlers (§5.4); built by Policy from the policy blocks + models + gateway."""
    max_read_bytes: int = 8388608
    max_write_bytes: int = 16777216
    max_chunk_bytes: int = 8388608
    max_delete_entries: int = 10000
    exec_default_timeout: float = 60.0
    exec_max_timeout: float = 3600.0
    max_output_bytes: int = 1048576
    jobs_max_running: int = 32
    jobs_max_runtime: float = 0.0
    services_max: int = 32
    allow_autostart: bool = True
    max_response_bytes: int = 8388608
    max_concurrent_pulls: int = 2
    max_concurrent_calls: int = 32
    max_call_seconds: float = 3700.0
    max_result_bytes: int = 33554432
    gateway_max_body_bytes: int = 8388608
    gateway_max_response_bytes: int = 67108864


# ---------------------------------------------------------------------------------------------------------------
# built-in default lists (§5.5; versioned tuples)

DEFAULT_SECRET_PATHS: Tuple[str, ...] = (
    "~/.ssh/**", "~/.gnupg/**", "~/.aws/**", "~/.azure/**", "~/.config/gcloud/**", "~/.kube/config",
    "~/.docker/config.json", "~/.netrc", "~/.git-credentials", "~/.pypirc", "~/.npmrc", "~/.cache/huggingface/token",
    "~/.cache/huggingface/stored_tokens", "~/.huggingface/token", "~/.mozilla/**", "~/.config/google-chrome/**",
    "~/.config/chromium/**", "~/Library/Application Support/Google/Chrome/**",
    "~/Library/Application Support/Firefox/Profiles/**", "~/Library/Application Support/Microsoft Edge/**",
    "~/Library/Keychains/**", "~/AppData/Local/Google/Chrome/User Data/**",
    "~/AppData/Local/Microsoft/Edge/User Data/**", "~/AppData/Roaming/Mozilla/Firefox/Profiles/**",
    "~/AppData/Roaming/Microsoft/Credentials/**", "~/AppData/Local/Microsoft/Credentials/**",
    "~/AppData/Roaming/Microsoft/Protect/**", "~/AppData/Local/Microsoft/Vault/**",
    "~/AppData/Roaming/Microsoft/Windows/PowerShell/PSReadLine/**", "~/.claude.json", "~/.claude/**", "~/.codex/**",
    "~/.cursor/**", "~/.config/gh/hosts.yml", "~/.ollama/id_ed25519", "~/.bash_history", "~/.zsh_history",
    "~/.local/share/keyrings/**", "~/.password-store/**", "~/.vault-token", "~/.terraform.d/credentials*",
    "~/.config/rclone/**", "~/.runpod/**", "~/.modelscope/**", "/etc/shadow", "/etc/sudoers*",
)

DEFAULT_EXEC_PATHS: Tuple[str, ...] = (
    "~/.bashrc", "~/.bash_profile", "~/.bash_login", "~/.bash_aliases", "~/.bash_logout", "~/.profile", "~/.zshrc",
    "~/.zshenv", "~/.zprofile", "~/.zlogin", "~/.config/fish/**", "~/.config/autostart/**",
    "~/.config/systemd/user/**", "~/.local/share/systemd/user/**", "~/.config/environment.d/**", "~/.wslconfig",
    "~/.config/pip/**", "~/.pip/**", "~/AppData/Roaming/pip/**", "~/.local/bin/**", "~/bin/**",
    "~/.local/share/applications/**", "~/.ssh/rc", "~/Library/LaunchAgents/**",
    "~/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/**", "~/Documents/PowerShell/**",
    "~/Documents/WindowsPowerShell/**", "~/OneDrive*/Documents/PowerShell/**",
    "~/OneDrive*/Documents/WindowsPowerShell/**", "~/.config/powershell/**", "~/.xprofile", "~/.xsessionrc",
    "~/.xinitrc", "~/.condarc", "~/.config/uv/**", "**/.git/hooks/**", "**/.git/config", "~/.gitconfig",
    "~/.config/git/**", "**/*.pth", "**/sitecustomize.py", "**/usercustomize.py", "**/site-packages/**",
    "**/dist-packages/**", "**/*.lnk", "~/AppData/Local/Packages/Microsoft.WindowsTerminal_*/LocalState/**",
    "~/.vimrc", "~/.config/nvim/**", "~/.ipython/**/startup/**", "~/.jupyter/**", "**/etc/conda/activate.d/**",
    "**/.vscode/tasks.json", "**/.envrc", "~/AppData/Local/Programs/**", "~/AppData/**/*.exe", "~/AppData/**/*.dll",
    "~/Applications/**",
    # v8.1 (external review): more autoruns a gated token could plant (sourced rc folders, login/session scripts,
    # Jupyter kernel specs, editor/terminal/WM configs that run commands, VCS and package-manager hooks)
    "~/.local/share/jupyter/**", "~/Library/Jupyter/**", "~/AppData/Roaming/jupyter/**", "~/.bashrc.d/**",
    "~/.zshrc.d/**", "~/.zlogout", "~/.oh-my-zsh/custom/**", "~/.xsession", "~/.pam_environment",
    "~/.config/plasma-workspace/**", "~/.kde*/Autostart/**", "~/.kde*/env/**", "~/.config/i3/**",
    "~/.config/sway/**", "~/.config/hypr/**", "~/.Rprofile", "~/.Renviron", "~/.gdbinit", "~/.emacs",
    "~/.emacs.d/**", "~/.config/emacs/**", "~/.vim/**", "~/.tmux.conf", "~/.config/tmux/**", "~/.wezterm.lua",
    "~/.config/wezterm/**", "~/.hgrc", "**/.hg/hgrc", "**/.yarnrc.yml", "~/.config/direnv/**",
    "~/.config/systemd/*.conf", "~/.config/systemd/*.conf.d/**",
    # v8.1 (external review, round 2): toolchain homes whose bin/ or shims/ lead PATH, files every shell sources,
    # terminal and window-manager configs that name the program to start, REPL startup files, editor extensions and
    # the pyvenv.cfg of any venv (its home= key picks the interpreter)
    "~/.cargo/**", "~/.rustup/**", "~/go/bin/**", "~/.bun/**", "~/.deno/**", "~/.npm-global/**", "~/.nvm/**",
    "~/.volta/**", "~/.local/share/pnpm/**", "~/.pyenv/**", "~/.rbenv/**", "~/.sdkman/**", "~/miniconda*/**",
    "~/anaconda*/**", "~/miniforge*/**", "~/mambaforge*/**", "~/.conda/**",
    "~/.oh-my-zsh/**", "~/.config/zsh/**", "~/.zsh/**", "~/.config/starship.toml", "~/.tcshrc", "~/.cshrc",
    "~/.login", "~/.kshrc", "~/.xonshrc", "~/.config/xonsh/**", "~/.config/nushell/**",
    "~/.config/alacritty/**", "~/.alacritty.toml", "~/.alacritty.yml", "~/.config/kitty/**", "~/.config/foot/**",
    "~/.config/ghostty/**", "~/Library/Application Support/com.mitchellh.ghostty/**", "~/.local/share/konsole/**",
    "~/.config/openbox/**", "~/.config/labwc/**", "~/.config/river/**", "~/.config/bspwm/**", "~/.config/sxhkd/**",
    "~/.config/awesome/**", "~/.config/qtile/**", "~/.config/niri/**", "~/.config/herbstluftwm/**",
    "~/.config/xmonad/**", "~/.xmonad/**", "~/.config/xfce4/xinitrc", "~/.config/lxsession/**",
    "~/.julia/config/**", "~/.octaverc", "~/.ghci", "~/.irbrc", "~/.pryrc", "~/.psqlrc", "~/.sqliterc",
    "~/.lldbinit", "~/.mailcap", "~/.vscode/extensions/**", "~/.vscode-server/extensions/**", "**/pyvenv.cfg",
    # v8.1 (audit round 2): submodule git dirs (their config sets core.hooksPath/fsmonitor and holds hooks), the
    # repo-local attributes file (filter/diff drivers), build-tool init scripts, D-Bus session service files and
    # VS Code workspace/user settings and launch configs (interpreter paths, terminal profiles, tasks on open)
    "**/.git/modules/**", "**/.git/info/attributes", "~/.mavenrc", "~/.gradle/init.gradle", "~/.gradle/init.d/**",
    "~/.local/share/dbus-1/services/**", "**/.vscode/settings.json", "**/.vscode/launch.json",
    "~/.config/Code*/User/**", "~/Library/Application Support/Code*/User/**", "~/AppData/Roaming/Code*/User/**",
    # … and the same one step further: VSCodium/Cursor user settings, per-worktree git config, the user-wide Gradle
    # properties (org.gradle.jvmargs can add a -javaagent) and project .npmrc (script-shell, node-options)
    "~/.config/VSCodium/User/**", "~/Library/Application Support/VSCodium/User/**",
    "~/AppData/Roaming/VSCodium/User/**", "~/.config/Cursor/User/**", "~/Library/Application Support/Cursor/User/**",
    "~/AppData/Roaming/Cursor/User/**", "**/.git/config.worktree", "**/.git/worktrees/*/config.worktree",
    "~/.gradle/gradle.properties", "**/.npmrc",
    # v8.1 (audit round 2, verification): Python bytecode (a planted tool.pyc or __pycache__/tool.<tag>.pyc runs in
    # place of a bound tool.py) and a gitfile (.git as a file whose "gitdir:" points at a folder with its own config)
    "**/__pycache__/**", "**/*.pyc", "**/.git",
)

DEFAULT_COMMAND_DENY: Tuple[str, ...] = (
    r"\brm\s+(-\S*\s+)*-\S*[rR]\S*\s+(-\S*\s+)*(/|~|\$HOME|/\*)\s*($|[;&|])",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\b[^\n]*\bof=/dev/(sd|hd|nvme|disk|mmcblk)",
    r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:",
    r"(^|[;&|(]\s*)(sudo\s+)?(shutdown|reboot|halt|poweroff)\b|\bsystemctl\s+(reboot|poweroff|halt)\b"
    r"|\b(Stop-Computer|Restart-Computer)\b",
    r"\bformat(\.com)?\s+[a-z]:",
    r"\b(diskpart|bcdedit|vssadmin\s+delete|cipher\s+/w)\b",
    r"\b(Clear-Disk|Format-Volume|Initialize-Disk)\b",
    r"\breg(\.exe)?\s+delete\s+HK(LM|EY_LOCAL_MACHINE)",
    r"\bRemove-Item\b[^\n]*-Recurse[^\n]*\s[A-Za-z]:\\?\s*($|[;|])",
    r"\bchmod\s+(-\S+\s+)*[0-7]{3,4}\s+/\s*$",
    r"[\\/]\.hive([\\/]|\s|$)",
    r"\bdocker\b[^\n]*\s(--privileged|--pid[\s=]+host|--userns[\s=]+host)\b",
    r"\bdocker\b[^\n]*\s(-v|--volume)[\s=]+[\"']?/:",
)

DEFAULT_KILL_PROTECT: Tuple[str, ...] = (
    "systemd", "init", "launchd", "kernel_task", "WindowServer", "loginwindow", "sshd", "tailscaled", "csrss.exe",
    "wininit.exe", "winlogon.exe", "services.exe", "lsass.exe", "smss.exe", "svchost.exe", "explorer.exe", "dwm.exe",
)

PATH_VARS = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "ProgramFiles", "ProgramFiles(x86)",
             "ProgramData", "SystemDrive", "SystemRoot", "WSL_DISTRO_NAME")
_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_()]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)|%([A-Za-z_][A-Za-z0-9_() ]*)%")
_RESERVED = {"con", "prn", "aux", "nul"} | {f"com{i}" for i in range(1, 10)} | {f"lpt{i}" for i in range(1, 10)}
_GLOB_CHARS = set("*?[")
WRITE_METHODS_SAFE = ("GET", "HEAD", "OPTIONS")
OPENAI_SUFFIXES = ("/models", "/chat/completions", "/completions", "/embeddings")
_HIVE_CMD_RE = re.compile(r"(?i)(-m\s+hive\b|hive\.pyz|[\\/]hive(\.exe)?\b|\bhive(\.exe)?\b)[^\n]*?\b"
                          r"(node|hub|mcp|_shim|_download)\b")
MAX_WALK = 100000
SECRET_HINT = "Do not try to work around node policy; ask the user."
EXEC_PATH_HINT = "writing this file would make it run code later; ask the owner"


# ---------------------------------------------------------------------------------------------------------------
# helpers

def _escape_glob(s: str) -> str:
    return "".join("[" + c + "]" if c in _GLOB_CHARS else c for c in s)


def _literal_prefix(pattern: str) -> str:
    """The part of a (normalized) pattern before its first wildcard component."""
    parts = pattern.split("/")
    out = []
    for p in parts:
        if any(c in _GLOB_CHARS for c in p):
            break
        out.append(p)
    return "/".join(out)


_CURRENT_USER: List[Optional[str]] = []


def _current_user() -> Optional[str]:
    """The node's own account name (cached): POSIX from the uid (not $USER), Windows ``getpass.getuser()``."""
    if not _CURRENT_USER:
        name: Optional[str] = None
        try:
            if IS_WINDOWS:  # pragma: no cover - Windows only
                import getpass
                name = getpass.getuser()
            else:
                import pwd
                name = pwd.getpwuid(os.getuid()).pw_name
        except Exception:  # noqa: BLE001
            name = None
        _CURRENT_USER.append(name or None)
    return _CURRENT_USER[0]


def _user_key(user: str) -> str:
    """Comparable account name: ``DOMAIN\\user`` / ``user@domain`` stripped, case-insensitive on Windows."""
    u = user.strip()
    if "\\" in u:
        u = u.rsplit("\\", 1)[1]
    if IS_WINDOWS:  # pragma: no cover - Windows only
        u = u.split("@", 1)[0].casefold()
    return u


def _is_wsl_drive_path(p: str) -> bool:
    return re.match(r"(?i)^/mnt/([a-z*]|\[[^\]]*\])(/|$)", p.replace("\\", "/")) is not None


def windows_string_checks(s: str, *, unc_allowed: bool = False) -> None:
    """PathGuard step 3 (Windows): ``\\\\?\\``/``\\\\.\\`` prefixes, UNC, ADS ``:``, device names, trailing dots/spaces."""
    t = s.replace("/", "\\")
    if t.startswith("\\\\?\\") or t.startswith("\\\\.\\"):
        raise denied(f"path form not allowed: {s!r}", rule="path_form", hint="use a normal drive path like D:\\work")
    if t.startswith("\\\\") and not unc_allowed:
        raise denied(f"network (UNC) paths are not allowed: {s!r}", rule="unc",
                     hint="only local paths inside the node's roots are allowed")
    body = t[2:] if re.match(r"^[A-Za-z]:", t) else t
    if ":" in body:
        raise denied(f"':' is only allowed after a drive letter (alternate data streams are refused): {s!r}",
                     rule="ads")
    for comp in body.split("\\"):
        if not comp or comp in (".", ".."):
            continue
        if comp != comp.rstrip(" ."):
            raise denied(f"path components may not end with a dot or space: {comp!r}", rule="trailing_dot")
        if comp.split(".", 1)[0].lower().rstrip(" ") in _RESERVED:
            raise denied(f"reserved device name in path: {comp!r}", rule="device_name")


def _long_path_name(p: str) -> str:  # pragma: no cover - Windows only
    import ctypes
    from ctypes import wintypes
    fn = ctypes.windll.kernel32.GetLongPathNameW  # type: ignore[attr-defined]
    fn.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    fn.restype = wintypes.DWORD
    n = fn(p, None, 0)
    if n == 0:
        raise denied(f"could not expand the short (8.3) path name {p!r}", rule="short_name")
    buf = ctypes.create_unicode_buffer(n + 1)
    if fn(p, buf, n + 1) == 0:
        raise denied(f"could not expand the short (8.3) path name {p!r}", rule="short_name")
    return buf.value


@dataclass(frozen=True)
class _Root:
    display: str
    mode: str
    star: bool
    keys: Tuple[str, ...]      # normalized keys: configured expanded form and realpath form
    real: str


class Policy:
    """Immutable node policy built once per config load (§5.4)."""

    def __init__(self, cfg: "PolicyConfig", *, workdir: Path, data_dir: Path, hive_home: Path,
                 protected: Tuple[List[Path], List[Path]], network: "NetworkConfig", models: "ModelsConfig",
                 secrets: "SecretStore", gateway: Optional["GatewayConfig"] = None, is_wsl: bool = False,
                 model_endpoints: Sequence[dict] = (), own_ports: Sequence[int] = ()):
        self.cfg = cfg
        self.secrets = secrets
        self.is_wsl = bool(is_wsl)
        self.casefold = IS_WINDOWS or IS_MAC
        self.warnings: List[str] = []
        self.preset = getattr(cfg, "preset", "standard")
        self.read_only = bool(cfg.read_only)
        # an explicit [] allows nothing (fail closed); only a missing value means the default ["*"]
        self.allow_actions = tuple(cfg.allow_actions if cfg.allow_actions is not None else ("*",))
        self.deny_actions = tuple(cfg.deny_actions or ())
        fs = dict(cfg.fs or {})
        ex = dict(cfg.exec or {})
        self.fs_cfg, self.exec_cfg = fs, ex
        self.net_cfg = dict(cfg.net or {})
        self.proc_cfg = dict(cfg.proc or {})
        home = os.path.expanduser("~")
        if not home or home.startswith("~") or not os.path.isabs(home) or not os.path.isdir(home):
            raise internal(f"the home directory does not expand to an existing absolute folder ({home!r})",
                           hint="the node's environment is broken (HOME/USERPROFILE); refusing to start")
        self.home = home
        self.home_key = self._norm(home)
        self.data_dir = Path(os.path.realpath(str(data_dir)))
        self.hive_home = Path(os.path.expanduser(str(hive_home)))
        # roots
        self._roots: List[_Root] = []
        roots_cfg = fs.get("roots")
        if roots_cfg is None:
            roots_cfg = [{"path": "~", "mode": "rw"}]
        elif not roots_cfg:  # an explicit [] never widens to the home folder (fail closed, loudly)
            raise bad_request("policy.fs.roots is empty: no folder would be reachable",
                              hint="list at least one root, e.g. {\"path\": \"~\", \"mode\": \"ro\"}, or remove the key "
                                   "for the default")
        for r in roots_cfg:
            self._roots.append(self._make_root(str(r.get("path")), r.get("mode") or "rw"))
        self.unc_roots = any(rt.display.replace("/", "\\").startswith("\\\\") for rt in self._roots if not rt.star)
        # workdir
        wd = self._expand_config_path(str(workdir))
        self._workdir = Path(os.path.realpath(wd))
        if not self._contained(self._norm(str(self._workdir)), write=False):
            raise internal(f"workdir {self._workdir} is outside the fs roots",
                           hint="set workdir to a folder inside policy.fs.roots")
        # deny lists
        self.deny = self._patterns(fs.get("deny") or (), remap_wsl=True)
        self.deny_write_cfg = self._patterns(fs.get("deny_write") or (), remap_wsl=True)
        self._live_alts: Tuple[float, List[str]] = (-1e9, [])
        self.has_deny_write = bool(fs.get("deny_write"))
        # hard denies
        self.hard_deny_all: List[str] = []
        self.hard_deny_write: List[str] = []
        self._build_hard_denies(protected)
        # node PATH
        prepend = [os.path.expanduser(p) for p in ex.get("path_prepend") or []]
        self.node_path = os.pathsep.join(prepend + [os.environ.get("PATH", "")])
        path_dirs = [d for d in self.node_path.split(os.pathsep) if d]
        trusted: List[str] = []
        self.path_deny: List[str] = []
        for d in path_dirs:
            if not os.path.isabs(d):
                continue
            key = self._norm(d)
            if self._in_rw_root(key):
                if self.has_deny_write:
                    if self._is_root_or_ancestor(key):
                        self._warn(f"PATH entry {d} is your home, a root or an ancestor of one; not protected")
                    else:
                        self.path_deny.append(_escape_glob(key) + "/**")
                continue
            if d not in trusted:
                trusted.append(d)
        if IS_WINDOWS and self.has_deny_write:  # pragma: no cover - Windows only
            self.path_deny.append(_escape_glob(self._norm(os.path.join(home, "AppData", "Local", "Microsoft",
                                                                       "WindowsApps"))) + "/**")
        self.trusted_path = os.pathsep.join(trusted)
        # exec
        self.exec_enabled = bool(ex.get("enabled", True))
        self.exec_mode = str(ex.get("mode", "open"))
        self.shells = tuple(ex.get("shells") or ("auto",))
        self.command_deny = [re.compile(p, re.IGNORECASE) for p in ex.get("command_deny") or ()]
        self.command_allow = [re.compile(p, re.IGNORECASE) for p in ex.get("command_allow") or ()]
        self.env_allow = tuple(ex.get("env_allow") or ())
        self.scrub_env = tuple(ex.get("scrub_env") or ("HIVE_*",))
        self.child_env_defaults = dict(ex.get("child_env_defaults") or {})
        self.cwd_in_roots = bool(ex.get("cwd_in_roots", True))
        self.proxy_children = bool(getattr(network, "proxy_children", True))
        self.no_proxy_extra = list(getattr(network, "no_proxy", None) or [])
        self.hf_endpoint = getattr(models, "hf_endpoint", None) or os.environ.get("HF_ENDPOINT") or None
        self.exec_contained = (not self.exec_enabled or self.exec_mode == "allowlist"
                               or glob_match("shell.run", self.deny_actions)
                               or not glob_match("shell.run", self.allow_actions) or self.read_only)
        # kill guard
        self.kill_protect = {self._proc_name(n) for n in (self.proc_cfg.get("kill_protect") or DEFAULT_KILL_PROTECT)}
        self.kill_own_user_only = bool(self.proc_cfg.get("kill_own_user_only", True))
        # model endpoints
        self.model_endpoints: List[dict] = [dict(m) for m in model_endpoints or ()]
        # limits
        jobs, svc, net, lim = dict(cfg.jobs or {}), dict(cfg.services or {}), self.net_cfg, dict(cfg.limits or {})
        self.limits = Limits(
            max_read_bytes=int(fs.get("max_read_bytes", 8388608)),
            max_write_bytes=int(fs.get("max_write_bytes", 16777216)),
            max_chunk_bytes=int(fs.get("max_chunk_bytes", 8388608)),
            max_delete_entries=int(fs.get("max_delete_entries", 10000)),
            exec_default_timeout=float(ex.get("default_timeout", 60)),
            exec_max_timeout=float(ex.get("max_timeout", 3600)),
            max_output_bytes=int(ex.get("max_output_bytes", 1048576)),
            jobs_max_running=int(jobs.get("max_running", 32)), jobs_max_runtime=float(jobs.get("max_runtime", 0)),
            services_max=int(svc.get("max", 32)), allow_autostart=bool(svc.get("allow_autostart", True)),
            max_response_bytes=int(net.get("max_response_bytes", 8388608)),
            max_concurrent_pulls=int(getattr(models, "max_concurrent_pulls", 2) or 2),
            max_concurrent_calls=int(lim.get("max_concurrent_calls", 32)),
            max_call_seconds=float(lim.get("max_call_seconds", 3700)),
            max_result_bytes=int(lim.get("max_result_bytes", 33554432)),
            gateway_max_body_bytes=int(getattr(gateway, "max_body_bytes", 8388608) if gateway else 8388608),
            gateway_max_response_bytes=int(getattr(gateway, "max_response_bytes", 67108864) if gateway
                                           else 67108864))
        # network (function-level imports: netproxy/netutil are WP3, §11.4)
        from . import netproxy, netutil
        net_dict = network.to_dict() if hasattr(network, "to_dict") else dataclasses.asdict(network)
        self._proxies = netproxy.load_proxy_settings(net_dict)
        download_allow = list(net.get("download_allow") or ["loopback", "public"])
        for ep in (self.hf_endpoint, getattr(models, "modelscope_endpoint", None)):
            host = (urlsplit(ep).hostname or "") if ep else ""
            if host and host not in download_allow:
                download_allow.append(host)
        self.http_allow = list(net.get("http_allow") or ["loopback", "public"])
        self.download_allow = download_allow
        self.http_deny = list(net.get("http_deny") or [])
        self.own_ports = tuple(int(p) for p in own_ports or () if p)
        self._net_guard = netutil.NetGuard(
            http_allow=self.http_allow, http_deny=self.http_deny, download_allow=download_allow,
            allow_metadata_ip=bool(net.get("allow_metadata_ip", False)), proxies=self._proxies,
            own_ports=self.own_ports, download_redirect_allow=list(net.get("download_redirect_allow") or []))

    # ------------------------------------------------------------------------------------------- normalization
    def _warn(self, msg: str) -> None:
        self.warnings.append(msg)
        log.warning("%s", msg)

    def _cf(self, key: str) -> str:
        if self.casefold or (self.is_wsl and _is_wsl_drive_path(key)):
            return key.casefold()
        return key

    def _norm(self, p: str) -> str:
        """``~`` expanded, realpath'd, ``util.path_key`` with the platform's case rule (§5.4)."""
        return self._cf(path_key(os.path.realpath(os.path.expanduser(str(p))), casefold=False))

    def _key_real(self, real: str) -> str:
        return self._cf(path_key(real, casefold=False))

    def _expand_config_path(self, p: str) -> str:
        e = os.path.expanduser(p)
        e = _VAR_RE.sub(lambda m: self._var(m, strict=False), e)
        if e.startswith("~"):
            raise internal(f"config path {p!r} still starts with '~' after expansion",
                           hint="the node's environment is broken (HOME/USERPROFILE); refusing to start")
        return os.path.abspath(e)

    def _var(self, m: "re.Match[str]", strict: bool = True) -> str:
        name = m.group(1) or m.group(2) or m.group(3)
        allowed = [v for v in PATH_VARS if (v.lower() == name.lower() if IS_WINDOWS else v == name)]
        if not allowed:
            return m.group(0)
        val = os.environ.get(allowed[0])
        if val is None and IS_WINDOWS:  # pragma: no cover
            for k, v in os.environ.items():
                if k.lower() == name.lower():
                    val = v
                    break
        return val if val is not None else m.group(0)

    def _norm_pattern(self, pat: str) -> List[str]:
        p = os.path.expanduser(str(pat).strip())
        if p.startswith("~"):
            raise internal(f"deny pattern {pat!r} still starts with '~' after expansion",
                           hint="the node's environment is broken (HOME/USERPROFILE); refusing to start")
        p = p.replace("\\", "/")
        if p.startswith("**"):
            return [self._cf(p)]
        if not (p.startswith("/") or re.match(r"^[A-Za-z]:", p)):
            return [self._cf("**/" + p.lstrip("/"))]
        lit = _literal_prefix(p)
        rest = p[len(lit):]
        out = [self._cf(path_key(p, casefold=False))]
        if lit:
            real = path_key(os.path.realpath(lit), casefold=False)
            alt = self._cf(_escape_glob(real) + rest)
            if alt not in out:
                out.append(alt)
        return out

    def live_deny_write(self) -> List[str]:
        """``deny_write_cfg`` plus the real targets of literal prefixes that are symbolic links NOW (v8.1, audit round
        2 verification): ``_norm_pattern`` resolves them once at start, but a dotfile manager (stow, rcm, dotbot) can
        link ``~/.bashrc`` or ``~/.config/systemd`` after that. Recomputed at most every 2 s."""
        t, alts = self._live_alts
        now = time.monotonic()
        if now - t > 2.0:
            alts = []
            for pat in self.deny_write_cfg:
                if pat.startswith("**"):
                    continue
                lit = _literal_prefix(pat)
                if not lit:
                    continue
                try:
                    real = self._cf(path_key(os.path.realpath(lit), casefold=False))
                except (OSError, ValueError):
                    continue
                if real != lit:
                    alt = _escape_glob(real) + pat[len(lit):]
                    if alt not in alts and alt not in self.deny_write_cfg:
                        alts.append(alt)
            self._live_alts = (now, alts)
        return list(self.deny_write_cfg) + alts

    def _patterns(self, items: Iterable[str], remap_wsl: bool = False) -> List[str]:
        out: List[str] = []
        for it in items:
            s = str(it)
            if not s or s.startswith("<"):
                continue
            pats = self._norm_pattern(s)
            if remap_wsl and self.is_wsl and s.startswith("~/"):
                pats.append(self._cf("/mnt/*/Users/*/" + s[2:]))
            for p in pats:
                if p not in out:
                    out.append(p)
        return out

    def _tree(self, p: Union[str, Path]) -> List[str]:
        """Patterns denying ``p`` and everything below it (literal, glob-escaped)."""
        raw = os.path.abspath(os.path.expanduser(str(p)))
        out = []
        for form in (path_key(raw, casefold=False), path_key(os.path.realpath(raw), casefold=False)):
            pat = self._cf(_escape_glob(form)) + "/**"
            if pat not in out:
                out.append(pat)
        return out

    # ------------------------------------------------------------------------------------------- roots
    def _make_root(self, path: str, mode: str) -> _Root:
        if path == "*":
            return _Root(display="*", mode=mode, star=True, keys=(), real="*")
        exp = self._expand_config_path(path)
        real = os.path.realpath(exp)
        keys = tuple(dict.fromkeys([self._cf(path_key(os.path.normpath(exp), casefold=False)),
                                    self._key_real(real)]))
        return _Root(display=path, mode=mode, star=False, keys=keys, real=real)

    @staticmethod
    def _under(key: str, base: str) -> bool:
        if key == base:
            return True
        b = base if base.endswith("/") else base + "/"
        return key.startswith(b)

    def _match_root(self, key: str) -> Optional[_Root]:
        best: Optional[_Root] = None
        best_len = -1
        for r in self._roots:
            if r.star:
                if best_len < 0:
                    best, best_len = r, 0
                continue
            for k in r.keys:
                if self._under(key, k) and len(k) > best_len:
                    best, best_len = r, len(k)
        return best

    def _contained(self, key: str, *, write: bool) -> bool:
        r = self._match_root(key)
        return r is not None and (not write or r.mode == "rw")

    def _in_rw_root(self, key: str) -> bool:
        r = self._match_root(key)
        return r is not None and r.mode == "rw"

    def _is_root_or_ancestor(self, key: str) -> bool:
        if key == self.home_key or self._under(self.home_key, key):
            return True
        for r in self._roots:
            for k in r.keys:
                if self._under(k, key):
                    return True
        return False

    # ------------------------------------------------------------------------------------------- hard denies
    def _build_hard_denies(self, protected: Tuple[List[Path], List[Path]]) -> None:
        deny_all = self.hard_deny_all
        homes = [os.path.join(self.home, ".hive")]
        if os.environ.get("HIVE_HOME"):
            homes.append(os.environ["HIVE_HOME"])
        homes.append(str(self.hive_home))
        for h in homes:
            deny_all.extend(x for x in self._tree(h) if x not in deny_all)
        deny_all.extend(x for x in self._tree(self.data_dir) if x not in deny_all)
        prot_all, prot_write = protected if protected else ([], [])
        for p in prot_all or ():
            deny_all.extend(x for x in self._tree(p) if x not in deny_all)
        if not IS_WINDOWS and any(r.star or any(self._under("/proc", k) for k in r.keys) for r in self._roots):
            for pat in ("/proc/*/environ", "/proc/*/mem", "/proc/self/**", "/proc/thread-self/**",
                        "/proc/*/task/*/environ", "/proc/*/task/*/mem"):
                deny_all.append(pat)
        # interpreter, hive code, site dirs (skip-with-warning for ~/roots/ancestors)
        candidates: List[Tuple[str, bool]] = []   # (path, is_tree)
        pkg_dir = Path(__file__).resolve().parent
        from . import util as _util
        pyz = _util._zipapp_path()
        candidates.append((str(pyz) if pyz else str(pkg_dir), not bool(pyz)))
        if sys.prefix != sys.base_prefix:
            candidates.append((sys.prefix, True))
        try:
            user_site = site.getusersitepackages()
            if user_site:
                candidates.append((user_site, True))
        except Exception:  # noqa: BLE001
            pass
        try:
            for sp in site.getsitepackages():
                candidates.append((sp, True))
        except Exception:  # noqa: BLE001
            pass
        exes = [sys.executable, _util.console_python()]
        if IS_WINDOWS:  # pragma: no cover
            exes.append(os.path.join(os.path.dirname(sys.executable), "pythonw.exe"))
        for exe in exes:
            if exe:
                candidates.append((exe, False))
                candidates.append((os.path.dirname(exe), True))
        paths = sysconfig.get_paths()
        for k in ("stdlib", "platstdlib", "purelib", "platlib", "scripts"):
            if paths.get(k):
                candidates.append((paths[k], True))
        for pre in {sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix}:
            if pre and self._in_rw_root(self._norm(pre)):
                candidates.append((pre, True))
        for c, is_tree in candidates:
            key = self._norm(c)
            if is_tree and self._is_root_or_ancestor(key):
                self._warn(f"{c} is your home, a root or an ancestor of one; Hive cannot protect it from writes")
                continue
            pats = self._tree(c) if is_tree else [self._cf(_escape_glob(key))]
            if not is_tree:
                pats.append(self._cf(_escape_glob(path_key(os.path.abspath(c), casefold=False))))
            for pat in pats:
                if pat not in self.hard_deny_write:
                    self.hard_deny_write.append(pat)
        # every sys.path directory inside an rw root (D56, fail closed)
        for entry in list(sys.path):
            d = entry or os.getcwd()
            if not os.path.isdir(d):
                continue
            key = self._norm(d)
            if not self._in_rw_root(key):
                continue
            if self._is_root_or_ancestor(key):
                raise internal(f"Hive's import path contains {d}, which agents may write",
                               hint=f"Hive's import path contains {d}, which agents may write; install Hive with pip "
                                    "or as hive.pyz, or start it from another folder")
            for pat in self._tree(d):
                if pat not in self.hard_deny_write:
                    self.hard_deny_write.append(pat)
        for p in prot_write or ():
            key = self._norm(str(p))
            if self._is_root_or_ancestor(key):
                self._warn(f"hub code path {p} is your home, a root or an ancestor of one; not protected")
                continue
            for pat in self._tree(p):
                if pat not in self.hard_deny_write:
                    self.hard_deny_write.append(pat)

    # ------------------------------------------------------------------------------------------- misc
    @staticmethod
    def _proc_name(n: str) -> str:
        n = str(n).strip().lower()
        return n[:-4] if n.endswith(".exe") else n

    @property
    def net_guard(self) -> Any:
        """netutil.NetGuard built from policy net + network + models."""
        return self._net_guard

    @property
    def proxies(self) -> Any:
        """netproxy.ProxySettings (built once per config load)."""
        return self._proxies

    def roots(self) -> List[dict]:
        """``[{"path","mode"}]`` as configured."""
        return [{"path": r.display, "mode": r.mode} for r in self._roots]

    def effective(self, identity: "Identity", *, paused: bool, approvals: Optional["ApprovalStore"] = None,
                  node: str = "", services: Optional[Callable[[], dict]] = None,
                  holds: Optional["FileHolds"] = None) -> "EffectivePolicy":
        """The per-identity view of this policy (write_exec_paths_denied computed once here)."""
        gated = (bool(identity.exec_gated) or _auth.exec_gated(identity, identity.require_approval)
                 or self.exec_contained)
        return EffectivePolicy(self, identity, paused=paused, approvals=approvals, node=node, services=services,
                               holds=holds, write_exec_paths_denied=gated)

    def summary(self) -> dict:
        """Policy summary (audit lifecycle start, doctor)."""
        return {"preset": self.preset, "read_only": self.read_only, "fs_roots": self.roots(),
                "exec": {"enabled": self.exec_enabled, "mode": self.exec_mode, "shells": list(self.shells),
                         "default_timeout": self.limits.exec_default_timeout,
                         "max_timeout": self.limits.exec_max_timeout},
                "exec_contained": self.exec_contained,
                "allow_actions": list(self.allow_actions), "deny_actions": list(self.deny_actions),
                "net": {"http_allow": list(self.http_allow), "download_allow": list(self.download_allow),
                        "http_deny": list(self.http_deny)},
                "warnings": list(self.warnings)}

    @classmethod
    def permissive(cls, tmp: Path) -> "Policy":
        """Test policy: roots ["*"], exec open, http loopback+public+private, no deny lists."""
        from .config import ModelsConfig, NetworkConfig, PolicyConfig, standard_policy
        from .secretstore import SecretStore
        tmp = Path(tmp)
        data = tmp / "data"
        data.mkdir(parents=True, exist_ok=True)
        pol = standard_policy()
        pol["fs"].update({"roots": [{"path": "*", "mode": "rw"}], "deny": [], "deny_write": []})
        pol["exec"]["command_deny"] = []
        pol["net"]["http_allow"] = ["loopback", "private", "public"]
        pol["net"]["download_allow"] = ["loopback", "private", "public"]
        pol["proc"]["kill_protect"] = list(DEFAULT_KILL_PROTECT)
        pc = PolicyConfig(**pol)
        pc.preset = "full"
        secrets = SecretStore(data / "secrets.json", passthrough=(), env={})
        return cls(pc, workdir=tmp, data_dir=data, hive_home=tmp / "hivehome", protected=([], []),
                   network=NetworkConfig(proxy="off", use_system_proxy=False), models=ModelsConfig(),
                   secrets=secrets)


class EffectivePolicy:
    """Policy for one identity (§5.3–§5.4)."""

    def __init__(self, policy: Policy, identity: "Identity", *, paused: bool, approvals: Optional["ApprovalStore"],
                 node: str, services: Optional[Callable[[], dict]], holds: Optional["FileHolds"],
                 write_exec_paths_denied: bool):
        self.p = policy
        self.identity = identity
        self.paused = bool(paused)
        self.approvals = approvals
        self.node = node
        self._services = services
        self.holds = holds
        self._gated = bool(write_exec_paths_denied)
        self.limits = policy.limits
        self.exec_enabled = policy.exec_enabled
        self.exec_mode = policy.exec_mode
        self.shells = policy.shells
        self.model_endpoints = list(policy.model_endpoints)
        self.node_path = policy.node_path
        self.grant_id: Optional[str] = None

    # ------------------------------------------------------------------------------------------- authorization
    def _role_name(self) -> str:
        for name, classes in _auth.ROLE_PRESETS.items():
            if set(classes) == set(self.identity.classes):
                return name
        return ",".join(sorted(self.identity.classes)) or "none"

    def check_action(self, spec: "ActionSpec") -> None:
        """§5.3 authorization formula: identity -> forbidden; node policy, read-only, paused -> policy_denied."""
        ident, n = self.identity, spec.name
        missing = sorted(set(spec.classes_required) - set(ident.classes))
        if missing:
            hint = f"your token lacks the class(es) {', '.join(missing)}; ask the owner for a token that allows {n}"
            if n == "model.forward" and ident.kind == "relay":
                hint = f"the relay role on this pod needs the net class for the gateway (role is {self._role_name()})"
            elif n == "model.forward" and ident.kind == "hub":
                hint = "the hub token on this node needs the net class for the gateway"
            raise forbidden(f"token '{ident.name}' may not call {n} (needs {', '.join(sorted(spec.classes_required))})",
                            hint=hint, missing_classes=missing)
        if not ident.allows_action(n):
            raise forbidden(f"token '{ident.name}' may not call {n} (denied for this token)",
                            hint="this action is denied for your token; " + SECRET_HINT)
        if not glob_match(n, self.p.allow_actions) or glob_match(n, self.p.deny_actions):
            raise denied(f"{n} is disabled on this node", rule="deny_actions",
                         hint="the node's policy does not allow this action; " + SECRET_HINT)
        if self.p.read_only and spec.risk != "read":
            raise denied(f"this node is read-only; {n} changes something", rule="read_only",
                         hint="only read actions are allowed on this node")
        if "exec" in spec.classes_required and not self.p.exec_enabled:
            raise denied(f"command execution is disabled on this node ({n})", rule="exec_disabled",
                         hint="use file and read actions; the owner disabled exec")
        if self.paused and spec.risk != "read":
            raise denied(f"node is paused; {n} is not allowed", rule="paused",
                         hint="the owner paused this node (hive node resume); only read actions work")

    def needs_approval(self, spec: "ActionSpec") -> bool:
        """§5.3 approval rule (direct agent tokens only; a valid grant skips it and is kept in ``grant_id``)."""
        ident = self.identity
        self.grant_id = None
        if ident.kind != "agent" or ident.via != "direct" or not glob_match(spec.name, ident.require_approval):
            return False
        if self.approvals is not None:
            gid = self.approvals.granted(ident, self.node, spec.name)
            if gid:
                self.grant_id = gid
                return False
        return True

    # ------------------------------------------------------------------------------------------- filesystem
    @property
    def write_exec_paths_denied(self) -> bool:
        """D53 rule (exec-free / gated identity, or a node policy that contains execution)."""
        return self._gated

    @property
    def workdir(self) -> Path:
        """Node workdir (realpath)."""
        return self.p._workdir

    def roots(self) -> List[dict]:
        """``[{"path","mode"}]``."""
        return self.p.roots()

    def _key(self, path: Union[str, Path]) -> str:
        """Normalized key of an already resolved path (used by FileHolds)."""
        return self.p._key_real(os.path.realpath(str(path)))

    def _expand(self, path: str) -> str:
        s = path
        if s == "~" or s.startswith("~/") or s.startswith("~\\"):
            s = self.p.home + s[1:]
        s = _VAR_RE.sub(lambda m: self.p._var(m), s)
        if IS_WINDOWS:  # pragma: no cover
            windows_string_checks(s, unc_allowed=self.p.unc_roots)
        if not os.path.isabs(s):
            s = os.path.join(str(self.p._workdir), s)
        return s

    def _roots_error(self, path: str, write: bool) -> HiveError:
        roots = [r["path"] + ("" if r["mode"] == "rw" else " (read-only)") for r in self.roots()]
        what = "writable " if write else ""
        return denied(f"path '{path}' is outside the {what}roots of this node", rule="fs_roots",
                      hint="Allowed roots: " + ", ".join(roots) + ". " + SECRET_HINT,
                      roots=[r["path"] for r in self.roots()])

    def _real(self, s: str) -> str:
        if os.path.lexists(s):
            real = os.path.realpath(s)
        else:
            parts = Path(s).parts
            real = ""
            for i in range(len(parts), 0, -1):
                prefix = os.path.join(*parts[:i])
                if os.path.lexists(prefix):
                    rest = list(parts[i:])
                    if ".." in rest:
                        raise bad_request(f"path '{s}' has '..' below a folder that does not exist",
                                          hint="use an absolute path without '..'")
                    real = os.path.join(os.path.realpath(prefix), *[r for r in rest if r != "."])
                    break
            if not real:
                real = os.path.normpath(s)
        if IS_WINDOWS and re.search(r"~\d", real):  # pragma: no cover - Windows only
            real = _long_path_name(real)
        return real

    def _real_nofollow(self, s: str) -> str:
        t = s
        seps = "/\\" if IS_WINDOWS else "/"
        while len(t) > 1 and t[-1] in seps and not (IS_WINDOWS and re.match(r"^[A-Za-z]:[/\\]$", t)):
            t = t[:-1]
        parent, base = os.path.split(t)
        if not base or base in (".", "..") or not parent or parent == t:
            return self._real(s)
        return os.path.join(self._real(parent), base)

    def _venv_bin(self, real: str) -> bool:
        p = Path(real)
        for anc in p.parents:
            nm = anc.name.casefold() if self.p.casefold else anc.name
            if nm in ("bin", "scripts") and (anc.name in ("bin", "Scripts") or self.p.casefold):
                if os.path.isfile(os.path.join(str(anc.parent), "pyvenv.cfg")):
                    return True
        return False

    def _service_code_patterns(self) -> List[str]:
        if self._services is None:
            return []
        try:
            view = self._services() or {}
        except Exception:  # noqa: BLE001
            return []
        out: List[str] = []
        for d in view.get("code_dirs") or ():
            try:
                key = self.p._norm(str(d))
            except Exception:  # noqa: BLE001
                continue
            if self.p._is_root_or_ancestor(key):
                continue
            out.append(_escape_glob(key) + "/**")
        for f in view.get("code_files") or ():   # v8.1: the files a service's approval bound
            try:
                out.append(_escape_glob(self.p._norm(str(f))))
            except Exception:  # noqa: BLE001
                continue
        return out

    def _hop_keys(self, named: str) -> List[str]:
        """Keys of the path as named, lexically normalized, and of each form it takes while its symbolic links are
        followed one hop at a time (v8.1, audit round 2 verification): exec paths are matched against all of them,
        so ``~/.bashrc -> dotfiles/bashrc`` or ``~/.config/systemd -> ../dotfiles/config/systemd`` is denied when
        written through the link as well as at its target."""
        try:
            s = os.path.abspath(named)
        except (TypeError, ValueError):
            return []
        forms = [os.path.normpath(s)]
        parts = Path(s).parts
        if not parts:
            return []
        cur = parts[0]
        for i in range(1, len(parts)):
            nxt = os.path.join(cur, parts[i])
            rest = parts[i + 1:]
            hops = 0
            while hops < 40 and os.path.islink(nxt):
                hops += 1
                forms.append(os.path.normpath(os.path.join(nxt, *rest)))
                try:
                    tgt = os.readlink(nxt)
                except OSError:
                    break
                nxt = os.path.normpath(os.path.join(os.path.dirname(nxt), tgt))
                forms.append(os.path.normpath(os.path.join(nxt, *rest)))
            cur = os.path.realpath(nxt)
        out: List[str] = []
        for f in forms:
            k = self.p._key_real(f)
            if k not in out:
                out.append(k)
        return out

    @staticmethod
    def _bare_repo_shape(real: str) -> bool:
        """Writing ``config`` beside a ``HEAD`` (or ``HEAD`` beside a ``config``) makes the folder look like a bare
        git repository; git run inside it reads that config (core.fsmonitor, hooksPath: commands)."""
        base = os.path.basename(real.rstrip("/\\"))
        if IS_WINDOWS or IS_MAC:
            other = {"config": "HEAD", "head": "config"}.get(base.casefold())
        else:
            other = {"config": "HEAD", "HEAD": "config"}.get(base)
        if other is None:
            return False
        try:
            return os.path.lexists(os.path.join(os.path.dirname(real), other))
        except (OSError, ValueError):
            return False

    def _deny_reason(self, key: str, real: str, *, write: bool, named: Optional[str] = None) -> Optional[HiveError]:
        if match_deny(self.p.hard_deny_all, key) or ".hive" in [c.casefold() for c in key.split("/")]:
            return denied(f"path '{real}' is internal to Hive", rule="hive_internal",
                          hint="Hive's own data, config, tokens and secrets cannot be read or changed through "
                               "actions. " + SECRET_HINT)
        if match_deny(self.p.deny, key):
            return denied(f"path '{real}' is denied by node policy", rule="fs_deny",
                          hint="this path holds secrets or is protected by the owner. " + SECRET_HINT)
        if not write:
            return None
        if match_deny(self.p.hard_deny_write, key):
            return denied(f"path '{real}' is Hive's own code or interpreter and cannot be written",
                          rule="hive_internal", hint="Hive's code and Python installation are never writable through "
                                                     "actions. " + SECRET_HINT)
        if self._gated:
            keys = [key] + [k for k in (self._hop_keys(named) if named else []) if k != key]
            pats = self.p.live_deny_write() if self.p.has_deny_write else []
            if self.p.has_deny_write and (any(match_deny(pats, k) or match_deny(self.p.path_deny, k) for k in keys)
                                          or self._venv_bin(real) or self._bare_repo_shape(real)):
                return denied(f"path '{real}' can make code run later (autostart, shell profile, hooks, PATH)",
                              rule="exec_path", hint=EXEC_PATH_HINT)
            if match_deny(self._service_code_patterns(), key):
                return denied(f"path '{real}' is code of a managed service", rule="exec_path", hint=EXEC_PATH_HINT)
            if self.holds is not None and self.holds.held(key):
                return self._held_error(real)
        return None

    @staticmethod
    def _held_error(real: str) -> HiveError:
        return conflict(f"'{real}' is in use by an approved command", rule="approved_input",
                        hint="this file is in use by an approved command; wait until it finishes or stop it")

    def _held_below(self, key: str) -> List[str]:
        """Held keys at or below ``key`` (gated writes only): moving, deleting or copying over a folder that holds an
        approved command's input would replace that input (§5.6 FileHolds, T16)."""
        if not self._gated or self.holds is None:
            return []
        held_keys = getattr(self.holds, "held_keys", None)
        if not callable(held_keys):
            return []
        return [k for k in held_keys() if self.p._under(k, key)]

    def path_key(self, path: Union[str, Path], *, follow: bool = True) -> str:
        """Comparison key of an absolute path with PathGuard's case rule (casefolded on Windows/macOS and WSL drive
        paths): realpath'd with ``follow=True``, else normalized lexically (no symbolic link is resolved)."""
        s = os.path.abspath(str(path))
        return self.p._key_real(os.path.realpath(s) if follow else os.path.normpath(s))

    def resolve_path(self, path: str, *, write: bool = False, must_exist: bool = False,
                     subtree: bool = False, nofollow_last: bool = False) -> Path:
        """PathGuard (§5.4, binding step order); returns the real path handlers open. With ``nofollow_last`` the last
        component is not resolved (realpath of the parent + the name as given): the path of a symbolic link itself
        is vetted (fs.delete / fs.move / fs.copy destinations act on the link, not its target)."""
        # 1. basic form
        if not isinstance(path, str) or not path or "\x00" in path or len(path) > 4096:
            raise bad_request("invalid path (empty, NUL or longer than 4096 characters)")
        if path.startswith("~") and not (path == "~" or path.startswith("~/") or path.startswith("~\\")):
            raise bad_request(f"'~user' paths are not supported: {path[:64]!r}", hint="use ~/… or an absolute path")
        # 2. expansion (~ and the fixed non-secret variables), relative -> workdir; 3. Windows string checks
        s = self._expand(path)
        # 4. lexical pre-check before any filesystem call
        lex = self.p._cf(path_key(os.path.normpath(s), casefold=False))
        if self.p._match_root(lex) is None:
            raise self._roots_error(path, write)
        # 5. realpath (of the parent only with nofollow_last)
        real = self._real_nofollow(s) if nofollow_last else self._real(s)
        key = self.p._key_real(real)
        # 6. root containment on the real path
        root = self.p._match_root(key)
        if root is None or (write and root.mode != "rw"):
            raise self._roots_error(real, write)
        # 7. denies (deny wins over roots)
        err = self._deny_reason(key, real, write=write, named=s)  # incl. held files of approved calls (gated writes)
        if err is not None:
            raise err
        # 8. subtree
        if subtree:
            if write and self._held_below(key):  # moving/deleting a folder that holds an approved command's input
                raise self._held_error(real)
            bad = self.denied_under(Path(real), write=write)
            if bad:
                raise denied(f"'{real}' contains protected entries ({bad[0]}{', …' if len(bad) > 1 else ''})",
                             rule="contains_denied", hint="choose a folder without protected files. " + SECRET_HINT,
                             entries=bad[:10])
        # 9. existence
        if must_exist and not os.path.lexists(real):
            raise not_found(f"path '{real}' does not exist")
        return Path(real)

    def _active_patterns(self, write: bool) -> List[str]:
        pats = list(self.p.hard_deny_all) + list(self.p.deny)
        if write:
            pats += list(self.p.hard_deny_write)
            if self._gated:
                if self.p.has_deny_write:
                    pats += self.p.live_deny_write() + list(self.p.path_deny)
                pats += self._service_code_patterns()
        return pats

    def deny_patterns(self, *, write: bool = True) -> List[str]:
        """The identity's normalized deny patterns (hard denies, fs.deny; for writes also the hard deny_write and,
        when gated, fs.deny_write, PATH dirs and service code dirs). WP3b CONTRACT-REQUEST: net.download hands them
        to the ``_download`` child for server-chosen names (§9.8, §11.3)."""
        return list(self._active_patterns(write))

    def denied_under(self, path: Path, *, write: bool = False, rebase: Optional[Path] = None) -> List[str]:
        """Denied entries in the tree at ``path`` (§5.4 step 8); with ``rebase`` each entry is tested at
        ``rebase/<relative path>`` (fs.move/fs.copy destinations)."""
        src = os.path.realpath(str(path))
        src_key = self.p._key_real(src)
        out: List[str] = []
        dst_real: Optional[str] = None
        if rebase is None:
            # cheap test: a pattern whose literal prefix lies strictly below the path (e.g. moving ~ or an ancestor
            # of HIVE_HOME); patterns with a leading ** are caught by the walk below
            for pat in self._active_patterns(write):
                lit = _literal_prefix(pat)
                if lit and lit != src_key and self.p._under(lit, src_key):
                    return [lit]
        else:
            dst_real = self._real(os.path.abspath(os.path.expanduser(str(rebase))))
        if write:
            # held inputs of approved calls below the written tree (§5.6 FileHolds): the folder itself, or with rebase
            # the destination (the source of a copy is only read; fs.move checks its source with subtree=True)
            held = self._held_below(self.p._key_real(dst_real) if dst_real is not None else src_key)
            if held:
                return held[:20]
        count = 0

        def test(entry_real: str, rel: str) -> None:
            if dst_real is not None:
                target = os.path.join(dst_real, rel) if rel else dst_real
            else:
                target = entry_real
            key = self.p._key_real(target)
            err = self._deny_reason(key, target, write=write)
            if err is not None:
                out.append(target)

        test(src, "")
        if os.path.isdir(src) and not os.path.islink(src):
            stack = [src]
            while stack and len(out) < 20:
                d = stack.pop()
                try:
                    with os.scandir(d) as it:
                        entries = list(it)
                except OSError:
                    continue
                for e in entries:
                    count += 1
                    if count > MAX_WALK:
                        raise too_large("folder too large to check; use a smaller subfolder", entries=MAX_WALK)
                    rel = os.path.relpath(e.path, src)
                    test(e.path, rel)
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                    except OSError:
                        continue
        return out

    # ------------------------------------------------------------------------------------------- exec
    def check_shell(self, shell: str) -> None:
        """The shell must be listed in ``exec.shells``."""
        if shell not in self.p.shells:
            raise denied(f"shell '{shell}' is not allowed on this node", rule="shell",
                         hint="allowed shells: " + ", ".join(self.p.shells))

    def _resolve_exe(self, name: str) -> Optional[str]:
        if os.path.isabs(name):
            return name if os.path.exists(name) else None
        if IS_WINDOWS and ("/" in name or "\\" in name):  # pragma: no cover
            return None
        return shutil.which(name, path=self.node_path)

    def check_command(self, command: Union[str, List[str]], shell: str) -> None:
        """CommandGuard (§5.4): open mode = command_deny regexes; allowlist mode = argv only + command_allow."""
        if self.p.exec_mode == "allowlist":
            if not isinstance(command, list) or not command:
                raise denied("this node only runs allow-listed programs given as argv (no shell)", rule="allowlist",
                             hint="pass argv=[program, args…] with shell omitted")
            first = str(command[0])
            if ".." in re.split(r"[\\/]", first):  # "/opt/tools/../../bin/sh" would match a "/opt/tools/.*" rule
                raise denied(f"program path '{first[:200]}' contains '..'", rule="command_allow",
                             hint="name the program by its plain absolute path or by its name on the node PATH")
            resolved = self._resolve_exe(first)
            if resolved is None:
                raise not_found(f"program '{first}' was not found on the node PATH")
            if IS_WINDOWS and resolved.lower().endswith((".bat", ".cmd")):  # pragma: no cover
                raise denied(f"'{first}' is a batch file; allow-listed programs must be executables",
                             rule="batch_target", hint="cmd.exe would re-parse the arguments")
            rkey = self.p._norm(resolved)
            if self.p._in_rw_root(rkey):
                raise denied(f"'{resolved}' lies in a folder agents can write", rule="exec_in_writable_root",
                             hint="allow-listed programs must be installed outside the writable roots")
            head = first if os.path.isabs(first) else os.path.basename(resolved)
            text = " ".join([head] + [str(a) for a in command[1:]])
            if any(rx.search(text) for rx in self.p.command_deny):
                raise denied("command matches a deny rule of this node", rule="command_deny", hint=SECRET_HINT)
            if not any(rx.fullmatch(text) for rx in self.p.command_allow):
                raise denied(f"command is not on this node's allow list: {text[:200]}", rule="command_allow",
                             hint="only commands matching policy.exec.command_allow run here; ask the owner")
            return
        if isinstance(command, list):
            text = " ".join(shlex.quote(str(a)) for a in command)
        else:
            text = str(command)
        for rx in self.p.command_deny:
            if rx.search(text):
                raise denied("command matches a deny rule of this node (destructive or Hive-internal)",
                             rule="command_deny", hint="this command is blocked as a safety speed bump; " + SECRET_HINT,
                             pattern=rx.pattern)

    def check_env(self, env: Mapping[str, str]) -> None:
        """Allowlist mode only: every env key must be listed in ``exec.env_allow``."""
        if self.p.exec_mode != "allowlist":
            return
        for k in env or {}:
            if k not in self.p.env_allow:
                raise bad_request(f"env variable {k!r} is not allowed on this node", rule="env_not_allowed",
                                  hint="allow-list nodes accept only env names in policy.exec.env_allow")

    def check_cwd(self, cwd: Optional[str]) -> Path:
        """None -> workdir; else a directory (inside the roots when ``exec.cwd_in_roots``)."""
        if cwd is None or cwd == "":
            return self.p._workdir
        if self.p.cwd_in_roots:
            p = self.resolve_path(cwd, must_exist=True)
        else:
            p = Path(os.path.realpath(self._expand(cwd)))
            if not p.exists():
                raise not_found(f"cwd '{p}' does not exist")
        if not p.is_dir():
            raise bad_request(f"cwd '{p}' is not a directory")
        return p

    def child_env(self, overrides: Dict[str, str], unset: Iterable[str] = (), gpus: Optional[str] = None,
                  secret_env: Sequence[str] = ()) -> Dict[str, str]:
        """Child environment in the §10.1 order (later wins)."""
        env = {k: v for k, v in os.environ.items() if not glob_match(k.upper(), [s.upper() for s in self.p.scrub_env])}
        for k in unset or ():
            env.pop(k, None)
            if IS_WINDOWS:  # pragma: no cover
                for kk in [x for x in env if x.lower() == str(k).lower()]:
                    env.pop(kk, None)
        for k, v in self.p.child_env_defaults.items():
            env[k] = str(v)
        if self.p.proxy_children:
            from . import netproxy
            try:
                for k, v in netproxy.child_proxy_env(self.p.proxies, self.p.no_proxy_extra).items():
                    if k not in env:
                        env[k] = v
            except Exception as e:  # noqa: BLE001
                log.warning("child proxy env unavailable: %s", e)
        if self.p.hf_endpoint:
            env["HF_ENDPOINT"] = self.p.hf_endpoint
        prepend = [os.path.expanduser(p) for p in self.p.exec_cfg.get("path_prepend") or []]
        if prepend:
            env["PATH"] = os.pathsep.join(prepend + ([env["PATH"]] if env.get("PATH") else []))
        if gpus is not None:
            gpu_keys = ("CUDA_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES")
            if gpus == "all":
                for k in gpu_keys:
                    env.pop(k, None)
            else:
                val = "" if gpus == "none" else str(gpus)
                for k in gpu_keys:
                    env[k] = val
        for name in secret_env or ():
            env[name] = self.p.secrets.resolve(name, purpose="exec")
        for k, v in (overrides or {}).items():
            env[str(k)] = str(v)
        return env

    # ------------------------------------------------------------------------------------------- net
    @property
    def net_guard(self) -> Any:
        """netutil.NetGuard."""
        return self.p.net_guard

    @property
    def proxies(self) -> Any:
        """netproxy.ProxySettings."""
        return self.p.proxies

    def _known_endpoint(self, target: Any) -> bool:
        bases: List[str] = [str(m.get("base_url") or "") for m in self.model_endpoints]
        if self._services is not None:
            try:
                bases += [str(e) for e in (self._services() or {}).get("endpoints") or ()]
            except Exception:  # noqa: BLE001
                pass
        path = str(target.path or "/").split("?", 1)[0].rstrip("/") or "/"
        for b in bases:
            try:
                u = urlsplit(b)
                port = u.port or (443 if u.scheme == "https" else 80)
            except ValueError:
                continue
            if (u.scheme or "").lower() != target.scheme or (u.hostname or "").lower() != target.host.lower() \
                    or port != target.port:
                continue
            base_path = (u.path or "").rstrip("/")
            if any(path == base_path + suf for suf in OPENAI_SUFFIXES):
                return True
        return False

    def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> Any:
        """NetGuard (WP3) + the identity-dependent rules 5a (loopback_write) and 5c (pinned writes)."""
        target = self.p.net_guard.check_url(url, purpose=purpose, method=method, redirect=redirect)
        m = (method or "GET").upper()
        if m in WRITE_METHODS_SAFE or not self._gated:
            return target
        cat = getattr(target, "category", "public")
        if purpose in ("http", "model") and cat in ("loopback", "private"):
            if not self._known_endpoint(target):
                raise denied(f"{m} to a local service is not allowed for this token", rule="loopback_write",
                             hint="POST/PUT to local services can run code (Docker API, Jupyter, ComfyUI); use "
                                  "model_chat, or ask the owner")
        route = getattr(target, "route", "direct")
        ip = getattr(target, "ip", None)
        if route == "proxy":
            if not ip or cat == "fakeip" or self._ip_cat(ip) == "fakeip":
                raise denied(f"{m} through the proxy needs a name that resolves locally", rule="loopback_write",
                             hint="write requests from this token need a name that resolves locally to a public IP")
            return dataclasses.replace(target, pin_ip=True)
        if cat == "fakeip" or (ip and self._ip_cat(ip) == "fakeip"):
            raise denied(f"{m} to a fake-ip (TUN) address is not allowed for this token", rule="loopback_write",
                         hint="write requests from this token need a name that resolves locally to a public IP")
        return target

    @staticmethod
    def _ip_cat(ip: str) -> str:
        from .util import ip_category
        try:
            return ip_category(ip)
        except HiveError:
            return "public"

    def auth_spec(self, name: str, *, method: str = "GET") -> Any:
        """SecretStore.resolve_auth(name); with a write ``method`` also rule 5b (``secret_write``)."""
        m = (method or "GET").upper()
        if m not in ("GET", "HEAD") and self._gated:
            raise denied(f"sending secret '{name}' with {m} is not allowed for this token", rule="secret_write",
                         hint="sending a secret with POST/PUT/DELETE needs an exec-capable, non-gated token; ask the "
                              "owner")
        return self.p.secrets.resolve_auth(name)

    # ------------------------------------------------------------------------------------------- process
    def check_kill(self, pid: int, info: dict, *, protected: Mapping[int, str]) -> Dict[str, Any]:
        """Kill guard (§5.4): protected pids, pid <= 4, kill_protect names, Hive processes, other users.

        Returns audit extras for the proc.kill handler to merge into ``ctx.audit_extra``: ``{"owner_unknown": True}``
        when ``kill_own_user_only`` is on but the owner could not be determined (allowed and audited, §5.4), else {}.
        """
        if pid in (protected or {}):
            raise denied(f"process {pid} ({protected[pid]}) is managed by Hive", rule="protected_process",
                         hint="use job_stop / service_control")
        if pid <= 4:
            raise denied(f"process {pid} is a system process", rule="kill_protect", hint=SECRET_HINT)
        info = info or {}
        name = self.p._proc_name(str(info.get("name") or ""))
        if name and name in self.p.kill_protect:
            raise denied(f"process {pid} ({info.get('name')}) is protected", rule="kill_protect", hint=SECRET_HINT)
        cmd = info.get("cmdline")
        text = " ".join(str(c) for c in cmd) if isinstance(cmd, list) else str(cmd or "")
        if text and _HIVE_CMD_RE.search(text):
            raise denied(f"process {pid} is a Hive process", rule="kill_protect",
                         hint="use hive node stop, job_stop or service_control")
        if pid in self._special_pids():
            raise denied(f"process {pid} is a Hive watchdog/hub process", rule="kill_protect", hint=SECRET_HINT)
        if not self.p.kill_own_user_only:
            return {}
        other = denied(f"process {pid} belongs to another user", rule="other_user",
                       hint="this node only stops processes of its own user")
        uid = info.get("uid")
        if not IS_WINDOWS and isinstance(uid, int) and not isinstance(uid, bool):
            if uid != os.getuid():
                raise other
            return {}
        user = info.get("user")
        me = _current_user()
        if isinstance(user, str) and user.strip() and me:
            if _user_key(user) != _user_key(me):
                raise other
            return {}
        return {"owner_unknown": True}

    def _special_pids(self) -> Set[int]:
        out: Set[int] = set()
        try:
            txt = (self.p.data_dir / "run" / "watchdog.pid").read_text(encoding="utf-8").strip()
            out.add(int(txt))
        except (OSError, ValueError):
            pass
        try:
            from .config import default_paths
            from .util import FileLock
            hp = FileLock(default_paths()["hub_data"] / "hub.lock").holder_pid()
            if hp:
                out.add(hp)
        except Exception:  # noqa: BLE001
            pass
        return out

    def check_timeout(self, value: Optional[float], default: float, maximum: float) -> float:
        """None -> default; > maximum -> bad_request naming the node limit; never clamps."""
        if value is None:
            return float(default)
        v = float(value)
        if v > float(maximum):
            raise bad_request(f"timeout {v:g} s is above the node limit", hint=f"max timeout on this node is "
                                                                             f"{float(maximum):g} s",
                              maximum=float(maximum))
        return v

    # ------------------------------------------------------------------------------------------- summary
    def summary(self) -> dict:
        """node.info policy block for this identity."""
        from . import catalog
        denied_actions, approval = [], []
        for name, spec in catalog.SPECS.items():
            if spec.scope != "node":
                continue
            try:
                EffectivePolicy.check_action(self, spec)
            except HiveError:
                denied_actions.append(name)
                continue
            ident = self.identity
            if ident.kind == "agent" and ident.via == "direct" and glob_match(name, ident.require_approval):
                approval.append(name)
        note = "shell access gives full user rights; fs roots are guardrails, not a sandbox"
        if not self.p.exec_enabled:
            note = "exec is disabled on this node; fs roots and denies apply to every action"
        lim = self.limits
        return {
            "preset": self.p.preset, "read_only": self.p.read_only, "classes": sorted(self.identity.classes),
            "denied_actions": denied_actions, "approval_actions": approval, "fs_roots": self.roots(),
            "exec": {"enabled": self.p.exec_enabled, "mode": self.p.exec_mode, "shells": list(self.p.shells),
                     "default_timeout": lim.exec_default_timeout, "max_timeout": lim.exec_max_timeout},
            "net": {"http_allow": list(self.p.http_allow), "download_allow": list(self.p.download_allow)},
            "io_limits": {"max_read_bytes": lim.max_read_bytes, "max_write_bytes": lim.max_write_bytes,
                          "max_chunk_bytes": lim.max_chunk_bytes, "max_output_bytes": lim.max_output_bytes,
                          "max_response_bytes": lim.max_response_bytes},
            "limits": {"max_concurrent_calls": lim.max_concurrent_calls, "max_call_seconds": lim.max_call_seconds,
                       "max_result_bytes": lim.max_result_bytes},
            "note": note,
        }


__all__ = ["Limits", "Policy", "EffectivePolicy", "DEFAULT_SECRET_PATHS", "DEFAULT_EXEC_PATHS",
           "DEFAULT_COMMAND_DENY", "DEFAULT_KILL_PROTECT", "PATH_VARS", "windows_string_checks"]
