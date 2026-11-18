"""WP3b tests: model.pull (§9.9, §11.3 pull mode, §11.5 hfhub) against tests/fixtures/hfstub.py — through a real
NodeRuntime (jobs) and in-process ``_download.run`` where DNS must be stubbed (CDN hop, proxy)."""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from hive import _download, hfhub, netproxy, netutil, util
from hive.errors import HiveError
from hive.netutil import AuthSpec, NetGuard
from tests import helpers, wp2util
from tests.fixtures.connect_proxy import ConnectProxy
from tests.fixtures.hfstub import LFS_MIN, HFStub, Repo, git_sha1
from tests.test_download import RunDir, guard, resolving

BIG = os.urandom(150000)
FILES = {"config.json": b'{"model_type": "qwen2"}', "model-00001-of-00002.safetensors": BIG,
         "model-00002-of-00002.safetensors": os.urandom(60000), "pytorch_model.bin": os.urandom(5000),
         "tokenizer/tokenizer.json": b'{"v": 1}' * 300, "README.md": b"# readme\n"}
REPO = "org/model"
READNETWRITE = wp2util.ident(("read", "write", "net"), name="rnw")


def wait_job(node: Any, jid: str, timeout: float = 60) -> dict:
    st = node.ok("job.status", {"job_id": jid, "wait": timeout})
    if st["state"] in ("starting", "running"):
        raise AssertionError(f"job still running: {st}")
    return st


class PullCase(unittest.TestCase):
    """A real node whose models.hf_endpoint is the hfstub."""

    repos: Dict[str, Repo] = {}

    def setUp(self) -> None:
        hfhub.clear_cache()
        self.addCleanup(hfhub.clear_cache)
        self.hf = HFStub({REPO: Repo(FILES), "org/gated": Repo({"w.safetensors": b"g" * 2000}, gated=True)},
                         token="hf_goodtoken123").start()
        self.addCleanup(self.hf.stop)
        self.node_cm = wp2util.Node(models={"hf_endpoint": self.hf.url, "modelscope_endpoint": self.hf.url,
                                            "dir": "~/models", "max_concurrent_pulls": 2},
                                    **self.node_overrides())
        self.node = self.node_cm.__enter__()
        self.addCleanup(self.node_cm.__exit__, None, None, None)
        self.models = self.node.home / "models"

    def node_overrides(self) -> dict:
        return {}

    def pull(self, **params: Any) -> dict:
        p = {"repo": REPO}
        p.update(params)
        return self.node.ok("model.pull", p)

    def pull_err(self, ident: Any = wp2util.OPERATOR, **params: Any) -> dict:
        p = {"repo": REPO}
        p.update(params)
        return self.node.err("model.pull", p, identity=ident)


