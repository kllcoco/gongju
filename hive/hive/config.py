"""Configuration: dataclasses, load/validate/defaults/presets/env-only/init_*, edit_config, protected_paths
(design §14, WP1).

Loading (§14): read JSON (UTF-8, BOM tolerated) -> drop every ``_``-prefixed key at every depth (comments, v7) ->
defaults (policy defaults come from the preset, §14.2) -> validation with ``schema.validate`` (unknown keys are a
startup error with a did-you-mean hint, D17; map-shaped blocks use ``additionalProperties: <schema>``) ->
``<DEFAULT_*>`` placeholder expansion -> cross-field checks -> dataclasses. Each dataclass has exactly the top-level
keys of its JSON block as fields. Env-only mode (pods without a config file) builds the node config in memory from
``HIVE_JOIN`` / ``HIVE_RELAY_*`` (§14.1). Config files are written with ``write_json_atomic(private=True)``.
"""
from __future__ import annotations

import copy
import dataclasses
import getpass
import logging
import os
import re
import socket
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, MutableMapping, Optional, Sequence, Tuple

from . import schema
from .errors import HiveError, bad_request, conflict, forbidden, not_found
from .util import (IS_WINDOWS, FileLock, atomic_write_bytes, glob_match, hash_token, ip_category, is_loopback_host,
                   iso_utc, new_token, read_json, secure_file, secure_mkdir, valid_name, write_json_atomic)

log = logging.getLogger("hive.config")

NODE_PORT = 7701
HUB_PORT = 7700
WSL_PORT = 7702
PRESETS = ("readonly", "standard", "cautious", "full")
ROLES = ("readonly", "files", "operator")
NAME_PATTERN = r"^[a-z0-9][a-z0-9_.-]{0,63}$"
SERVICE_NAME_PATTERN = r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,63}$"
SECRET_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]{0,127}$"
SHA_PATTERN = r"^sha256:[0-9a-f]{64}$"
PIN_PATTERN = r"^sha256:[0-9A-Fa-f:]{64,}$"
CAUTIOUS_DOWNLOAD_ALLOW = ("loopback", "huggingface.co", "*.huggingface.co", "hf.co", "*.hf.co", "modelscope.cn",
                           "*.modelscope.cn", "github.com", "pypi.org", "files.pythonhosted.org")
CAUTIOUS_REDIRECT_ALLOW = ("*.aliyuncs.com", "*.githubusercontent.com")
READONLY_DENY_ACTIONS = ("net.*", "model.chat", "model.pull", "model.forward")
FORBIDDEN_ENV_ALLOW = ("PATH", "LD_*", "DYLD_*", "PYTHON*", "BASH_ENV", "ENV", "NODE_OPTIONS", "PERL5*", "RUBYOPT",
                       "JAVA_TOOL_OPTIONS")
TSINGHUA_PYPI = "https://pypi.tuna.tsinghua.edu.cn/simple"
HF_MIRROR = "https://hf-mirror.com"
WORKSPACE = Path("/workspace")          # pod volume (RunPod/Vast); a module constant so tests can patch it
PLACEHOLDERS = ("<DEFAULT_SECRET_PATHS>", "<DEFAULT_EXEC_PATHS>", "<DEFAULT_COMMAND_DENY>", "<DEFAULT_KILL_PROTECT>")


# ---------------------------------------------------------------------------------------------------------------
# dataclasses (§14.5)

@dataclass
class TlsConfig:
    """listen.tls (§14.1)."""
    cert: Optional[str] = None
    key: Optional[str] = None


@dataclass
class ListenConfig:
    """Node ``listen`` block (§14.1)."""
    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = NODE_PORT
    proxy_port: Optional[int] = None
    tls: TlsConfig = field(default_factory=TlsConfig)
    allow_insecure_remote: bool = False
    allowed_hosts: List[str] = field(default_factory=list)
    allowed_origins: List[str] = field(default_factory=list)
    trusted_proxies: List[str] = field(default_factory=list)
    max_connections: int = 64
    max_body_bytes: int = 50331648
    max_buffered_bytes: int = 134217728


@dataclass
class HubListenConfig:
    """Hub ``listen`` block (§14.3)."""
    host: str = "127.0.0.1"
    port: int = HUB_PORT
    proxy_port: Optional[int] = None
    tls: TlsConfig = field(default_factory=TlsConfig)
    allow_insecure_remote: bool = False
    allowed_hosts: List[str] = field(default_factory=list)
    allowed_origins: List[str] = field(default_factory=list)
    trusted_proxies: List[str] = field(default_factory=list)
    max_connections: int = 256
    max_body_bytes: int = 50331648
    max_buffered_bytes: int = 268435456
    mcp_http: bool = True
    mcp_profile: str = "standard"


@dataclass
class RelayConfig:
    """Node ``relay`` block (§14.1)."""
    enabled: bool = False
    hub_url: Optional[str] = None
    token: Optional[str] = field(default=None, repr=False)
    token_file: Optional[str] = None
    tls_pin: Optional[str] = None
    ca_file: Optional[str] = None
    allow_insecure: bool = False
    max_parallel: int = 8
    proxy: str = "auto"
    role: str = "operator"
    deny_actions: List[str] = field(default_factory=list)
    auto_update: bool = False


@dataclass
class HubRelayConfig:
    """Hub ``relay`` block (§14.3)."""
    poll_wait: float = 25
    offline_after: float = 75
    pickup_timeout: float = 20
    relay_grace: float = 10
    lost_grace: float = 10
    max_queue_per_node: int = 64
    max_result_bytes: int = 33554432
    max_poll_bytes: int = 33554432


@dataclass
class TokenEntry:
    """One token entry (§5.2)."""
    name: str
    sha256: str
    kind: str
    role: Optional[str] = None
    classes: Optional[List[str]] = None
    deny_actions: List[str] = field(default_factory=list)
    require_approval: List[str] = field(default_factory=list)
    nodes: List[str] = field(default_factory=lambda: ["*"])
    models: List[str] = field(default_factory=list)
    expires: Optional[str] = None
    note: str = ""

    def to_dict(self) -> dict:
        """Plain dict (for TokenStore)."""
        return dataclasses.asdict(self)


@dataclass
class AuthConfig:
    """Node ``auth`` block."""
    tokens: List[TokenEntry] = field(default_factory=list)
    throttle: Dict[str, Any] = field(default_factory=lambda: {"max_failures": 10, "window": 60, "block": 300})


@dataclass
class PolicyConfig:
    """Node ``policy`` block (sub-blocks stay validated plain dicts, §4.6)."""
    preset: str = "standard"
    read_only: bool = False
    allow_actions: List[str] = field(default_factory=lambda: ["*"])
    deny_actions: List[str] = field(default_factory=list)
    fs: Dict[str, Any] = field(default_factory=dict)
    exec: Dict[str, Any] = field(default_factory=dict)
    jobs: Dict[str, Any] = field(default_factory=dict)
    services: Dict[str, Any] = field(default_factory=dict)
    net: Dict[str, Any] = field(default_factory=dict)
    proc: Dict[str, Any] = field(default_factory=dict)
    limits: Dict[str, Any] = field(default_factory=dict)
    approvals: Dict[str, Any] = field(default_factory=dict)


@dataclass
class NetworkConfig:
    """``network`` block."""
    proxy: Optional[str] = None
    no_proxy: List[str] = field(default_factory=list)
    use_system_proxy: bool = True
    proxy_children: bool = True
    strict_dns: bool = False
    ca_file: Optional[str] = None

    def to_dict(self) -> dict:
        """Plain dict (netproxy.load_proxy_settings input)."""
        return dataclasses.asdict(self)


@dataclass
class ModelsConfig:
    """``models`` block."""
    dir: Optional[str] = None
    hf_endpoint: Optional[str] = None
    modelscope_endpoint: str = "https://www.modelscope.cn"
    max_concurrent_pulls: int = 2


@dataclass
class SecretsConfig:
    """``secrets`` block."""
    env_passthrough: List[str] = field(default_factory=lambda: ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN",
                                                                "MODELSCOPE_API_TOKEN"])


@dataclass
class WslConfig:
    """``wsl`` block."""
    keepalive: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class GatewayConfig:
    """``gateway`` block (hub and node, §8.7)."""
    enabled: bool = True
    cors: bool = True
    max_body_bytes: int = 8388608
    max_response_bytes: int = 67108864
    timeout: float = 1800
    idle_timeout: float = 300
    max_inflight_per_token: int = 4


