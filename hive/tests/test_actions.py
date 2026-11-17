"""actions/__init__.py (P0, §4.5): @handler, load_all, ActionContext (field order, remaining/check/sleep)."""
from __future__ import annotations

import dataclasses
import threading
import time
import unittest
from unittest import mock

from hive import actions
from hive.actions import ACTION_MODULES, HANDLERS, ActionContext, handler, load_all
from hive.errors import HiveError
from tests import helpers


class TestRegistry(unittest.TestCase):
    def test_modules(self):
        self.assertEqual(ACTION_MODULES, ["node", "system", "shell", "jobs", "services", "files", "net", "model"])

    def test_handler_asserts(self):
        with self.assertRaises(AssertionError):
            handler("no.such")
        with self.assertRaises(AssertionError):
            handler("hub.nodes")  # hub scope
        before = dict(HANDLERS)
        with mock.patch.dict(HANDLERS, {}, clear=True):
            @handler("node.info")
            def h(ctx, params):
                return {}
            self.assertIs(HANDLERS["node.info"], h)
            with self.assertRaises(AssertionError):
                handler("node.info")
        self.assertEqual(HANDLERS, before)

    def test_load_all_skips_broken_module(self):
        real = actions.importlib.import_module

        def fake(name, *a):
            if name.endswith(".shell"):
                raise RuntimeError("broken module")
            return real(name, *a)
        with mock.patch.object(actions.importlib, "import_module", side_effect=fake), \
                self.assertLogs("hive.actions", level="ERROR") as logs:
            out = load_all()
        self.assertIs(out, HANDLERS)
        self.assertTrue(any("shell" in m for m in logs.output))


class TestActionContext(unittest.TestCase):
    def test_field_order(self):
        names = [f.name for f in dataclasses.fields(ActionContext)]
        self.assertEqual(names, ["rt", "identity", "policy", "spec", "request_id", "deadline", "cancel", "via",
                                 "hold", "given", "progress", "audit_extra"])
        required = [f.name for f in dataclasses.fields(ActionContext)
                    if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING]
        self.assertEqual(required[-1], "via")

    def test_defaults(self):
        ctx = helpers.make_ctx("fs.read")
        self.assertIsNone(ctx.hold)
        self.assertEqual(ctx.given, frozenset())
        self.assertIsNone(ctx.progress(1, None, "x"))
        self.assertEqual(ctx.audit_extra, {})
        self.assertIsNot(ctx.audit_extra, helpers.make_ctx("fs.read").audit_extra)

    def test_remaining_and_check(self):
        ctx = helpers.make_ctx("fs.read", timeout=5)
        self.assertGreater(ctx.remaining(), 4)
        ctx.check()
        ctx.cancel.set()
        with self.assertRaises(HiveError) as cm:
            ctx.check()
        self.assertEqual(cm.exception.code, "cancelled")

    def test_deadline_timeout_maybe_executed(self):
        ctx = helpers.make_ctx("fs.read")
        ctx.deadline = time.monotonic() - 1
        self.assertEqual(ctx.remaining(), 0)
        with self.assertRaises(HiveError) as cm:
            ctx.check()
        self.assertEqual((cm.exception.code, cm.exception.details["maybe_executed"]), ("timeout", False))
        ctx = helpers.make_ctx("fs.write")
        ctx.deadline = time.monotonic() - 1
        with self.assertRaises(HiveError) as cm:
            ctx.check()
        self.assertIs(cm.exception.details["maybe_executed"], True)

    def test_sleep_cancel_aware(self):
        ctx = helpers.make_ctx("job.status", timeout=30)
        threading.Timer(0.1, ctx.cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(HiveError) as cm:
            ctx.sleep(10)
        self.assertEqual(cm.exception.code, "cancelled")
        self.assertLess(time.monotonic() - t0, 5)

    def test_sleep_bounded_by_deadline(self):
        ctx = helpers.make_ctx("job.status", timeout=0.2)
        t0 = time.monotonic()
        with self.assertRaises(HiveError) as cm:
            ctx.sleep(10)
        self.assertEqual(cm.exception.code, "timeout")
        self.assertLess(time.monotonic() - t0, 5)
        ctx = helpers.make_ctx("job.status", timeout=5)
        ctx.sleep(0.01)


if __name__ == "__main__":
    unittest.main()
