"""Hugging Face / ModelScope repo listing and URL building for model.pull (design §11.5, WP3).

Live-API shapes (confirmed 2026-11 against huggingface.co and www.modelscope.cn):
- HF ``GET {ep}/api/{models|datasets}/{repo}/revision/{rev}`` -> ``{"id", "sha", …}``; ``GET …/tree/{sha}?recursive=true``
  -> ``[{"type":"file"|"directory","oid":<git blob sha1>,"size","path","lfs":{"oid":<sha256>,"size"}?,
  "xetHash"?}]`` with ``Link: <…>; rel="next"`` pagination (``xetHash`` is not a sha256: LFS files use
  ``lfs.oid``); errors carry ``X-Error-Code`` (``GatedRepo``, ``RepoNotFound``, ``RevisionNotFound``).
- ModelScope ``GET {ep}/api/v1/models/{repo}/repo/files?Revision={rev}&Recursive=true`` ->
  ``{"Code":200,"Data":{"Files":[{"Path","Size","Sha256","Type":"blob"|"tree","Revision":<last commit of the file>,
  "CommittedDate"}]},"Success":true}``; a missing repo answers ``Success:false`` with "record not found".
  ModelScope has no HEAD sha in the listing: the commit is the newest file ``Revision`` (every listed file is
  unchanged since then), and ``repo?Revision=<sha>&FilePath=…`` serves files at that commit.

Imports only stdlib, hive.util, hive.errors, hive.netproxy and hive.netutil (the _download child imports it).
"""
from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import quote, unquote, urljoin, urlsplit

from . import netutil
from .errors import HiveError, bad_request, forbidden, not_found, timeout, too_large, unavailable

if TYPE_CHECKING:  # pragma: no cover
    from .netutil import AuthSpec, NetGuard

