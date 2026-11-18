"""Encoding detection, line paging, edit matching, search walker, check_regex, part tracking (design §11.1, WP3).

Used by the fs.* handlers (``actions/files.py``) and by WP2 (``check_regex`` for service ``ready_pattern``). Every
function raises only ``HiveError``; OS errors are mapped by the handlers (``os_errors`` below, §11.1 mapping).
"""
from __future__ import annotations

import codecs
import contextlib
import difflib
import errno
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from . import util
from .errors import HiveError, bad_request, conflict, denied, not_found, too_large, unsupported

if TYPE_CHECKING:  # pragma: no cover
    from .policy import EffectivePolicy

log = logging.getLogger("hive.fsops")

HEAD_BYTES = 8192                      # binary sniffing / encoding detection window
WHOLE_FILE_MAX = 64 << 20              # total_lines + version only up to this size (§9.7)
LONG_LINE = 2000                       # lines longer than this are cut with "…[+N chars]"
REGEX_MAX_LEN = 200
LITERAL_MAX_LEN = 500
MAX_UNBOUNDED = 4
MAX_GROUP_REPEAT = 10                  # a repeated group holding a quantifier/'|' may repeat at most this often
O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
O_BINARY = getattr(os, "O_BINARY", 0)

# ``mask(kept, rest) -> (kept', masked)``: masks secret values cut by the end of ``kept`` (``rest`` follows it)
EdgeMask = Callable[[str, str], Tuple[str, bool]]

_LINE_RE = re.compile(r"[^\r\n]*(?:\r\n|\r|\n)|[^\r\n]+\Z")
_EOL_RE = re.compile(r"(?:\r\n|\r|\n)\Z")


# ---------------------------------------------------------------------------------------------------------------
# OS error mapping (§11.1)

@contextlib.contextmanager
def os_errors(path: Any = "") -> Iterator[None]:
    """Map OS errors to HiveErrors: FileNotFound -> not_found, Permission -> policy_denied rule os_permission,
    FileExists -> conflict, IsADirectory/NotADirectory -> bad_request, ENOSPC -> too_large "disk full"."""
    try:
        yield
    except HiveError:
        raise
    except FileNotFoundError as e:
        raise not_found(f"path '{e.filename or path}' does not exist")
    except FileExistsError as e:
        raise conflict(f"path '{e.filename or path}' already exists")
    except PermissionError as e:
        raise denied(f"the operating system refused access to '{e.filename or path}'", rule="os_permission",
                     hint="the node's OS user lacks permission for this path")
    except IsADirectoryError as e:
        raise bad_request(f"'{e.filename or path}' is a directory")
    except NotADirectoryError as e:
        raise bad_request(f"'{e.filename or path}' is not a directory (or a parent is a file)")
    except OSError as e:
        if e.errno == errno.ENOSPC:
            raise too_large("disk full", hint="disk full", path=str(path))
        if e.errno == errno.ENAMETOOLONG:
            raise bad_request(f"path name too long: '{path}'")
        if e.errno == errno.ENOTEMPTY:
            raise conflict(f"directory '{e.filename or path}' is not empty")
        if e.errno == errno.ELOOP:
            raise denied(f"'{e.filename or path}' is a symbolic link", rule="symlink",
                         hint="writes never follow a symbolic link in the last path component")
        raise bad_request(f"file operation on '{path}' failed: {e.strerror or e}")


# ---------------------------------------------------------------------------------------------------------------
# encodings

def detect_encoding(head: bytes) -> Tuple[str, bool]:
    """("utf-8"|"utf-8-sig"|"utf-16-le"|"utf-16-be"|"locale", is_binary) from the first bytes of a file."""
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", False
    if head.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", False
    if head.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", False
    sample = head[:HEAD_BYTES]
    if b"\x00" in sample:
        return "utf-8", True
    try:
        codecs.getincrementaldecoder("utf-8")().decode(sample, final=False)
        return "utf-8", False
    except UnicodeDecodeError:
        return "locale", False


def _bom_len(encoding: str) -> int:
    return {"utf-8-sig": 3, "utf-16-le": 2, "utf-16-be": 2}.get(encoding, 0)


