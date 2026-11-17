"""WP1 secrets tests (§21.2 secrets row): store file + perms, host binding, passthrough, env imports, exec flag,
reload on change, masking (incl. range edges), resolve_auth."""
from __future__ import annotations

import os
import shutil
import stat
import tempfile
import time
import unittest
from pathlib import Path

from hive import util
from hive.errors import HiveError
from hive.secretstore import DEFAULT_SECRET_HOSTS, SecretStore, parse_hosts

HF = "hf_abcdefghijklmnopqrstuvwxyz012345"


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.d = Path(tempfile.mkdtemp(prefix="hivesec-"))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.path = self.d / "secrets.json"

    def store(self, env=None, passthrough=()):
        return SecretStore(self.path, passthrough=passthrough, env={} if env is None else env)

    def err(self, fn):
        with self.assertRaises(HiveError) as cm:
            fn()
        return cm.exception


class TestStore(Base):
    def test_set_list_unset_and_perms(self):
        st = self.store()
        st.set("HF_TOKEN", HF)
        st.set("MY_KEY", "value-1234567", hosts=["api.example.com"], exec_ok=False)
        names = {n["name"]: n for n in st.names()}
        self.assertEqual(names["HF_TOKEN"]["hosts"], list(DEFAULT_SECRET_HOSTS["HF_TOKEN"]))
        self.assertEqual((names["MY_KEY"]["exec"], names["MY_KEY"]["source"]), (False, "store"))
        self.assertNotIn(HF, repr(st.names()))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        raw = util.read_json(self.path)
        self.assertEqual(raw["version"], 1)
        self.assertTrue(st.unset("MY_KEY"))
        self.assertFalse(st.unset("MY_KEY"))
        self.assertEqual([n["name"] for n in st.names()], ["HF_TOKEN"])

    def test_validation(self):
        st = self.store()
        for name in ("hf_token", "HIVE_X", "1ABC", "A-B"):
            self.assertEqual(self.err(lambda: st.set(name, "x" * 10)).code, "bad_request", name)
        for value in ("", "a\nb", "a\x00b", "x" * 16385):
            self.assertEqual(self.err(lambda: st.set("OK_NAME", value)).code, "bad_request")

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_group_writable_not_loaded_and_broad_mode_fixed(self):
        self.store().set("A_SECRET", "abcdefghijk")
        os.chmod(self.path, 0o664)
        self.assertEqual(self.store().names(), [])
        os.chmod(self.path, 0o644)
        st = self.store()
        self.assertEqual([n["name"] for n in st.names()], ["A_SECRET"])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_reload_on_change(self):
        st = self.store()
        other = self.store()
        other.set("NEW_ONE", "abcdefghijk")
        time.sleep(0.01)
        self.assertEqual(st.resolve("NEW_ONE", purpose="exec"), "abcdefghijk")


