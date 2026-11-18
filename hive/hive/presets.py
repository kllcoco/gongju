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


def candidate_words(argv_or_command: Union[List[str], str], *, shell: Optional[str] = None,
                    os_name: Optional[str] = None) -> List[str]:
    """Words a command names (§5.6 file binding): argv elements, or the command split (shlex for POSIX shells,
    whitespace with quotes stripped for PowerShell/cmd), plus the part after the first ``=`` of KEY=VALUE /
    --opt=VALUE words; words starting with ``-`` are skipped."""
    if isinstance(argv_or_command, (list, tuple)):
        words = [str(w) for w in argv_or_command]
    else:
        text = str(argv_or_command)
        if _windows_shell(shell, os_name):
            words = [x.strip("\"'") for w in text.split() for x in _META_SPLIT.split(w)]
        else:
            try:
                # Phase 2 review: shell operators split words even without spaces ("bash run.sh;true",
                # "run.sh|bash", "<in.sh"), so a script next to a metacharacter is still bound (§5.6)
                lex = shlex.shlex(text, posix=True, punctuation_chars=True)
                lex.whitespace_split = True
                words = [w for w in lex if not (w and all(c in _SHELL_OPS for c in w))]
            except ValueError:
                words = [x.strip("\"'") for w in text.split() for x in _META_SPLIT.split(w)]
    out: List[str] = []
    for w in words:
        if not w:
            continue
        if not w.startswith("-") and w not in out:
            out.append(w)
        if "=" in w:
            v = w.split("=", 1)[1]
            if v and v not in out and not v.startswith("-"):
                out.append(v)
    return out


def _pathlike(word: str) -> bool:
    return "/" in word or "\\" in word or word.lower().endswith(PATHLIKE_EXT)


def bind_files(words: List[str], *, policy: Any, cwd: str, mask: Callable[[str], str]
               ) -> Tuple[List[dict], List[str], List[str]]:
    """(files entries, content lines, not-bound header items) per the §5.6 v8 binding rules."""
    files: List[dict] = []
    lines: List[str] = []
    not_bound: List[str] = []
    seen: set = set()
    bound = 0
    shown_total = 0
    hash_end = time.monotonic() + HASH_BUDGET_S
    for w in words:
        if "\x00" in w:
            continue
        expanded = os.path.expanduser(w) if w.startswith("~") else w
        cand = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        try:
            real = str(policy.resolve_path(cand, write=True))
        except HiveError:
            if _pathlike(w):
                not_bound.append(f"{w} (not resolvable here)")
            continue
        except Exception:  # noqa: BLE001
            continue
        if real in seen:
            continue
        try:
            st = os.stat(real)
            exists = True
        except FileNotFoundError:
            exists = False
        except OSError:
            if _pathlike(w):
                not_bound.append(f"{w} (not resolvable here)")
            continue
        if exists and not os.path.isfile(real):
            continue
        if not exists and not _pathlike(w):
            continue
        seen.add(real)
        if bound >= BIND_MAX_FILES:
            files.append({"path": real, "bind": "none", "sha256": None, "size": st.st_size if exists else None,
                          "shown": False})
            not_bound.append(f"{w} (over the {BIND_MAX_FILES}-file limit)")
            continue
        bound += 1
        if not exists:
            files.append({"path": real, "bind": "absent", "sha256": None, "size": None, "shown": False})
            not_bound.append(f"{w} (missing here)")
            continue
        size = st.st_size
        if size > HASH_MAX or time.monotonic() > hash_end:
            fp = f"stat:{size}:{iso_utc(st.st_mtime)}"
            files.append({"path": real, "bind": "stat", "sha256": None, "size": size, "shown": False,
                          "fingerprint": fp, "mtime": iso_utc(st.st_mtime)})
            lines.append(f"--- file {real} ({human_bytes(size)}, bound by size and mtime) ---")
            lines.append(NOT_SHOWN)
            continue
        try:
            with open(real, "rb") as f:
                data = f.read(HASH_MAX + 1)
        except OSError:
            bound -= 1
            seen.discard(real)
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
        files.append({"path": real, "bind": "sha256", "sha256": digest, "size": len(data), "shown": shown})
        lines.append(f"--- file {real} ({human_bytes(len(data))}, sha256 {digest[:12]}) ---")
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


