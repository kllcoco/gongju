"""Hugging Face / ModelScope repo listing and URL building for model.pull (design §11.5, WP3).

Phase 0: ``RepoFile`` is real; the functions raise unsupported until WP3 merges.
Imports only stdlib, hive.util, hive.errors, hive.netproxy and hive.netutil (the _download child imports it).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, List, Mapping, Optional, Tuple

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .netutil import AuthSpec, NetGuard


@dataclass
class RepoFile:
    """One file of a repo snapshot."""
    path: str
    size: int
    sha256: Optional[str]
    git_sha1: Optional[str]


def _na(what: str):
    return unsupported(f"hfhub.{what} not available yet (WP3)")


def endpoint_for(source: str, cfg: Any, env: Mapping[str, str]) -> str:
    """Endpoint from node config / env (no agent-supplied endpoint)."""
    raise _na("endpoint_for")


def normalize_repo(repo: str, source: str) -> Tuple[str, Optional[str]]:
    """(repo id, revision from a /tree/REV URL)."""
    raise _na("normalize_repo")


def list_repo(source: str, endpoint: str, repo: str, repo_type: str, revision: str, *, guard: "NetGuard",
              auth: Optional["AuthSpec"], deadline: float) -> Tuple[str, List[RepoFile]]:
    """(commit, files), cached 10 min."""
    raise _na("list_repo")


def file_url(source: str, endpoint: str, repo: str, repo_type: str, commit: str, path: str) -> str:
    """Download URL of one file at a commit."""
    raise _na("file_url")


__all__ = ["RepoFile", "endpoint_for", "normalize_repo", "list_repo", "file_url"]
