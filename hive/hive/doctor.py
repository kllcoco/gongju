"""``hive doctor`` checks (config, files, listen, network, secrets, docker, wsl, windows, linux, proxy, policy, relay)
(design §12.4, WP4; CLI wiring WP6).

``run(kind)`` returns rows ``{"check", "status": "ok|warn|fail", "detail", "fix"}``; informational findings are
``ok`` rows whose detail starts with ``INFO:``. Every external probe is bounded (≤ 5 s; ``--deep`` docker GPU test
longer) and resolved on the trusted PATH. Doctor never prints secret values (names only).
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import logging
import os
import re
import socket
import stat
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import util
from .errors import HiveError

log = logging.getLogger("hive.doctor")

IS_WINDOWS = os.name == "nt"
IS_MAC = sys.platform == "darwin"
CHECK_TIMEOUT = 5.0
FAKEIP = ipaddress.ip_network("198.18.0.0/15")

# network targets (tests point these at stub servers)
ENDPOINTS = {"huggingface": "https://huggingface.co/api/models?limit=1",
             "hf_mirror": "https://hf-mirror.com/api/models?limit=1",
             "modelscope": "https://www.modelscope.cn/api/v1/models?PageSize=1",
             "pypi": "https://pypi.org/simple/pip/"}
TUNNEL_TARGET = ("huggingface.co", 443)
DOCKER_GPU_FIX = ("sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker (install the "
                  "NVIDIA Container Toolkit first; in China use a mirror of the libnvidia-container apt repo)")
HYPERV_FIX = ("in an admin PowerShell on Windows: New-NetFirewallHyperVRule -Name HiveWslNode -DisplayName "
              "\"Hive WSL node\" -Direction Inbound -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' "
              "-Protocol TCP -LocalPorts {port}")
OPEN_PRINCIPALS = ("everyone", "users", "authenticated users", "jeder", "benutzer", "authentifizierte benutzer",
                   "tout le monde", "utilisateurs", "utilisateurs authentifiés", "todos", "usuarios",
                   "usuarios autentificados", "s-1-1-0", "s-1-5-32-545", "s-1-5-11")


def _r(check: str, status: str, detail: str, fix: Optional[str] = None) -> dict:
    return {"check": check, "status": status, "detail": detail, "fix": fix}


# ---------------------------------------------------------------------------------------------------------------
# seams

def _resolve(host: str, timeout: float = CHECK_TIMEOUT) -> List[str]:
    """Addresses of ``host`` (getaddrinfo in a helper thread, bounded)."""
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        fut = ex.submit(socket.getaddrinfo, host, None)
        infos = fut.result(timeout=timeout)
    except Exception:  # noqa: BLE001
        return []
    finally:
        ex.shutdown(wait=False)
    out: List[str] = []
    for i in infos:
        a = str(i[4][0]).split("%", 1)[0]
        if a not in out:
            out.append(a)
    return out


def _run(argv: List[str], timeout: float = CHECK_TIMEOUT, env: Optional[Dict[str, str]] = None
         ) -> Tuple[Optional[int], str]:
    """(exit code | None when it could not run, decoded stdout+stderr)."""
    from . import proctree
    try:
        code, out, err = proctree.run_quick(argv, timeout=timeout, env=env)
    except Exception as e:  # noqa: BLE001
        return None, str(getattr(e, "message", e))
    return code, (util.decode_text(out) + "\n" + util.decode_text(err)).strip()


def _tool(name: str, fixed: Iterable[str] = ()) -> Optional[str]:
    from . import proctree
    p = proctree.which(name, proctree.trusted_path())
    if p:
        return p
    for f in fixed:
        if os.path.isfile(f):
            return f
    return None


def _is_wsl() -> bool:
    try:
        from . import wsl
        return wsl.is_wsl()
    except Exception:  # noqa: BLE001
        return False


def _hostname() -> str:
    return socket.gethostname()


# ---------------------------------------------------------------------------------------------------------------
# helpers (parsers are public for tests)

def parse_icacls(text: str) -> List[str]:
    """Principals in ``icacls <path>`` output that are open groups (Users, Authenticated Users, Everyone, incl. common
    localized names and SIDs)."""
    out: List[str] = []
    for line in (text or "").splitlines():
        m = re.search(r"(?:^|\s)([^\s:][^:]*?):\(", line)
        if not m:
            continue
        who = m.group(1).strip()
        name = who.rsplit("\\", 1)[-1].strip().lower()
        if name in OPEN_PRINCIPALS or who.lower() in OPEN_PRINCIPALS:
            if who not in out:
                out.append(who)
    return out


def parse_powercfg(text: str) -> Tuple[Optional[int], Optional[int]]:
    """``powercfg /query SCHEME_CURRENT SUB_SLEEP STANDBYIDLE`` -> (AC seconds, DC seconds). The labels are localized,
    so the last two hex values (AC, then DC) are used."""
    vals = re.findall(r":\s*0x([0-9a-fA-F]+)\s*$", text or "", re.MULTILINE)
    if len(vals) < 2:
        return None, None
    return int(vals[-2], 16), int(vals[-1], 16)


def firewall_rule_present(text: str, port: int) -> bool:
    """``netsh advfirewall firewall show rule name=all dir=in`` mentions an enabled allow rule for ``port``."""
    for block in re.split(r"\r?\n\s*\r?\n", text or ""):
        if re.search(r"(?im)^\s*\S[^:]*:\s*" + str(int(port)) + r"\s*$", block) and \
                re.search(r"(?i)\b(allow|zulassen|autoriser|permitir)\b", block):
            return True
    return False


def _mode_problem(path: Path) -> Optional[str]:
    """POSIX: group/other permissions or a foreign owner; Windows: an ACE for an open group (icacls)."""
    try:
        st = path.stat()
    except OSError:
        return None
    if IS_WINDOWS:  # pragma: no cover - Windows only
        icacls = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "icacls.exe")
        code, out = _run([icacls, str(path)])
        if code is None:
            return None
        bad = parse_icacls(out)
        return f"readable by {', '.join(bad)}" if bad else None
    problems = []
    if st.st_mode & 0o077:
        problems.append(f"mode {stat.S_IMODE(st.st_mode):04o}")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        problems.append(f"owned by uid {st.st_uid}")
    return ", ".join(problems) or None


def _fix_perm(path: Path, is_dir: bool = False) -> str:
    if IS_WINDOWS:  # pragma: no cover - Windows only
        return f'icacls "{path}" /inheritance:r /grant:r "%USERNAME%:F"'
    return f"chmod {'700' if is_dir else '600'} '{path}'"


def _url_host(url: Optional[str]) -> Optional[str]:
    if not url:
        return None
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).hostname
    except ValueError:
        return None


def _guard(network_cfg: Optional[dict]) -> Any:
    from . import netproxy, netutil
    settings = netproxy.load_proxy_settings(network_cfg or {"proxy": None})
    return netutil.NetGuard(http_allow=["loopback", "private", "public"], http_deny=[], download_allow=[],
                            allow_metadata_ip=False, proxies=settings)


def _get(url: str, guard: Any, timeout: float = CHECK_TIMEOUT) -> Tuple[bool, str]:
    """(reachable with a non-5xx status, short description)."""
    from . import netutil
    try:
        r = netutil.guarded_request("GET", url, guard=guard, purpose="http", timeout=timeout, max_bytes=2048)
    except HiveError as e:
        return False, f"{e.code}: {e.message}"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    return r.status < 500, f"HTTP {r.status}"


def _cfg_dict(obj: Any) -> dict:
    import dataclasses
    if obj is None:
        return {}
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    return dict(obj) if isinstance(obj, dict) else {}


# ---------------------------------------------------------------------------------------------------------------
# the run

class _Doctor:
    def __init__(self, kind: str, network: bool, deep: bool):
        from . import config
        self.kind = kind
        self.network = network
        self.deep = deep
        self.rows: List[dict] = []
        self.paths = config.default_paths()
        self.node_cfg: Any = None
        self.hub_cfg: Any = None
        self.client_cfg: Any = None
        self.policy: Any = None
        self.secrets: Any = None
        self.data_dir: Optional[Path] = None

    def add(self, check: str, status: str, detail: str, fix: Optional[str] = None) -> None:
        self.rows.append(_r(check, status, detail, fix))

    # -- loading
    def load(self) -> None:
        from . import config
        if self.paths["node_config"].exists() or self.kind == "node":
            try:
                self.node_cfg = config.load_node_config(str(self.paths["node_config"])
                                                        if self.paths["node_config"].exists() else None)
            except HiveError as e:
                if self.kind == "node":
                    self.add("config", "fail", e.message, e.hint or "hive init node")
        if self.paths["hub_config"].exists():
            try:
                self.hub_cfg = config.load_hub_config(str(self.paths["hub_config"]))
            except HiveError as e:
                if self.kind == "hub":
                    self.add("config", "fail", e.message, e.hint or "hive init hub")
        elif self.kind == "hub":
            self.add("config", "fail", f"no hub config at {self.paths['hub_config']}", "hive init hub")
        try:
            self.client_cfg = config.load_client_config()
        except HiveError as e:
            if self.kind == "client":
                self.add("config", "fail", e.message, e.hint)
        cfg = self.node_cfg if self.kind == "node" else self.hub_cfg
        if cfg is not None and getattr(cfg, "data_dir", None):
            self.data_dir = Path(cfg.data_dir)
        if self.node_cfg is not None and self.kind == "node":
            self._build_policy()

    def _build_policy(self) -> None:
        from . import config
        from .policy import Policy
        from .secretstore import SecretStore
        cfg = self.node_cfg
        try:
            data = Path(cfg.data_dir)
            self.secrets = SecretStore(data / "secrets.json", passthrough=cfg.secrets.env_passthrough, env=dict(os.environ))
            protected = config.protected_paths(cfg, self.hub_cfg, self.client_cfg)
            self.policy = Policy(cfg.policy, workdir=Path(cfg.workdir), data_dir=data, hive_home=self.paths["home"],
                                 protected=protected, network=cfg.network, models=cfg.models, secrets=self.secrets,
                                 gateway=cfg.gateway, is_wsl=_is_wsl(), model_endpoints=cfg.model_endpoints)
            from . import proctree
            proctree.set_trusted_path(self.policy.trusted_path)
        except Exception as e:  # noqa: BLE001
            self.add("policy", "fail", f"the node policy cannot be built: {getattr(e, 'message', e)}",
                     getattr(e, "hint", None))

    # -- groups
    def group(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - one broken group never hides the others
            log.debug("doctor group %s failed", name, exc_info=True)
            self.add(name, "warn", f"check failed: {type(e).__name__}: {getattr(e, 'message', e)}")

    def g_config(self) -> None:
        from . import config
        cfg = self.node_cfg if self.kind == "node" else self.hub_cfg
        if cfg is None:
            return
        try:
            warns = config.startup_safety_checks(cfg, self.kind)
        except HiveError as e:
            self.add("config", "fail", e.message, e.hint)
            return
        for w in warns:
            self.add("config", "warn", w)
        if not warns:
            self.add("config", "ok", f"{self.kind} config {cfg._path if getattr(cfg, '_path', None) else ''} "
                                     "loads; consent recorded".replace("  ", " "))

    def g_files(self) -> None:
        files: List[Tuple[Path, bool]] = []
        if self.kind in ("node", "client") or self.node_cfg is not None:
            if self.node_cfg is not None and getattr(self.node_cfg, "_path", None):
                files.append((Path(self.node_cfg._path), False))
            if self.node_cfg is not None:
                d = Path(self.node_cfg.data_dir)
                files += [(d / "secrets.json", False), (d / "approvals.json", False), (d, True)]
                key = self.node_cfg.listen.tls.key
                if key:
                    files.append((Path(os.path.expanduser(key)), False))
        if self.hub_cfg is not None:
            if getattr(self.hub_cfg, "_path", None):
                files.append((Path(self.hub_cfg._path), False))
            if self.hub_cfg.listen.tls.key:
                files.append((Path(os.path.expanduser(self.hub_cfg.listen.tls.key)), False))
            if self.hub_cfg.data_dir:
                files.append((Path(self.hub_cfg.data_dir), True))
        files.append((self.paths["client_config"], False))
        tok = self.paths["tokens"]
        if tok.is_dir():
            files.append((tok, True))
            files += [(p, False) for p in sorted(tok.iterdir()) if p.is_file()]
        if self.client_cfg is not None:
            for c in (getattr(self.client_cfg, "connections", None) or {}).values():
                tf = getattr(c, "token_file", None)
                if tf:
                    p = Path(os.path.expanduser(tf))
                    if not p.is_absolute():
                        p = self.paths["client_config"].parent / p
                    files.append((p, False))
        seen = set()
        bad = 0
        for p, is_dir in files:
            k = str(p)
            if k in seen or not p.exists():
                continue
            seen.add(k)
            prob = _mode_problem(p)
            if prob:
                bad += 1
                self.add("files", "warn", f"{p}: {prob}", _fix_perm(p, is_dir))
        if not bad:
            self.add("files", "ok", f"{len(seen)} config/token/secret files are private")

    def _audit_rows(self) -> List[dict]:
        if self.data_dir is None:
            return []
        from .audit import AuditLog
        try:
            return AuditLog(self.data_dir / "audit").tail(2000)
        except Exception:  # noqa: BLE001
            return []

    def _log_text(self) -> str:
        if self.data_dir is None:
            return ""
        p = self.data_dir / "logs" / f"{self.kind}.log"
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - (1 << 20)))
                return util.decode_text(f.read())
        except OSError:
            return ""

    def g_listen(self) -> None:
        cfg = self.node_cfg if self.kind == "node" else self.hub_cfg
        if cfg is None:
            return
        listen = cfg.listen
        if not getattr(listen, "enabled", True):
            self.add("listen", "ok", "listen is off (relay only)")
            return
        if "forwarded request on the main port" in self._log_text():
            self.add("listen", "warn", "forwarded requests were seen on the main port (a reverse proxy or tunnel "
                                       "points at it)", f"point the proxy at listen.proxy_port "
                                                        f"({listen.proxy_port or 'set one, e.g. 7709'})")
        proxied = [r for r in self._audit_rows() if str(r.get("ip") or "").startswith("proxy:")]
        if proxied:
            self.add("proxy", "warn", f"{len(proxied)} recent requests came through a proxy without "
                                      "X-Forwarded-For (all share one throttle bucket)",
                     "let the proxy add X-Forwarded-For (Caddy, cloudflared and Tailscale Funnel do by default)")
        elif listen.proxy_port:
            self.add("proxy", "ok", f"proxy port {listen.proxy_port}: forwarded requests carry X-Forwarded-For")
        host = str(listen.host)
        if IS_WINDOWS and not (host.startswith("127.") or host in ("localhost", "::1")):  # pragma: no cover
            self._firewall(host, int(listen.port))
        self.add("listen", "ok", f"listening on {host}:{listen.port}"
                 + (" with TLS" if listen.tls.cert else "")
                 + (f", proxy port {listen.proxy_port}" if listen.proxy_port else ""))

    def _firewall(self, host: str, port: int) -> None:  # pragma: no cover - Windows only
        netsh = _tool("netsh", [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "netsh.exe")])
        if not netsh:
            return
        code, out = _run([netsh, "advfirewall", "firewall", "show", "rule", "name=all", "dir=in"], timeout=10)
        if code is None:
            return
        if firewall_rule_present(out, port):
            self.add("listen", "ok", f"Windows firewall has an inbound rule for port {port}")
            return
        tail = "remoteip=100.64.0.0/10 profile=any" if host == "tailscale" or host.startswith("100.") \
            else "profile=private"
        name = "Hive Node (tailnet)" if "100.64" in tail else "Hive Node"
        self.add("listen", "warn", f"no Windows firewall rule for port {port}",
                 f'netsh advfirewall firewall add rule name="{name}" dir=in action=allow protocol=TCP '
                 f"localport={port} {tail}")

    def g_network(self) -> None:
        from . import netproxy
        net = _cfg_dict(self.node_cfg.network if self.node_cfg is not None else
                        (self.hub_cfg.network if self.hub_cfg is not None else None))
        settings = netproxy.load_proxy_settings(net or {"proxy": None})
        desc = settings.describe()
        env_proxies = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                                       "ALL_PROXY", "all_proxy") if os.environ.get(k)}
        socks = [k for k, v in env_proxies.items() if str(v).lower().startswith("socks")]
        if socks and not (settings.https or settings.http):
            self.add("network", "warn", f"only a SOCKS proxy is set ({', '.join(sorted(socks))}); Hive and most "
                                        "tools need an http:// proxy",
                     "use the HTTP port of Clash/v2rayN (e.g. http://127.0.0.1:7890) in network.proxy or HTTPS_PROXY")
        if settings.https or settings.http:
            proxy = settings.https or settings.http
            try:
                s = netproxy.open_tunnel(proxy, TUNNEL_TARGET[0], TUNNEL_TARGET[1], timeout=CHECK_TIMEOUT)
                s.close()
                self.add("network", "ok", f"proxy in effect: {desc}; tunnel to {TUNNEL_TARGET[0]} works")
            except HiveError as e:
                self.add("network", "fail", f"proxy in effect: {desc}; {e.message}", e.hint)
        else:
            self.add("network", "ok", "no proxy in effect (direct)")
        guard = _guard(net)
        hf_endpoint = self.node_cfg.models.hf_endpoint if self.node_cfg is not None else None
        targets = [("huggingface.co", ENDPOINTS["huggingface"])]
        if hf_endpoint:
            targets.append((_url_host(hf_endpoint) or hf_endpoint, hf_endpoint.rstrip("/") + "/api/models?limit=1"))
        targets.append(("modelscope", ENDPOINTS["modelscope"]))
        pip_index = os.environ.get("PIP_INDEX_URL")
        targets.append(("pip index", (pip_index.rstrip("/") + "/pip/") if pip_index else ENDPOINTS["pypi"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
            res = list(ex.map(lambda t: (t[0], t[1]) + _get(t[1], guard), targets))
        status = {name: ok for name, _u, ok, _d in res}
        for name, url, ok, d in res:
            self.add("network", "ok" if ok else "warn", f"{name}: {'reachable' if ok else 'unreachable'} ({d})")
        if not status.get("huggingface.co") and not hf_endpoint:
            ok, d = _get(ENDPOINTS["hf_mirror"], guard)
            if ok:
                self.add("network", "warn", "huggingface.co unreachable, hf-mirror.com OK",
                         "run hive init node --mirror cn or set models.hf_endpoint")
        if hf_endpoint and self.secrets is not None:
            mirror = _url_host(hf_endpoint) or ""
            hf = next((s for s in self.secrets.names() if s.get("name") == "HF_TOKEN"), None)
            if hf is not None and mirror and mirror not in (hf.get("hosts") or []):
                self.add("network", "warn", f"models.hf_endpoint is {mirror} but HF_TOKEN is not bound to it: gated "
                                            "repos will fail via the mirror",
                         f"bind with hive node secret set HF_TOKEN --hosts +{mirror} if you trust it")
        self._fakeip()

    def _ts_names(self) -> List[str]:
        urls: List[str] = []
        if self.node_cfg is not None:
            urls.append(self.node_cfg.relay.hub_url or "")
        if self.hub_cfg is not None:
            urls.append(self.hub_cfg.public_url or "")
            urls += [n.url or "" for n in self.hub_cfg.nodes]
        if self.client_cfg is not None:
            urls += [getattr(c, "url", "") or "" for c in (self.client_cfg.connections or {}).values()]
        names = []
        for u in urls:
            h = _url_host(u)
            if h and h.lower().endswith(".ts.net") and h not in names:
                names.append(h)
        return names

    def _fakeip(self) -> None:
        for name in self._ts_names():
            ips = _resolve(name)
            fake = [ip for ip in ips if _in(ip, FAKEIP)]
            if fake:
                self.add("network", "warn", f"{name} resolves to {', '.join(fake)} (Clash/Mihomo fake-ip)",
                         "Clash/Mihomo fake-ip answers *.ts.net: add `+.ts.net` to fake-ip-filter (or use the tailnet "
                         "IP)")

    def g_secrets(self) -> None:
        if self.secrets is None:
            return
        names = self.secrets.names()
        if not names:
            self.add("secrets", "ok", "no node secrets")
            return
        parts = [f"{s['name']} ({s.get('source')}; hosts: {', '.join(s.get('hosts') or []) or '-'}"
                 f"{'; exec' if s.get('exec') else ''})" for s in names]
        self.add("secrets", "ok", "secrets: " + "; ".join(parts))

    def g_docker(self) -> None:
        from . import sysinfo
        exe = _tool("docker")
        if not exe:
            return
        caps = sysinfo.capabilities(refresh=True)
        if not caps.get("docker"):
            self.add("docker", "warn", "the docker CLI is installed but the daemon is not reachable",
                     "start Docker (or add your user to the docker group)")
            return
        self.add("docker", "ok", "docker daemon reachable")
        if sys.platform.startswith("linux") and caps.get("nvidia_smi") and caps.get("docker_gpu") is False:
            self.add("docker", "warn", "this GPU machine's Docker has no NVIDIA runtime", DOCKER_GPU_FIX)
        user = _username()
        self.add("docker", "ok", f"INFO: user {user} controls Docker (root-equivalent): an agent with exec can become "
                                 "root via Docker; the docker preset blocks the obvious paths, shell_run does not")
        if self.deep and caps.get("docker_gpu"):
            code, out = _run([exe, "images", "--format", "{{.Repository}}:{{.Tag}}"], timeout=10)
            imgs = [ln for ln in out.splitlines() if "cuda" in ln.lower()] if code == 0 else []
            image = imgs[0] if imgs else "nvidia/cuda:12.4.1-base-ubuntu22.04"
            code, out = _run([exe, "run", "--rm", "--gpus", "all", image, "nvidia-smi"], timeout=300)
            if code == 0:
                self.add("docker", "ok", f"docker run --gpus all {image} nvidia-smi works")
            else:
                self.add("docker", "fail", f"docker run --gpus all {image} nvidia-smi failed: {out[-300:]}",
                         DOCKER_GPU_FIX)

    def g_wsl(self) -> None:
        from . import wsl
        if IS_WINDOWS:  # pragma: no cover - Windows only
            distros = wsl.list_distros()
            if distros:
                self.add("wsl", "ok", "WSL distros: " + ", ".join(f"{d['name']} ({d['state']})" for d in distros))
            entries = list(self.node_cfg.wsl.keepalive or []) if self.node_cfg is not None else []
            names = {d["name"] for d in distros}
            for e in entries:
                if e.get("distro") not in names:
                    self.add("wsl", "warn", f"wsl.keepalive names {e.get('distro')}, which is not installed",
                             "fix the distro name in the config (wsl -l -v)")
            return
        if not wsl.is_wsl():
            return
        info = wsl.wsl_info() or {}
        self.add("wsl", "ok" if info.get("systemd") else "warn",
                 f"WSL distro {info.get('distro')}: systemd {'on' if info.get('systemd') else 'off'}",
                 None if info.get("systemd") else "add [boot] systemd=true to /etc/wsl.conf, then wsl --shutdown "
                                                  "(else the watchdog method and the Windows keepalive start the node)")
        user = _username()
        if info.get("systemd") and not os.path.exists(f"/var/lib/systemd/linger/{user}"):
            self.add("wsl", "warn", "linger is off: the user unit stops with your last session",
                     f"sudo loginctl enable-linger {user}")
        if wsl.interop_enabled():
            self.add("wsl", "warn", "Windows interop is on: programs in WSL can start Windows programs as you, outside "
                                    "the Windows node's policy (T25)",
                     "give both nodes the same preset, or set [interop] enabled=false, appendWindowsPath=false and "
                     "[automount] enabled=false in /etc/wsl.conf")
        if self.node_cfg is not None:
            mdir = os.path.expanduser(self.node_cfg.models.dir or "~/models")
            if mdir.startswith("/mnt/"):
                self.add("wsl", "warn", f"models.dir {mdir} is on a Windows drive (9P is very slow)",
                         "keep weights on the WSL disk, e.g. models.dir ~/models")
        mode = info.get("networking")
        self.add("wsl", "ok", f"networking mode: {mode or 'unknown'}")
        if self.node_cfg is not None:
            listen = self.node_cfg.listen
            if mode == "mirrored" and listen.enabled and str(listen.host) == "tailscale":
                ip = None
                try:
                    from . import httpserver
                    ip = (httpserver.tailscale_info() or {}).get("ip")
                except Exception:  # noqa: BLE001
                    ip = None
                self.add("wsl", "ok", f"mirrored networking, listen tailscale -> {ip or 'tailnet IP not found'}; "
                                      "inbound to WSL also needs the Hyper-V firewall to allow the port",
                         HYPERV_FIX.replace("{port}", str(listen.port)))
            proxy = self.node_cfg.network.proxy or ""
            ph = _url_host(proxy)
            if mode == "nat" and ph and _is_private_ip(ph):
                self.add("wsl", "warn", f"network.proxy uses the fixed address {ph} in NAT mode",
                         f"use http://wsl-host:{_port(proxy) or 7890} (the Windows host address changes after reboots)")
            if ph == "wsl-host":
                from . import netproxy
                try:
                    settings = netproxy.load_proxy_settings(_cfg_dict(self.node_cfg.network))
                    url = settings.https or settings.http
                    h, p = _url_host(url) or "", _port(url or "") or 7890
                    from . import netutil
                    ok, _ms, _err = netutil.tcp_check(h, p, CHECK_TIMEOUT)
                except Exception:  # noqa: BLE001
                    ok = False
                if not ok:
                    self.add("wsl", "warn", "the wsl-host proxy is not reachable",
                             "enable 'allow LAN' in Clash/v2rayN and allow the port in the Windows firewall")
        self._wsl_power()

    def _wsl_power(self) -> None:
        from . import wsl
        tags = list(self.node_cfg.tags or []) if self.node_cfg is not None else []
        if not any(str(t).startswith("wsl-host:") for t in tags):
            return
        if not wsl.interop_enabled():
            self.add("wsl", "ok", "INFO: Windows power plan not checked (interop is off); make sure Windows does not "
                                  "sleep while plugged in")
            return
        exe = _tool("powercfg.exe", ["/mnt/c/Windows/System32/powercfg.exe"])
        if not exe:
            self.add("wsl", "ok", "INFO: powercfg.exe not found; Windows power plan not checked")
            return
        code, out = _run([exe, "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
        ac, _dc = parse_powercfg(out) if code == 0 else (None, None)
        if ac:
            self.add("wsl", "warn", f"Windows sleeps after {round(ac / 60)} min and suspends this WSL node with its "
                                    "jobs and services; keep_awake does not work on Linux",
                     "set the power plan to never sleep while plugged in (powercfg /change standby-timeout-ac 0)")
        elif ac == 0:
            self.add("wsl", "ok", "Windows never sleeps on AC power")

    def g_windows(self) -> None:  # pragma: no cover - Windows only
        if not IS_WINDOWS:
            return
        from . import install, winapi
        info = winapi.platform_info() or {}
        mode = info.get("spawn_mode")
        expl = {"free": "jobs/services are started outside any job object",
                "silent": "the task's job allows silent breakaway; children leave it automatically",
                "breakaway": "children break away from the task's job",
                "escaped": "the node escaped the task's job once (WMI relaunch); children are free",
                "job": "children would die with the task: run hive service install node again"}
        self.add("windows", "ok" if mode != "job" else "warn",
                 f"spawn mode {mode} (session {info.get('session_id')}, in job {info.get('in_job')}): "
                 f"{expl.get(str(mode), '')}")
        st = install.status("node")
        if st.get("method") == "schtasks":
            code, out = _run(install._schtasks("/Query", "/TN", install.TASK_NAMES["node"], "/XML"), timeout=10)
            if code == 0 and "--supervise" not in out:
                self.add("policy", "warn", "the Scheduled Task Action lacks --supervise (an install from v5)",
                         "re-run hive service install node")
            if code == 0 and "<Priority>5</Priority>" not in out:
                self.add("windows", "warn", "the Scheduled Task does not run at priority 5",
                         "re-run hive service install node")
        pc = _tool("powercfg", [os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                                             "powercfg.exe")])
        if pc and st.get("installed"):
            code, out = _run([pc, "/query", "SCHEME_CURRENT", "SUB_SLEEP", "STANDBYIDLE"])
            ac, _dc = parse_powercfg(out) if code == 0 else (None, None)
            if ac:
                self.add("windows", "warn", f"Windows sleeps after {round(ac / 60)} min on AC power; jobs without "
                                            "keep_awake stop while it sleeps",
                         "powercfg /change standby-timeout-ac 0, or start long jobs with keep_awake")

    def g_linux(self) -> None:
        if not sys.platform.startswith("linux"):
            return
        from . import install, proctree
        tp = proctree.trusted_path()
        if not proctree.which("python", tp) and proctree.which("python3", tp):
            self.add("linux", "ok", "INFO: python is absent but python3 is present; presets use python3")
        st = install.status(self.kind if self.kind in ("node", "hub") else "node")
        if st.get("method") in ("systemd", "watchdog") and st.get("files"):
            captured = _captured_path(Path(st["files"][0]))
            if captured is not None:
                missing = []
                lb = os.path.expanduser("~/.local/bin")
                if os.path.isdir(lb) and lb not in captured:
                    missing.append(lb)
                venv = os.environ.get("VIRTUAL_ENV")
                if venv and os.path.join(venv, "bin") not in captured:
                    missing.append(os.path.join(venv, "bin"))
                if missing:
                    self.add("linux", "warn", f"the installed service's PATH lacks {', '.join(missing)}",
                             f"re-run hive service install {self.kind} from a shell where these tools work, or add "
                             "the folders to exec.path_prepend")

    def g_policy(self) -> None:
        if self.policy is not None:
            for w in self.policy.warnings:
                self.add("policy", "warn", w)
        node_preset = getattr(getattr(self.node_cfg, "policy", None), "preset", None)
        if self.node_cfg is not None:
            for t in self.node_cfg.auth.tokens:
                if t.kind != "agent" or not _has_exec(t):
                    continue
                if node_preset == "cautious" and not t.require_approval:
                    self.add("policy", "warn", f"agent token '{t.name}' runs commands on this cautious node without "
                                               "approvals", f"hive node token add {t.name} --cautious (re-create it)")
                elif self.hub_cfg is not None and not t.require_approval:
                    self.add("policy", "warn", f"agent token '{t.name}' can run commands on the hub's machine without "
                                               "approvals: an exec-capable agent on the hub machine can act as the "
                                               "hub (T5)", "use --cautious tokens for agents on this machine")
        if self.hub_cfg is not None:
            local = self._hub_local_nodes()
            for t in self.hub_cfg.tokens:
                if t.kind != "agent" or not _has_exec(t) or t.require_approval:
                    continue
                hits = [n for n in local if any(_glob(n, g) for g in (t.nodes or ["*"]))]
                if hits:
                    self.add("policy", "warn", f"hub agent token '{t.name}' reaches {', '.join(hits)} on the hub's "
                                               "machine without approvals: an exec-capable agent on the hub machine "
                                               "can act as the hub (T5)",
                             f"hive hub add-agent {t.name} --cautious (re-create it), or narrow its nodes")
        if _is_wsl() and os.path.isdir("/mnt/c"):
            hubs = _windows_hub_configs()
            if hubs:
                self.add("policy", "warn", f"agents on this WSL node can read the Windows hub's tokens ({hubs[0]}) "
                                           "(T25)", "use --cautious hub tokens or set [automount] enabled=false")

    def _hub_local_nodes(self) -> List[str]:
        out = []
        host = _hostname().lower()
        own_ts: List[str] = []
        try:
            from . import sysinfo
            ts = sysinfo.tailscale_ip()
            own_ts = [ts] if ts else []
        except Exception:  # noqa: BLE001
            own_ts = []
        for n in self.hub_cfg.nodes:
            h = (_url_host(n.url) or "").lower()
            local = False
            if n.mode == "direct" and h:
                local = util.is_loopback_host(h) or h in own_ts or h == host or h.startswith(host + ".")
            if any(str(tag).lower() == f"wsl-host:{host}" for tag in (n.tags or [])):
                local = True
            if local:
                out.append(n.name)
        return out

    def g_relay(self) -> None:
        cfg = self.node_cfg
        if cfg is None or not cfg.relay.enabled:
            return
        r = cfg.relay
        self.add("relay", "ok", f"relay to {r.hub_url}: role {r.role}, auto_update {'on' if r.auto_update else 'off'}"
                 + (f", denied actions {', '.join(r.deny_actions)}" if r.deny_actions else ""))
        if r.auto_update and (r.role != "operator" or r.deny_actions):
            self.add("relay", "warn", "auto-update lets the hub replace this node's code, which voids the narrowed "
                                      "relay role", "set relay.auto_update false")
        if not cfg.listen.enabled and any(t.require_approval for t in cfg.auth.tokens):
            self.add("relay", "warn", "this node has no listener but its tokens carry require_approval rules",
                     "approval rules belong on the hub agent token (hive hub add-agent --cautious), or set this "
                     "node's preset to cautious (the hub then gates hub-routed agents)")

    def g_client(self) -> None:
        if self.client_cfg is None:
            return
        conns = self.client_cfg.connections or {}
        self.add("client", "ok", f"{len(conns)} connection(s): {', '.join(sorted(conns)) or '-'}"
                 + (f"; default {self.client_cfg.default}" if self.client_cfg.default else ""))


def _in(ip: str, net: Any) -> bool:
    try:
        return ipaddress.ip_address(ip) in net
    except ValueError:
        return False


def _is_private_ip(host: str) -> bool:
    try:
        a = ipaddress.ip_address(host)
    except ValueError:
        return False
    return a.is_private and not a.is_loopback


def _port(url: str) -> Optional[int]:
    from urllib.parse import urlsplit
    try:
        return urlsplit(url).port
    except ValueError:
        return None


def _has_exec(t: Any) -> bool:
    from .auth import _classes_for
    return "exec" in _classes_for({"classes": t.classes, "role": t.role})


def _glob(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatchcase(name.lower(), str(pattern).lower())


def _username() -> str:
    try:
        import getpass
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return os.environ.get("USER") or os.environ.get("USERNAME") or "?"


_MNT = "/mnt"


def _windows_hub_configs() -> List[str]:
    """Windows hub configs visible through drvfs (``/mnt/*/Users/*/.hive/hub/config.json``)."""
    import glob
    return sorted(glob.glob(os.path.join(_MNT, "*", "Users", "*", ".hive", "hub", "config.json")))[:3]


def _captured_path(f: Path) -> Optional[List[str]]:
    """PATH captured in a systemd unit (``Environment="PATH=…"``) or a watchdog script (``export PATH='…'``)."""
    import shlex
    try:
        text = f.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("Environment=") and "PATH=" in s:
            try:
                val = shlex.split(s)[0].split("=", 2)[2].replace("%%", "%")
            except (ValueError, IndexError):
                continue
            return val.split(":")
        if s.startswith("export PATH="):
            try:
                return shlex.split(s[len("export "):])[0].split("=", 1)[1].split(":")
            except (ValueError, IndexError):
                continue
    return None


def run(kind: str = "node", *, network: bool = True, deep: bool = False) -> List[dict]:
    """``[{"check","status":"ok|warn|fail","detail","fix"}]`` for ``kind`` node|hub|client."""
    if kind not in ("node", "hub", "client"):
        from .errors import bad_request
        raise bad_request(f"unknown doctor target '{kind}'", hint="use node, hub or client")
    d = _Doctor(kind, network, deep)
    d.load()
    groups: List[Tuple[str, Callable[[], None]]] = [("config", d.g_config), ("files", d.g_files)]
    if kind in ("node", "hub"):
        groups.append(("listen", d.g_listen))
    if network:
        groups.append(("network", d.g_network))
    elif kind in ("node", "hub", "client"):
        groups.append(("network", d._fakeip))
    if kind == "node":
        groups += [("secrets", d.g_secrets), ("docker", d.g_docker), ("wsl", d.g_wsl), ("windows", d.g_windows),
                   ("linux", d.g_linux), ("relay", d.g_relay)]
    if kind == "hub":
        groups.append(("linux", d.g_linux))
    groups.append(("policy", d.g_policy))
    if kind == "client":
        groups.append(("client", d.g_client))
    for name, fn in groups:
        d.group(name, fn)
    return d.rows


__all__ = ["run", "parse_icacls", "parse_powercfg", "firewall_rule_present", "ENDPOINTS"]
