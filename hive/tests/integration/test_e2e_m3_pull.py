"""M3 (§22.1 step 6): ``model.pull`` end to end on a real node daemon against local stub hubs.

A node (``hive init node`` + ``hive node run``) whose ``models.hf_endpoint`` is an ``hfstub`` playing a mirror (its
host 127.0.0.1 is not among ``HF_TOKEN``'s default hosts) and whose ``models.modelscope_endpoint`` is a second stub;
driven through ``hive mcp --local`` (the agent's ``model_pull`` / ``job_status`` / ``job_stop`` / ``job_logs``
tools), the Python client (owner, structured results) and the CLI (``node secret set``):

* dry run → pull → every file verified against the listing (LFS sha256 / git blob sha1), manifest written; a second
  identical call skips everything (manifest resume);
* resume: a slow pull is stopped mid-file (``job_stop``), a second pull into the same folder meanwhile is a
  ``conflict`` naming the running job, and the same call afterwards resumes the part file with ``Range`` and does not
  fetch the finished files again;
* per-file verification: a corrupted body is retried once and then fails the job (exit 3, the part is removed);
  ``verify=full`` re-fetches a file changed on disk that ``verify=fast`` trusts;
* disk-space precheck: a listing larger than the free space → dry run ``fits:false`` and ``too_large`` (need, free,
  path) without a job;
* mirror: the stored ``HF_TOKEN`` is not bound to the mirror → anonymous, the dry run's ``note`` and the gated
  error's hint name ``--hosts +127.0.0.1``; after ``hive node secret set HF_TOKEN --hosts +loopback`` the gated repo
  is pulled with the token; ``hive init node --mirror cn`` writes ``https://hf-mirror.com``;
* ModelScope: listing + files at the pinned revision, sha256-verified.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict

from hive.errors import HiveError

from tests.fixtures.hfstub import HFStub, Repo
from tests.integration import m2util
from tests.integration.m3util import IS_WINDOWS, NodeDaemon, job_id_of, wait_for, wait_job

BIG = os.urandom(600_000)
FILES: Dict[str, bytes] = {
    "config.json": b'{"model_type": "qwen2", "hidden_size": 64}',
    "tokenizer.json": b'{"v": 1}' * 200,                        # 1600 bytes: LFS (sha256)
    "model.safetensors": BIG,
    "pytorch_model.bin": os.urandom(3000),                       # dropped by prefer_safetensors
    "sub/notes.txt": b"hello\n",
}
GATED = {"w.safetensors": b"g" * 2000, "config.json": b"{}"}
MS_FILES = {"configuration.json": b'{"framework": "pytorch"}', "model.safetensors": os.urandom(5000)}
HUGE = 1 << 50                                                   # 1 PiB, listed only


class LyingHub(HFStub):
    """Lists ``org/huge``'s weights as 1 PiB (the bytes are never requested: the precheck refuses first)."""

    def _entry(self, path: str, data: bytes) -> dict:
        e = super()._entry(path, data)
        if path == "huge.safetensors":
            e["size"] = HUGE
            e["lfs"]["size"] = HUGE
        return e