class TestResolve(Base):
    def test_host_binding(self):
        st = self.store()
        st.set("HF_TOKEN", HF)
        st.set("LOCAL_KEY", "abcdefghijkl")
        st.set("LAN_KEY", "abcdefghijklm", hosts=["private"])
        self.assertEqual(st.resolve("HF_TOKEN", purpose="auth", host="huggingface.co"), HF)
        self.assertEqual(st.resolve("HF_TOKEN", purpose="auth", host="cdn-lfs.hf.co"), HF)
        e = self.err(lambda: st.resolve("HF_TOKEN", purpose="auth", host="evil.example"))
        self.assertEqual((e.code, e.details["rule"]), ("policy_denied", "secret_host"))
        self.assertIn("hive node secret set HF_TOKEN --hosts +", e.hint)
        self.assertEqual(self.err(lambda: st.resolve("HF_TOKEN", purpose="auth", host="huggingface.co",
                                                     scheme="http")).details["rule"], "secret_host")
        self.assertEqual(st.resolve("LOCAL_KEY", purpose="auth", host="127.0.0.1", scheme="http"), "abcdefghijkl")
        self.assertEqual(self.err(lambda: st.resolve("LOCAL_KEY", purpose="auth", host="10.0.0.2")).code,
                         "policy_denied")
        self.assertEqual(st.resolve("LAN_KEY", purpose="auth", host="192.168.1.5", scheme="http"), "abcdefghijklm")
        a = st.resolve_auth("HF_TOKEN")
        self.assertEqual((a.name, a.header, a.value, a.hosts), ("HF_TOKEN", "Authorization", "Bearer " + HF,
                                                               DEFAULT_SECRET_HOSTS["HF_TOKEN"]))

    def test_missing_and_passthrough(self):
        env = {"HF_TOKEN": HF, "AWS_SECRET_ACCESS_KEY": "aws-secret-value"}
        st = self.store(env=env, passthrough=["HF_TOKEN"])
        self.assertEqual(st.resolve("HF_TOKEN", purpose="exec"), HF)
        self.assertEqual(st.names()[0]["source"], "passthrough")
        st.node_name = "pod-a"
        e = self.err(lambda: st.resolve("AWS_SECRET_ACCESS_KEY", purpose="auth"))
        self.assertEqual((e.code, e.details["secret"]), ("not_found", "AWS_SECRET_ACCESS_KEY"))
        self.assertEqual(e.message, "secret 'AWS_SECRET_ACCESS_KEY' is not set on node 'pod-a'")
        self.assertEqual(e.hint, "ask the owner to run on that machine: hive node secret set AWS_SECRET_ACCESS_KEY")

    def test_env_import_removed(self):
        env = {"HIVE_SECRET_HF_TOKEN": HF, "HIVE_SECRET_HOSTS_HF_TOKEN": "+hf-mirror.com",
               "HIVE_SECRET_bad": "x" * 9, "HIVE_SECRET_HOSTS_HF_HOME": "+x", "OTHER": "1",
               "HIVE_SECRET_HOSTS_PASS_T": "private", "PASS_T": "abcdefghij"}
        st = self.store(env=env, passthrough=["PASS_T"])
        self.assertEqual(set(env), {"OTHER", "PASS_T"})
        n = {x["name"]: x for x in st.names()}
        self.assertEqual(n["HF_TOKEN"]["source"], "env-import")
        self.assertIn("hf-mirror.com", n["HF_TOKEN"]["hosts"])
        self.assertIn("huggingface.co", n["HF_TOKEN"]["hosts"])
        self.assertEqual(n["PASS_T"]["hosts"], ["private"])
        e = self.err(lambda: st.resolve("HF_TOKEN", purpose="auth", host="evil.example"))
        self.assertIn("HIVE_SECRET_HOSTS_HF_TOKEN", e.hint)

    def test_exec_flag(self):
        st = self.store()
        st.set("NOEXEC", "abcdefghijk", exec_ok=False)
        self.assertEqual(self.err(lambda: st.resolve("NOEXEC", purpose="exec")).details["rule"], "secret_env")
        self.assertEqual(st.resolve("NOEXEC", purpose="auth"), "abcdefghijk")

    def test_parse_hosts(self):
        self.assertEqual(parse_hosts("X", None), ("loopback",))
        self.assertEqual(parse_hosts("X", ["a.com,b.com"]), ("a.com", "b.com"))
        self.assertEqual(parse_hosts("HF_TOKEN", ["+hf-mirror.com"])[-1], "hf-mirror.com")
        self.assertRaises(HiveError, parse_hosts, "X", ["a b"])


class TestMask(Base):
    def test_mask(self):
        st = self.store()
        self.assertFalse(st.has_values)
        st.set("HF_TOKEN", HF)
        st.set("SHORT", "abc")
        self.assertTrue(st.has_values)
        self.assertEqual(st.mask(f"x {HF} y abc"), "x «secret:HF_TOKEN» y abc")
        self.assertEqual(st.max_value_bytes, len(HF))

    def test_mask_edges(self):
        st = self.store()
        st.set("HF_TOKEN", HF)
        blob = b"AAAA" + HF.encode() + b"BBBB"
        cut = 4 + 10
        first, second = blob[:cut], blob[cut:]
        pad = st.max_value_bytes - 1
        d1, m1 = st.mask_edges(first, b"", second[:pad])
        d2, m2 = st.mask_edges(second, first[-pad:], b"")
        self.assertTrue(m1 and m2)
        for half in (HF[:10].encode(), HF[10:].encode()):
            self.assertNotIn(half, d1 + d2)
        self.assertEqual(d1, b"AAAA" + "«secret:HF_TOKEN»".encode("utf-8"))
        self.assertEqual(d2, "«secret:HF_TOKEN»".encode("utf-8") + b"BBBB")
        self.assertEqual(st.mask_edges(b"plain", b"", b""), (b"plain", False))


if __name__ == "__main__":
    unittest.main()