HF_DEFAULT = "https://huggingface.co"
MS_DEFAULT = "https://www.modelscope.cn"
SOURCES = ("huggingface", "modelscope")
REPO_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}(/[A-Za-z0-9][A-Za-z0-9._-]{0,95})?\Z")
SHA1_RE = re.compile(r"^[0-9a-f]{40}\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
LINK_NEXT_RE = re.compile(r'<([^<>]{1,8192})>\s*;\s*rel="?next"?', re.IGNORECASE)
CACHE_TTL = 600.0
MAX_PAGES = 20
MAX_PAGE_BYTES = 16 << 20
MAX_ENTRIES = 200_000                  # audit round 4: listing entries in all (a hostile mirror cannot exhaust memory)
CACHE_MAX_ENTRIES = 20_000             # only listings up to this many files are cached (64 of them at most)
TOO_MANY_HINT = ("the repository lists too many files to pull with model.pull (include/exclude only filter the "
                 "listing); download the files you need with net.download, or ask the owner")
REQUEST_TIMEOUT = 60.0
LIST_BUDGET = 600.0
NOT_FOUND_HINT = "check the repo id/revision; private repos need a token"
GATED_HINT = ("gated model: accept the license at {url} with the account whose token is on the node, then ask the "
              "owner to run `hive node secret set HF_TOKEN` on the node")


@dataclass
class RepoFile:
    """One file of a repo snapshot."""
    path: str
    size: int
    sha256: Optional[str]
    git_sha1: Optional[str]


def _get(cfg: Any, key: str) -> Any:
    if cfg is None:
        return None
    if isinstance(cfg, Mapping):
        return cfg.get(key)
    return getattr(cfg, key, None)


def _clean_endpoint(ep: str, what: str) -> str:
    e = str(ep).strip().rstrip("/")
    try:
        p = urlsplit(e)
        _ = p.port
    except ValueError:
        raise bad_request(f"invalid {what} {e[:200]!r}", hint="e.g. https://hf-mirror.com")
    if p.scheme.lower() not in ("http", "https") or not p.hostname or "@" in p.netloc or p.query or p.fragment:
        raise bad_request(f"invalid {what} {e[:200]!r}", hint="e.g. https://hf-mirror.com (no credentials or query)")
    return e


def endpoint_for(source: str, cfg: Any, env: Mapping[str, str]) -> str:
    """Endpoint from node config / env only (no agent-supplied endpoint): huggingface = ``models.hf_endpoint`` ->
    node env ``HF_ENDPOINT`` -> https://huggingface.co; modelscope = ``models.modelscope_endpoint``."""
    if source == "huggingface":
        ep = _get(cfg, "hf_endpoint") or (env or {}).get("HF_ENDPOINT") or HF_DEFAULT
        return _clean_endpoint(ep, "models.hf_endpoint")
    if source == "modelscope":
        return _clean_endpoint(_get(cfg, "modelscope_endpoint") or MS_DEFAULT, "models.modelscope_endpoint")
    raise bad_request(f"unknown source {source!r}", hint="huggingface or modelscope")


def _check_id(repo: str) -> str:
    if not REPO_RE.match(repo) or ".." in repo:
        raise bad_request(f"invalid repository id {repo[:120]!r}",
                          hint="use 'Org/Name' (e.g. Qwen/Qwen2.5-7B-Instruct) or its URL")
    return repo


def normalize_repo(repo: str, source: str) -> Tuple[str, Optional[str]]:
    """(repo id, revision from a /tree/REV URL). Accepts ``Org/Name``, ``https://huggingface.co/Org/Name[/tree/REV]``,
    ``https://hf-mirror.com/Org/Name``, ``https://www.modelscope.cn/models/Org/Name``."""
    s = str(repo or "").strip()
    if not s:
        raise bad_request("repo is empty", hint="use 'Org/Name'")
    low = s.lower()
    if low.startswith(("http://", "https://")) or re.match(r"^(www\.)?(huggingface\.co|hf\.co|hf-mirror\.com|"
                                                           r"modelscope\.cn)/", low):
        if not low.startswith(("http://", "https://")):
            s = "https://" + s
        try:
            parts = urlsplit(s)
        except ValueError:
            raise bad_request(f"invalid repository URL {s[:200]!r}")
        segs = [unquote(x) for x in parts.path.split("/") if x]
        if segs and segs[0] in ("models", "datasets"):
            segs = segs[1:]
        if len(segs) < 1:
            raise bad_request(f"no repository in {s[:200]!r}", hint="use 'Org/Name'")
        rev: Optional[str] = None
        if len(segs) >= 2 and segs[1] not in ("tree", "resolve", "blob", "files", "summary"):
            rid = segs[0] + "/" + segs[1]
            rest = segs[2:]
        else:
            rid = segs[0]
            rest = segs[1:]
        if len(rest) >= 2 and rest[0] in ("tree", "resolve", "blob"):
            rev = rest[1]
        return _check_id(rid), rev
    return _check_id(s.rstrip("/")), None


def file_url(source: str, endpoint: str, repo: str, repo_type: str, commit: str, path: str) -> str:
    """Download URL of one file at a commit."""
    ep = endpoint.rstrip("/")
    if source == "modelscope":
        return (f"{ep}/api/v1/models/{repo}/repo?Revision={quote(commit, safe='')}"
                f"&FilePath={quote(path, safe='')}")
    prefix = f"{ep}/datasets/{repo}" if repo_type == "dataset" else f"{ep}/{repo}"
    return f"{prefix}/resolve/{quote(commit, safe='')}/{quote(path, safe='/')}"


# ---------------------------------------------------------------------------------------------------------------
# listing

class _Listing:
    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[Tuple[str, List[RepoFile]]] = None
        self.error: Optional[BaseException] = None


_lock = threading.Lock()
_cache: Dict[Tuple[Any, ...], Tuple[float, str, List[RepoFile]]] = {}
_inflight: Dict[Tuple[Any, ...], _Listing] = {}


def clear_cache() -> None:
    """Forget cached listings (tests)."""
    with _lock:
        _cache.clear()


def _json(r: "netutil.HttpResult", what: str) -> Any:
    if r.truncated:
        raise too_large(f"{what} is larger than {MAX_PAGE_BYTES >> 20} MiB", hint="use include to narrow the files")
    try:
        return json.loads(r.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise unavailable(f"{what}: the server sent invalid JSON (HTTP {r.status})", reason="upstream",
                          maybe_executed=False, status=r.status)


def _hdr(r: "netutil.HttpResult", name: str) -> str:
    low = name.lower()
    for k, v in (r.headers or {}).items():
        if str(k).lower() == low:
            return str(v)
    return ""


def _snippet(r: "netutil.HttpResult") -> str:
    return r.body[:300].decode("utf-8", "replace")


def _hf_error(r: "netutil.HttpResult", repo: str, endpoint: str) -> HiveError:
    code = _hdr(r, "X-Error-Code")
    text = _snippet(r).lower()
    if r.status in (401, 403) and (code == "GatedRepo" or "gated" in text or "restricted" in text):
        return forbidden(f"{repo} is a gated repository: access was refused (HTTP {r.status})",
                         hint=GATED_HINT.format(url=f"{HF_DEFAULT}/{repo}"), gated=True, status=r.status, repo=repo)
    if r.status == 404 or code in ("RepoNotFound", "RevisionNotFound", "EntryNotFound"):
        return not_found(f"repository or revision not found: {repo} ({code or r.status})", hint=NOT_FOUND_HINT,
                         status=r.status, repo=repo)
    if r.status == 401:
        return not_found(f"repository {repo} not found or private (HTTP 401)", hint=NOT_FOUND_HINT, status=401,
                         repo=repo)
    if r.status == 403:
        return forbidden(f"access to {repo} was refused (HTTP 403)",
                         hint="the token on the node may lack access; ask the owner", status=403, repo=repo)
    if r.status == 429:
        return unavailable(f"{endpoint} is rate limiting requests (HTTP 429)", reason="upstream",
                           maybe_executed=False, status=429, hint="wait a minute and call again")
    return unavailable(f"{endpoint} answered HTTP {r.status}", reason="upstream", maybe_executed=False,
                       status=r.status, body=_snippet(r))


def _request(url: str, guard: Any, auth: Optional["AuthSpec"], end: float) -> "netutil.HttpResult":
    left = end - time.monotonic()
    if left <= 0:
        raise timeout("repository listing took too long", maybe_executed=False, hint="narrow include")
    return netutil.guarded_request("GET", url, guard=guard, purpose="download",
                                   headers={"Accept": "application/json"}, timeout=min(REQUEST_TIMEOUT, left),
                                   max_bytes=MAX_PAGE_BYTES, follow_redirects=True, auth=auth)


def _too_many(repo: str) -> HiveError:
    return too_large(f"the file listing of {repo} has more than {MAX_ENTRIES} entries", hint=TOO_MANY_HINT,
                     max_entries=MAX_ENTRIES)


def _hf_list(endpoint: str, repo: str, repo_type: str, revision: str, guard: Any, auth: Optional["AuthSpec"],
             end: float) -> Tuple[str, List[RepoFile]]:
    kind = "datasets" if repo_type == "dataset" else "models"
    base = f"{endpoint}/api/{kind}/{repo}"
    r = _request(f"{base}/revision/{quote(revision, safe='')}", guard, auth, end)
    if r.status != 200:
        raise _hf_error(r, repo, endpoint)
    info = _json(r, "revision info")
    sha = info.get("sha") if isinstance(info, dict) else None
    if not isinstance(sha, str) or not SHA1_RE.match(sha):
        raise unavailable("the revision info has no commit sha", reason="upstream", maybe_executed=False,
                          body=_snippet(r))
    url: Optional[str] = f"{base}/tree/{sha}?recursive=true"
    files: List[RepoFile] = []
    pages = seen = 0
    while url:
        pages += 1
        if pages > MAX_PAGES:
            raise too_large(f"the file listing of {repo} has more than {MAX_PAGES} pages",
                            hint="this repository is too large to pull whole; ask the owner")
        r = _request(url, guard, auth, end)
        if r.status != 200:
            raise _hf_error(r, repo, endpoint)
        page = _json(r, "file listing")
        if not isinstance(page, list):
            raise unavailable("the file listing is not a list", reason="upstream", maybe_executed=False)
        seen += len(page)
        if seen > MAX_ENTRIES:
            raise _too_many(repo)
        for e in page:
            if not isinstance(e, dict) or e.get("type") != "file":
                continue
            path, size = e.get("path"), e.get("size")
            lfs = e.get("lfs") if isinstance(e.get("lfs"), dict) else None
            if lfs is not None and isinstance(lfs.get("size"), int):
                size = lfs["size"]
            if not isinstance(path, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
                continue
            sha256 = None
            git_sha1 = None
            if lfs is not None:
                oid = str(lfs.get("oid") or "").lower()
                sha256 = oid if SHA256_RE.match(oid) else None
            else:
                oid = str(e.get("oid") or "").lower()
                git_sha1 = oid if SHA1_RE.match(oid) else None
            files.append(RepoFile(path=path, size=int(size), sha256=sha256, git_sha1=git_sha1))
        nxt = None
        link = _hdr(r, "Link")
        if link:
            m = LINK_NEXT_RE.search(str(link))
            if m:
                nxt = urljoin(url, m.group(1))
        url = nxt
    return sha, files


def _ms_error(r: "netutil.HttpResult", repo: str, endpoint: str, data: Any = None) -> HiveError:
    msg = str(data.get("Message") or "") if isinstance(data, dict) else _snippet(r)
    low = msg.lower()
    if r.status == 404 or "not found" in low or "not exist" in low:
        return not_found(f"repository or revision not found on ModelScope: {repo}", hint=NOT_FOUND_HINT,
                         status=r.status, repo=repo)
    if r.status in (401, 403) or "permission" in low or "unauthorized" in low:
        return forbidden(f"access to {repo} was refused by ModelScope (HTTP {r.status})",
                         hint="private or gated repos need MODELSCOPE_API_TOKEN on the node; ask the owner",
                         status=r.status, repo=repo)
    return unavailable(f"{endpoint} answered HTTP {r.status}: {msg[:200]}", reason="upstream", maybe_executed=False,
                       status=r.status)


def _ms_list(endpoint: str, repo: str, revision: str, guard: Any, auth: Optional["AuthSpec"],
             end: float) -> Tuple[str, List[RepoFile]]:
    url = (f"{endpoint}/api/v1/models/{repo}/repo/files?Revision={quote(revision, safe='')}&Recursive=true")
    r = _request(url, guard, auth, end)
    data = _json(r, "file listing") if r.status == 200 else None
    if r.status != 200 or not isinstance(data, dict) or data.get("Success") is False \
            or data.get("Code") not in (None, 200):
        if r.status == 200 and not isinstance(data, dict):
            raise unavailable("the ModelScope file listing is not an object", reason="upstream", maybe_executed=False)
        raise _ms_error(r, repo, endpoint, data)
    d = data.get("Data") if isinstance(data.get("Data"), dict) else {}
    entries = d.get("Files")
    if not isinstance(entries, list):
        raise unavailable("the ModelScope file listing has no Files", reason="upstream", maybe_executed=False)
    if len(entries) > MAX_ENTRIES:
        raise _too_many(repo)
    files: List[RepoFile] = []
    newest: Tuple[float, str] = (-1.0, "")
    for e in entries:
        if not isinstance(e, dict) or e.get("Type") not in ("blob", "file"):
            continue
        path, size = e.get("Path"), e.get("Size")
        if not isinstance(path, str) or not isinstance(size, int) or isinstance(size, bool) or size < 0:
            continue
        sha = str(e.get("Sha256") or "").lower()
        files.append(RepoFile(path=path, size=int(size), sha256=sha if SHA256_RE.match(sha) else None,
                              git_sha1=None))
        rev = str(e.get("Revision") or "").lower()
        when = e.get("CommittedDate")
        if SHA1_RE.match(rev) and isinstance(when, (int, float)) and float(when) > newest[0]:
            newest = (float(when), rev)
    if SHA1_RE.match(revision.lower()):
        commit = revision.lower()
    else:
        commit = newest[1] or revision
    return commit, files


def _worker(key: Tuple[Any, ...], listing: _Listing, source: str, endpoint: str, repo: str, repo_type: str,
            revision: str, guard: Any, auth: Optional["AuthSpec"]) -> None:
    end = time.monotonic() + LIST_BUDGET
    try:
        if source == "modelscope":
            res = _ms_list(endpoint, repo, revision, guard, auth, end)
        else:
            res = _hf_list(endpoint, repo, repo_type, revision, guard, auth, end)
        listing.result = res
        if len(res[1]) <= CACHE_MAX_ENTRIES:
            with _lock:
                if len(_cache) > 64:
                    _cache.clear()
                _cache[key] = (time.monotonic(), res[0], res[1])
    except BaseException as e:  # noqa: BLE001 - handed to every waiter
        listing.error = e
    finally:
        with _lock:
            _inflight.pop(key, None)
        listing.done.set()


def list_repo(source: str, endpoint: str, repo: str, repo_type: str, revision: str, *, guard: "NetGuard",
              auth: Optional["AuthSpec"], deadline: float) -> Tuple[str, List[RepoFile]]:
    """(commit, files) of a repo snapshot; results cached 10 min per (endpoint, repo, repo_type, revision) when they
    hold at most CACHE_MAX_ENTRIES files; more than MAX_ENTRIES listing entries raise ``too_large``. A listing
    still running at ``deadline`` (monotonic) raises ``timeout`` but goes on in the background, so the next call
    with the same arguments returns quickly from the cache."""
    if source not in SOURCES:
        raise bad_request(f"unknown source {source!r}")
    if source == "modelscope" and repo_type == "dataset":
        raise bad_request("datasets cannot be pulled from ModelScope", hint="use source='huggingface'")
    endpoint = endpoint.rstrip("/")
    key = (source, endpoint, repo, repo_type, revision, getattr(auth, "name", None))
    with _lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_TTL:
            return hit[1], [replace(f) for f in hit[2]]
        listing = _inflight.get(key)
        if listing is None:
            listing = _Listing()
            _inflight[key] = listing
            threading.Thread(target=_worker, name="hive-hf-list", daemon=True,
                             args=(key, listing, source, endpoint, repo, repo_type, revision, guard, auth)).start()
    if not listing.done.wait(max(0.0, deadline - time.monotonic())):
        raise timeout(f"listing {repo} is still in progress", maybe_executed=False,
                      hint="listing still in progress; call again (cached) or narrow include")
    if listing.error is not None:
        raise listing.error
    assert listing.result is not None
    return listing.result[0], [replace(f) for f in listing.result[1]]


__all__ = ["RepoFile", "endpoint_for", "normalize_repo", "list_repo", "file_url", "clear_cache", "REPO_RE"]
