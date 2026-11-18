"""Service presets (vllm, sglang, ollama, llamacpp, comfyui, docker), expand_define, approval_review
(design §10.6, §9.6 and §5.6, WP2).

``build`` turns ordered ``preset_args`` pairs into service-definition fields; ``expand_define`` merges them with the
caller-given service.define params (only keys in ``given`` override the preset, v6); ``approval_review`` builds the
owner-facing review in the pinned §5.6 shape and binds the files a command names (v7/v8 file binding).
Every non-docker preset binds 127.0.0.1; the docker preset publishes ports on 127.0.0.1 only. The docker guard rules
are convenience, not security (T23).
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, AbstractSet, Any, Callable, Dict, List, Optional, Tuple, Union

from .errors import HiveError, bad_request, too_large, unsupported
from .util import escape_terminal, human_bytes, iso_utc, parse_kv

if TYPE_CHECKING:  # pragma: no cover
    from .policy import EffectivePolicy

# env keys whose values change what program runs (§5.6 "changes what runs" header)
RISKY_ENV_PATTERNS = ("PATH", "LD_*", "DYLD_*", "BASH_ENV", "ENV", "PYTHON*", "NODE_OPTIONS", "PERL5*", "RUBYOPT",
                      "JAVA_TOOL_OPTIONS", "GIT_*")
HUB_DEFINE_NOTE = ("preset is expanded on the node; for vllm/sglang the node checks whether model is a local folder "
                   "(served name from its .hive-pull.json, no token) or a repo id (may add secret_env HF_TOKEN)")
HUB_START_NOTE = "the definition stored on the node runs; check service_status"
BIND_HINT = "presets bind 127.0.0.1; to use the model from apps use the model gateway (guide('gateway'))"
_MANIFEST_MAX = 1 << 20

# file binding (§5.6 v7/v8)
BIND_MAX_FILES = 8
BIND_MAX_ABSENT = 48     # absent entries (a stat each, nothing hashed) have their own cap (verification of audit 2)
HASH_MAX = 16 << 20
SHOW_MAX = 256 << 10
REVIEW_MAX = 256 << 10
HASH_BUDGET_S = 10.0
PATHLIKE_EXT = (".sh", ".bash", ".py", ".ps1", ".bat", ".cmd", ".js")
NOT_SHOWN = "content not shown; inspect it on the node"

# service definition keys (wire params of service.define that describe the service; not replace/start/wait)
DEF_KEYS = ("name", "description", "preset", "preset_args", "command", "argv", "shell", "cwd", "env", "secret_env",
            "login", "gpus", "stop_command", "port", "endpoint", "endpoint_kind", "api_key_env", "health",
            "health_url", "health_expect", "ready_pattern", "start_period", "health_interval", "health_timeout",
            "unhealthy_threshold", "restart_on_unhealthy", "restart", "restart_delay", "max_restarts",
            "restart_window", "autostart", "stop_timeout", "log_max_mb", "log_keep", "keep_awake", "force_port")
INTERNAL_KEYS = ("container", "post_exit", "volumes")


@dataclass(frozen=True)
class Preset:
    """One service preset (§10.6)."""
    name: str
    description: str
    args: List[dict]                  # [{"name","required","default","description","multi":bool}]
    build: Callable[[Dict[str, Union[str, List[str]]], str, "PresetCtx"], dict]
    example: str
    platforms: Tuple[str, ...] = ("linux", "darwin", "windows")


def _identity_mask(s: str) -> str:
    return s


@dataclass
class PresetCtx:
    """What a builder may consult (no I/O beyond these methods)."""
    os: str
    node_id: str
    policy: "EffectivePolicy"
    secret_names: List[str]
    docker: bool
    mask: Callable[[str], str] = _identity_mask

    def is_local_model(self, model: str) -> bool:
        """True for a path by form (/, ~, ., \\ or :) or a path PathGuard resolves (must exist); False = repo id."""
        if not isinstance(model, str) or not model:
            return False
        if model.startswith(("/", "~", ".")) or "\\" in model or ":" in model:
            return True
        try:
            self.policy.resolve_path(model, must_exist=True)
            return True
        except Exception:  # noqa: BLE001 - resolve errors mean "not local"
            return False

    def manifest_repo(self, path: str) -> Optional[str]:
        """``repo`` of ``<path>/.hive-pull.json`` read through PathGuard (<= 1 MiB, shape-checked), else None."""
        try:
            p = self.policy.resolve_path(os.path.join(path, ".hive-pull.json"), must_exist=True)
            with open(p, "rb") as f:
                raw = f.read(_MANIFEST_MAX + 1)
            if len(raw) > _MANIFEST_MAX:
                return None
            d = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001 - absent, unreadable or denied
            return None
        if isinstance(d, dict) and isinstance(d.get("repo"), str) and d["repo"]:
            return d["repo"]
        return None


# ---------------------------------------------------------------------------------------------------------------
# argument helpers

def _arg(name: str, description: str, *, required: bool = False, default: Any = None, multi: bool = False) -> dict:
    return {"name": name, "required": required, "default": default, "description": description, "multi": multi}


class _ArgError(Exception):
    pass


def _int(v: str, name: str, lo: int, hi: int) -> int:
    try:
        n = int(str(v).strip())
    except ValueError:
        raise _ArgError(f"{name} must be an integer (got {v!r})")
    if not lo <= n <= hi:
        raise _ArgError(f"{name} must be between {lo} and {hi} (got {n})")
    return n


def _float(v: str, name: str, lo: float, hi: float) -> float:
    try:
        x = float(str(v).strip())
    except ValueError:
        raise _ArgError(f"{name} must be a number (got {v!r})")
    if not lo <= x <= hi:
        raise _ArgError(f"{name} must be between {lo:g} and {hi:g} (got {x:g})")
    return x


_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_SERVED_RE = re.compile(r"^[A-Za-z0-9._/:@-]{1,200}$")
_GPUS_RE = re.compile(r"^(all|none|\d{1,3}(,\d{1,3}){0,63})$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}$")
_DOCKER_FLAG_RE = re.compile(r"^--[a-z][a-z0-9-]*(=.*)?$")
_SHM_RE = re.compile(r"^\d{1,6}[bkmgBKMG]?$")
DOCKER_COLLIDING = ("--volume", "--mount", "--publish", "--publish-all", "--name", "--label", "--rm", "--detach",
                    "--env", "--env-file", "--gpus")
DOCKER_FORBIDDEN = ("--privileged", "--pid=host", "--userns=host", "--uts=host", "--network=host", "--net=host",
                    "--cgroupns=host")
HOST_FLAGS = ("--host", "--listen", "--bind")


def _word(v: str, name: str) -> str:
    v = str(v).strip()
    if not v:
        raise _ArgError(f"{name} must not be empty")
    if v.startswith("-"):
        raise _ArgError(f"{name} must not start with '-'")
    if "\x00" in v or "\n" in v:
        raise _ArgError(f"{name} contains control characters")
    return v


def _program(v: str, name: str) -> str:
    v = _word(v, name)
    return os.path.expanduser(v) if v.startswith("~") else v


def _gpus(v: str, *, docker: bool = False) -> str:
    v = str(v).strip().lower().replace(" ", "")
    if docker and v == "amd":
        return v
    if not _GPUS_RE.match(v):
        raise _ArgError(f"gpus must be all, none{', amd' if docker else ''} or a list like 0,1 (got {v!r})")
    return v


def _extra_args(values: List[str]) -> List[str]:
    out = []
    for v in values:
        v = str(v)
        if not v or "\x00" in v or "\n" in v:
            raise _ArgError("extra_arg must be a single non-empty token")
        low = v.lower()
        flag = low.split("=", 1)[0]
        # §10.6: a value *containing* a bind flag is refused; argparse servers (vLLM, SGLang, ComfyUI) also accept
        # unique prefixes (--hos=0.0.0.0), so a flag that abbreviates one is refused too
        if any(h in low for h in HOST_FLAGS) or (
                flag.startswith("--") and len(flag) >= 3 and any(h.startswith(flag) for h in HOST_FLAGS)):
            raise _ArgError(BIND_HINT)
        out.append(v)
    if len(out) > 50:
        raise _ArgError("at most 50 extra_arg values")
    return out


def _local_model(model: str, ctx: PresetCtx) -> Tuple[str, bool]:
    """(model argument for the server, is_local)."""
    if not ctx.is_local_model(model):
        return model, False
    try:
        return str(ctx.policy.resolve_path(model, must_exist=True)), True
    except Exception:  # noqa: BLE001 - outside the roots: a path by form, passed through (no I/O)
        return os.path.expanduser(model), True


def _served_name(a: Dict[str, Any], model_arg: str, raw_model: str, local: bool, ctx: PresetCtx) -> str:
    if a.get("served_model_name"):
        v = str(a["served_model_name"])
        if not _SERVED_RE.match(v):
            raise _ArgError(f"served_model_name has invalid characters (got {v!r})")
        return v
    if local:
        repo = ctx.manifest_repo(model_arg)
        if repo:
            return repo
        return os.path.basename(model_arg.rstrip("/\\")) or raw_model
    return raw_model


def _hf_secret(local: bool, ctx: PresetCtx) -> List[str]:
    return ["HF_TOKEN"] if (not local and "HF_TOKEN" in (ctx.secret_names or [])) else []


def _default_python(ctx: PresetCtx) -> str:
    return "python" if ctx.os == "windows" else "python3"


def _http(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _linux_only(server: str, ctx: PresetCtx) -> None:
    if ctx.os == "linux":
        return
    if ctx.os == "windows":
        hint = "use the WSL node (nodes_list shows e.g. desk-wsl) or preset=docker with Docker Desktop"
    else:
        hint = "use ollama or llamacpp (Metal)"
    raise unsupported(f"{server} needs Linux", hint=hint)


# ---------------------------------------------------------------------------------------------------------------
# builders

def _build_vllm(a: Dict[str, Any], name: str, ctx: PresetCtx) -> dict:
    _linux_only("vLLM", ctx)
    raw = _word(a["model"], "model")
    model, local = _local_model(raw, ctx)
    port = _int(a.get("port", 8000), "port", 1, 65535)
    tp = _int(a.get("tp", 1), "tp", 1, 64)
    served = _served_name(a, model, raw, local, ctx)
    python = _program(a.get("python") or _default_python(ctx), "python")
    argv = [python, "-m", "vllm.entrypoints.openai.api_server", "--model", model, "--served-model-name", served,
            "--host", "127.0.0.1", "--port", str(port), "-tp", str(tp)]
    if a.get("max_model_len"):
        argv += ["--max-model-len", str(_int(a["max_model_len"], "max_model_len", 1, 10_000_000))]
    if a.get("gpu_memory_utilization"):
        argv += ["--gpu-memory-utilization", f"{_float(a['gpu_memory_utilization'], 'gpu_memory_utilization', 0.01, 1.0):g}"]
    if a.get("dtype"):
        if not _TOKEN_RE.match(str(a["dtype"])):
            raise _ArgError("dtype must be a plain word such as auto, bfloat16, float16")
        argv += ["--dtype", str(a["dtype"])]
    argv += _extra_args(a.get("extra_arg") or [])
    d = {"argv": argv, "port": port, "endpoint": _http(port, "/v1"), "endpoint_kind": "openai", "health": "http",
         "health_url": _http(port, "/health"), "start_period": 1800, "stop_timeout": 60, "keep_awake": True,
         "secret_env": _hf_secret(local, ctx)}
    if a.get("gpus"):
        d["gpus"] = _gpus(a["gpus"])
    return d


def _build_sglang(a: Dict[str, Any], name: str, ctx: PresetCtx) -> dict:
    _linux_only("SGLang", ctx)
    raw = _word(a["model"], "model")
    model, local = _local_model(raw, ctx)
    port = _int(a.get("port", 30000), "port", 1, 65535)
    tp = _int(a.get("tp", 1), "tp", 1, 64)
    served = _served_name(a, model, raw, local, ctx)
    python = _program(a.get("python") or _default_python(ctx), "python")
    argv = [python, "-m", "sglang.launch_server", "--model-path", model, "--served-model-name", served,
            "--host", "127.0.0.1", "--port", str(port), "--tp", str(tp)] + _extra_args(a.get("extra_arg") or [])
    d = {"argv": argv, "port": port, "endpoint": _http(port, "/v1"), "endpoint_kind": "openai", "health": "http",
         "health_url": _http(port, "/health"), "start_period": 1800, "secret_env": _hf_secret(local, ctx)}
    if a.get("gpus"):
        d["gpus"] = _gpus(a["gpus"])
    return d


def _build_ollama(a: Dict[str, Any], name: str, ctx: PresetCtx) -> dict:
    port = _int(a.get("port", 11434), "port", 1, 65535)
    env = {"OLLAMA_HOST": f"127.0.0.1:{port}"}
    if a.get("models_dir"):
        md = _word(a["models_dir"], "models_dir")
        try:
            env["OLLAMA_MODELS"] = str(ctx.policy.resolve_path(md, write=True))
        except HiveError:
            raise
    d = {"argv": ["ollama", "serve"], "env": env, "port": port, "endpoint": _http(port, "/v1"),
         "endpoint_kind": "ollama", "health": "http", "health_url": _http(port, "/"), "health_expect": [200, 200],
         "start_period": 60}
    if a.get("gpus"):
        d["gpus"] = _gpus(a["gpus"])
    return d


def _build_llamacpp(a: Dict[str, Any], name: str, ctx: PresetCtx) -> dict:
    mp = _word(a["model_path"], "model_path")
    try:
        model = str(ctx.policy.resolve_path(mp, must_exist=True))
    except HiveError:
        raise
    port = _int(a.get("port", 8080), "port", 1, 65535)
    ngl = _int(a.get("n_gpu_layers", 99), "n_gpu_layers", 0, 100000)
    ctxs = _int(a.get("ctx_size", 8192), "ctx_size", 0, 10_000_000)
    binary = _program(a.get("binary") or "llama-server", "binary")
    argv = [binary, "-m", model, "--host", "127.0.0.1", "--port", str(port), "-ngl", str(ngl), "-c", str(ctxs)]
    argv += _extra_args(a.get("extra_arg") or [])
    return {"argv": argv, "port": port, "endpoint": _http(port, "/v1"), "endpoint_kind": "openai", "health": "http",
            "health_url": _http(port, "/health"), "start_period": 600}


def _build_comfyui(a: Dict[str, Any], name: str, ctx: PresetCtx) -> dict:
    d0 = _word(a["dir"], "dir")
    cwd = str(ctx.policy.resolve_path(d0, must_exist=True))
    port = _int(a.get("port", 8188), "port", 1, 65535)
    python = _program(a.get("python") or _default_python(ctx), "python")
    argv = [python, "main.py", "--listen", "127.0.0.1", "--port", str(port)] + _extra_args(a.get("extra_arg") or [])
    return {"argv": argv, "cwd": cwd, "port": port, "health": "http", "health_url": _http(port, "/system_stats"),
            "start_period": 300}


def _split_windows(s: str) -> List[str]:
    """CommandLineToArgvW-style splitting (backslashes before quotes, double quotes)."""
    out: List[str] = []
    cur: List[str] = []
    i, n = 0, len(s)
    in_q = False
    have = False
    while i < n:
        c = s[i]
        if c == "\\":
            j = i
            while j < n and s[j] == "\\":
                j += 1
            nb = j - i
            if j < n and s[j] == '"':
                cur.append("\\" * (nb // 2))
                if nb % 2:
                    cur.append('"')
                    i = j + 1
                else:
                    i = j
                have = True
                continue
            cur.append("\\" * nb)
            i = j
            have = True
            continue
        if c == '"':
            if in_q and i + 1 < n and s[i + 1] == '"':
                cur.append('"')
                i += 2
                continue
            in_q = not in_q
            have = True
            i += 1
            continue
        if c in " \t" and not in_q:
            if have or cur:
                out.append("".join(cur))
                cur, have = [], False
            i += 1
            continue
        cur.append(c)
        have = True
        i += 1
    if have or cur:
        out.append("".join(cur))
    return out


def _split_volume(v: str) -> Tuple[str, str, Optional[str]]:
    v = str(v).strip()
    mode: Optional[str] = None
    low = v.lower()
    if low.endswith(":ro") or low.endswith(":rw"):
        mode = low[-2:]
        v = v[:-3]
    idx = v.rfind(":/")
    if idx <= 0:
        raise _ArgError(f"volume must be HOST:/CONTAINER[:ro|:rw] (got {v!r})")
    host, container = v[:idx], v[idx + 1:]
    if not ("/" in host or "\\" in host or host == "~" or host.startswith("~")):
        raise _ArgError(f"volume host {host!r} is not a path (named volumes are not supported); "
                        "use an absolute or ~ path")
    if not container.startswith("/") or "\x00" in container:
        raise _ArgError(f"volume container path must be absolute (got {container!r})")
    return host, container, mode


def docker_volume(v: str, policy: Any) -> Tuple[str, str, str]:
    """(resolved host realpath, container path, mode) of one docker ``volume`` arg, checked by PathGuard (an ro root or
    an exec path forces :ro when no mode is given; rw on an ro root -> policy_denied; a folder holding a denied path ->
    contains_denied)."""
    host, container, mode = _split_volume(v)
    if mode == "rw":
        real = policy.resolve_path(host, write=True, must_exist=True, subtree=True)
    elif mode == "ro":
        real = policy.resolve_path(host, write=False, must_exist=True, subtree=True)
    else:
        try:
            real = policy.resolve_path(host, write=True, must_exist=True, subtree=True)
            mode = "rw"
        except HiveError as e:
            if e.code != "policy_denied" or e.details.get("rule") in ("contains_denied", "hive_internal", "fs_deny"):
                raise
            real = policy.resolve_path(host, write=False, must_exist=True, subtree=True)
            mode = "ro"
    return str(real), container, mode


def _is_pod() -> bool:
    return bool(os.environ.get("RUNPOD_POD_ID") or os.environ.get("VAST_CONTAINERLABEL")
                or os.path.isdir("/root/autodl-tmp"))


def _build_docker(a: Dict[str, Any], name: str, ctx: PresetCtx) -> dict:
    if not ctx.docker:
        if _is_pod():
            raise unsupported("docker not found or daemon not running",
                              hint="pods are containers without a Docker daemon; use the vllm preset directly")
        raise unsupported("docker not found or daemon not running",
                          hint="install Docker (Desktop) and start it, then retry (guide('docker'))")
    image = str(a["image"]).strip()
    if not _IMAGE_RE.match(image):
        raise _ArgError(f"image has invalid characters (got {image!r})")
    port = _int(a.get("port", 8000), "port", 1, 65535)
    cport = _int(a.get("container_port") or port, "container_port", 1, 65535)
    gpus = _gpus(a.get("gpus") or "all", docker=True)
    shm = str(a.get("shm_size") or "16g")
    if not _SHM_RE.match(shm):
        raise _ArgError(f"shm_size must look like 16g or 512m (got {shm!r})")
    pull = str(a.get("pull") or "missing").lower()
    if pull not in ("always", "missing", "never"):
        raise _ArgError("pull must be always, missing or never")
    health_path = str(a.get("health_path") or "/health")
    if not health_path.startswith("/") or any(c.isspace() for c in health_path):
        raise _ArgError("health_path must start with / and contain no spaces")
    openai = str(a.get("openai", "true")).strip().lower()
    if openai not in ("true", "false", "1", "0", "yes", "no"):
        raise _ArgError("openai must be true or false")
    stop_timeout = _float(a.get("stop_timeout", 30), "stop_timeout", 0, 300)
    container = f"hive-{name}"
    gpu_args: List[str] = []
    if gpus == "all":
        gpu_args = ["--gpus", "all"]
    elif gpus == "amd":
        gpu_args = ["--device=/dev/kfd", "--device=/dev/dri", "--group-add=video"]
    elif gpus != "none":
        gpu_args = ["--gpus", f'"device={gpus}"']
    vol_args: List[str] = []
    volumes: List[dict] = []
    for v in a.get("volume") or []:
        try:
            real, cpath, mode = docker_volume(v, ctx.policy)
        except _ArgError:
            raise
        except HiveError as e:
            if e.details.get("rule") == "contains_denied":
                raise HiveError(e.code, e.message, details=dict(e.details),
                                hint="mount a subfolder such as ~/models")
            raise
        vol_args += ["-v", f"{real}:{cpath}" + (":ro" if mode == "ro" else "")]
        volumes.append({"spec": str(v), "host": real, "container": cpath, "mode": mode})
    extra: List[str] = []
    for x in a.get("extra_arg") or []:
        x = str(x)
        if not _DOCKER_FLAG_RE.match(x):
            raise _ArgError(f"extra_arg {x!r} must be one --flag or --flag=value token (no short flags or "
                            "separate values)")
        flag = x.split("=", 1)[0]
        if flag in DOCKER_COLLIDING:
            raise _ArgError(f"extra_arg {flag} collides with a dedicated preset arg")
        if x in DOCKER_FORBIDDEN or flag == "--privileged" or (
                flag in ("--pid", "--userns", "--uts", "--network", "--net", "--cgroupns")
                and x.split("=", 1)[-1].lower() == "host"):
            raise _ArgError(f"extra_arg {x} is refused (privileged or host namespace)")
        extra.append(x)
    if len(extra) > 50:
        raise _ArgError("at most 50 extra_arg values")
    args_s = str(a.get("args") or "")
    try:
        cargs = _split_windows(args_s) if ctx.os == "windows" else shlex.split(args_s)
    except ValueError as e:
        raise _ArgError(f"args: {e}")
    argv = (["docker", "run", "--rm", "--init", "--name", container, "--label", f"ai.hive.node={ctx.node_id}",
             "--label", f"ai.hive.service={name}", f"--pull={pull}", f"--shm-size={shm}"] + gpu_args
            + ["-p", f"127.0.0.1:{port}:{cport}"] + vol_args)
    image_at = len(argv)
    argv += extra + [image] + cargs
    d = {"argv": argv, "port": port, "health": "http", "health_url": _http(port, health_path),
         "start_period": 1800, "stop_timeout": stop_timeout + 10,
         "stop_command": ["docker", "stop", "-t", f"{stop_timeout:g}", container],
         "post_exit": ["docker", "rm", "-f", container], "container": container, "volumes": volumes,
         "_env_at": image_at}
    if openai in ("true", "1", "yes"):
        d["endpoint"] = _http(port, "/v1")
        d["endpoint_kind"] = "openai"
    return d


PRESETS: Dict[str, Preset] = {
    "vllm": Preset(
        name="vllm", platforms=("linux",),
        description="vLLM OpenAI-compatible server (Linux; on Windows use the WSL node). model = a local folder "
                    "(after model_pull) or a Hugging Face repo id.",
        args=[_arg("model", "local model folder or HF repo id", required=True),
              _arg("port", "port on 127.0.0.1", default=8000), _arg("gpus", "GPU ids like 0,1, all or none"),
              _arg("tp", "tensor parallel size", default=1),
              _arg("served_model_name", "model name clients use (default: repo id or folder name)"),
              _arg("max_model_len", "context length"), _arg("gpu_memory_utilization", "0..1 fraction of VRAM"),
              _arg("dtype", "auto, bfloat16, float16, …"),
              _arg("python", "python of the vLLM venv, e.g. ~/.venvs/vllm/bin/python", default="python3"),
              _arg("extra_arg", "one extra vLLM flag per value, e.g. --enable-prefix-caching", multi=True)],
        build=_build_vllm,
        example="preset=vllm preset_args=['model=~/models/Qwen--Qwen2.5-7B-Instruct','python=~/.venvs/vllm/bin/python',"
                "'max_model_len=32768']"),
    "sglang": Preset(
        name="sglang", platforms=("linux",),
        description="SGLang server (Linux). model = a local folder or a Hugging Face repo id.",
        args=[_arg("model", "local model folder or HF repo id", required=True),
              _arg("port", "port on 127.0.0.1", default=30000), _arg("gpus", "GPU ids like 0,1, all or none"),
              _arg("tp", "tensor parallel size", default=1),
              _arg("served_model_name", "model name clients use"),
              _arg("python", "python of the SGLang venv", default="python3"),
              _arg("extra_arg", "one extra flag per value", multi=True)],
        build=_build_sglang,
        example="preset=sglang preset_args=['model=~/models/Qwen--Qwen2.5-7B-Instruct','python=~/.venvs/sglang/bin/python']"),
    "ollama": Preset(
        name="ollama",
        description="ollama serve on 127.0.0.1 (OpenAI API at /v1).",
        args=[_arg("port", "port on 127.0.0.1", default=11434), _arg("models_dir", "OLLAMA_MODELS folder"),
              _arg("gpus", "GPU ids like 0,1, all or none")],
        build=_build_ollama, example="preset=ollama preset_args=['port=11434']"),
    "llamacpp": Preset(
        name="llamacpp",
        description="llama.cpp llama-server for a GGUF file (OpenAI API at /v1).",
        args=[_arg("model_path", "GGUF file", required=True), _arg("port", "port on 127.0.0.1", default=8080),
              _arg("binary", "llama-server binary", default="llama-server"),
              _arg("n_gpu_layers", "layers offloaded to the GPU", default=99),
              _arg("ctx_size", "context size", default=8192),
              _arg("extra_arg", "one extra flag per value", multi=True)],
        build=_build_llamacpp, example="preset=llamacpp preset_args=['model_path=~/models/qwen2.5-7b-q4_k_m.gguf']"),
    "comfyui": Preset(
        name="comfyui",
        description="ComfyUI (python main.py) in its folder; no OpenAI endpoint.",
        args=[_arg("dir", "ComfyUI folder (holds main.py)", required=True),
              _arg("port", "port on 127.0.0.1", default=8188),
              _arg("python", "python of the ComfyUI venv (Windows: python)", default="python3"),
              _arg("extra_arg", "one extra flag per value", multi=True)],
        build=_build_comfyui, example="preset=comfyui preset_args=['dir=~/ComfyUI','python=~/ComfyUI/venv/bin/python']"),
    "docker": Preset(
        name="docker",
        description="A container run in the foreground under the shim (docker run --rm --init), published on "
                    "127.0.0.1; stopped with docker stop. Needs Docker.",
        args=[_arg("image", "image reference", required=True), _arg("port", "host port on 127.0.0.1", default=8000),
              _arg("container_port", "port inside the container (default: port)"),
              _arg("gpus", "all, none, amd or ids like 0,1", default="all"),
              _arg("volume", "HOST:/CONTAINER[:ro|:rw] (HOST checked by node policy)", multi=True),
              _arg("shm_size", "shared memory", default="16g"),
              _arg("pull", "always, missing or never", default="missing"),
              _arg("args", "container command (split like a shell command line)"),
              _arg("extra_arg", "one --flag or --flag=value docker run option", multi=True),
              _arg("health_path", "HTTP health path", default="/health"),
              _arg("openai", "the container serves the OpenAI API at /v1", default="true"),
              _arg("stop_timeout", "seconds docker stop waits", default=30)],
        build=_build_docker,
        example="preset=docker preset_args=['image=vllm/vllm-openai:<tag>','port=8000',"
                "'volume=/home/me/models:/models:ro','args=--model /models/Qwen--Qwen2.5-7B-Instruct "
                "--served-model-name qwen2.5-7b --host 0.0.0.0']"),
}


def _bad_args(p: Preset, msg: str) -> HiveError:
    return bad_request(f"preset {p.name}: {msg}", hint=p.example, args=[dict(a) for a in p.args])


def build(preset: str, args: List[Tuple[str, str]], name: str, *, ctx: PresetCtx) -> dict:
    """Build service-definition fields from ordered ``(key, value)`` preset args (single-valued args take the last
    occurrence, multi args accumulate); bad args -> bad_request with ``details.args`` and the example as hint."""
    p = PRESETS.get(preset)
    if p is None:
        raise bad_request(f"unknown preset {preset!r}", hint="presets: " + ", ".join(sorted(PRESETS)))
    if ctx.os not in p.platforms and preset not in ("vllm", "sglang"):
        raise unsupported(f"preset {preset} is not available on {ctx.os}")
    table = {a["name"]: a for a in p.args}
    vals: Dict[str, Any] = {}
    for k, v in args:
        if k not in table:
            raise _bad_args(p, f"unknown arg {k!r}")
        if table[k]["multi"]:
            vals.setdefault(k, []).append(v)
        else:
            vals[k] = v
    for a in p.args:
        if a["required"] and not vals.get(a["name"]):
            raise _bad_args(p, f"missing required arg {a['name']!r}")
    try:
        out = p.build(vals, name, ctx)
    except _ArgError as e:
        raise _bad_args(p, str(e))
    return out


def _env_dict(env: Any) -> Dict[str, str]:
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    return parse_kv(env or [])


def expand_define(params: dict, *, ctx: PresetCtx, given: AbstractSet[str]) -> dict:
    """The full service definition from validated service.define params: preset args -> build(), the top-level
    ``port`` rule, caller-given fields override (only keys in ``given``), env merged key by key, secret_env union
    incl. auto-added names. Without a preset the params are used as they are."""
    params = dict(params or {})
    name = str(params.get("name") or "")
    preset_name = params.get("preset")
    if not preset_name:
        d = {k: params[k] for k in DEF_KEYS if params.get(k) is not None}
        d["env"] = _env_dict(params.get("env"))
        d["secret_env"] = list(params.get("secret_env") or [])
        return d
    p = PRESETS.get(preset_name)
    if p is None:
        raise bad_request(f"unknown preset {preset_name!r}", hint="presets: " + ", ".join(sorted(PRESETS)))
    pairs: List[Tuple[str, str]] = []
    for item in params.get("preset_args") or []:
        s = str(item)
        if "=" not in s:
            raise _bad_args(p, f"preset_args item {s!r} is not key=value")
        k, v = s.split("=", 1)
        pairs.append((k.strip(), v))
    if params.get("port") is not None and "port" in given and any(a["name"] == "port" for a in p.args):
        mine = [v for k, v in pairs if k == "port"]
        if mine:
            try:
                same = int(mine[-1]) == int(params["port"])
            except ValueError:
                same = False
            if not same:
                raise bad_request(f"port {params['port']} conflicts with preset_args port={mine[-1]}",
                                  hint="give the port once (top-level port or preset_args)")
        else:
            pairs.append(("port", str(params["port"])))
    built = build(preset_name, pairs, name, ctx=ctx)
    d: Dict[str, Any] = {}
    for k in DEF_KEYS:
        if k in ("env", "secret_env"):
            continue
        if k in given and params.get(k) is not None:
            d[k] = params[k]
        elif built.get(k) is not None:
            d[k] = built[k]
        elif params.get(k) is not None:
            d[k] = params[k]
    caller_env = _env_dict(params.get("env")) if "env" in given else {}
    if preset_name != "docker" and any(k.upper() == "OLLAMA_HOST" for k in caller_env):
        raise bad_request("env OLLAMA_HOST is not allowed with this preset: " + BIND_HINT, hint=BIND_HINT)
    env = _env_dict(built.get("env"))
    env.update(caller_env)
    d["env"] = env
    caller_secrets = [str(x) for x in (params.get("secret_env") or [])] if "secret_env" in given else []
    secrets: List[str] = []
    for s in list(built.get("secret_env") or []) + caller_secrets:
        if s not in secrets:
            secrets.append(s)
    d["secret_env"] = secrets
    d["preset"] = preset_name
    d["preset_args"] = [str(x) for x in (params.get("preset_args") or [])]
    d["name"] = name
    for k in INTERNAL_KEYS:
        if built.get(k) is not None:
            d[k] = built[k]
    if preset_name == "docker" and isinstance(d.get("argv"), list) and "_env_at" in built:
        at = int(built["_env_at"])
        flags: List[str] = []
        for n in list(env) + caller_secrets:
            if n not in flags[1::2]:
                flags += ["-e", n]
        d["argv"] = d["argv"][:at] + flags + d["argv"][at:]
    return d


def describe() -> List[dict]:
    """Preset table for ``guide('presets')`` (args, defaults, multi, platforms, example)."""
    return [{"name": p.name, "description": p.description, "platforms": list(p.platforms),
             "args": [dict(a) for a in p.args], "example": p.example} for p in PRESETS.values()]


# ---------------------------------------------------------------------------------------------------------------
# approval review (§5.6)

def _env_pairs(env: object) -> List[Tuple[str, str]]:
    if isinstance(env, dict):
        return [(str(k), str(v)) for k, v in env.items()]
    out: List[Tuple[str, str]] = []
    for item in env if isinstance(env, (list, tuple)) else ():
        s = str(item)
        k, _, v = s.partition("=")
        out.append((k, v))
    return out


def _secret_key_re() -> "re.Pattern[str]":
    try:
        from .audit import SECRET_KEY_RE
    except Exception:  # noqa: BLE001
        SECRET_KEY_RE = r"(?i)(pass|secret|token|key|auth|cred|cookie|bearer)"
    return re.compile(SECRET_KEY_RE)


def _redact_value(v: str) -> str:
    # WP1 CONTRACT-REQUEST (PHASE0_NOTES): redact_text masks values only in context (KEY=value, --token X, known
    # prefixes); a bare value of a secret-named env key that redact_text leaves unchanged is shown as ***
    try:
        from .audit import redact_text
        out = redact_text(v)
        return out if out != v else "***"
    except (ImportError, HiveError):
        return "***"


def _risky(keys: List[str]) -> List[str]:
    out: List[str] = []
    for k in keys:
        if any(fnmatch.fnmatchcase(k.upper(), p) for p in RISKY_ENV_PATTERNS) and k not in out:
            out.append(k)
    return out


def _windows_shell(shell: Optional[str], os_name: Optional[str]) -> bool:
    if shell in ("powershell", "pwsh", "cmd"):
        return True
    return shell in (None, "", "auto") and os_name == "windows"


_SHELL_OPS = frozenset("();<>|&")
_META_SPLIT = re.compile(r"[;|&<>()]+")


def _raw_words(argv_or_command: Union[List[str], str], *, shell: Optional[str], os_name: Optional[str]) -> List[str]:
    return [w for w, _s in _spelled_words(argv_or_command, shell=shell, os_name=os_name)]


# v8.1 (external review): quoting state of each character of a POSIX shell word: 0 unquoted, 1 inside "…" ($ still
# expands, ~ does not), 2 literal ('…', $'…', backslash-escaped: nothing expands)
_Q_NONE, _Q_DOUBLE, _Q_LITERAL = 0, 1, 2


def _posix_tokens(text: str) -> List[List[Tuple[str, int]]]:
    """Words of a POSIX shell string as ``[(char, quoting)]``: operators ``();<>|&`` and whitespace split words,
    ``#`` at a word start begins a comment; an unclosed quote raises ValueError."""
    return [val[0] for kind, val in _posix_scan(text) if kind == "w"]