class TestPlan(PullCase):
    def test_dry_run_shape_and_filters(self):
        r = self.pull(dry_run=True)
        wp2util.assert_result_schema(self, "model.pull", r)
        self.assertEqual((r["repo"], r["source"], r["endpoint"], r["revision"]), (REPO, "huggingface", self.hf.url,
                                                                                    "main"))
        self.assertEqual(r["commit"], self.hf.repos[REPO].commit)
        self.assertEqual(r["path"], str(self.models / "org--model"))
        self.assertEqual(r["skipped_pickle"], ["pytorch_model.bin"])
        self.assertEqual(r["file_count"], 5)
        self.assertEqual(r["total_bytes"], sum(len(v) for k, v in FILES.items() if k != "pytorch_model.bin"))
        self.assertEqual(r["to_download_bytes"], r["total_bytes"])
        self.assertEqual(r["files"][0]["path"], "model-00001-of-00002.safetensors")      # largest first
        self.assertTrue(all(f["done"] is False for f in r["files"]))
        self.assertIsInstance(r["fits"], bool)
        self.assertFalse((self.models / "org--model").exists())                         # dry run writes nothing
        r = self.pull(dry_run=True, include=["*.json"], exclude=["tokenizer/*"])
        self.assertEqual([f["path"] for f in r["files"]], ["config.json"])
        r = self.pull(dry_run=True, prefer_safetensors=False, include=["*.bin"])
        self.assertEqual([f["path"] for f in r["files"]], ["pytorch_model.bin"])
        r = self.pull(dry_run=True, include=["*.bin", "*.safetensors"])
        self.assertIn("pytorch_model.bin", [f["path"] for f in r["files"]])           # explicitly included
        e = self.pull_err(dry_run=True, max_files=2)
        self.assertEqual(e["code"], "too_large")
        self.assertIn("include", e["hint"])

    def test_repo_url_forms_and_validation(self):
        r = self.pull(repo=f"https://huggingface.co/{REPO}/tree/main", dry_run=True)
        self.assertEqual((r["repo"], r["revision"]), (REPO, "main"))
        r = self.pull(repo=f"https://hf-mirror.com/{REPO}", dry_run=True)
        self.assertEqual(r["repo"], REPO)
        self.assertEqual(hfhub.normalize_repo("https://www.modelscope.cn/models/Qwen/Qwen2.5-7B", "modelscope"),
                         ("Qwen/Qwen2.5-7B", None))
        self.assertEqual(hfhub.normalize_repo("gpt2", "huggingface"), ("gpt2", None))
        for bad in ("a/b/c", "../x", "a/..b", "-x/y", "a b"):
            with self.subTest(bad=bad):
                self.assertEqual(self.pull_err(repo=bad, dry_run=True)["code"], "bad_request")
        self.assertEqual(self.pull_err(revision="../x", dry_run=True)["code"], "bad_request")
        e = self.pull_err(source="modelscope", repo_type="dataset", dry_run=True)
        self.assertEqual(e["code"], "unsupported")

    def test_unsafe_names_skipped(self):
        files = dict(FILES)
        files["con.txt"] = b"x"
        files["bad:name.json"] = b"x"
        self.hf.repos[REPO] = Repo(files)
        r = self.pull(dry_run=True)
        self.assertEqual(sorted(r["skipped_invalid"]), ["bad:name.json", "con.txt"])
        self.assertNotIn("con.txt", [f["path"] for f in r["files"]])

    def test_hive_names_and_case_duplicates(self):
        """Review fix: repo files named like Hive's manifest/lock or ``*.hive-part``, or with a segment the
        filesystem cannot hold, are skipped_invalid; names that collide after casefolding are skipped_unsafe."""
        files = dict(FILES)
        files[".hive-pull.json"] = b"{}"
        files[".hive-pull.lock"] = b"x"
        files["config.json.hive-part"] = b"x"
        files["sub/" + "模型" * 42 + ".bin"] = b"x"         # 252 bytes + .hive-part > NAME_MAX
        files["readme.md"] = b"# lower\n"
        self.hf.repos[REPO] = Repo(files)
        r = self.pull(dry_run=True)
        self.assertEqual(sorted(r["skipped_invalid"]), sorted([".hive-pull.json", ".hive-pull.lock",
                                                               "config.json.hive-part", "sub/" + "模型" * 42 + ".bin"]))
        self.assertIn("readme.md", [f["path"] for f in r["files"]])       # case-sensitive here: two files
        from hive import policy as policy_mod
        real_key = policy_mod.EffectivePolicy.path_key

        def folded(self_: Any, path: Any, *, follow: bool = True) -> str:
            return real_key(self_, path, follow=follow).casefold()
        with mock.patch.object(policy_mod.EffectivePolicy, "path_key", folded):
            r = self.pull(dry_run=True)
        names = [f["path"] for f in r["files"]]
        self.assertEqual(sum(1 for n in names if n.casefold() == "readme.md"), 1)
        self.assertIn("would be skipped (unsafe destination)", r["note"])

    def test_symlinked_lock_refused(self):
        """Review fix: model.pull refuses a target whose .hive-pull.lock / .hive-pull.json is a symbolic link."""
        t = self.models / "org--model"
        t.mkdir(parents=True)
        victim = self.node.home / "victim.txt"
        victim.write_text("precious")
        for name in (".hive-pull.lock", ".hive-pull.json"):
            with self.subTest(name=name):
                os.symlink(str(victim), str(t / name))
                e = self.pull_err(overwrite=True)
                self.assertEqual(e["code"], "conflict")
                self.assertIn("symbolic link", e["message"])
                os.unlink(str(t / name))
        self.assertEqual(victim.read_text(), "precious")

    def test_errors(self):
        e = self.pull_err(repo="org/nope", dry_run=True)
        self.assertEqual(e["code"], "not_found")
        self.assertIn("check the repo id/revision", e["hint"])
        e = self.pull_err(revision="v9", dry_run=True)
        self.assertEqual(e["code"], "not_found")
        e = self.pull_err(repo="org/gated", dry_run=True)
        self.assertEqual(e["code"], "forbidden")
        self.assertIn("accept the license at https://huggingface.co/org/gated", e["hint"])
        self.assertIn("test-node", e["hint"])

    def test_targets_refused(self):
        for path, rule in (("~", "protected_path"), ("/", "protected_path")):
            with self.subTest(path=path):
                e = self.pull_err(path=path, dry_run=True)
                self.assertEqual(e["code"], "policy_denied")
        e = self.pull_err(path="~/.ssh/models", dry_run=True)
        self.assertEqual(e["code"], "policy_denied")

    def test_existing_target_conflicts(self):
        t = self.models / "org--model"
        t.mkdir(parents=True)
        (t / "other.txt").write_text("mine")
        e = self.pull_err(dry_run=True)
        self.assertEqual(e["code"], "conflict")
        (t / "other.txt").unlink()
        util.write_json_atomic(t / ".hive-pull.json", {"source": "huggingface", "repo": REPO, "files": {}})
        (t / "config.json").write_text("different size!")
        e = self.pull_err(dry_run=True)
        self.assertEqual(e["code"], "conflict")
        self.assertEqual(e["details"]["paths"], ["config.json"])
        r = self.pull(dry_run=True, overwrite=True)
        self.assertEqual(r["file_count"], 5)

    def test_disk_preflight(self):
        usage = mock.Mock(free=1000, total=10 ** 9, used=0)
        with mock.patch("hive.actions.model.shutil.disk_usage", return_value=usage):
            r = self.pull(dry_run=True)
            self.assertEqual((r["fits"], r["disk_free"]), (False, 1000))
            e = self.pull_err()
        self.assertEqual(e["code"], "too_large")
        self.assertEqual(e["details"]["free"], 1000)
        self.assertGreater(e["details"]["need"], 1 << 30)
        self.assertIn("path", e["details"])

    def test_token_rules(self):
        # HF_TOKEN with its default hosts (huggingface.co, …) is not bound to the mirror: anonymous + note
        self.node.rt.secrets.set("HF_TOKEN", "hf_goodtoken123")
        r = self.pull(dry_run=True)
        self.assertIn("HF_TOKEN exists on test-node but is not bound to 127.0.0.1", r["note"])
        self.assertIn("--hosts +127.0.0.1", r["note"])
        self.assertIn("HIVE_SECRET_HOSTS_HF_TOKEN=+127.0.0.1", r["note"])
        self.assertTrue(all("Authorization" not in q["headers"] for q in self.hf.requests))
        e = self.pull_err(repo="org/gated", dry_run=True)
        self.assertEqual(e["code"], "forbidden")
        self.assertIn("hive node secret set HF_TOKEN --hosts +127.0.0.1", e["hint"])
        e = self.pull_err(dry_run=True, auth_env="HF_TOKEN")
        self.assertEqual((e["code"], e["details"]["rule"]), ("policy_denied", "secret_host"))
        self.assertIn("--hosts +127.0.0.1", e["hint"])
        e = self.pull_err(dry_run=True, auth_env="NOPE_TOKEN")
        self.assertEqual(e["code"], "not_found")
        # bound to loopback: sent (plain http to loopback is allowed for "loopback" entries)
        self.node.rt.secrets.set("HF_TOKEN", "hf_goodtoken123", hosts=["loopback"])
        r = self.pull(repo="org/gated", dry_run=True)
        self.assertNotIn("note", r)
        self.assertEqual(self.hf.requests[-1]["headers"].get("Authorization"), "Bearer hf_goodtoken123")
        n = len(self.hf.requests)
        hfhub.clear_cache()
        self.pull(dry_run=True, auth_env="none")      # anonymous on request
        self.assertTrue(all("Authorization" not in q["headers"] for q in self.hf.requests[n:]))
        self.assertEqual(self.pull_err(repo="org/gated", dry_run=True, auth_env="none")["code"], "forbidden")


