"""Approvals at the entry server, time-boxed grants and FileHolds (verify-and-hold of bound files)
(design §5.6, D13, WP1).

An approval is a property of the agent token and is decided only by the server that authenticated it. A record binds
token name + token sha256 + node + action + ``sha256(canonical_json({"a": action, "p": params}))``; it is single use,
expires after ``ttl`` (600 s) and only owner tokens from an allowed source (loopback by default; proxied requests are
never loopback) decide. ``require`` returns the consumed record's file fingerprints; the node verifies and holds them
in one step right before the handler runs (``FileHolds.acquire``, §13.2 step 8a).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import secrets
import stat as _stat
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Set

from .errors import (HiveError, approval_required, bad_request, conflict, forbidden, not_found, rate_limited,
                     too_large)
from .util import canonical_json, ip_category, iso_utc, parse_iso, read_json, sha256_text, write_json_atomic

if TYPE_CHECKING:  # pragma: no cover
    from .auth import Identity
    from .policy import EffectivePolicy

log = logging.getLogger("hive.approvals")

APPROVAL_ID_PATTERN = r"^ap_[A-Za-z0-9_-]{6,40}$"
GRANT_ID_PATTERN = r"^gr_[A-Za-z0-9_-]{6,40}$"
REVIEW_MAX = 256 * 1024
FILES_SHOWN_MAX = 256 * 1024
SUMMARY_MAX = 4096
HASH_MAX = 16 * 1024 * 1024
KEEP_DONE_S = 86400
MAX_RECORDS = 500


def params_hash(action: str, params: dict) -> str:
    """``sha256(canonical_json({"a": action, "p": params}))``."""
    return sha256_text(canonical_json({"a": action, "p": params}))


def file_fingerprint(path: str, *, prefer_stat: bool = False) -> Optional[str]:
    """``"sha256:<hex>"`` for regular files <= 16 MiB, else ``"stat:<size>:<mtime ISO UTC>"``; None when absent."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    if not _stat.S_ISREG(st.st_mode):
        return f"stat:{st.st_size}:{iso_utc(st.st_mtime)}"
    if prefer_stat or st.st_size > HASH_MAX:
        return f"stat:{st.st_size}:{iso_utc(st.st_mtime)}"
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _entry_fingerprint(entry: dict) -> Optional[str]:
    """Fingerprint of a review ``files`` entry (§5.6 v8 shape)."""
    fp = entry.get("fingerprint")
    if isinstance(fp, str) and fp:
        return fp
    bind = entry.get("bind")
    if bind == "absent":
        return None
    sha = entry.get("sha256")
    if bind == "stat":
        if isinstance(sha, str) and sha.startswith("stat:"):
            return sha
        return f"stat:{entry.get('size')}:{entry.get('mtime')}"
    if isinstance(sha, str) and sha:
        return sha if sha.startswith(("sha256:", "stat:")) else "sha256:" + sha
    return None