def _subst_end(text: str, i: int) -> int:
    """Index after the command substitution starting at ``text[i]`` (`` `…` `` or ``$(…)``, nested parentheses and
    quotes inside skipped); -1 when it is not closed."""
    n = len(text)
    if text[i] == "`":
        j = i + 1
        while j < n and text[j] != "`":
            j += 2 if text[j] == "\\" else 1
        return j + 1 if j < n else -1
    depth = 0
    j = i + 1
    while j < n:
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c in "'\"":
            k = text.find(c, j + 1)
            if k < 0:
                return -1
            j = k + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return -1


def _posix_scan(text: str) -> List[Tuple[str, Any]]:
    """``_posix_tokens`` with the command structure kept (v8.1, audit round 2): ``("w", (chars, redirect))`` per
    word and ``("sep", ops)`` where a simple command ends (``; & | ( )`` and newlines; ``ops`` the operator
    characters). ``redirect`` marks a word after ``<``/``>`` (its target: ``"in"`` for ``<``/``<>``, ``"here"`` for
    ``<<``, ``"herestr"`` for ``<<<``, ``"fd"`` for ``<&``, ``"out"`` for ``>`` forms) and a digit word right before
    one (``2>&1``: ``"fd"``); ``""`` otherwise. ``$(…)`` and `` `…` `` stay inside their word."""
    items: List[Tuple[str, Any]] = []
    cur: Optional[List[Tuple[str, int]]] = None
    redir = ""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n" or c in _SHELL_OPS:
            if cur is not None:
                fd = c in "<>" and all(q == _Q_NONE and ch.isdigit() for ch, q in cur)
                items.append(("w", (cur, redir or ("fd" if fd else ""))))
                redir = ""
                cur = None
            prev = text[i - 1] if i else ""
            nxt = text[i + 1] if i + 1 < n else ""
            if c == "<":
                if prev != "<":
                    redir = ("herestr" if text[i + 2:i + 3] == "<" else "here") if nxt == "<" else \
                        "fd" if nxt == "&" else "" if nxt == "(" else "in"
            elif c == ">":
                if prev != "<":
                    redir = "out"
            elif (c == "&" and (prev in "<>" or nxt == ">")) or (c == "|" and prev == ">"):
                pass        # >&, <&, &>, >|: part of a redirection
            elif c == "\n" or c in _SHELL_OPS:
                if items and items[-1][0] == "sep":
                    items[-1] = ("sep", items[-1][1] + c)
                else:
                    items.append(("sep", c))
                redir = ""
            i += 1
            continue
        if c == "#" and cur is None:
            j = text.find("\n", i)
            i = n if j < 0 else j
            continue
        if cur is None:
            cur = []
        if c == "\\":
            if i + 1 < n and text[i + 1] != "\n":
                cur.append((text[i + 1], _Q_LITERAL))
            i += 2
            continue
        if c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise ValueError("no closing quotation")
            cur.extend((ch, _Q_LITERAL) for ch in text[i + 1:j])
            i = j + 1
            continue
        if c == "`" or (c == "$" and text[i + 1:i + 2] == "(" and text[i + 2:i + 3] != "("):
            # v8.1 (audit round 2 verification): a command substitution stays inside its word (its text is planned
            # again by _scan_commands; a word holding one cannot be bound: _shell_spellings)
            j = _subst_end(text, i)
            if j < 0:
                raise ValueError("no closing command substitution")
            cur.extend((ch, _Q_NONE) for ch in text[i:j])
            i = j
            continue
        if c == "$" and i + 1 < n and text[i + 1] == "'":
            j = i + 2
            while j < n and text[j] != "'":
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                cur.append((text[j], _Q_LITERAL))
                j += 1
            if j >= n:
                raise ValueError("no closing quotation")
            i = j + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n and text[j + 1] in '$`"\\\n':
                    if text[j + 1] != "\n":
                        cur.append((text[j + 1], _Q_LITERAL))
                    j += 2
                    continue
                cur.append((text[j], _Q_DOUBLE))
                j += 1
            if j >= n:
                raise ValueError("no closing quotation")
            i = j + 1
            continue
        cur.append((c, _Q_NONE))
        i += 1
    if cur is not None:
        items.append(("w", (cur, redir)))
    return items


