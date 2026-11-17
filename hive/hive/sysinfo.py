"""OS/CPU/memory/disk/network/software probes (psutil optional), capabilities, summary_line, live_status
(design §12.1, WP4).

Phase 0 stub: the probes node.info and ``GET /v1/nodes`` compose (§9.2, §8) are neutral (``capabilities`` -> ``{}``,
``summary_line`` -> ``""``, ``live_status`` -> ``{}``) so WP1 works before WP4 merges; every other function raises
unsupported until WP4 merges.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .errors import unsupported


def _na(what: str):
    return unsupported(f"sysinfo.{what} not available yet (WP4)")


def os_info() -> dict:
    """OS section."""
    raise _na("os_info")


def cpu_info() -> dict:
    """CPU section."""
    raise _na("cpu_info")


def memory() -> dict:
    """Memory section."""
    raise _na("memory")


def disks() -> List[dict]:
    """Disks section."""
    raise _na("disks")


def cpu_percent(sample_ms: int = 300) -> Optional[float]:
    """CPU percent over a short sample."""
    raise _na("cpu_percent")


def load_avg() -> Optional[List[float]]:
    """1/5/15-minute load average."""
    raise _na("load_avg")


def addresses() -> List[str]:
    """Local IP addresses."""
    raise _na("addresses")


def uptime() -> Optional[float]:
    """Seconds since boot."""
    raise _na("uptime")


def is_admin() -> bool:
    """Running as root/Administrator."""
    raise _na("is_admin")


def cloud_info() -> dict:
    """{provider, pod_id}."""
    raise _na("cloud_info")


def software(names: Iterable[str]) -> Dict[str, Optional[str]]:
    """{name: "version-line @ path" | None}."""
    raise _na("software")


def capabilities() -> dict:
    """{"psutil","nvidia_smi","docker","docker_gpu","wsl"} (P0 stub: ``{}``)."""
    return {}


def summary_line() -> str:
    """One-line GPU summary, e.g. "1× NVIDIA RTX 4090 24GB" (P0 stub: ``""``)."""
    return ""


def live_status() -> dict:
    """<= 4 KiB live status for relay/NodeSummary (P0 stub: ``{}``)."""
    return {}


__all__ = ["os_info", "cpu_info", "memory", "disks", "cpu_percent", "load_avg", "addresses", "uptime", "is_admin",
           "cloud_info", "software", "capabilities", "summary_line", "live_status"]
