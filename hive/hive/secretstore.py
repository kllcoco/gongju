"""Node secrets: SecretStore, host binding, masking (design §5.13, WP1; NOT secrets.py, which would shadow the stdlib).

Phase 0: ``Secret`` and ``DEFAULT_SECRET_HOSTS`` are real; ``SecretStore`` is a stub behaving like an EMPTY store
(no names, no values, masking is the identity, every lookup is ``not_found``); ``set``/``unset`` are unsupported.
WP1 replaces it. Host matching is ``netutil.auth_host_allowed`` (§5.13).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, MutableMapping, Optional, Sequence, Tuple

from .errors import not_found, unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .netutil import AuthSpec

_HF = ("huggingface.co", "*.huggingface.co", "hf.co", "*.hf.co")
_GH = ("github.com", "api.github.com", "*.githubusercontent.com")
DEFAULT_SECRET_HOSTS: Dict[str, Tuple[str, ...]] = {
    "HF_TOKEN": _HF, "HUGGING_FACE_HUB_TOKEN": _HF,
    "MODELSCOPE_API_TOKEN": ("modelscope.cn", "*.modelscope.cn"),
    "OPENAI_API_KEY": ("api.openai.com",), "ANTHROPIC_API_KEY": ("api.anthropic.com",),
    "CIVITAI_API_TOKEN": ("civitai.com", "*.civitai.com"),
    "GITHUB_TOKEN": _GH, "GH_TOKEN": _GH,
}
OTHER_SECRET_HOSTS: Tuple[str, ...] = ("loopback",)   # any other name defaults to loopback only


@dataclass(frozen=True)
class Secret:
    """One node secret; ``repr`` never shows the value."""
    name: str
    value: str = field(repr=False)
    hosts: Tuple[str, ...]
    exec_ok: bool
    set_at: str
    source: str                       # "store" | "env-import" | "passthrough"

    def __repr__(self) -> str:
        return (f"Secret(name={self.name!r}, hosts={self.hosts!r}, exec_ok={self.exec_ok!r}, "
                f"set_at={self.set_at!r}, source={self.source!r})")


def _missing(name: str):
    return not_found(f"secret '{name}' is not set on this node",
                     hint=f"ask the owner to run on that machine: hive node secret set {name}", secret=name)


class SecretStore:
    """Phase 0 stub: an empty store (WP1 implements loading, env import, host binding and masking)."""

    def __init__(self, path: Path, *, passthrough: Sequence[str], env: MutableMapping[str, str] = os.environ):
        self.path = Path(path)
        self.passthrough = tuple(passthrough)

    def names(self) -> List[dict]:
        """``[{"name","hosts","exec","source","set_at"}]``; never values (stub: [])."""
        return []

    def resolve(self, name: str, *, purpose: str, host: Optional[str] = None, scheme: str = "https") -> str:
        """Secret value for ``purpose`` "auth"/"exec" (stub: always not_found)."""
        raise _missing(name)

    def resolve_auth(self, name: str) -> "AuthSpec":
        """AuthSpec(Authorization: Bearer …, the secret's hosts) (stub: always not_found)."""
        raise _missing(name)

    def mask(self, text: str) -> str:
        """Replace secret values with «secret:NAME» (stub: identity)."""
        return text

    def mask_edges(self, data: bytes, before: bytes, after: bytes) -> Tuple[bytes, bool]:
        """Mask secrets cut by range edges (stub: unchanged)."""
        return data, False

    @property
    def has_values(self) -> bool:
        """Any secret value loaded (stub: False)."""
        return False

    def set(self, name: str, value: str, *, hosts: Optional[Sequence[str]] = None, exec_ok: bool = True) -> None:
        """Store a secret (CLI only; WP1)."""
        raise unsupported("secretstore.SecretStore.set not available yet (WP1)")

    def unset(self, name: str) -> bool:
        """Remove a secret (CLI only; WP1)."""
        raise unsupported("secretstore.SecretStore.unset not available yet (WP1)")


__all__ = ["Secret", "DEFAULT_SECRET_HOSTS", "OTHER_SECRET_HOSTS", "SecretStore"]