_NAME_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELL_VAR_RE = re.compile(r"\$(\{[^}]*\}?|[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-])")
_LEADING_VAR_RE = re.compile(r"^\$(\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")
UNBOUND_VARS = "(variables; not bound)"
LITERAL_PREFIX = "./"


def _known_var(s: str) -> bool:
    """``s`` holds a ``$NAME``/``${NAME}``/``%NAME%`` that PathGuard would expand (policy.PATH_VARS)."""
    try:
        from .policy import PATH_VARS, _VAR_RE
    except Exception:  # noqa: BLE001
        return "$" in s or "%" in s
    names = {v.lower() for v in PATH_VARS}
    return any((m.group(1) or m.group(2) or m.group(3) or "").lower() in names for m in _VAR_RE.finditer(s))


def _literal_spelling(s: str) -> Optional[str]:
    """The word as a path the program opens literally (no ``~`` or variable expansion): ``~…`` -> ``./~…`` (relative
    to the cwd, so the binding does not expand it); None when PathGuard would expand a variable in it."""
    if _known_var(s):
        return None
    return LITERAL_PREFIX + s if s.startswith("~") else s


def _shell_spellings(chars: List[Tuple[str, int]]) -> List[Optional[str]]:
    """Bind spellings of one POSIX shell word (or the value part of a KEY=VALUE word): the path the shell hands the
    program. An unquoted leading ``~`` (up to the first ``/``) stays (the binding expands it); a quoted or escaped one
    is literal (``./~…``); a leading ``$HOME``-style variable outside single quotes stays (PathGuard expands the
    same fixed set); any other expanded ``$`` makes the word unbindable (None)."""
    raw = "".join(c for c, _q in chars)
    if not raw:
        return []
    expandable = "".join(c if q != _Q_LITERAL else "\x01" for c, q in chars)
    if "`" in expandable or "$(" in expandable:
        return [None]          # a command substitution: the shell hands the program its output (v8.1)
    dollars = list(_SHELL_VAR_RE.finditer(expandable))
    if dollars:
        m = _LEADING_VAR_RE.match(expandable)
        try:
            from .policy import PATH_VARS
        except Exception:  # noqa: BLE001
            PATH_VARS = ()   # type: ignore[assignment]
        name = (m.group(2) or m.group(3)) if m else None
        if not (m and len(dollars) == 1 and name in PATH_VARS):
            return [None]
        rest = raw[m.end():]
        return [None] if _known_var(rest) else [raw]
    if raw.startswith("~"):
        prefix = chars[:next((i for i, (c, q) in enumerate(chars) if c == "/" and q == _Q_NONE), len(chars))]
        if all(q == _Q_NONE for _c, q in prefix):
            return [None] if _known_var(raw) else [raw]
    return [_literal_spelling(raw)]


def _spelled_words(argv_or_command: Union[List[str], str], *, shell: Optional[str], os_name: Optional[str]
                   ) -> List[Tuple[str, List[Tuple[str, List[Optional[str]]]]]]:
    """[(raw word, [(candidate, its bind spellings)])] (v8.1, external review): what each candidate (``_word_candidates``)
    names as a path. argv elements are literal (nothing expands ``~`` or variables; a leading ``~`` is bound both as
    the literal ``./~…`` and expanded, since programs often expand it themselves); a POSIX shell expands only an
    unquoted leading ``~`` (``_shell_spellings``); PowerShell/cmd words are bound literally and, for ``~``, expanded
    too. A None spelling cannot be bound (the header lists it)."""
    out: List[Tuple[str, List[Tuple[str, List[Optional[str]]]]]] = []

    def both(c: str) -> List[Optional[str]]:
        lit = _literal_spelling(c)
        if c.startswith("~"):
            return [lit, None if _known_var(c) else c]
        return [lit]

    if isinstance(argv_or_command, (list, tuple)):
        for w in (str(x) for x in argv_or_command):
            out.append((w, [(c, both(c)) for c in _word_candidates(w)]))
        return out
    text = str(argv_or_command)
    toks: Optional[List[List[Tuple[str, int]]]] = None
    if not _windows_shell(shell, os_name):
        try:
            toks = _posix_tokens(text)
        except ValueError:
            toks = None
    if toks is None:
        for w in (x.strip("\"'") for w in text.split() for x in _META_SPLIT.split(w)):
            out.append((w, [(c, both(c)) for c in _word_candidates(w)]))
        return out
    for chars in toks:
        w = "".join(c for c, _q in chars)
        cands: List[Tuple[str, List[Optional[str]]]] = []
        if w and not w.startswith("-"):
            cands.append((w, _shell_spellings(chars)))
        if "=" in w:
            k = w.index("=")
            v = w[k + 1:]
            if v and not v.startswith("-"):
                vs = _shell_spellings(chars[k + 1:])
                if vs == [v] and v.startswith("~") and not _NAME_ASSIGN_RE.match(w):
                    # bash expands ~ after '=' only in NAME=… words; a program may still expand --opt=~/x itself
                    vs = [_literal_spelling(v), v]
                cands.append((v, vs))
        out.append((w, cands))
    return out


def _word_candidates(w: str) -> List[str]:
    out: List[str] = []
    if not w:
        return out
    if not w.startswith("-"):
        out.append(w)
    if "=" in w:
        v = w.split("=", 1)[1]
        if v and not v.startswith("-"):
            out.append(v)
    return out


# v8.1 (external review): words that change the directory later words are resolved in. After one of them a relative
# word is not bound (the review cannot know which file it names) and is listed as "not bound (cwd may change)".
CWD_CHANGERS = frozenset({"cd", "pushd", "popd", "chdir", "set-location", "sl", "push-location", "pop-location"})
_CWD_OPTION_RE = re.compile(r"^(-C.*|--directory(=.*)?|--chdir(=.*)?|-chdir=.*|--work-tree(=.*)?|--git-dir(=.*)?)$")


def changes_cwd(word: str) -> bool:
    """``word`` changes the directory later words are resolved in (cd/pushd/Set-Location/sl/chdir, ``-C <dir>``,
    ``--directory``, ``--chdir``, terraform ``-chdir=``)."""
    base = os.path.basename(word.replace("\\", "/")).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base in CWD_CHANGERS or bool(_CWD_OPTION_RE.match(word))


def relative_word(w: str) -> bool:
    """Not absolute, not ``~``-based and not a drive path (resolved against the cwd)."""
    return not (w.startswith(("~", "/", "\\")) or re.match(r"^[A-Za-z]:[\\/]", w))


def candidate_words(argv_or_command: Union[List[str], str], *, shell: Optional[str] = None,
                    os_name: Optional[str] = None) -> List[str]:
    """Words a command names (§5.6 file binding): argv elements, or the command split (shlex for POSIX shells,
    whitespace with quotes stripped for PowerShell/cmd), plus the part after the first ``=`` of KEY=VALUE /
    --opt=VALUE words; words starting with ``-`` are skipped."""
    out: List[str] = []
    for w in _raw_words(argv_or_command, shell=shell, os_name=os_name):
        for c in _word_candidates(w):
            if c not in out:
                out.append(c)
    return out


# v8.1 (external review, round 2): in a shell string a cwd change can come from anywhere (a function or loop body run
# later, eval, a substitution, a variable in command position, cmd's "cd/d" or "cd..", PowerShell's
# .SetLocation()), so the decision is made on the whole text: when it may change the cwd, NO relative word is bound.
_CWD_TEXT_RE = re.compile(
    r"(?i)(?<![\w.-])(cd|chdir|pushd|popd|set-location|sl|push-location|pop-location)(?![\w-]|\.\w)"
    r"|setlocation|setcurrentdirectory|currentdirectory\s*=")
_DYNAMIC_TEXT_RE = re.compile(
    r"(?i)`|\$\(|(?<![\w.-])(eval|source|iex|invoke-expression|invoke-command|icm)(?![\w-])"
    r"|(^|[;&|(){}\n]|\b(do|then|else|exec|command|env|xargs|sudo|nohup|time)\s)\s*[\"']?\$[{\w]"
    r"|(^|[;&|(){}\n])\s*\.\s")


def text_may_change_cwd(text: str) -> bool:
    """The shell text may change the directory its relative words are resolved in (a cwd changer anywhere, or code
    the review cannot read: backticks, ``$(``, eval/source/iex, a variable in command position) (v8.1)."""
    return bool(_CWD_TEXT_RE.search(text) or _DYNAMIC_TEXT_RE.search(text))


# ---------------------------------------------------------------------------------------------------------------
# v8.1 (audit round 2, finding 1): the script an interpreter runs is bound whatever its name looks like.
#
# Each simple command is read the way the program reads its arguments: leading NAME=VALUE assignments and shell
# keywords, wrappers that run the rest of the line (env, nice, timeout, sudo, xargs, uv run, …), then a known
# interpreter (sh family, python, perl, ruby, node, deno, bun, Rscript/R, php, lua, awk, sed, pwsh/powershell, cmd)
# whose options are skipped with their values (``python -X dev``, ``bash -O extglob``, ``node --require x``,
# ``perl -I dir``) until the script operand, which is bound as ``./name`` (a missing one is bound ``absent``).
# ``python -m mod`` binds the module files the cwd would shadow; ``sh -c``/``cmd /c``/``pwsh -Command`` text is
# read again as a command line. Words recognized as command names, option values that are not files, and code
# (``-c``/``-e`` text) are "quiet": a missing one is not listed in the review header; every other missing word that
# is not a flag or a path is listed as "no file here, not bound" (approval_review/hub).

class _W:
    """One word of a simple command: its text (quotes removed), bind spellings and its redirect kind (``_posix_scan``:
    "in", "here", "herestr", "fd", "out"; "" for an argument). Verification of audit round 2: whether THIS occurrence
    is quiet (``quiet``; ``vquiet`` for the part after ``=``) is kept on the word, so a name hushed in one place is
    still listed where it is a plain argument; ``parent`` is the word an option value was cut from (``-Xdev``,
    ``--opt=val``); ``seg`` the simple command it belongs to."""
    __slots__ = ("text", "spellings", "redir", "quiet", "vquiet", "parent", "seg")

    def __init__(self, text: str, spellings: List[Optional[str]], redir: str = "", parent: "Optional[_W]" = None,
                 seg: int = 0) -> None:
        self.text = text
        self.spellings = spellings
        self.redir = redir
        self.quiet = False
        self.vquiet = False
        self.parent = parent
        self.seg = parent.seg if parent is not None else seg


def _sub(text: str, parent: _W) -> _W:
    """An option value cut from ``parent`` (attached rest or the part after ``=``)."""
    return _W(text, _plain_spellings(text), parent=parent)


def _plain_spellings(w: str) -> List[Optional[str]]:
    """Spellings of a word nothing expands (argv element, PowerShell/cmd word, option value)."""
    lit = _literal_spelling(w)
    if w.startswith("~"):
        return [lit, None if _known_var(w) else w]
    return [lit]


