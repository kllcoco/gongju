"""Node policy: Policy, EffectivePolicy (PathGuard, CommandGuard, kill guard) and Limits (design §5.3–§5.5, WP1).

Phase 0: ``Limits`` is real (frozen dataclass; field defaults = the §14.1 standard policy values, gateway values from
the node ``gateway`` block defaults); ``Policy`` and ``EffectivePolicy`` are stubs that WP1 replaces. Other WPs test
against ``tests/helpers.FakeEffectivePolicy`` until WP1 merges (§21.1). NetGuard lives in ``netutil.py`` (WP3).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, Union

from .errors import unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .approvals import ApprovalStore, FileHolds
    from .auth import Identity
    from .catalog import ActionSpec
    from .config import GatewayConfig, ModelsConfig, NetworkConfig, PolicyConfig
    from .secretstore import SecretStore


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


def _na(what: str):
    return unsupported(f"policy.{what} not available yet (WP1)")


class Policy:
    """Immutable node policy built once per config load (WP1; Phase 0 stub)."""

    read_only: bool
    limits: Limits
    model_endpoints: List[dict]
    trusted_path: str
    node_path: str
    exec_contained: bool

    def __init__(self, cfg: "PolicyConfig", *, workdir: Path, data_dir: Path, hive_home: Path,
                 protected: Tuple[List[Path], List[Path]], network: "NetworkConfig", models: "ModelsConfig",
                 secrets: "SecretStore", gateway: Optional["GatewayConfig"] = None, is_wsl: bool = False,
                 model_endpoints: Sequence[dict] = ()):
        raise _na("Policy")

    @property
    def net_guard(self) -> Any:
        """netutil.NetGuard built from policy net + network + models (WP1/WP3)."""
        raise _na("Policy.net_guard")

    def effective(self, identity: "Identity", *, paused: bool, approvals: Optional["ApprovalStore"] = None,
                  node: str = "", services: Optional[Callable[[], dict]] = None,
                  holds: Optional["FileHolds"] = None) -> "EffectivePolicy":
        """The per-identity view of this policy (WP1)."""
        raise _na("Policy.effective")

    def summary(self) -> dict:
        """Policy summary (WP1)."""
        raise _na("Policy.summary")

    @classmethod
    def permissive(cls, tmp: Path) -> "Policy":
        """Test policy: roots ["*"], exec open, http loopback+public+private (WP1)."""
        raise _na("Policy.permissive")


class EffectivePolicy:
    """Policy for one identity: check_action, PathGuard, CommandGuard, NetGuard rules 5a–5c, kill guard (WP1 stub)."""

    identity: "Identity"
    limits: Limits
    exec_enabled: bool
    exec_mode: str
    shells: Tuple[str, ...]
    model_endpoints: List[dict]
    node_path: str

    def check_action(self, spec: "ActionSpec") -> None:
        """§5.3 authorization formula."""
        raise _na("EffectivePolicy.check_action")

    def needs_approval(self, spec: "ActionSpec") -> bool:
        """§5.3 approval rule."""
        raise _na("EffectivePolicy.needs_approval")

    def resolve_path(self, path: str, *, write: bool = False, must_exist: bool = False,
                     subtree: bool = False) -> Path:
        """PathGuard (§5.4)."""
        raise _na("EffectivePolicy.resolve_path")

    def denied_under(self, path: Path, *, write: bool = False, rebase: Optional[Path] = None) -> List[str]:
        """Denied entries in a tree (§5.4 step 8)."""
        raise _na("EffectivePolicy.denied_under")

    def roots(self) -> List[dict]:
        """``[{"path","mode"}]``."""
        raise _na("EffectivePolicy.roots")

    @property
    def workdir(self) -> Path:
        """Node workdir."""
        raise _na("EffectivePolicy.workdir")

    @property
    def write_exec_paths_denied(self) -> bool:
        """D53 rule, computed once by Policy.effective()."""
        raise _na("EffectivePolicy.write_exec_paths_denied")

    def check_shell(self, shell: str) -> None:
        """Shell allowed by exec.shells."""
        raise _na("EffectivePolicy.check_shell")

    def check_command(self, command: Union[str, List[str]], shell: str) -> None:
        """CommandGuard (§5.4)."""
        raise _na("EffectivePolicy.check_command")

    def check_env(self, env: Mapping[str, str]) -> None:
        """Allowlist-mode env check."""
        raise _na("EffectivePolicy.check_env")

    def check_cwd(self, cwd: Optional[str]) -> Path:
        """Working directory check (None -> workdir)."""
        raise _na("EffectivePolicy.check_cwd")

    def child_env(self, overrides: Dict[str, str], unset: Iterable[str] = (), gpus: Optional[str] = None,
                  secret_env: Sequence[str] = ()) -> Dict[str, str]:
        """Child environment in the §10.1 order."""
        raise _na("EffectivePolicy.child_env")

    @property
    def net_guard(self) -> Any:
        """netutil.NetGuard."""
        raise _na("EffectivePolicy.net_guard")

    def check_url(self, url: str, *, purpose: str, method: str = "GET", redirect: bool = False) -> Any:
        """NetGuard + rules 5a/5c (returns netutil.VettedTarget)."""
        raise _na("EffectivePolicy.check_url")

    def auth_spec(self, name: str) -> Any:
        """SecretStore.resolve_auth(name) (returns netutil.AuthSpec)."""
        raise _na("EffectivePolicy.auth_spec")

    @property
    def proxies(self) -> Any:
        """netproxy.ProxySettings."""
        raise _na("EffectivePolicy.proxies")

    def check_kill(self, pid: int, info: dict, *, protected: Mapping[int, str]) -> None:
        """Kill guard (§5.4)."""
        raise _na("EffectivePolicy.check_kill")

    def check_timeout(self, value: Optional[float], default: float, maximum: float) -> float:
        """None -> default; > maximum -> bad_request; never clamps."""
        raise _na("EffectivePolicy.check_timeout")

    def summary(self) -> dict:
        """node.info policy block for this identity."""
        raise _na("EffectivePolicy.summary")


__all__ = ["Limits", "Policy", "EffectivePolicy"]