class ApprovalStore:
    """Approval records and grants in approvals.json (0600) (§5.6)."""

    def __init__(self, path: Path, *, ttl: int, max_pending: int, where: str,
                 decide_from: Sequence[str] = ("loopback",), max_grant_minutes: int = 240):
        self.path = Path(path)
        self.ttl = int(ttl)
        self.max_pending = int(max_pending)
        self.where = where
        self.decide_from = tuple(decide_from or ("loopback",))
        self.max_grant_minutes = int(max_grant_minutes)
        self._lock = threading.RLock()
        self._records: Dict[str, dict] = {}
        self._grants: Dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            raw = read_json(self.path, default=None)
        except HiveError as e:
            log.error("approvals file unreadable (%s); starting empty", e.message)
            raw = None
        if isinstance(raw, dict):
            for r in raw.get("records") or []:
                if isinstance(r, dict) and isinstance(r.get("id"), str):
                    self._records[r["id"]] = r
            for g in raw.get("grants") or []:
                if isinstance(g, dict) and isinstance(g.get("id"), str):
                    self._grants[g["id"]] = g

    def _save(self) -> None:
        now = time.time()
        done = [r for r in self._records.values() if r.get("status") != "pending"]
        for r in done:
            ts = r.get("decided_at") or r.get("created")
            try:
                old = now - parse_iso(ts) > KEEP_DONE_S
            except Exception:  # noqa: BLE001
                old = True
            if old:
                self._records.pop(r["id"], None)
        if len(self._records) > MAX_RECORDS:
            for r in sorted(self._records.values(), key=lambda x: x.get("created", ""))[:len(self._records) - MAX_RECORDS]:
                self._records.pop(r["id"], None)
        for g in list(self._grants.values()):
            if self._until(g) <= now:
                self._grants.pop(g["id"], None)
        try:
            write_json_atomic(self.path, {"version": 1, "records": list(self._records.values()),
                                          "grants": list(self._grants.values())}, private=True)
        except OSError as e:
            log.error("approvals file could not be written: %s", e)

    @staticmethod
    def _until(g: dict) -> float:
        try:
            return parse_iso(g.get("until", ""))
        except Exception:  # noqa: BLE001
            return 0.0

    def _expire(self, now: float) -> None:
        for r in self._records.values():
            if r.get("status") in ("pending", "approved"):
                try:
                    exp = parse_iso(r["expires"])
                except Exception:  # noqa: BLE001
                    exp = 0.0
                if exp <= now:
                    r["status"] = "expired"

    # ------------------------------------------------------------------------------------------- requests
    def _how(self, aid: str) -> str:
        return (f"hive approvals approve {aid} (run on {self.where} as owner; add --for 30m to allow this action for "
                f"30 minutes)")

    def _required(self, rec: dict) -> HiveError:
        return approval_required(approval_id=rec["id"], expires_at=rec["expires"], where=self.where,
                                 how=self._how(rec["id"]))

    @staticmethod
    def _review_size(review: dict) -> int:
        total = len(canonical_json(review).encode("utf-8"))
        shown = 0
        for f in review.get("files") or []:
            if isinstance(f, dict) and f.get("shown") and isinstance(f.get("size"), int):
                shown += f["size"]
        return total - min(shown, FILES_SHOWN_MAX)

    def require(self, identity: "Identity", node: str, action: str, params: dict, review: dict,
                approval_id: Optional[str]) -> Dict[str, Optional[str]]:
        """Consume an approved record (returns its file fingerprints) or create one and raise approval_required."""
        h = params_hash(action, params)
        now = time.time()
        with self._lock:
            self._expire(now)
            if approval_id:
                if not isinstance(approval_id, str) or not re.match(APPROVAL_ID_PATTERN, approval_id):
                    raise bad_request("invalid approval_id", hint="pass the ap_… id from approval_required")
                rec = self._records.get(approval_id)
                if rec is None:
                    raise forbidden(f"approval {approval_id} is unknown here", hint="request approval again")
                if (rec.get("requester") != identity.name or rec.get("requester_key", "") != identity.token_sha256
                        or rec.get("node") != node or rec.get("action") != action or rec.get("params_hash") != h):
                    raise forbidden(f"approval {approval_id} does not match this call",
                                    hint="an approval covers exactly the call it was requested for (same token, "
                                         "action and parameters); request approval again")
                st = rec.get("status")
                if st == "pending":
                    raise self._required(rec)
                if st != "approved":
                    raise forbidden(f"approval {approval_id} is {st}", hint="request approval again")
                rec["status"] = "used"
                rec["used_at"] = iso_utc(now)
                self._save()
                want = rec.get("cwd")
                if isinstance(want, str) and want:
                    got = review.get("cwd") if isinstance(review, dict) else None
                    if got != want:     # v8.1: the cwd the words were bound against must be the cwd it runs in
                        raise conflict("the working folder of the approved command changed; request approval again",
                                       changed=[want], hint="request approval again; the owner sees the new folder")
                return dict(rec.get("files") or {})
            for rec in self._records.values():
                if (rec.get("status") == "pending" and rec.get("requester") == identity.name
                        and rec.get("requester_key", "") == identity.token_sha256 and rec.get("node") == node
                        and rec.get("action") == action and rec.get("params_hash") == h):
                    raise self._required(rec)
            review = review if isinstance(review, dict) else {"action": action, "lines": [], "changes_what_runs": [],
                                                                "files": []}
            if self._review_size(review) > REVIEW_MAX:
                raise too_large("the approval review is larger than 256 KiB", hint="split the input or ask the owner")
            pending = [r for r in self._records.values() if r.get("status") == "pending"]
            if len(pending) >= self.max_pending:
                oldest = min(parse_iso(r["expires"]) for r in pending)
                raise rate_limited("too many approval requests are pending", retry_after=max(1.0, oldest - now),
                                   reason="approvals_pending", hint="wait for the owner to decide the open requests")
            files: Dict[str, Optional[str]] = {}
            for f in review.get("files") or []:
                if isinstance(f, dict) and f.get("bind", "sha256") != "none" and isinstance(f.get("path"), str):
                    files[f["path"]] = _entry_fingerprint(f)
            from .audit import redact_params
            summary = canonical_json(redact_params(action, params))
            if len(summary) > SUMMARY_MAX:
                summary = summary[:SUMMARY_MAX] + f"[TRUNCATED, {len(summary) - SUMMARY_MAX} bytes hidden]"
            aid = "ap_" + secrets.token_urlsafe(15)
            rec = {"id": aid, "created": iso_utc(now), "expires": iso_utc(now + self.ttl), "requester": identity.name,
                   "requester_key": identity.token_sha256, "agent": identity.agent or None, "node": node,
                   "action": action, "params_hash": h, "summary": summary, "review": review, "files": files,
                   "status": "pending", "decided_by": None, "decided_at": None, "note": ""}
            if isinstance(review.get("cwd"), str) and review.get("cwd"):
                rec["cwd"] = review["cwd"]     # v8.1: the real cwd of the review, checked again on consume
            self._records[aid] = rec
            self._save()
            raise self._required(rec)

    # ------------------------------------------------------------------------------------------- decisions
    def may_decide(self, identity: "Identity") -> bool:
        """Owner token from an allowed source (``loopback`` default, ``private`` opt-in, ``any``)."""
        if identity is None or identity.kind != "owner":
            return False
        if "any" in self.decide_from:
            return True
        ip = (identity.source_ip or "").strip()
        if not ip or ip.startswith("proxy:"):
            return False
        try:
            cat = ip_category(ip)
        except HiveError:
            return False
        if cat == "loopback" and "loopback" in self.decide_from:
            return True
        if cat in ("private", "loopback") and "private" in self.decide_from:
            return True
        return False

    def list(self, include_done: bool = False) -> List[dict]:
        """Records (pending only unless ``include_done``), oldest first."""
        with self._lock:
            self._expire(time.time())
            recs = [dict(r) for r in self._records.values() if include_done or r.get("status") == "pending"]
        return sorted(recs, key=lambda r: r.get("created", ""))

    def get(self, approval_id: str) -> dict:
        """One record (not_found when unknown)."""
        with self._lock:
            rec = self._records.get(approval_id)
            if rec is None:
                raise not_found(f"approval {approval_id} not found")
            return dict(rec)

    def decide(self, approval_id: str, approve: bool, decider: "Identity", note: str = "",
               grant_minutes: Optional[int] = None) -> dict:
        """Approve/deny a pending record (owner from an allowed source); ``grant_minutes`` also records a grant."""
        if not self.may_decide(decider):
            raise forbidden("only the owner may decide approvals, from this machine",
                            hint="run hive approvals approve on the machine that asked (owner token, loopback)")
        if grant_minutes is not None:
            if not approve:
                raise bad_request("grant_minutes is only valid when approving")
            if not isinstance(grant_minutes, int) or isinstance(grant_minutes, bool) or \
                    not (1 <= grant_minutes <= self.max_grant_minutes):
                raise bad_request(f"grant_minutes must be between 1 and {self.max_grant_minutes}")
        now = time.time()
        with self._lock:
            self._expire(now)
            rec = self._records.get(approval_id)
            if rec is None:
                raise not_found(f"approval {approval_id} not found")
            if rec.get("status") != "pending":
                raise conflict(f"approval {approval_id} is already {rec.get('status')}")
            rec["status"] = "approved" if approve else "denied"
            rec["decided_by"] = decider.name
            rec["decided_at"] = iso_utc(now)
            rec["note"] = str(note or "")[:500]
            if approve:
                rec["expires"] = iso_utc(max(parse_iso(rec["expires"]), now + self.ttl))
            grant = None
            if approve and grant_minutes:
                gid = "gr_" + secrets.token_urlsafe(15)
                grant = {"id": gid, "identity": rec["requester"], "identity_key": rec.get("requester_key", ""),
                         "node": rec["node"], "action": rec["action"], "until": iso_utc(now + grant_minutes * 60),
                         "decided_by": decider.name, "created": iso_utc(now)}
                self._grants[gid] = grant
            self._save()
            out = dict(rec)
            out["grant"] = dict(grant) if grant else None
            return out

    # ------------------------------------------------------------------------------------------- grants
    def granted(self, identity: "Identity", node: str, action: str) -> Optional[str]:
        """Id of a valid grant for (token name + sha256, node, action), else None."""
        now = time.time()
        with self._lock:
            for g in self._grants.values():
                if (g.get("identity") == identity.name and g.get("identity_key", "") == identity.token_sha256
                        and g.get("node") == node and g.get("action") == action and self._until(g) > now):
                    return g["id"]
        return None

    def grants(self) -> List[dict]:
        """Valid grants."""
        now = time.time()
        with self._lock:
            return [dict(g) for g in self._grants.values() if self._until(g) > now]

    def revoke(self, grant_id: Optional[str] = None) -> int:
        """Revoke one grant (by id) or all of them (None); returns the number removed."""
        with self._lock:
            if grant_id is None:
                n = len(self._grants)
                self._grants.clear()
            else:
                n = 1 if self._grants.pop(grant_id, None) is not None else 0
            if n:
                self._save()
            return n


