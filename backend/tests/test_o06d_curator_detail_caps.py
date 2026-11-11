"""LLM detail-field caps check for the wiki curator (collated wiki lint
lifecycle trace, issues #929 + #932 + #935).

Harness mirrors backend/tests/test_o05c_wiki_curator_feedback_pins.py:
``_make_env`` (temp DB + run_migrations + vault/files seed), the
``_FlagSnapshot`` settings guard, and the ``_run_curator_payload`` pattern
(stub CuratorClient via ``patch("app.services.wiki_compiler.CuratorClient",
_Stub)``, then ``compile_ingest_job(1, {"file_id": 1, "text": ...})``).

C10 (AC10, DISCRIMINATING): the curator stores LLM-derived lint detail
fields UNCAPPED — ``claim_text`` in the unsupported-claim rejection details,
the model-derived ``chunk_id`` in the unknown-chunk and quote-mismatch
rejection details, and ``claim_a``/``claim_b``/``reason``/``source_quote``
in the contradiction details (only ``attempted_quote`` is capped ``[:500]``
and the ``[:80]`` slices feed the TITLE, not the details). A hostile or
runaway operator-side LLM can therefore push arbitrarily large
attacker-influenced strings into ``wiki_lint_findings.details``. The check
drives one draft-mode compile whose stubbed payload carries a 5000-char
claim_text (missing source_quote), a 5000-char unknown chunk_id, a
quote-mismatch claim, and a contradiction whose four detail fields are
each 5000 chars, then asserts every stored JSON string field is <= 500
chars — the existing ``attempted_quote`` cap value, so the oracle is
implementation-independent (the fix may introduce any documented module
constant, as long as it does not exceed 500). RED at base, GREEN after the
fix.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _mod in ("lancedb", "pyarrow"):
    try:
        __import__(_mod)
    except ImportError:
        sys.modules[_mod] = types.ModuleType(_mod)

from app.config import settings  # noqa: E402
from app.models.database import run_migrations  # noqa: E402
from app.services.wiki_compiler import WikiCompiler  # noqa: E402
from app.services.wiki_store import WikiStore  # noqa: E402

# The bound the check asserts: the existing attempted_quote cap value.
_MAX_DETAIL_CHARS = 500


def _make_env():
    td = tempfile.mkdtemp()
    db_path = str(Path(td) / "test.db")
    run_migrations(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("INSERT OR IGNORE INTO vaults (id, name) VALUES (1, 'T')")
    conn.execute(
        "INSERT OR IGNORE INTO files (id, vault_id, file_path, file_name, "
        "file_size, status) VALUES (1, 1, ?, 'test.txt', 100, 'indexed')",
        (str(Path(td) / "test.txt"),),
    )
    conn.commit()
    store = WikiStore(conn)
    compiler = WikiCompiler(conn, store)
    return conn, store, compiler, td


_FLAG_FIELDS = (
    "wiki_llm_curator_enabled",
    "wiki_llm_curator_run_on_ingest",
    "wiki_llm_curator_url",
    "wiki_llm_curator_model",
    "wiki_llm_curator_mode",
    "wiki_llm_curator_require_quote_match",
    "wiki_llm_curator_require_chunk_id",
)


class _FlagSnapshot:
    """Snapshot/restore the curator settings block around a test."""

    def __enter__(self):
        self._snap = {f: getattr(settings, f) for f in _FLAG_FIELDS}
        return self

    def __exit__(self, *exc):
        for f, v in self._snap.items():
            setattr(settings, f, v)
        return False


class TestCuratorDetailCaps(unittest.TestCase):
    """C10 (AC10, DISCRIMINATING): every LLM-derived string inside stored
    curator lint finding details is bounded. RED at base, GREEN after the
    fix."""

    def setUp(self):
        self._flag = _FlagSnapshot()
        self._flag.__enter__()
        self.conn, self.store, self.compiler, self._td = _make_env()
        self.addCleanup(self._flag.__exit__, None, None, None)

    def tearDown(self):
        self.conn.close()

    def test_llm_detail_fields_capped(self):
        settings.wiki_llm_curator_enabled = True
        settings.wiki_llm_curator_run_on_ingest = True
        settings.wiki_llm_curator_url = "https://curator-invalid.example"
        settings.wiki_llm_curator_model = "qwen-1b"
        settings.wiki_llm_curator_mode = "draft"
        settings.wiki_llm_curator_require_quote_match = True
        settings.wiki_llm_curator_require_chunk_id = True

        payload = json.dumps({
            "claims": [
                {
                    # Missing source_quote → unsupported_claim rejection whose
                    # details embed claim_text verbatim (uncapped at base).
                    # Text sorts FIRST ("A" < "M" < "T") so the recorded base
                    # failure signature stays the claim_text violation.
                    "claim_text": "A" * 5000,
                    "claim_type": "fact",
                    "source_quote": "",
                    "chunk_id": "1_0",
                    "confidence": 0.9,
                },
                {
                    # Unknown oversized chunk_id → missing-chunk rejection
                    # whose details embed the model-derived chunk_id
                    # verbatim (uncapped at base; plan-critic round 1,
                    # blocker 3). Sorts second.
                    "claim_text": "M" * 5000,
                    "claim_type": "fact",
                    "source_quote": "M" * 50,
                    "chunk_id": "C" * 5000,
                    "confidence": 0.9,
                },
                {
                    # Known chunk + non-matching quote → quote-mismatch
                    # rejection whose details embed chunk_id and the capped
                    # attempted_quote. Its chunk_id is always a short
                    # internal id (an oversized id can never be KNOWN), so
                    # this leg pins the field's presence on that site, not
                    # its length. Sorts third.
                    "claim_text": "T" * 100,
                    "claim_type": "fact",
                    "source_quote": "T" * 100,
                    "chunk_id": "1_0",
                    "confidence": 0.9,
                },
            ],
            "contradictions": [{
                # All four detail fields uncapped at base.
                "claim_a": "A" * 5000,
                "claim_b": "B" * 5000,
                "reason": "R" * 5000,
                "source_quote": "Q" * 5000,
            }],
        })

        class _Stub:
            def __init__(self, *a, **k):
                pass

            async def propose(self, *_a, **_k):
                return payload

        with patch("app.services.wiki_compiler.CuratorClient", _Stub):
            self.compiler.compile_ingest_job(
                1, {"file_id": 1, "text": "Alpha is verified. Filler text."}
            )

        rows = self.conn.execute(
            "SELECT details FROM wiki_lint_findings WHERE origin = 'curator'"
        ).fetchall()
        # Guard: all four probe findings must actually have been persisted —
        # an empty result set would make the cap loop vacuously green.
        self.assertEqual(
            len(rows), 4,
            f"expected the 4 curator probe findings (missing-quote + "
            f"unknown-chunk + quote-mismatch unsupported_claims + "
            f"contradiction), found {len(rows)}",
        )
        for row in rows:
            details = json.loads(row["details"] or "{}")
            self.assertTrue(details, "curator finding details must be non-empty")
            for key, value in details.items():
                if isinstance(value, str):
                    self.assertLessEqual(
                        len(value), _MAX_DETAIL_CHARS,
                        f"curator lint detail field {key!r} must be capped "
                        f"at <= {_MAX_DETAIL_CHARS} chars (got {len(value)})",
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
