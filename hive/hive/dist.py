"""Distribution: zipapp bytes (build_pyz), hvj1 join codes (encode_join/decode_join), pod self_update
(design §20.2–§20.4, D49, WP5).

``encode_join``/``decode_join`` (§20.3: ``hvj1.`` + base64url(JSON {"u","t","p","n"})), ``build_pyz`` (deterministic
zipapp of the running package), ``self_update`` (pods: ``hive _update``, only when ``relay.auto_update``) and
``add_node_instructions`` (the §20.4 T/A/B/C blocks the CLI prints for ``hub add-node --relay [--via]``).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .errors import HiveError, bad_request, unsupported
from .util import valid_name

log = logging.getLogger("hive.dist")

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


# ---------------------------------------------------------------------------------------------------------------
# zipapp (§20.2)

_ZIP_DATE = (1980, 1, 1, 0, 0, 0)
_ROOT_MAIN = (b"# hive.pyz entry point (built by hive dist build / the hub's /v1/dist/hive.pyz)\n"
              b"import sys\n"
              b"from hive.__main__ import _main\n"
              b"sys.exit(_main())\n")
_SKIP_DIRS = {"__pycache__"}
_INCLUDE_EXT = (".py", ".json", ".txt", ".md", ".tmpl", ".typed")


def _package_files() -> List[Tuple[str, bytes]]:
    """(archive name, bytes) of every package file, from the source tree or from the running zipapp."""
    from . import util
    out: List[Tuple[str, bytes]] = []
    pyz = util._zipapp_path()
    if pyz is not None:
        with zipfile.ZipFile(str(pyz)) as zf:
            for name in zf.namelist():
                if not name.startswith("hive/") or name.endswith("/"):
                    continue
                parts = name.split("/")
                if any(p in _SKIP_DIRS for p in parts) or not name.endswith(_INCLUDE_EXT):
                    continue
                out.append((name, zf.read(name)))
        return sorted(out)
    pkg = util._package_dir()
    for dirpath, dirnames, filenames in os.walk(str(pkg)):
        dirnames[:] = sorted(d for d in dirnames if d not in _SKIP_DIRS and not d.startswith("."))
        for fn in sorted(filenames):
            if not fn.endswith(_INCLUDE_EXT):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, str(pkg.parent)).replace(os.sep, "/")
            with open(full, "rb") as f:
                out.append((rel, f.read()))
    return sorted(out)


def build_pyz() -> bytes:
    """Zipapp of the running package (deterministic order and timestamps; ``__main__`` -> ``hive.__main__._main``)."""
    files = _package_files()
    if not any(n == "hive/__init__.py" for n, _ in files):
        raise unsupported("pyz build not available: the hive package files were not found")
    buf = io.BytesIO()
    buf.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in [("__main__.py", _ROOT_MAIN)] + files:
            zi = zipfile.ZipInfo(name, date_time=_ZIP_DATE)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = (0o644 & 0xFFFF) << 16
            zi.create_system = 3
            zf.writestr(zi, data)
    return buf.getvalue()


def pyz_sha256(data: bytes) -> str:
    """Hex sha256 of pyz bytes (the ``X-Hive-Sha256`` header value)."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------------------------------------------
# self-update (§20.1 `hive _update`, D49)

UPDATE_429_RETRIES = 5
UPDATE_RETRY_AFTER_MAX = 60.0
_sleep = time.sleep          # test seam


