"""The action catalog: ActionSpec and all 38 wire actions (design §4.3 and §9, Phase 0).

The single source of truth for every action (D1): node, hub, tool layer, MCP, docs and tests use these specs.
``params`` schemas are binding and strict-friendly (§4.4 lint). ``result`` schemas document the result shape
(asserted in tests, never at runtime); they are closed and catalog.py adds the optional ``masked: boolean``
property (§13.2 step 10a) to every one of them. Result schemas are DRAFT until milestone M1 (§22.1): fields that
may be null or have several shapes carry no ``type``, only a description.
"""
from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from .errors import HiveError
from .util import canonical_json, sha256_text

RISKS = ("read", "write", "exec", "net", "persist")
SCOPES = ("node", "hub")
PLATFORMS = ("linux", "darwin", "windows")


@dataclass(frozen=True, eq=False)      # eq=False: dict fields would make the generated __hash__ raise
class ActionSpec:
    """One wire action (§4.3)."""
    name: str                          # ^[a-z]+\.[a-z_]+$  e.g. "shell.run"
    title: str
    description: str                   # agent-facing, <= 1000 chars, English, says when to prefer another action
    params: dict                       # JSON schema, type=object, additionalProperties=false, every property described
    result: dict                       # documentation schema of the result (closed; + optional `masked`)
    risk: str                          # one of RISKS
    extra_classes: Tuple[str, ...] = ()
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    long_running: bool = False
    default_timeout: float = 30.0
    deadline_params: Tuple[str, ...] = ()
    scope: str = "node"
    platforms: Tuple[str, ...] = PLATFORMS
    since: str = "1.0"

    @property
    def mutating(self) -> bool:
        """True for every risk other than ``read``."""
        return self.risk != "read"

    @property
    def classes_required(self) -> FrozenSet[str]:
        """``{risk} ∪ extra_classes`` (D47)."""
        return frozenset((self.risk,) + tuple(self.extra_classes))


# ---------------------------------------------------------------------------------------------------------------
# schema builders (params)

def _p(t: str, desc: str, **kw: Any) -> dict:
    d: Dict[str, Any] = {"type": t, "description": desc}
    d.update(kw)
    return d


def _s(desc: str, **kw: Any) -> dict:
    return _p("string", desc, **kw)


def _i(desc: str, **kw: Any) -> dict:
    return _p("integer", desc, **kw)


def _n(desc: str, **kw: Any) -> dict:
    return _p("number", desc, **kw)


def _b(desc: str, default: Optional[bool] = None) -> dict:
    d = _p("boolean", desc)
    if default is not None:
        d["default"] = default
    return d


def _e(values: List[str], desc: str, default: Optional[str] = None) -> dict:
    d = _p("string", desc, enum=list(values))
    if default is not None:
        d["default"] = default
    return d


def _a(items: dict, desc: str, **kw: Any) -> dict:
    return _p("array", desc, items=items, **kw)


def _obj(props: Dict[str, dict], required: Tuple[str, ...] = (), **ext: Any) -> dict:
    d: Dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        d["required"] = list(required)
    d.update(ext)
    return d


# result builders: closed objects; _any = documented field without a fixed type (nullable / polymorphic)

def _any(desc: str) -> dict:
    return {"description": desc}


def _ro(props: Dict[str, dict], desc: str = "", required: Tuple[str, ...] = ()) -> dict:
    d = _obj(props, required)
    if desc:
        d["description"] = desc
    return d


def _map(desc: str) -> dict:
    """An open map in a result (keys are data, e.g. HTTP headers); documentation only."""
    return {"type": "object", "description": desc, "additionalProperties": True}


def _res(props: Dict[str, dict], required: Tuple[str, ...] = ()) -> dict:
    return _obj(props, required)


# ---------------------------------------------------------------------------------------------------------------
# shared patterns and fragments

JOB_ID_PATTERN = r"^j_[A-Za-z0-9_-]{6,40}$"
SERVICE_NAME_PATTERN = r"^(?!.*\.\.)[a-z0-9][a-z0-9._-]{0,63}$"     # §9: service name pattern and no ".."
SECRET_NAME_PATTERN = r"^[A-Z_][A-Z0-9_]{0,127}$"
WHICH_PATTERN = r"^[A-Za-z0-9._+-]{1,64}$"
REVISION_PATTERN = r"^(?!.*(\.\.|//))[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$"
AUTH_ENV_PULL_PATTERN = r"^(auto|none|[A-Z_][A-Z0-9_]{0,127})$"
FILE_MODE_PATTERN = r"^0?[0-7]{3}$"

SHELLS = ["auto", "bash", "sh", "zsh", "powershell", "pwsh", "cmd"]
PRESET_NAMES = ["vllm", "sglang", "ollama", "llamacpp", "comfyui", "docker"]

_JOB_ID = _s("Job id as returned by job_start/shell_run, e.g. j_18f3a2c1d4e5.", pattern=JOB_ID_PATTERN)
_SERVICE = _s("Service name (lowercase letters, digits, '.', '_', '-'; max 64 chars).", pattern=SERVICE_NAME_PATTERN)
_SECRET_ENV = _a(_s("Secret name.", pattern=SECRET_NAME_PATTERN),
                 "Names of node secrets to put into the child environment (listed by node.info); values are never "
                 "shown to you.", maxItems=20)
_AUTH_ENV = _s("Name of a secret stored on the node (listed by node.info), sent as 'Authorization: Bearer' only to "
               "the hosts the secret is bound to; the value never reaches you.", pattern=SECRET_NAME_PATTERN)


def _exec_props(with_timeout_merge: bool = False) -> Dict[str, dict]:
    """command/argv/shell/cwd/env/env_unset/secret_env/stdin/login/gpus shared by shell.run and job.start."""
    return {
        "command": _s("Command line for the shell.", minLength=1, maxLength=100000),
        "argv": _a(_s("Argument."), "Run a program directly without a shell: program and arguments.",
                   minItems=1, maxItems=1000),
        "shell": _e(SHELLS, "Shell for `command` (ignored with argv). auto = bash/sh on Linux/macOS, PowerShell "
                            "on Windows.", "auto"),
        "cwd": _s("Working directory (default: the node workdir). Relative paths are relative to the workdir."),
        "env": _a(_s("KEY=VALUE"), "Extra environment variables as KEY=VALUE.", maxItems=100, **{"x-kv": "="}),
        "env_unset": _a(_s("Variable name."), "Environment variable names to remove from the child environment."),
        "secret_env": copy.deepcopy(_SECRET_ENV),
        "stdin": _s("Text sent to the process's standard input.", maxLength=1048576),
        "login": _b("Use a login shell (bash/zsh -l).", False),
        "gpus": _s("GPU selection for the child: \"0,1\", \"all\" or \"none\" (sets CUDA_VISIBLE_DEVICES)."),
    }


def _logs_props(id_key: str, id_schema: dict) -> Dict[str, dict]:
    return {
        id_key: copy.deepcopy(id_schema),
        "offset": _i("Absolute byte offset in the logical log to read from (absent = tail).", minimum=0),
        "tail_lines": _i("Lines to return from the end when offset is absent.", default=100, minimum=1, maximum=5000),
        "max_bytes": _i("Maximum bytes to return.", default=32768, minimum=1024, maximum=1048576),
        "wait": _n("Seconds to block for new data (with offset) or the end of the process.", default=0, minimum=0,
                   maximum=60),
        "stream": _e(["output", "stdout", "stderr"], "Which log: output (merged or stdout), stdout or stderr.",
                     "output"),
        "raw": _b("Skip terminal cleaning (ANSI codes, progress-bar carriage returns).", False),
    }


_LOGS_RESULT_PROPS: Dict[str, dict] = {
    "state": _s("Process state."),
    "text": _s("Log text (cleaned unless raw)."),
    "offset": _i("Offset the text starts at."),
    "next_offset": _i("Offset to pass next time to continue."),
    "size": _i("Logical log size in bytes."),
    "eof": _b("Process finished and next_offset is the end."),
    "skipped_bytes": _i("Bytes skipped because the requested offset was rotated away."),
    "truncated": _b("More data was available than max_bytes."),
}

_PROGRESS = _any("null or {done, total|null, percent|null, rate_bps|null, eta_s|null, note, files_done?, "
                 "files_total?, current?: [<= 4 paths]} (downloads and pulls).")

