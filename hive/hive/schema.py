"""JSON-schema subset validator, lenient coercion, export stripping and strict-friendliness lint (design §4.4, Phase 0).

Supported keywords: ``type`` (a single string), ``properties``, ``required``, ``additionalProperties`` (bool, or
validator-only a schema every extra key's value must match; ``lint_schema`` rejects that form in catalog/tool schemas),
``items``, ``enum``, ``const``, ``default``, ``minimum``, ``maximum``, ``minLength``, ``maxLength``, ``pattern``,
``minItems``, ``maxItems``, ``description``, ``title``.
Validator-only extension keywords (removed on export and restated in the description): ``x-kv`` ("=" or ":"),
``x-one-of-required``, ``x-any-of-required``. A schema without ``type`` accepts any JSON value (used by the
documentation-only result schemas of catalog.py for nullable/polymorphic fields).
"""
from __future__ import annotations

import copy
import difflib
import math
import functools
import re
from typing import Any, Dict, List, Optional, Tuple

from .errors import HiveError, bad_request


@functools.lru_cache(maxsize=512)
def ecma_regex(pattern: str) -> "re.Pattern[str]":
    """Compile a JSON Schema ``pattern`` with ECMA-262 end-of-input semantics (§4.4).

    Python's ``$`` also matches before a trailing ``"\\n"``; ECMA-262 (no ``m`` flag) does not. Every unescaped ``$``
    outside a character class is rewritten to ``\\Z``, so ``"foo\\n"`` fails ``^[a-z]+$`` as it does in JSON Schema.
    """
    out: List[str] = []
    i, n, in_class = 0, len(pattern), False
    while i < n:
        c = pattern[i]
        if c == "\\" and i + 1 < n:
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            out.append(c)
            i += 1
            # a "]" (after an optional "^") right after "[" is a literal in Python
            if i < n and pattern[i] == "^":
                out.append("^")
                i += 1
            if i < n and pattern[i] == "]":
                out.append("]")
                i += 1
            continue
        elif c == "$":
            out.append(r"\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return re.compile("".join(out))


EXPORT_FORMATS = ("mcp", "anthropic", "openai", "openai-responses")

SUPPORTED_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum", "const", "default", "minimum",
    "maximum", "minLength", "maxLength", "pattern", "minItems", "maxItems", "description", "title",
    "x-kv", "x-one-of-required", "x-any-of-required",
})
_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")

# common LLM spellings -> canonical parameter (used for did-you-mean when the canonical name exists)
_ALIASES: Dict[str, Tuple[str, ...]] = {
    "cwd": ("working_dir", "workdir", "working_directory", "workingdir", "directory", "dir_path", "cwd_path"),
    "command": ("cmd", "commandline", "command_line", "script"),
    "argv": ("args", "arguments"),
    "path": ("file", "filepath", "file_path", "filename", "file_name"),
    "timeout": ("timeout_s", "timeout_sec", "timeout_seconds", "time_limit"),
    "content": ("text", "contents", "data_text"),
    "env": ("environment", "env_vars", "envs"),
}

_TRUE = ("true", "1", "yes")
_FALSE = ("false", "0", "no")


class _Absent:
    pass


ABSENT = _Absent()


def _suggest(name: str, choices: List[str]) -> Optional[str]:
    low = name.lower()
    for canon, aliases in _ALIASES.items():
        if canon in choices and (low in aliases or low == canon):
            return canon
    for c in choices:
        if c.lower() == low:
            return c
    m = difflib.get_close_matches(name, choices, n=1, cutoff=0.6)
    return m[0] if m else None


def _range_text(schema: dict, what: str = "") -> str:
    lo, hi = schema.get("minimum"), schema.get("maximum")
    if lo is not None and hi is not None:
        return f"between {lo} and {hi}"
    if lo is not None:
        return f">= {lo}"
    return f"<= {hi}"