def _file_sha256(path: Path) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(str(path), "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _relay_token(relay_cfg: Any) -> Optional[str]:
    tok = getattr(relay_cfg, "token", None)
    if tok:
        return str(tok)
    tf = getattr(relay_cfg, "token_file", None)
    if tf:
        try:
            with open(os.path.expanduser(str(tf)), encoding="utf-8") as f:
                return (f.readline() or "").strip() or None
        except OSError:
            return None
    return None


def _header(headers: Any, name: str) -> Optional[str]:
    try:
        v = headers.get(name)
    except Exception:  # noqa: BLE001
        v = None
    if v is None and isinstance(headers, dict):
        for k, x in headers.items():
            if str(k).lower() == name.lower():
                return str(x)
    return None if v is None else str(v)


def self_update(relay_cfg: Optional[Any] = None, *, pyz_path: Optional[Path] = None) -> bool:
    """Pod side of auto-update: fetch ``{hub_url}/v1/dist/hive.pyz`` over the relay transport and replace the running
    ``hive.pyz`` when the hub's verified sha256 differs. No request at all unless ``relay.auto_update`` is true.

    ``relay_cfg=None`` loads it with ``config.load_node_config()`` (the config file, or env-only mode from the same
    variables the node reads). Returns whether the file was replaced; failures are logged and return False.
    """
    from . import util
    if relay_cfg is None:
        try:
            from . import config as cfgmod
            node_cfg = cfgmod.load_node_config()
        except HiveError as e:
            log.info("_update: no node config (%s); nothing to do", e.message)
            return False
        relay_cfg = node_cfg.relay
        network = node_cfg.network
    else:
        network = None
    if not getattr(relay_cfg, "auto_update", False):
        return False
    if not getattr(relay_cfg, "enabled", True) or not getattr(relay_cfg, "hub_url", None):
        return False
    target = Path(pyz_path) if pyz_path is not None else util._zipapp_path()
    if target is None:
        log.warning("_update: hive is not running from hive.pyz; upgrade with pip (pip install -U hive-agent)")
        return False
    try:
        from .relay import relay_transport
        tr = relay_transport(relay_cfg, network=network, token=None, timeout=300.0)
    except HiveError as e:
        log.warning("_update: %s", e.message)
        return False
    status, headers, body = 0, {}, b""
    for attempt in range(UPDATE_429_RETRIES + 1):
        try:
            status, headers, body = tr.request("GET", "/v1/dist/hive.pyz", raw=True)
        except HiveError as e:
            log.warning("_update: cannot fetch hive.pyz from the hub: %s", e.message)
            return False
        if status != 429:
            break
        if attempt >= UPDATE_429_RETRIES:
            log.warning("_update: the hub keeps answering 429 (rate limited); keeping the current version")
            return False
        try:
            wait = float(_header(headers, "Retry-After") or 5)
        except ValueError:
            wait = 5.0
        _sleep(max(1.0, min(UPDATE_RETRY_AFTER_MAX, wait)))
    if status != 200:
        log.warning("_update: the hub answered HTTP %s for /v1/dist/hive.pyz", status)
        return False
    want = (_header(headers, "X-Hive-Sha256") or "").strip().lower()
    got = hashlib.sha256(body).hexdigest()
    if not want or want != got:
        log.warning("_update: downloaded hive.pyz does not match the hub's X-Hive-Sha256; not replaced")
        return False
    if want == _file_sha256(target):
        return False
    new = target.with_name(target.name + ".new")
    try:
        with open(str(new), "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        if _file_sha256(new) != want:
            raise OSError("written file does not match the expected sha256")
        util.replace_with_retry(new, target)
    except OSError as e:
        log.warning("_update: cannot replace %s: %s", target, e)
        try:
            new.unlink()
        except OSError:
            pass
        return False
    log.warning("_update: hive.pyz updated from the hub (sha256 %s)", want[:12])
    return True


# ---------------------------------------------------------------------------------------------------------------
# `hive hub add-node NAME --relay [--via tailnet|public]` text (§20.4, variants T/A/B/C); the CLI (WP6b) prints it

def _q(s: str) -> str:
    return "'" + str(s).replace("'", "'\"'\"'") + "'"


def _listen_pin(hub_cfg: Any) -> Optional[str]:
    cert = getattr(getattr(getattr(hub_cfg, "listen", None), "tls", None), "cert", None)
    if not cert:
        return None
    try:
        import ssl
        from .transport import cert_pin
        with open(os.path.expanduser(str(cert)), encoding="ascii", errors="replace") as f:
            return cert_pin(ssl.PEM_cert_to_DER_cert(f.read()))
    except (OSError, ValueError):
        return None


def _url_host(host: str) -> str:
    """Bracket an IPv6 literal for a URL."""
    h = str(host).strip().strip("[]")
    return f"[{h}]" if ":" in h else h


def _reachable_host(host: Optional[str]) -> Optional[str]:
    """``host`` when a remote machine could reach it: not empty, not ``tailscale``, not a wildcard, not loopback."""
    from .util import is_loopback_host
    h = str(host or "").strip().strip("[]")
    if not h or h.lower() == "tailscale" or h in ("0.0.0.0", "::", "*") or is_loopback_host(h):
        return None
    return h


def _plaintext_host_ok(host: str) -> bool:
    """§5.9 rule C1 (``transport.plaintext_ok``) for a printed http:// hub address."""
    try:
        from .transport import plaintext_ok, resolve_host
        return bool(plaintext_ok(host, resolve_host(host, 80, timeout=3.0)))
    except Exception:  # noqa: BLE001
        return False


def add_node_instructions(hub_cfg: Any, name: str, token: str, *, via: Optional[str] = None,
                          tailnet_ip: Optional[str] = None, pyz_sha256_hex: Optional[str] = None,
                          tls_pin: Optional[str] = None, host: Optional[str] = None) -> Dict[str, Any]:
    """Join codes and printed blocks for a new relay node (§20.4): ``{"variants": [...], "codes": {variant: code},
    "lines": [str]}``. ``via`` None on a ``"tailscale"`` hub means ``--via tailnet``: T (plus the public block A when
    ``public_url`` is set), then C; otherwise A for a hub with an https ``public_url``, else B (self-signed TLS) or T
    (plain LAN hub). ``host`` (additive, WP5b review) is the hub address a remote machine uses when the listen host is
    a wildcard or loopback. A join code never points at a loopback or wildcard address: without a usable host the
    call fails with ``bad_request`` (the relay token would otherwise be handed out in an unusable one-time code)."""
    if via not in (None, "tailnet", "public"):
        raise bad_request("--via must be tailnet or public")
    listen = hub_cfg.listen
    port = int(listen.port)
    public = (hub_cfg.public_url or "").rstrip("/") or None
    tailscale = str(listen.host) == "tailscale"
    if tailnet_ip is None and tailscale and via != "public":
        try:
            from .httpserver import tailscale_info
            tailnet_ip = tailscale_info()[0]
        except Exception:  # noqa: BLE001
            tailnet_ip = None
    variants: List[str] = []
    if via == "tailnet" or (via is None and tailscale):
        variants.append("T")
        if via is None and public:
            variants.append("A")
        variants.append("C")
    elif via == "public":
        if not public and not listen.tls.cert:
            raise bad_request("this hub has no public URL", hint="set public_url (hive init hub --funnel) or use "
                                                                 "--via tailnet")
        variants.append("A" if public else "B")
    elif public and public.startswith("https://"):
        variants.append("A")
    elif listen.tls.cert:
        variants.append("B")
    else:
        variants.append("T")
    plain_host: Optional[str] = None
    if "T" in variants or "C" in variants:
        plain_host = _reachable_host(tailnet_ip) or _reachable_host(host) or \
            (None if tailscale else _reachable_host(str(listen.host)))
        if plain_host is None:
            if tailscale:
                raise bad_request("cannot determine the hub's tailnet IP (tailscale ip -4 failed)",
                                  hint="is tailscale up on the hub? start it, or use --via public"
                                       + (" (public_url is set)" if public else " with public_url set"))
            raise bad_request(f"the hub listens on {listen.host}, which another machine cannot reach",
                              hint="give the hub's LAN/tailnet address, or set public_url")
        if not _plaintext_host_ok(plain_host):
            # Phase 2 review: a node refuses a plaintext relay to a public address (§5.9, relay.check_hub_url), and
            # a pod bootstrap would fetch code over it: never print an http:// join code for such a host
            raise bad_request(f"refusing a plaintext (http://) join code for {plain_host}, which is not a "
                              "loopback/LAN/tailnet address",
                              hint="serve the hub over TLS (listen.tls or public_url with Funnel/Caddy) and use "
                                   "--via public, or give the hub's LAN/tailnet address")
    tls_host: Optional[str] = None
    if "B" in variants and not public:
        tls_host = _reachable_host(host) or _reachable_host(str(listen.host))
        if tls_host is None:
            raise bad_request(f"the hub listens on {listen.host}, which another machine cannot reach",
                              hint="set public_url to the address pods use (https://HUB:7700), or give the hub "
                                   "address")
    sha_line = pyz_sha256_hex or "<sha256 printed by hive dist build>"
    codes: Dict[str, str] = {}
    lines: List[str] = []
    for v in variants:
        if v in ("T", "C"):
            url = f"http://{_url_host(str(plain_host))}:{port}"
            pin = None
        elif v == "A":
            url, pin = str(public), None
        else:
            url = public or f"https://{_url_host(str(tls_host))}:{port}"
            pin = tls_pin or _listen_pin(hub_cfg)
        code = encode_join({"u": url, "t": token, "p": pin, "n": name})
        codes[v] = code
        if v == "T":
            lines += [f"Machine on your tailnet ({name}): run there",
                      f"  HIVE_JOIN={_q(code)} hive join",
                      "  (inside WSL without a node config add --wsl; an existing node config keeps its settings and "
                      "gets only the relay block)",
                      f"  in WSL with the venv: HIVE_JOIN={_q(code)} ~/.venvs/hive/bin/hive join", ""]
        elif v == "C":
            lines += ["Pod on your tailnet (userspace tailscaled with --outbound-http-proxy-listen=localhost:1055):",
                      f"  export HIVE_JOIN={_q(code)}",
                      "  export HIVE_RELAY_PROXY=http://localhost:1055",
                      "  mkdir -p /workspace/.hive && cd /workspace/.hive",
                      f"  curl -fsSL -x http://localhost:1055 {url}/v1/dist/hive.pyz -o hive.pyz",
                      f"  echo \"{sha_line}  hive.pyz\" | sha256sum -c - || exit 1",
                      "  HIVE_HOME=/workspace/.hive python3 hive.pyz join --install", ""]
        else:
            title = ("Cloud pod (not on your tailnet):" if "T" in variants else
                     "Cloud pod / remote machine:" if v == "A" else
                     "Pod with a self-signed hub certificate (curl -k only affects public code, which sha256sum "
                     "checks; the node pins the hub with the join code):")
            k = " -k" if v == "B" else ""
            lines += [title,
                      f"  export HIVE_JOIN={_q(code)}",
                      "  mkdir -p /workspace/.hive && cd /workspace/.hive",
                      f"  curl -fsSL{k} {url}/v1/dist/hive.pyz -o hive.pyz",
                      f"  echo \"{sha_line}  hive.pyz\" | sha256sum -c - || exit 1",
                      "  HIVE_HOME=/workspace/.hive python3 hive.pyz join --install",
                      "  Pod template instead: env HIVE_JOIN=<the code above> (+ optional HIVE_PYZ_SHA256, "
                      "HIVE_SECRET_<NAME>, HIVE_PROXY, HIVE_HF_ENDPOINT); start command: deploy/runpod/start-hive.sh",
                      ""]
    lines.append("The join code contains the relay token and is shown only once; if it leaks: "
                 f"hive hub rotate-token {name}")
    return {"variants": variants, "codes": codes, "lines": lines}

__all__ = ["JOIN_PREFIX", "encode_join", "decode_join", "build_pyz", "pyz_sha256", "self_update",
           "add_node_instructions"]
