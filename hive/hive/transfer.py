"""Chunked copy engine, LocalFS/NodeFS endpoints, TransferManager (runs in the tool-host process)
(design §15.3, D9, WP5).

Phase 0 stub: names exist and raise unsupported until WP5b merges.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence, Tuple

try:
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore

from .errors import unsupported


def _na(what: str):
    return unsupported(f"transfer.{what} not available yet (WP5)")


class FsEndpoint(Protocol):
    """One side of a copy."""

    def stat(self, path: str) -> dict:
        """fs.stat-like dict."""
        ...

    def read_chunk(self, path: str, offset: int, length: int) -> Tuple[bytes, bool]:
        """(data, eof)."""
        ...

    def write_chunk(self, path: str, offset: int, data: bytes, *, final: bool, sha256: Optional[str] = None,
                    total_size: Optional[int] = None, overwrite: bool = False) -> dict:
        """fs.write_chunk-like."""
        ...


class LocalFS:
    """Local files (roots=None unconfined; () refuses everything)."""

    def __init__(self, roots: Optional[Sequence[str]]):
        raise _na("LocalFS")


class NodeFS:
    """fs.read_chunk / fs.write_chunk on one node."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("NodeFS")


def copy(src: FsEndpoint, sp: str, dst: FsEndpoint, dp: str, *, chunk: int = 4 << 20, overwrite: bool = False,
         progress: Any = None, cancel: Any = None) -> dict:
    """{"bytes","sha256","duration_s","resumed_from"}."""
    raise _na("copy")


class TransferManager:
    """Background transfers (max 4 concurrent)."""

    def __init__(self, *args: Any, **kwargs: Any):
        raise _na("TransferManager")

    def start(self, src: str, dst: str, **kw: Any) -> str:
        """Start; returns tx_… id."""
        raise _na("TransferManager.start")

    def status(self, transfer_id: str, wait: float = 0) -> dict:
        """Transfer state."""
        raise _na("TransferManager.status")

    def cancel(self, transfer_id: str) -> bool:
        """Cancel a transfer."""
        raise _na("TransferManager.cancel")


__all__ = ["FsEndpoint", "LocalFS", "NodeFS", "copy", "TransferManager"]
