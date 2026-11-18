"""A scripted Hugging Face / ModelScope hub for model.pull tests (design §21.2, WP3 fixture).

HF routes (shapes as confirmed against huggingface.co, see hive/hfhub.py):
  GET /api/{models|datasets}/<repo>/revision/<rev>      -> {"id", "sha"}; X-Error-Code RepoNotFound/RevisionNotFound/GatedRepo
  GET /api/{models|datasets}/<repo>/tree/<sha>?recursive=true[&cursor=N]  -> page of entries + Link rel="next"
  GET [/datasets]/<repo>/resolve/<commit>/<path>         -> file (Range), or 302 to the CDN host when ``cdn_host``
  GET /cdn/<repo>/<commit>/<path>                        -> file (Range)
ModelScope routes:
  GET /api/v1/models/<repo>/repo/files?Revision=&Recursive=true  -> {"Code":200,"Data":{"Files":[…]},"Success":true}
  GET /api/v1/models/<repo>/repo?Revision=&FilePath=             -> file (Range)

Knobs: ``token`` (required for gated repos; checked as ``Authorization: Bearer <token>``), ``page_size``,
``listing_delay`` (seconds per listing request), ``corrupt`` {path: times} (wrong bytes, same length), ``oversize``
{path: extra bytes} (more bytes than listed), ``fail`` {path: times} (HTTP 500), ``cut`` {path: (after_bytes, times)}
(connection closed mid-body), ``file_delay`` (seconds before each file chunk), ``requests`` (log with headers).
"""
from __future__ import annotations

import hashlib
import http.server
import json
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote, unquote, urlsplit

LFS_MIN = 1000      # files of at least this size are listed as LFS (sha256), smaller ones with a git blob sha1


