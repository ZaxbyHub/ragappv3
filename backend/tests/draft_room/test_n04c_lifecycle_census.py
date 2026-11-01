"""Lifecycle census guardrail for issue #788 (defect class C8 rung).

The declared draft-status vocabulary drifted from the writers once already
(``running`` sat in ``DRAFT_STATUSES`` and the transition table with no
producer, so a compiling draft reported ``queued`` for its whole run). This
census makes that class of drift fail CI: every status in the vocabulary
must register a producer, and each registered producer's marker must still
exist in the draft-lifecycle source. Adding a status to ``DRAFT_STATUSES``
without a writer fails the census; deleting a writer while its status stays
declared fails the census.
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.services.draft_store import DRAFT_STATUSES  # noqa: E402

_BACKEND = Path(__file__).resolve().parents[2] / "app"

_LIFECYCLE_FILES = (
    "services/draft_store.py",
    "api/routes/draft_room.py",
    "services/draft_pipeline.py",
    "services/draft_job_processor.py",
)

#: producer registry: status -> (how it is produced, a marker string that
#: must appear in the lifecycle sources while that producer exists).
#: ``draft`` is the schema default at INSERT (create_draft's INSERT lists no
#: status column), pinned by the INSERT marker itself.
_STATUS_PRODUCERS = {
    "draft": (
        "schema default at drafts INSERT (create_draft)",
        "INSERT INTO drafts (vault_id",
    ),
    "queued": (
        "compile enqueue / retry re-queue / recovery reset",
        "UPDATE drafts SET status = 'queued'",
    ),
    "running": (
        "claim_next_compile_job moves the claimed compile's draft",
        "UPDATE drafts SET status = 'running'",
    ),
    "needs_review": (
        "manual revision / apply / pipeline publish / restore",
        "UPDATE drafts SET status = 'needs_review'",
    ),
    "ready": (
        "_sync_mark_ready (the only writer of 'ready')",
        "UPDATE drafts SET status = 'ready'",
    ),
    "failed": (
        "compile fail settle (_move_draft / processor)",
        "UPDATE drafts SET status = 'failed'",
    ),
    "cancelled": (
        "compile cancel settle (_move_draft / request_job_cancel)",
        "UPDATE drafts SET status = 'cancelled'",
    ),
    "archived": (
        "archive_draft",
        "UPDATE drafts SET status = 'archived'",
    ),
}


def _combined_source() -> str:
    """Concatenate the lifecycle sources, failing loudly if one is missing.

    A missing file must not silently shrink the scanned set: that would let a
    moved writer hide behind markers that still occur in the other files.
    """
    missing = [
        rel for rel in _LIFECYCLE_FILES if not (_BACKEND / rel).is_file()
    ]
    assert not missing, (
        f"census lifecycle sources not found under {_BACKEND}: {missing}; "
        f"update _LIFECYCLE_FILES when a lifecycle module moves"
    )
    return "\n".join((_BACKEND / rel).read_text(encoding="utf-8") for rel in _LIFECYCLE_FILES)


class TestN04CLifecycleCensus(unittest.TestCase):
    def test_every_declared_draft_status_has_a_live_producer(self):
        source = _combined_source()
        missing_registry = sorted(DRAFT_STATUSES - set(_STATUS_PRODUCERS))
        self.assertEqual(
            missing_registry,
            [],
            f"DRAFT_STATUSES gained value(s) with no registered producer: "
            f"{missing_registry}; register the writer in "
            f"_STATUS_PRODUCERS or remove the value from the vocabulary "
            f"(issue #788 class C8: declared-vs-actual lifecycle drift)",
        )
        dead_markers = {
            status: marker
            for status, (_how, marker) in _STATUS_PRODUCERS.items()
            if status in DRAFT_STATUSES and marker not in source
        }
        self.assertEqual(
            dead_markers,
            {},
            f"declared draft status(es) lost their producer: {dead_markers}; "
            f"a status in DRAFT_STATUSES with no writer is dead vocabulary "
            f"(issue #788 class C8)",
        )

    def test_census_registry_has_no_dead_entries(self):
        stale = sorted(set(_STATUS_PRODUCERS) - DRAFT_STATUSES)
        self.assertEqual(
            stale,
            [],
            f"_STATUS_PRODUCERS registers producer(s) for status(es) no "
            f"longer in DRAFT_STATUSES: {stale}; update the census",
        )


if __name__ == "__main__":
    unittest.main()