class _Roles:
    """What ``_scan_commands`` found: script operands (text -> spellings), quiet words, extra paths bound quietly
    (python -m module files, cmd PATHEXT names), command text to read again, header items, cwd changes.

    Verification of audit round 2: positions are kept per simple command (``seg``): ``cwd_segs`` are commands after
    which the cwd may differ (cd/pushd/popd, eval, source, a program named by a variable or substitution),
    ``late_segs`` start code that may run later (loops, functions, alias, trap); ``nested`` entries are
    ``(text, dialect, seg, late, quiet)``."""

    def __init__(self) -> None:
        self.ops: Dict[str, List[Optional[str]]] = {}
        self.op_seg: Dict[str, int] = {}
        self.entry_ops: set = set()
        self.quiet: set = set()
        self.extras: List[Tuple[str, int]] = []
        self.nested: List[Tuple[str, str, int, bool, bool]] = []
        self.unbound: List[str] = []
        self.cwd = False
        self.seg = 0
        self.cwd_segs: List[int] = []
        self.late_segs: List[int] = []
        self.stdin_kind: Optional[str] = None
        self.words: List[_W] = []
        self.dialect = ""

    def hush(self, w: Union[_W, str], *, paths: bool = False) -> None:
        """Not listed when missing (a path-like word is still bound and listed as before, unless ``paths``: a word
        that is never run, such as a lock file, is not listed even when it cannot be resolved)."""
        if isinstance(w, _W):
            if w.parent is not None:
                w.parent.vquiet = True
            else:
                w.quiet = w.vquiet = True
        text = w.text if isinstance(w, _W) else w
        for sp in [text] + list(w.spellings if isinstance(w, _W) else _plain_spellings(text)):
            if sp is not None and (paths or not _pathlike(sp)):
                self.quiet.add(sp)

    def op(self, w: Union[_W, str], *, pathext: bool = False) -> None:
        """``w`` is a script the program runs: bind it as a path relative to the cwd (``./name``)."""
        if isinstance(w, str):
            w = _W(w, _plain_spellings(w), seg=self.seg)
        if not w.text or w.text == "-":
            return
        if pathext and "." not in os.path.basename(w.text.replace("\\", "/")):
            self.hush(w)
            for ext in _pathext():   # cmd runs name + each PATHEXT extension from the cwd before PATH
                self.extra(w.text + ext)
            return
        out: List[Optional[str]] = []
        for sp in w.spellings:
            if sp is not None and relative_word(sp) and not _pathlike(sp) and not sp.startswith(("./", ".\\")):
                sp = LITERAL_PREFIX + sp
            if sp not in out:
                out.append(sp)
        self.ops.setdefault(w.text, out)
        self.op_seg.setdefault(w.text, self.seg)

    def extra(self, path: str) -> None:
        if relative_word(path) and not path.startswith(("./", ".\\")):
            path = LITERAL_PREFIX + path
        if path not in [p for p, _s in self.extras]:
            self.extras.append((path, self.seg))
            self.quiet.add(path)

    def nest(self, text: str, dialect: str, *, late: bool = False, quiet: bool = False,
             seg: Optional[int] = None) -> None:
        """``text`` is a command line the program runs (planned again): at this command's position, or ``late`` (at
        an unknown time: trap, alias, watch, an argument that looks like a command line); ``quiet`` words add no
        header items."""
        item = (text, dialect, self.seg if seg is None else seg, late, quiet)
        if text.strip() and item not in self.nested:
            self.nested.append(item)

    def mark_cwd(self) -> None:
        if self.seg not in self.cwd_segs:
            self.cwd_segs.append(self.seg)

    def mark_late(self) -> None:
        if self.seg not in self.late_segs:
            self.late_segs.append(self.seg)


# Windows' default PATHEXT (cmd and ShellExecute run name + each of these from the cwd; .js/.vbs/.wsf run through
# Windows Script Host); on a Windows node its own PATHEXT is added (verification of audit round 2)
_PATHEXT = (".com", ".exe", ".bat", ".cmd", ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".msc")


def _pathext() -> Tuple[str, ...]:
    out = list(_PATHEXT)
    if os.name == "nt":  # pragma: no cover - Windows only
        for e in (os.environ.get("PATHEXT") or "").split(";"):
            e = e.strip().lower()
            if e.startswith(".") and len(e) <= 8 and e not in out:
                out.append(e)
    return tuple(out)
_SCAN_DEPTH = 4
_SH_KEYWORDS = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "fi", "done", "esac",
                          "function", "coproc", "[[", "]]"})
_CMD_BUILTINS = frozenset({
    "assoc", "break", "call", "cd", "chdir", "cls", "color", "copy", "date", "del", "dir", "echo", "endlocal",
    "erase", "exit", "for", "ftype", "goto", "if", "md", "mkdir", "mklink", "move", "path", "pause", "popd", "prompt",
    "pushd", "rd", "rem", "ren", "rename", "rmdir", "set", "setlocal", "shift", "start", "time", "title", "type",
    "ver", "verify", "vol"})
# a number, size, duration, port or version (``10``, ``5s``, ``4GiB``, ``0.0.0.0:8000``, ``1e-3``, ``80%``); not
# ``1run`` or ``9x`` (verification of audit round 2: such names are listed when missing)
_NUMERIC_RE = re.compile(r"^[+-]?[0-9][0-9.:,_]*([eE][+-]?[0-9]+)?"
                         r"(%|[kKmMgGtTpPeE]i?[bB]?|[bB]|[nmu]?s|m|h|d|w|ms|us|ns|min|sec)?$")


def _cmd_base(word: str) -> str:
    base = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


# wrappers that run the rest of their words as a command:
# (short options with a value, long options with a value, positional words before the command, options that change
#  the cwd, options after which no command runs, options whose value is a command line, options whose value is a file)
_Wrap = Tuple[str, AbstractSet[str], int, AbstractSet[str], AbstractSet[str], AbstractSet[str], AbstractSet[str]]
_E: AbstractSet[str] = frozenset()
_WRAPPERS: Dict[str, _Wrap] = {
    "env": ("uCSP", frozenset({"--unset", "--chdir", "--split-string"}), 0, frozenset({"-C", "--chdir"}), _E,
            frozenset({"-S", "--split-string"}), _E),
    "nice": ("n", frozenset({"--adjustment"}), 0, _E, _E, _E, _E),
    "timeout": ("sk", frozenset({"--signal", "--kill-after"}), 1, _E, _E, _E, _E),
    "sudo": ("ugChprtTUDRac", frozenset({"--user", "--group", "--close-from", "--chdir", "--chroot", "--prompt",
                                          "--role", "--type", "--command-timeout", "--other-user", "--host",
                                          "--login-class", "--auth-type"}), 0,
             frozenset({"-D", "--chdir", "-R", "--chroot"}),
             frozenset({"-e", "--edit", "-l", "--list", "-v", "--validate", "-K", "-V", "--version"}), _E, _E),
    "doas": ("uC", _E, 0, _E, _E, _E, _E),
    "xargs": ("adEILnPs", frozenset({"--arg-file", "--delimiter", "--max-lines", "--max-args", "--max-procs",
                                     "--max-chars", "--process-slot-var", "--replace", "--eof"}), 0, _E, _E, _E,
              frozenset({"-a", "--arg-file"})),
    "nohup": ("", _E, 0, _E, _E, _E, _E),
    "setsid": ("", _E, 0, _E, _E, _E, _E),
    "time": ("fo", frozenset({"--format", "--output"}), 0, _E, _E, _E, _E),
    "stdbuf": ("ioe", frozenset({"--input", "--output", "--error"}), 0, _E, _E, _E, _E),
    "ionice": ("cnpPu", frozenset({"--class", "--classdata", "--pid", "--pgid", "--uid"}), 0, _E,
               frozenset({"-p", "--pid", "-P", "--pgid", "-u", "--uid"}), _E, _E),
    "chrt": ("TPD", frozenset({"--sched-runtime", "--sched-period", "--sched-deadline"}), 1, _E,
             frozenset({"-p", "--pid", "-m", "--max"}), _E, _E),
    "taskset": ("", _E, 1, _E, frozenset({"-p", "--pid"}), _E, _E),
    "exec": ("a", _E, 0, _E, _E, _E, _E),
    "command": ("", _E, 0, _E, frozenset({"-v", "-V"}), _E, _E),
    "builtin": ("", _E, 0, _E, _E, _E, _E),
    "caffeinate": ("wt", _E, 0, _E, _E, _E, _E),
    "chroot": ("", frozenset({"--userspec", "--groups"}), 1, frozenset({"<positional>"}), _E, _E, _E),
    "busybox": ("", _E, 0, _E, _E, _E, _E),
    "wsl": ("du", frozenset({"--distribution", "--user", "--cd"}), 0, frozenset({"--cd"}),
            frozenset({"-l", "--list", "--install", "--shutdown", "--terminate", "-t", "--export", "--import",
                       "--unregister", "--set-default", "-s", "--status", "--update", "--mount", "--unmount",
                       "--set-version", "--set-default-version", "--help", "--version", "-v"}), _E, _E),
    # verification of audit round 2: wrappers seen at home (lock, trace, namespace, sandbox, unit, pty helpers)
    "flock": ("wE", frozenset({"--wait", "--timeout", "--conflict-exit-code"}), 1, _E, _E,
              frozenset({"-c", "--command"}), _E),
    "strace": ("abeEIoOpPsSuUX", frozenset({"--output", "--expr", "--trace", "--signal", "--status", "--attach",
                                            "--trace-path", "--string-limit", "--summary-sort-by", "--user",
                                            "--env", "--columns", "--const-print-style", "--decode-fds",
                                            "--quiet", "--trace-fds", "--abbrev", "--verbose", "--raw",
                                            "--read", "--write", "--inject", "--fault", "--kvm"}), 0, _E, _E, _E,
               _E),
    "ltrace": ("aAeEFlLnopsSuwx", frozenset({"--output", "--library", "--config"}), 0, _E, _E, _E, _E),
    "nsenter": ("tSG", frozenset({"--target", "--setuid", "--setgid"}), 0,
                frozenset({"-r", "-w", "--root", "--wd", "--wdns"}), _E, _E, _E),
    "systemd-run": ("upHME", frozenset({"--unit", "--property", "--host", "--machine", "--setenv",
                                        "--description", "--slice", "--uid", "--gid", "--nice",
                                        "--working-directory", "--service-type", "--on-active", "--on-boot",
                                        "--on-startup", "--on-unit-active", "--on-unit-inactive", "--on-calendar",
                                        "--timer-property", "--path-property", "--socket-property", "--json",
                                        "--background", "--shell-prompt-prefix"}), 0,
                    frozenset({"--working-directory"}), _E, _E, _E),
    "setpriv": ("", frozenset({"--reuid", "--regid", "--groups", "--inh-caps", "--ambient-caps", "--bounding-set",
                               "--securebits", "--pdeathsig", "--selinux-label", "--apparmor-profile",
                               "--landlock-access", "--landlock-rule", "--seccomp-filter", "--ptracer",
                               "--rlimit"}), 0, _E, frozenset({"-d", "--dump"}), _E, _E),
    "firejail": ("", _E, 0, frozenset({"--private", "--private-cwd"}), _E, _E, _E),
    "unbuffer": ("", _E, 0, _E, _E, _E, _E),
    "valgrind": ("", _E, 0, _E, _E, _E, _E),
    "torsocks": ("uapP", frozenset({"--user", "--pass", "--address", "--port"}), 0, _E, _E, _E, _E),
    "proxychains": ("f", _E, 0, _E, _E, _E, _E),
    "proxychains4": ("f", _E, 0, _E, _E, _E, _E),
    "numactl": ("cCmNpPi", frozenset({"--cpunodebind", "--physcpubind", "--membind", "--preferred",
                                      "--interleave", "--localalloc", "--weighted-interleave"}), 0, _E,
                frozenset({"-s", "--show", "-H", "--hardware"}), _E, _E),
    "prlimit": ("p", frozenset({"--pid"}), 0, _E, _E, _E, _E),
    "xvfb-run": ("efnpsw", frozenset({"--error-file", "--server-num", "--auth-file", "--xauth-protocol",
                                      "--server-args", "--wait"}), 0, _E, _E, _E, _E),
    "dbus-run-session": ("", frozenset({"--config-file", "--dbus-daemon"}), 0, _E, _E, _E, _E),
    "catchsegv": ("", _E, 0, _E, _E, _E, _E),
    "nocache": ("", _E, 0, _E, _E, _E, _E),
}
# programs whose arguments hold a command line (planned again; ``_scan_text_runner``), and launchers whose first
# operand is a Python script (``_scan_launcher``)
_TEXT_RUNNERS = frozenset({"trap", "watch", "su", "runuser", "sg", "script", "ssh", "alias"})
_LAUNCHERS = frozenset({"torchrun", "deepspeed", "accelerate"})
# tools whose "run" subcommand runs the rest as a command (their value options; cwd options)
_RUNNERS: Dict[str, _Wrap] = {
    "uv": ("pf", frozenset({"--with", "--with-editable", "--with-requirements", "--python", "--project", "--directory",
                            "--env-file", "--extra", "--group", "--only-group", "--no-group", "--no-extra", "--package",
                            "--index", "--default-index", "--index-url", "--extra-index-url", "--find-links",
                            "--config-file", "--cache-dir", "--prerelease", "--resolution", "--index-strategy",
                            "--keyring-provider", "--exclude-newer", "--link-mode", "--refresh-package",
                            "--reinstall-package", "--upgrade-package", "--no-binary-package", "--no-build-package",
                            "--python-platform", "--color", "--allow-insecure-host", "--config-setting"}), 0,
           frozenset({"--directory", "--project"}), _E, _E, frozenset({"--env-file", "--with-requirements"})),
    "poetry": ("PC", frozenset({"--project", "--directory"}), 0, frozenset({"-C", "--directory", "-P", "--project"}),
               _E, _E, _E),
    "pipenv": ("", _E, 0, _E, _E, _E, _E),
    "pdm": ("p", frozenset({"--project", "--venv"}), 0, frozenset({"-p", "--project"}), _E, _E, _E),
    "conda": ("np", frozenset({"--name", "--prefix", "--cwd"}), 0, frozenset({"--cwd"}), _E, _E, _E),
    "mamba": ("np", frozenset({"--name", "--prefix", "--cwd"}), 0, frozenset({"--cwd"}), _E, _E, _E),
    "micromamba": ("np", frozenset({"--name", "--prefix", "--cwd"}), 0, frozenset({"--cwd"}), _E, _E, _E),
}

# interpreters: short options (letter -> role), long options (name -> role), what the first operand is, subcommands.
# Roles: val (a value that is not a file: quiet), file (a value naming a file: listed when missing), rest (takes the
# rest of the word only), code (code text; no script follows), code+ (code text; a script may still follow), prog
# (awk/sed program text: operands are input files), script (the value is the script: bound), script! (the value
# is the script and options end), module (python -m), lmod (lua -l), cwd (changes the cwd), nested (the value is a
# command line: read again), nested+ (same, a script may still follow), shc (sh -c: the first operand is a command
# line), stdin (no script operand), stop (nothing more to read), safe (python -I/-P: the cwd is not on sys.path).
# Long names ending in "?" take a value only after "=".
_Spec = Dict[str, Any]
_SH_SPEC: _Spec = {"short": {"c": "shc", "s": "stdin", "o": "val", "O": "val"}, "plus": True,
                   "long": {"--rcfile": "file", "--init-file": "file"}}