def _locale_codec(data: bytes) -> Tuple[str, str]:
    """(codec, errors) that decodes ``data`` losslessly: Windows ANSI code page / GBK when strict decoding works,
    else UTF-8 with surrogateescape (unknown bytes survive a decode/encode round trip)."""
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        for enc in ("mbcs", "gbk"):
            try:
                data.decode(enc)
                return enc, "strict"
            except (UnicodeDecodeError, LookupError):
                continue
    return "utf-8", "surrogateescape"


def decode_bytes(data: bytes, encoding: str) -> Tuple[str, str, str]:
    """Decode a whole file: returns (text, codec, errors) for a lossless re-encode. ``encoding`` comes from
    ``detect_encoding``; a "utf-8" file that is not valid UTF-8 after the head falls back to "locale"."""
    if encoding in ("utf-16-le", "utf-16-be"):
        return data[2:].decode(encoding, "surrogatepass"), encoding, "surrogatepass"
    if encoding == "utf-8-sig":
        try:
            return data[3:].decode("utf-8"), "utf-8", "strict"
        except UnicodeDecodeError:
            return data[3:].decode("utf-8", "surrogateescape"), "utf-8", "surrogateescape"
    if encoding == "utf-8":
        try:
            return data.decode("utf-8"), "utf-8", "strict"
        except UnicodeDecodeError:
            pass
    codec, errors = _locale_codec(data)
    return data.decode(codec, errors), codec, errors


def encode_text(text: str, encoding: str, codec: str, errors: str) -> bytes:
    """Re-encode text decoded by ``decode_bytes`` with the same encoding and BOM."""
    bom = {"utf-8-sig": codecs.BOM_UTF8, "utf-16-le": codecs.BOM_UTF16_LE,
           "utf-16-be": codecs.BOM_UTF16_BE}.get(encoding, b"")
    try:
        return bom + text.encode(codec, errors)
    except UnicodeEncodeError as e:
        raise bad_request(f"the new text cannot be written in the file's encoding ({codec}): {e.reason}",
                          hint="use characters the file's encoding supports")


def display_text(text: str) -> str:
    """Lone surrogates (undecodable bytes kept by surrogateescape) shown as U+FFFD, so results stay valid JSON."""
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        pass
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")


def binary_error(path: Any, size: int) -> HiveError:
    """The fs.read refusal for binary files."""
    return unsupported(f"'{path}' looks like a binary file ({size} bytes)",
                       hint=f"binary file ({size} bytes); use fs.read_chunk or fs_transfer", size=size)


# ---------------------------------------------------------------------------------------------------------------
# lines

def split_lines(text: str) -> List[str]:
    """Lines with their endings (\\r\\n, \\r or \\n only; form feeds etc. are not line breaks)."""
    return _LINE_RE.findall(text)


def _strip_eol(line: str) -> str:
    return _EOL_RE.sub("", line)


def _cut_line(line: str, mask: Optional[EdgeMask] = None) -> Tuple[str, bool, bool]:
    """(shown line, was cut, masked): lines longer than LONG_LINE chars are cut; a secret value cut by the edge is
    masked through ``mask`` (step 10a only masks whole values)."""
    body = _strip_eol(line)
    if len(body) > LONG_LINE:
        kept, masked = body[:LONG_LINE], False
        if mask is not None:
            kept, masked = mask(kept, body[LONG_LINE:])
        return kept + f"…[+{len(body) - LONG_LINE} chars]" + line[len(body):], True, masked
    return line, False, False


