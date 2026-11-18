"""Regression tests for the cross-package bugs found while bringing up milestone M1 (docs/PHASE0_NOTES.md, "M1
integration")."""
from __future__ import annotations

import time
import unittest

from hive import jobs, render
from tests.wp2util import PY, FakeRt, handler_call


class TestShellYieldAtTimeout(unittest.TestCase):
    """The Toolkit sends ``yield_after = min(timeout, w)`` (§17.4): ``yield_after == timeout`` for every short
    timeout. A command the shim kills at ``timeout`` must come back as ``state:"timeout"``, not as a yielded job."""

    def setUp(self) -> None:
        self.rt = FakeRt()
        self.rt.jobs = jobs.JobManager(self.rt)
        self.rt.jobs.start()
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        for jid, r in list(self.rt.jobs._jobs.items()):
            if r.get("state") in ("starting", "running"):
                self.rt.jobs.stop(jid, force=True)
        self.rt.jobs.shutdown()
        self.rt.cleanup()

    def test_yield_equal_to_timeout_reports_timeout(self) -> None:
        t0 = time.monotonic()
        r = handler_call("shell.run", {"argv": [PY, "-c", "import time; time.sleep(30)"], "timeout": 1,
                                       "yield_after": 1}, runtime=self.rt)
        self.assertEqual(r["state"], "timeout", r)
        self.assertIsNone(r["job_id"])
        self.assertLess(time.monotonic() - t0, 1 + 3 + 1 + 2)    # timeout + grace + 1 s bound (+ slack)

    def test_yield_below_timeout_still_yields(self) -> None:
        r = handler_call("shell.run", {"argv": [PY, "-c", "import time; time.sleep(3)"], "timeout": 10,
                                       "yield_after": 1}, runtime=self.rt)
        self.assertEqual(r["state"], "running")
        self.assertTrue(r["job_id"])

    def test_background_on_timeout_unchanged(self) -> None:
        r = handler_call("shell.run", {"argv": [PY, "-c", "import time; time.sleep(3)"], "timeout": 1,
                                       "yield_after": 1, "on_timeout": "background"}, runtime=self.rt)
        self.assertEqual(r["state"], "running")


class TestRenderBooleans(unittest.TestCase):
    def test_definition_booleans_use_json_spelling(self) -> None:
        lines = render._definition_lines({"argv": ["srv"], "autostart": False, "port": 8080})
        self.assertIn("autostart: false", lines)
        self.assertNotIn("autostart: False", lines)
        self.assertEqual(render.clean(True), "true")


if __name__ == "__main__":
    unittest.main()
