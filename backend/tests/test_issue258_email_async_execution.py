"""Issue #258 (E2) acceptance checks — AC5 / TEST-005: async bodies execute.

Phase 2.5 CHECKS ONLY (tier L). TEST-005's defect:
``tests/test_email_service.py::TestSaveAttachmentSentinel`` (line ~1046)
declares four ``async def test_*`` methods on a SYNC ``unittest.TestCase``
base. Neither pytest (which does not apply pytest-asyncio to unittest
classes) nor plain unittest (which calls the method, gets an un-awaited
coroutine, and reports a vacuous pass) ever runs those bodies — the sentinel
double-close coverage they claim does not exist. Sibling classes in the same
file correctly use ``IsolatedAsyncioTestCase``.

This node is DISCRIMINATING and RED at base for the stated reason (wrong
base class):

1. Subclass check — ``TestSaveAttachmentSentinel`` must be a subclass of
   ``unittest.IsolatedAsyncioTestCase`` (the conversion target of Phase 4).
2. Canary — the four test methods must be coroutine functions (green at base,
   must stay green: the conversion keeps async methods).
3. Execution probe — one method is temporarily wrapped with an instrumented
   async wrapper whose body only records when it is AWAITED, the whole class
   is run through the real unittest runner, and the probe must have actually
   executed. At base the sync base class never awaits it, so this fails for
   exactly the root cause under repair. After the Phase-4 conversion the
   bodies run for real, so any latent fixture-arg breakage surfaces here too
   (AC5's "latent fixture args fixed" clause).

NOTE: the class under repair is imported INSIDE the test function on purpose
— a module-level binding makes pytest re-collect the foreign class as part of
this module's suite.
"""

import inspect
import io
import unittest

_METHOD_NAMES = [
    "test_save_attachment_success_closes_fd_once",
    "test_save_attachment_error_closes_fd_once",
    "test_save_attachment_error_unlinks_temp_file",
    "test_save_attachment_sentinel_prevents_double_close",
]
_PROBE_TARGET = _METHOD_NAMES[0]


class TestEmailAsyncBodiesExecute(unittest.TestCase):
    """AC5: the sentinel tests' async bodies must actually run."""

    def test_sentinel_class_runs_async_bodies(self) -> None:
        from test_email_service import TestSaveAttachmentSentinel

        # Canary: the conversion target is a class whose test methods are
        # coroutine functions (true at base; must remain true after Phase 4).
        for name in _METHOD_NAMES:
            method = getattr(TestSaveAttachmentSentinel, name, None)
            self.assertIsNotNone(method, f"method {name} disappeared")
            self.assertTrue(
                inspect.iscoroutinefunction(method),
                f"{name} is no longer a coroutine function",
            )

        # AC5 CHECK — TestSaveAttachmentSentinel is still on a sync
        # unittest.TestCase base: async bodies are vacuous passes.
        print("AC5 CHECK: FAIL — TestSaveAttachmentSentinel still on a sync unittest.TestCase base")
        self.assertTrue(
            issubclass(TestSaveAttachmentSentinel, unittest.IsolatedAsyncioTestCase),
            "TestSaveAttachmentSentinel must derive from "
            "unittest.IsolatedAsyncioTestCase so its async bodies execute",
        )

        # Execution probe: run the class through the real unittest runner
        # with one method instrumented to record AWAITED execution.
        executed: list = []
        original = getattr(TestSaveAttachmentSentinel, _PROBE_TARGET)

        async def _probe(self):
            executed.append(True)
            await original(self)

        _probe.__name__ = original.__name__
        setattr(TestSaveAttachmentSentinel, _PROBE_TARGET, _probe)
        try:
            loader = unittest.TestLoader()
            suite = loader.loadTestsFromTestCase(TestSaveAttachmentSentinel)
            runner = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0)
            result = runner.run(suite)
        finally:
            setattr(TestSaveAttachmentSentinel, _PROBE_TARGET, original)

        # AC5 CHECK — the class's test bodies did not execute under the
        # unittest runner (probe never awaited).
        print("AC5 CHECK: FAIL — sentinel test bodies did not execute under the unittest runner")
        self.assertEqual(result.testsRun, len(_METHOD_NAMES))
        self.assertEqual(
            len(result.failures) + len(result.errors),
            0,
            f"sentinel bodies raised: "
            f"{[str(f[1]) for f in result.failures + result.errors]}",
        )
        self.assertEqual(executed, [True])


if __name__ == "__main__":
    unittest.main()