_INTERP: Dict[str, _Spec] = {
    "sh": _SH_SPEC,
    "fish": {"short": {"c": "nested", "C": "nested+", "p": "file", "d": "val", "o": "file"},
             "long": {"--command": "nested", "--init-command": "nested+", "--profile": "file", "--debug": "val",
                      "--debug-output": "file", "--features": "val", "--profile-startup": "file"}},
    "python": {"short": {"c": "code", "m": "module", "W": "val", "X": "val", "Q": "val", "I": "safe", "P": "safe"},
               "long": {"--check-hash-based-pycs": "val"}},
    "perl": {"short": {"e": "code", "E": "code", "I": "file", **{c: "rest" for c in "xi0lCdDFMmV"}}, "code_last": True},
    "ruby": {"short": {"e": "code", "I": "file", "r": "file", "C": "cwd", "E": "val",
                       **{c: "rest" for c in "xiF0lKTW"}},
             "long": {"--encoding": "val", "--external-encoding": "val", "--internal-encoding": "val"},
             "code_last": True},
    "node": {"short": {"e": "code", "p": "code", "r": "file", "C": "val"},
             "long": {**{k: "file" for k in ("--require", "--import", "--loader", "--experimental-loader",
                                             "--env-file", "--env-file-if-exists", "--openssl-config",
                                             "--experimental-policy", "--experimental-config-file", "--icu-data-dir",
                                             "--redirect-warnings", "--localstorage-file")},
                      **{k: "val" for k in ("--conditions", "--inspect-port", "--debug-port", "--input-type",
                                            "--title", "--diagnostic-dir", "--tls-cipher-list", "--tls-keylog",
                                            "--unhandled-rejections", "--dns-result-order", "--heapsnapshot-signal",
                                            "--report-dir", "--report-directory", "--report-filename",
                                            "--report-signal", "--watch-path", "--policy-integrity",
                                            "--secure-heap", "--secure-heap-min", "--cpu-prof-dir",
                                            "--cpu-prof-name", "--cpu-prof-interval", "--heap-prof-dir",
                                            "--heap-prof-name", "--heap-prof-interval", "--test-reporter",
                                            "--test-reporter-destination", "--test-name-pattern",
                                            "--test-skip-pattern", "--test-concurrency", "--test-shard",
                                            "--test-timeout", "--disable-warning", "--max-http-header-size")},
                      "--eval": "code", "--print": "code", "--run": "stop"},
             "subs": {"inspect": "again"}},
    "deno": {"short": {"c": "file", "L": "val"},
             "long": {"--config": "file", "--import-map": "file", "--lock": "file", "--cert": "file",
                      "--location": "val", "--seed": "val", "--ext": "val", "--log-level": "val",
                      "--env-file?": "file", "--preload": "file", "--watch-exclude?": "val"},
             "subs": {**{k: "again" for k in ("run", "serve", "bench", "test", "compile", "check")}, "eval": "code",
                      **{k: "stop" for k in ("task", "repl", "install", "x", "add", "remove", "fmt", "lint",
                                             "upgrade", "init", "publish", "jupyter", "completions", "coverage",
                                             "types", "uninstall", "outdated", "clean", "info", "doc", "lsp",
                                             "vendor", "cache", "help", "approve-scripts")}}},
    "bun": {"short": {"e": "code", "p": "code", "r": "file", "c": "file", "d": "val", "l": "val"},
            "long": {"--cwd": "cwd", "--preload": "file", "--config": "file", "--env-file": "file",
                     "--tsconfig-override": "file", "--define": "val", "--loader": "val", "--main-fields": "val",
                     "--conditions": "val", "--port": "val", "--eval": "code", "--print": "code", "--shell": "val",
                     "--filter": "val", "--elide-lines": "val"},
            "subs": {"run": "again", **{k: "stop" for k in (
                "test", "x", "install", "i", "add", "a", "remove", "rm", "build", "pm", "create", "c", "init",
                "upgrade", "link", "unlink", "update", "outdated", "publish", "patch", "audit", "info", "exec", "repl",
                "help")}}},
    "rscript": {"short": {"e": "code"}, "long": {}, "code_last": True},
    "r": {"short": {"f": "script!", "e": "code"}, "long": {"--file?": "script!"}, "first": "none",
          "subs": {"CMD": "rcmd"}},
    "php": {"short": {"c": "file", "d": "val", "f": "script!", "r": "code", "B": "code+", "R": "code", "F": "script",
                      "E": "code+", "z": "file", "t": "file", "S": "val"},
            "long": {"--php-ini": "file", "--define": "val", "--file": "script!", "--run": "code",
                     "--process-begin": "code+", "--process-code": "code", "--process-file": "script",
                     "--process-end": "code+", "--zend-extension": "file"},
            "code_last": True},
    "lua": {"short": {"e": "code+", "l": "lmod", "b": "stop", "j": "val", "O": "rest"}},
    "awk": {"short": {"f": "script", "E": "script", "i": "script", "v": "val", "F": "val", "l": "file"},
            "long": {"--file": "script", "--exec": "script", "--include": "script", "--assign": "val",
                     "--field-separator": "val", "--load": "file", "--source": "prog"},
            "first": "prog"},
    "sed": {"short": {"f": "script", "e": "prog", "l": "val", "i": "rest"},
            "long": {"--file": "script", "--expression": "prog", "--line-length": "val", "--in-place?": "val"},
            "first": "prog"},
    "osascript": {"short": {"e": "code", "l": "val", "s": "val"}, "code_last": True},
    "tclsh": {"short": {}},
}
_PY_RE = re.compile(r"^(python|pypy|ipython)[0-9.]*[wmd]?$|^pyw?$")


def _interp_kind(base: str) -> Optional[str]:
    if re.match(r"^((ba|da|z|k|mk|a|ya|r|lk|ok|pdk|po|c|tc)?sh|ksh93)$", base):
        return "sh"
    if _PY_RE.match(base):
        return "python"
    if re.match(r"^perl[0-9.]*$", base):
        return "perl"
    if re.match(r"^(j|truffle)?ruby[0-9.]*$", base):
        return "ruby"
    if base in ("node", "nodejs"):
        return "node"
    if re.match(r"^php[0-9.]*(-cli)?$", base):
        return "php"
    if re.match(r"^lua(jit|[0-9.]*)$", base):
        return "lua"
    if base in ("awk", "gawk", "mawk", "nawk"):
        return "awk"
    if base in ("sed", "gsed"):
        return "sed"
    if re.match(r"^(tclsh|wish)[0-9.]*$", base):
        return "tclsh"
    if base in ("pwsh", "pwsh-preview", "powershell", "powershell_ise"):
        return "pwsh" if base.startswith("pwsh") else "powershell"
    if base in _INTERP or base == "cmd":
        return base
    return None


def _value(words: List[_W], i: int, rest: str) -> Tuple[Optional[_W], int]:
    """(value word, index after it) of an option at ``words[i]``: the attached rest, else the next word."""
    if rest:
        return _sub(rest, words[i]), i + 1
    if i + 1 < len(words):
        return words[i + 1], i + 2
    return None, i + 1


def _skip_wrapper(spec: _Wrap, base: str, words: List[_W], i: int, roles: _Roles) -> int:
    """Index of the command word a wrapper at ``words[i - 1]`` runs (``len(words)`` when none)."""
    short, longs, npos, cwd, stops, splits, files = spec
    n = len(words)
    opts = True
    while i < n:
        w = words[i]
        t = w.text
        if t == "--" and opts:
            opts = False
            i += 1
            continue
        if base == "env" and _NAME_ASSIGN_RE.match(t):
            _assignment(w, roles)
            i += 1
            continue
        if opts and t.startswith("--") and len(t) > 2:
            name, eq, val = t.partition("=")
            if name in stops:
                return n
            if name in cwd:
                roles.cwd = True
            if name in splits or name in longs:
                if eq:
                    v: Optional[_W] = _sub(val, w)
                    i += 1
                else:
                    v, i = _value(words, i, "")
                if v is not None:
                    if name in splits:
                        _split_text(base, v, words, i, roles)
                        return n
                    if name not in files:
                        roles.hush(v)
                continue
            i += 1
            continue
        if opts and t.startswith("-") and len(t) > 1:
            j = 1
            nxt = i + 1
            while j < len(t):
                ch = t[j]
                if "-" + ch in stops:
                    return n
                if "-" + ch in cwd:
                    roles.cwd = True
                if ch in short or "-" + ch in splits:
                    v, nxt = _value(words, i, t[j + 1:])
                    if v is not None:
                        if "-" + ch in splits:
                            _split_text(base, v, words, nxt, roles)
                            return n
                        if "-" + ch not in files:
                            roles.hush(v)
                    break
                j += 1
            i = nxt
            continue
        if npos > 0:
            roles.hush(w, paths=True)
            if "<positional>" in cwd:
                roles.cwd = True
            npos -= 1
            i += 1
            continue
        return i
    return n


def _split_text(base: str, v: _W, words: List[_W], i: int, roles: _Roles) -> None:
    """A wrapper option whose value is a command line (``env -S``, ``flock -c``): planned again; ``env -S`` passes
    the words after it on as arguments, so they are part of that line (verification of audit round 2)."""
    roles.hush(v)
    text = v.text
    if base == "env" and i < len(words):
        for x in words[i:]:
            roles.hush(x)
        text += " " + " ".join(shlex.quote(x.text) for x in words[i:])
    roles.nest(text, "bash")


def _assignment(w: _W, roles: _Roles) -> None:
    """A NAME=VALUE word before a command: the value is quiet, unless NAME loads a file (BASH_ENV, …: bound)."""
    k, _eq, v = w.text.partition("=")
    w.quiet = True
    roles.hush(w.text)
    for _key, x in env_file_values({k: v}):
        if "$" in x or "`" in x:
            roles.unbound.append(f"{k}={x} {UNBOUND_VARS}")
        else:
            roles.op(x)
    if not env_file_values({k: v}):
        w.vquiet = True
        roles.hush(v)


def _module_files(mod: str, roles: _Roles) -> None:
    """Files in the cwd that ``python -m mod`` would run instead of the installed module (bound quietly)."""
    parts = [p for p in mod.split(".") if p]
    if not parts or not all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", p) for p in parts):
        return
    roles.extra(parts[0] + ".py")
    roles.extra(parts[0] + "/__init__.py")
    if len(parts) == 1:
        roles.extra(parts[0] + "/__main__.py")
    else:
        roles.extra("/".join(parts) + ".py")
    # sourceless modules (verification of audit round 2): Python runs a .pyc in place of a missing .py
    roles.extra(parts[0] + ".pyc")
    roles.extra(parts[0] + "/__init__.pyc")
    roles.extra("/".join(parts) + ("/__main__.pyc" if len(parts) == 1 else ".pyc"))


def _scan_interp(kind: str, words: List[_W], i: int, roles: _Roles) -> None:
    """Read an interpreter's options from ``words[i]`` on and mark its script operand."""
    if kind in ("pwsh", "powershell"):
        _scan_pwsh(kind, words, i, roles)
        return
    if kind == "cmd":
        _scan_cmd(words, i, roles)
        return
    spec = _INTERP[kind]
    short: Dict[str, str] = spec.get("short") or {}
    longs: Dict[str, str] = spec.get("long") or {}
    first = spec.get("first", "script")
    n = len(words)
    mode = ""          # "" script expected, "shc" first operand is a command line, "none" no script operand
    safe = False
    program = False
    subbed = False
    stdin = False
    while True:
        while i < n:
            w = words[i]
            t = w.text
            if t == "--":
                i += 1
                break
            if t == "-":
                roles.stdin_kind = kind
                return
            role: Optional[str] = None
            v: Optional[_W] = None
            if t.startswith("--") and len(t) > 2:
                name, eq, val = t.partition("=")
                role = longs.get(name)
                if role is None and eq:
                    role = longs.get(name + "?")
                if role is None:
                    i += 1
                    continue
                if eq:
                    v = _sub(val, w)
                    i += 1
                else:
                    v, i = _value(words, i, "")
            elif (t.startswith("-") or (spec.get("plus") and t.startswith("+"))) and len(t) > 1:
                j = 1
                nxt = i + 1
                while j < len(t):
                    r = short.get(t[j])
                    if r is None:
                        j += 1
                        continue
                    if r == "rest":
                        break
                    if r in ("shc", "stdin", "safe"):
                        if r == "safe":
                            safe = True
                        else:
                            mode = "shc" if r == "shc" else "none"
                            stdin = r == "stdin"
                        j += 1
                        continue
                    if r == "stop":
                        return
                    role = r
                    v, nxt = _value(words, i, t[j + 1:])
                    break
                i = nxt
            else:
                break
            if role is None or v is None:
                if role in ("code", "module", "script!", "nested"):
                    return
                continue
            if role == "val":
                roles.hush(v)
            elif role == "file":
                pass
            elif role == "cwd":
                roles.hush(v)
                roles.cwd = True
            elif role in ("code", "code+", "prog"):
                roles.hush(v)
                if role == "prog":
                    program = True
                if role == "code":
                    if not spec.get("code_last"):
                        return
                    mode = "none"
            elif role in ("script", "script!"):
                roles.op(v)
                program = True
                mode = "none"
                if role == "script!":
                    return
            elif role == "module":
                roles.hush(v)
                if not safe:
                    _module_files(v.text, roles)
                return
            elif role == "lmod":
                roles.hush(v)
                if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", v.text):
                    roles.extra(v.text.replace(".", "/") + ".lua")
            elif role in ("nested", "nested+"):
                roles.hush(v)
                roles.nest(v.text, "bash")
                if role == "nested":
                    return
        if i >= n:
            if (mode == "" or stdin) and first == "script":
                roles.stdin_kind = kind      # the program text comes from stdin (a pipe or here-string)
            return
        w = words[i]
        sub = (spec.get("subs") or {}).get(w.text) if not subbed else None
        if sub is not None and mode == "":
            roles.hush(w)
            subbed = True
            if sub == "again":
                i += 1
                continue
            if sub == "code" and i + 1 < n:
                roles.hush(words[i + 1])
            if sub == "rcmd":     # R CMD BATCH [options] file
                if i + 1 < n and words[i + 1].text == "BATCH":
                    roles.hush(words[i + 1])
                    k = i + 2
                    while k < n and words[k].text.startswith("-"):
                        k += 1
                    if k < n:
                        roles.op(words[k])
            return
        break
    if mode == "shc":
        roles.hush(w)
        roles.nest(w.text, "bash")
    elif mode == "none" or first == "none":
        if stdin:
            roles.stdin_kind = kind
        return
    elif first == "prog":
        if not program:
            roles.hush(w)
    else:
        roles.op(w)
        _entry_files(kind, w.text, roles)


_NODE_EXT = (".js", ".mjs", ".cjs", ".node", ".json", ".ts", ".mts", ".cts")


def _entry_files(kind: str, name: str, roles: _Roles) -> None:
    """Files an interpreter runs for a script operand besides the name itself (bound quietly): a folder operand runs
    ``name/__main__.py`` or ``name/__main__.pyc`` (python) or ``name/index.js``/``index.node``/``package.json``
    "main" (node), and node also tries ``name.js``/``name.node``. Such an operand that is a folder at review is
    named in the header (``bind_files``)."""
    if not name or name == "-" or "$" in name:
        return
    if kind == "python" and not name.lower().endswith((".py", ".pyw", ".pyc", ".pyz", ".zip")):
        roles.entry_ops.add(name)
        roles.extra(name.rstrip("/\\") + "/__main__.py")
        roles.extra(name.rstrip("/\\") + "/__main__.pyc")
    elif kind == "node" and not name.lower().endswith(_NODE_EXT):
        roles.entry_ops.add(name)
        base = name.rstrip("/\\")
        for x in (base + ".js", base + ".node", base + "/index.js", base + "/index.node", base + "/package.json"):
            roles.extra(x)


_PWSH_VALUES = ("executionpolicy", "windowstyle", "configurationname", "configurationfile", "custompipename",
                "inputformat", "outputformat", "settingsfile", "psconsolefile", "workingdirectory", "version")


def _scan_pwsh(kind: str, words: List[_W], i: int, roles: _Roles) -> None:
    """pwsh/powershell: ``-File script`` (or pwsh's first operand) is the script; ``-Command`` text (or Windows
    PowerShell's first operand) is read again; ``-EncodedCommand`` cannot be read. Parameter names are
    case-insensitive and may be shortened."""
    n = len(words)
    while i < n:
        w = words[i]
        t = w.text
        if t == "--":
            i += 1
            break
        if not (t.startswith(("-", "/")) and len(t) > 1):
            break
        p = t.lstrip("-/").lower()
        p, _c, attached = p.partition(":")
        if p in ("f", "file") or (len(p) >= 2 and "file".startswith(p)):
            if i + 1 < n:
                roles.op(words[i + 1])
            return
        if p in ("c", "command") or (len(p) >= 2 and "command".startswith(p)):
            for x in words[i + 1:]:
                roles.hush(x)
            rest = " ".join(x.text for x in words[i + 1:])
            if rest.strip() and rest.strip() != "-":
                roles.nest(rest, "powershell")
            return
        if p.startswith("ex") or p == "ep":
            pass
        elif p in ("e", "ec") or (len(p) >= 2 and "encodedcommand".startswith(p)):
            roles.unbound.append(f"{t} (encoded command; not read or bound)")
            for x in words[i + 1:i + 2]:
                roles.hush(x)
            return
        if p in ("wd",) or (len(p) >= 2 and "workingdirectory".startswith(p)):
            roles.cwd = True
        takes = p in ("ep", "wd", "w", "config", "if", "of", "inp", "settings") or p.startswith("ex") or any(
            len(p) >= 2 and full.startswith(p) for full in _PWSH_VALUES)
        if p in ("v", "version") and kind == "pwsh":
            takes = False
        if takes and not attached and i + 1 < n:
            roles.hush(words[i + 1])
            i += 2
            continue
        i += 1
    if i >= n:
        return
    if kind == "pwsh":
        roles.op(words[i])
    else:
        for x in words[i:]:
            roles.hush(x)
        roles.nest(" ".join(x.text for x in words[i:]), "powershell")


def _scan_cmd(words: List[_W], i: int, roles: _Roles) -> None:
    """``cmd /c text`` / ``/k text``: the text is read again as a cmd line (its command word is a script there)."""
    n = len(words)
    while i < n:
        t = words[i].text
        if t.lower() in ("/c", "/k", "/r"):
            for x in words[i + 1:]:
                roles.hush(x)
            rest = " ".join(x.text for x in words[i + 1:])
            if rest.strip():
                roles.nest(rest, "cmd")
            return
        if t[:1] != "/":
            return
        i += 1


