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


def _raw_words(argv_or_command: Union[List[str], str], *, shell: Optional[str], os_name: Optional[str]) -> List[str]:
    return [w for w, _s in _spelled_words(argv_or_command, shell=shell, os_name=os_name)]


# v8.1 (external review): quoting state of each character of a POSIX shell word: 0 unquoted, 1 inside "…" ($ still
# expands, ~ does not), 2 literal ('…', $'…', backslash-escaped: nothing expands)
_Q_NONE, _Q_DOUBLE, _Q_LITERAL = 0, 1, 2


def _posix_tokens(text: str) -> List[List[Tuple[str, int]]]:
    """Words of a POSIX shell string as ``[(char, quoting)]``: operators ``();<>|&`` and whitespace split words,
    ``#`` at a word start begins a comment; an unclosed quote raises ValueError."""
    words: List[List[Tuple[str, int]]] = []
    cur: Optional[List[Tuple[str, int]]] = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n" or c in _SHELL_OPS:
            if cur is not None:
                words.append(cur)
                cur = None
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
        words.append(cur)
    return words


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


def bind_plan(argv_or_command: Union[List[str], str], *, shell: Optional[str] = None,
              os_name: Optional[str] = None) -> Tuple[List[str], List[str], List[str]]:
    """(spellings to bind, relative words whose cwd may change, header items for words that cannot be bound) (v8.1).

    Spellings are the paths the program opens (``_spelled_words``: a quoted ``~`` or any ``~`` in argv is bound
    literally as ``./~…``). argv lists: once a word changes the directory (``changes_cwd``: ``git -C sub``), later
    relative words move to the second list. Shell strings: when the whole text may change the cwd
    (``text_may_change_cwd``) every relative word moves, before and after; otherwise the argv rule applies. Absolute
    and ``~`` words stay bound. The node review and the hub review share it."""
    words: List[str] = []
    moved: List[str] = []
    unbound: List[str] = []
    after = not isinstance(argv_or_command, (list, tuple)) and text_may_change_cwd(str(argv_or_command))
    for w, cands in _spelled_words(argv_or_command, shell=shell, os_name=os_name):
        for c, spellings in cands:
            for sp in spellings:
                if sp is None:
                    item = f"{c} {UNBOUND_VARS}"
                    if _pathlike(c) and item not in unbound:
                        unbound.append(item)
                elif after and relative_word(sp):
                    if sp not in moved:
                        moved.append(sp)
                elif sp not in words:
                    words.append(sp)
        if not after and changes_cwd(w):
            after = True
    return words, moved, unbound


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


def moved_items(moved: List[str], is_file: Callable[[str], bool]) -> List[str]:
    """Header items "<word> (cwd may change)" for the relative words after a cwd change (``bindable_words``): the
    path-like ones and those naming a file in the review cwd (the same words binding would have bound); command names
    such as ``bash`` and the ``cd`` target folder are left out (v8.1)."""
    out: List[str] = []
    for w in moved:
        try:
            hit = _pathlike(w) or bool(is_file(w))
        except Exception:  # noqa: BLE001
            hit = False
        if hit:
            out.append(f"{w} (cwd may change)")
    return out


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
        if w.startswith("$"):   # a leading $HOME-style word (bind_plan): PathGuard expands it, not joined to the cwd
            cand = w
        else:
            cand = expanded if os.path.isabs(expanded) else os.path.join(cwd, expanded)
        try:
            # v8.1 (external review): resolved with the READ check, so a file agents may not write (service code,
            # exec paths, held inputs) is still shown and bound; the line says agents cannot change it
            real = str(policy.resolve_path(cand))
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
        try:
            policy.resolve_path(cand, write=True)
            locked = ""
        except Exception:  # noqa: BLE001
            locked = "; agents cannot change it"
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
            lines.append(f"--- file {real} ({human_bytes(size)}, bound by size and mtime{locked}) ---")
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
            words, moved, unbound = bind_plan(params.get("argv") or str(params.get("command") or ""), shell=shell,
                                              os_name=ctx.os)
            words, unbound = _env_words(params.get("env"), words, unbound)
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=str(cwd), mask=ctx.mask)
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
                words, moved, unbound = bind_plan(d["argv"])
            else:
                shell = _resolved_shell(d.get("shell"), ctx)
                body.append(f"shell: {shell}")
                body.append("command:")
                body.extend(str(d.get("command") or "").split("\n"))
                words, moved, unbound = bind_plan(str(d.get("command") or ""), shell=shell, os_name=ctx.os)
            if d.get("stop_command"):
                body.append("stop_command: " + json.dumps(d["stop_command"], ensure_ascii=False))
                sw, sm, su = bind_plan(d["stop_command"])
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
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=cwd, mask=ctx.mask)
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
                words, moved, unbound = bind_plan(argv)
                mt = _argv0_mtime(str(argv[0]), ctx.policy)
                body.append(f"argv[0] modified: {mt or 'unknown'}")
            else:
                shell = _resolved_shell(service.get("shell"), ctx)
                body.append(f"shell: {shell}")
                body.append("command:")
                body.extend(str(service.get("command") or "").split("\n"))
                words, moved, unbound = bind_plan(str(service.get("command") or ""), shell=shell, os_name=ctx.os)
            body.append(f"cwd: {cwd_text}")
            body.append(f"defined_by: {service.get('defined_by') or 'unknown'}")
            words, unbound = _env_words(service.get("env"), words, unbound)
            files, file_lines, not_bound = bind_files(words, policy=ctx.policy, cwd=cwd, mask=ctx.mask)
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
        not_bound += moved_items(moved, lambda w: os.path.isfile(os.path.join(str(real_cwd), os.path.expanduser(w))))
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
           "candidate_words", "bindable_words", "bind_plan", "text_may_change_cwd", "changes_cwd", "moved_items", "bind_files", "docker_volume", "RISKY_ENV_PATTERNS", "HUB_DEFINE_NOTE",
           "HUB_START_NOTE", "DEF_KEYS", "INTERNAL_KEYS", "BIND_HINT"]
