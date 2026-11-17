"""errors.py (P0, §4.1): codes, retryable truth table, wire round trip, helpers."""
from __future__ import annotations

import unittest

from hive import errors
from hive.errors import CODES, HiveError


class TestCodes(unittest.TestCase):
    def test_codes(self):
        self.assertEqual(CODES["approval_required"], 428)
        self.assertEqual(CODES["cancelled"], 499)
        self.assertEqual(CODES["unsupported"], 501)
        self.assertEqual(CODES["policy_denied"], 403)
        self.assertEqual(len(CODES), 14)

    def test_unknown_code_becomes_internal(self):
        e = HiveError("weird", "x")
        self.assertEqual(e.code, "internal")
        self.assertEqual(e.details["orig_code"], "weird")
        self.assertEqual(str(e), "[internal] x")

    def test_http_status_with_status(self):
        self.assertEqual(HiveError("bad_request", "x").http_status, 400)
        self.assertEqual(HiveError("bad_request", "chunked", status=411).http_status, 411)
        self.assertEqual(HiveError("bad_request", "ct", status=415).http_status, 415)
        self.assertEqual(HiveError("approval_required", "x").http_status, 428)


class TestRetryableTruthTable(unittest.TestCase):
    """unavailable/timeout × maybe_executed true/false/missing × reason; rate_limited always true (P0 truth table)."""

    def test_table(self):
        reasons = [None, "lost", "connect", "busy", "tls_pin", "proxy_auth", "identity_changed"]
        maybe = {"true": True, "false": False, "missing": None}
        for code in ("unavailable", "timeout"):
            for mk, mv in maybe.items():
                for reason in reasons:
                    details = {}
                    if mv is not None:
                        details["maybe_executed"] = mv
                    if reason is not None:
                        details["reason"] = reason
                    e = HiveError(code, "m", details)
                    expected = (mv is False and reason not in ("tls_pin", "proxy_auth", "identity_changed"))
                    with self.subTest(code=code, maybe_executed=mk, reason=reason):
                        self.assertIs(e.retryable, expected)

    def test_rate_limited_always(self):
        self.assertTrue(errors.rate_limited("slow down", 3).retryable)
        self.assertTrue(HiveError("rate_limited", "x").retryable)

    def test_other_codes_never(self):
        for code in CODES:
            if code in ("rate_limited", "unavailable", "timeout"):
                continue
            with self.subTest(code=code):
                self.assertFalse(HiveError(code, "m", {"maybe_executed": False}).retryable)

    def test_identity_changed_not_retryable(self):
        e = errors.unavailable("awaiting ack", reason="identity_changed", maybe_executed=False)
        self.assertFalse(e.retryable)
        self.assertTrue(errors.unavailable("offline", reason="offline", maybe_executed=False).retryable)


class TestWire(unittest.TestCase):
    def test_to_dict_minimal(self):
        self.assertEqual(HiveError("not_found", "nope").to_dict(), {"code": "not_found", "message": "nope"})

    def test_to_dict_full_and_round_trip(self):
        e = errors.denied("outside roots", hint="ask the user", rule="fs_roots", roots=["/a"])
        d = e.to_dict()
        self.assertEqual(d, {"code": "policy_denied", "message": "outside roots", "hint": "ask the user",
                             "details": {"rule": "fs_roots", "roots": ["/a"]}})
        back = HiveError.from_dict(d)
        self.assertEqual((back.code, back.message, back.hint, back.details), ("policy_denied", "outside roots",
                                                                              "ask the user", d["details"]))

    def test_from_dict_unknown_code(self):
        e = HiveError.from_dict({"code": "teapot", "message": "short and stout", "details": {"a": 1}})
        self.assertEqual(e.code, "internal")
        self.assertEqual(e.details, {"a": 1, "orig_code": "teapot"})

    def test_from_dict_garbage(self):
        e = HiveError.from_dict("boom")
        self.assertEqual((e.code, e.message), ("internal", "boom"))
        e = HiveError.from_dict({})
        self.assertEqual(e.code, "internal")


class TestHelpers(unittest.TestCase):
    def test_simple_helpers(self):
        table = [(errors.bad_request, "bad_request"), (errors.not_found, "not_found"), (errors.denied, "policy_denied"),
                 (errors.forbidden, "forbidden"), (errors.unauthorized, "unauthorized"),
                 (errors.conflict, "conflict"), (errors.too_large, "too_large"),
                 (errors.unsupported, "unsupported"), (errors.internal, "internal")]
        for fn, code in table:
            with self.subTest(code=code):
                e = fn("msg", hint="h", k=1)
                self.assertEqual((e.code, e.message, e.hint, e.details), (code, "msg", "h", {"k": 1}))

    def test_unavailable_sets_maybe_executed_explicitly(self):
        e = errors.unavailable("down")
        self.assertIs(e.details["maybe_executed"], False)
        self.assertNotIn("reason", e.details)
        e = errors.unavailable("lost", reason="lost", maybe_executed=True, hint="check status", job="j")
        self.assertEqual(e.details, {"reason": "lost", "maybe_executed": True, "job": "j"})

    def test_timeout(self):
        e = errors.timeout("slow", maybe_executed=True)
        self.assertEqual((e.code, e.details), ("timeout", {"maybe_executed": True}))
        with self.assertRaises(TypeError):
            errors.timeout("slow")  # maybe_executed is required

    def test_cancelled_default(self):
        self.assertEqual(errors.cancelled().message, "cancelled")
        self.assertEqual(errors.cancelled().http_status, 499)

    def test_rate_limited(self):
        e = errors.rate_limited("too many", 2.5, reason="jobs_running")
        self.assertEqual(e.details, {"retry_after": 2.5, "reason": "jobs_running"})

    def test_approval_required(self):
        e = errors.approval_required(approval_id="ap_abcdef", expires_at="2026-11-17T09:12:03.120Z",
                                     where="node:desk", how="hive approvals approve ap_abcdef")
        self.assertEqual(e.code, "approval_required")
        self.assertEqual(set(e.details), {"approval_id", "expires_at", "where", "how"})
        self.assertEqual(e.http_status, 428)


if __name__ == "__main__":
    unittest.main()
