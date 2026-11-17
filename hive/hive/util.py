"""Small stdlib helpers shared across modules."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Tuple

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"


def new_token(nbytes: int = 32) -> str:
    """URL-safe random token suitable for Bearer auth."""
    return secrets.token_urlsafe(nbytes)


def new_id(prefix: str = "") -> str:
    """Short sortable-ish unique id: <prefix><unix-ms hex><random hex>."""
    return f"{prefix}{int(time.time() * 1000):x}{secrets.token_hex(4)}"


def token_equal(a: Optional[str], b: Optional[str]) -> bool:
    """Constant-time comparison; False if either side is empty."""
    if not a or not b:
        return False
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def now() -> float:
    return time.time()


def iso(ts: Optional[float] = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts if ts is not None else time.time()))


def json_dumps(obj: Any, pretty: bool = False) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2 if pretty else None, default=str,
                      separators=None if pretty else (",", ":"))


def read_json(path: os.PathLike, default: Any = None) -> Any:
    p = Path(path)
    if not p.exists():
        return default
    with open(p, "r", encoding="utf-8") as f:
        return json.load(f)


def write_json_atomic(path: os.PathLike, obj: Any, private: bool = False) -> None:
    """Write JSON atomically (tmp file + replace). ``private`` chmods 600 on POSIX."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json_dumps(obj, pretty=True))
            f.write("\n")
        if private and not IS_WINDOWS:
            os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=False)


def truncate_bytes(data: bytes, limit: int) -> Tuple[bytes, bool]:
    """Keep the head and tail of ``data`` within ``limit`` bytes. Returns (data, truncated)."""
    if limit <= 0 or len(data) <= limit:
        return data, False
    marker = b"\n...[truncated %d bytes]...\n" % (len(data) - limit)
    head = limit // 2
    tail = limit - head
    return data[:head] + marker + data[-tail:], True


def decode_text(data: bytes) -> str:
    """Decode process output / file bytes leniently (UTF-8, then local codepage)."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if IS_WINDOWS:
        for enc in ("mbcs", "gbk"):
            try:
                return data.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
    return data.decode("utf-8", errors="replace")


def default_data_dir(name: str) -> Path:
    base = os.environ.get("HIVE_HOME")
    if base:
        return Path(base).expanduser() / name
    return Path.home() / ".hive" / name


def platform_summary() -> dict:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
    }


def clamp(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))
