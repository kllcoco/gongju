"""Identities and tokens (design §5.2, WP1).

Phase 0: ``Identity`` (frozen dataclass), the class/role/cautious constants and ``exec_gated`` (the ONE definition,
§5.2 v6) are real; ``parse_bearer``, ``identity_from_entry``, ``relay_identity`` and ``TokenStore`` are stubs that
WP1 replaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, FrozenSet, List, Optional, Sequence, Tuple

from .errors import HiveError, unsupported
from .util import glob_match

if TYPE_CHECKING:  # pragma: no cover
    from .config import NodeConfig

CLASSES = ("read", "write", "exec", "net", "persist")
ROLE_PRESETS = {"readonly": {"read"}, "files": {"read", "write"},
                "operator": {"read", "write", "exec", "net", "persist"}}
KINDS = ("owner", "agent", "hub", "relay", "node", "models", "system")

CAUTIOUS_ACTIONS = ("shell.run", "job.start", "service.define", "service.start", "service.restart", "service.remove",
                    "fs.delete", "fs.move", "proc.kill")


@dataclass(frozen=True)
class Identity:
    """An authenticated caller (§5.2)."""
    name: str
    kind: str                          # "owner" | "agent" | "hub" | "relay" | "node" | "models" | "system"
    classes: FrozenSet[str]
    deny_actions: Tuple[str, ...] = ()
    require_approval: Tuple[str, ...] = ()
    nodes: Tuple[str, ...] = ("*",)
    models: Tuple[str, ...] = ()
    source_ip: str = ""
    via: str = "direct"                # direct | hub | relay
    agent: str = ""                    # informational only; NEVER used for authorization
    exec_gated: bool = False           # narrowing-only (D53)
    node_name: str = ""
    token_sha256: str = ""

    @property
    def approver(self) -> bool:
        """Only owner tokens decide approvals."""
        return self.kind == "owner"

    @property
    def read_only(self) -> bool:
        """classes == {"read"}."""
        return set(self.classes) == {"read"}

    def allows_node(self, node: str) -> bool:
        """Node-name glob check (hub tokens)."""
        return glob_match(node.lower(), self.nodes)

    def allows_action(self, name: str) -> bool:
        """Not matched by deny_actions."""
        return not glob_match(name, self.deny_actions)

    @property
    def display_name(self) -> str:
        """``name/agent`` when an agent name is set, else ``name``."""
        return f"{self.name}/{self.agent}" if self.agent else self.name


def exec_gated(agent: Identity, approval_set: Sequence[str]) -> bool:
    """The ONE exec_gated rule (§5.2 v6): no exec class, shell.run denied, or shell.run needs approval."""
    return ("exec" not in agent.classes
            or glob_match("shell.run", agent.deny_actions)
            or glob_match("shell.run", approval_set))


def parse_bearer(header_value: Optional[str]) -> Optional[str]:
    """Token from an ``Authorization: Bearer`` header value (WP1)."""
    raise unsupported("auth.parse_bearer not available yet (WP1)")


def identity_from_entry(entry: dict, kind_default: str) -> Identity:
    """Identity for a token entry (WP1)."""
    raise unsupported("auth.identity_from_entry not available yet (WP1)")


def relay_identity(cfg: "NodeConfig") -> Identity:
    """Identity for relay tasks from ``relay.role``/``relay.deny_actions`` (WP1)."""
    raise unsupported("auth.relay_identity not available yet (WP1)")


class TokenStore:
    """Hashed token entries and authentication (WP1)."""

    def __init__(self, entries: List[dict], *, kind_default: str):
        self.entries = list(entries)
        self.kind_default = kind_default

    def authenticate(self, presented: Optional[str], source_ip: str) -> Identity:
        """Authenticate a presented token (WP1)."""
        raise unsupported("auth.TokenStore not available yet (WP1)")


__all__ = ["CLASSES", "ROLE_PRESETS", "KINDS", "CAUTIOUS_ACTIONS", "Identity", "exec_gated", "parse_bearer",
           "identity_from_entry", "relay_identity", "TokenStore", "HiveError"]