@dataclass
class NodeConfig:
    """Node config (§14.1). ``data_dir`` holds the resolved absolute data directory after loading."""
    version: int = 1
    name: str = ""
    node_id: Optional[str] = None
    description: str = ""
    tags: List[str] = field(default_factory=list)
    consent: Optional[Dict[str, Any]] = None
    allow_root: bool = False
    workdir: str = "~"
    data_dir: Optional[str] = None
    listen: ListenConfig = field(default_factory=ListenConfig)
    relay: RelayConfig = field(default_factory=RelayConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    models: ModelsConfig = field(default_factory=ModelsConfig)
    secrets: SecretsConfig = field(default_factory=SecretsConfig)
    wsl: WslConfig = field(default_factory=WslConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    model_endpoints: List[Dict[str, Any]] = field(default_factory=list)
    audit: Dict[str, Any] = field(default_factory=lambda: {"enabled": True, "retention_days": 30, "required": False})
    log: Dict[str, Any] = field(default_factory=lambda: {"level": "info", "file": "logs/node.log", "max_mb": 10,
                                                         "keep": 5})


@dataclass
class HubNodeEntry:
    """Hub ``nodes[]`` entry (§14.3)."""
    name: str
    mode: str
    token_sha256: Optional[str] = None
    url: Optional[str] = None
    token: Optional[str] = field(default=None, repr=False)
    tls_pin: Optional[str] = None
    ca_file: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    description: str = ""
    aliases: List[str] = field(default_factory=list)


@dataclass
class HubConfig:
    """Hub config (§14.3). ``data_dir`` holds the resolved absolute data directory after loading."""
    version: int = 1
    name: str = ""
    public_url: Optional[str] = None
    consent: Optional[Dict[str, Any]] = None
    allow_root: bool = False
    data_dir: Optional[str] = None
    listen: HubListenConfig = field(default_factory=HubListenConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    tokens: List[TokenEntry] = field(default_factory=list)
    throttle: Dict[str, Any] = field(default_factory=lambda: {"max_failures": 10, "window": 60, "block": 300})
    nodes: List[HubNodeEntry] = field(default_factory=list)
    default_node: Optional[str] = None
    relay: HubRelayConfig = field(default_factory=HubRelayConfig)
    limits: Dict[str, Any] = field(default_factory=dict)
    gateway: GatewayConfig = field(default_factory=GatewayConfig)
    approvals: Dict[str, Any] = field(default_factory=dict)
    dist: Dict[str, Any] = field(default_factory=dict)
    audit: Dict[str, Any] = field(default_factory=dict)
    log: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Connection:
    """Client connection (§14.4)."""
    url: str = ""
    token: Optional[str] = field(default=None, repr=False)
    token_file: Optional[str] = None
    tls_pin: Optional[str] = None
    ca_file: Optional[str] = None
    default_node: Optional[str] = None
    timeout: float = 120
    proxy: str = "auto"


@dataclass
class ToolsConfig:
    """Client ``tools`` block (§14.4)."""
    profile: str = "standard"
    include: List[str] = field(default_factory=list)
    exclude: List[str] = field(default_factory=list)
    prefix: str = ""
    wait_budget: Optional[float] = None
    char_budget: int = 20000
    structured_content: bool = False


@dataclass
class ClientConfig:
    """Client config (§14.4)."""
    version: int = 1
    default: Optional[str] = None
    connections: Dict[str, Connection] = field(default_factory=dict)
    tools: ToolsConfig = field(default_factory=ToolsConfig)
    local_files: Dict[str, Any] = field(default_factory=lambda: {"roots": ["~/Hive"]})


# ---------------------------------------------------------------------------------------------------------------
# schema building helpers

def _s(**kw: Any) -> dict:
    return dict({"type": "string"}, **kw)


def _i(lo: Optional[int] = None, hi: Optional[int] = None) -> dict:
    d: Dict[str, Any] = {"type": "integer"}
    if lo is not None:
        d["minimum"] = lo
    if hi is not None:
        d["maximum"] = hi
    return d


def _n(lo: Optional[float] = None, hi: Optional[float] = None) -> dict:
    d: Dict[str, Any] = {"type": "number"}
    if lo is not None:
        d["minimum"] = lo
    if hi is not None:
        d["maximum"] = hi
    return d


_B = {"type": "boolean"}


def _arr(items: dict, **kw: Any) -> dict:
    return dict({"type": "array", "items": items}, **kw)


def _o(props: Dict[str, dict], required: Sequence[str] = (), addl: Any = False) -> dict:
    d: Dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": addl}
    if required:
        d["required"] = list(required)
    return d


_STRS = _arr(_s())
_TLS = _o({"cert": _s(), "key": _s()})
_LISTEN_COMMON = {
    "host": _s(minLength=1, maxLength=255), "port": _i(0, 65535), "proxy_port": _i(1, 65535), "tls": _TLS,
    "allow_insecure_remote": _B, "allowed_hosts": _STRS, "allowed_origins": _STRS, "trusted_proxies": _STRS,
    "max_connections": _i(1, 4096), "max_body_bytes": _i(1024, 1 << 31), "max_buffered_bytes": _i(1 << 20, 1 << 34),
}
NODE_LISTEN_SCHEMA = _o(dict(_LISTEN_COMMON, enabled=_B))
HUB_LISTEN_SCHEMA = _o(dict(_LISTEN_COMMON, mcp_http=_B,
                            mcp_profile=_s(enum=["minimal", "standard", "full"])))
NODE_RELAY_SCHEMA = _o({
    "enabled": _B, "hub_url": _s(pattern=r"^https?://"), "token": _s(), "token_file": _s(),
    "tls_pin": _s(pattern=PIN_PATTERN), "ca_file": _s(), "allow_insecure": _B, "max_parallel": _i(1, 64),
    "proxy": _s(minLength=1), "role": _s(enum=list(ROLES)), "deny_actions": _STRS, "auto_update": _B,
})
HUB_RELAY_SCHEMA = _o({
    "poll_wait": _n(1, 120), "offline_after": _n(5, 3600), "pickup_timeout": _n(1, 600), "relay_grace": _n(0, 600),
    "lost_grace": _n(0, 600), "max_queue_per_node": _i(1, 10000), "max_result_bytes": _i(65536, 1 << 31),
    "max_poll_bytes": _i(65536, 1 << 31),
})
_TOKEN_SCHEMA = _o({
    "name": _s(pattern=NAME_PATTERN), "sha256": _s(pattern=SHA_PATTERN),
    "kind": _s(enum=["owner", "agent", "hub", "models"]), "role": _s(enum=list(ROLES)),
    "classes": _arr(_s(enum=["read", "write", "exec", "net", "persist"])), "deny_actions": _STRS,
    "require_approval": _STRS, "nodes": _STRS, "models": _STRS, "expires": _s(), "note": _s(maxLength=500),
}, required=("name", "sha256", "kind"))
_THROTTLE = _o({"max_failures": _i(1, 1000), "window": _n(1, 86400), "block": _n(1, 86400)})
GATEWAY_SCHEMA = _o({
    "enabled": _B, "cors": _B, "max_body_bytes": _i(1024, 1 << 31), "max_response_bytes": _i(1024, 1 << 34),
    "timeout": _n(1, 1800), "idle_timeout": _n(1, 3600), "max_inflight_per_token": _i(1, 1024),
})
NETWORK_SCHEMA = _o({
    "proxy": _s(), "no_proxy": _STRS, "use_system_proxy": _B, "proxy_children": _B, "strict_dns": _B, "ca_file": _s(),
})
MODELS_SCHEMA = _o({"dir": _s(), "hf_endpoint": _s(pattern=r"^https?://"),
                    "modelscope_endpoint": _s(pattern=r"^https?://"), "max_concurrent_pulls": _i(1, 16)})
_ROOT = _o({"path": _s(minLength=1), "mode": _s(enum=["rw", "ro"])}, required=("path",))
POLICY_SCHEMA = _o({
    "preset": _s(enum=list(PRESETS)), "read_only": _B, "allow_actions": _STRS, "deny_actions": _STRS,
    "fs": _o({"roots": _arr(_ROOT), "deny": _STRS, "deny_write": _STRS, "max_read_bytes": _i(1, 1 << 31),
              "max_write_bytes": _i(1, 1 << 31), "max_chunk_bytes": _i(1, 1 << 30),
              "max_delete_entries": _i(1, 10000000)}),
    "exec": _o({"enabled": _B, "mode": _s(enum=["open", "allowlist"]),
                "shells": _arr(_s(enum=["auto", "bash", "sh", "zsh", "powershell", "pwsh", "cmd"])),
                "command_allow": _STRS, "command_deny": _STRS, "env_allow": _STRS,
                "default_timeout": _n(1, 86400), "max_timeout": _n(1, 86400), "max_output_bytes": _i(1024, 1 << 30),
                "cwd_in_roots": _B, "scrub_env": _STRS,
                "child_env_defaults": {"type": "object", "properties": {}, "additionalProperties": _s()},
                "path_prepend": _STRS}),
    "jobs": _o({"max_running": _i(1, 1024), "max_records": _i(1, 100000), "retention_days": _n(0, 3650),
                "max_runtime": _n(0, 10 ** 7)}),
    "services": _o({"max": _i(0, 1024), "allow_autostart": _B, "stop_on_exit": _B}),
    "net": _o({"http_allow": _STRS, "http_deny": _STRS, "download_allow": _STRS, "download_redirect_allow": _STRS,
               "allow_metadata_ip": _B, "max_response_bytes": _i(1, 1 << 31)}),
    "proc": _o({"kill_protect": _STRS, "kill_own_user_only": _B}),
    "limits": _o({"max_concurrent_calls": _i(1, 1024), "max_call_seconds": _n(1, 86400),
                  "max_result_bytes": _i(65536, 1 << 31), "max_calls_per_identity": _i(1, 1024),
                  "owner_reserved_calls": _i(0, 64)}),
    "approvals": _o({"ttl": _i(10, 86400), "max_pending": _i(1, 1000),
                     "decide_from": _arr(_s(enum=["loopback", "private", "any"])),
                     "max_grant_minutes": _i(1, 1440)}),
})
_CONSENT = _o({"by": _s(minLength=1), "at": _s(), "text": _s()}, required=("by",))
_AUDIT = _o({"enabled": _B, "retention_days": _i(0, 3650), "required": _B})
_LOG = _o({"level": _s(enum=["debug", "info", "warning", "error"]), "file": _s(minLength=1),
           "max_mb": _i(1, 10000), "keep": _i(0, 100)})
NODE_SCHEMA = _o({
    "version": _i(1, 1), "name": _s(pattern=NAME_PATTERN), "node_id": _s(pattern=r"^n_[A-Za-z0-9_-]{4,64}$"),
    "description": _s(maxLength=2000), "tags": _STRS, "consent": _CONSENT, "allow_root": _B, "workdir": _s(minLength=1),
    "data_dir": _s(minLength=1), "listen": NODE_LISTEN_SCHEMA, "relay": NODE_RELAY_SCHEMA,
    "auth": _o({"tokens": _arr(_TOKEN_SCHEMA), "throttle": _THROTTLE}), "gateway": GATEWAY_SCHEMA,
    "network": NETWORK_SCHEMA, "models": MODELS_SCHEMA,
    "secrets": _o({"env_passthrough": _arr(_s(pattern=SECRET_NAME_PATTERN))}),
    # M3-3: `command` is an argv list (§20.7 step 6: ["/bin/sh","-lc","exec $HOME/.hive/node/run-node.sh"]); wsl.exe
    # --exec runs it without a shell, and wsl.Keepalive only uses a list (a string was silently ignored)
    "wsl": _o({"keepalive": _arr(_o({"distro": _s(minLength=1, maxLength=128),
                                     "command": _arr(_s(minLength=1, maxLength=4096), minItems=1, maxItems=64)},
                                    required=("distro",)))}),
    "policy": POLICY_SCHEMA,
    "model_endpoints": _arr(_o({"name": _s(pattern=SERVICE_NAME_PATTERN), "base_url": _s(pattern=r"^https?://"),
                                "api_key_env": _s(pattern=SECRET_NAME_PATTERN)}, required=("name", "base_url"))),
    "audit": _AUDIT, "log": _LOG,
}, required=("version", "name"))
_HUB_NODE = _o({
    "name": _s(pattern=NAME_PATTERN), "mode": _s(enum=["relay", "direct"]), "token_sha256": _s(pattern=SHA_PATTERN),
    "url": _s(pattern=r"^https?://"), "token": _s(), "tls_pin": _s(pattern=PIN_PATTERN), "ca_file": _s(),
    "tags": _STRS, "description": _s(maxLength=2000), "aliases": _arr(_s(pattern=NAME_PATTERN)),
}, required=("name", "mode"))
HUB_SCHEMA = _o({
    "version": _i(1, 1), "name": _s(pattern=NAME_PATTERN), "public_url": _s(pattern=r"^https?://"),
    "consent": _CONSENT, "allow_root": _B, "data_dir": _s(minLength=1), "listen": HUB_LISTEN_SCHEMA,
    "network": NETWORK_SCHEMA, "tokens": _arr(_TOKEN_SCHEMA), "throttle": _THROTTLE, "nodes": _arr(_HUB_NODE),
    "default_node": _s(), "relay": HUB_RELAY_SCHEMA,
    "limits": _o({"max_call_seconds": _n(1, 86400), "max_inflight_per_agent": _i(1, 10000),
                  "call_timeout_slack": _n(0, 600)}),
    "gateway": GATEWAY_SCHEMA,
    "approvals": _o({"ttl": _i(10, 86400), "max_pending": _i(1, 1000),
                     "decide_from": _arr(_s(enum=["loopback", "private", "any"])),
                     "max_grant_minutes": _i(1, 1440)}),
    "dist": _o({"enabled": _B}), "audit": _AUDIT, "log": _LOG,
}, required=("version", "name"))
_CONNECTION = _o({"url": _s(pattern=r"^https?://"), "token": _s(), "token_file": _s(),
                  "tls_pin": _s(pattern=PIN_PATTERN), "ca_file": _s(), "default_node": _s(), "timeout": _n(1, 86400),
                  "proxy": _s(minLength=1)}, required=("url",))
CLIENT_SCHEMA = _o({
    "version": _i(1, 1), "default": _s(),
    "connections": {"type": "object", "properties": {}, "additionalProperties": _CONNECTION},
    "tools": _o({"profile": _s(enum=["minimal", "standard", "full"]), "include": _STRS, "exclude": _STRS,
                 "prefix": _s(pattern=r"^[a-z0-9_]{0,16}$"), "wait_budget": _n(1, 3600),
                 "char_budget": _i(1000, 1000000), "structured_content": _B}),
    "local_files": _o({"roots": _STRS}),
}, required=("version",))


# ---------------------------------------------------------------------------------------------------------------
# defaults

def standard_policy() -> dict:
    """The §14.1 standard policy block (with placeholders, unexpanded)."""
    return {
        "preset": "standard", "read_only": False, "allow_actions": ["*"], "deny_actions": [],
        "fs": {"roots": [{"path": "~", "mode": "rw"}], "deny": ["<DEFAULT_SECRET_PATHS>"],
               "deny_write": ["<DEFAULT_EXEC_PATHS>"], "max_read_bytes": 8388608, "max_write_bytes": 16777216,
               "max_chunk_bytes": 8388608, "max_delete_entries": 10000},
        "exec": {"enabled": True, "mode": "open", "shells": ["auto", "bash", "sh", "zsh", "powershell", "pwsh", "cmd"],
                 "command_allow": [], "command_deny": ["<DEFAULT_COMMAND_DENY>"], "env_allow": [],
                 "default_timeout": 60, "max_timeout": 3600, "max_output_bytes": 1048576, "cwd_in_roots": True,
                 "scrub_env": ["HIVE_*"],
                 "child_env_defaults": {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "NO_COLOR": "1",
                                        "PIP_PROGRESS_BAR": "off"},
                 "path_prepend": []},
        "jobs": {"max_running": 32, "max_records": 200, "retention_days": 7, "max_runtime": 0},
        "services": {"max": 32, "allow_autostart": True, "stop_on_exit": False},
        "net": {"http_allow": ["loopback", "public"], "http_deny": [], "download_allow": ["loopback", "public"],
                "download_redirect_allow": [], "allow_metadata_ip": False, "max_response_bytes": 8388608},
        "proc": {"kill_protect": ["<DEFAULT_KILL_PROTECT>"], "kill_own_user_only": True},
        "limits": {"max_concurrent_calls": 32, "max_call_seconds": 3700, "max_result_bytes": 33554432},
        "approvals": {"ttl": 600, "max_pending": 50, "decide_from": ["loopback"], "max_grant_minutes": 240},
    }


def _parse_root_spec(spec: str) -> Dict[str, str]:
    s = str(spec).strip()
    mode = "rw"
    for suffix in (":ro", ":rw"):
        if s.lower().endswith(suffix) and len(s) > len(suffix):
            head = s[:-len(suffix)]
            # "C:" alone (a drive) is not a mode suffix
            if not re.fullmatch(r"[A-Za-z]", head):
                mode = suffix[1:]
                s = head
            break
    if not s:
        raise bad_request(f"invalid root {spec!r}", hint="use PATH or PATH:ro")
    return {"path": s, "mode": mode}


def apply_preset(policy: dict, preset: str, *, workdir: Path, roots: Sequence[str]) -> dict:
    """Apply a policy preset (§14.2) to a policy block and append ``--root PATH[:ro]`` roots."""
    if preset not in PRESETS:
        raise bad_request(f"unknown preset {preset!r}", hint="presets: " + ", ".join(PRESETS))
    p = copy.deepcopy(policy)
    p["preset"] = preset
    for k in ("fs", "exec", "net"):
        p.setdefault(k, {})
    if preset == "readonly":
        p["read_only"] = True
        p["exec"]["enabled"] = False
        p["fs"]["roots"] = [{"path": "~", "mode": "ro"}]
        p["net"]["http_allow"] = ["loopback"]
        deny = list(p.get("deny_actions") or [])
        p["deny_actions"] = deny + [a for a in READONLY_DENY_ACTIONS if a not in deny]
    elif preset == "cautious":
        p["net"]["http_allow"] = ["loopback"]
        p["net"]["download_allow"] = list(CAUTIOUS_DOWNLOAD_ALLOW)
        p["net"]["download_redirect_allow"] = list(CAUTIOUS_REDIRECT_ALLOW)
    elif preset == "full":
        p["fs"]["roots"] = [{"path": "*", "mode": "rw"}]
        p["fs"]["deny"] = []
        p["fs"]["deny_write"] = []
        p["exec"]["command_deny"] = []
        p["net"]["http_allow"] = ["loopback", "private", "public"]
        p["net"]["download_allow"] = ["loopback", "private", "public"]
    existing = p["fs"].setdefault("roots", [])
    for r in roots or ():
        entry = _parse_root_spec(r)
        if not any(x.get("path") == entry["path"] for x in existing):
            existing.append(entry)
    return p


def _node_defaults() -> dict:
    return {
        "version": 1, "name": "", "node_id": None, "description": "", "tags": [], "consent": None,
        "allow_root": False, "workdir": "~", "data_dir": None,
        "listen": {"enabled": True, "host": "127.0.0.1", "port": NODE_PORT, "proxy_port": None,
                   "tls": {"cert": None, "key": None}, "allow_insecure_remote": False, "allowed_hosts": [],
                   "allowed_origins": [], "trusted_proxies": [], "max_connections": 64, "max_body_bytes": 50331648,
                   "max_buffered_bytes": 134217728},
        "relay": {"enabled": False, "hub_url": None, "token": None, "token_file": None, "tls_pin": None,
                  "ca_file": None, "allow_insecure": False, "max_parallel": 8, "proxy": "auto", "role": "operator",
                  "deny_actions": [], "auto_update": False},
        "auth": {"tokens": [], "throttle": {"max_failures": 10, "window": 60, "block": 300}},
        "gateway": {"enabled": True, "cors": True, "max_body_bytes": 8388608, "max_response_bytes": 67108864,
                    "timeout": 1800, "idle_timeout": 300, "max_inflight_per_token": 4},
        "network": {"proxy": None, "no_proxy": [], "use_system_proxy": True, "proxy_children": True,
                    "strict_dns": False, "ca_file": None},
        "models": {"dir": None, "hf_endpoint": None, "modelscope_endpoint": "https://www.modelscope.cn",
                   "max_concurrent_pulls": 2},
        "secrets": {"env_passthrough": ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "MODELSCOPE_API_TOKEN"]},
        "wsl": {"keepalive": []},
        "policy": standard_policy(),
        "model_endpoints": [],
        "audit": {"enabled": True, "retention_days": 30, "required": False},
        "log": {"level": "info", "file": "logs/node.log", "max_mb": 10, "keep": 5},
    }


def _hub_defaults() -> dict:
    return {
        "version": 1, "name": "", "public_url": None, "consent": None, "allow_root": False, "data_dir": None,
        "listen": {"host": "127.0.0.1", "port": HUB_PORT, "proxy_port": None, "tls": {"cert": None, "key": None},
                   "allow_insecure_remote": False, "allowed_hosts": [], "allowed_origins": [], "trusted_proxies": [],
                   "max_connections": 256, "max_body_bytes": 50331648, "max_buffered_bytes": 268435456,
                   "mcp_http": True, "mcp_profile": "standard"},
        "network": {"proxy": None, "no_proxy": [], "use_system_proxy": True, "proxy_children": True,
                    "strict_dns": False, "ca_file": None},
        "tokens": [], "throttle": {"max_failures": 10, "window": 60, "block": 300}, "nodes": [], "default_node": None,
        "relay": {"poll_wait": 25, "offline_after": 75, "pickup_timeout": 20, "relay_grace": 10, "lost_grace": 10,
                  "max_queue_per_node": 64, "max_result_bytes": 33554432, "max_poll_bytes": 33554432},
        "limits": {"max_call_seconds": 3700, "max_inflight_per_agent": 32, "call_timeout_slack": 15},
        "gateway": {"enabled": True, "cors": True, "max_body_bytes": 8388608, "max_response_bytes": 67108864,
                    "timeout": 1800, "idle_timeout": 300, "max_inflight_per_token": 4},
        "approvals": {"ttl": 600, "max_pending": 50, "decide_from": ["loopback"], "max_grant_minutes": 240},
        "dist": {"enabled": True},
        "audit": {"enabled": True, "retention_days": 90, "required": False},
        "log": {"level": "info", "file": "logs/hub.log", "max_mb": 10, "keep": 5},
    }


def _client_defaults() -> dict:
    return {"version": 1, "default": None, "connections": {},
            "tools": {"profile": "standard", "include": [], "exclude": [], "prefix": "", "wait_budget": None,
                      "char_budget": 20000, "structured_content": False},
            "local_files": {"roots": ["~/Hive"]}}


# ---------------------------------------------------------------------------------------------------------------
# generic loading helpers

def _strip_comments(obj: Any) -> Any:
    """Drop every ``_``-prefixed key at every depth (objects inside lists included); returns a copy."""
    if isinstance(obj, dict):
        return {k: _strip_comments(v) for k, v in obj.items() if not (isinstance(k, str) and k.startswith("_"))}
    if isinstance(obj, list):
        return [_strip_comments(v) for v in obj]
    return copy.deepcopy(obj)


def _merge(base: dict, over: Mapping[str, Any]) -> dict:
    """Deep merge (dicts merged, everything else replaced); explicit null keeps the default."""
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif v is None and k in out:
            continue
        else:
            out[k] = copy.deepcopy(v)
    return out


def _validate(sch: dict, raw: dict, what: str, path: Optional[Path]) -> dict:
    try:
        return schema.validate(sch, raw, coerce=False, path="config")
    except HiveError as e:
        where = f" {path}" if path else ""
        hint = "fix the key named above (unknown keys are errors; keys starting with _ are comments)"
        raise bad_request(f"invalid {what} config{where}: {e.message.split(': ', 1)[-1]}", hint=hint,
                          errors=e.details.get("errors", []))


def _expand_list(items: List[Any], table: Mapping[str, Sequence[str]]) -> List[Any]:
    out: List[Any] = []
    for it in items:
        if isinstance(it, str) and it in table:
            for x in table[it]:
                if x not in out:
                    out.append(x)
        elif it not in out or not isinstance(it, str):
            out.append(it)
    return out


def expand_placeholders(raw: dict) -> dict:
    """``<DEFAULT_*>`` expansion in every list (used only at load); returns a copy."""
    from . import policy as _policy
    table = {"<DEFAULT_SECRET_PATHS>": _policy.DEFAULT_SECRET_PATHS,
             "<DEFAULT_EXEC_PATHS>": _policy.DEFAULT_EXEC_PATHS,
             "<DEFAULT_COMMAND_DENY>": _policy.DEFAULT_COMMAND_DENY,
             "<DEFAULT_KILL_PROTECT>": _policy.DEFAULT_KILL_PROTECT}

    def walk(o: Any) -> Any:
        if isinstance(o, dict):
            return {k: walk(v) for k, v in o.items()}
        if isinstance(o, list):
            return [walk(v) for v in _expand_list(o, table)]
        return o
    return walk(raw)


def _check_file_mode(path: Path) -> None:
    if IS_WINDOWS:  # pragma: no cover - ACLs are verified by doctor
        return
    try:
        st = os.stat(path)
    except OSError:
        return
    mode = stat.S_IMODE(st.st_mode)
    if mode & stat.S_IWOTH:
        raise forbidden(f"config file {path} is world-writable; refusing to load it",
                        hint=f"fix it: chmod 600 {path}")
    if mode & 0o077 and st.st_uid == os.getuid():
        try:
            os.chmod(path, 0o600)
            log.warning("config file %s had mode %o; fixed to 600", path, mode)
        except OSError:
            log.warning("config file %s has mode %o (should be 600)", path, mode)


def _read_raw(path: Path, what: str) -> dict:
    if not path.exists():
        raise not_found(f"no {what} config at {path}", hint=f"run: hive init {what}")
    _check_file_mode(path)
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise bad_request(f"invalid {what} config {path}: the top level must be a JSON object")
    return raw


def _tokens(items: List[dict]) -> List[TokenEntry]:
    out = []
    for t in items:
        out.append(TokenEntry(name=t["name"], sha256=t["sha256"], kind=t["kind"], role=t.get("role"),
                              classes=t.get("classes"), deny_actions=list(t.get("deny_actions") or []),
                              require_approval=list(t.get("require_approval") or []),
                              nodes=list(t.get("nodes") or ["*"]), models=list(t.get("models") or []),
                              expires=t.get("expires"), note=t.get("note") or ""))
    return out


def _check_tokens(tokens: List[dict], role: str) -> None:
    names, hashes = set(), set()
    allowed = ("owner", "agent", "hub", "models") if role == "node" else ("owner", "agent", "models")
    for t in tokens:
        n = t["name"]
        if n in names:
            raise bad_request(f"duplicate token name {n!r} in config")
        if t["sha256"] in hashes:
            raise bad_request(f"token {n!r} has the same hash as another token")
        names.add(n)
        hashes.add(t["sha256"])
        if t["kind"] not in allowed:
            raise bad_request(f"token {n!r}: kind {t['kind']!r} is not allowed in a {role} config",
                              hint="kinds: " + ", ".join(allowed))
        if t.get("require_approval") and t["kind"] != "agent":
            raise bad_request(f"token {n!r}: require_approval is only for agent tokens")
        if t.get("models") and t["kind"] != "models":
            raise bad_request(f"token {n!r}: models is only for kind models")
        if t["kind"] == "models" and not t.get("models"):
            raise bad_request(f"token {n!r}: a models token needs a models list")
        if t.get("expires"):
            from .util import parse_iso
            parse_iso(t["expires"])


def _check_policy(pol: dict) -> None:
    ex = pol["exec"]
    for name in ex.get("env_allow") or []:
        if glob_match(str(name).upper(), FORBIDDEN_ENV_ALLOW):
            raise bad_request(f"policy.exec.env_allow may not contain {name!r} (it changes what runs)",
                              hint="PATH, LD_*, DYLD_*, PYTHON*, BASH_ENV, ENV, NODE_OPTIONS, PERL5*, RUBYOPT and "
                                   "JAVA_TOOL_OPTIONS cannot be allowed")
    for key in ("command_deny", "command_allow"):
        for rx in ex.get(key) or []:
            try:
                re.compile(rx)
            except re.error as e:
                raise bad_request(f"policy.exec.{key}: invalid regex {rx!r}: {e}")
    for r in pol["fs"].get("roots") or []:
        r.setdefault("mode", "rw")
    if float(ex.get("default_timeout", 60)) > float(ex.get("max_timeout", 3600)):
        raise bad_request("policy.exec.default_timeout is larger than max_timeout")


def _resolve_dir(value: Optional[str], base: Path) -> str:
    if not value:
        return str(base.resolve())
    p = Path(os.path.expanduser(value))
    if not p.is_absolute():
        p = base / p
    return str(p.resolve())


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def _is_tailnet_or_private(host: str, *, resolve: bool = True) -> bool:
    if not host:
        return False
    if host.endswith(".ts.net"):
        return True
    try:
        return ip_category(host) in ("loopback", "private")
    except HiveError:
        pass
    if host == "localhost":
        return True
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
        cats = {ip_category(i[4][0]) for i in infos}
        return bool(cats) and cats <= {"loopback", "private"}
    except (OSError, HiveError, UnicodeError):
        return False


def _relay_insecure_ok(url: Optional[str], *, resolve: bool = True) -> bool:
    """True when an http:// hub URL points at a loopback/private/tailnet host (§5.9 join rule)."""
    if not url or not url.lower().startswith("http://"):
        return False
    return _is_tailnet_or_private(_host_of(url), resolve=resolve)


# ---------------------------------------------------------------------------------------------------------------
# node config

def _parse_node(raw: dict, path: Optional[Path], *, data_dir: Optional[str] = None) -> NodeConfig:
    raw = _strip_comments(raw)
    if raw.get("version", 1) != 1:
        raise bad_request(f"unsupported node config version {raw.get('version')!r}", hint="expected 1")
    preset = (raw.get("policy") or {}).get("preset") or "standard"
    if not isinstance(preset, str) or preset not in PRESETS:
        raise bad_request(f"invalid node config: unknown policy preset {preset!r}", hint="presets: " + ", ".join(PRESETS))
    defaults = _node_defaults()
    defaults["policy"] = apply_preset(standard_policy(), preset, workdir=Path("~"), roots=())
    merged = _merge(defaults, raw)
    v = _validate(NODE_SCHEMA, merged, "node", path)
    v = expand_placeholders(v)
    _check_tokens(v["auth"]["tokens"], "node")
    _check_policy(v["policy"])
    lst, rel = v["listen"], v["relay"]
    if bool(lst["tls"].get("cert")) != bool(lst["tls"].get("key")):
        raise bad_request("listen.tls needs both cert and key")
    if rel["enabled"]:
        if not rel.get("hub_url"):
            raise bad_request("relay.enabled needs relay.hub_url")
        if not rel.get("token") and not rel.get("token_file"):
            raise bad_request("relay.enabled needs relay.token or relay.token_file")
    names = [m["name"] for m in v["model_endpoints"]]
    if len(names) != len(set(names)):
        raise bad_request("model_endpoints names must be unique")
    base = path.parent if path is not None else Path.cwd()
    v["data_dir"] = data_dir or _resolve_dir(v.get("data_dir"), base)
    pol = v["policy"]
    cfg = NodeConfig(
        version=1, name=v["name"], node_id=v.get("node_id"), description=v.get("description") or "",
        tags=list(v["tags"]), consent=v.get("consent"), allow_root=v["allow_root"], workdir=v["workdir"],
        data_dir=v["data_dir"],
        listen=ListenConfig(**dict(lst, tls=TlsConfig(**lst["tls"]))),
        relay=RelayConfig(**rel),
        auth=AuthConfig(tokens=_tokens(v["auth"]["tokens"]), throttle=dict(v["auth"]["throttle"])),
        gateway=GatewayConfig(**v["gateway"]), network=NetworkConfig(**v["network"]),
        models=ModelsConfig(**v["models"]), secrets=SecretsConfig(**v["secrets"]),
        wsl=WslConfig(keepalive=[dict(k) for k in v["wsl"]["keepalive"]]),
        policy=PolicyConfig(**pol), model_endpoints=[dict(m) for m in v["model_endpoints"]],
        audit=dict(v["audit"]), log=dict(v["log"]))
    cfg._path = path  # type: ignore[attr-defined]
    cfg._env_only = False  # type: ignore[attr-defined]
    return cfg


def hive_home(env: Mapping[str, str] = os.environ) -> Path:
    """``HIVE_HOME`` or ``~/.hive``; pods without ``~/.hive`` use ``/workspace/.hive`` (§14 v6 rule)."""
    h = env.get("HIVE_HOME")
    if h:
        return Path(os.path.expanduser(h))
    home = Path(os.path.expanduser("~")) / ".hive"
    if not home.exists() and not IS_WINDOWS:
        ws = WORKSPACE
        if (ws / ".hive").exists() or (ws.is_dir() and (env.get("RUNPOD_POD_ID") or env.get("HIVE_TOPOLOGY") == "pod")):
            return ws / ".hive"
    return home


def default_paths(env: Mapping[str, str] = os.environ) -> Dict[str, Path]:
    """Default config/data paths: home, node_config, node_data, hub_config, hub_data, client_config, tokens."""
    home = hive_home(env)
    return {"home": home, "node_config": home / "node" / "config.json", "node_data": home / "node",
            "hub_config": home / "hub" / "config.json", "hub_data": home / "hub",
            "client_config": home / "client.json", "tokens": home / "tokens"}


def _sanitize_name(s: str) -> str:
    n = re.sub(r"[^a-z0-9_.-]+", "-", (s or "").lower()).strip("-._")
    n = n[:64] or "node"
    if not re.match(r"^[a-z0-9]", n):
        n = "n" + n[:63]
    return n


def _pod_dirs() -> Dict[str, Any]:
    autodl = Path("/root/autodl-tmp")
    ws = Path("/workspace")
    base = autodl if autodl.is_dir() else ws
    roots = [{"path": "/workspace", "mode": "rw"}, {"path": "/root", "mode": "rw"}, {"path": "/tmp", "mode": "rw"},
             {"path": "~", "mode": "rw"}]
    if autodl.is_dir():
        roots.append({"path": str(autodl), "mode": "rw"})
    hf_home = str(base / ".cache" / "huggingface")
    return {"roots": roots, "workdir": "/workspace" if ws.is_dir() else "~", "models_dir": str(base / "models"),
            "hf_home": hf_home if base.is_dir() else None}


def _apply_pod(raw: dict) -> None:
    pod = _pod_dirs()
    raw["workdir"] = pod["workdir"]
    raw["allow_root"] = True
    pol = raw.setdefault("policy", {})
    fs = pol.setdefault("fs", {})
    roots = fs.setdefault("roots", [{"path": "~", "mode": "rw"}])
    for r in pod["roots"]:
        if not any(x.get("path") == r["path"] for x in roots):
            roots.append(r)
    raw.setdefault("models", {})["dir"] = pod["models_dir"]
    if pod["hf_home"]:
        ex = pol.setdefault("exec", {})
        cd = ex.setdefault("child_env_defaults", dict(standard_policy()["exec"]["child_env_defaults"]))
        cd["HF_HOME"] = pod["hf_home"]
        deny = fs.setdefault("deny", ["<DEFAULT_SECRET_PATHS>"])
        tok = pod["hf_home"] + "/token"
        if tok not in deny:
            deny.append(tok)
    tags = raw.setdefault("tags", [])
    if os.environ.get("RUNPOD_POD_ID") and "runpod" not in tags:
        tags.append("runpod")
    if "cloud" not in tags:
        tags.append("cloud")


def _env_only_config(env: MutableMapping[str, str]) -> NodeConfig:
    from .dist import decode_join
    if env.get("HIVE_JOIN"):
        j = decode_join(env["HIVE_JOIN"])
        url, token, pin, jname = j["u"], j["t"], j.get("p"), j.get("n")
    else:
        url, token, pin, jname = env.get("HIVE_RELAY_URL"), env.get("HIVE_RELAY_TOKEN"), env.get("HIVE_RELAY_PIN"), None
    for k in ("HIVE_JOIN", "HIVE_RELAY_TOKEN", "HIVE_RELAY_URL"):
        try:
            env.pop(k, None)
        except (TypeError, AttributeError):
            pass
    home = hive_home(env)
    data = home / "node"
    secure_mkdir(data, required=True)
    id_file = data / "node_id"
    node_id = None
    try:
        node_id = id_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        pass
    if not node_id or not re.match(r"^n_[A-Za-z0-9_-]{4,64}$", node_id):
        node_id = "n_" + os.urandom(8).hex()
        atomic_write_bytes(id_file, node_id.encode("utf-8"), private=True)
    name = (env.get("HIVE_NODE_NAME") or jname or _sanitize_name(socket.gethostname())).lower()
    preset = env.get("HIVE_PRESET") or "standard"
    role = env.get("HIVE_RELAY_ROLE") or "operator"
    if role not in ROLES:
        raise bad_request(f"HIVE_RELAY_ROLE {role!r} is not a role", hint="readonly | files | operator")
    raw: Dict[str, Any] = {
        "version": 1, "name": name, "node_id": node_id,
        "consent": {"by": "env", "at": iso_utc(), "text": "consent given by HIVE_JOIN"},
        "listen": {"enabled": False},
        "relay": {"enabled": True, "hub_url": url, "token": token, "tls_pin": pin, "role": role,
                  "allow_insecure": _relay_insecure_ok(url), "proxy": env.get("HIVE_RELAY_PROXY") or "auto",
                  "auto_update": env.get("HIVE_WATCHDOG") == "1" and role == "operator"},
        "policy": {"preset": preset},
    }
    if env.get("HIVE_PROXY"):
        raw["network"] = {"proxy": env["HIVE_PROXY"]}
    if env.get("HIVE_HF_ENDPOINT"):
        raw.setdefault("models", {})["hf_endpoint"] = env["HIVE_HF_ENDPOINT"]
    if env.get("HIVE_TOPOLOGY") == "pod" or env.get("RUNPOD_POD_ID"):
        _apply_pod(raw)
    if env.get("HIVE_WORKDIR"):
        raw["workdir"] = env["HIVE_WORKDIR"]
    if raw["relay"]["allow_insecure"]:
        log.info("plaintext relay over the local network/tailnet (%s)", _host_of(url or ""))
    cfg = _parse_node(raw, None, data_dir=str(data.resolve()))
    cfg._env_only = True  # type: ignore[attr-defined]
    return cfg


def load_node_config(path: Optional[str] = None, *, env: Mapping[str, str] = os.environ) -> NodeConfig:
    """Load the node config (env-only mode when there is no config file and HIVE_JOIN/HIVE_RELAY_* are set)."""
    if path is None:
        p = default_paths(env)["node_config"]
        if not p.exists() and (env.get("HIVE_JOIN") or (env.get("HIVE_RELAY_URL") and env.get("HIVE_RELAY_TOKEN"))):
            return _env_only_config(env)  # type: ignore[arg-type]
    else:
        p = Path(os.path.expanduser(str(path)))
    p = p.resolve() if p.exists() else p
    return _parse_node(_read_raw(p, "node"), p)


# ---------------------------------------------------------------------------------------------------------------
# hub and client config

def _parse_hub(raw: dict, path: Optional[Path]) -> HubConfig:
    raw = _strip_comments(raw)
    if raw.get("version", 1) != 1:
        raise bad_request(f"unsupported hub config version {raw.get('version')!r}", hint="expected 1")
    v = _validate(HUB_SCHEMA, _merge(_hub_defaults(), raw), "hub", path)
    _check_tokens(v["tokens"], "hub")
    names = set()
    for n in v["nodes"]:
        if n["name"] in names:
            raise bad_request(f"duplicate hub node {n['name']!r}")
        names.add(n["name"])
        if n["mode"] == "relay" and not n.get("token_sha256"):
            raise bad_request(f"relay node {n['name']!r} needs token_sha256")
        if n["mode"] == "direct" and (not n.get("url") or not n.get("token")):
            raise bad_request(f"direct node {n['name']!r} needs url and token")
    lst = v["listen"]
    if bool(lst["tls"].get("cert")) != bool(lst["tls"].get("key")):
        raise bad_request("listen.tls needs both cert and key")
    base = path.parent if path is not None else default_paths()["hub_data"]
    v["data_dir"] = _resolve_dir(v.get("data_dir"), base)
    cfg = HubConfig(
        version=1, name=v["name"], public_url=v.get("public_url"), consent=v.get("consent"),
        allow_root=v["allow_root"], data_dir=v["data_dir"],
        listen=HubListenConfig(**dict(lst, tls=TlsConfig(**lst["tls"]))), network=NetworkConfig(**v["network"]),
        tokens=_tokens(v["tokens"]), throttle=dict(v["throttle"]),
        nodes=[HubNodeEntry(**dict({"tags": [], "aliases": [], "description": ""}, **n)) for n in v["nodes"]],
        default_node=v.get("default_node"), relay=HubRelayConfig(**v["relay"]), limits=dict(v["limits"]),
        gateway=GatewayConfig(**v["gateway"]), approvals=dict(v["approvals"]), dist=dict(v["dist"]),
        audit=dict(v["audit"]), log=dict(v["log"]))
    cfg._path = path  # type: ignore[attr-defined]
    return cfg


def load_hub_config(path: Optional[str] = None) -> HubConfig:
    """Load the hub config."""
    p = Path(os.path.expanduser(str(path))) if path else default_paths()["hub_config"]
    p = p.resolve() if p.exists() else p
    return _parse_hub(_read_raw(p, "hub"), p)


def _parse_client(raw: dict, path: Optional[Path]) -> ClientConfig:
    raw = _strip_comments(raw)
    v = _validate(CLIENT_SCHEMA, _merge(_client_defaults(), raw), "client", path)
    conns = {}
    for name, c in v["connections"].items():
        if not re.match(NAME_PATTERN, name):
            raise bad_request(f"invalid connection name {name!r}", hint="lowercase letters, digits, . _ -")
        conns[name] = Connection(**dict({"timeout": 120, "proxy": "auto"}, **c))
    if v.get("default") and v["default"] not in conns:
        raise bad_request(f"client default {v['default']!r} is not a connection")
    cfg = ClientConfig(version=1, default=v.get("default"), connections=conns, tools=ToolsConfig(**v["tools"]),
                       local_files=dict(v["local_files"]))
    cfg._path = path  # type: ignore[attr-defined]
    return cfg


def load_client_config(path: Optional[str] = None) -> ClientConfig:
    """Load client.json (an empty config when the file does not exist)."""
    p = Path(os.path.expanduser(str(path))) if path else default_paths()["client_config"]
    if not p.exists():
        cfg = _parse_client({"version": 1}, p)
        return cfg
    return _parse_client(_read_raw(p, "client"), p)


_PARSERS: Dict[str, Callable[[dict, Optional[Path]], Any]] = {"node": _parse_node, "hub": _parse_hub,
                                                                "client": _parse_client}


def edit_config(path: Path, fn: Callable[[dict], None], *, kind: str) -> None:
    """Locked read-modify-validate-write of the RAW config (comments and placeholders survive)."""
    if kind not in _PARSERS:
        raise ValueError(f"unknown config kind {kind!r}")
    path = Path(path)
    lock = FileLock(str(path) + ".lock")
    deadline = time.monotonic() + 10
    while not lock.acquire(note="edit_config"):
        if time.monotonic() > deadline:
            raise conflict(f"{path} is being edited by another process; try again")
        time.sleep(0.05)
    try:
        raw = read_json(path, default=None)
        if raw is None:
            raw = {"version": 1}
        if not isinstance(raw, dict):
            raise bad_request(f"{path}: the top level must be a JSON object")
        fn(raw)
        _PARSERS[kind](copy.deepcopy(raw), path)          # validation; errors abort with nothing written
        write_json_atomic(path, raw, private=True)
        secure_file(path, required=True)
    finally:
        lock.release()


# ---------------------------------------------------------------------------------------------------------------
# protected paths and startup checks

def _p(value: Optional[str], base: Optional[Path] = None) -> Optional[Path]:
    if not value:
        return None
    p = Path(os.path.expanduser(str(value)))
    if not p.is_absolute() and base is not None:
        p = base / p
    return p


def protected_paths(node_cfg: Any, hub_cfg: Any = None, client_cfg: Any = None) -> Tuple[List[Path], List[Path]]:
    """(deny_all, deny_write) named by the loaded configs (§5.4 step 7): config files, token files, TLS/CA files,
    relay token files, and the hub's code paths from ``<hub data>/run/hub-code.json``."""
    paths = default_paths()
    deny_all: List[Path] = [paths["node_config"], paths["hub_config"], paths["client_config"], paths["tokens"]]
    deny_write: List[Path] = []

    def add(p: Optional[Path]) -> None:
        if p is not None and p not in deny_all:
            deny_all.append(p)

    if node_cfg is not None:
        add(getattr(node_cfg, "_path", None))
        base = Path(node_cfg.data_dir) if getattr(node_cfg, "data_dir", None) else None
        add(_p(node_cfg.listen.tls.cert, base))
        add(_p(node_cfg.listen.tls.key, base))
        add(_p(node_cfg.relay.token_file, base))
        add(_p(node_cfg.relay.ca_file, base))
        add(_p(node_cfg.network.ca_file, base))
    hub_data = paths["hub_data"]
    if hub_cfg is not None:
        add(getattr(hub_cfg, "_path", None))
        hub_data = Path(hub_cfg.data_dir) if hub_cfg.data_dir else hub_data
        add(_p(hub_cfg.listen.tls.cert, hub_data))
        add(_p(hub_cfg.listen.tls.key, hub_data))
        add(_p(hub_cfg.network.ca_file, hub_data))
        for n in hub_cfg.nodes:
            add(_p(n.ca_file, hub_data))
    if client_cfg is not None:
        add(getattr(client_cfg, "_path", None))
        for c in client_cfg.connections.values():
            add(_p(c.token_file))
            add(_p(c.ca_file))
    code = read_json(hub_data / "run" / "hub-code.json", default=None) if (hub_data / "run" / "hub-code.json").exists() \
        else None
    if isinstance(code, dict):
        for key in ("package_dir", "executable"):
            if isinstance(code.get(key), str) and code[key]:
                deny_write.append(Path(code[key]))
        for key in ("prefixes", "sys_path"):
            for item in code.get(key) or []:
                if isinstance(item, str) and item:
                    deny_write.append(Path(item))
    return deny_all, deny_write


def is_privileged() -> bool:
    """Running as root (POSIX) or as an elevated Administrator (Windows)."""
    if IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return False
    try:
        return os.geteuid() == 0
    except AttributeError:  # pragma: no cover
        return False


def _is_cgnat_literal(host: str) -> bool:
    """A literal IPv4 in 100.64.0.0/10 (tailnet range; verified against `tailscale ip` when binding)."""
    import ipaddress
    try:
        return ipaddress.ip_address(str(host)) in ipaddress.ip_network("100.64.0.0/10")
    except ValueError:
        return False


def _is_loopback_bind(host: str) -> bool:
    return host in ("127.0.0.1", "::1", "localhost") or (host not in ("tailscale",) and is_loopback_host(host))


def check_listen_raw(lst: dict) -> None:
    """The listen part of ``startup_safety_checks`` on a raw ``listen`` dict, run before an init writes it (v8.1,
    external review round 2): plaintext HTTP on a non-loopback, non-tailnet address needs TLS or
    ``allow_insecure_remote``, else the daemon would refuse to start after the change."""
    if not isinstance(lst, dict) or lst.get("enabled", True) is False:
        return
    host = str(lst.get("host") or "127.0.0.1")
    tls = lst.get("tls") if isinstance(lst.get("tls"), dict) else {}
    if bool(tls.get("cert")) != bool(tls.get("key")):
        raise bad_request("listen.tls needs both cert and key", hint="pass both --tls-cert and --tls-key")
    if _is_loopback_bind(host) or host == "tailscale" or _is_cgnat_literal(host) or tls.get("cert") \
            or lst.get("allow_insecure_remote"):
        return
    raise forbidden(f"refusing plaintext HTTP on {host}; nothing was changed",
                    hint="use --tailscale (WireGuard, no certificate), --tls-cert/--tls-key, or listen on 127.0.0.1")


def startup_safety_checks(cfg: Any, kind: str) -> List[str]:
    """Raise on fatal startup problems (§5.1 item 4, consent, root); return warnings."""
    warnings: List[str] = []
    consent = getattr(cfg, "consent", None)
    if not isinstance(consent, dict) or not consent.get("by"):
        raise forbidden(f"no owner consent recorded in the {kind} config; refusing to start",
                        hint=f"run: hive init {kind} (it asks you to confirm)")
    if is_privileged():
        if not cfg.allow_root:
            raise forbidden("refusing to run as root/Administrator",
                            hint="run Hive as a normal user, or set allow_root: true (pods do)")
        warnings.append("running as root/Administrator (allow_root): agents get full control of this machine")
    listen = cfg.listen
    enabled = getattr(listen, "enabled", True)
    tokens = cfg.auth.tokens if kind == "node" else cfg.tokens
    if enabled:
        host = listen.host
        if bool(listen.tls.cert) != bool(listen.tls.key):
            raise bad_request("listen.tls needs both cert and key")
        if not _is_loopback_bind(host):
            usable = [t for t in tokens if t.kind in ("owner", "agent", "hub")]
            if not usable and not (kind == "hub" and cfg.nodes):
                raise forbidden(f"refusing to listen on {host} with no tokens configured",
                                hint="add a token first (hive node token add), or listen on 127.0.0.1")
            # an explicit 100.64/10 address is a tailnet bind like "tailscale": the server verifies it against
            # `tailscale ip -4` at bind time and refuses to start on a mismatch (§5.9), so plaintext is allowed here
            if host != "tailscale" and not _is_cgnat_literal(host) and not listen.tls.cert:
                if not listen.allow_insecure_remote:
                    raise forbidden(f"refusing plaintext HTTP on {host}",
                                    hint="use --tailscale (WireGuard, no certificate), set listen.tls.cert/key, or "
                                         "listen.allow_insecure_remote: true")
                warnings.append(f"PLAINTEXT HTTP on {host}: tokens and data cross the network unencrypted "
                                "(allow_insecure_remote)")
    if kind == "node":
        rel = cfg.relay
        if rel.enabled:
            url = rel.hub_url or ""
            host = _host_of(url)
            if url.lower().startswith("http://") and not is_loopback_host(host):
                # §5.9: plaintext only to a private/tailnet host (private IP, *.ts.net, or a name that resolves only
                # to private/loopback addresses) with allow_insecure; public http:// hosts stay refused
                if not (rel.allow_insecure and _relay_insecure_ok(url, resolve=True)):
                    raise forbidden(f"relay hub URL {url} is plaintext to a non-local host",
                                    hint="use https://, or relay.allow_insecure: true for a private/tailnet hub "
                                         "(private IP, *.ts.net, or a name that resolves only to private addresses)")
        if cfg.policy.preset == "full":
            warnings.append("policy preset 'full': agents may read and write anywhere this user can")
    return warnings


# ---------------------------------------------------------------------------------------------------------------
# init_* (fresh configs and in-place edits)

def _consent(by: str) -> dict:
    user = _user()
    return {"by": by, "at": iso_utc(),
            "text": f"Remote AI agents will be able to run commands and change files on this computer as user "
                    f"'{user}' within this policy."}


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER") or os.environ.get("USERNAME") or "unknown"


def _write_token(path: Path, token: str) -> None:
    secure_mkdir(path.parent, required=True)
    atomic_write_bytes(path, (token + "\n").encode("utf-8"), private=True)
    secure_file(path, required=True)


def _parse_listen(spec: str, default_port: int) -> Tuple[str, int]:
    s = spec.strip()
    if s.startswith("["):
        host, _, rest = s[1:].partition("]")
        port = rest.lstrip(":")
    elif s.count(":") == 1:
        host, port = s.split(":")
    else:
        host, port = s, ""
    try:
        p = int(port) if port else default_port
    except ValueError:
        raise bad_request(f"invalid listen address {spec!r}", hint="HOST:PORT")
    if not (0 <= p <= 65535):
        raise bad_request(f"invalid port in {spec!r}")
    return host or "127.0.0.1", p


def _client_update(fn: Callable[[dict], None]) -> None:
    path = default_paths()["client_config"]
    secure_mkdir(path.parent, required=True)
    edit_config(path, fn, kind="client")


def _env_proxy() -> Optional[str]:
    for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
        v = os.environ.get(k)
        if v and v.lower().startswith("http://"):
            return v
    return None


def _apply_relay(raw: dict, *, url: str, token: Optional[str], pin: Optional[str], role: str,
                 proxy: Optional[str], pod: bool) -> None:
    if url.lower().startswith("http://"):
        if not (is_loopback_host(_host_of(url)) or _relay_insecure_ok(url)):
            raise bad_request(f"refusing plaintext relay to a public host ({url})", hint="use an https:// hub URL")
    rel = raw.setdefault("relay", {})
    rel.update({"enabled": True, "hub_url": url, "token": token, "tls_pin": pin, "role": role,
                "allow_insecure": _relay_insecure_ok(url),
                "proxy": proxy or os.environ.get("HIVE_RELAY_PROXY") or "auto"})
    rel["auto_update"] = bool(pod and role == "operator" and not rel.get("deny_actions"))


def _set_auto_update(raw: dict, on: bool) -> None:
    """relay.auto_update = ``on``; turning it on needs a relay with role operator and no deny_actions (B5g)."""
    rel = raw.setdefault("relay", {})
    if on:
        if not rel.get("enabled") or not rel.get("hub_url"):
            raise bad_request("auto-update is for relay nodes (pods) only", hint="join a hub first: hive join CODE")
        if (rel.get("role") or "operator") != "operator":
            raise bad_request(f"auto-update needs relay role operator (this node has {rel.get('role')!r})",
                              hint="hive init node --relay-role operator --auto-update")
        if rel.get("deny_actions"):
            raise bad_request("auto-update needs a relay without deny_actions",
                              hint="remove relay.deny_actions from the node config first")
    rel["auto_update"] = bool(on)


def init_node(*, name: Optional[str] = None, preset: str = "standard", topology: Optional[str] = None,
              listen: Optional[str] = None, roots: Sequence[str] = (), relay_url: Optional[str] = None,
              relay_token: Optional[str] = None, relay_role: Optional[str] = None, relay_proxy: Optional[str] = None,
              tls_pin: Optional[str] = None, join: Optional[str] = None, tls_cert: Optional[str] = None,
              tls_key: Optional[str] = None, proxy: Optional[str] = None, mirror: Optional[str] = None,
              models_dir: Optional[str] = None, wsl: bool = False, wsl_keepalive: Sequence[str] = (),
              consent_by: str, force: bool = False, auto_update: Optional[bool] = None
              ) -> Tuple[NodeConfig, Dict[str, str]]:
    """Create (or edit in place, v7/v8) a node config; returns (config, plaintext tokens {"owner","agent"}).

    ``relay_role`` None = not given (a new config gets ``operator``); on an existing config any given role, including
    ``operator``, replaces ``relay.role`` (§14.5 v8; CONTRACT-REQUEST WP6a). ``auto_update`` None = not given; True
    / False sets ``relay.auto_update`` (final acceptance B5g; True needs a relay with role operator and no
    deny_actions)."""
    paths = default_paths()
    cfg_path = paths["node_config"]
    if relay_role is not None and relay_role not in ROLES:
        raise bad_request(f"unknown relay role {relay_role!r}", hint="readonly | files | operator")
    jd = None
    if join:
        from .dist import decode_join
        jd = decode_join(join)
    if cfg_path.exists() and not force:
        return _edit_existing_node(cfg_path, jd=jd, topology=topology, listen=listen, roots=roots,
                                   relay_url=relay_url, relay_token=relay_token, relay_role=relay_role,
                                   relay_proxy=relay_proxy, tls_pin=tls_pin, tls_cert=tls_cert, tls_key=tls_key,
                                   proxy=proxy, mirror=mirror, models_dir=models_dir, wsl_keepalive=wsl_keepalive,
                                   auto_update=auto_update)
    host_name = _sanitize_name(socket.gethostname())
    raw: Dict[str, Any] = {"version": 1, "name": host_name, "node_id": "n_" + os.urandom(8).hex(),
                           "description": "", "tags": [], "consent": _consent(consent_by), "allow_root": False,
                           "workdir": "~", "data_dir": str(paths["node_data"].resolve()),
                           "listen": {"enabled": True, "host": "127.0.0.1", "port": NODE_PORT},
                           "policy": apply_preset(standard_policy(), preset, workdir=Path("~"), roots=roots)}
    if wsl:
        raw["name"] = _sanitize_name(host_name + "-wsl")
        raw["listen"]["port"] = WSL_PORT
        raw["policy"]["fs"]["roots"] = [{"path": "~", "mode": "rw"}] + [
            r for r in raw["policy"]["fs"]["roots"] if r["path"] != "~"]
        raw.setdefault("models", {})["dir"] = "~/models"
        raw["tags"] = ["wsl", f"wsl-host:{host_name}"]
    topo = topology
    if topo == "tailscale":
        raw["listen"]["host"] = "tailscale"
    elif topo in ("lan", "lan-insecure"):
        raw["listen"]["host"] = "0.0.0.0"
        if not (tls_cert and tls_key) and topo != "lan-insecure":
            raise bad_request("a LAN bind needs TLS: --tls-cert/--tls-key (README shows a one-line openssl command), "
                              "or use --tailscale (no certificate) or relay mode")
        if topo == "lan-insecure":
            raw["listen"]["allow_insecure_remote"] = True
    if tls_cert or tls_key:
        raw["listen"]["tls"] = {"cert": tls_cert, "key": tls_key}
    if listen:
        if listen in ("off", "none", "no"):
            raw["listen"]["enabled"] = False
        elif listen == "tailscale":
            raw["listen"]["host"] = "tailscale"
        else:
            h, p = _parse_listen(listen, raw["listen"]["port"])
            raw["listen"]["host"], raw["listen"]["port"] = h, p
    if name:
        n = name.lower()
        if not valid_name(n):
            raise bad_request(f"invalid node name {name!r}", hint="lowercase letters, digits, . _ - (max 64)")
        raw["name"] = n
    pod = topo == "pod"
    if pod:
        _apply_pod(raw)
        raw["listen"]["enabled"] = False
    if jd is not None:
        relay_url, relay_token, tls_pin = jd["u"], jd["t"], jd.get("p")
        if jd.get("n") and not name:
            raw["name"] = jd["n"]
    if relay_url:
        if not relay_token:
            raise bad_request("relay mode needs a relay token (--join CODE, or --token-env/--token-file)")
        _apply_relay(raw, url=relay_url, token=relay_token, pin=tls_pin, role=relay_role or "operator",
                     proxy=relay_proxy, pod=pod)
        if auto_update is not None:
            _set_auto_update(raw, auto_update)
    elif pod:
        raise bad_request("--pod needs --join CODE or --relay URL")
    net_proxy = proxy if proxy is not None else _env_proxy()
    if net_proxy:
        raw["network"] = {"proxy": net_proxy}
    if mirror == "cn":
        raw.setdefault("models", {})["hf_endpoint"] = HF_MIRROR
        cd = raw["policy"]["exec"].setdefault("child_env_defaults", {})
        cd["PIP_INDEX_URL"] = TSINGHUA_PYPI
        cd["UV_DEFAULT_INDEX"] = TSINGHUA_PYPI
    elif mirror not in (None, "none"):
        raise bad_request(f"unknown mirror {mirror!r}", hint="cn | none")
    if models_dir:
        raw.setdefault("models", {})["dir"] = models_dir
        _add_root_if_outside(raw["policy"]["fs"]["roots"], models_dir)
    if wsl_keepalive:
        raw["wsl"] = {"keepalive": [{"distro": d, "command": None} for d in wsl_keepalive]}
    owner_tok, agent_tok = new_token(), new_token()
    agent_entry: Dict[str, Any] = {"name": "this-pc", "sha256": hash_token(agent_tok), "kind": "agent",
                                   "role": "readonly" if preset == "readonly" else "operator",
                                   "note": "local MCP agents"}
    if preset == "cautious":
        from .auth import CAUTIOUS_ACTIONS
        agent_entry["require_approval"] = list(CAUTIOUS_ACTIONS)
    raw["auth"] = {"tokens": [{"name": "owner", "sha256": hash_token(owner_tok), "kind": "owner",
                               "note": "local CLI"}, agent_entry]}
    check_listen_raw(raw["listen"])
    cfg = _parse_node(copy.deepcopy(raw), cfg_path)
    secure_mkdir(cfg_path.parent, required=True)
    write_json_atomic(cfg_path, raw, private=True)
    secure_file(cfg_path, required=True)
    if pod:
        _write_pod_wrapper(paths["home"])
    tokdir = paths["tokens"]
    _write_token(tokdir / "this-pc.token", agent_tok)
    _write_token(tokdir / "this-pc-owner.token", owner_tok)
    lst = raw["listen"]
    if lst.get("enabled", True):
        scheme = "https" if (lst.get("tls") or {}).get("cert") else "http"
        url = f"{scheme}://127.0.0.1:{lst.get('port', NODE_PORT)}"

        def upd(c: dict) -> None:
            conns = c.setdefault("connections", {})
            conns["this-pc"] = {"url": url, "token_file": str(tokdir / "this-pc.token")}
            conns["this-pc-owner"] = {"url": url, "token_file": str(tokdir / "this-pc-owner.token")}
            if not c.get("default"):
                c["default"] = "this-pc"
        _client_update(upd)
    return cfg, {"owner": owner_tok, "agent": agent_tok}


def _add_root_if_outside(roots: List[dict], path: str) -> None:
    target = os.path.normpath(os.path.expanduser(path))
    for r in roots:
        if r.get("path") == "*":
            return
        base = os.path.normpath(os.path.expanduser(r.get("path", "")))
        if r.get("mode", "rw") == "rw" and (target == base or target.startswith(base.rstrip("/\\") + os.sep)):
            return
    roots.append({"path": path, "mode": "rw"})


def _write_pod_wrapper(home: Path) -> None:
    from .util import self_command
    import shlex
    body = "#!/bin/sh\nexport HIVE_HOME=" + shlex.quote(str(home)) + "\nexec " + \
        " ".join(shlex.quote(a) for a in self_command()) + ' "$@"\n'
    p = home / "hive"
    atomic_write_bytes(p, body.encode("utf-8"))
    try:
        os.chmod(p, 0o700)
    except OSError:
        pass


def _edit_existing_node(cfg_path: Path, *, jd: Optional[dict], topology: Optional[str], listen: Optional[str],
                        roots: Sequence[str], relay_url: Optional[str], relay_token: Optional[str],
                        relay_role: Optional[str], relay_proxy: Optional[str], tls_pin: Optional[str],
                        tls_cert: Optional[str], tls_key: Optional[str], proxy: Optional[str], mirror: Optional[str],
                        models_dir: Optional[str], wsl_keepalive: Sequence[str],
                        auto_update: Optional[bool] = None) -> Tuple[NodeConfig, Dict[str, str]]:
    changed: List[str] = []
    before: Dict[str, Any] = {}

    def fn(raw: dict) -> None:
        before["listen"] = copy.deepcopy(raw.get("listen") or {})
        if jd is not None or relay_url:
            url = jd["u"] if jd is not None else relay_url
            tok = jd["t"] if jd is not None else relay_token
            pin = jd.get("p") if jd is not None else tls_pin
            if not tok:
                raise bad_request("relay mode needs a relay token")
            pod = bool((raw.get("relay") or {}).get("auto_update"))
            rel = dict(raw.get("relay") or {})
            raw["relay"] = rel
            _apply_relay(raw, url=url, token=tok, pin=pin, role=relay_role or "operator", proxy=relay_proxy, pod=pod)
            if auto_update is not None:
                _set_auto_update(raw, auto_update)
            changed.append("relay")
            if jd is not None and jd.get("n"):
                raw["name"] = jd["n"]
                changed.append("name")
            return
        lst = raw.setdefault("listen", {})
        if topology == "tailscale" or listen == "tailscale":
            lst["host"] = "tailscale"
            lst["enabled"] = True
            changed.append("listen")
        elif topology in ("lan", "lan-insecure"):
            if not (tls_cert and tls_key) and topology != "lan-insecure":
                raise bad_request("a LAN bind needs TLS: --tls-cert/--tls-key, or use --tailscale")
            lst["host"] = "0.0.0.0"
            lst["enabled"] = True
            if topology == "lan-insecure":
                lst["allow_insecure_remote"] = True
            changed.append("listen")
        elif listen:
            if listen in ("off", "none", "no"):
                lst["enabled"] = False
            else:
                h, p = _parse_listen(listen, lst.get("port", NODE_PORT))
                lst.update({"host": h, "port": p, "enabled": True})
            changed.append("listen")
        if tls_cert or tls_key:
            lst["tls"] = {"cert": tls_cert, "key": tls_key}
            changed.append("listen.tls")
        if wsl_keepalive:
            ka = raw.setdefault("wsl", {}).setdefault("keepalive", [])
            for d in wsl_keepalive:
                if not any(e.get("distro") == d for e in ka):
                    ka.append({"distro": d, "command": None})
            changed.append("wsl.keepalive")
        if proxy is not None:
            raw.setdefault("network", {})["proxy"] = proxy
            changed.append("network.proxy")
        pol = raw.setdefault("policy", {})
        if mirror == "cn":
            raw.setdefault("models", {})["hf_endpoint"] = HF_MIRROR
            ex = pol.setdefault("exec", {})
            cd = ex.setdefault("child_env_defaults", dict(standard_policy()["exec"]["child_env_defaults"]))
            cd["PIP_INDEX_URL"] = TSINGHUA_PYPI
            cd["UV_DEFAULT_INDEX"] = TSINGHUA_PYPI
            changed.append("mirror")
        fs_roots = pol.setdefault("fs", {}).setdefault(
            "roots", apply_preset(standard_policy(), pol.get("preset") or "standard", workdir=Path("~"),
                                  roots=())["fs"]["roots"])
        if models_dir:
            raw.setdefault("models", {})["dir"] = models_dir
            _add_root_if_outside(fs_roots, models_dir)
            changed.append("models.dir")
        for r in roots or ():
            entry = _parse_root_spec(r)
            if not any(x.get("path") == entry["path"] for x in fs_roots):
                fs_roots.append(entry)
            changed.append("roots")
        if relay_role is not None or relay_proxy:
            rel = raw.setdefault("relay", {})
            if relay_role is not None:
                rel["role"] = relay_role
                if relay_role != "operator":
                    rel["auto_update"] = False
            if relay_proxy:
                rel["proxy"] = relay_proxy
            changed.append("relay")
        if auto_update is not None:
            _set_auto_update(raw, auto_update)
            changed.append("relay")
        if not changed:
            raise bad_request("config exists; pass the settings to change, or --force to rewrite (new tokens)",
                              path=str(cfg_path))
        if lst != before["listen"]:
            check_listen_raw(lst)

    edit_config(cfg_path, fn, kind="node")
    cfg = load_node_config(str(cfg_path))
    cfg._changed = sorted(set(changed))  # type: ignore[attr-defined]
    cfg._notes = []  # type: ignore[attr-defined]
    if "listen" in changed or "listen.tls" in changed:
        cfg._notes = _sync_local_node_listen(before.get("listen") or {}, cfg)  # type: ignore[attr-defined]
    return cfg, {}


def _listen_url(enabled: bool, port: Any, tls: bool, default_port: int) -> Optional[str]:
    if not enabled:
        return None
    return f"{'https' if tls else 'http'}://127.0.0.1:{port or default_port}"


def _sync_local_node_listen(old: dict, cfg: NodeConfig) -> List[str]:
    """After ``init node --listen/--no-listen`` on an existing config (v8.1, external review round 2): point the
    client.json connections ``this-pc``/``this-pc-owner`` at the new local URL (dropped when the node stops
    listening, so the CLI never talks to another node on the old port), and the hub-on-this-machine's direct entry
    for the old URL (``--with-local-node``) at the new one. Returns lines for the CLI."""
    notes: List[str] = []
    paths = default_paths()
    tokdir = paths["tokens"]
    lst = cfg.listen
    old_url = _listen_url(old.get("enabled", True) is not False, old.get("port", NODE_PORT),
                          bool((old.get("tls") or {}).get("cert")), NODE_PORT)
    new_url = _listen_url(bool(lst.enabled), lst.port, bool(lst.tls.cert), NODE_PORT)
    if old_url == new_url:
        return notes
    names = ("this-pc", "this-pc-owner")

    def upd(c: dict) -> None:
        conns = c.setdefault("connections", {})
        for n in names:
            if new_url is None:
                if conns.pop(n, None) is not None:
                    notes.append(f"client.json: removed connection {n} (the node no longer listens)")
            elif n in conns or (tokdir / f"{n}.token").exists():
                e = dict(conns.get(n) or {"token_file": str(tokdir / f"{n}.token")})
                e["url"] = new_url
                conns[n] = e
                notes.append(f"client.json: {n} -> {new_url}")
        if c.get("default") in names and c.get("default") not in conns:
            rest = sorted(conns)
            if rest:
                c["default"] = rest[0]
            else:
                c.pop("default", None)
    _client_update(upd)
    hub_path = paths["hub_config"]
    if old_url and hub_path.exists():
        hit: List[str] = []

        def hub_fn(raw: dict) -> None:
            for n in raw.get("nodes") or []:
                if isinstance(n, dict) and n.get("mode") == "direct" and \
                        str(n.get("url") or "").rstrip("/") == old_url:
                    if new_url is not None:
                        n["url"] = new_url
                    hit.append(str(n.get("name")))
        try:
            raw0 = read_json(hub_path, default=None) or {}
            if any(isinstance(n, dict) and n.get("mode") == "direct" and str(n.get("url") or "").rstrip("/") == old_url
                   for n in (raw0.get("nodes") or [] if isinstance(raw0, dict) else [])):
                if new_url is not None:
                    edit_config(hub_path, hub_fn, kind="hub")
                else:
                    hub_fn(copy.deepcopy(raw0))
        except HiveError as e:
            notes.append(f"warning: could not update the hub config: {e.message}")
        for name in hit:
            if new_url is not None:
                notes.append(f"hub config: local node '{name}' -> {new_url} (restart the hub to apply: "
                             "hive hub restart)")
            else:
                notes.append(f"warning: the hub on this machine reaches node '{name}' at {old_url}, which no longer "
                             f"listens; give the node a loopback listen again or remove it: hive hub remove-node "
                             f"{name}")
    return notes


def init_hub(*, name: Optional[str] = None, listen: str = "127.0.0.1:7700", public_url: Optional[str] = None,
             tls_cert: Optional[str] = None, tls_key: Optional[str] = None, tailscale: bool = False,
             funnel: bool = False, local_node: bool = False, consent_by: str,
             force: bool = False) -> Tuple[HubConfig, Dict[str, str]]:
    """Create a hub config; returns (config, {"owner": token})."""
    paths = default_paths()
    cfg_path = paths["hub_config"]
    if cfg_path.exists() and not force:
        raise conflict(f"hub config exists at {cfg_path}", hint="use --force to rewrite it (new tokens), or "
                       "hive init hub --listen/--tailscale/--public-url/--funnel/--with-local-node to change one "
                       "setting")
    host, port = _parse_listen(listen, HUB_PORT)
    raw: Dict[str, Any] = {"version": 1, "name": _sanitize_name(name or socket.gethostname()),
                           "consent": _consent(consent_by), "allow_root": False,
                           "data_dir": str(paths["hub_data"].resolve()),
                           "listen": {"host": "tailscale" if tailscale else host, "port": port}}
    if tls_cert or tls_key:
        raw["listen"]["tls"] = {"cert": tls_cert, "key": tls_key}
    if public_url:
        raw["public_url"] = public_url.rstrip("/")
        h = _host_of(public_url)
        if h:
            raw["listen"]["allowed_hosts"] = [h]
    if funnel:
        raw["listen"]["proxy_port"] = 7709
    owner_tok = new_token()
    raw["tokens"] = [{"name": "owner", "sha256": hash_token(owner_tok), "kind": "owner", "note": "local CLI"}]
    out = {"owner": owner_tok}
    local_name = None
    if local_node:
        entry = _local_node_entry(raw["name"])
        local_name = entry["name"]
        raw["nodes"] = [entry]
    check_listen_raw(raw["listen"])
    cfg = _parse_hub(copy.deepcopy(raw), cfg_path)
    cfg._local_node = local_name  # type: ignore[attr-defined]
    secure_mkdir(cfg_path.parent, required=True)
    write_json_atomic(cfg_path, raw, private=True)
    secure_file(cfg_path, required=True)
    tok_path = paths["tokens"] / "hub-owner.token"
    _write_token(tok_path, owner_tok)
    scheme = "https" if tls_cert else "http"

    def upd(c: dict) -> None:
        conns = c.setdefault("connections", {})
        entry: Dict[str, Any] = {"url": f"{scheme}://127.0.0.1:{port}", "token_file": str(tok_path)}
        conns["hub-local"] = entry
        if not c.get("default"):
            c["default"] = "hub-local"
    _client_update(upd)
    return cfg, out


def _local_node_entry(hub_name: str) -> Dict[str, Any]:
    """Register this machine's node with the hub on this machine (``--with-local-node``): a fresh hub token goes into
    the node config (its hash) and the returned hub ``nodes`` entry (plaintext). The entry is called ``hubhost`` when
    the node has the hub's name (both default to the computer name), else the node's own name."""
    paths = default_paths()
    node_path = paths["node_config"]
    if not node_path.exists():
        raise not_found("no local node config to register", hint="run: hive init node first")
    node_cfg = load_node_config(str(node_path))
    if not node_cfg.listen.enabled:
        raise bad_request("the local node does not listen, so the hub cannot reach it",
                          hint="give it a loopback listen first: hive init node --listen 127.0.0.1:7701")
    hub_tok = new_token()
    tok_name = _sanitize_name("hub-" + hub_name)

    def add_hub_token(r: dict) -> None:
        toks = r.setdefault("auth", {}).setdefault("tokens", [])
        toks[:] = [t for t in toks if t.get("name") != tok_name]
        toks.append({"name": tok_name, "sha256": hash_token(hub_tok), "kind": "hub", "role": "operator",
                     "note": "hub on this machine"})
    edit_config(node_path, add_hub_token, kind="node")
    scheme = "https" if node_cfg.listen.tls.cert else "http"
    return {"name": "hubhost" if node_cfg.name == hub_name else node_cfg.name, "mode": "direct",
            "url": f"{scheme}://127.0.0.1:{node_cfg.listen.port}", "token": hub_tok}


def edit_hub_init(*, listen: Optional[str] = None, tailscale: bool = False, public_url: Optional[str] = None,
                  funnel: bool = False, local_node: bool = False) -> HubConfig:
    """``hive init hub`` on an existing hub config (v8.1, like ``init node``): change only the given settings, keep
    every token. ``cfg._changed`` lists them; ``cfg._local_node`` names the (re-)registered local node."""
    paths = default_paths()
    cfg_path = paths["hub_config"]
    changed: List[str] = []
    local: Dict[str, Any] = {}

    def fn(raw: dict) -> None:
        lst = raw.setdefault("listen", {})
        local["old"] = copy.deepcopy(lst)
        if tailscale:
            lst["host"] = "tailscale"
            changed.append("listen")
        elif listen:
            h, p = _parse_listen(listen, lst.get("port", HUB_PORT))
            lst["host"], lst["port"] = h, p
            changed.append("listen")
        if public_url:
            raw["public_url"] = public_url.rstrip("/")
            h = _host_of(public_url)
            if h:
                hosts = [x for x in lst.get("allowed_hosts") or [] if isinstance(x, str)]
                if h not in hosts:
                    lst["allowed_hosts"] = hosts + [h]
            changed.append("public_url")
        if funnel and not lst.get("proxy_port"):
            lst["proxy_port"] = 7709
            changed.append("funnel")
        if local_node:
            entry = local["entry"]
            nodes = [n for n in raw.get("nodes") or [] if isinstance(n, dict)]
            raw["nodes"] = [n for n in nodes if n.get("name") != entry["name"]] + [entry]
            changed.append("local node")
        if not changed:
            raise bad_request("hub config exists; pass the settings to change, or --force to rewrite (new tokens)",
                              path=str(cfg_path))
        if lst != local["old"]:
            check_listen_raw(lst)

    if local_node:
        # refuse a name clash before a token is minted into the node config
        raw0 = read_json(cfg_path, default=None) or {}
        hub_name = str(raw0.get("name") or "") if isinstance(raw0, dict) else ""
        node_path = paths["node_config"]
        if not node_path.exists():
            raise not_found("no local node config to register", hint="run: hive init node first")
        ncfg = load_node_config(str(node_path))
        want = "hubhost" if ncfg.name == hub_name else ncfg.name
        url_tail = f"://127.0.0.1:{ncfg.listen.port}"
        for n in (raw0.get("nodes") or []) if isinstance(raw0, dict) else []:
            if isinstance(n, dict) and n.get("name") == want and not (n.get("mode") == "direct"
                                                                     and str(n.get("url") or "").endswith(url_tail)):
                raise conflict(f"the hub already has a different node named '{want}'",
                               hint=f"remove it first: hive hub remove-node {want}")
        local["entry"] = _local_node_entry(hub_name)
    edit_config(cfg_path, fn, kind="hub")
    cfg = load_hub_config(str(cfg_path))
    cfg._changed = sorted(set(changed))  # type: ignore[attr-defined]
    cfg._local_node = (local.get("entry") or {}).get("name")  # type: ignore[attr-defined]
    cfg._notes = []  # type: ignore[attr-defined]
    old = local.get("old") or {}
    old_url = _listen_url(True, old.get("port", HUB_PORT), bool((old.get("tls") or {}).get("cert")), HUB_PORT)
    new_url = _listen_url(True, cfg.listen.port, bool(cfg.listen.tls.cert), HUB_PORT)
    if "listen" in changed and old_url != new_url:
        # v8.1 (external review round 2): hub-local follows the hub's port
        def upd(c: dict) -> None:
            conns = c.setdefault("connections", {})
            if "hub-local" in conns or (paths["tokens"] / "hub-owner.token").exists():
                e = dict(conns.get("hub-local") or {"token_file": str(paths["tokens"] / "hub-owner.token")})
                e["url"] = new_url
                conns["hub-local"] = e
                cfg._notes.append(f"client.json: hub-local -> {new_url}")  # type: ignore[attr-defined]
        _client_update(upd)
    return cfg


def init_client(*, url: Optional[str] = None, token: Optional[str] = None, token_file: Optional[str] = None,
                tls_pin: Optional[str] = None, name: str = "default",
                make_default: Optional[bool] = None) -> ClientConfig:
    """Add (or replace) a client connection; a plaintext token is written to ``tokens/<name>.token`` (0600)."""
    if not url:
        raise bad_request("a connection needs --url")
    if not re.match(NAME_PATTERN, name):
        raise bad_request(f"invalid connection name {name!r}")
    paths = default_paths()
    if token:
        tf = paths["tokens"] / f"{name}.token"
        _write_token(tf, token)
        token_file = str(tf)

    def upd(c: dict) -> None:
        conns = c.setdefault("connections", {})
        first = not conns
        entry: Dict[str, Any] = {"url": url}
        if token_file:
            entry["token_file"] = token_file
        if tls_pin:
            entry["tls_pin"] = tls_pin
        conns[name] = entry
        if make_default or (make_default is None and (first or not c.get("default"))):
            c["default"] = name
    _client_update(upd)
    return load_client_config()


__all__ = ["TlsConfig", "ListenConfig", "RelayConfig", "TokenEntry", "AuthConfig", "PolicyConfig", "NodeConfig",
           "NetworkConfig", "ModelsConfig", "SecretsConfig", "WslConfig", "GatewayConfig", "HubNodeEntry",
           "HubConfig", "Connection", "ToolsConfig", "ClientConfig", "HubListenConfig", "HubRelayConfig",
           "default_paths", "hive_home", "load_node_config", "load_hub_config", "load_client_config", "edit_hub_init",
           "expand_placeholders", "edit_config", "protected_paths", "startup_safety_checks", "init_node", "init_hub",
           "init_client", "apply_preset", "standard_policy", "is_privileged", "PRESETS", "ROLES", "PLACEHOLDERS"]
