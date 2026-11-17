"""Configuration: dataclasses, load/validate/defaults/presets/env-only/init_*, edit_config, protected_paths
(design §14, WP1).

Phase 0 stub: the config dataclass NAMES exist (fields per §14.1/§14.3/§14.4 are written by WP1: each dataclass has
exactly the top-level keys of its JSON block); every function raises unsupported until WP1 merges.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import unsupported


def _na(what: str):
    return unsupported(f"config.{what} not available yet (WP1)")


@dataclass
class TlsConfig:
    """listen.tls (§14.1)."""


@dataclass
class ListenConfig:
    """Node ``listen`` block (§14.1)."""


@dataclass
class RelayConfig:
    """Node ``relay`` block (§14.1)."""


@dataclass
class TokenEntry:
    """One token entry (§5.2)."""


@dataclass
class AuthConfig:
    """Node ``auth`` block."""


@dataclass
class PolicyConfig:
    """Node ``policy`` block (sub-blocks stay validated plain dicts)."""


@dataclass
class NodeConfig:
    """Node config (§14.1)."""


@dataclass
class NetworkConfig:
    """``network`` block."""


@dataclass
class ModelsConfig:
    """``models`` block."""


@dataclass
class SecretsConfig:
    """``secrets`` block."""


@dataclass
class WslConfig:
    """``wsl`` block."""


@dataclass
class GatewayConfig:
    """``gateway`` block."""


@dataclass
class HubNodeEntry:
    """Hub ``nodes[]`` entry (§14.3)."""


@dataclass
class HubConfig:
    """Hub config (§14.3)."""


@dataclass
class Connection:
    """Client connection (§14.4)."""


@dataclass
class ToolsConfig:
    """Client ``tools`` block (§14.4)."""


@dataclass
class ClientConfig:
    """Client config (§14.4)."""


@dataclass
class HubListenConfig:
    """Hub ``listen`` block (§14.3)."""


@dataclass
class HubRelayConfig:
    """Hub ``relay`` block (§14.3)."""


def default_paths() -> Dict[str, Path]:
    """Default config/data paths."""
    raise _na("default_paths")


def load_node_config(path: Optional[str] = None, *, env: Mapping[str, str] = os.environ) -> NodeConfig:
    """Load (or build from env in env-only mode) the node config."""
    raise _na("load_node_config")


def load_hub_config(path: Optional[str] = None) -> HubConfig:
    """Load the hub config."""
    raise _na("load_hub_config")


def load_client_config(path: Optional[str] = None) -> ClientConfig:
    """Load client.json."""
    raise _na("load_client_config")


def expand_placeholders(raw: dict) -> dict:
    """<DEFAULT_*> expansion (load only)."""
    raise _na("expand_placeholders")


def edit_config(path: Path, fn: Callable[[dict], None], *, kind: str) -> None:
    """Locked read-modify-validate-write of the RAW config."""
    raise _na("edit_config")


def protected_paths(node_cfg: object, hub_cfg: object = None, client_cfg: object = None
                    ) -> Tuple[List[Path], List[Path]]:
    """(deny_all, deny_write) named by the loaded configs (§5.4)."""
    raise _na("protected_paths")


def startup_safety_checks(cfg: object, kind: str) -> List[str]:
    """Raise on fatal startup problems; return warnings."""
    raise _na("startup_safety_checks")


def init_node(*, name: Optional[str] = None, preset: str = "standard", topology: Optional[str] = None,
              listen: Optional[str] = None, roots: Sequence[str] = (), relay_url: Optional[str] = None,
              relay_token: Optional[str] = None, relay_role: str = "operator", relay_proxy: Optional[str] = None,
              tls_pin: Optional[str] = None, join: Optional[str] = None, tls_cert: Optional[str] = None,
              tls_key: Optional[str] = None, proxy: Optional[str] = None, mirror: Optional[str] = None,
              models_dir: Optional[str] = None, wsl: bool = False, wsl_keepalive: Sequence[str] = (),
              consent_by: str, force: bool = False) -> Tuple[NodeConfig, Dict[str, str]]:
    """Create (or edit) a node config; returns (config, plaintext tokens)."""
    raise _na("init_node")


def init_hub(*, name: Optional[str] = None, listen: str = "127.0.0.1:7700", public_url: Optional[str] = None,
             tls_cert: Optional[str] = None, tls_key: Optional[str] = None, tailscale: bool = False,
             funnel: bool = False, local_node: bool = False, consent_by: str,
             force: bool = False) -> Tuple[HubConfig, Dict[str, str]]:
    """Create a hub config."""
    raise _na("init_hub")


def init_client(*, url: Optional[str] = None, token: Optional[str] = None, token_file: Optional[str] = None,
                tls_pin: Optional[str] = None, name: str = "default",
                make_default: Optional[bool] = None) -> ClientConfig:
    """Add a client connection."""
    raise _na("init_client")


def apply_preset(policy: dict, preset: str, *, workdir: Path, roots: Sequence[str]) -> dict:
    """Apply a policy preset (§14.2)."""
    raise _na("apply_preset")


__all__ = ["TlsConfig", "ListenConfig", "RelayConfig", "TokenEntry", "AuthConfig", "PolicyConfig", "NodeConfig",
           "NetworkConfig", "ModelsConfig", "SecretsConfig", "WslConfig", "GatewayConfig", "HubNodeEntry",
           "HubConfig", "Connection", "ToolsConfig", "ClientConfig", "HubListenConfig", "HubRelayConfig",
           "default_paths", "load_node_config", "load_hub_config", "load_client_config", "expand_placeholders",
           "edit_config", "protected_paths", "startup_safety_checks", "init_node", "init_hub", "init_client",
           "apply_preset"]