class TestPullJobs(PullCase):
    def test_full_pull_then_resume_from_manifest(self):
        r = self.pull()
        wp2util.assert_result_schema(self, "model.pull", r)
        self.assertEqual(r["state"], "running")
        st = wait_job(self.node, r["job_id"])
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0), st)
        res = st["result"]
        t = self.models / "org--model"
        self.assertEqual(res["path"], str(t))
        self.assertEqual((res["files"], res["skipped_existing"], res["stale"]), (5, 0, []))
        self.assertEqual(res["skipped_pickle"], ["pytorch_model.bin"])
        self.assertEqual(res["downloaded_bytes"], res["bytes"])
        self.assertEqual(res["manifest"], str(t / ".hive-pull.json"))
        for name, data in FILES.items():
            if name != "pytorch_model.bin":
                self.assertEqual((t / name).read_bytes(), data, name)
        self.assertFalse((t / "pytorch_model.bin").exists())
        self.assertFalse((t / ".hive-pull.lock").exists())
        man = json.loads((t / ".hive-pull.json").read_text())
        self.assertEqual((man["repo"], man["commit"], man["source"]), (REPO, self.hf.repos[REPO].commit,
                                                                       "huggingface"))
        self.assertIsNotNone(man["completed_at"])
        extra = self.node.rt.audit.tail(10, action="model.pull")[-1]["extra"]      # §5.4 NetGuard rule 8
        self.assertEqual((extra["route"], extra["dns"], extra["job_id"]), ("direct", "local", r["job_id"]))
        self.assertEqual(man["files"]["model-00001-of-00002.safetensors"]["hash"],
                         "sha256:" + hashlib.sha256(BIG).hexdigest())
        self.assertEqual(man["files"]["config.json"]["hash"], "sha1:" + git_sha1(FILES["config.json"]))
        # commit pinning: every file URL used the commit sha
        commit = self.hf.repos[REPO].commit
        frs = [q for q in self.hf.requests if q["kind"] == "file"]
        self.assertTrue(frs and all(q["revision"] == commit for q in frs))
        self.assertEqual(st["progress"]["files_total"], 5)
        # same call again: everything verified from the manifest, nothing downloaded
        n = len(self.hf.requests)
        r2 = self.pull()
        self.assertEqual(r2["to_download_bytes"], 0)
        st2 = wait_job(self.node, r2["job_id"])
        self.assertEqual((st2["result"]["skipped_existing"], st2["result"]["downloaded_bytes"]), (5, 0))
        self.assertFalse([q for q in self.hf.requests[n:] if q["kind"] == "file"])
        # the repo moved on: a removed file is reported stale, never deleted
        files = dict(FILES)
        files.pop("README.md")
        self.hf.repos[REPO] = Repo(files, commit="b" * 40)
        hfhub.clear_cache()
        st3 = wait_job(self.node, self.pull()["job_id"])
        self.assertEqual(st3["result"]["stale"], ["README.md"])
        self.assertTrue((t / "README.md").exists())

    def test_pagination_and_verify_full(self):
        self.hf.page_size = 2
        st = wait_job(self.node, self.pull()["job_id"])
        self.assertEqual(st["result"]["files"], 5)
        lists = [q for q in self.hf.requests if q["kind"] == "list" and "/tree/" in q["path"]]
        self.assertGreaterEqual(len(lists), 3)
        # verify=full re-hashes existing files instead of trusting the manifest (a corrupted file is re-fetched)
        t = self.models / "org--model"
        data = bytearray((t / "config.json").read_bytes())
        data[0] ^= 1
        (t / "config.json").write_bytes(bytes(data))
        st = wait_job(self.node, self.pull(verify="full")["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertEqual((t / "config.json").read_bytes(), FILES["config.json"])

    def test_hash_mismatch_retry_then_fail(self):
        self.hf.corrupt["config.json"] = 1          # once: the retry succeeds
        st = wait_job(self.node, self.pull(include=["*.json"])["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertEqual(len(self.hf.file_requests("config.json")), 2)
        shutil.rmtree(str(self.models / "org--model"))
        self.hf.corrupt["model-00001-of-00002.safetensors"] = 5
        st = wait_job(self.node, self.pull(include=["*.safetensors"])["job_id"])
        self.assertEqual(st["exit_code"], 3, st)
        self.assertIn("model-00001-of-00002.safetensors", st["error"])
        self.assertFalse((self.models / "org--model" / "model-00001-of-00002.safetensors").exists())

    def test_oversize_body_aborted(self):
        self.hf.oversize["config.json"] = 10 ** 6
        st = wait_job(self.node, self.pull(include=["config.json"])["job_id"])
        self.assertEqual(st["exit_code"], 3, st)
        part = self.models / "org--model" / "config.json.hive-part"
        self.assertFalse(part.exists() and part.stat().st_size > len(FILES["config.json"]) + 1)

    def test_range_resume_after_cut_and_after_kill(self):
        self.hf.cut["model-00001-of-00002.safetensors"] = (40000, 1)
        st = wait_job(self.node, self.pull(include=["*.safetensors"])["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        reqs = self.hf.file_requests("model-00001-of-00002.safetensors")
        self.assertGreaterEqual(len(reqs), 2)
        self.assertTrue(reqs[-1]["headers"].get("Range", "").startswith("bytes="), reqs[-1])
        # stop a slow pull, then the same call resumes from the part file
        shutil.rmtree(str(self.models / "org--model"))
        self.hf.file_delay = 0.1
        r = self.pull(include=["model-00001-of-00002.safetensors"])
        t = self.models / "org--model"
        part = t / "model-00001-of-00002.safetensors.hive-part"
        wp2util.wait_for(lambda: part.exists() and part.stat().st_size > 20000, timeout=30)
        prog = wp2util.wait_for(lambda: self.node.ok("job.status", {"job_id": r["job_id"]}).get("progress"),
                                timeout=10)
        for k in ("done", "total", "percent", "files_done", "files_total", "current", "note"):
            self.assertIn(k, prog)
        self.node.ok("job.stop", {"job_id": r["job_id"]})
        st = wait_job(self.node, r["job_id"])
        self.assertNotEqual(st["state"], "exited")
        have = part.stat().st_size
        self.assertGreater(have, 0)
        self.hf.file_delay = 0.0
        st = wait_job(self.node, self.pull(include=["model-00001-of-00002.safetensors"])["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        last = self.hf.file_requests("model-00001-of-00002.safetensors")[-1]
        self.assertEqual(last["headers"].get("Range"), f"bytes={have}-")
        self.assertEqual((t / "model-00001-of-00002.safetensors").read_bytes(), BIG)

    def test_busy_target_and_concurrency_cap(self):
        self.hf.file_delay = 0.02
        r = self.pull(include=["model-00001-of-00002.safetensors"])
        e = self.pull_err(include=["*.json"])
        self.assertEqual(e["code"], "conflict")
        self.assertEqual(e["details"]["job_id"], r["job_id"])
        r2 = self.pull(path="~/models/second", include=["model-00001-of-00002.safetensors"])
        e = self.pull_err(path="~/models/third")
        self.assertEqual((e["code"], e["details"]["reason"]), ("rate_limited", "pulls_running"))
        self.assertTrue(self.pull(path="~/models/third", dry_run=True)["file_count"])     # dry runs are not capped
        for jid in (r["job_id"], r2["job_id"]):
            self.node.ok("job.stop", {"job_id": jid})

    def test_two_concurrent_calls_one_job(self):
        self.hf.file_delay = 0.02
        self.pull(dry_run=True)        # warm the listing cache so both calls race on the start
        out: List[dict] = []

        def go() -> None:
            out.append(self.node.call("model.pull", {"repo": REPO, "include": ["*.safetensors"]}))
        ts = [threading.Thread(target=go) for _ in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(60)
        oks = [e for e in out if e.get("ok")]
        errs = [e for e in out if not e.get("ok")]
        self.assertEqual((len(oks), len(errs)), (1, 1), out)
        self.assertEqual(errs[0]["error"]["code"], "conflict")
        self.node.ok("job.stop", {"job_id": oks[0]["result"]["job_id"]})

    def test_symlink_and_deny_write_skipped_unsafe(self):
        files = dict(FILES)
        files[".git/hooks/post-checkout"] = b"#!/bin/sh\necho pwned\n"
        self.hf.repos[REPO] = Repo(files)
        t = self.models / "org--model"
        (t / "tokenizer").parent.mkdir(parents=True, exist_ok=True)
        outside = self.node.home / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(t / "tokenizer"))
        r = self.node.ok("model.pull", {"repo": REPO, "overwrite": True}, identity=READNETWRITE)
        st = wait_job(self.node, r["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertIn("tokenizer/tokenizer.json", st["result"]["skipped_unsafe"])
        self.assertIn(".git/hooks/post-checkout", st["result"]["skipped_unsafe"])
        self.assertEqual(os.listdir(str(outside)), [])
        self.assertFalse((t / ".git" / "hooks" / "post-checkout").exists())

    def test_modelscope(self):
        r = self.pull(source="modelscope", dry_run=True)
        self.assertEqual((r["source"], r["revision"], r["commit"]), ("modelscope", "master",
                                                                     self.hf.repos[REPO].commit))
        st = wait_job(self.node, self.pull(source="modelscope", path="~/models/ms")["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertEqual((self.node.home / "models" / "ms" / "config.json").read_bytes(), FILES["config.json"])
        frs = [q for q in self.hf.requests if q["kind"] == "file"]
        self.assertTrue(all(q["revision"] == self.hf.repos[REPO].commit for q in frs))
        e = self.pull_err(source="modelscope", repo="org/nope", dry_run=True)
        self.assertEqual(e["code"], "not_found")


class TestHfhubUnits(unittest.TestCase):
    def test_endpoint_for(self):
        self.assertEqual(hfhub.endpoint_for("huggingface", None, {}), "https://huggingface.co")
        self.assertEqual(hfhub.endpoint_for("huggingface", None, {"HF_ENDPOINT": "https://hf-mirror.com/"}),
                         "https://hf-mirror.com")
        self.assertEqual(hfhub.endpoint_for("huggingface", {"hf_endpoint": "https://a.example"},
                                            {"HF_ENDPOINT": "https://b.example"}), "https://a.example")
        self.assertEqual(hfhub.endpoint_for("modelscope", None, {}), "https://www.modelscope.cn")
        with self.assertRaises(HiveError):
            hfhub.endpoint_for("huggingface", {"hf_endpoint": "https://u:p@x.example"}, {})

    def test_file_url(self):
        self.assertEqual(hfhub.file_url("huggingface", "https://hf.co", "o/n", "model", "c" * 40, "a b/c#.json"),
                         "https://hf.co/o/n/resolve/" + "c" * 40 + "/a%20b/c%23.json")
        self.assertTrue(hfhub.file_url("huggingface", "https://hf.co", "o/n", "dataset", "c", "x")
                        .startswith("https://hf.co/datasets/o/n/resolve/c/"))
        self.assertEqual(hfhub.file_url("modelscope", "https://www.modelscope.cn", "o/n", "model", "abc", "d/e.bin"),
                         "https://www.modelscope.cn/api/v1/models/o/n/repo?Revision=abc&FilePath=d%2Fe.bin")

    def test_normalize(self):
        n = hfhub.normalize_repo
        self.assertEqual(n("https://huggingface.co/Qwen/Qwen2.5-7B/tree/v1.0", "huggingface"),
                         ("Qwen/Qwen2.5-7B", "v1.0"))
        self.assertEqual(n("huggingface.co/Qwen/Qwen2.5-7B", "huggingface"), ("Qwen/Qwen2.5-7B", None))
        self.assertEqual(n("https://hf-mirror.com/gpt2", "huggingface"), ("gpt2", None))
        for bad in ("", "a/b/c", "x/..", ".hidden/x", "a" * 97 + "/b"):
            with self.subTest(bad=bad):
                with self.assertRaises(HiveError):
                    n(bad, "huggingface")


class TestListingTimeout(unittest.TestCase):
    def test_slow_listing_then_cached_retry(self):
        hfhub.clear_cache()
        self.addCleanup(hfhub.clear_cache)
        with HFStub({REPO: Repo(FILES)}) as hf, wp2util.Node(models={"hf_endpoint": hf.url}) as node:
            hf.listing_delay = 1.5
            env = node.call(wp2util.OPERATOR, "model.pull", {"repo": REPO, "dry_run": True}, timeout=2) \
                if False else node.rt.call(wp2util.OPERATOR, "model.pull", {"repo": REPO, "dry_run": True},
                                           timeout=2)
            self.assertFalse(env["ok"])
            err = env["error"]
            self.assertEqual((err["code"], err["details"]["maybe_executed"]), ("timeout", False))
            self.assertIn("call again (cached)", err["hint"])
            time.sleep(2.5)
            t0 = time.monotonic()
            r = node.ok("model.pull", {"repo": REPO, "dry_run": True})
            self.assertLess(time.monotonic() - t0, 1.0)
            self.assertEqual(r["file_count"], 5)


class TestChildPull(unittest.TestCase):
    """In-process ``_download.run`` in pull mode (stubbed DNS for the CDN hop and the proxy)."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="hivepull-"))
        self.addCleanup(shutil.rmtree, str(self.tmp), True)

    def spec(self, hf: HFStub, g: Any, *, endpoint: Optional[str] = None, auth: Optional[AuthSpec] = None,
             files: Optional[Dict[str, bytes]] = None, **kw: Any) -> dict:
        repo = hf.repos[REPO]
        listed = []
        for p, d in sorted((files or repo.files).items()):
            big = len(d) >= LFS_MIN
            listed.append({"path": p, "size": len(d), "sha256": hashlib.sha256(d).hexdigest() if big else None,
                           "git_sha1": None if big else git_sha1(d)})
        s = {"v": 1, "mode": "pull", "net": g.to_dict(),
             "auth": ({"name": auth.name, "header": auth.header, "value": auth.value, "hosts": list(auth.hosts)}
                      if auth else None),
             "source": "huggingface", "endpoint": endpoint or hf.url, "repo": REPO, "repo_type": "model",
             "revision": "main", "commit": repo.commit, "target": str(self.tmp / "t"), "files": listed,
             "max_parallel": 3, "verify": "fast", "overwrite": False, "lock_file": str(self.tmp / "t" / ".hive-pull.lock")}
        s.update(kw)
        return s

    def test_token_not_sent_to_cdn_hop(self):
        with HFStub({REPO: Repo(FILES, gated=True)}, token="hf_tok12345678", cdn_host="cdn.test") as hf, \
                resolving({"cdn.test": ["127.0.0.1"], "hub.test": ["127.0.0.1"]}):
            auth = AuthSpec(name="HF_TOKEN", header="Authorization", value="Bearer hf_tok12345678",
                            hosts=("loopback",))
            with RunDir():
                code = _download.run(self.spec(hf, guard(), auth=auth))
            self.assertEqual(code, 0)
            resolves = [q for q in hf.requests if "/resolve/" in q["path"]]
            cdn = [q for q in hf.requests if q.get("cdn")]
            self.assertTrue(resolves and cdn)
            self.assertTrue(all(q["headers"].get("Authorization") == "Bearer hf_tok12345678" for q in resolves))
            self.assertTrue(all("Authorization" not in q["headers"] for q in cdn))
            # a mirror the secret is not bound to never receives it
            hf.requests.clear()
            shutil.rmtree(str(self.tmp / "t"))
            hf.repos[REPO].gated = False
            with RunDir():
                code = _download.run(self.spec(hf, guard(), auth=AuthSpec(
                    name="HF_TOKEN", header="Authorization", value="Bearer hf_tok12345678",
                    hosts=("huggingface.co",)), endpoint=f"http://hub.test:{hf.port}"))
            self.assertEqual(code, 0)
            self.assertTrue(all("Authorization" not in q["headers"] for q in hf.requests))

    def test_lock_held_exit_6(self):
        with HFStub({REPO: Repo(FILES)}) as hf:
            (self.tmp / "t").mkdir()
            other = util.FileLock(str(self.tmp / "t" / ".hive-pull.lock"))
            self.assertTrue(other.acquire(note="j_other"))
            try:
                with RunDir(), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                    code = _download.run(self.spec(hf, guard()))
            finally:
                other.release()
            self.assertEqual(code, 6)
            self.assertEqual(out.getvalue().strip().splitlines()[-1], "error: another pull into this folder is running")
            self.assertFalse([q for q in hf.requests if q["kind"] == "file"])

    def test_same_size_other_content_conflict(self):
        """W6 / §21.2: an existing file outside the manifest with the listed size but another hash fails that file
        (overwrite=false) and is left untouched."""
        with HFStub({REPO: Repo(FILES)}) as hf:
            t = self.tmp / "t"
            t.mkdir()
            other = bytes(b ^ 0x20 for b in FILES["config.json"])
            (t / "config.json").write_bytes(other)
            with RunDir(), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                code = _download.run(self.spec(hf, guard()))
            self.assertEqual(code, 1)
            last = out.getvalue().strip().splitlines()[-1]
            self.assertTrue(last.startswith("error: 1 file(s) failed: config.json"), last)
            self.assertIn("config.json exists with other content (use overwrite=true)", last)
            self.assertEqual((t / "config.json").read_bytes(), other)
            with RunDir():
                code = _download.run(self.spec(hf, guard(), overwrite=True))
            self.assertEqual((code, (t / "config.json").read_bytes()), (0, FILES["config.json"]))

    def test_dest_creates_nothing_behind_a_symlink(self):
        """Review fix (§11.3): no folder is created through a component planted as a symlink to the outside."""
        t = self.tmp / "t"
        t.mkdir()
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(str(outside), str(t / "tok"))
        with HFStub({REPO: Repo(FILES)}) as hf:
            puller = _download.Puller(self.spec(hf, guard()), guard(), None, threading.Event())
        self.assertIsNone(puller._dest("tok/sub/deeper/x.json"))
        self.assertIsNone(puller._dest("tok/x.json"))
        self.assertEqual(os.listdir(str(outside)), [])
        d = puller._dest("a/b/c.json")
        self.assertEqual(d, Path(os.path.realpath(str(t))) / "a" / "b" / "c.json")
        self.assertTrue((t / "a" / "b").is_dir())
        # Hive's own names are never a destination
        for rel in (".hive-pull.json", ".hive-pull.lock", "x.bin.hive-part"):
            self.assertIsNone(puller._dest(rel), rel)

    def test_symlinked_lock_or_manifest_refused(self):
        """Review fix: a symlinked .hive-pull.lock / .hive-pull.json is refused before anything is written."""
        with HFStub({REPO: Repo(FILES)}) as hf:
            t = self.tmp / "t"
            t.mkdir()
            victim = self.tmp / "victim.txt"
            for name in (".hive-pull.lock", ".hive-pull.json"):
                with self.subTest(name=name):
                    victim.write_text("precious")
                    os.symlink(str(victim), str(t / name))
                    with RunDir(), mock.patch("sys.stdout", new_callable=io.StringIO) as out:
                        code = _download.run(self.spec(hf, guard()))
                    self.assertEqual(code, 1)
                    self.assertIn("is a symbolic link", out.getvalue())
                    self.assertEqual(victim.read_text(), "precious")
                    self.assertTrue(os.path.islink(str(t / name)))
                    os.unlink(str(t / name))
            self.assertFalse([q for q in hf.requests if q["kind"] == "file"])

    def test_through_connect_proxy(self):
        with HFStub({REPO: Repo(FILES)}) as hf, \
                ConnectProxy(routes={f"hub.example.com:{hf.port}": ("127.0.0.1", hf.port)}) as px, \
                resolving({"hub.example.com": ["93.184.216.34"]}):
            ps = netproxy.ProxySettings(px.url, px.url, netproxy.ALWAYS_NO_PROXY, "config")
            g = guard(proxies=ps)
            ep = f"http://hub.example.com:{hf.port}"
            commit, files = hfhub.list_repo("huggingface", ep, REPO, "model", "main", guard=g, auth=None,
                                            deadline=time.monotonic() + 20)
            self.assertEqual(commit, hf.repos[REPO].commit)
            with RunDir():
                code = _download.run(self.spec(hf, g, endpoint=ep))
            self.assertEqual(code, 0)
            self.assertTrue(px.log and all(x["kind"] == "absolute" for x in px.log))
            self.assertEqual((self.tmp / "t" / "config.json").read_bytes(), FILES["config.json"])

    def test_stop_flushes_manifest(self):
        with HFStub({REPO: Repo(FILES)}) as hf:
            hf.file_delay = 0.05
            stop = threading.Event()
            threading.Timer(0.5, stop.set).start()
            with RunDir() as rd:
                code = _download.run(self.spec(hf, guard(), max_parallel=1), stop)
                self.assertFalse((rd / "result.json").exists())
            self.assertEqual(code, 7)
            man = json.loads((self.tmp / "t" / ".hive-pull.json").read_text())
            self.assertIsNone(man["completed_at"])
            self.assertFalse((self.tmp / "t" / ".hive-pull.lock").exists())



@unittest.skipUnless(os.environ.get("HIVE_LIVE_NET") == "1", "live network check (set HIVE_LIVE_NET=1)")
class TestLiveListing(unittest.TestCase):
    """Opt-in: the real HF / ModelScope listing APIs still have the shapes hfhub expects."""

    def _guard(self) -> NetGuard:
        return NetGuard(http_allow=["public"], http_deny=[], download_allow=["public"], allow_metadata_ip=False,
                        proxies=netproxy.load_proxy_settings({"proxy": None, "use_system_proxy": True}))

    def test_huggingface(self):
        commit, files = hfhub.list_repo("huggingface", "https://huggingface.co", "openai-community/gpt2", "model",
                                        "main", guard=self._guard(), auth=None, deadline=time.monotonic() + 60)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        by = {f.path: f for f in files}
        self.assertRegex(by["model.safetensors"].sha256 or "", r"^[0-9a-f]{64}$")
        self.assertRegex(by["config.json"].git_sha1 or "", r"^[0-9a-f]{40}$")

    def test_modelscope(self):
        commit, files = hfhub.list_repo("modelscope", "https://www.modelscope.cn", "Qwen/Qwen2.5-0.5B-Instruct",
                                        "model", "master", guard=self._guard(), auth=None,
                                        deadline=time.monotonic() + 60)
        self.assertRegex(commit, r"^[0-9a-f]{40}$")
        self.assertTrue(any(f.path == "config.json" and f.sha256 for f in files))


if __name__ == "__main__":
    unittest.main()