def sha256(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class TestE2EM3Pull(unittest.TestCase):

    @classmethod
    def setUpClass(cls) -> None:
        cls.hf = LyingHub({"org/tiny": Repo(FILES), "org/gated": Repo(GATED, gated=True),
                           "org/huge": Repo({"huge.safetensors": b"x" * 2000, "config.json": b"{}"})},
                          token="hf_m3goodtoken").start()
        cls.ms = HFStub({"Qwen/tiny-ms": Repo(MS_FILES)}).start()

        def cfg(raw: dict) -> None:
            raw.setdefault("models", {}).update(hf_endpoint=cls.hf.url, modelscope_endpoint=cls.ms.url,
                                                dir="~/models")
        cls.node = NodeDaemon("m3pull", init_args=("--proxy", "off", "--mirror", "none"), cfg_edit=cfg)
        try:
            cls.node.start()
            # HF_TOKEN with its default hosts (huggingface.co, …): not bound to the mirror stub on 127.0.0.1
            cls.node.secret_set("HF_TOKEN", "hf_m3goodtoken")
            cls.mcp = cls.node.mcp()
        except BaseException:
            cls.tearDownClass()
            raise
        cls.models = cls.node.user / "models"

    @classmethod
    def tearDownClass(cls) -> None:
        node = getattr(cls, "node", None)
        if node is not None:
            node.stop()
        for s in (getattr(cls, "hf", None), getattr(cls, "ms", None)):
            if s is not None:
                s.stop()

    def setUp(self) -> None:
        self.hf.file_delay = 0.0
        self.hf.corrupt.clear()

    # -------------------------------------------------------------------------------------------------------------
    def tool(self, name: str, args: dict, *, error: bool = False) -> str:
        text, is_err = self.mcp.tool(name, args)
        self.assertEqual(is_err, error, f"{name}: {text}")
        return text

    def pull(self, **params: Any) -> dict:
        p = {"repo": "org/tiny"}
        p.update(params)
        return self.node.call("model.pull", p)

    def pull_err(self, **params: Any) -> HiveError:
        with self.assertRaises(HiveError) as cm:
            self.pull(**params)
        return cm.exception

    def assert_tree(self, target: Path, files: Dict[str, bytes]) -> None:
        for name, data in files.items():
            p = target / name
            self.assertTrue(p.is_file(), name)
            self.assertEqual(sha256(p.read_bytes()), sha256(data), name)

    # -------------------------------------------------------------------------------------------------------------
    def test_01_pull_verify_and_manifest_resume(self) -> None:
        text = self.tool("model_pull", {"repo": "org/tiny", "dry_run": True})
        self.assertIn("org/tiny", text)
        self.assertIn(self.hf.url, text)
        dry = self.pull(dry_run=True)
        self.assertEqual(dry["commit"], self.hf.repos["org/tiny"].commit)
        self.assertEqual(dry["file_count"], 4)                   # pytorch_model.bin dropped (prefer_safetensors)
        self.assertEqual(dry["skipped_pickle"], ["pytorch_model.bin"])
        self.assertTrue(dry["fits"])
        self.assertIn("HF_TOKEN exists on m3pull but is not bound to 127.0.0.1", dry["note"])
        self.assertIn("--hosts +127.0.0.1", dry["note"])

        text = self.tool("model_pull", {"repo": "org/tiny"})
        jid = job_id_of(text)
        text = self.tool("job_status", {"job_id": jid, "wait": 60})
        st = wait_job(self.node, jid)
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 0), st)
        target = self.models / "org--tiny"
        res = st["result"]
        self.assertEqual(Path(res["path"]).resolve(), target.resolve())
        self.assertEqual((res["files"], res["skipped_existing"]), (4, 0), res)
        self.assertEqual(res["skipped_pickle"], ["pytorch_model.bin"])
        self.assertEqual(res["downloaded_bytes"], sum(len(v) for k, v in FILES.items() if k != "pytorch_model.bin"))
        self.assert_tree(target, {k: v for k, v in FILES.items() if k != "pytorch_model.bin"})
        self.assertFalse((target / "pytorch_model.bin").exists())
        manifest = json.loads((target / ".hive-pull.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest.get("repo"), "org/tiny")
        self.assertFalse(list(target.rglob("*.hive-part")))
        # the agent never sent a token to the mirror
        self.assertTrue(all("Authorization" not in q["headers"] for q in self.hf.requests))
        logs = self.tool("job_logs", {"job_id": jid})
        self.assertIn(jid, logs)

        # the same call again: everything is verified-complete in the manifest, nothing is downloaded
        n_files = len([q for q in self.hf.requests if q["kind"] == "file"])
        st = wait_job(self.node, self.pull()["job_id"])
        self.assertEqual((st["exit_code"], st["result"]["skipped_existing"], st["result"]["downloaded_bytes"]),
                         (0, 4, 0), st)
        self.assertEqual(len([q for q in self.hf.requests if q["kind"] == "file"]), n_files)

    def test_02_stop_then_resume_with_range(self) -> None:
        path = "~/models/resume"
        target = self.models / "resume"
        self.hf.file_delay = 0.05                                 # ~7 s for the 600 kB file
        text = self.tool("model_pull", {"repo": "org/tiny", "path": path})
        jid = job_id_of(text)
        part = target / "model.safetensors.hive-part"
        wait_for(lambda: part.exists() and part.stat().st_size > 60_000, 60, what="part file growing")
        # meanwhile a second pull into the same folder is refused with the running job's id
        e = self.pull_err(path=path)
        self.assertEqual(e.code, "conflict")
        self.assertEqual(e.details.get("job_id"), jid)
        st = self.node.call("job.status", {"job_id": jid})
        self.assertEqual(st["kind"], "pull")
        prog = st["progress"]
        for k in ("done", "total", "percent", "files_done", "files_total", "current"):
            self.assertIn(k, prog)
        self.assertEqual(prog["files_total"], 4)
        self.tool("job_stop", {"job_id": jid})
        st = wait_job(self.node, jid)
        self.assertNotEqual(st.get("exit_code"), 0, st)
        have = part.stat().st_size
        self.assertGreater(have, 0)
        self.assertLess(have, len(BIG))
        small = [q for q in self.hf.requests if q.get("file") == "config.json"]

        self.hf.file_delay = 0.0
        jid2 = job_id_of(self.tool("model_pull", {"repo": "org/tiny", "path": path}))
        st = wait_job(self.node, jid2)
        self.assertEqual(st["exit_code"], 0, st)
        last = self.hf.file_requests("model.safetensors")[-1]
        self.assertEqual(last["headers"].get("Range"), f"bytes={have}-")
        self.assertEqual(len([q for q in self.hf.requests if q.get("file") == "config.json"]), len(small),
                         "finished files are not fetched again")
        self.assert_tree(target, {"model.safetensors": BIG, "config.json": FILES["config.json"]})
        self.assertFalse(part.exists())

    def test_03_per_file_verify(self) -> None:
        # one corrupted body (same length, other bytes): retried once, then fine
        self.hf.corrupt["tokenizer.json"] = 1
        st = wait_job(self.node, self.pull(path="~/models/verify", include=["*.json"])["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertEqual(len(self.hf.file_requests("tokenizer.json")) >= 2, True)
        target = self.models / "verify"
        self.assert_tree(target, {"tokenizer.json": FILES["tokenizer.json"], "config.json": FILES["config.json"]})
        # always corrupted: the job fails with exit 3 and leaves no part or file behind
        self.hf.corrupt["sub/notes.txt"] = 10
        st = wait_job(self.node, self.pull(path="~/models/verify", include=["*.json", "sub/*"])["job_id"])
        self.assertEqual((st["state"], st["exit_code"]), ("exited", 3), st)
        self.assertIn("notes.txt", st["error"] or "")
        self.assertFalse((target / "sub" / "notes.txt").exists())
        self.assertFalse(list(target.rglob("*.hive-part")))
        self.hf.corrupt.clear()
        st = wait_job(self.node, self.pull(path="~/models/verify", include=["*.json", "sub/*"])["job_id"])
        self.assertEqual((st["exit_code"], st["result"]["skipped_existing"]), (0, 2), st)
        # a file changed on disk: verify=fast trusts the manifest, verify=full re-hashes and re-fetches it
        p = target / "config.json"
        data = bytearray(p.read_bytes())
        data[0] ^= 1
        p.write_bytes(bytes(data))
        st = wait_job(self.node, self.pull(path="~/models/verify", include=["*.json", "sub/*"])["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertNotEqual(p.read_bytes(), FILES["config.json"])
        st = wait_job(self.node, self.pull(path="~/models/verify", include=["*.json", "sub/*"],
                                           verify="full")["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assertEqual(p.read_bytes(), FILES["config.json"])

    def test_04_disk_space_precheck(self) -> None:
        dry = self.pull(repo="org/huge", dry_run=True)
        self.assertFalse(dry["fits"])
        self.assertEqual(dry["total_bytes"], HUGE + 2)
        e = self.pull_err(repo="org/huge")
        self.assertEqual(e.code, "too_large")
        self.assertGreater(e.details["need"], HUGE)
        self.assertLess(e.details["free"], HUGE)
        self.assertIn("path", e.details)
        self.assertIn("exclude files", e.hint or "")
        text = self.tool("model_pull", {"repo": "org/huge"}, error=True)
        self.assertIn("too_large", text)
        self.assertFalse((self.models / "org--huge").exists())
        self.assertEqual(self.hf.file_requests("huge.safetensors"), [])
        # excluding the weights makes it fit
        dry = self.pull(repo="org/huge", dry_run=True, exclude=["*.safetensors"])
        self.assertTrue(dry["fits"])

    def test_05_mirror_token_binding(self) -> None:
        e = self.pull_err(repo="org/gated", dry_run=True)
        self.assertEqual(e.code, "forbidden")
        self.assertIn("hive node secret set HF_TOKEN --hosts +127.0.0.1", e.hint or "")
        e = self.pull_err(repo="org/gated", dry_run=True, auth_env="HF_TOKEN")
        self.assertEqual((e.code, e.details.get("rule")), ("policy_denied", "secret_host"))
        self.assertTrue(all("Authorization" not in q["headers"] for q in self.hf.requests))
        # the owner binds the token to loopback (the mirror stub): now it is sent there and the gated pull works
        self.node.secret_set("HF_TOKEN", "hf_m3goodtoken", "--hosts", "+loopback")
        try:
            def gated_ok() -> Any:
                try:
                    return self.pull(repo="org/gated", dry_run=True)
                except HiveError as ex:
                    if ex.code != "forbidden":
                        raise
                    return None
            dry = wait_for(gated_ok, 20, what="the new secret binding")
            self.assertNotIn("note", dry)
            st = wait_job(self.node, self.pull(repo="org/gated")["job_id"])
            self.assertEqual(st["exit_code"], 0, st)
            self.assert_tree(self.models / "org--gated", GATED)
            auth = {q["headers"].get("Authorization") for q in self.hf.requests if q.get("file") == "w.safetensors"}
            self.assertEqual(auth, {"Bearer hf_m3goodtoken"})
        finally:
            self.node.secret_set("HF_TOKEN", "hf_m3goodtoken")    # default hosts again

    def test_06_init_mirror_cn(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="hivem3cn-")).resolve()
        try:
            env = m2util.base_env(HIVE_HOME=str(home / "h"), HOME=str(home), PYTHONPATH=self.node.env["PYTHONPATH"])
            if IS_WINDOWS:  # pragma: no cover - Windows only
                env["USERPROFILE"] = str(home)
            r = m2util.run_cli(["init", "node", "--yes", "--name", "cn", "--listen", "127.0.0.1:0", "--proxy", "off",
                                "--mirror", "cn"], env=env)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            raw = json.loads((home / "h" / "node" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["models"]["hf_endpoint"], "https://hf-mirror.com")
        finally:
            shutil.rmtree(str(home), ignore_errors=True)

    def test_07_modelscope(self) -> None:
        text = self.tool("model_pull", {"repo": "Qwen/tiny-ms", "source": "modelscope", "dry_run": True})
        self.assertIn("Qwen/tiny-ms", text)
        r = self.node.call("model.pull", {"repo": "Qwen/tiny-ms", "source": "modelscope"})
        st = wait_job(self.node, r["job_id"])
        self.assertEqual(st["exit_code"], 0, st)
        self.assert_tree(self.models / "Qwen--tiny-ms", MS_FILES)
        commit = self.ms.repos["Qwen/tiny-ms"].commit
        revs = {q.get("revision") for q in self.ms.requests if q["kind"] == "file"}
        self.assertEqual(revs, {commit})
        e = self.pull_err(repo="org/tiny", source="modelscope", repo_type="dataset", dry_run=True)
        self.assertEqual(e.code, "unsupported")


if __name__ == "__main__":
    unittest.main()
