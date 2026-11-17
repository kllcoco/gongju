"""Approvals at the entry server, time-boxed grants and FileHolds (verify-and-hold of bound files)
(design §5.6, D13, WP1).

Phase 0 stub: the classes exist; their methods raise unsupported until WP1 merges. WP1's approval tests use
hand-built review dicts (``presets.approval_review`` P0 stub returns the pinned shape).
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .policy import EffectivePolicy

APPROVAL_ID_PATTERN = r"^ap_[A-Za-z0-9_-]{6,40}$"
GRANT_ID_PATTERN = r"^gr_[A-Za-z0-9_-]{6,40}$"


def _na(what: str):
    return unsupported(f"approvals.{what} not available yet (WP1)")


class ApprovalStore:
    """Approval records and grants in approvals.json (WP1)."""

    def __init__(self, path: Path, *, ttl: int, max_pending: int, where: str,
                 decide_from: Sequence[str] = ("loopback",), max_grant_minutes: int = 240):
        self.path, self.where = Path(path), where

    def require(self, identity: "Identity", node: str, action: str, params: dict, review: dict,
                approval_id: Optional[str]) -> Dict[str, Optional[str]]:
        """Create/consume an approval; returns the consumed record's file fingerprints."""
        raise _na("ApprovalStore.require")

    def may_decide(self, identity: "Identity") -> bool:
        """Owner from an allowed source."""
        raise _na("ApprovalStore.may_decide")

    def list(self, include_done: bool = False) -> List[dict]:
        """Records."""
        raise _na("ApprovalStore.list")

    def decide(self, approval_id: str, approve: bool, decider: "Identity", note: str = "",
               grant_minutes: Optional[int] = None) -> dict:
        """Approve/deny (optionally with a grant)."""
        raise _na("ApprovalStore.decide")

    def granted(self, identity: "Identity", node: str, action: str) -> Optional[str]:
        """Grant id covering the call, or None."""
        raise _na("ApprovalStore.granted")

    def grants(self) -> List[dict]:
        """Valid grants."""
        raise _na("ApprovalStore.grants")

    def revoke(self, grant_id: Optional[str] = None) -> int:
        """Revoke one grant or all."""
        raise _na("ApprovalStore.revoke")


class FileHolds:
    """Verify-and-hold of an approved call's bound files (v8)."""

    def acquire(self, files: Dict[str, Optional[str]], *, policy: "EffectivePolicy") -> str:
        """Verify fingerprints and hold the paths; returns a hold id ``ho_…``."""
        raise _na("FileHolds.acquire")

    def release(self, hold_id: str) -> None:
        """Release a hold."""
        raise _na("FileHolds.release")

    def held(self, path_key: str) -> bool:
        """Path (normalized key) currently held."""
        raise _na("FileHolds.held")


__all__ = ["APPROVAL_ID_PATTERN", "GRANT_ID_PATTERN", "ApprovalStore", "FileHolds"]
