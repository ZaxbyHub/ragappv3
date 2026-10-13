"""Frozen AC2 check for issue #787 (TQ-sweep-B06-04): a corrupted, inherited
checkpoint must re-run its stage, not terminally fail the retry.

``_CompileRun._reusable`` re-hashes ``artifact_json`` before trusting an
inherited checkpoint, so corruption is only ever CONSULTED when the stored
JSON and its stored SHA-256 are rewritten together. The probe this check
freezes: a hash-consistent ``{"sections": 7}`` on the parent's outline row is
inherited by a retry from ``copy``, and the unguarded
``OutlineArtifact.model_validate_json`` (draft_pipeline.py) then raises
pydantic's ``ValidationError``. ``run_compile``'s generic handler settles the
job as a non-retryable ``internal_error`` raised ``from None``, so the raw
class name survives ONLY in the ``draft compile: job id=%d raised %s`` ERROR
line, never in the exception chain.

Base behaviour: ``'ValidationError' == outcome`` (terminal internal_error,
the stage never re-runs). Required behaviour: the poisoned row is discarded
and Outline re-runs (``model.count("outline") >= 1``), completing the retry.
"""

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import sha256_text  # noqa: E402
from draft_room import test_draft_pipeline as tdp  # noqa: E402


class _CollectingHandler(logging.Handler):
    """Append every record that reaches the root logger to a list."""

    def __init__(self, sink):
        super().__init__(level=logging.INFO)
        self.sink = sink

    def emit(self, record):
        self.sink.append(record)


class TestN03CheckpointResume(tdp.TestRetryStartStageInheritance):
    """Inherits the harness's retry fixtures (seeded parent, retry, corrupt)."""

    longMessage = False

    async def test_corrupted_outline_checkpoint_reruns_the_stage(self):
        await self._seed_failed_parent()

        # Hash-consistent corruption: _reusable re-hashes artifact_json, so
        # only a row whose stored hash matches its stored (corrupt) JSON is
        # ever consulted. start_stage="copy" keeps every upstream stage input
        # hash identical to the parent's, so the poisoned row IS inherited.
        bad = '{"sections": 7}'
        self._corrupt_parent_stage(
            "outline", artifact_json=bad, artifact_sha256=sha256_text(bad)
        )
        retry_id = self._make_retry("copy")

        records = []
        handler = _CollectingHandler(records)
        root = logging.getLogger()
        previous_level = root.level
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        try:
            outcome = "rerun"
            model = None
            try:
                model = await self._run_job(retry_id)
                if model.count("outline") == 0:
                    outcome = "completed-without-outline-model"
            except Exception as exc:
                outcome = getattr(exc, "code", None) or type(exc).__name__
            # The generic handler raises `from None`, so the raw class name
            # is observable only in the ERROR log line.
            if any(
                record.levelno >= logging.ERROR
                and "ValidationError" in record.getMessage()
                for record in records
            ):
                outcome = "ValidationError"
        finally:
            root.removeHandler(handler)
            root.setLevel(previous_level)

        self.assertEqual(outcome, "rerun", f"assert {outcome!r} == 'rerun'")