def open_regular(path: Any) -> BinaryIO:
    """Open a regular file for reading without blocking on FIFOs/devices (``O_NONBLOCK``); anything but a regular
    file -> bad_request."""
    fd = os.open(long_path(Path(path)), os.O_RDONLY | O_BINARY | O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if stat.S_ISDIR(st.st_mode):
            raise bad_request(f"'{path}' is a directory", hint="use fs_list for folders")
        if not stat.S_ISREG(st.st_mode):
            raise bad_request(f"'{path}' is not a regular file (device, FIFO or socket)")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def read_lines(path: Path, start_line: int, max_lines: int, max_bytes: int, *, line_numbers: bool = False,
               check: Optional[Callable[[], None]] = None, mask: Optional[EdgeMask] = None) -> dict:
    """fs.read core: ``{"content","start_line","end_line","total_lines","next_start_line","size","version",
    "encoding","long_lines_cut"}`` (+ ``"masked": true`` when ``mask`` hid a secret cut by a line cut); binary files
    (NUL in the first 8 KiB) -> unsupported; non-regular files -> bad_request. ``check`` (e.g. ``ctx.check``) is
    called between chunks of the streamed (> 64 MiB) path so a timed-out or cancelled call stops reading."""
    start_line = max(1, int(start_line))
    masked = False
    with os_errors(path):
        st = os.stat(long_path(Path(path)))
        if stat.S_ISDIR(st.st_mode):
            raise bad_request(f"'{path}' is a directory", hint="use fs_list for folders")
        if not stat.S_ISREG(st.st_mode):
            raise bad_request(f"'{path}' is not a regular file (device, FIFO or socket)")
        size = st.st_size
        with open_regular(path) as f:
            head = f.read(HEAD_BYTES)
            encoding, binary = detect_encoding(head)
            if binary:
                raise binary_error(path, size)
            if size <= WHOLE_FILE_MAX:
                f.seek(0)
                data = f.read(WHOLE_FILE_MAX + 1)
                version: Optional[str] = util.sha256_bytes(data)
                text, codec, errors = decode_bytes(data, encoding)
                enc_name = "locale" if encoding == "utf-8" and (codec != "utf-8" or errors != "strict") else encoding
                lines = split_lines(text)
                total: Optional[int] = len(lines)
                selected = lines[start_line - 1:start_line - 1 + max_lines + 1]
            else:
                version, total = None, None
                f.seek(_bom_len(encoding))
                codec = {"utf-8-sig": "utf-8", "locale": "utf-8"}.get(encoding, encoding)
                errors = "replace"
                enc_name = encoding
                stream = codecs.getreader(codec)(f, errors=errors)
                selected = []
                n = 0
                buf = ""
                while len(selected) <= max_lines:
                    if check is not None:
                        check()
                    chunk = stream.read(1 << 16)
                    parts: List[str]
                    if not chunk:
                        parts, buf = ([buf] if buf else []), ""
                    else:
                        buf += chunk
                        parts = split_lines(buf)
                        if parts and (not _EOL_RE.search(parts[-1]) or parts[-1].endswith("\r")):
                            buf = parts.pop()      # incomplete line (or a "\r" that may start "\r\n")
                        else:
                            buf = ""
                    for ln in parts:
                        n += 1
                        if n >= start_line:
                            selected.append(ln)
                            if len(selected) > max_lines:
                                break
                    if not chunk:
                        break
    more = len(selected) > max_lines
    selected = selected[:max_lines]
    out: List[str] = []
    used = 0
    cut = 0
    for i, ln in enumerate(selected):
        shown, was_cut, m = _cut_line(display_text(ln), mask)
        masked = masked or m
        if line_numbers:
            shown = f"{start_line + i:6d}\t{shown}"
        b = len(shown.encode("utf-8"))
        if used + b > max_bytes:
            if not out:   # one line larger than max_bytes: cut it (never return nothing)
                raw = shown.encode("utf-8")[:max_bytes]
                kept = raw.decode("utf-8", "ignore")
                if mask is not None:   # the caller picks max_bytes: mask a secret cut by this edge (§5.13 v8)
                    kept, m = mask(kept, shown[len(kept):])
                    masked = masked or m
                out.append(kept)
                cut += 1
            more = True
            break
        out.append(shown)
        used += b
        cut += int(was_cut)
    end_line = start_line + len(out) - 1
    next_start: Optional[int] = end_line + 1 if (more or len(out) < len(selected)) else None
    if total is not None and next_start is not None and next_start > total:
        next_start = None
    res = {"content": "".join(out), "start_line": start_line, "end_line": end_line, "total_lines": total,
           "next_start_line": next_start, "size": size, "version": version, "encoding": enc_name,
           "long_lines_cut": cut}
    if masked:
        res["masked"] = True
    return res


# ---------------------------------------------------------------------------------------------------------------
# edit

def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1 if "\n" in text else text.count("\r", 0, index) + 1


def normalize_newlines(text: str, old: str, new: str) -> Tuple[str, str]:
    """CRLF files: an ``old`` written with bare LF is matched (and ``new`` written) with CRLF (§9.7)."""
    if "\r\n" in text and "\n" in old and "\r\n" not in old:
        old = old.replace("\n", "\r\n")
        new = new.replace("\r\n", "\n").replace("\n", "\r\n")
    return old, new


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> Tuple[str, int, List[int]]:
    """Exact replacement: (new text, replacements, start lines of the matches). 0 matches -> not_found with
    ``details.closest``; more than one without ``replace_all`` -> conflict with ``details.lines``."""
    if not old:
        raise bad_request("old_text must not be empty")
    old, new = normalize_newlines(text, old, new)
    idx: List[int] = []
    pos = text.find(old)
    while pos >= 0:
        idx.append(pos)
        pos = text.find(old, pos + len(old))
    if not idx:
        closest = closest_window(text, old)
        raise not_found("old_text was not found in the file", closest=closest,
                        hint="old_text must match exactly (whitespace and line breaks included); details.closest "
                             "shows the most similar lines")
    lines = [_line_of(text, i) for i in idx]
    if len(idx) > 1 and not replace_all:
        raise conflict(f"old_text matches {len(idx)} times (lines {', '.join(map(str, lines[:20]))})",
                       lines=lines[:100], hint="add surrounding lines to old_text to make it unique, or set "
                                               "replace_all=true")
    if replace_all:
        return text.replace(old, new), len(idx), lines
    i = idx[0]
    return text[:i] + new + text[i + len(old):], 1, lines


def closest_window(text: str, old: str) -> dict:
    """``{"start_line","lines":[numbered]}``: the most similar window of len(old lines) (±2 lines of context)."""
    tl = [_strip_eol(x) for x in split_lines(text)]
    ol = [_strip_eol(x) for x in split_lines(old)] or [old]
    if not tl:
        return {"start_line": 1, "lines": [], "ratio": 0.0}
    k = max(1, min(len(ol), len(tl)))
    target = "\n".join(ol)
    sm = difflib.SequenceMatcher(None, autojunk=False)
    sm.set_seq2(target)
    best, best_i = -1.0, 0
    windows = len(tl) - k + 1
    step = max(1, windows // 20000)          # bound the work on huge files
    for i in range(0, windows, step):
        sm.set_seq1("\n".join(tl[i:i + k]))
        if sm.real_quick_ratio() <= best or sm.quick_ratio() <= best:
            continue
        r = sm.ratio()
        if r > best:
            best, best_i = r, i
    lo = max(0, best_i - 2)
    hi = min(len(tl), best_i + k + 2)
    lines = [f"{n + 1}: {display_text(tl[n])[:LONG_LINE]}" for n in range(lo, hi)]
    return {"start_line": lo + 1, "lines": lines, "ratio": round(max(best, 0.0), 3)}


def snippet(text: str, line: int, new_lines: int, context: int = 3) -> str:
    """Numbered lines around an edit (``line`` .. ``line+new_lines-1`` ± context)."""
    tl = [_strip_eol(x) for x in split_lines(text)]
    lo = max(1, line - context)
    hi = min(len(tl), line + max(new_lines, 1) - 1 + context)
    return "\n".join(f"{n:6d}\t{display_text(tl[n - 1])[:LONG_LINE]}" for n in range(lo, hi + 1))


# ---------------------------------------------------------------------------------------------------------------
# regex safety (§9.7)

def _scan_regex(pattern: str) -> Tuple[int, List[str]]:
    """(unbounded quantifier count, problems) from a light tokenizer: groups followed by an unbounded quantifier
    (``*``, ``+``, ``{n,}``), or repeated more than MAX_GROUP_REPEAT times in all (bounded counts of nested groups
    multiply), must not contain a quantifier or ``|``; back-references and verbose mode (``x`` flag) are refused."""
    problems: List[str] = []
    unbounded = 0
    stack: List[Dict[str, Any]] = []          # open groups: {"inner_q": bool, "alt": bool, "mult": repeat count}
    i, n = 0, len(pattern)
    prev_atom_end = False                     # an atom precedes (so a quantifier char quantifies it)
    last_quant = False

    def mark_quant() -> None:
        for g in stack:
            g["inner_q"] = True

    while i < n:
        c = pattern[i]
        if c == "\\":
            nxt = pattern[i + 1:i + 2]
            if nxt.isdigit() and nxt != "0":
                problems.append("back-reference")
            elif nxt == "g":
                problems.append("back-reference")
            i += 2
            prev_atom_end, last_quant = True, False
            continue
        if c == "[":
            j = i + 1
            if j < n and pattern[j] == "^":
                j += 1
            if j < n and pattern[j] == "]":
                j += 1
            while j < n and pattern[j] != "]":
                j += 2 if pattern[j] == "\\" else 1
            i = j + 1
            prev_atom_end, last_quant = True, False
            continue
        if c == "(":
            if pattern.startswith("(?P=", i):
                problems.append("back-reference")
            if pattern.startswith("(?#", i):
                j = pattern.find(")", i)
                i = n if j < 0 else j + 1
                continue
            stack.append({"inner_q": False, "alt": False, "mult": 1})
            i += 1
            if i < n and pattern[i] == "?":       # (?:  (?P<n>  (?=  (?!  (?<=  (?i) …: skip the marker
                i += 1
                if i < n and pattern[i] == "P" and i + 1 < n and pattern[i + 1] == "<":
                    j = pattern.find(">", i)
                    i = n if j < 0 else j + 1
                elif i < n and pattern[i] == "<" and i + 1 < n and pattern[i + 1] in "=!":
                    i += 2
                elif i < n and pattern[i] in ":=!>":
                    i += 1
                else:
                    j = i
                    while i < n and (pattern[i].isalpha() or pattern[i] == "-"):
                        i += 1
                    if "x" in pattern[j:i]:      # verbose: whitespace and '#' change what the engine sees
                        problems.append("verbose")
                    if i < n and pattern[i] == ":":
                        i += 1
            prev_atom_end, last_quant = False, False
            continue
        if c == ")":
            g = stack.pop() if stack else {"inner_q": False, "alt": False, "mult": 1}
            i += 1
            # quantifier after the group?
            q_unbounded, q_any, j, upper = _read_quant(pattern, i)
            mult = g["mult"]
            if q_any:
                mult = mult * upper if not q_unbounded else REPEAT_INF
                if q_unbounded:
                    unbounded += 1
                if (q_unbounded or mult > MAX_GROUP_REPEAT) and (g["inner_q"] or g["alt"]):
                    problems.append("nested quantifier")
                mark_quant()
                i = j
                last_quant = True
            else:
                last_quant = False
            # the group's own contents count as quantified/alternated for enclosing groups
            if stack:
                stack[-1]["inner_q"] = stack[-1]["inner_q"] or g["inner_q"]
                stack[-1]["alt"] = stack[-1]["alt"] or g["alt"]
                stack[-1]["mult"] = max(stack[-1]["mult"], min(mult, REPEAT_INF))
            prev_atom_end = True
            continue
        if c == "|":
            if stack:
                stack[-1]["alt"] = True
            i += 1
            prev_atom_end, last_quant = False, False
            continue
        if c in "*+?{" and prev_atom_end and not last_quant:
            q_unbounded, q_any, j, _upper = _read_quant(pattern, i)
            if q_any:
                if q_unbounded:
                    unbounded += 1
                mark_quant()
                i = j
                last_quant = True
                continue
        i += 1
        prev_atom_end, last_quant = True, False
    return unbounded, problems


REPEAT_INF = 1 << 30


def _read_quant(p: str, i: int) -> Tuple[bool, bool, int, int]:
    """(unbounded, is_quantifier, end index, upper bound) of a quantifier at p[i] (incl. a lazy/possessive
    suffix); the upper bound is REPEAT_INF when unbounded."""
    n = len(p)
    if i >= n:
        return False, False, i, 1
    c = p[i]
    if c in "*+":
        unb, j, upper = True, i + 1, REPEAT_INF
    elif c == "?":
        unb, j, upper = False, i + 1, 1
    elif c == "{":
        m = re.match(r"\{(\d*)(,?)(\d*)\}", p[i:])
        if not m or (not m.group(1) and not m.group(3)):
            return False, False, i, 1
        unb = bool(m.group(2)) and not m.group(3)
        if unb:
            upper = REPEAT_INF
        else:
            upper = min(REPEAT_INF, int(m.group(3) or m.group(1)))
        j = i + m.end()
    else:
        return False, False, i, 1
    if j < n and p[j] in "?+":
        j += 1
    return unb, True, j, upper


def check_regex(pattern: str, *, max_len: int = REGEX_MAX_LEN) -> "re.Pattern[str]":
    """Compile an agent-supplied regex after the §9.7 safety rules; ``bad_request`` hint "simplify or use
    literal=true" when it is longer than ``max_len``, has a quantified group containing a quantifier or ``|``,
    more than 4 unbounded quantifiers, or a back-reference."""
    if not isinstance(pattern, str):
        raise bad_request("pattern must be a string")
    hint = "simplify or use literal=true"
    if len(pattern) > max_len:
        raise bad_request(f"regex longer than {max_len} characters", hint=hint)
    unbounded, problems = _scan_regex(pattern)
    if "back-reference" in problems:
        raise bad_request("regex back-references are not allowed", hint=hint)
    if "verbose" in problems:
        raise bad_request("regex verbose mode ((?x)) is not allowed", hint=hint)
    if "nested quantifier" in problems:
        raise bad_request("a repeated group may not contain a quantifier or '|' (catastrophic backtracking)",
                          hint=hint)
    if unbounded > MAX_UNBOUNDED:
        raise bad_request(f"regex has {unbounded} unbounded quantifiers (max {MAX_UNBOUNDED})", hint=hint)
    try:
        rx = re.compile(pattern)
    except (re.error, OverflowError, RecursionError) as e:
        raise bad_request(f"invalid regex: {e}", hint=hint)
    if rx.flags & re.VERBOSE:      # belt and braces: the scan above must have seen every flag group
        raise bad_request("regex verbose mode ((?x)) is not allowed", hint=hint)
    return rx


# ---------------------------------------------------------------------------------------------------------------
# walking

FILE_ATTRIBUTE_HIDDEN = 0x2


def is_hidden(entry: Any) -> bool:
    """Dotfile, or (Windows) FILE_ATTRIBUTE_HIDDEN set; ``entry`` is an os.DirEntry or a path."""
    name = entry.name if hasattr(entry, "name") else os.path.basename(str(entry))
    if name.startswith("."):
        return True
    if util.IS_WINDOWS:  # pragma: no cover - Windows only
        try:
            st = entry.stat(follow_symlinks=False) if hasattr(entry, "stat") else os.lstat(str(entry))
            return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_HIDDEN)
        except OSError:
            return False
    return False


def entry_allowed(guard: Any, path: str) -> bool:
    """True when the policy lets the caller read ``path`` (realpath + roots + denies)."""
    try:
        guard.resolve_path(path)
        return True
    except HiveError:
        return False


def walk(root: Path, *, depth: int, include_hidden: bool, guard: "EffectivePolicy",
         deadline: float, skip_dirs: Iterable[str] = (), state: Optional[Dict[str, Any]] = None
         ) -> Iterator[os.DirEntry]:
    """Breadth-first walk to ``depth`` (1 = direct children). Skips denied entries and never descends into them,
    nor into symbolic links (they are yielded, unresolved) or ``skip_dirs`` names (§5.4 step 9). Stops quietly
    when ``time.monotonic()`` passes ``deadline``; then ``state["truncated"]`` is set to True (callers that must not
    report partial work as complete check it)."""
    skip = set(skip_dirs)
    if state is not None:
        state["truncated"] = False
    level = [str(root)]
    for d in range(1, max(1, int(depth)) + 1):
        nxt: List[str] = []
        for folder in level:
            if time.monotonic() > deadline:
                if state is not None:
                    state["truncated"] = True
                return
            try:
                with os.scandir(long_path(Path(folder))) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError:
                continue
            for e in entries:
                if time.monotonic() > deadline:
                    if state is not None:
                        state["truncated"] = True
                    return
                if not include_hidden and is_hidden(e):
                    continue
                path = os.path.join(folder, e.name)
                try:
                    is_link = e.is_symlink()
                except OSError:
                    is_link = False
                if is_link:
                    yield e
                    continue
                if not entry_allowed(guard, path):
                    continue
                yield e
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                if is_dir and d < depth and e.name not in skip:
                    nxt.append(path)
        level = nxt
        if not level:
            return


def long_path(p: Path) -> str:
    """Windows: ``\\\\?\\`` prefix for absolute paths longer than 240 characters; elsewhere ``str(p)``."""
    s = str(p)
    if not util.IS_WINDOWS or len(s) <= 240 or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s).replace("/", "\\")  # pragma: no cover - Windows only
    if s.startswith("\\\\"):  # pragma: no cover
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s  # pragma: no cover


