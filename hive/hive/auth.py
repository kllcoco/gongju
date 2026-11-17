"""Identities and tokens (design §5.2, WP1).

``Identity`` (frozen), role presets, ``exec_gated`` (the ONE definition, §5.2 v6), Bearer parsing, token entries ->
identities, ``relay_identity`` (D3) and ``TokenStore`` (hashes only; every stored hash is compared with
``hmac.compare_digest``; expiry; plaintext tokens never kept).
"""
from __future__ import annotations

import dataclasses
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional, Sequence, Tuple

from .errors import HiveError, unauthorized
from .util import glob_match, match_token_hash, parse_iso

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


_BEARER_RE = re.compile(r"^\s*bearer\s+(\S+)\s*$", re.IGNORECASE)
_TOKEN_CHARS_RE = re.compile(r"^hv_[A-Za-z0-9_-]{8,256}$")


def parse_bearer(header_value: Optional[str]) -> Optional[str]:
    """Token from an ``Authorization: Bearer <token>`` header value; None when absent or not a Bearer header."""
    if not header_value or not isinstance(header_value, str):
        return None
    m = _BEARER_RE.match(header_value)
    return m.group(1) if m else None


def _entry_dict(entry: Any) -> Dict[str, Any]:
    if isinstance(entry, dict):
        return entry
    to_dict = getattr(entry, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    if dataclasses.is_dataclass(entry):
        return dataclasses.asdict(entry)
    raise TypeError(f"token entry must be a dict, got {type(entry).__name__}")


def _classes_for(e: Dict[str, Any]) -> FrozenSet[str]:
    classes = e.get("classes")
    if classes:
        return frozenset(c for c in classes if c in CLASSES)
    role = e.get("role") or "operator"
    return frozenset(ROLE_PRESETS.get(role, ROLE_PRESETS["readonly"]))


def identity_from_entry(entry: dict, kind_default: str) -> Identity:
    """Identity for a token entry (§5.2): owner -> all classes; agent/hub -> role/classes; hub -> via "hub";
    node -> no classes, node_name = name; models -> {"net"}, models globs, nodes = their node parts."""
    e = _entry_dict(entry)
    kind = e.get("kind") or kind_default
    name = str(e.get("name") or "")
    common = dict(name=name, kind=kind, deny_actions=tuple(e.get("deny_actions") or ()),
                  token_sha256=str(e.get("sha256") or e.get("token_sha256") or ""))
    if kind == "owner":
        return Identity(classes=frozenset(CLASSES), **common)
    if kind == "agent":
        return Identity(classes=_classes_for(e), require_approval=tuple(e.get("require_approval") or ()),
                        nodes=tuple(e.get("nodes") or ("*",)), **common)
    if kind == "hub":
        return Identity(classes=_classes_for(e), via="hub", **common)
    if kind == "node":
        return Identity(classes=frozenset(), node_name=name, nodes=(name,), **common)
    if kind == "models":
        models = tuple(str(m) for m in (e.get("models") or ()))
        node_parts = tuple(sorted({m.split("/", 1)[0] for m in models if "/" in m})) or ("*",)
        return Identity(classes=frozenset({"net"}), models=models, nodes=node_parts, **common)
    raise ValueError(f"unknown token kind {kind!r}")


def relay_identity(cfg: "NodeConfig") -> Identity:
    """Identity for relay tasks: ``relay.role`` classes + ``relay.deny_actions`` (D3; takes the whole NodeConfig)."""
    relay = cfg.relay
    role = getattr(relay, "role", None) or "operator"
    return Identity(name="relay", kind="relay", classes=frozenset(ROLE_PRESETS.get(role, ROLE_PRESETS["readonly"])),
                    deny_actions=tuple(getattr(relay, "deny_actions", None) or ()), via="relay")


def system_identity(name: str = "system") -> Identity:
    """The node's own identity (autostart, supervisor restarts, relay register info): every class, kind "system"."""
    return Identity(name=name, kind="system", classes=frozenset(CLASSES))


class TokenStore:
    """Hashed token entries and authentication (§5.2): plaintext tokens are never stored."""

    def __init__(self, entries: List[dict], *, kind_default: str):
        self.kind_default = kind_default
        self.entries: List[Dict[str, Any]] = [dict(_entry_dict(e)) for e in (entries or ())]
        self._hashes: List[str] = [str(e.get("sha256") or e.get("token_sha256") or "") for e in self.entries]
        self._identities: List[Identity] = [identity_from_entry(e, kind_default) for e in self.entries]
        self._expires: List[Optional[float]] = []
        for e in self.entries:
            exp = e.get("expires")
            try:
                self._expires.append(parse_iso(exp) if exp else None)
            except HiveError:
                self._expires.append(0.0)  # unparsable expiry = expired (fail closed)

    def __len__(self) -> int:
        return len(self.entries)

    def names(self) -> List[str]:
        """Token names (never hashes)."""
        return [i.name for i in self._identities]

    def identities(self) -> List[Identity]:
        """The identities of all entries (no source IP)."""
        return list(self._identities)

    def authenticate(self, presented: Optional[str], source_ip: str) -> Identity:
        """Identity for a presented token; unauthorized when missing, malformed, unknown or expired."""
        if not presented:
            raise unauthorized("missing token", hint="send Authorization: Bearer <token>")
        if not presented.startswith("hv_") or not _TOKEN_CHARS_RE.match(presented):
            raise unauthorized("invalid token")
        idx = match_token_hash(presented, self._hashes)
        if idx < 0:
            raise unauthorized("invalid token")
        exp = self._expires[idx]
        if exp is not None and time.time() >= exp:
            raise unauthorized("token expired", hint="ask the owner for a new token")
        return dataclasses.replace(self._identities[idx], source_ip=source_ip or "")


__all__ = ["CLASSES", "ROLE_PRESETS", "KINDS", "CAUTIOUS_ACTIONS", "Identity", "exec_gated", "parse_bearer",
           "identity_from_entry", "relay_identity", "system_identity", "TokenStore", "HiveError"]