class _V:
    def __init__(self, coerce: bool, defaults: bool):
        self.coerce = coerce
        self.defaults = defaults
        self.errors: List[Dict[str, str]] = []

    def err(self, path: str, msg: str) -> None:
        self.errors.append({"path": path, "message": msg})

    # -----------------------------------------------------------------------------------------------------------
    def check(self, schema: dict, value: Any, path: str, top: bool = False) -> Any:
        if not isinstance(schema, dict):
            return value
        t = schema.get("type")
        if "const" in schema:
            value = self._enum_const(schema, [schema["const"]], value, path, const=True)
            return value
        if t is None:
            out = copy.deepcopy(value)
            if "enum" in schema:
                out = self._enum_const(schema, schema["enum"], out, path)
            return out
        if t == "object":
            return self._object(schema, value, path, top)
        if t == "array":
            return self._array(schema, value, path)
        if t == "string":
            return self._string(schema, value, path)
        if t in ("integer", "number"):
            return self._number(schema, value, path, t)
        if t == "boolean":
            return self._boolean(schema, value, path)
        if t == "null":
            if value is not None:
                self.err(path, "must be null")
            return None
        self.err(path, f"unsupported schema type {t!r}")
        return value

    def _enum_const(self, schema: dict, allowed: List[Any], value: Any, path: str, const: bool = False) -> Any:
        for a in allowed:
            if isinstance(a, str) and isinstance(value, str):
                if a.lower() == value.lower():
                    return a
            elif type(a) is type(value) and a == value:
                return a
            elif isinstance(a, (int, float)) and not isinstance(a, bool) and isinstance(value, (int, float)) \
                    and not isinstance(value, bool) and a == value:
                return a
        if const:
            self.err(path, f"must be {allowed[0]!r}")
        else:
            self.err(path, "must be one of: " + ", ".join(str(a) for a in allowed))
        return value

    # -----------------------------------------------------------------------------------------------------------
    def _object(self, schema: dict, value: Any, path: str, top: bool) -> Any:
        if not isinstance(value, dict):
            self.err(path, "must be an object")
            return value
        props: Dict[str, dict] = schema.get("properties") or {}
        required = list(schema.get("required") or ())
        addl = schema.get("additionalProperties", True)
        noun = "parameter" if top else "property"
        out: Dict[str, Any] = {}
        for key, v in value.items():
            if not isinstance(key, str):
                self.err(path, f"invalid {noun} name {key!r}")
                continue
            sub = f"{path}.{key}"
            if key in props:
                if v is None and props[key].get("type") not in (None, "null"):
                    continue  # null = absent (a required one is reported as missing below); untyped keeps null
                out[key] = self.check(props[key], v, sub)
            elif isinstance(addl, dict):
                out[key] = self.check(addl, v, sub)
            elif addl is False:
                hint = _suggest(key, list(props))
                self.err(sub, f"unknown {noun} '{key}'" + (f" (did you mean '{hint}'?)" if hint else ""))
            else:
                out[key] = copy.deepcopy(v)
        for key in required:
            if key not in out and value.get(key) is None:
                self.err(f"{path}.{key}", f"missing required {noun} '{key}'")
        if self.defaults:
            for key, ps in props.items():
                if key not in out and isinstance(ps, dict) and "default" in ps:
                    out[key] = copy.deepcopy(ps["default"])
        one = schema.get("x-one-of-required")
        if one:
            given = [n for n in one if value.get(n) is not None]
            if len(given) != 1:
                if not given:
                    self.err(path, "exactly one of: " + ", ".join(one) + " is required")
                else:
                    self.err(path, "give only one of: " + ", ".join(one) + f" (got {', '.join(given)})")
        anyof = schema.get("x-any-of-required")
        if anyof and not any(value.get(n) is not None for n in anyof):
            self.err(path, "at least one of: " + ", ".join(anyof) + " is required")
        return out

    def _array(self, schema: dict, value: Any, path: str) -> Any:
        items = schema.get("items") if isinstance(schema.get("items"), dict) else None
        kv = schema.get("x-kv")
        if not isinstance(value, list):
            if not self.coerce:
                self.err(path, "must be an array")
                return value
            if kv and isinstance(value, dict):
                sep = "=" if kv == "=" else ": "
                value = [f"{k}{sep}{v}" for k, v in value.items()]
            elif isinstance(value, tuple):
                value = list(value)
            elif isinstance(value, str) and items and items.get("type") in ("integer", "number") and "," in value:
                value = [p.strip() for p in value.split(",") if p.strip()]
            elif isinstance(value, dict) and not (items and items.get("type") == "object"):
                self.err(path, "must be an array")
                return value
            else:
                value = [value]
        out = []
        for i, v in enumerate(value):
            sub = f"{path}[{i}]"
            nv = self.check(items, v, sub) if items else copy.deepcopy(v)
            if kv and isinstance(nv, str):
                sep = "=" if kv == "=" else ":"
                k = nv.split(sep, 1)[0].strip() if sep in nv else ""
                if not k:
                    self.err(sub, f"expected 'KEY{sep}VALUE'" if sep == "=" else "expected 'Name: value'")
            out.append(nv)
        if "minItems" in schema and len(out) < schema["minItems"]:
            self.err(path, f"must have at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(out) > schema["maxItems"]:
            self.err(path, f"must have at most {schema['maxItems']} item(s) (got {len(out)})")
        return out

    def _string(self, schema: dict, value: Any, path: str) -> Any:
        if not isinstance(value, str):
            self.err(path, "must be a string")
            return value
        if "enum" in schema:
            return self._enum_const(schema, schema["enum"], value, path)
        if "minLength" in schema and len(value) < schema["minLength"]:
            self.err(path, f"must be at least {schema['minLength']} character(s) long")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            self.err(path, f"must be at most {schema['maxLength']} characters long (got {len(value)})")
        pat = schema.get("pattern")
        if pat:
            try:
                ok = ecma_regex(pat).search(value) is not None
            except (re.error, TypeError):
                ok = False
            if not ok:
                self.err(path, f"does not match pattern {pat}")
        return value

    def _number(self, schema: dict, value: Any, path: str, t: str) -> Any:
        noun = "an integer" if t == "integer" else "a number"
        if isinstance(value, bool):
            self.err(path, f"must be {noun} (got a boolean)")
            return value
        v: Any = value
        if isinstance(v, str) and self.coerce:
            s = v.strip()
            try:
                v = int(s) if re.fullmatch(r"[+-]?\d+", s) else float(s)
            except ValueError:
                self.err(path, f"must be {noun}")
                return value
        if not isinstance(v, (int, float)):
            self.err(path, f"must be {noun}")
            return value
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            self.err(path, f"must be a finite number")
            return value
        if t == "integer" and not isinstance(v, int):
            if self.coerce and float(v).is_integer():
                v = int(v)
            else:
                self.err(path, f"must be {noun}")
                return value
        if "enum" in schema:
            return self._enum_const(schema, schema["enum"], v, path)
        lo, hi = schema.get("minimum"), schema.get("maximum")
        if (lo is not None and v < lo) or (hi is not None and v > hi):
            self.err(path, f"must be {_range_text(schema)} (got {v})")
        return v

    def _boolean(self, schema: dict, value: Any, path: str) -> Any:
        if isinstance(value, bool):
            return value
        if self.coerce and isinstance(value, str):
            s = value.strip().lower()
            if s in _TRUE:
                return True
            if s in _FALSE:
                return False
        self.err(path, "must be a boolean")
        return value


def validate(schema: dict, value: Any, *, coerce: bool = True, path: str = "params", defaults: bool = True) -> Any:
    """Validate ``value``; return a normalized deep copy (defaults applied unless ``defaults=False``).

    Raises HiveError bad_request with ``details={"errors":[{"path","message"}...], "valid_params":[...]}`` and a hint,
    reporting ALL errors at once.
    """
    v = _V(coerce=coerce, defaults=defaults)
    if value is None and isinstance(schema, dict) and schema.get("type") == "object":
        value = {}
    out = v.check(schema, value, path, top=True)
    if v.errors:
        props = list((schema.get("properties") or {}).keys()) if isinstance(schema, dict) else []
        first = v.errors[0]
        if len(v.errors) == 1:
            msg = f"invalid {path}: {first['path']}: {first['message']}"
        else:
            msg = f"{len(v.errors)} invalid {path}: " + "; ".join(f"{e['path']}: {e['message']}" for e in v.errors[:5])
            if len(v.errors) > 5:
                msg += f"; … {len(v.errors) - 5} more"
        hint = ("valid parameters: " + ", ".join(props)) if props else None
        raise HiveError("bad_request", msg, {"errors": v.errors, "valid_params": props}, hint=hint)
    return out


# ---------------------------------------------------------------------------------------------------------------
# export

def _restate(desc: Optional[str], extra: str) -> str:
    desc = (desc or "").rstrip()
    if not desc:
        return extra + "."
    if not desc.endswith("."):
        desc += "."
    return f"{desc} ({extra})."


def _strip(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_strip(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out: Dict[str, Any] = {}
    notes: List[str] = []
    for k, v in schema.items():
        if k.startswith("x-"):
            if k == "x-one-of-required":
                notes.append("exactly one of: " + ", ".join(v))
            elif k == "x-any-of-required":
                notes.append("at least one of: " + ", ".join(v))
            elif k == "x-kv":
                notes.append("list of 'KEY=VALUE' strings" if v == "=" else "list of 'Name: value' strings")
            continue
        if k == "properties" and isinstance(v, dict):
            out[k] = {pk: _strip(pv) for pk, pv in v.items()}
        elif k in ("items", "additionalProperties") and isinstance(v, dict):
            out[k] = _strip(v)
        else:
            out[k] = copy.deepcopy(v)
    if notes:
        out["description"] = _restate(out.get("description"), "; ".join(notes))
    return out


def export_schema(schema: dict, fmt: str) -> dict:
    """Non-strict export for ``mcp|anthropic|openai|openai-responses``: x-* keywords removed and restated."""
    if fmt not in EXPORT_FORMATS:
        raise bad_request(f"unknown export format {fmt!r}", hint="one of: " + ", ".join(EXPORT_FORMATS))
    out = _strip(schema)
    if out.get("type") == "object":
        out.setdefault("properties", {})
    return out


# ---------------------------------------------------------------------------------------------------------------
# lint (P0, catalog and tool PARAM schemas)

_FORBIDDEN = ("anyOf", "oneOf", "allOf", "not", "$ref", "$defs", "definitions", "patternProperties", "if", "then",
              "else")


def _lint_keywords(schema: dict, path: str, errs: List[str]) -> None:
    for k in schema:
        if k in _FORBIDDEN:
            errs.append(f"{path}: keyword '{k}' is not allowed")
        elif k not in SUPPORTED_KEYWORDS:
            errs.append(f"{path}: unsupported keyword '{k}'")
    t = schema.get("type")
    if isinstance(t, list):
        errs.append(f"{path}: type arrays are not allowed")
    elif t is not None and t not in _TYPES:
        errs.append(f"{path}: unknown type {t!r}")
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        errs.append(f"{path}: enum must be a non-empty list")
    if "pattern" in schema:
        try:
            re.compile(schema["pattern"])
        except (re.error, TypeError):
            errs.append(f"{path}: invalid pattern")
    if "x-kv" in schema and schema["x-kv"] not in ("=", ":"):
        errs.append(f"{path}: x-kv must be '=' or ':'")
    if "default" in schema:
        try:
            validate(schema, schema["default"], coerce=False, path=path, defaults=False)
        except HiveError as e:
            errs.append(f"{path}: default does not validate ({e.message})")


def _lint_object(schema: dict, path: str, errs: List[str], nested: bool) -> None:
    props = schema.get("properties")
    if not isinstance(props, dict):
        errs.append(f"{path}: object without properties (free object)")
        props = {}
    addl = schema.get("additionalProperties")
    if isinstance(addl, dict):
        errs.append(f"{path}: additionalProperties as a schema is validator-only (not allowed here)")
    elif addl is not False:
        errs.append(f"{path}: additionalProperties must be false")
    for r in schema.get("required") or ():
        if r not in props:
            errs.append(f"{path}: required property '{r}' is not defined")
    for kw in ("x-one-of-required", "x-any-of-required"):
        for r in schema.get(kw) or ():
            if r not in props:
                errs.append(f"{path}: {kw} names unknown property '{r}'")
    for name, ps in props.items():
        p = f"{path}.{name}"
        if not isinstance(ps, dict):
            errs.append(f"{p}: property schema must be an object")
            continue
        _lint_keywords(ps, p, errs)
        if not str(ps.get("description") or "").strip():
            errs.append(f"{p}: missing description")
        if "type" not in ps and "const" not in ps:
            errs.append(f"{p}: missing type")
        t = ps.get("type")
        if t == "object":
            errs.append(f"{p}: nested objects are not allowed (objects only as array items)")
        elif t == "array":
            items = ps.get("items")
            if not isinstance(items, dict):
                errs.append(f"{p}: array without items schema")
                continue
            _lint_keywords(items, p + "[]", errs)
            it = items.get("type")
            if it == "object":
                if nested:
                    errs.append(f"{p}[]: objects nested more than one level")
                else:
                    _lint_object(items, p + "[]", errs, nested=True)
            elif it == "array":
                errs.append(f"{p}[]: arrays of arrays are not allowed")
            elif it is None:
                errs.append(f"{p}[]: missing item type")


def lint_schema(schema: dict) -> List[str]:
    """Strict-friendliness lint of a catalog/tool PARAM schema: [] when clean (§4.4)."""
    errs: List[str] = []
    if not isinstance(schema, dict):
        return ["schema must be an object"]
    _lint_keywords(schema, "$", errs)
    if schema.get("type") != "object":
        errs.append("$: root type must be object")
        return errs
    _lint_object(schema, "$", errs, nested=False)
    return errs
