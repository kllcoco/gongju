"""Action handler registry and ActionContext (design §4.5, Phase 0).

Action modules only register handlers with ``@handler(name)``; they never import each other (§22.3 rule 2).
Handler contract (§4.5): receives validated params with defaults (and ``ctx.given``); returns a JSON-serializable
dict; raises only HiveError; never calls audit; never holds a manager lock during I/O; honours ``ctx.remaining()`` /
``ctx.cancel`` in loops; truncates its own output before ``max_result_bytes``; does not mask secrets (the pipeline
does, §13.2 step 10a) except fs.read_chunk / job.logs / service.logs range edges (``SecretStore.mask_edges``).
"""
from __future__ import annotations

import importlib
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, FrozenSet, Optional

from .. import catalog
from ..errors import HiveError, cancelled, timeout

if TYPE_CHECKING:  # pragma: no cover
    from ..auth import Identity
    from ..catalog import ActionSpec
    from ..node import NodeRuntime
    from ..policy import EffectivePolicy

log = logging.getLogger("hive.actions")

Handler = Callable[["ActionContext", dict], dict]
HANDLERS: Dict[str, Handler] = {}
ACTION_MODULES = ["node", "system", "shell", "jobs", "services", "files", "net", "model"]


def handler(name: str) -> Callable[[Handler], Handler]:
    """Register a node-scope handler for catalog action ``name`` (asserts: known, node scope, not yet registered)."""
    spec = catalog.SPECS.get(name)
    if spec is None:
        raise AssertionError(f"@handler({name!r}): not in catalog.SPECS")
    if spec.scope != "node":
        raise AssertionError(f"@handler({name!r}): scope is {spec.scope!r}, handlers are node-only")
    if name in HANDLERS:
        raise AssertionError(f"@handler({name!r}): already registered")

    def deco(fn: Handler) -> Handler:
        if name in HANDLERS:
            raise AssertionError(f"@handler({name!r}): already registered")
        HANDLERS[name] = fn
        return fn

    return deco


def load_all() -> Dict[str, Handler]:
    """Import every action module; a failing module is logged at ERROR and skipped (its actions become unsupported)."""
    for mod in ACTION_MODULES:
        try:
            importlib.import_module(f"{__name__}.{mod}")
        except Exception:  # noqa: BLE001 - any failure only disables that module's actions
            log.error("action module %s failed to load; its actions are unsupported", mod, exc_info=True)
    return HANDLERS


def _no_progress(done: float, total: Optional[float], msg: str) -> None:
    return None


@dataclass
class ActionContext:
    """Everything a handler may use (§4.5). Field order is binding (v7/v8)."""
    rt: "NodeRuntime"                 # .name .policy .jobs .services .data_dir .workdir .audit .parts .secrets
    identity: "Identity"
    policy: "EffectivePolicy"
    spec: "ActionSpec"
    request_id: str
    deadline: float                   # time.monotonic() based
    cancel: threading.Event
    via: str                          # "direct" | "hub" | "relay"
    hold: Optional[str] = None        # v8: FileHolds id of an approved call's bound files (§13.2 step 8a)
    given: FrozenSet[str] = frozenset()   # v6: top-level param names present and non-null in the RAW params
    progress: Callable[[float, Optional[float], str], None] = _no_progress
    audit_extra: Dict[str, Any] = field(default_factory=dict)

    def remaining(self) -> float:
        """Seconds left until the deadline (>= 0)."""
        return max(0.0, self.deadline - time.monotonic())

    def check(self) -> None:
        """Raise ``cancelled`` if cancelled, ``timeout`` if past the deadline."""
        if self.cancel.is_set():
            raise cancelled("cancelled by the caller")
        if time.monotonic() >= self.deadline:
            mutating = bool(getattr(self.spec, "mutating", True))
            raise timeout(f"{getattr(self.spec, 'name', 'action')} exceeded its deadline", maybe_executed=mutating)

    def sleep(self, seconds: float) -> None:
        """Cancel-aware sleep bounded by the deadline; raises like check()."""
        self.check()
        wait = max(0.0, min(float(seconds), self.remaining()))
        if wait > 0:
            self.cancel.wait(wait)
        self.check()


__all__ = ["Handler", "HANDLERS", "ACTION_MODULES", "handler", "load_all", "ActionContext", "HiveError"]