class FileHolds:
    """Verify-and-hold of an approved call's bound files (v8, §5.6)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._holds: Dict[str, Set[str]] = {}
        self._count: Dict[str, int] = {}
        self._paths: Dict[str, List[str]] = {}
        self._fps: Dict[str, Dict[str, Optional[str]]] = {}

    def acquire(self, files: Dict[str, Optional[str]], *, policy: "EffectivePolicy") -> str:
        """Re-fingerprint every path through ``policy.resolve_path`` and hold them; ``conflict`` when any changed."""
        with self._lock:
            changed: List[str] = []
            keys: Set[str] = set()
            reals: List[str] = []
            fps: Dict[str, Optional[str]] = {}
            for p, expected in (files or {}).items():
                try:
                    real = policy.resolve_path(p)
                    cur = file_fingerprint(str(real), prefer_stat=bool(expected and expected.startswith("stat:")))
                    if expected is not None and expected.startswith("sha256:") and cur is not None \
                            and cur.startswith("stat:"):
                        cur = "changed"
                    keyfn = getattr(policy, "_key", None)
                    key = keyfn(real) if callable(keyfn) else str(real)
                except Exception:  # noqa: BLE001 - any error counts as changed
                    changed.append(p)
                    continue
                if cur != expected:
                    changed.append(p)
                    continue
                keys.add(key)
                reals.append(str(real))
                fps[str(real)] = expected
            if changed:
                raise conflict("a file used by the approved command changed; request approval again",
                               changed=changed, hint="request approval again; the owner sees the new content")
            hid = "ho_" + secrets.token_urlsafe(12)
            self._holds[hid] = keys
            self._paths[hid] = reals
            self._fps[hid] = fps
            for k in keys:
                self._count[k] = self._count.get(k, 0) + 1
            return hid

    def release(self, hold_id: str) -> None:
        """Release a hold (unknown ids are ignored)."""
        with self._lock:
            keys = self._holds.pop(hold_id, None)
            self._paths.pop(hold_id, None)
            self._fps.pop(hold_id, None)
            for k in keys or ():
                n = self._count.get(k, 0) - 1
                if n <= 0:
                    self._count.pop(k, None)
                else:
                    self._count[k] = n

    def held(self, path_key: str) -> bool:
        """Path (normalized key) currently held."""
        with self._lock:
            return path_key in self._count

    def held_keys(self) -> List[str]:
        """All currently held keys (PathGuard flags held files below a folder that is moved, copied over or deleted)."""
        with self._lock:
            return list(self._count)

    def paths(self, hold_id: str) -> List[str]:
        """Real paths of a hold (job records and service run state store them)."""
        with self._lock:
            return list(self._paths.get(hold_id, []))

    def fingerprints(self, hold_id: str) -> Dict[str, Optional[str]]:
        """``{real path: verified fingerprint}`` of a hold (None: bound as absent). v8.1: service run state and job
        records keep them, so a respawn, autostart or re-hold after a node restart checks the APPROVED content."""
        with self._lock:
            return dict(self._fps.get(hold_id, {}))


__all__ = ["APPROVAL_ID_PATTERN", "GRANT_ID_PATTERN", "ApprovalStore", "FileHolds", "params_hash",
           "file_fingerprint"]