def _scan_start_process(words: List[_W], i: int, roles: _Roles) -> None:
    """PowerShell ``Start-Process [-FilePath] prog [[-ArgumentList] args]``: an interpreter with its arguments is
    read again as a command line; another program (Windows looks in the cwd too) binds name.com/.exe/.bat/.cmd."""
    n = len(words)
    prog: Optional[_W] = None
    args: List[str] = []
    while i < n:
        t = words[i].text
        if t.startswith("-") and len(t) > 1:
            p = t[1:].lower()
            v = words[i + 1] if i + 1 < n else None
            if p in ("wait", "nonewwindow", "passthru", "usenewenvironment", "loaduserprofile", "noprofile",
                     "useshellexecute"):
                i += 1
                continue
            if v is not None and "filepath".startswith(p):
                prog = v
            elif v is not None and ("argumentlist".startswith(p) or p == "args"):
                args.append(v.text.replace(",", " "))
            elif v is not None and len(p) >= 2 and "workingdirectory".startswith(p):
                roles.cwd = True
                roles.hush(v)
            elif v is not None and "redirectstandardinput".startswith(p) and len(p) >= 12:
                roles.op(v)
            elif v is not None:
                roles.hush(v)
            i += 2
            continue
        if prog is None:
            prog = words[i]
        else:
            args.append(t)
        i += 1
    if prog is None:
        return
    roles.hush(prog)
    if _interp_kind(_cmd_base(prog.text)) is None:
        if not os.path.isabs(prog.text) and ":" not in prog.text:
            roles.op(prog, pathext=True)
        return
    roles.nest(" ".join([prog.text] + args), "powershell")


def _cluster_value(words: List[_W], i: int, letters: str, valued: str) -> Tuple[Optional[str], Optional[_W], int]:
    """Short-option cluster at ``words[i]``: (letter of ``letters`` found, its value, index after); a letter of
    ``valued`` takes the rest of the cluster or the next word as its value."""
    t = words[i].text
    for j in range(1, len(t)):
        ch = t[j]
        if ch in letters or ch in valued:
            v, nxt = _value(words, i, t[j + 1:])
            return (ch if ch in letters else None), v, nxt
    return None, None, i + 1


def _scan_text_runner(base: str, words: List[_W], i: int, roles: _Roles) -> Optional[int]:
    """Programs whose arguments hold a command line (verification of audit round 2): ``trap 'cmd' SIG``, ``watch
    cmd…``, ``su/runuser/sg -c cmd``, ``script -c cmd``, ``alias n='cmd'`` (planned again); ``ssh host cmd`` (a
    header item: it runs elsewhere). Returns the index of a command word to read on (``runuser -u u cmd``), else
    None."""
    n = len(words)
    if base == "trap":
        k = i
        while k < n and words[k].text in ("--", "-p", "-l", "-P"):
            roles.hush(words[k])
            k += 1
        roles.mark_late()
        if k + 1 < n and words[k].text != "-":
            roles.nest(words[k].text, "bash", late=True)
        for x in words[k:]:
            roles.hush(x)
        return None
    if base == "alias":
        roles.mark_late()
        for x in words[i:]:
            roles.hush(x)
            _name, eq, val = x.text.partition("=")
            if eq and val.strip():
                roles.nest(val, "bash", late=True)
        return None
    if base == "watch":
        k = i
        while k < n and words[k].text.startswith("-") and len(words[k].text) > 1:
            t = words[k].text
            roles.hush(words[k])
            if t in ("-x", "--exec"):
                return k + 1 if k + 1 < n else None
            if t in ("-n", "--interval", "-q", "--equexit") and k + 1 < n:
                roles.hush(words[k + 1])
                k += 2
                continue
            k += 1
        for x in words[k:]:
            roles.hush(x)
        if k < n:
            roles.nest(" ".join(x.text for x in words[k:]), "bash")
        return None
    if base == "ssh":
        k = i
        while k < n and words[k].text.startswith("-") and len(words[k].text) > 1:
            roles.hush(words[k])
            t = words[k].text
            if t == "--":
                k += 1
                break
            if t[-1] in "BbcDEeFIiJLlmOoPpQRSWw" and len(t) == 2 and k + 1 < n:
                roles.hush(words[k + 1])
                k += 2
                continue
            k += 1
        for x in words[k:]:
            roles.hush(x)
        if k + 1 < n:
            roles.unbound.append(" ".join(x.text for x in words[k + 1:]) + " (remote command; not read or bound)")
        return None
    # su / runuser / sg / script: -c CMD, --command[=]CMD, -cCMD inside a cluster (script -qc CMD)
    k = i
    positional = 0
    while k < n:
        w = words[k]
        t = w.text
        if t == "--":
            roles.hush(w)
            if base == "runuser" and k + 1 < n:
                return k + 1
            k += 1
            continue
        if t in ("-", "-l", "--login") and base in ("su", "runuser", "sg"):
            roles.cwd = True            # a login shell starts in the user's home
            roles.hush(w)
            k += 1
            continue
        if t.startswith("--command"):
            roles.hush(w)
            _name, eq, val = t.partition("=")
            v = _sub(val, w) if eq else (words[k + 1] if k + 1 < n else None)
            if v is not None:
                roles.hush(v)
                roles.nest(v.text, "bash")
            return None
        if t.startswith("-") and not t.startswith("--") and len(t) > 1:
            roles.hush(w)
            valued = {"su": "sgGw", "runuser": "sgGuw", "sg": "", "script": "EIOBTmo"}.get(base, "")
            ch, v, nxt = _cluster_value(words, k, "c", valued)
            if ch == "c" and v is not None:
                roles.hush(v)
                roles.nest(v.text, "bash")
                return None
            if v is not None:
                roles.hush(v)
                if base == "runuser" and t == "-u":
                    # runuser -u user [--] command args: a wrapper
                    j = nxt
                    if j < n and words[j].text == "--":
                        roles.hush(words[j])
                        j += 1
                    if j < n and not words[j].text.startswith("-"):
                        return j
            k = nxt
            continue
        if t.startswith("--"):
            roles.hush(w)
            k += 1
            continue
        roles.hush(w)
        positional += 1
        if base == "sg" and positional == 2:     # sg group command…
            roles.nest(" ".join(x.text for x in words[k:]), "bash")
            for x in words[k:]:
                roles.hush(x)
            return None
        k += 1
    return None


# launchers whose first operand is a Python script (``--no-python``: a command; ``-m``: a module)
_LAUNCH_VALUES = {
    "torchrun": frozenset({"--nnodes", "--nproc-per-node", "--rdzv-backend", "--rdzv-endpoint", "--rdzv-id",
                           "--rdzv-conf", "--max-restarts", "--monitor-interval", "--start-method", "--role",
                           "--master-addr", "--master-port", "--node-rank", "--local-addr", "--log-dir", "-r",
                           "--redirects", "-t", "--tee", "--local-ranks-filter", "--logs-specs",
                           "--signals-to-handle", "--event-log-handler", "--numa-binding"}),
    "deepspeed": frozenset({"-H", "--hostfile", "-i", "--include", "-e", "--exclude", "--num-nodes", "--num-gpus",
                            "--min-elastic-nodes", "--max-elastic-nodes", "--master-port", "--master-addr",
                            "--node-rank", "--launcher", "--launcher-args", "--ssh-port", "--comment",
                            "--bind-core-list", "--venv-script", "--autotuning", "--enable-each-rank-log"}),
}
_ACCELERATE_FLAGS = frozenset({
    "--cpu", "--multi-gpu", "--tpu", "--use-deepspeed", "--use-fsdp", "--use-megatron-lm", "--use-xpu", "--debug",
    "-q", "--quiet", "--same-network", "--enable-cpu-affinity", "--downcast-bf16", "--tpu-use-cluster",
    "--tpu-use-sudo", "--ipex", "--use-cpu", "--no-python", "-m", "--module", "-h", "--help",
    "--deepspeed-moe-layer-cls-names", "--fsdp-use-orig-params", "--fsdp-cpu-ram-efficient-loading"})


def _scan_launcher(base: str, words: List[_W], i: int, roles: _Roles) -> Optional[int]:
    """torchrun / deepspeed / ``accelerate launch``: options skipped with their values, then the Python script
    (bound like ``python script``), a module (``-m``) or, with ``--no-python``, a command (its index returned)."""
    n = len(words)
    if base == "accelerate":
        if i >= n or words[i].text != "launch":
            return None
        roles.hush(words[i])
        i += 1
    module = nopy = False
    while i < n:
        w = words[i]
        t = w.text
        if t == "--":
            roles.hush(w)
            i += 1
            break
        if not (t.startswith("-") and len(t) > 1):
            break
        roles.hush(w)
        name, eq, val = t.partition("=")
        if name.startswith("--"):
            name = "--" + name[2:].replace("_", "-")
        if name in ("-m", "--module"):
            module = True
        elif name == "--no-python":
            nopy = True
        if eq:
            roles.hush(_sub(val, w))
            i += 1
            continue
        takes = (name not in _ACCELERATE_FLAGS) if base == "accelerate" else name in _LAUNCH_VALUES.get(base, _E)
        if takes and i + 1 < n:
            roles.hush(words[i + 1])
            i += 2
            continue
        i += 1
    if i >= n:
        return None
    w = words[i]
    if nopy:
        return i
    roles.hush(w)
    if module:
        _module_files(w.text, roles)
        return None
    roles.op(w)
    _entry_files("python", w.text, roles)
    return None


def _uv_run(words: List[_W], k: int, roles: _Roles) -> Optional[int]:
    """``uv run [options] target …`` (verification of audit round 2): ``-s/--script/--gui-script`` make the target
    a Python script and ``-m/--module`` a module; a target that is not a known program may still be a script or a
    folder uv runs with Python (its file and entry files are bound quietly). Returns the index of a program to read
    on."""
    n = len(words)
    spec = _RUNNERS["uv"]
    script = module = False
    j = k
    while j < n:
        t = words[j].text
        if t == "--" or not t.startswith("-") or t == "-":
            break
        if t in ("-m", "--module"):
            module = True
        elif t in ("-s", "--script", "--gui-script"):
            script = True
        elif "=" not in t and (t in spec[1] or (len(t) == 2 and t[1] in spec[0])):
            j += 1
        j += 1
    i = _skip_wrapper(spec, "uv", words, k, roles)
    if i >= n:
        return None
    target = words[i]
    tb = _cmd_base(target.text)
    if module:
        roles.hush(target)
        _module_files(target.text, roles)
        return None
    if script or target.text.lower().endswith((".py", ".pyw")):
        roles.hush(target)
        roles.op(target)
        _entry_files("python", target.text, roles)
        return None
    if _interp_kind(tb) or tb in _WRAPPERS or tb in _RUNNERS or tb in _TEXT_RUNNERS or tb in _LAUNCHERS:
        return i
    roles.hush(target)
    if "$" not in target.text:
        roles.extra(target.text)
        _entry_files("python", target.text, roles)
    return None


_CMD_TEXT_START = frozenset({"eval", "source", ".", "exec", "command", "cd", "iex", "invoke-expression"})


def _looks_like_command(text: str) -> bool:
    """``text`` has spaces and starts with a known interpreter, wrapper, runner or eval/source/cd: a command line
    handed to some program (``tmux new -d 'bash x'``, ``git commit -m "bash is fun"``)."""
    parts = text.split()
    if len(parts) < 2:
        return False
    base = _cmd_base(parts[0].strip("\"'"))
    return bool(_interp_kind(base) or base in _WRAPPERS or base in _RUNNERS or base in _TEXT_RUNNERS
                or base in _LAUNCHERS or base in _CMD_TEXT_START)


def _scan_command(words: List[_W], roles: _Roles, dialect: str) -> Optional[int]:
    """One simple command (redirect targets removed): assignments, keywords, wrappers, then the program. Returns
    the index of the program word (None when there is none)."""
    n = len(words)
    i = 0
    while i < n:
        t = words[i].text
        if dialect == "posix" and t in _SH_KEYWORDS:
            roles.hush(words[i])
            if t in ("while", "until", "function", "coproc"):
                roles.mark_late()
            i += 1
            continue
        if dialect == "posix" and _NAME_ASSIGN_RE.match(t):
            _assignment(words[i], roles)
            i += 1
            continue
        break
    guard = 0
    while i < n and guard < 16:
        guard += 1
        w = words[i]
        base = _cmd_base(w.text)
        if dialect == "posix":
            if None in w.spellings or "`" in w.text:
                # $cmd, $(…), `…` as the program: it may be cd or eval (verification of audit round 2)
                roles.mark_cwd()
                roles.hush(w)
                return i
            if base in CWD_CHANGERS:
                roles.mark_cwd()
                for x in words[i:]:
                    roles.hush(x)
                return i
            if base in ("for", "select", "case"):
                if base != "case":
                    roles.mark_late()
                for x in words[i:]:
                    roles.hush(x)
                return i
            if base == "eval":
                # eval joins its arguments and runs them as a command line: planned again (verification)
                roles.mark_cwd()
                for x in words[i:]:
                    roles.hush(x)
                roles.nest(" ".join(x.text for x in words[i + 1:]), "bash")
                return i
        if dialect == "pwsh" and base in ("iex", "invoke-expression"):
            for x in words[i:]:
                roles.hush(x)
            rest = [x.text for x in words[i + 1:] if x.text.lower() not in ("-command", "-c")]
            roles.nest(" ".join(rest), "powershell")
            return i
        if dialect == "cmd":
            if base in ("call", "start"):
                roles.hush(w)
                k = i + 1
                while k < n and (not words[k].text or words[k].text.startswith("/")):
                    k += 1       # start "title" /b /wait …: the empty title and the switches
                if k < n and not words[k].text.startswith(":"):
                    roles.op(words[k], pathext=True)
                    if base == "start":
                        i = k
                        continue
                return i
            if base not in _CMD_BUILTINS and not os.path.isabs(w.text) and ":" not in w.text:
                if "." in base and not _interp_kind(base):
                    roles.op(w)       # run3.bat: the script itself
                    return i
                roles.op(w, pathext=True)   # npm, python, run3: cmd tries name + PATHEXT in the cwd first
        if dialect == "pwsh" and base in ("start-process", "saps", "start"):
            roles.hush(w)
            _scan_start_process(words, i + 1, roles)
            return i
        roles.hush(w)
        if dialect == "posix" and base in ("source", ".") and w.text in ("source", "."):
            roles.mark_cwd()        # the sourced file may change the cwd for what follows; its own operand is bound
            k = i + 1
            while k < n and words[k].text.startswith("-"):
                k += 1
            if k < n:
                roles.op(words[k])
            return i
        if base in _WRAPPERS:
            if base == "systemd-run" and not any(x.text in ("-d", "--same-dir", "--scope") for x in words[i + 1:]):
                roles.cwd = True      # a transient service starts in / (or the user's home)
            i = _skip_wrapper(_WRAPPERS[base], base, words, i + 1, roles)
            continue
        if base in _TEXT_RUNNERS:
            k2 = _scan_text_runner(base, words, i + 1, roles)
            if k2 is None:
                return i
            i = k2
            continue
        if base in _LAUNCHERS:
            k2 = _scan_launcher(base, words, i + 1, roles)
            if k2 is None:
                return i
            i = k2
            continue
        if base in _RUNNERS:
            if i + 1 < n and words[i + 1].text == "run":
                roles.hush(words[i + 1])
                if base == "uv":
                    k2 = _uv_run(words, i + 2, roles)
                    if k2 is None:
                        return i
                    i = k2
                    continue
                i = _skip_wrapper(_RUNNERS[base], base, words, i + 2, roles)
                continue
            return i
        kind = _interp_kind(base)
        if kind is not None:
            _scan_interp(kind, words, i + 1, roles)
        elif dialect == "pwsh" and ("/" in w.text or "\\" in w.text) and "." not in base and "$" not in w.text:
            # .\run3 / & .\run3: PowerShell runs run3.ps1, then run3 + each PATHEXT extension (verification)
            for ext in (".ps1",) + _pathext():
                roles.extra(w.text + ext)
        return i
    return None


def _win_segments(text: str, dialect: str) -> List[List[_W]]:
    """Simple commands of a PowerShell/cmd line: split on ``; & | ( ) { }`` and newlines outside quotes (``"`` for
    cmd, ``"`` and ``'`` for PowerShell); a word after ``<``/``>`` is a redirect target."""
    quotes = '"' if dialect == "cmd" else "\"'"
    segs: List[List[_W]] = [[]]
    cur: Optional[List[str]] = None
    q = ""
    redir = ""

    def end() -> None:
        nonlocal cur, redir
        if cur is not None:
            t = "".join(cur)
            segs[-1].append(_W(t, _plain_spellings(t), redir, seg=len(segs) - 1))
            redir = ""
            cur = None

    for c in text:
        if q:
            if c == q:
                q = ""
            else:
                cur.append(c)  # type: ignore[union-attr]
            continue
        if c in quotes:
            q = c
            if cur is None:
                cur = []
            continue
        if c in " \t\r":
            end()
            continue
        if c in ";&|(){}\n":
            end()
            if segs[-1]:
                segs.append([])
            continue
        if c in "<>":
            end()
            redir = "in" if c == "<" else "out"
            continue
        if cur is None:
            cur = []
        cur.append(c)
    end()
    return [s for s in segs if s]


