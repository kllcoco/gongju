"""Error type shared by every LingShu component.

Every failure that crosses a module or network boundary is a ``LingShuError`` with a
stable machine-readable ``code`` (see ``CODES``) and a human-readable message.
On the wire it is serialised as ``{"ok": false, "error": {"code": ..., "message": ...}}``.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

# code -> HTTP status used when the error is returned by a server
CODES: Dict[str, int] = {
    "bad_request": 400,      # malformed request / params fail schema validation
    "unauthorized": 401,     # missing or wrong token
    "forbidden": 403,        # token valid but not allowed (e.g. agent token scoped to other nodes)
    "policy_denied": 403,    # node policy refused the action / command / path / host
    "not_found": 404,        # unknown action, node, job, service, file
    "conflict": 409,         # version mismatch, already exists, already running
    "too_large": 413,        # body / file / output above configured limits
    "rate_limited": 429,     # too many failed auth attempts
    "internal": 500,         # unexpected exception
    "unavailable": 503,      # node offline / unreachable
    "timeout": 504,          # action or relay round-trip exceeded its deadline
}


class LingShuError(Exception):
    def __init__(self, code: str, message: str, details: Optional[Dict[str, Any]] = None):
        if code not in CODES:
            code = "internal"
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.details = details or {}

    @property
    def http_status(self) -> int:
        return CODES.get(self.code, 500)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            d["details"] = self.details
        return d

    @classmethod
    def from_dict(cls, d: Any) -> "LingShuError":
        if not isinstance(d, dict):
            return cls("internal", str(d))
        return cls(str(d.get("code", "internal")), str(d.get("message", "unknown error")), d.get("details") or None)


def bad_request(msg: str) -> LingShuError:
    return LingShuError("bad_request", msg)


def not_found(msg: str) -> LingShuError:
    return LingShuError("not_found", msg)


def denied(msg: str) -> LingShuError:
    return LingShuError("policy_denied", msg)