def _resolve_cwd(policy: Any, cwd: Optional[str]) -> str:
    try:
        return str(policy.check_cwd(cwd))
    except Exception:  # noqa: BLE001
        try:
            return str(policy.workdir)
        except Exception:  # noqa: BLE001
            return os.getcwd()


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
        cwd = _resolve_cwd(ctx.policy, params.get("cwd")) if ctx is not None else params.get("cwd")
        body.append(f"cwd: {cwd if cwd else '(node workdir)'}")
        if params.get("secret_env"):
            body.append("secret_env: " + ", ".join(params["secret_env"]))
        body.extend(_param_lines(params, skip=("argv", "command", "shell", "cwd", "secret_env"), secret_re=secret_re,
                                 shows_env=True))
        if ctx is not None:
            words = candidate_words(params.get("argv") or str(params.get("command") or ""), shell=shell,
                                    os_name=ctx.os)
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=str(cwd), mask=ctx.mask)
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
            cwd = _resolve_cwd(ctx.policy, d.get("cwd"))
            if d.get("argv"):
                body.append("argv: " + json.dumps(d["argv"], ensure_ascii=False))
                words = candidate_words(d["argv"])
            else:
                shell = _resolved_shell(d.get("shell"), ctx)
                body.append(f"shell: {shell}")
                body.append("command:")
                body.extend(str(d.get("command") or "").split("\n"))
                words = candidate_words(str(d.get("command") or ""), shell=shell, os_name=ctx.os)
            if d.get("stop_command"):
                body.append("stop_command: " + json.dumps(d["stop_command"], ensure_ascii=False))
                words += [w for w in candidate_words(d["stop_command"]) if w not in words]
            body.append(f"cwd: {cwd}")
            auto = [s for s in (d.get("secret_env") or []) if s not in (params.get("secret_env") or [])]
            if d.get("secret_env"):
                body.append("secret_env: " + ", ".join(d["secret_env"])
                            + (f" (added by the preset: {', '.join(auto)})" if auto else ""))
            body.extend(_param_lines(d, skip=("name", "preset", "argv", "command", "shell", "cwd", "secret_env",
                                              "stop_command", "volumes"), secret_re=secret_re, shows_env=True))
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=cwd, mask=ctx.mask)
    elif action in ("service.start", "service.restart"):
        body.append(f"action: {action}")
        body.append(f"name: {params.get('name')}")
        if ctx is None or not service:
            note = HUB_START_NOTE
            if params.get("wait"):
                body.append(f"wait: {json.dumps(params.get('wait'))}")
        else:
            argv = service.get("argv")
            cwd = _resolve_cwd(ctx.policy, service.get("cwd"))
            if argv:
                body.append("argv: " + json.dumps(argv, ensure_ascii=False))
                words = candidate_words(argv)
                mt = _argv0_mtime(str(argv[0]), ctx.policy)
                body.append(f"argv[0] modified: {mt or 'unknown'}")
            else:
                shell = _resolved_shell(service.get("shell"), ctx)
                body.append(f"shell: {shell}")
                body.append("command:")
                body.extend(str(service.get("command") or "").split("\n"))
                words = candidate_words(str(service.get("command") or ""), shell=shell, os_name=ctx.os)
            body.append(f"cwd: {cwd}")
            body.append(f"defined_by: {service.get('defined_by') or 'unknown'}")
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=cwd, mask=ctx.mask)
    else:
        try:
            from .audit import redact_params
            red = redact_params(action, params)
        except Exception:  # noqa: BLE001
            red = {k: "***" if secret_re.search(k) else v for k, v in params.items()}
        body.append(f"action: {action}")
        body.extend(_param_lines(red, skip=(), secret_re=secret_re, shows_env=False))
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
    return review


__all__ = ["Preset", "PRESETS", "PresetCtx", "build", "expand_define", "approval_review", "describe",
           "candidate_words", "bind_files", "docker_volume", "RISKY_ENV_PATTERNS", "HUB_DEFINE_NOTE",
           "HUB_START_NOTE", "DEF_KEYS", "INTERNAL_KEYS", "BIND_HINT"]