def _posix_segments(text: str) -> Tuple[List[List[_W]], List[bool]]:
    """(simple commands of a POSIX shell line, whether each one's output is piped into the next): ``_posix_tokens``
    words, split at operators and newlines; a word after ``<``/``>`` and a digit word right before one are
    redirections."""
    segs: List[List[_W]] = [[]]
    pipes: List[bool] = []
    for kind, val in _posix_scan(text):
        if kind == "w":
            chars, redir = val
            t = "".join(c for c, _q in chars)
            segs[-1].append(_W(t, _shell_spellings(chars), redir, seg=len(segs) - 1))
        elif segs[-1]:
            pipes.append(val[:1] == "|" and val[1:2] != "|")
            segs.append([])
    segs = [s for s in segs if s]
    pipes = (pipes + [False] * len(segs))[:len(segs)]
    return segs, pipes


def _substitution_inputs(sub: str, roles: _Roles) -> None:
    """``$(cat run3)``: the file a substitution prints becomes part of the command line (``bash -c "$(cat x)"``,
    ``eval "$(cat x)"``): bound like a script operand."""
    try:
        segs, _p = _posix_segments(sub)
    except ValueError:
        return
    for seg in segs:
        plain = [x for x in seg if not x.redir]
        if plain and _cmd_base(plain[0].text) in ("cat", "tac", "head", "tail"):
            for a in plain[1:]:
                if not a.text.startswith("-"):
                    roles.op(_W(a.text, a.spellings, seg=roles.seg))