_JOBINFO_PROPS: Dict[str, dict] = {
    "job_id": _s("Job id."),
    "name": _s("Job name."),
    "kind": _e(["job", "shell", "download", "pull"], "Job kind."),
    "state": _e(["starting", "running", "exited", "failed", "killed", "timeout", "lost"], "Job state."),
    "exit_code": _any("Exit code (integer) or null."),
    "signal": _any("Signal name or null."),
    "pid": _any("Child pid or null."),
    "adopted": _b("The job was adopted after a node restart."),
    "created_at": _s("ISO UTC creation time."),
    "started_at": _any("ISO UTC start time or null."),
    "ended_at": _any("ISO UTC end time or null."),
    "duration_s": _any("Duration in seconds or null."),
    "command": _s("Command (<= 200 chars, redacted)."),
    "cwd": _any("Working directory."),
    "owner": _s("Identity name that started the job."),
    "tags": _a(_s("Tag."), "Job tags."),
    "output_bytes": _i("Total output bytes."),
    "rotated_bytes": _i("Bytes rotated away from the logs."),
    "tail": _s("Cleaned tail of the output."),
    "progress": _PROGRESS,
    "result": _any("null or the job result (download: {path,size,sha256}; pull: §9.9 job result)."),
    "error": _any("Error text or null."),
}

_SUMMARY_PROPS: Dict[str, dict] = {
    "name": _s("Service name."),
    "state": _e(["stopped", "starting", "running", "unhealthy", "stopping", "backoff", "failed"], "Service state."),
    "desired": _s("Desired state (running, stopped, removed)."),
    "health": _e(["none", "unknown", "starting", "healthy", "unhealthy"], "Health state."),
    "pid": _any("Child pid or null."),
    "uptime_s": _any("Seconds since start or null."),
    "restarts": _i("Restarts within the restart window."),
    "port": _any("Port or null."),
    "endpoint": _any("OpenAI-compatible base URL or null."),
    "autostart": _b("Starts after reboot."),
    "last_exit_code": _any("Last exit code or null."),
    "last_error": _any("Last error text or null."),
    "adopted": _b("Adopted after a node restart."),
    "description": _any("Description."),
}

_STATUS_PROPS: Dict[str, dict] = dict(_SUMMARY_PROPS)
_STATUS_PROPS.update({
    "definition": _map("Service definition (env values redacted to keys, secret_env names only)."),
    "defined_by": _any("Identity display name that defined the service."),
    "next_restart_at": _any("ISO UTC time of the next restart (backoff) or null."),
    "started_at": _any("ISO UTC start time or null."),
    "health_detail": _ro({"last_check": _any("ISO UTC time or null."), "latency_ms": _any("Latency or null."),
                          "consecutive_failures": _i("Consecutive failed checks."),
                          "last_error": _any("Last health error or null.")}, "Health check details."),
    "resources": _ro({"pids": _a(_i("pid"), "Process ids."), "rss_total": _any("Resident bytes or null."),
                      "cpu_pct": _any("CPU percent or null."), "gpu_memory_mb": _any("GPU memory or null.")},
                     "Resource usage."),
    "container": _any("null or {name, id|null, state|null, image|null} (docker preset)."),
    "tail": _s("Last log lines (always included when the service is not healthy)."),
})

_NODE_SUMMARY = _ro({
    "name": _s("Node name."), "online": _b("Online."), "mode": _e(["direct", "relay", "self"], "Link mode."),
    "os": _any("OS name."), "os_label": _any("Windows 10 | Windows 11 | macOS | Linux (WSL2) | Linux."),
    "arch": _any("CPU architecture."), "version": _any("Hive version."),
    "gpus": _a(_ro({"name": _s("GPU name."), "memory_total_mb": _any("MiB or null.")}), "GPUs."),
    "gpus_summary": _any("One-line GPU summary."), "tags": _a(_s("Tag."), "Tags."),
    "description": _any("Description."), "wsl": _b("The node runs inside WSL2."),
    "wsl_distros": _a(_s("Distro."), "WSL distros (Windows node)."), "secrets": _a(_s("Name."), "Secret names."),
    "last_seen": _any("ISO UTC time or null."), "last_call_at": _any("ISO UTC time or null."),
    "idle_s": _any("Seconds since the last routed call or null."), "inflight": _i("Calls in flight."),
    "queued": _i("Queued relay tasks."), "identity_changed": _b("Relay node awaiting ack-node."),
    "outdated": _b("Version differs from the hub."), "preset": _any("Policy preset or null (unknown)."),
    "auto_update": _b("relay.auto_update."), "max_timeout": _any("Node exec max_timeout or null."),
    "status": _map("Live status (cpu_pct, mem_pct, gpus …)."),
}, "NodeSummary (§9.10).")


# ---------------------------------------------------------------------------------------------------------------
# the specs

