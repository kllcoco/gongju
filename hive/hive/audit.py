"""AuditLog (daily JSONL) and redaction helpers redact_params/redact_env/redact_text, SECRET_KEY_RE
(design §5.8, WP1; v1 redact.py merged here).

Phase 0: ``SECRET_KEY_RE`` is real; ``AuditLog`` and the redaction functions raise unsupported until WP1 merges.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Mapping, Optional

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity

SECRET_KEY_RE = r"(?i)(pass|secret|token|key|auth|cred|cookie|bearer)"


def _na(what: str):
    return unsupported(f"audit.{what} not available yet (WP1)")


class AuditLog:
    """Daily JSONL audit files with retention (WP1)."""

    def __init__(self, dir: Path, *, enabled: bool = True, retention_days: int = 30, required: bool = False,
                 mask: Optional[Callable[[str], str]] = None):
        raise _na("AuditLog")

    def record(self, *, kind: str, identity: Optional["Identity"], ip: str, via: str, node: str,
               action: Optional[str], params: Optional[dict], ok: bool, code: Optional[str],
               ms: int, request_id: str, extra: Optional[dict] = None) -> None:
        """Append one record."""
        raise _na("AuditLog.record")

    def tail(self, n: int = 50, *, since: Optional[str] = None, action: Optional[str] = None) -> List[dict]:
        """Last records."""
        raise _na("AuditLog.tail")

    def prune(self) -> None:
        """Retention pruning."""
        raise _na("AuditLog.prune")


def redact_params(action: str, params: dict) -> dict:
    """Params with secrets/bodies redacted."""
    raise _na("redact_params")


def redact_env(env: Mapping[str, str]) -> Dict[str, str]:
    """Env with values of secret-looking keys redacted."""
    raise _na("redact_env")


def redact_text(s: str) -> str:
    """Text with tokens, keys, URL userinfo and known secret prefixes masked."""
    raise _na("redact_text")


__all__ = ["SECRET_KEY_RE", "AuditLog", "redact_params", "redact_env", "redact_text"]