def _substitutions(text: str) -> List[str]:
    """The command texts of the ``$(…)``/`` `…` `` substitutions in a word."""
    out: List[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c == "`" or (c == "$" and text[i + 1:i + 2] == "(" and text[i + 2:i + 3] != "("):
            j = _subst_end(text, i)
            if j < 0:
                break
            out.append(text[i + 1:j - 1] if c == "`" else text[i + 2:j - 1])
            i = j
            continue
        i += 2 if c == "\\" else 1
    return out


def _scan_commands(argv_or_command: Union[List[str], str], *, shell: Optional[str], os_name: Optional[str]
                   ) -> _Roles:
    """Roles of the words of a command (argv or shell line)."""
    roles = _Roles()
    pipes: List[bool] = []
    if isinstance(argv_or_command, (list, tuple)):
        segs = [[_W(str(x), _plain_spellings(str(x))) for x in argv_or_command]]
        dialect = "argv"
    elif _windows_shell(shell, os_name):
        dialect = "cmd" if shell == "cmd" else "pwsh"
        segs = _win_segments(str(argv_or_command), dialect)
    else:
        dialect = "posix"
        try:
            segs, pipes = _posix_segments(str(argv_or_command))
        except ValueError:
            dialect = "pwsh-fallback"
            segs = _win_segments(str(argv_or_command), "pwsh")
    roles.dialect = dialect
    nest_dialect = {"cmd": "cmd", "pwsh": "powershell"}.get(dialect, "bash")
    info: List[Tuple[List[_W], Optional[int], Optional[str]]] = []
    for s, seg in enumerate(segs):
        roles.seg = s
        roles.stdin_kind = None
        roles.words.extend(seg)
        for w in seg:
            if w.redir == "in":       # bash < run3: an input file is what the program reads (and runs)
                roles.op(w)
            elif w.redir in ("here", "fd", "out"):
                roles.hush(w)         # output targets are written, not run: never a header item (verification)
        plain = [w for w in seg if not w.redir]
        prog = _scan_command(plain, roles, "pwsh" if dialect == "pwsh-fallback" else dialect)
        for w in seg:
            if w.redir == "herestr":  # bash <<< 'cmd': the here-string is the script a shell reads from stdin
                roles.hush(w)
                if roles.stdin_kind in ("sh", "fish"):
                    roles.nest(w.text, "bash")
        info.append((plain, prog, roles.stdin_kind))
        # a word holding a live command substitution runs it here: its text is planned again
        if dialect == "posix":
            for w in seg:
                if None in w.spellings and ("$(" in w.text or "`" in w.text):
                    for sub in _substitutions(w.text):
                        roles.nest(sub, "bash")
                        _substitution_inputs(sub, roles)
        # an argument that looks like a command line (trap/tmux/ssh-style text, or a commit message): planned
        # again late and quietly (its operands bound, nothing listed)
        for k, w in enumerate(plain):
            if k == prog or w.quiet:
                continue
            for c in [w.text] + ([w.text.split("=", 1)[1]] if "=" in w.text and not w.vquiet else []):
                if _looks_like_command(c):
                    roles.nest(c, nest_dialect, late=True, quiet=True)
    # echo 'cmd' | sh, printf 'cmd\n' | bash, cat run3 | sh: the piped text is the script (verification)
    for s in range(len(info) - 1):
        if not (s < len(pipes) and pipes[s]) or info[s + 1][2] not in ("sh", "fish"):
            continue
        plain, prog, _k = info[s]
        if prog is None:
            continue
        base = _cmd_base(plain[prog].text)
        args = plain[prog + 1:]
        roles.seg = s
        if base in ("echo", "printf"):
            texts = [a.text for a in args]
            while base == "echo" and texts and texts[0] in ("-n", "-e", "-E", "-ne", "-en"):
                texts.pop(0)
            roles.nest(" ".join(texts).replace("\\n", "\n"), "bash", seg=s + 1)
        elif base in ("cat", "tac", "head", "tail"):
            for a in args:
                if not a.text.startswith("-"):
                    roles.op(a)
    return roles


def listable_missing(word: str, quiet: AbstractSet[str]) -> bool:
    """A missing, non-path word the review header lists as "no file here, not bound" (audit round 2): not quiet,
    not a number or size, not a NAME=VALUE pair and not text with spaces."""
    return bool(word) and word not in quiet and not word.startswith("-") and not _NUMERIC_RE.match(word) \
        and not _NAME_ASSIGN_RE.match(word) and not re.search(r"\s", word)


MISSING_UNBOUND = "(no file here, not bound)"
FOLDER_OPERAND = "(folder; only its entry files are bound)"


def bind_plan(argv_or_command: Union[List[str], str], *, shell: Optional[str] = None,
              os_name: Optional[str] = None) -> Tuple[List[str], List[str], List[str]]:
    """(spellings to bind, relative words whose cwd may change, header items for words that cannot be bound) (v8.1):
    the first three parts of ``bind_plan_ex``."""
    words, moved, unbound, _q = bind_plan_ex(argv_or_command, shell=shell, os_name=os_name)
    return words, moved, unbound


def bind_plan_ex(argv_or_command: Union[List[str], str], *, shell: Optional[str] = None,
                 os_name: Optional[str] = None, _depth: int = 0
                 ) -> Tuple[List[str], List[str], List[str], AbstractSet[str]]:
    """(spellings to bind, relative words whose cwd may change, header items for words that cannot be bound, quiet
    words) (v8.1).

    Spellings are the paths the program opens (``_spelled_words``: a quoted ``~`` or any ``~`` in argv is bound
    literally as ``./~…``). argv lists: once a word changes the directory (``changes_cwd``: ``git -C sub``), later
    relative words move to the second list. PowerShell/cmd strings: when the whole text may change the cwd
    (``text_may_change_cwd``) every relative word moves, before and after. POSIX shell strings (verification of audit
    round 2): the cwd is followed per simple command (``_Roles.cwd_segs``: cd/pushd/popd, eval, source, a program
    named by a variable or substitution), so relative words of commands before the first such command (and that
    command's own words: ``source run3``) stay bound and later ones move; a loop, function, alias or trap that starts
    before it, or a wrapper option that changes the cwd (``sudo -D``, ``bun --cwd``, …), moves every relative word;
    ``-C``/``--directory`` moves the rest of its command. Absolute and ``~`` words stay bound. Audit round 2: the
    script operand of a known interpreter (``_scan_commands``) is bound as ``./name`` whatever it looks like
    (missing -> ``absent``), ``python -m`` module files and cmd PATHEXT names are added (quiet), ``sh -c``/``eval``/
    ``cmd /c``/``pwsh -Command``/``iex`` text, here-strings and text piped into a shell are planned again. Quiet words
    (command names, option values that are not files, code) are not listed when missing (``bind_files``); quiet is
    decided per occurrence (a word also used as a plain argument is listed). Output redirect targets are not bound.
    The node review and the hub review share it."""
    words, moved, unbound, quiet, _loud = _plan(argv_or_command, shell, os_name, _depth)
    return words, moved, unbound, frozenset(quiet)


# a POSIX function definition (``f() {``): its body runs where it is called
_FUNC_DEF_RE = re.compile(r"(^|[\s;&|(){}])[A-Za-z_][\w.:-]*\s*\(\s*\)")
OP_MARK = "\x00op:"     # quiet-set entry: this spelling is a python/node operand (a folder there is named)


def _plan(argv_or_command: Union[List[str], str], shell: Optional[str], os_name: Optional[str], depth: int
          ) -> Tuple[List[str], List[str], List[str], set, set]:
    words: List[str] = []
    moved: List[str] = []
    unbound: List[str] = []
    try:
        roles = _scan_commands(argv_or_command, shell=shell, os_name=os_name)
    except Exception:  # noqa: BLE001 - the plain word rules still apply
        roles = _Roles()
    is_argv = isinstance(argv_or_command, (list, tuple))
    text = "" if is_argv else str(argv_or_command)
    spelled = _spelled_words(argv_or_command, shell=shell, os_name=os_name)
    posix = roles.dialect == "posix" and len(roles.words) == len(spelled) and all(
        rw.text == sw for rw, (sw, _c) in zip(roles.words, spelled))
    loud: set = set()
    for x in roles.words:
        if not x.quiet:
            loud.add(x.text)
            loud.update(sp for sp in x.spellings if sp)
        if "=" in x.text and not x.vquiet:
            v = x.text.split("=", 1)[1]
            loud.add(v)
            loud.update(sp for sp in _plain_spellings(v) if sp)
    loud -= set(roles.ops)          # operands are bound as ./name; the bare word is never listed
    first_cwd = min(roles.cwd_segs) if roles.cwd_segs else None
    taint = roles.cwd or (posix and first_cwd is not None and (any(sg <= first_cwd for sg in roles.late_segs)
                                                               or bool(_FUNC_DEF_RE.search(text))))
    after_flag = roles.cwd or (not is_argv and not posix and text_may_change_cwd(text))

    def seg_after(sg: int) -> bool:
        if not posix:
            return after_flag
        return taint or (first_cwd is not None and sg > first_cwd)

    any_after = (taint or first_cwd is not None) if posix else after_flag

    def put(c: str, spellings: List[Optional[str]], forced: bool, after: bool) -> None:
        for sp in spellings:
            if sp is None:
                item = f"{c} {UNBOUND_VARS}"
                if (forced or _pathlike(c)) and item not in unbound:
                    unbound.append(item)
            elif after and relative_word(sp):
                if sp not in moved:
                    moved.append(sp)
            elif sp not in words:
                words.append(sp)

    used: set = set()
    cur_seg = -1
    opt_after = False
    for k, (w, cands) in enumerate(spelled):
        if posix:
            rw = roles.words[k]
            if rw.redir == "out":
                continue          # written by the command, not run
            if rw.seg != cur_seg:
                cur_seg, opt_after = rw.seg, False
            a = seg_after(rw.seg) or opt_after
        else:
            a = after_flag
        for c, spellings in cands:
            if c in roles.ops:
                used.add(c)
                put(c, roles.ops[c], True, a)
            else:
                put(c, spellings, False, a)
        if posix:
            if _CWD_OPTION_RE.match(w):
                opt_after = True      # make -C sub x: the rest of this command
        elif not after_flag and changes_cwd(w):
            after_flag = True
    for c, spellings in roles.ops.items():
        if c not in used:
            put(c, spellings, True, seg_after(roles.op_seg.get(c, 0)))
    nested_quiet: set = set()
    forced_quiet: set = set()
    order = [x for x in roles.nested if not x[4]] + [x for x in roles.nested if x[4]]
    if depth < _SCAN_DEPTH:
        for text2, sh2, sg, late, q in order:
            w2, m2, u2, q2, l2 = _plan(text2, sh2, os_name, depth + 1)
            a = any_after if late else seg_after(sg)
            if q:
                before = set(words) | set(moved)
                put(text2, list(w2), False, a)
                forced_quiet |= {x for x in set(words) | set(moved) if x not in before}
                continue
            put(text2, list(w2), False, a)
            moved.extend(x for x in m2 if x not in moved)
            unbound.extend(x for x in u2 if x not in unbound)
            nested_quiet |= q2
            loud |= l2
    elif any(not x[4] for x in roles.nested):
        unbound.append(f"{[x for x in roles.nested if not x[4]][0][0]} (nested too deep; not bound)")
    for p, sg in roles.extras:
        put("", [p], False, seg_after(sg))
    quiet = ((set(roles.quiet) | nested_quiet) - loud) | forced_quiet
    quiet |= {c for c in roles.ops if not _pathlike(c)}     # bound as ./name; the bare word is not listed again
    for name in roles.entry_ops:
        quiet.update(OP_MARK + sp for sp in roles.ops.get(name) or [] if sp)
    unbound.extend(x for x in roles.unbound if x not in unbound)
    return words, moved, unbound, quiet, loud


def bindable_words(argv_or_command: Union[List[str], str], *, shell: Optional[str] = None,
                   os_name: Optional[str] = None) -> Tuple[List[str], List[str]]:
    """(words to bind, relative words whose cwd may change): the first two parts of ``bind_plan``."""
    words, moved, _u = bind_plan(argv_or_command, shell=shell, os_name=os_name)
    return words, moved


# v8.1 (external review): env keys whose value names a file the program loads before the approved command runs
_ENV_FILE_KEYS = ("BASH_ENV", "ENV", "PYTHONSTARTUP", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS")
_NODE_FILE_OPTS = ("-r", "--require", "--import", "--loader", "--experimental-loader")


def env_file_values(env: Any) -> List[Tuple[str, str]]:
    """``[(key, file value)]`` of the env keys that name a file loaded before the command (BASH_ENV, ENV,
    PYTHONSTARTUP, LD_PRELOAD/DYLD_INSERT_LIBRARIES entries, NODE_OPTIONS --require/--import targets)."""
    out: List[Tuple[str, str]] = []
    for k, v in _env_pairs(env):
        ku = k.upper()
        if ku not in _ENV_FILE_KEYS or not v:
            continue
        if ku in ("LD_PRELOAD", "DYLD_INSERT_LIBRARIES"):
            vals = [x for x in re.split(r"[:\s]+", v) if x]
        elif ku == "NODE_OPTIONS":
            try:
                toks = shlex.split(v)
            except ValueError:
                toks = v.split()
            vals = []
            for i, t in enumerate(toks):
                if t in _NODE_FILE_OPTS and i + 1 < len(toks):
                    vals.append(toks[i + 1])
                elif "=" in t and t.split("=", 1)[0] in _NODE_FILE_OPTS:
                    vals.append(t.split("=", 1)[1])
        else:
            vals = [v]
        out += [(k, x) for x in vals if x]
    return out


def _env_words(env: Any, words: List[str], unbound: List[str]) -> Tuple[List[str], List[str]]:
    """Add the files named by env values (``env_file_values``) to the words to bind (v8.1): relative values are
    resolved against the reviewed cwd (made path-like so a missing one is bound as absent), a ``~`` value both
    expanded and literally; a value with ``$`` or a backtick is listed as not bound."""
    words = list(words)
    unbound = list(unbound)
    for k, x in env_file_values(env):
        if "$" in x or "`" in x or _known_var(x):
            item = f"env {k}={x} {UNBOUND_VARS}"
            if item not in unbound:
                unbound.append(item)
            continue
        if x.startswith("~"):
            spellings = [x, LITERAL_PREFIX + x]
        elif relative_word(x) and not x.startswith(("./", ".\\")):
            spellings = [LITERAL_PREFIX + x]
        else:
            spellings = [x]
        for sp in spellings:
            if sp not in words:
                words.append(sp)
    return words, unbound


def _pathlike(word: str) -> bool:
    return "/" in word or "\\" in word or word.lower().endswith(PATHLIKE_EXT)


def moved_items(moved: List[str], is_file: Callable[[str], bool], quiet: Optional[AbstractSet[str]] = None,
                exists: Optional[Callable[[str], bool]] = None) -> List[str]:
    """Header items "<word> (cwd may change)" for the relative words after a cwd change (``bindable_words``): the
    path-like ones and those naming a file in the review cwd (the same words binding would have bound); command names
    such as ``bash`` and the ``cd`` target folder are left out (v8.1). Verification of audit round 2: with ``quiet``
    given, every other word that would be listed when missing (``listable_missing``) and is not a folder here is
    listed as "<word> (no file here, not bound)", so a plain name after a cwd change is never dropped."""
    out: List[str] = []
    for w in moved:
        try:
            hit = _pathlike(w) or bool(is_file(w))
        except Exception:  # noqa: BLE001
            hit = False
        if hit:
            out.append(f"{w} (cwd may change)")
        elif quiet is not None and listable_missing(w, quiet):
            try:
                there = bool(exists(w)) if exists is not None else False
            except Exception:  # noqa: BLE001
                there = False
            if not there:
                out.append(f"{w} {MISSING_UNBOUND}")
    return out


def _strip_dot_segments(p: str) -> str:
    """``p`` without ``./`` segments (``/cwd/./run3`` -> ``/cwd/run3``); ``..`` is kept (a link may sit before it)."""
    prev = None
    while prev != p:
        prev = p
        p = re.sub(r"([\\/])\.(?:[\\/])", r"\1", p)
    return p


def bind_files(words: List[str], *, policy: Any, cwd: str, mask: Callable[[str], str],
               quiet: AbstractSet[str] = frozenset()) -> Tuple[List[dict], List[str], List[str]]:
    """(files entries, content lines, not-bound header items) per the §5.6 v8 binding rules.

    v8.1 (audit round 2): each entry's ``path`` is the path as the command names it (cwd + word, before symbolic
    links are resolved; ``real`` = the real path when it differs), so ``FileHolds.acquire`` re-resolves it at consume
    and a link retargeted after approval is a conflict (as on the hub); a second word naming an already bound file
    through another path adds an entry for that path too. A missing word that is not path-like is listed as "no file
    here, not bound" unless it is quiet (``bind_plan_ex``); a quiet path-like word is bound without a header item."""
    files: List[dict] = []
    lines: List[str] = []
    not_bound: List[str] = []
    seen: Dict[str, dict] = {}
    named_seen: set = set()
    bound = 0
    absent = 0
    shown_total = 0
    hash_end = time.monotonic() + HASH_BUDGET_S
    for w in words:
        if "\x00" in w:
            continue
        expanded = os.path.expanduser(w) if w.startswith("~") else w
        if w.startswith("$"):   # a leading $HOME-style word (bind_plan): PathGuard expands it, not joined to the cwd
            cand = w
        else:
            cand = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        try:
            # v8.1 (external review): resolved with the READ check, so a file agents may not write (service code,
            # exec paths, held inputs) is still shown and bound; the line says agents cannot change it
            real = str(policy.resolve_path(cand))
        except HiveError:
            if _pathlike(w) and w not in quiet:
                not_bound.append(f"{w} (not resolvable here)")
            continue
        except Exception:  # noqa: BLE001
            continue
        named = _strip_dot_segments(cand)
        if real in seen:
            first = seen[real]
            if named != real and named not in named_seen and first.get("bind") != "none":
                named_seen.add(named)
                alias = dict(first, path=named, real=real, shown=False)
                files.append(alias)
            continue
        try:
            st = os.stat(real)
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError:
            if _pathlike(w) and w not in quiet:
                not_bound.append(f"{w} (not resolvable here)")
            continue
        if exists and not os.path.isfile(real):
            if OP_MARK + w in quiet and os.path.isdir(real):
                not_bound.append(f"{w.rstrip('/')}/ {FOLDER_OPERAND}")
            continue
        if not exists and not _pathlike(w):
            if listable_missing(w, quiet):
                not_bound.append(f"{w} {MISSING_UNBOUND}")
            continue
        named_seen.add(named)

        def entry(**kw: Any) -> dict:
            e = dict(kw)
            if named != real:
                e["path"] = named
                e["real"] = real
            seen[real] = e
            return e
        seen[real] = {"bind": "none"}
        try:
            policy.resolve_path(cand, write=True)
            locked = ""
        except Exception:  # noqa: BLE001
            locked = "; agents cannot change it"
        if (bound >= BIND_MAX_FILES) if exists else (absent >= BIND_MAX_ABSENT):
            files.append(entry(path=real, bind="none", sha256=None, size=st.st_size if exists else None,
                               shown=False))
            if w not in quiet:
                not_bound.append(f"{w} (over the {BIND_MAX_FILES if exists else BIND_MAX_ABSENT}-file limit)")
            continue
        if not exists:
            absent += 1
            files.append(entry(path=real, bind="absent", sha256=None, size=None, shown=False))
            if w not in quiet:
                not_bound.append(f"{w} (missing here)")
            continue
        bound += 1
        size = st.st_size
        if size > HASH_MAX or time.monotonic() > hash_end:
            fp = f"stat:{size}:{iso_utc(st.st_mtime)}"
            files.append(entry(path=real, bind="stat", sha256=None, size=size, shown=False, fingerprint=fp,
                               mtime=iso_utc(st.st_mtime)))
            lines.append(f"--- file {real} ({human_bytes(size)}, bound by size and mtime{locked}) ---")
            lines.append(NOT_SHOWN)
            continue
        try:
            with open(real, "rb") as f:
                data = f.read(HASH_MAX + 1)
        except OSError:
            bound -= 1
            seen.pop(real, None)
            named_seen.discard(named)
            if _pathlike(w):
                not_bound.append(f"{w} (not readable here)")
            continue
        digest = hashlib.sha256(data).hexdigest()
        shown = False
        text: Optional[str] = None
        if b"\x00" not in data:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        if text is not None and shown_total + len(data) <= SHOW_MAX:
            shown = True
            shown_total += len(data)
        files.append(entry(path=real, bind="sha256", sha256=digest, size=len(data), shown=shown))
        lines.append(f"--- file {real} ({human_bytes(len(data))}, sha256 {digest[:12]}{locked}) ---")
        if shown and text is not None:
            lines.extend(mask(text).split("\n"))
        else:
            lines.append(NOT_SHOWN)
    return files, lines, not_bound


def _param_lines(params: dict, *, skip: Tuple[str, ...], secret_re: "re.Pattern[str]",
                 shows_env: bool) -> List[str]:
    lines: List[str] = []
    for key, value in params.items():
        if key in skip or value is None:
            continue
        if key == "env":
            for k, v in _env_pairs(value):
                lines.append(f"env {k}={_redact_value(v) if secret_re.search(k) else v}")
        elif key == "stdin" and isinstance(value, str):
            lines.append("stdin:")
            lines.extend(value.split("\n"))
        elif isinstance(value, str) and "\n" in value:
            lines.append(f"{key}:")
            lines.extend(value.split("\n"))
        elif not shows_env and secret_re.search(key) and key not in ("secret_env", "auth_env", "api_key_env"):
            lines.append(f"{key}: ***")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return lines


def _review_cwd(policy: Any, cwd: Optional[str]) -> Tuple[str, str]:
    """(text for the ``cwd:`` line, real cwd the command words are bound against) (v8.1, external review).

    Never falls back to the workdir for a given cwd: the cwd is resolved like ``check_cwd`` resolves it at run time
    (``~``/variables expanded, relative -> workdir, realpath) through PathGuard without requiring it to exist; a
    missing or refused cwd is shown verbatim with the reason, and the words are still bound against it (a script in a
    missing folder is ``absent``). The real cwd goes into the record (``review["cwd"]``); ``require`` refuses a
    consume whose cwd now resolves elsewhere."""
    if cwd is None or cwd == "":
        try:
            real = str(policy.check_cwd(None))
        except Exception:  # noqa: BLE001
            try:
                real = str(policy.workdir)
            except Exception:  # noqa: BLE001
                real = os.getcwd()
        return f"{real} (node workdir)", real
    cwd = str(cwd)
    reason: Optional[str] = None
    try:
        real = str(policy.resolve_path(cwd))
    except HiveError as e:
        reason = e.message
        real = ""
    except Exception as e:  # noqa: BLE001
        reason = type(e).__name__
        real = ""
    if not real:
        try:
            expand = getattr(policy, "_expand", None)
            s = expand(cwd) if callable(expand) else os.path.expanduser(cwd)
            if not os.path.isabs(s):
                s = os.path.join(str(policy.workdir), s)
            real = os.path.realpath(s)
        except Exception:  # noqa: BLE001
            real = cwd
    text = cwd if cwd == real else f"{cwd} (= {real})"
    if reason:
        text += f" (not usable here: {reason})"
    elif not os.path.exists(real):
        text += " (does not exist here yet)"
    elif not os.path.isdir(real):
        text += " (not a folder)"
    return text, real


def _resolved_shell(shell: Optional[str], ctx: PresetCtx) -> str:
    if shell not in (None, "", "auto"):
        return str(shell)
    try:
        from . import proctree
        return proctree.default_shell()
    except Exception:  # noqa: BLE001
        return "powershell" if ctx.os == "windows" else "bash"


def _argv0_mtime(argv0: str, policy: Any) -> Optional[str]:
    try:
        from . import proctree
        p = proctree.which(argv0, getattr(policy, "node_path", os.environ.get("PATH", "")))
        if p:
            return iso_utc(os.stat(p).st_mtime)
    except Exception:  # noqa: BLE001
        return None
    return None


def approval_review(action: str, params: dict, *, ctx: Optional[PresetCtx], given: AbstractSet[str] = frozenset(),
                    service: Optional[dict] = None) -> dict:
    """The approval review (§5.6 pinned shape ``{"action","lines","changes_what_runs","files","note"?}``).

    shell.run/job.start: full command/argv, shell, cwd, secret_env names, env WITH values (secret-named keys
    redacted), full stdin. service.define: on the node (ctx given) the built definition (expand_define), on the hub
    (ctx None) the params + the HUB_DEFINE_NOTE. service.start/restart: the stored definition's argv/cwd/defined_by
    and argv[0]'s mtime (node) or the name + HUB_START_NOTE (hub). Other actions: audit.redact_params. With ctx
    given, the files the command names are bound (§5.6 file binding, at most 8, shown text through ``ctx.mask``).
    """
    params = dict(params or {})
    secret_re = _secret_key_re()
    note: Optional[str] = None
    body: List[str] = []
    env_keys: List[str] = []
    files: List[dict] = []
    file_lines: List[str] = []
    not_bound: List[str] = []
    moved: List[str] = []
    quiet: AbstractSet[str] = frozenset()
    real_cwd: Optional[str] = None
    if action in ("shell.run", "job.start"):
        env_keys = [k for k, _v in _env_pairs(params.get("env"))]
        shell = params.get("shell")
        if ctx is not None and not params.get("argv"):
            shell = _resolved_shell(shell, ctx)
        body.append(f"action: {action}")
        if params.get("argv"):
            body.append("argv: " + json.dumps(params["argv"], ensure_ascii=False))
        else:
            body.append(f"shell: {shell or 'auto'}")
            body.append("command:")
            body.extend(str(params.get("command") or "").split("\n"))
        if ctx is not None:
            cwd_text, cwd = _review_cwd(ctx.policy, params.get("cwd"))
            real_cwd = cwd
        else:
            cwd = params.get("cwd")
            cwd_text = cwd if cwd else "(node workdir)"
        body.append(f"cwd: {cwd_text}")
        if params.get("secret_env"):
            body.append("secret_env: " + ", ".join(params["secret_env"]))
        body.extend(_param_lines(params, skip=("argv", "command", "shell", "cwd", "secret_env"), secret_re=secret_re,
                                 shows_env=True))
        if ctx is not None:
            words, moved, unbound, quiet = bind_plan_ex(params.get("argv") or str(params.get("command") or ""),
                                                        shell=shell, os_name=ctx.os)
            words, unbound = _env_words(params.get("env"), words, unbound)
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=str(cwd), mask=ctx.mask,
                                                      quiet=quiet)
            not_bound += unbound
    elif action == "service.define":
        if ctx is None:
            note = HUB_DEFINE_NOTE
            env_keys = [k for k, _v in _env_pairs(params.get("env"))]
            body.append(f"action: {action}")
            body.extend(_param_lines(params, skip=(), secret_re=secret_re, shows_env=True))
        else:
            d = expand_define(params, ctx=ctx, given=given)
            env_keys = list(d.get("env") or {})
            body.append(f"action: {action}")
            body.append(f"name: {d.get('name')}")
            if d.get("preset"):
                body.append(f"preset: {d['preset']} (expanded on this node)")
            cwd_text, cwd = _review_cwd(ctx.policy, d.get("cwd"))
            real_cwd = cwd
            if d.get("argv"):
                body.append("argv: " + json.dumps(d["argv"], ensure_ascii=False))
                words, moved, unbound, quiet = bind_plan_ex(d["argv"])
            else:
                shell = _resolved_shell(d.get("shell"), ctx)
                body.append(f"shell: {shell}")
                body.append("command:")
                body.extend(str(d.get("command") or "").split("\n"))
                words, moved, unbound, quiet = bind_plan_ex(str(d.get("command") or ""), shell=shell,
                                                            os_name=ctx.os)
            if d.get("stop_command"):
                body.append("stop_command: " + json.dumps(d["stop_command"], ensure_ascii=False))
                sw, sm, su, sq = bind_plan_ex(d["stop_command"])
                quiet = frozenset(quiet) | frozenset(sq)
                words += [w for w in sw if w not in words]
                moved += [w for w in sm if w not in moved]
                unbound += [w for w in su if w not in unbound]
            words, unbound = _env_words(d.get("env"), words, unbound)
            body.append(f"cwd: {cwd_text}")
            auto = [s for s in (d.get("secret_env") or []) if s not in (params.get("secret_env") or [])]
            if d.get("secret_env"):
                body.append("secret_env: " + ", ".join(d["secret_env"])
                            + (f" (added by the preset: {', '.join(auto)})" if auto else ""))
            body.extend(_param_lines(d, skip=("name", "preset", "argv", "command", "shell", "cwd", "secret_env",
                                              "stop_command", "volumes"), secret_re=secret_re, shows_env=True))
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=cwd, mask=ctx.mask, quiet=quiet)
            not_bound += unbound
    elif action in ("service.start", "service.restart"):
        body.append(f"action: {action}")
        body.append(f"name: {params.get('name')}")
        if ctx is None or not service:
            note = HUB_START_NOTE
            if params.get("wait"):
                body.append(f"wait: {json.dumps(params.get('wait'))}")
        else:
            argv = service.get("argv")
            cwd_text, cwd = _review_cwd(ctx.policy, service.get("cwd"))
            real_cwd = cwd
            if argv:
                body.append("argv: " + json.dumps(argv, ensure_ascii=False))
                words, moved, unbound, quiet = bind_plan_ex(argv)
                mt = _argv0_mtime(str(argv[0]), ctx.policy)
                body.append(f"argv[0] modified: {mt or 'unknown'}")
            else:
                shell = _resolved_shell(service.get("shell"), ctx)
                body.append(f"shell: {shell}")
                body.append("command:")
                body.extend(str(service.get("command") or "").split("\n"))
                words, moved, unbound, quiet = bind_plan_ex(str(service.get("command") or ""), shell=shell,
                                                            os_name=ctx.os)
            body.append(f"cwd: {cwd_text}")
            body.append(f"defined_by: {service.get('defined_by') or 'unknown'}")
            words, unbound = _env_words(service.get("env"), words, unbound)
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=cwd, mask=ctx.mask, quiet=quiet)
            not_bound += unbound
    else:
        try:
            from .audit import redact_params
            red = redact_params(action, params)
        except Exception:  # noqa: BLE001
            red = {k: "***" if secret_re.search(k) else v for k, v in params.items()}
        body.append(f"action: {action}")
        body.extend(_param_lines(red, skip=(), secret_re=secret_re, shows_env=False))
    if moved and real_cwd is not None:
        not_bound += moved_items(moved, lambda w: os.path.isfile(os.path.join(str(real_cwd), os.path.expanduser(w))),
                                 quiet, lambda w: os.path.exists(os.path.join(str(real_cwd), os.path.expanduser(w))))
    changes = _risky(env_keys)
    header: List[str] = []
    if changes:
        header.append("changes what runs: " + ", ".join(changes))
    if not_bound:
        header.append("not bound to their content (may change after approval): " + ", ".join(not_bound))
    if note:
        header.append("note: " + note)
    main_lines = [escape_terminal(x) for x in header + body]
    if sum(len(x.encode("utf-8")) + 1 for x in main_lines) > REVIEW_MAX:
        raise too_large("the approval review is larger than 256 KiB", hint="split the input or ask the owner")
    review: Dict[str, Any] = {"action": action, "lines": main_lines + [escape_terminal(x) for x in file_lines],
                              "changes_what_runs": changes, "files": files}
    if note:
        review["note"] = note
    if real_cwd is not None:
        review["cwd"] = real_cwd     # v8.1: require() keeps it in the record and checks it again on consume
    return review


__all__ = ["Preset", "PRESETS", "PresetCtx", "build", "expand_define", "approval_review", "describe",
           "OP_MARK", "FOLDER_OPERAND", "BIND_MAX_ABSENT", "candidate_words", "bindable_words", "bind_plan", "bind_plan_ex", "listable_missing", "text_may_change_cwd", "changes_cwd", "moved_items", "bind_files", "docker_volume", "RISKY_ENV_PATTERNS", "HUB_DEFINE_NOTE",
           "HUB_START_NOTE", "DEF_KEYS", "INTERNAL_KEYS", "BIND_HINT"]