# ---------------------------------------------------------------------------------------------------------------
# part files

class PartTracker:
    """``state/parts.json`` = {part path: last write ISO UTC}; idle parts are deleted by ``prune`` (24 h)."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> Dict[str, str]:
        try:
            d = util.read_json(self.path, default={})
        except HiveError:
            d = {}
        if not isinstance(d, dict):
            return {}
        return {str(k): str(v) for k, v in (d.get("parts") or {}).items()} if "parts" in d else \
            {str(k): str(v) for k, v in d.items() if isinstance(v, str)}

    def _save(self, parts: Dict[str, str]) -> None:
        try:
            util.secure_mkdir(self.path.parent)
            util.write_json_atomic(self.path, {"v": 1, "parts": parts}, private=True)
        except OSError as e:  # pragma: no cover - disk trouble is logged, not fatal
            log.warning("cannot write %s: %s", self.path, e)

    def touch(self, part: Path) -> None:
        """Record a write to a part file."""
        with self._lock:
            parts = self._load()
            parts[str(part)] = util.iso_utc()
            self._save(parts)

    def done(self, part: Path) -> None:
        """Forget a finished (or deleted) part file."""
        with self._lock:
            parts = self._load()
            if parts.pop(str(part), None) is not None:
                self._save(parts)

    def prune(self, max_idle_s: int = 86400) -> int:
        """Delete part files idle longer than ``max_idle_s`` (last tracked write and file mtime); returns the
        count. Only paths ending in ``.hive-part`` are ever deleted."""
        now = time.time()
        removed = 0
        with self._lock:
            parts = self._load()
            keep: Dict[str, str] = {}
            for p, ts in parts.items():
                try:
                    last = util.parse_iso(ts)
                except HiveError:
                    last = 0.0
                try:
                    last = max(last, os.lstat(p).st_mtime)
                    exists = True
                except OSError:
                    exists = False
                if not exists:
                    continue
                if now - last <= max_idle_s:
                    keep[p] = ts
                    continue
                if p.endswith(".hive-part"):
                    try:
                        st = os.lstat(p)
                        if stat.S_ISREG(st.st_mode):
                            os.unlink(p)
                            removed += 1
                            continue
                    except OSError:
                        keep[p] = ts
                        continue
            if keep != parts:
                self._save(keep)
        return removed


def json_depth_ok(obj: Any, limit: int = 64) -> bool:
    """True when a decoded JSON value nests at most ``limit`` levels."""
    stack = [(obj, 1)]
    while stack:
        o, d = stack.pop()
        if d > limit:
            return False
        if isinstance(o, dict):
            stack.extend((v, d + 1) for v in o.values())
        elif isinstance(o, list):
            stack.extend((v, d + 1) for v in o)
    return True


__all__ = ["check_regex", "detect_encoding", "read_lines", "apply_edit", "closest_window", "walk", "is_hidden",
           "long_path", "PartTracker", "os_errors", "decode_bytes", "encode_text", "split_lines", "snippet",
           "normalize_newlines", "entry_allowed", "display_text", "binary_error", "json_depth_ok", "open_regular"]