def _specs() -> List[ActionSpec]:
    S: List[ActionSpec] = []

    # ---- 9.2 node and system ------------------------------------------------------------------------------------
    S.append(ActionSpec(
        name="node.info", title="Node information",
        description=("Describe this node: name, OS, user, shells, GPUs, capabilities, proxy/mirror settings, secret "
                     "names (never values), implemented actions, and the policy that applies to your token (roots, "
                     "exec limits, network allow lists, size limits). Call it first on an unknown node. For live "
                     "CPU/memory/disk/GPU usage use system.info."),
        params=_obj({}),
        result=_res({
            "name": _s("Node name."), "node_id": _s("Stable node id."), "version": _s("Hive version."),
            "api": _i("Wire API version (1)."), "hostname": _s("Host name."), "os": _s("OS name."),
            "os_release": _s("OS release."), "arch": _s("CPU architecture."), "python": _s("Python version."),
            "user": _s("User the node runs as."), "is_admin": _b("Runs as root/Administrator."),
            "pid": _i("Node process id."), "workdir": _s("Default working directory."),
            "data_dir": _s("Node data directory."), "default_shell": _any("Default shell or null."),
            "shells": _a(_s("Shell."), "Available shells."),
            "modes": _ro({"listen": _any("host:port or null."), "relay": _any("Hub URL or null."),
                          "gateway": _b("Gateway enabled."), "auto_update": _b("relay.auto_update.")},
                         "Connection modes."),
            "tags": _a(_s("Tag."), "Tags."), "description": _s("Description."),
            "started_at": _s("ISO UTC start time."), "time": _s("ISO UTC now."), "paused": _b("Node is paused."),
            "capabilities": _ro({"psutil": _b("psutil importable."), "nvidia_smi": _b("nvidia-smi found."),
                                 "docker": _b("Docker daemon reachable."),
                                 "docker_gpu": _any("Docker GPU runtime (true/false/null)."),
                                 "wsl": _b("WSL distros present (Windows).")}, "Capabilities."),
            "gpus_summary": _any("One-line GPU summary or null."),
            "gpus": _a(_ro({"name": _s("GPU name."), "memory_total_mb": _any("MiB or null.")}), "GPUs."),
            "platform": _any("Windows only: {spawn_mode, session_id, in_job}; else null."),
            "wsl": _any("null, {distro, networking, systemd} inside WSL2, or {keepalive, distros} on Windows."),
            "network": _ro({"proxy": _any("Effective proxy (redacted) or null."),
                            "hf_endpoint": _any("Hugging Face endpoint or null."),
                            "modelscope_endpoint": _any("ModelScope endpoint."),
                            "models_dir": _any("Default model folder.")}, "Network settings."),
            "secrets": _a(_ro({"name": _s("Secret name."), "hosts": _a(_s("Host."), "Bound hosts."),
                               "exec": _b("May be injected into children."), "source": _s("store|env-import|"
                                                                                          "passthrough."),
                               "set_at": _any("ISO UTC time or null.")}), "Secret names (never values)."),
            "actions": _a(_s("Action name."), "Actions this node implements."),
            "identity": _ro({"name": _s("Token name."), "kind": _s("Token kind."),
                             "classes": _a(_s("Class."), "Classes."), "via": _s("direct|hub|relay.")},
                            "Your identity on this node."),
            "policy": _ro({
                "preset": _any("Policy preset."), "read_only": _b("Read-only node."),
                "classes": _a(_s("Class."), "Your classes."),
                "denied_actions": _a(_s("Action."), "Actions denied to you."),
                "approval_actions": _a(_s("Action."), "Actions that need the owner's approval."),
                "fs_roots": _a(_ro({"path": _s("Root."), "mode": _e(["rw", "ro"], "Mode.")}), "Filesystem roots."),
                "exec": _ro({"enabled": _b("Exec enabled."), "mode": _s("open|allowlist."),
                             "shells": _a(_s("Shell."), "Allowed shells."),
                             "default_timeout": _n("Default timeout (s)."), "max_timeout": _n("Max timeout (s).")},
                            "Exec policy."),
                "net": _ro({"http_allow": _a(_s("Entry."), "http_allow."),
                            "download_allow": _a(_s("Entry."), "download_allow.")}, "Network policy."),
                "io_limits": _ro({"max_read_bytes": _i("Bytes."), "max_write_bytes": _i("Bytes."),
                                  "max_chunk_bytes": _i("Bytes."), "max_output_bytes": _i("Bytes."),
                                  "max_response_bytes": _i("Bytes.")}, "I/O limits."),
                "limits": _ro({"max_concurrent_calls": _i("Calls."), "max_call_seconds": _n("Seconds."),
                               "max_result_bytes": _i("Bytes.")}, "Call limits."),
                "note": _s("Reminder about what shell access means."),
            }, "Policy for your identity."),
        }, ("name", "node_id", "version", "api", "hostname", "os", "arch", "user", "pid", "workdir", "modes",
            "paused", "capabilities", "actions", "identity", "policy")),
        risk="read", idempotent=True, default_timeout=10))

    S.append(ActionSpec(
        name="system.info", title="System information",
        description=("Report OS, CPU, memory, disks, GPUs (VRAM, utilization, GPU processes), network addresses and "
                     "installed software versions; with `which`, look up executables on the node PATH. Use before "
                     "deploying a model (VRAM, disk). Static sections are cached 60 s (refresh=true bypasses). For "
                     "node identity, policy and secret names use node.info."),
        params=_obj({
            "sections": _a(_e(["os", "cpu", "memory", "disk", "gpu", "network", "software", "all"], "Section."),
                           "Sections to report.", default=["os", "cpu", "memory", "disk", "gpu"]),
            "refresh": _b("Bypass the 60 s static cache.", False),
            "which": _a(_s("Executable name.", pattern=WHICH_PATTERN),
                        "Executables to look up on the node PATH (e.g. nvcc, docker).", minItems=1, maxItems=50),
            "versions": _b("With which: probe `--version` of known tools (3 s each).", True),
        }),
        result=_res({
            "os": _ro({
                "system": _s("OS name (Linux, Darwin, Windows)."), "release": _s("OS release."),
                "version": _s("OS version string."), "machine": _s("Machine architecture."),
                "hostname": _s("Host name."), "distro": _any("Linux distribution name or null."),
                "python": _s("Python version."), "boot_time": _any("Boot time (UTC ISO) or null."),
                "uptime_s": _any("Seconds since boot or null."), "timezone": _any("Local time zone or null."),
                "cloud": _ro({"provider": _any("runpod, vast, autodl or null."),
                              "pod_id": _any("Pod/instance id or null.")}, "Cloud GPU provider.",
                             ("provider", "pod_id")),
                "wsl": _any("null, or {distro, version:2, networking:'nat'|'mirrored'|null, systemd:bool} inside WSL."),
                "wsl_distros": _a(_ro({"name": _s("Distro name."), "state": _s("State."),
                                       "version": _any("WSL version.")}, "WSL distro.", ("name", "state", "version")),
                                  "Windows only: installed WSL distros."),
            }, "OS section.", ("system", "release", "version", "machine", "hostname", "distro", "python", "boot_time",
                               "uptime_s", "timezone", "cloud", "wsl")),
            "cpu": _ro({"model": _any("CPU model or null."), "logical": _i("Logical CPUs."),
                        "physical": _any("Physical cores or null."),
                        "load_avg": _any("[1, 5, 15]-minute load average or null."),
                        "percent": _any("CPU percent or null.")}, "CPU section.",
                       ("model", "logical", "physical", "load_avg", "percent")),
            "memory": _ro({"total": _n("Bytes."), "available": _n("Bytes."), "used_pct": _n("Percent used."),
                           "swap_total": _any("Bytes or null."), "swap_used": _any("Bytes or null."),
                           "container_limit": _any("cgroup memory limit in bytes or null.")}, "Memory section.",
                          ("total", "available", "used_pct", "swap_total", "swap_used", "container_limit")),
            "disks": _a(_ro({"mount": _s("Mount point or drive."), "fs": _s("File system type."),
                             "total": _n("Bytes."), "free": _n("Bytes."), "used_pct": _n("Percent used.")},
                            "Disk.", ("mount", "fs", "total", "free", "used_pct")), "Disks."),
            "gpus": _a(_ro({
                "index": _i("GPU index."), "vendor": _s("Vendor (nvidia, apple, amd, …)."), "name": _s("GPU name."),
                "uuid": _any("GPU UUID or null."), "memory_total_mb": _n("Total memory, MiB."),
                "memory_used_mb": _any("Used memory, MiB, or null."),
                "utilization_pct": _any("Utilization percent or null."),
                "temperature_c": _any("Temperature in °C or null."), "power_w": _any("Power draw in W or null."),
                "driver": _any("Driver version or null."), "cuda": _any("CUDA version or null."),
                "unified_memory": _b("True for Apple unified memory."),
                "processes": _a(_ro({"pid": _i("Process id."), "name": _any("Process name or null."),
                                     "memory_mb": _any("GPU memory in MiB or null.")}, "GPU process.",
                                    ("pid", "name", "memory_mb")), "Processes using this GPU."),
            }, "GPU.", ("index", "vendor", "name", "uuid", "memory_total_mb", "memory_used_mb", "utilization_pct",
                        "temperature_c", "power_w", "driver", "cuda", "unified_memory", "processes")), "GPUs."),
            "gpu_backend": _any("GPU backend (nvidia, apple, …) or null."),
            "gpu_note": _any("Note about GPU data or null."),
            "network": _ro({"primary_ip": _any("Primary IP address or null."),
                            "addresses": _a(_s("IP address."), "Local IP addresses."),
                            "tailscale_ip": _any("Tailscale IP or null.")}, "Network section.",
                           ("primary_ip", "addresses", "tailscale_ip")),
            "software": _map("{name: 'version-line @ path' | null}."),
            "which": _ro({"found": _map("{name: {path, version|null, note?}}."),
                          "missing": _a(_s("Name."), "Names not found.")}, "Executable lookup."),
        }),
        risk="read", idempotent=True, default_timeout=30))

    # ---- 9.3 processes ------------------------------------------------------------------------------------------
    S.append(ActionSpec(
        name="proc.list", title="List processes",
        description=("List processes with pid, parent, user, CPU, memory, start time and (redacted) command line, "
                     "sorted by memory by default; rows started by Hive are marked managed (job:… / service:…). "
                     "Use to find what uses memory/GPU or a port's owner. For Hive's own jobs and services prefer "
                     "job.list / service.list."),
        params=_obj({
            "filter": _s("Case-insensitive substring of the process name or command line."),
            "pids": _a(_i("Process id.", minimum=0), "Only these process ids."),
            "user": _s("Only processes of this user."),
            "sort": _e(["memory", "cpu", "pid", "name", "started"], "Sort order.", "memory"),
            "limit": _i("Maximum rows.", default=30, minimum=1, maximum=1000),
            "cmdline": _b("Include command lines (<= 300 chars, redacted).", True),
        }),
        result=_res({
            "processes": _a(_ro({"pid": _i("pid"), "ppid": _any("Parent pid or null."), "name": _s("Name."),
                                 "user": _any("User or null."), "cpu_pct": _any("CPU % or null."),
                                 "mem_bytes": _any("RSS bytes or null."), "started": _any("ISO UTC or null."),
                                 "cmd": _any("Command line or null."),
                                 "managed": _any("null, job:<id> or service:<name>.")}), "Processes."),
            "total": _i("Matching processes."), "shown": _i("Rows returned."),
            "method": _s("psutil|proc|ps|toolhelp."),
        }, ("processes", "total", "shown", "method")),
        risk="read", idempotent=True, default_timeout=30))

    S.append(ActionSpec(
        name="proc.kill", title="Kill a process",
        description=("Stop a process (and by default its child tree): graceful signal first, then force after "
                     "`grace` seconds. Refused for Hive's own processes, jobs and services and for protected system "
                     "processes. For Hive jobs and services use job.stop / service.stop instead (graceful through "
                     "the shim)."),
        params=_obj({
            "pid": _i("Process id to stop.", minimum=1),
            "tree": _b("Also stop the process's descendants.", True),
            "force": _b("Skip the graceful step and kill immediately.", False),
            "grace": _n("Seconds to wait after the graceful signal before forcing.", default=5, minimum=0, maximum=60),
        }, ("pid",)),
        result=_res({"killed": _a(_i("pid"), "Processes that ended."),
                     "still_alive": _a(_i("pid"), "Processes still alive."),
                     "method": _s("killpg|signal|taskkill|taskkill/F|terminate.")},
                    ("killed", "still_alive", "method")),
        risk="exec", destructive=True, default_timeout=30, deadline_params=("grace",)))

    # ---- 9.4 shell ----------------------------------------------------------------------------------------------
    sh = _exec_props()
    sh.update({
        "timeout": _n("Hard limit in seconds (default: the node's exec default, usually 60; max: the node's "
                      "max_timeout).", minimum=1, maximum=86400),
        "yield_after": _n("If still running after this many seconds, return state 'running' with a job_id; the "
                          "process keeps running as a job until timeout.", minimum=1, maximum=86400),
        "on_timeout": _e(["kill", "background"], "At timeout: stop the process tree, or detach it as an unlimited "
                                                 "background job.", "kill"),
        "merge_stderr": _b("Merge stderr into stdout (one `output` field).", True),
        "max_output": _i("Bytes kept per stream (25% head, 75% tail).", default=65536, minimum=1024,
                         maximum=1048576),
    })
    S.append(ActionSpec(
        name="shell.run", title="Run shell command",
        description=("Run a command (or argv without a shell) on the node and wait for it: exit code, output, "
                     "duration. Default shell: bash/sh on Linux/macOS, PowerShell on Windows. A command still "
                     "running after `yield_after` seconds (or near the call deadline) becomes a background job and "
                     "you get its job_id. For long tasks (training, servers, big downloads) use job.start; for "
                     "servers that must stay up use service.define; to fetch model weights use model.pull."),
        params=_obj(sh, **{"x-one-of-required": ["command", "argv"]}),
        result=_res({
            "state": _e(["exited", "timeout", "killed", "running"], "Outcome."),
            "exit_code": _any("Exit code or null."), "signal": _any("Signal name or null."),
            "duration_s": _n("Seconds."), "shell": _any("Shell used."), "cwd": _s("Working directory."),
            "pid": _any("Child pid or null."),
            "output": _s("Merged output (merge_stderr=true)."),
            "stdout": _s("Standard output (merge_stderr=false)."), "stderr": _s("Standard error (merge_stderr=false)."),
            "output_bytes": _i("Total output bytes."), "truncated": _b("Output was cut to max_output."),
            "job_id": _any("Job id when yielded, backgrounded or truncated (full log kept), else null."),
        }, ("state", "exit_code", "duration_s", "truncated", "job_id")),
        risk="exec", destructive=True, long_running=True, open_world=True, default_timeout=15,
        deadline_params=("timeout",)))

    # ---- 9.5 jobs -----------------------------------------------------------------------------------------------
    jp = _exec_props()
    jp.update({
        "name": _s("Job name (default: start of the command).", maxLength=64),
        "timeout": _n("Hard limit in seconds; 0 = unlimited (or the node's jobs.max_runtime).", default=0,
                      minimum=0, maximum=2592000),
        "merge_stderr": _b("Merge stderr into stdout (one log).", True),
        "log_max_mb": _i("Log size cap in MiB (rotated).", default=64, minimum=1, maximum=4096),
        "keep_awake": _b("Keep the machine from sleeping while the job runs (Windows/macOS).", False),
        "tags": _a(_s("Tag."), "Free-form tags.", maxItems=10),
    })
    S.append(ActionSpec(
        name="job.start", title="Start background job",
        description=("Start a long-running command in the background and return immediately with a job_id; the job "
                     "survives node restarts. Follow it with job.status (wait=…) and job.logs. Use for training, "
                     "benchmarks, builds and other long tasks; for short commands use shell.run; for servers that "
                     "must be restarted on crash use service.define."),
        params=_obj(jp, **{"x-one-of-required": ["command", "argv"]}),
        result=_res({"job_id": _s("Job id."), "name": _s("Job name."), "pid": _any("Child pid or null."),
                     "state": _s("running (or failed)."), "started_at": _any("ISO UTC start time."),
                     "log_path": _s("Log file path on the node."),
                     "warnings": _a(_s("Warning."), "Warnings (e.g. keep_awake unsupported).")},
                    ("job_id", "name", "state")),
        risk="exec", open_world=True, default_timeout=30))

    S.append(ActionSpec(
        name="job.status", title="Job status",
        description=("Get a job's state, exit code, duration, progress (downloads/pulls) and output tail; with "
                     "`wait`, block until the job ends or the wait elapses. Use job.logs for more output."),
        params=_obj({"job_id": copy.deepcopy(_JOB_ID),
                     "wait": _n("Seconds to block until the job ends.", default=0, minimum=0, maximum=300),
                     "tail_lines": _i("Output lines to include.", default=20, minimum=0, maximum=500)},
                    ("job_id",)),
        result=_res(copy.deepcopy(_JOBINFO_PROPS), ("job_id", "state", "kind")),
        risk="read", idempotent=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="job.logs", title="Job logs",
        description=("Read a job's output by byte offset (follow with next_offset) or the last lines; `wait` blocks "
                     "for new output. Logs rotate; offsets stay absolute. For the state only use job.status."),
        params=_obj(_logs_props("job_id", _JOB_ID), ("job_id",)),
        result=_res(dict(_LOGS_RESULT_PROPS, job_id=_s("Job id.")),
                    ("job_id", "state", "text", "offset", "next_offset", "size", "eof")),
        risk="read", idempotent=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="job.list", title="List jobs",
        description=("List background jobs, newest first, with state, exit code and duration. Finished jobs are "
                     "pruned automatically after the retention period."),
        params=_obj({"state": _e(["running", "finished", "all"], "Filter by state.", "all"),
                     "limit": _i("Maximum rows.", default=20, minimum=1, maximum=500),
                     "include_shell": _b("Include the short-lived logs kept for truncated shell.run calls.", False)}),
        result=_res({"jobs": _a(_ro({"job_id": _s("Job id."), "name": _s("Name."), "kind": _s("Kind."),
                                     "state": _s("State."), "exit_code": _any("Exit code or null."),
                                     "started_at": _any("ISO UTC or null."), "duration_s": _any("Seconds or null."),
                                     "command": _s("Command (<= 80 chars).")}), "Jobs."),
                     "total": _i("Matching jobs.")}, ("jobs", "total")),
        risk="read", idempotent=True, default_timeout=30))

    S.append(ActionSpec(
        name="job.stop", title="Stop job",
        description=("Stop a background job gracefully (its process tree), forcing after `grace` seconds. Returns the "
                     "job's final info. Use proc.kill only for processes Hive did not start."),
        params=_obj({"job_id": copy.deepcopy(_JOB_ID),
                     "force": _b("Kill immediately without the graceful step.", False),
                     "grace": _n("Seconds between the graceful stop and the forced kill.", default=10, minimum=0,
                                 maximum=300)}, ("job_id",)),
        result=_res({k: v for k, v in copy.deepcopy(_JOBINFO_PROPS).items() if k != "tail"}, ("job_id", "state")),
        risk="exec", destructive=True, idempotent=True, default_timeout=30, deadline_params=("grace",)))

    # ---- 9.6 services -------------------------------------------------------------------------------------------
    sd = _exec_props()
    for k in ("env_unset", "stdin"):
        del sd[k]
    sd = {
        "name": copy.deepcopy(_SERVICE),
        "description": _s("What this service is for.", maxLength=200),
        "preset": _e(PRESET_NAMES, "Build the definition from a preset (guide('presets') lists their args)."),
        "preset_args": _a(_s("key=value"), "Preset arguments as 'key=value' (repeat a key for multi-value args such "
                                           "as volume or extra_arg).", maxItems=50),
        **sd,
        "stop_command": _a(_s("Argument."), "argv run (no shell) when stopping, before any signal, e.g. "
                                            "['docker','compose','down'].", minItems=1, maxItems=100),
        "port": _i("TCP port the service listens on (port preflight, TCP health default, model.* resolution).",
                   minimum=1, maximum=65535),
        "endpoint": _s("OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1."),
        "endpoint_kind": _e(["openai", "ollama"], "API flavour of the endpoint.", "openai"),
        "api_key_env": _s("Name of a node secret (or of a key in this service's env) sent as Bearer by model.*."),
        "health": _e(["auto", "http", "tcp", "none"], "Health check: auto = http if health_url or endpoint, else tcp "
                                                      "if port, else none.", "auto"),
        "health_url": _s("URL for the http health check (default: endpoint + /models)."),
        "health_expect": _a(_i("HTTP status."), "Inclusive range of healthy HTTP statuses [low, high].",
                            default=[200, 399], minItems=2, maxItems=2),
        "ready_pattern": _s("Regex on new log output that also counts as healthy.", maxLength=200),
        "start_period": _n("Seconds allowed for the first successful health check.", default=120, minimum=0,
                           maximum=7200),
        "health_interval": _n("Seconds between health checks.", default=15, minimum=2, maximum=600),
        "health_timeout": _n("Seconds per health check.", default=5, minimum=1, maximum=60),
        "unhealthy_threshold": _i("Consecutive failures before unhealthy.", default=3, minimum=1, maximum=20),
        "restart_on_unhealthy": _b("Restart the service when it becomes unhealthy.", False),
        "restart": _e(["no", "on-failure", "always"], "Restart policy after the process exits.", "on-failure"),
        "restart_delay": _n("Base seconds before a restart (doubles for short runs, max 300).", default=3, minimum=0,
                            maximum=300),
        "max_restarts": _i("Restarts allowed within restart_window before failed.", default=5, minimum=0,
                           maximum=1000),
        "restart_window": _n("Window in seconds for max_restarts.", default=600, minimum=60, maximum=86400),
        "autostart": _b("Start after a reboot or pod restart.", False),
        "stop_timeout": _n("Seconds for a graceful stop before forcing.", default=15, minimum=0, maximum=300),
        "log_max_mb": _i("Log size cap in MiB.", default=64, minimum=1, maximum=4096),
        "log_keep": _i("Rotated log files kept.", default=2, minimum=0, maximum=10),
        "keep_awake": _b("Keep the machine from sleeping while the service runs.", False),
        "force_port": _b("Start even if the port is already in use.", False),
        "replace": _b("Update the service if it exists (false + exists = conflict).", True),
        "start": _b("Start (or restart if the definition changed) after defining.", True),
        "wait": _n("Seconds to wait for the service to come up after starting.", default=0, minimum=0, maximum=600),
    }
    svc_status = _res(copy.deepcopy(_STATUS_PROPS), ("name", "state", "health"))
    S.append(ActionSpec(
        name="service.define", title="Define managed service",
        description=("Create or update a managed service: a long-running process the node supervises (restart on "
                     "crash, health checks, rotating logs, optional autostart after reboot). Give `preset` "
                     "(vllm, sglang, ollama, llamacpp, comfyui, docker) with `preset_args`, or `command`/`argv`. "
                     "Servers bind 127.0.0.1; reach models with model.chat. Starts it by default; follow with "
                     "service.status (wait=…). For one-off commands use shell.run or job.start."),
        params=_obj(sd, ("name",), **{"x-one-of-required": ["command", "argv", "preset"]}),
        result=_res(dict(copy.deepcopy(_STATUS_PROPS), **{
            "created": _b("The service was new."),
            "changed": _a(_s("Field."), "Changed definition fields."),
            "restarted": _b("A running service was restarted for the change."),
            "warnings": _a(_s("Warning."), "Warnings."),
        }), ("name", "state", "created", "changed", "restarted", "definition", "warnings")),
        risk="persist", extra_classes=("exec",), default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="service.list", title="List services",
        description=("List managed services with state, health, pid, uptime, restarts, port and endpoint. Use "
                     "service.status for one service's details and log tail."),
        params=_obj({}),
        result=_res({"services": _a(_ro(copy.deepcopy(_SUMMARY_PROPS)), "Services.")}, ("services",)),
        risk="read", idempotent=True, default_timeout=30))

    S.append(ActionSpec(
        name="service.status", title="Service status",
        description=("Get one service's state, health, definition (env values redacted), resources and log tail; "
                     "with `wait`, block until it is running and healthy, failed, stopped or in backoff (the "
                     "read-only way to wait for a model to load)."),
        params=_obj({"name": copy.deepcopy(_SERVICE),
                     "tail_lines": _i("Log lines to include.", default=30, minimum=0, maximum=500),
                     "wait": _n("Seconds to block until healthy/failed/stopped.", default=0, minimum=0, maximum=600)},
                    ("name",)),
        result=copy.deepcopy(svc_status),
        risk="read", idempotent=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="service.start", title="Start service",
        description=("Start a defined service; with `wait`, block until it is running and healthy (or fails). "
                     "Starting a service that is already starting or running only waits."),
        params=_obj({"name": copy.deepcopy(_SERVICE),
                     "wait": _n("Seconds to wait for running/healthy.", default=0, minimum=0, maximum=600)},
                    ("name",)),
        result=copy.deepcopy(svc_status),
        risk="exec", idempotent=True, long_running=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="service.stop", title="Stop service",
        description=("Stop a service gracefully (stop_command, then signal, then force after stop_timeout) and keep "
                     "its definition; it stays stopped until started again."),
        params=_obj({"name": copy.deepcopy(_SERVICE),
                     "wait": _n("Seconds to wait for the stop.", default=20, minimum=0, maximum=300)}, ("name",)),
        result=copy.deepcopy(svc_status),
        risk="exec", destructive=True, idempotent=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="service.restart", title="Restart service",
        description=("Stop and start a service again (always respawns); with `wait`, block until it is healthy. "
                     "Use after changing files the service reads; service.define restarts automatically when the "
                     "definition changes."),
        params=_obj({"name": copy.deepcopy(_SERVICE),
                     "wait": _n("Seconds to wait for running/healthy.", default=0, minimum=0, maximum=600)},
                    ("name",)),
        result=copy.deepcopy(svc_status),
        risk="exec", destructive=True, long_running=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="service.remove", title="Remove service",
        description=("Stop a service and delete its definition (and logs if asked). If it does not stop within "
                     "`wait`, removal completes in the background (pending=true)."),
        params=_obj({"name": copy.deepcopy(_SERVICE),
                     "delete_logs": _b("Also delete the service's logs.", False),
                     "wait": _n("Seconds to wait for the stop.", default=30, minimum=0, maximum=600)}, ("name",)),
        result=_res({"removed": _s("Service name."), "pending": _b("Removal finishes in the background."),
                     "state": _s("stopping (when pending).")}, ("removed", "pending")),
        risk="persist", destructive=True, default_timeout=30, deadline_params=("wait",)))

    S.append(ActionSpec(
        name="service.logs", title="Service logs",
        description=("Read a service's log by byte offset (follow with next_offset) or the last lines, plus a short "
                     "status. Logs persist across restarts and rotate."),
        params=_obj(_logs_props("name", _SERVICE), ("name",)),
        result=_res(dict(_LOGS_RESULT_PROPS, status=_ro({"state": _s("State."), "health": _s("Health."),
                                                         "pid": _any("pid or null."),
                                                         "restarts": _i("Restarts."),
                                                         "uptime_s": _any("Seconds or null.")}, "Service status.")),
                    ("text", "offset", "next_offset", "size", "eof", "status")),
        risk="read", idempotent=True, default_timeout=30, deadline_params=("wait",)))

    # ---- 9.7 files ----------------------------------------------------------------------------------------------
    S.append(ActionSpec(
        name="fs.list", title="List directory",
        description=("List a directory (optionally recursive up to depth 5) with type, size and mtime, paged. "
                     "Use fs.search to find files by content or name pattern across a tree."),
        params=_obj({
            "path": _s("Directory (default: the node workdir).", default="."),
            "depth": _i("Recursion depth (1 = this directory only).", default=1, minimum=1, maximum=5),
            "pattern": _s("Glob on entry names (directories are still traversed when depth > 1)."),
            "include_hidden": _b("Include dotfiles and hidden files.", False),
            "limit": _i("Maximum entries.", default=200, minimum=1, maximum=2000),
            "offset": _i("Entries to skip (paging).", default=0, minimum=0),
            "sort": _e(["name", "mtime", "size"], "Sort order.", "name"),
        }),
        result=_res({"path": _s("Absolute directory path."),
                     "entries": _a(_ro({"path": _s("Relative path ('/' separators, dirs end with '/')."),
                                        "type": _e(["file", "dir", "link", "other"], "Entry type."),
                                        "size": _any("Bytes or null."), "mtime": _any("ISO UTC or null.")}),
                                   "Entries."),
                     "total": _i("Total entries (capped at 100000)."), "total_capped": _b("total hit the cap."),
                     "next_offset": _any("Offset of the next page or null.")},
                    ("path", "entries", "total", "next_offset")),
        risk="read", idempotent=True, default_timeout=30))

    S.append(ActionSpec(
        name="fs.stat", title="File status",
        description=("Get a path's type, size, times, mode, link target and optionally its sha256; a missing path "
                     "returns exists=false (not an error)."),
        params=_obj({"path": _s("File or directory path."),
                     "sha256": _b("Compute the file's sha256 (slow for big files).", False)}, ("path",)),
        result=_res({"path": _s("Absolute path."), "exists": _b("Path exists."), "type": _s("file|dir|link|other."),
                     "size": _i("Bytes."), "mtime": _s("ISO UTC."), "ctime": _s("ISO UTC."),
                     "mode": _s("Octal mode, e.g. 0o644."), "link_target": _s("Symlink target."),
                     "sha256": _s("Hex sha256."), "readable": _b("Readable."), "writable": _b("Writable.")},
                    ("path", "exists")),
        risk="read", idempotent=True, default_timeout=120))

    S.append(ActionSpec(
        name="fs.read", title="Read text file",
        description=("Read a text file by lines (paged with start_line/next_start_line), with its version (sha256) "
                     "for safe edits. Binary files are refused: use fs.read_chunk or a transfer. To change a file "
                     "prefer fs.edit with the exact text."),
        params=_obj({"path": _s("File path."),
                     "start_line": _i("First line to return (1-based).", default=1, minimum=1),
                     "max_lines": _i("Maximum lines.", default=500, minimum=1, maximum=20000),
                     "max_bytes": _i("Maximum bytes of content.", default=65536, minimum=1024, maximum=8388608),
                     "line_numbers": _b("Prefix each line with its number.", False)}, ("path",)),
        result=_res({"path": _s("Absolute path."), "content": _s("Text."), "start_line": _i("First line."),
                     "end_line": _i("Last line returned."), "total_lines": _any("Line count or null (> 64 MiB)."),
                     "next_start_line": _any("Next page start or null."), "size": _i("File bytes."),
                     "version": _any("sha256 of the whole file or null (> 64 MiB)."),
                     "encoding": _s("Detected encoding."), "long_lines_cut": _i("Lines cut at 2000 chars.")},
                    ("path", "content", "start_line", "end_line", "next_start_line", "size", "version", "encoding")),
        risk="read", idempotent=True, default_timeout=30))

    S.append(ActionSpec(
        name="fs.read_chunk", title="Read binary chunk",
        description=("Read raw bytes (base64) from a file at an offset; for binary files and chunked downloads. "
                     "Copy whole files with a transfer (fs_transfer) instead of looping yourself."),
        params=_obj({"path": _s("File path."),
                     "offset": _i("Byte offset.", default=0, minimum=0),
                     "length": _i("Bytes to read.", default=4194304, minimum=1, maximum=8388608)}, ("path",)),
        result=_res({"data": _s("Base64 bytes."), "offset": _i("Offset read."), "length": _i("Bytes returned."),
                     "size": _i("File size."), "eof": _b("End of file reached."), "mtime": _s("ISO UTC mtime.")},
                    ("data", "offset", "length", "size", "eof")),
        risk="read", idempotent=True, default_timeout=60))

    S.append(ActionSpec(
        name="fs.write", title="Write file",
        description=("Write a whole file (UTF-8 text or base64), atomically; mode overwrite (default), create (fails "
                     "if it exists) or append; if_version guards against concurrent changes. To change part of a "
                     "file prefer fs.edit; for large files use a transfer."),
        params=_obj({"path": _s("File path."), "content": _s("File content (text, or base64 with encoding=base64)."),
                     "encoding": _e(["utf-8", "base64"], "Encoding of content.", "utf-8"),
                     "mode": _e(["overwrite", "create", "append"], "Write mode.", "overwrite"),
                     "if_version": _s("Only write if the file's current version (sha256 from fs.read) matches."),
                     "make_dirs": _b("Create missing parent directories.", True),
                     "file_mode": _s("POSIX permission bits, e.g. 644 or 0755.", pattern=FILE_MODE_PATTERN)},
                    ("path", "content")),
        result=_res({"path": _s("Absolute path."), "bytes": _i("Bytes written."), "version": _s("New sha256."),
                     "created": _b("The file was new.")}, ("path", "bytes", "version", "created")),
        risk="write", destructive=True, default_timeout=60))

    S.append(ActionSpec(
        name="fs.write_chunk", title="Write file chunk",
        description=("Upload a file in base64 chunks to <path>.hive-part (offset must equal the current part size; "
                     "offset 0 starts over) and move it into place with final=true after a sha256 check. Used by "
                     "transfers; for small text files use fs.write."),
        params=_obj({"path": _s("Destination file path."), "data": _s("Base64 chunk (<= 8 MiB decoded)."),
                     "offset": _i("Byte offset of this chunk (= current part size).", minimum=0),
                     "final": _b("Last chunk: verify and move into place.", False),
                     "sha256": _s("Expected sha256 of the whole file (checked at final)."),
                     "total_size": _i("Total file size (checked for free disk space at offset 0).", minimum=0),
                     "overwrite": _b("Replace an existing destination.", False),
                     "make_dirs": _b("Create missing parent directories.", True)}, ("path", "data", "offset")),
        result=_res({"path": _s("Destination path."), "part_bytes": _i("Part file size."),
                     "done": _b("The file is complete and in place."), "sha256": _s("Final sha256.")},
                    ("path", "part_bytes", "done")),
        risk="write", default_timeout=120))

    S.append(ActionSpec(
        name="fs.edit", title="Edit file",
        description=("Replace an exact text snippet in a file (encoding, BOM and line endings preserved). The snippet "
                     "must match exactly once unless replace_all; on no match you get the closest lines. Prefer this "
                     "over rewriting whole files with fs.write."),
        params=_obj({"path": _s("File path."),
                     "old_text": _s("Exact text to replace.", minLength=1, maxLength=1048576),
                     "new_text": _s("Replacement text."),
                     "replace_all": _b("Replace every occurrence.", False),
                     "if_version": _s("Only edit if the file's version (sha256) matches.")},
                    ("path", "old_text", "new_text")),
        result=_res({"path": _s("Absolute path."), "replacements": _i("Replacements made."),
                     "version": _s("New sha256."), "snippet": _s("Edited region with line numbers.")},
                    ("path", "replacements", "version")),
        risk="write", default_timeout=60))

    S.append(ActionSpec(
        name="fs.search", title="Search files",
        description=("Search file contents with a regex (or literal text) under a directory, or match file names "
                     "with names_only; skips binary files and .git/node_modules/venv folders. Returns matches with "
                     "line numbers and context. To list one directory use fs.list."),
        params=_obj({"pattern": _s("Regex (or literal text with literal=true; a glob with names_only=true).",
                                   maxLength=500),
                     "path": _s("Directory to search.", default="."),
                     "glob": _s("Only files whose name matches this glob, e.g. *.py."),
                     "literal": _b("Treat pattern as plain text.", False),
                     "ignore_case": _b("Case-insensitive.", False),
                     "context": _i("Lines of context around matches.", default=0, minimum=0, maximum=5),
                     "max_results": _i("Maximum matches.", default=100, minimum=1, maximum=2000),
                     "include_hidden": _b("Search dotfiles and hidden folders.", False),
                     "max_file_mb": _n("Skip files larger than this (MiB).", default=10, minimum=0.1, maximum=100),
                     "names_only": _b("Match pattern as a glob against file names instead of contents.", False)},
                    ("pattern",)),
        result=_res({"matches": _a(_ro({"file": _s("File path."), "line": _any("Line number or null."),
                                        "text": _any("Matched line or null."),
                                        "before": _a(_s("Line."), "Context before."),
                                        "after": _a(_s("Line."), "Context after."),
                                        "cut": _b("Match within the first 1 KiB of a longer line.")}), "Matches."),
                     "files_scanned": _i("Files scanned."), "files_matched": _i("Files with matches."),
                     "truncated": _b("Stopped early."), "reason": _any("Why it stopped (timeout, max_results) or null."),
                     "elapsed_s": _n("Seconds.")}, ("matches", "files_scanned", "files_matched", "truncated")),
        risk="read", idempotent=True, long_running=True, default_timeout=60))

    S.append(ActionSpec(
        name="fs.mkdir", title="Create directory",
        description="Create a directory (with parents by default); succeeds if it already exists.",
        params=_obj({"path": _s("Directory path."), "parents": _b("Create missing parents.", True)}, ("path",)),
        result=_res({"path": _s("Absolute path."), "created": _b("The directory was new.")}, ("path", "created")),
        risk="write", idempotent=True, default_timeout=30))

    S.append(ActionSpec(
        name="fs.move", title="Move or rename",
        description="Move or rename a file or directory; refuses to replace an existing destination unless overwrite.",
        params=_obj({"src": _s("Source path."), "dst": _s("Destination path."),
                     "overwrite": _b("Replace an existing destination.", False)}, ("src", "dst")),
        result=_res({"src": _s("Source path."), "dst": _s("Destination path.")}, ("src", "dst")),
        risk="write", destructive=True, default_timeout=120))

    S.append(ActionSpec(
        name="fs.copy", title="Copy",
        description=("Copy a file, or a directory with recursive=true, on the same node. To copy between machines "
                     "use a transfer (fs_transfer); to fetch from the internet use net.download or model.pull."),
        params=_obj({"src": _s("Source path."), "dst": _s("Destination path."),
                     "overwrite": _b("Replace existing destination files.", False),
                     "recursive": _b("Copy a directory tree.", False)}, ("src", "dst")),
        result=_res({"src": _s("Source path."), "dst": _s("Destination path."), "files": _i("Files copied."),
                     "bytes": _i("Bytes copied."),
                     "skipped_links": _i("Symbolic links not recreated (Windows without the symlink privilege); "
                                         "present only when non-zero.")}, ("src", "dst", "files", "bytes")),
        risk="write", default_timeout=600))

    S.append(ActionSpec(
        name="fs.delete", title="Delete",
        description=("Delete a file, or a directory with recursive=true. Refuses roots, the home dir, the workdir and "
                     "Hive's data dir. Deleted files cannot be recovered: only delete what you created or were "
                     "asked to."),
        params=_obj({"path": _s("Path to delete."), "recursive": _b("Delete a non-empty directory tree.", False),
                     "missing_ok": _b("Succeed if the path does not exist.", False)}, ("path",)),
        result=_res({"path": _s("Absolute path."), "deleted_files": _i("Files deleted."),
                     "deleted_dirs": _i("Directories deleted.")}, ("path", "deleted_files", "deleted_dirs")),
        risk="write", destructive=True, default_timeout=300))

    # ---- 9.8 network --------------------------------------------------------------------------------------------
    S.append(ActionSpec(
        name="net.http", title="HTTP request",
        description=("Send one HTTP(S) request from the node (local services, APIs) and get status, headers and body "
                     "(4xx/5xx are results, not errors). Targets are limited by the node's network policy; secrets "
                     "go by name in auth_env. To download a file use net.download; to call a local model use "
                     "model.chat."),
        params=_obj({"url": _s("http:// or https:// URL."),
                     "method": _e(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"], "HTTP method.", "GET"),
                     "headers": _a(_s("Name: value"), "Request headers as 'Name: value'.", maxItems=50,
                                   **{"x-kv": ":"}),
                     "body": _s("Request body (text, or base64 with body_base64=true)."),
                     "body_base64": _b("body is base64.", False),
                     "content_type": _s("Content-Type of the body."),
                     "timeout": _n("Seconds.", default=30, minimum=1, maximum=600),
                     "max_bytes": _i("Maximum response body bytes kept.", default=65536, minimum=0, maximum=8388608),
                     "follow_redirects": _b("Follow up to 5 redirects (each re-checked).", True),
                     "auth_env": copy.deepcopy(_AUTH_ENV),
                     "verify_tls": _b("Verify TLS certificates.", True)}, ("url",)),
        result=_res({"status": _i("HTTP status."), "reason": _s("Reason phrase."), "url": _s("Final URL."),
                     "headers": _map("Response headers (<= 50)."), "body": _s("Body text."),
                     "body_base64": _s("Body as base64 (binary)."), "truncated": _b("Body cut at max_bytes."),
                     "bytes": _i("Body bytes received."), "elapsed_s": _n("Seconds."),
                     "route": _e(["direct", "proxy"], "How the node connected.")},
                    ("status", "url", "headers", "truncated", "bytes", "route")),
        risk="net", open_world=True, default_timeout=15, deadline_params=("timeout",)))

    S.append(ActionSpec(
        name="net.download", title="Download file",
        description=("Download one URL to a file on the node in the background (resumable, optional sha256 check) and "
                     "return a job_id; follow with job.status. Secrets go by name in auth_env. For a whole Hugging "
                     "Face / ModelScope repository use model.pull."),
        params=_obj({"url": _s("http:// or https:// URL."),
                     "path": _s("Destination file, or an existing directory (name from the server)."),
                     "headers": _a(_s("Name: value"), "Request headers as 'Name: value'.", maxItems=50,
                                   **{"x-kv": ":"}),
                     "auth_env": copy.deepcopy(_AUTH_ENV),
                     "sha256": _s("Expected sha256 of the file."),
                     "overwrite": _b("Replace an existing file.", False),
                     "resume": _b("Resume a partial download.", True),
                     "make_dirs": _b("Create missing parent directories.", True)}, ("url", "path")),
        result=_res({"job_id": _s("Job id."), "path": _s("Destination."), "state": _s("running.")},
                    ("job_id", "path", "state")),
        risk="net", extra_classes=("write",), open_world=True, long_running=True, default_timeout=30))

    S.append(ActionSpec(
        name="net.ports", title="Check local ports",
        description=("Check which loopback TCP ports are open (with the owning process), or list all listening "
                     "sockets when no ports are given. Loopback only; check remote ports through the shell."),
        params=_obj({"ports": _a(_i("Port.", minimum=1, maximum=65535), "Ports to check on 127.0.0.1.",
                                 maxItems=100),
                     "timeout": _n("Seconds per connection attempt.", default=1.0, minimum=0.1, maximum=10)}),
        result=_res({"ports": _a(_ro({"port": _i("Port."), "open": _b("Open."), "latency_ms": _any("ms or null."),
                                      "pid": _any("pid or null."), "process": _any("Name or null."),
                                      "address": _any("Address or null.")}), "Checked ports."),
                     "listeners": _a(_ro({"proto": _s("Protocol."), "address": _s("Address."), "port": _i("Port."),
                                          "pid": _any("pid or null."), "process": _any("Name or null.")}),
                                     "Listening sockets."),
                     "method": _s("psutil|proc|ss|lsof|netstat|unavailable.")}, ("method",)),
        risk="read", idempotent=True, default_timeout=30))

    # ---- 9.9 models ---------------------------------------------------------------------------------------------
    S.append(ActionSpec(
        name="model.endpoints", title="Model endpoints",
        description=("Find local OpenAI-compatible model servers on the node (managed services, configured endpoints "
                     "and common ports: Ollama 11434, vLLM 8000, llama.cpp 8080, SGLang 30000, LM Studio 1234) and "
                     "their models; with service or port, list that endpoint's models. Use before model.chat."),
        params=_obj({"probe": _b("Probe common local ports.", True),
                     "service": _s("Only this managed service or configured endpoint name.",
                                   pattern=SERVICE_NAME_PATTERN),
                     "port": _i("Only the server on this 127.0.0.1 port.", minimum=1, maximum=65535),
                     "timeout": _n("Seconds per query.", default=10, minimum=1, maximum=60)}),
        result=_res({"endpoints": _a(_ro({"name": _any("Name or null."),
                                          "source": _e(["service", "probe", "config"], "Where it came from."),
                                          "api": _e(["openai", "ollama"], "Protocol."),
                                          "server": _s("vllm|ollama|llamacpp|sglang|lmstudio|openai|unknown."),
                                          "base_url": _s("Base URL."), "healthy": _any("true/false/null."),
                                          "models": _any("Model ids or null.")}), "Endpoints."),
                     "models": _a(_ro({"id": _s("Model id."), "owned_by": _any("Owner or null.")}),
                                  "Models (with service or port).")}, ("endpoints",)),
        risk="read", idempotent=True, default_timeout=20, deadline_params=("timeout",)))

    S.append(ActionSpec(
        name="model.chat", title="Chat with a local model",
        description=("Send a chat request to a model served on the node (by service name, local port or base_url; "
                     "default: the only healthy endpoint) and get the reply, token usage and speed. Use to test a "
                     "deployed model. Give messages or a prompt. For embeddings or other APIs use net.http."),
        params=_obj({"service": _s("Managed service or configured endpoint name.", pattern=SERVICE_NAME_PATTERN),
                     "port": _i("Server on this 127.0.0.1 port (/v1).", minimum=1, maximum=65535),
                     "base_url": _s("OpenAI-compatible base URL, e.g. http://127.0.0.1:8000/v1."),
                     "model": _s("Model id (default: the first from /models)."),
                     "messages": _a(_obj({"role": _e(["system", "user", "assistant"], "Message role."),
                                          "content": _s("Message text.")}, ("role", "content")),
                                    "Chat messages.", minItems=1, maxItems=500),
                     "prompt": _s("Single user message (instead of messages)."),
                     "system": _s("System prompt."),
                     "max_tokens": _i("Maximum tokens to generate.", default=512, minimum=1, maximum=32768),
                     "temperature": _n("Sampling temperature.", minimum=0, maximum=2),
                     "top_p": _n("Nucleus sampling.", minimum=0, maximum=1),
                     "stop": _a(_s("Stop sequence."), "Stop sequences."),
                     "extra_json": _s("JSON object merged into the request body (not model/messages/stream)."),
                     "timeout": _n("Seconds.", default=120, minimum=1, maximum=1800),
                     "max_chars": _i("Maximum reply characters returned.", default=8000, minimum=100, maximum=200000)},
                    **{"x-any-of-required": ["messages", "prompt"]}),
        result=_res({"content": _s("Reply text."), "reasoning": _any("Reasoning text (<= 2000 chars) or null."),
                     "finish_reason": _any("Finish reason."), "model": _s("Model id."),
                     "usage": _any("{prompt_tokens, completion_tokens} or null."), "latency_s": _n("Seconds."),
                     "tokens_per_s": _any("Generation speed or null."), "base_url": _s("Endpoint used.")},
                    ("content", "model", "latency_s", "base_url")),
        risk="net", long_running=True, default_timeout=15, deadline_params=("timeout",)))

    S.append(ActionSpec(
        name="model.pull", title="Pull model repository",
        description=("Download a whole Hugging Face (or ModelScope) repository snapshot into a folder on the node in "
                     "the background: commit-pinned, include/exclude globs, safetensors preferred, resumable, hash "
                     "checked, disk space checked first. Returns a job_id (follow with job.status); dry_run shows "
                     "the plan. Re-run the same call to resume. For a single URL use net.download."),
        params=_obj({"repo": _s("Repository id 'Org/Name' (or its URL).", minLength=1, maxLength=300),
                     "source": _e(["huggingface", "modelscope"], "Where to pull from (endpoint set by the owner).",
                                  "huggingface"),
                     "repo_type": _e(["model", "dataset"], "Repository type.", "model"),
                     "revision": _s("Branch, tag or commit (default main / master).", pattern=REVISION_PATTERN),
                     "path": _s("Target folder (default <models dir>/<Org>--<Name>)."),
                     "include": _a(_s("Glob."), "Only files matching these globs, e.g. ['*.safetensors','*.json'].",
                                   maxItems=50),
                     "exclude": _a(_s("Glob."), "Skip files matching these globs (wins over include).", maxItems=50),
                     "auth_env": _s("Secret name for the token; auto = HF_TOKEN etc. if bound to the endpoint; none = "
                                    "anonymous.", default="auto", pattern=AUTH_ENV_PULL_PATTERN),
                     "prefer_safetensors": _b("Skip pickle weights when safetensors exist.", True),
                     "max_files": _i("Maximum files.", default=1000, minimum=1, maximum=10000),
                     "max_parallel": _i("Parallel file downloads.", default=4, minimum=1, maximum=8),
                     "verify": _e(["fast", "full"], "fast = trust verified manifest entries; full = re-hash all.",
                                  "fast"),
                     "overwrite": _b("Replace existing files that do not match.", False),
                     "dry_run": _b("Only show the plan (files, sizes, disk space).", False)}, ("repo",)),
        result=_res({"job_id": _s("Job id."), "path": _s("Target folder."), "commit": _s("Pinned commit."),
                     "file_count": _i("Files."), "total_bytes": _i("Bytes in the snapshot."),
                     "to_download_bytes": _i("Bytes still to download."), "state": _s("running."),
                     "repo": _s("Repository id (dry_run)."), "source": _s("Source (dry_run)."),
                     "endpoint": _s("Endpoint (dry_run)."), "revision": _s("Revision (dry_run)."),
                     "disk_free": _i("Free bytes (dry_run)."), "fits": _b("Enough disk (dry_run)."),
                     "files": _a(_ro({"path": _s("Path."), "size": _i("Bytes."), "done": _b("Already complete.")}),
                                 "Largest files (dry_run)."),
                     "skipped_invalid": _a(_s("Name."), "Unsafe remote names skipped."),
                     "skipped_pickle": _a(_s("Path."), "Pickle weights skipped."),
                     "note": _s("Note (dry_run).")}, ("path", "file_count", "total_bytes", "to_download_bytes")),
        risk="net", extra_classes=("write",), open_world=True, long_running=True, idempotent=True,
        default_timeout=60))

    S.append(ActionSpec(
        name="model.forward", title="Forward model API call",
        description=("Internal: forward one OpenAI-compatible API call (models, chat/completions, completions, "
                     "embeddings) to a model service on the node; used by the model gateway for relay nodes. "
                     "Agents should use model.chat."),
        params=_obj({"service": _s("Managed service or configured endpoint name.", pattern=SERVICE_NAME_PATTERN),
                     "path": _e(["models", "chat/completions", "completions", "embeddings"], "API path."),
                     "body": _s("JSON text of the request object (required unless path=models).", maxLength=8388608),
                     "timeout": _n("Seconds.", default=300, minimum=1, maximum=1800)}, ("service", "path")),
        result=_res({"status": _i("Upstream HTTP status."), "content_type": _any("Content-Type or null."),
                     "body": _s("Response text."), "truncated": _b("Body was cut."), "elapsed_s": _n("Seconds.")},
                    ("status", "body", "truncated")),
        risk="net", long_running=True, default_timeout=15, deadline_params=("timeout",)))

    # ---- 9.10 hub -----------------------------------------------------------------------------------------------
    S.append(ActionSpec(
        name="hub.nodes", title="List nodes",
        description=("List the machines (nodes) this token may use, with online state, OS, GPUs, tags, secret names, "
                     "idle time and live status. Call first to pick a node; then node.info for one node's details."),
        params=_obj({"include_offline": _b("Include offline nodes.", True)}),
        result=_res({"nodes": _a(copy.deepcopy(_NODE_SUMMARY), "Nodes.")}, ("nodes",)),
        risk="read", idempotent=True, default_timeout=10, scope="hub"))

    return S


def _add_masked(spec: ActionSpec) -> ActionSpec:
    props = spec.result.setdefault("properties", {})
    props["masked"] = {"type": "boolean",
                       "description": "Present (true) when node secret values in this result were replaced by "
                                      "«secret:NAME» placeholders."}
    spec.result["additionalProperties"] = False
    return spec


SPECS: Dict[str, ActionSpec] = {s.name: _add_masked(s) for s in _specs()}

REMOVED: Dict[str, str] = {
    "system.which": "use system.info with which=[names]",
    "system.env": "removed; run a shell command (env / Get-ChildItem Env:) with shell.run when exec is allowed",
    "job.remove": "removed; finished jobs are pruned automatically (job.list shows them)",
    "service.from_preset": "use service.define with preset and preset_args",
    "service.presets": "use service.define with preset; guide('presets') lists presets and their args",
    "model.list": "use model.endpoints with service or port",
    "model.embed": "use net.http to POST /v1/embeddings on the local model server",
    "hub.transfer": "use the client-side transfer (fs_transfer, hive push/pull); fetch model weights with model.pull",
    "hub.transfer_status": "use the client-side transfer (fs_transfer status) instead",
}


def get(name: str) -> ActionSpec:
    """The spec for ``name``; HiveError not_found with did-you-mean (details.suggestions) or a replacement hint."""
    spec = SPECS.get(name)
    if spec is not None:
        return spec
    if name in REMOVED:
        raise HiveError("not_found", f"action '{name}' was removed", {"removed": True, "suggestions": []},
                        hint=REMOVED[name])
    sugg = difflib.get_close_matches(str(name), list(SPECS), n=3, cutoff=0.6)
    hint = ("did you mean " + " or ".join(f"'{s}'" for s in sugg) + "?") if sugg else \
        "list the available actions with GET /v1/actions (list_actions)"
    raise HiveError("not_found", f"unknown action '{name}'", {"suggestions": sugg}, hint=hint)


_PUBLIC_KEYS = ("name", "title", "description", "params", "result", "risk", "classes_required", "mutating",
                "destructive", "idempotent", "open_world", "long_running", "default_timeout", "scope", "since")


def public_dict(spec: ActionSpec) -> dict:
    """Wire form of a spec (GET /v1/actions rows)."""
    d: Dict[str, Any] = {}
    for k in _PUBLIC_KEYS:
        v = getattr(spec, k)
        if k == "classes_required":
            v = sorted(v)
        elif k in ("params", "result"):
            v = copy.deepcopy(v)
        d[k] = v
    return d


def catalog_hash() -> str:
    """sha256 of the canonical JSON of all public dicts (catalog order)."""
    return sha256_text(canonical_json([public_dict(s) for s in SPECS.values()]))


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip())
        except ValueError:
            return None
    else:
        return None
    if f != f or f in (float("inf"), float("-inf")):
        return None
    return f


def deadline(spec: ActionSpec, params: Optional[dict], timeout: Optional[float] = None, *,
             max_seconds: float = 3700.0) -> float:
    """The one deadline formula (§6.4): timeout, else default_timeout + Σ deadline params; clamped to [1, max]."""
    t = _num(timeout)
    if t is None:
        t = float(spec.default_timeout)
        props = (spec.params or {}).get("properties", {})
        p = params if isinstance(params, dict) else {}
        for name in spec.deadline_params:
            v = _num(p.get(name))
            if v is None:
                dflt = _num((props.get(name) or {}).get("default"))
                v = dflt if dflt is not None else (60.0 if name == "timeout" else 0.0)
            t += v
    return max(1.0, min(float(max_seconds), t))
