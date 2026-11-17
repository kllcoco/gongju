"""Service presets (vllm, sglang, ollama, llamacpp, comfyui, docker), expand_define, approval_review
(design §10.6 and §5.6, WP2).

Phase 0: ``Preset`` and ``PresetCtx`` (incl. ``is_local_model``/``manifest_repo``) are REAL; ``approval_review`` is a
stub returning the pinned §5.6 review shape ``{"action","lines","changes_what_runs","files":[],"note"?}`` with the
params (env values and the full stdin shown, v5 rule) so WP1/WP5 approval tests work before WP2 merges.
``PRESETS`` is empty and ``build``/``expand_define`` are unsupported until WP2 merges.
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, AbstractSet, Callable, Dict, List, Optional, Tuple, Union

from .errors import HiveError, unsupported
from .util import escape_terminal

if TYPE_CHECKING:  # pragma: no cover
    from .policy import EffectivePolicy

# env keys whose values change what program runs (§5.6 "changes what runs" header)
RISKY_ENV_PATTERNS = ("PATH", "LD_*", "DYLD_*", "BASH_ENV", "ENV", "PYTHON*", "NODE_OPTIONS", "PERL5*", "RUBYOPT",
                      "JAVA_TOOL_OPTIONS", "GIT_*")
HUB_DEFINE_NOTE = ("preset is expanded on the node; for vllm/sglang the node checks whether model is a local folder "
                   "(served name from its .hive-pull.json, no token) or a repo id (may add secret_env HF_TOKEN)")
_MANIFEST_MAX = 1 << 20


@dataclass(frozen=True)
class Preset:
    """One service preset (§10.6)."""
    name: str
    description: str
    args: List[dict]                  # [{"name","required","default","description","multi":bool}]
    build: Callable[[Dict[str, Union[str, List[str]]], str, "PresetCtx"], dict]
    example: str
    platforms: Tuple[str, ...] = ("linux", "darwin", "windows")


PRESETS: Dict[str, Preset] = {}       # WP2 fills vllm, sglang, ollama, llamacpp, comfyui, docker


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


def build(preset: str, args: List[Tuple[str, str]], name: str, *, ctx: PresetCtx) -> dict:
    """Build a service definition from preset args (WP2)."""
    raise unsupported("presets.build not available yet (WP2)")


def expand_define(params: dict, *, ctx: PresetCtx, given: AbstractSet[str]) -> dict:
    """Full service definition from validated service.define params (WP2)."""
    raise unsupported("presets.expand_define not available yet (WP2)")


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


def approval_review(action: str, params: dict, *, ctx: Optional[PresetCtx], given: AbstractSet[str] = frozenset(),
                    service: Optional[dict] = None) -> dict:
    """The approval review in the pinned §5.6 shape (P0 stub: params lines, env values, full stdin, files: [])."""
    params = params or {}
    secret_re = _secret_key_re()
    env = _env_pairs(params.get("env"))
    changes: List[str] = []
    for k, _v in env:
        if any(fnmatch.fnmatchcase(k.upper(), p) for p in RISKY_ENV_PATTERNS) and k not in changes:
            changes.append(k)
    note = HUB_DEFINE_NOTE if (action == "service.define" and ctx is None) else None
    lines: List[str] = []
    if changes:
        lines.append("changes what runs: " + ", ".join(changes))
    if note:
        lines.append("note: " + note)
    lines.append(f"action: {action}")
    shows_env = action in ("shell.run", "job.start", "service.define")
    for key, value in params.items():
        if key == "env":
            for k, v in env:
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
    if service and isinstance(service, dict):
        lines.append("stored definition: " + json.dumps({k: service[k] for k in ("argv", "cwd", "defined_by")
                                                          if k in service}, ensure_ascii=False))
    review: Dict[str, object] = {"action": action, "lines": [escape_terminal(x) for x in lines],
                                 "changes_what_runs": changes, "files": []}
    if note:
        review["note"] = note
    return review


__all__ = ["Preset", "PRESETS", "PresetCtx", "build", "expand_define", "approval_review", "RISKY_ENV_PATTERNS",
           "HUB_DEFINE_NOTE"]
