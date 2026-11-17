"""Error type shared by every Hive component (design §4.1, Phase 0).

Every failure that crosses a module or network boundary is a ``HiveError`` with a
stable machine-readable ``code`` (see ``CODES``), a human-readable message, an
optional ``hint`` an agent can act on and optional ``details``.
On the wire it is serialised as
``{"ok": false, "error": {"code": ..., "message": ..., "hint"?: ..., "details"?: {...}}}`` (§6.2).
Unexpected exceptions are mapped once, in ``NodeRuntime.call`` / ``Hub.call`` (§13.2 step 9).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# code -> HTTP status used when the error is returned by a server
CODES: Dict[str, int] = {
    "bad_request": 400,        # malformed request / params fail schema validation
    "unauthorized": 401,       # missing or wrong token
    "forbidden": 403,          # token valid but not allowed (identity part of the formula, §5.3)
    "policy_denied": 403,      # node policy refused the action / command / path / host
    "not_found": 404,          # unknown action, node, job, service, file
    "conflict": 409,           # version mismatch, already exists, already running
    "too_large": 413,          # body / file / output above configured limits
    "approval_required": 428,  # human approval needed; details {approval_id, expires_at, where, how}
    "rate_limited": 429,       # auth throttling, inflight/queue/job/pull caps (details.reason, §6.2)
    "cancelled": 499,          # caller cancelled (MCP notifications/cancelled, POST /v1/cancel)
    "internal": 500,           # unexpected exception
    "unsupported": 501,        # not available on this OS / missing tool / manager not loaded
    "unavailable": 503,        # node offline / unreachable (details.reason + details.maybe_executed)
    "timeout": 504,            # action or relay round-trip exceeded its deadline (details.maybe_executed)
}

# unavailable/timeout reasons that are never retryable even when maybe_executed is False (§4.1)
_NOT_RETRYABLE_REASONS = ("tls_pin", "proxy_auth", "identity_changed")


class HiveError(Exception):
    """The one error type crossing module and network boundaries (§4.1)."""

    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None,
                 hint: Optional[str] = None, status: Optional[int] = None):
        details = dict(details or {})
        if code not in CODES:
            details.setdefault("orig_code", code)
            code = "internal"
        super().__init__(f"[{code}] {message}")
        self.code: str = code
        self.message: str = message
        self.details: Dict[str, Any] = details
        self.hint: Optional[str] = hint
        self.status: Optional[int] = status

    @property
    def http_status(self) -> int:
        """HTTP status: the explicit transport-level ``status`` when set (411/415/421), else ``CODES[code]``."""
        if self.status is not None:
            return int(self.status)
        return CODES.get(self.code, 500)

    @property
    def retryable(self) -> bool:
        """Advisory retry flag for agents and callers; transports never retry on it (§4.1, D50)."""
        if self.code == "rate_limited":
            return True
        if self.code in ("unavailable", "timeout"):
            return (self.details.get("maybe_executed") is False
                    and self.details.get("reason") not in _NOT_RETRYABLE_REASONS)
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Wire form ``{"code","message"[,"hint"][,"details"]}``."""
        d: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.hint:
            d["hint"] = self.hint
        if self.details:
            d["details"] = self.details
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "HiveError":
        """Rebuild from the wire form; an unknown code becomes ``internal`` with ``details["orig_code"]``."""
        if not isinstance(d, dict):
            return cls("internal", str(d))
        details = d.get("details")
        details = dict(details) if isinstance(details, dict) else {}
        hint = d.get("hint")
        return cls(str(d.get("code", "internal")), str(d.get("message", "unknown error")), details or None,
                   hint=str(hint) if hint else None)

    def __repr__(self) -> str:
        return f"HiveError({self.code!r}, {self.message!r})"


# ---------------------------------------------------------------------------------------------------------------
# helpers (all accept hint= and **details)

def bad_request(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """400 ``bad_request``."""
    return HiveError("bad_request", msg, details, hint=hint)


def not_found(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """404 ``not_found``."""
    return HiveError("not_found", msg, details, hint=hint)


def denied(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """403 ``policy_denied`` (node policy refused; put the rule name in ``rule=``)."""
    return HiveError("policy_denied", msg, details, hint=hint)


def forbidden(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """403 ``forbidden`` (identity not allowed)."""
    return HiveError("forbidden", msg, details, hint=hint)


def unauthorized(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """401 ``unauthorized``."""
    return HiveError("unauthorized", msg, details, hint=hint)


def conflict(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """409 ``conflict``."""
    return HiveError("conflict", msg, details, hint=hint)


def too_large(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """413 ``too_large``."""
    return HiveError("too_large", msg, details, hint=hint)


def unavailable(msg: str, *, reason: Optional[str] = None, maybe_executed: bool = False,
                hint: Optional[str] = None, **details: Any) -> HiveError:
    """503 ``unavailable``; ``details.maybe_executed`` is always set explicitly (§4.1, §6.2)."""
    d: Dict[str, Any] = dict(details)
    if reason is not None:
        d["reason"] = reason
    d["maybe_executed"] = bool(maybe_executed)
    return HiveError("unavailable", msg, d, hint=hint)


def timeout(msg: str, *, maybe_executed: bool, hint: Optional[str] = None, **details: Any) -> HiveError:
    """504 ``timeout``; ``details.maybe_executed`` is always set explicitly."""
    d: Dict[str, Any] = dict(details)
    d["maybe_executed"] = bool(maybe_executed)
    return HiveError("timeout", msg, d, hint=hint)


def unsupported(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """501 ``unsupported`` (OS, missing tool, manager or module not available)."""
    return HiveError("unsupported", msg, details, hint=hint)


def cancelled(msg: str = "cancelled", hint: Optional[str] = None, **details: Any) -> HiveError:
    """499 ``cancelled``."""
    return HiveError("cancelled", msg, details, hint=hint)


def rate_limited(msg: str, retry_after: float, reason: Optional[str] = None, hint: Optional[str] = None,
                 **details: Any) -> HiveError:
    """429 ``rate_limited`` with ``details {retry_after, reason}``."""
    d: Dict[str, Any] = dict(details)
    d["retry_after"] = float(retry_after)
    d["reason"] = reason
    return HiveError("rate_limited", msg, d, hint=hint)


def approval_required(*, approval_id: str, expires_at: str, where: str, how: str) -> HiveError:
    """428 ``approval_required`` with ``details {approval_id, expires_at, where, how}`` (§5.6)."""
    return HiveError("approval_required", f"this call needs the owner's approval ({where})",
                     {"approval_id": approval_id, "expires_at": expires_at, "where": where, "how": how},
                     hint=how)


def internal(msg: str, hint: Optional[str] = None, **details: Any) -> HiveError:
    """500 ``internal``."""
    return HiveError("internal", msg, details, hint=hint)
