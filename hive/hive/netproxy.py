"""HTTP proxy settings, bypass matching, CONNECT tunnel, child proxy env (design §11.4, WP3; P0 no-proxy stub).

Phase 0 ships a WORKING NO-PROXY STUB (it never raises, so WP1/WP2 tests pass before WP3 lands):
``load_proxy_settings`` -> ``ProxySettings(None, None, ("localhost","127.0.0.1","::1"), "none")``, ``proxy_for`` -> None,
``bypass`` -> True, ``child_proxy_env`` -> {}, ``redact_proxy`` strips userinfo, ``open_tunnel`` -> unsupported.
Imports only stdlib, ``hive.util`` and ``hive.errors`` (the ``_download`` child imports it).
"""
from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from typing import Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit

from .errors import unsupported

ALWAYS_NO_PROXY: Tuple[str, ...] = ("localhost", "127.0.0.1", "::1")


def redact_proxy(url: str) -> str:
    """Proxy URL with userinfo replaced by ``***`` (never log credentials)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    if "@" not in parts.netloc:
        return url
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, "***@" + host, parts.path, parts.query, parts.fragment))


@dataclass(frozen=True)
class ProxySettings:
    """Effective proxy settings (§11.4)."""
    http: Optional[str]
    https: Optional[str]
    no_proxy: Tuple[str, ...]
    source: str                      # "config" | "env" | "system" | "none"
    strict_dns: bool = False
    ca_file: Optional[str] = None

    def describe(self) -> str:
        """Credentials redacted, e.g. ``http://***@127.0.0.1:7890 (env)``; ``none`` when no proxy."""
        url = self.https or self.http
        if not url:
            return "none"
        return f"{redact_proxy(url)} ({self.source})"

    def to_dict(self) -> dict:
        """For _download specs (credentials included; the spec file is 0600)."""
        return {"http": self.http, "https": self.https, "no_proxy": list(self.no_proxy), "source": self.source,
                "strict_dns": self.strict_dns, "ca_file": self.ca_file}


def load_proxy_settings(network_cfg: Optional[dict], env: Mapping[str, str] = os.environ, *,
                        explicit: Optional[str] = None) -> ProxySettings:
    """Effective settings from config/env/system (P0 stub: always no proxy)."""
    return ProxySettings(None, None, ALWAYS_NO_PROXY, "none")


def proxy_for(settings: ProxySettings, scheme: str, host: str, ips: Sequence[str] = ()) -> Optional[str]:
    """Proxy URL for a target or None (P0 stub: None)."""
    return None


def bypass(settings: ProxySettings, host: str, ips: Sequence[str] = ()) -> bool:
    """True when the host must go direct (P0 stub: True)."""
    return True


def open_tunnel(proxy_url: str, target_host: str, target_port: int, *, timeout: float) -> socket.socket:
    """CONNECT tunnel through an HTTP proxy (WP3)."""
    raise unsupported("netproxy.open_tunnel not available yet (WP3)", hint="proxies arrive with WP3")


def child_proxy_env(settings: ProxySettings, extra_no_proxy: Sequence[str] = ()) -> Dict[str, str]:
    """HTTP(S)_PROXY / NO_PROXY for children (§10.1 step 3); {} when no proxy (P0 stub: {})."""
    return {}


__all__ = ["ProxySettings", "load_proxy_settings", "proxy_for", "bypass", "open_tunnel", "child_proxy_env",
           "redact_proxy", "ALWAYS_NO_PROXY"]
