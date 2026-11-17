"""Distribution: zipapp bytes (build_pyz), hvj1 join codes (encode_join/decode_join), pod self_update
(design §20.2–§20.4, D49, WP5).

Phase 0: ``encode_join``/``decode_join`` are REAL (§20.3: ``hvj1.`` + base64url(JSON {"u","t","p","n"}));
``build_pyz`` raises unsupported "pyz build not available yet" until WP5b; ``self_update`` returns False (no request).
"""
from __future__ import annotations

import base64
import json
from typing import Any, Dict, Optional

from .errors import HiveError, bad_request, unsupported
from .util import valid_name

JOIN_PREFIX = "hvj1."
_LONG_KEYS = {"hub_url": "u", "relay_token": "t", "token": "t", "tls_pin": "p", "pin": "p", "node_name": "n",
              "name": "n"}


def _bad(msg: str) -> HiveError:
    return bad_request(f"invalid join code: {msg}", hint="copy the whole hvj1.… code printed by "
                                                         "`hive hub add-node NAME --relay`")


def _check(d: Dict[str, Any]) -> Dict[str, Any]:
    u, t, p, n = d.get("u"), d.get("t"), d.get("p"), d.get("n")
    if not isinstance(u, str) or not (u.startswith("http://") or u.startswith("https://")) or len(u) > 2048:
        raise _bad("hub URL must be an http(s) URL")
    if not isinstance(t, str) or not t or len(t) > 512 or any(c.isspace() for c in t):
        raise _bad("relay token missing")
    if p is not None and (not isinstance(p, str) or not p or len(p) > 512):
        raise _bad("TLS pin must be a string or null")
    if not isinstance(n, str) or not valid_name(n):
        raise _bad("node name invalid")
    return {"u": u, "t": t, "p": p, "n": n}


def encode_join(d: Dict[str, Any]) -> str:
    """``hvj1.`` + base64url(JSON {"u": hub_url, "t": relay_token, "p": tls_pin|null, "n": node_name})."""
    norm: Dict[str, Any] = {}
    for k, v in (d or {}).items():
        norm[_LONG_KEYS.get(k, k)] = v
    norm.setdefault("p", None)
    body = _check(norm)
    raw = json.dumps(body, separators=(",", ":"), ensure_ascii=False, sort_keys=True).encode("utf-8")
    return JOIN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_join(code: str) -> Dict[str, Any]:
    """Validate the version prefix and fields; returns ``{"u","t","p","n"}``."""
    if not isinstance(code, str):
        raise _bad("not a string")
    code = code.strip()
    if not code.startswith(JOIN_PREFIX):
        raise _bad("unknown version prefix (expected hvj1.)")
    payload = code[len(JOIN_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        d = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise _bad("not base64url JSON")
    if not isinstance(d, dict):
        raise _bad("not an object")
    extra = set(d) - {"u", "t", "p", "n"}
    if extra:
        raise _bad("unknown fields " + ", ".join(sorted(extra)))
    return _check(d)


def build_pyz() -> bytes:
    """Zipapp of the running package (deterministic; WP5b)."""
    raise unsupported("pyz build not available yet")


def self_update(relay_cfg: Optional[Any] = None) -> bool:
    """Pod side of auto-update (WP5b; P0 stub: no request, returns False)."""
    return False


__all__ = ["JOIN_PREFIX", "encode_join", "decode_join", "build_pyz", "self_update"]