def git_sha1(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()  # noqa: S324


class Repo:
    def __init__(self, files: Dict[str, bytes], *, commit: Optional[str] = None, gated: bool = False,
                 revisions: Tuple[str, ...] = ("main", "master")):
        self.files = dict(files)
        self.commit = commit or hashlib.sha1(json.dumps(sorted(files)).encode()).hexdigest()  # noqa: S324
        self.gated = gated
        self.revisions = revisions


class HFStub:
    """``with HFStub({"org/model": Repo({...})}) as hf: hf.url``"""

    def __init__(self, repos: Dict[str, Repo], *, token: Optional[str] = None, page_size: int = 1000,
                 cdn_host: Optional[str] = None):
        self.repos = dict(repos)
        self.token = token
        self.page_size = page_size
        self.cdn_host = cdn_host
        self.listing_delay = 0.0
        self.file_delay = 0.0
        self.corrupt: Dict[str, int] = {}
        self.oversize: Dict[str, int] = {}
        self.fail: Dict[str, int] = {}
        self.cut: Dict[str, Tuple[int, int]] = {}
        self.requests: List[dict] = []
        self.lock = threading.Lock()
        self._httpd: Optional[http.server.ThreadingHTTPServer] = None

    # lifecycle
    def start(self) -> "HFStub":
        stub = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:
                return None

            def do_GET(self) -> None:
                stub._handle(self)

            do_HEAD = do_GET

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        httpd.daemon_threads = True
        httpd.handle_error = lambda *a: None  # type: ignore[assignment]
        self._httpd = httpd
        threading.Thread(target=httpd.serve_forever, name="hfstub", daemon=True).start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None

    def __enter__(self) -> "HFStub":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    @property
    def port(self) -> int:
        assert self._httpd is not None
        return self._httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def file_requests(self, path: str) -> List[dict]:
        return [r for r in self.requests if r["kind"] == "file" and r.get("file") == path]

    # helpers
    def _send(self, h: Any, status: int, body: bytes = b"", headers: Optional[Dict[str, str]] = None) -> None:
        h.send_response(status)
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _json(self, h: Any, status: int, obj: Any, headers: Optional[Dict[str, str]] = None) -> None:
        hh = {"Content-Type": "application/json"}
        hh.update(headers or {})
        self._send(h, status, json.dumps(obj).encode(), hh)

    def _authorized(self, h: Any, repo: Repo) -> bool:
        if not repo.gated:
            return True
        return self.token is not None and h.headers.get("Authorization") == f"Bearer {self.token}"

    def _entry(self, path: str, data: bytes) -> dict:
        e: Dict[str, Any] = {"type": "file", "oid": git_sha1(data), "size": len(data), "path": path}
        if len(data) >= LFS_MIN:
            e["lfs"] = {"oid": hashlib.sha256(data).hexdigest(), "size": len(data), "pointerSize": 134}
            e["xetHash"] = hashlib.sha256(b"xet" + data).hexdigest()
        return e

    def _take(self, table: Dict[str, Any], path: str) -> Any:
        with self.lock:
            v = table.get(path)
            if v is None:
                return None
            if isinstance(v, tuple):
                after, n = v
                if n <= 0:
                    return None
                table[path] = (after, n - 1)
                return after
            if v <= 0:
                return None
            table[path] = v - 1
            return v

    def _serve_file(self, h: Any, repo_id: str, repo: Repo, path: str) -> None:
        data = repo.files.get(path)
        if data is None:
            self._json(h, 404, {"error": "Entry not found"}, {"X-Error-Code": "EntryNotFound"})
            return
        if self._take(self.fail, path):
            self._json(h, 500, {"error": "boom"})
            return
        if self._take(self.corrupt, path):
            data = bytes(b ^ 0xFF for b in data)
        extra = self.oversize.get(path)
        if extra:
            data = data + b"X" * extra
        size = len(data)
        start = 0
        status = 200
        headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes",
                   "X-Repo-Commit": repo.commit}
        rng = h.headers.get("Range")
        if rng and rng.startswith("bytes=") and rng.endswith("-"):
            try:
                start = int(rng[6:-1])
            except ValueError:
                start = 0
            if start >= size:
                self._send(h, 416, b"", {"Content-Range": f"bytes */{size}"})
                return
            status = 206
            headers["Content-Range"] = f"bytes {start}-{size - 1}/{size}"
        body = data[start:]
        cut = self._take(self.cut, path)
        h.send_response(status)
        for k, v in headers.items():
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command == "HEAD":
            return
        sent = 0
        step = 4096
        while sent < len(body):
            if cut is not None and sent >= cut:
                h.close_connection = True
                try:
                    h.connection.shutdown(2)
                except OSError:
                    pass
                return
            if self.file_delay:
                time.sleep(self.file_delay)
            chunk = body[sent:sent + step]
            try:
                h.wfile.write(chunk)
            except OSError:
                return
            sent += len(chunk)

    def _handle(self, h: Any) -> None:
        u = urlsplit(h.path)
        q = parse_qs(u.query)
        path = unquote(u.path)
        rec = {"path": h.path, "headers": dict(h.headers.items()), "host": h.headers.get("Host"), "kind": "other"}
        with self.lock:
            self.requests.append(rec)
        segs = [s for s in u.path.split("/") if s]
        # ModelScope
        if path.startswith("/api/v1/models/"):
            rest = segs[3:]
            if len(rest) >= 3 and rest[-2:] == ["repo", "files"]:
                rec["kind"] = "list"
                repo_id = "/".join(unquote(x) for x in rest[:-2])
                repo = self.repos.get(repo_id)
                if self.listing_delay:
                    time.sleep(self.listing_delay)
                if repo is None or not self._authorized(h, repo):
                    self._json(h, 200, {"Code": 10010205001, "Message": "获取模型目录树失败，信息：record not found",
                                        "Success": False})
                    return
                files = [{"Path": p, "Name": p.rsplit("/", 1)[-1], "Size": len(d), "Type": "blob",
                          "Sha256": hashlib.sha256(d).hexdigest(), "Revision": repo.commit,
                          "CommittedDate": 1726625619, "IsLFS": len(d) >= LFS_MIN}
                         for p, d in sorted(repo.files.items())]
                files.append({"Path": "sub", "Name": "sub", "Size": 0, "Type": "tree", "Revision": repo.commit})
                self._json(h, 200, {"Code": 200, "Data": {"Files": files}, "Success": True})
                return
            if len(rest) >= 2 and rest[-1] == "repo":
                rec["kind"] = "file"
                repo_id = "/".join(unquote(x) for x in rest[:-1])
                repo = self.repos.get(repo_id)
                fp = (q.get("FilePath") or [""])[0]
                rec["file"] = fp
                rev = (q.get("Revision") or [""])[0]
                rec["revision"] = rev
                if repo is None or not self._authorized(h, repo):
                    self._json(h, 404, {"Code": 404, "Message": "not found", "Success": False})
                    return
                self._serve_file(h, repo_id, repo, fp)
                return
        # HF API
        if len(segs) >= 2 and segs[0] == "api" and segs[1] in ("models", "datasets"):
            rest = [unquote(x) for x in segs[2:]]
            for marker in ("revision", "tree"):
                if marker in rest:
                    i = rest.index(marker)
                    repo_id = "/".join(rest[:i])
                    rev = "/".join(rest[i + 1:])
                    repo = self.repos.get(repo_id)
                    rec["kind"] = "list"
                    if self.listing_delay:
                        time.sleep(self.listing_delay)
                    if repo is None:
                        self._json(h, 401, {"error": "Repository not found"}, {"X-Error-Code": "RepoNotFound"})
                        return
                    if not self._authorized(h, repo):
                        self._json(h, 403, {"error": "Access to model is restricted. You must be authenticated"},
                                   {"X-Error-Code": "GatedRepo"})
                        return
                    if marker == "revision":
                        if rev not in repo.revisions and rev != repo.commit:
                            self._json(h, 404, {"error": "Revision not found"}, {"X-Error-Code": "RevisionNotFound"})
                            return
                        self._json(h, 200, {"id": repo_id, "sha": repo.commit, "siblings": []})
                        return
                    if rev != repo.commit:
                        self._json(h, 404, {"error": "Revision not found"}, {"X-Error-Code": "RevisionNotFound"})
                        return
                    entries = [{"type": "directory", "oid": "0" * 40, "size": 0, "path": "sub"}]
                    entries += [self._entry(p, d) for p, d in sorted(repo.files.items())]
                    cursor = int((q.get("cursor") or ["0"])[0])
                    page = entries[cursor:cursor + self.page_size]
                    headers = {}
                    if cursor + self.page_size < len(entries):
                        base = h.path.split("?", 1)[0]
                        headers["Link"] = f'<{base}?recursive=true&cursor={cursor + self.page_size}>; rel="next"'
                    self._json(h, 200, page, headers)
                    return
        # CDN
        if segs and segs[0] == "cdn":
            rest = [unquote(x) for x in segs[1:]]
            for n in (2, 1):
                repo_id = "/".join(rest[:n])
                if repo_id in self.repos and len(rest) > n + 1:
                    rec["kind"] = "file"
                    fp = "/".join(rest[n + 1:])
                    rec["file"] = fp
                    rec["cdn"] = True
                    self._serve_file(h, repo_id, self.repos[repo_id], fp)
                    return
        # HF resolve
        if "resolve" in segs:
            rest = [unquote(x) for x in segs]
            if rest and rest[0] == "datasets":
                rest = rest[1:]
            i = rest.index("resolve")
            repo_id = "/".join(rest[:i])
            commit = rest[i + 1] if len(rest) > i + 1 else ""
            fp = "/".join(rest[i + 2:])
            repo = self.repos.get(repo_id)
            rec.update(kind="file", file=fp, revision=commit)
            if repo is None or not self._authorized(h, repo):
                self._json(h, 401, {"error": "Repository not found"}, {"X-Error-Code": "RepoNotFound"})
                return
            if commit != repo.commit:
                self._json(h, 404, {"error": "Revision not found"}, {"X-Error-Code": "RevisionNotFound"})
                return
            if self.cdn_host:
                loc = f"http://{self.cdn_host}:{self.port}/cdn/{repo_id}/{commit}/{quote(fp)}"
                self._send(h, 302, b"", {"Location": loc})
                return
            self._serve_file(h, repo_id, repo, fp)
            return
        self._json(h, 404, {"error": "no route", "path": h.path})


__all__ = ["HFStub", "Repo", "git_sha1", "LFS_MIN"]
