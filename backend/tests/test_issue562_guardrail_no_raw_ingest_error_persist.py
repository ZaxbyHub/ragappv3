"""Guardrail for issue #562 (audit finding C26) — defect-class census.

Class: server-internal strings (absolute filesystem paths, raw Python
exception text) interpolated into ``files``-table error columns or API-visible
fields without passing a redaction boundary.

This census pins the invariant that the ingestion persist sites in
``document_processor.py`` and ``background_tasks.py`` never interpolate
``str(e)``/f-string payloads into persisted error fields, and that the
documents API never forwards the raw stored ``file_path`` to a response.
"""

import ast
import os
import unittest

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROCESSOR = os.path.join(BACKEND, "app", "services", "document_processor.py")
BACKGROUND = os.path.join(BACKEND, "app", "services", "background_tasks.py")
DOCUMENTS = os.path.join(BACKEND, "app", "api", "routes", "documents.py")

# The shipped stable-code set (issue #562). Deliberately NOT the acceptance
# superset used by the trace's frozen checks: UNSUPPORTED_FORMAT is
# unreachable (SpreadsheetParser's extension gate matches _is_spreadsheet_file)
# and a future reachable code may be added here deliberately.
SHIPPED_INGEST_ERROR_CODES = frozenset(
    {"PARSER_UNAVAILABLE", "PARSE_FAILED", "FILE_MISSING", "ENRICHMENT_FAILED"}
)

_PERSIST_PATTERNS = (
    "error_message=str(",
    "message=str(",
    'error_message=f"',
)


def _source(path: str) -> str:
    with open(path, "r", encoding="utf-8") as handle:
        return handle.read()


class NoRawIngestErrorPersistTest(unittest.TestCase):
    """No raw exception interpolation reaches a persisted error field."""

    def test_processor_has_no_raw_error_persist_interpolation(self):
        source = _source(PROCESSOR)
        for pattern in _PERSIST_PATTERNS:
            self.assertNotIn(
                pattern,
                source,
                f"document_processor.py must not contain {pattern!r}: persisted"
                " error fields are user-facing (issue #562); use"
                " redact_ingest_error()/format_ingest_error()",
            )

    def test_background_tasks_has_no_raw_error_persist_interpolation(self):
        source = _source(BACKGROUND)
        for pattern in _PERSIST_PATTERNS:
            self.assertNotIn(
                pattern,
                source,
                f"background_tasks.py must not contain {pattern!r}: persisted"
                " error fields are user-facing (issue #562); the worker passes"
                " exceptions to _handle_failure which redacts them",
            )

    def test_worker_persists_through_redaction(self):
        # Structural pin: _mark_task_permanently_failed must go through
        # redact_ingest_error for exception payloads.
        tree = ast.parse(_source(BACKGROUND))
        found_redact_call = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == (
                "_mark_task_permanently_failed"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "redact_ingest_error"
                    ):
                        found_redact_call = True
        self.assertTrue(
            found_redact_call,
            "_mark_task_permanently_failed must persist via"
            " redact_ingest_error() for exception payloads (issue #562)",
        )

    def test_documents_routes_have_no_raw_exception_http_details(self):
        # The synchronous ingestion handlers must not interpolate raw
        # exception text into HTTP details (issue #562 sweep).
        import re

        source = _source(DOCUMENTS)
        raw_detail = re.compile(r'detail=f"[^"]*\{(e|exc)\}')
        self.assertEqual(
            raw_detail.findall(source),
            [],
            "documents.py must not interpolate raw exceptions into HTTP"
            " details; keep the detail fixed and the raw text in the log",
        )

    def test_response_layer_does_not_forward_raw_file_path(self):
        source = _source(DOCUMENTS)
        self.assertNotIn(
            'file_path=row["file_path"]',
            source,
            "_row_to_document_response must project file_path through"
            " _vault_relative_file_path() (issue #562)",
        )

    def test_shipped_code_set_is_stable_and_known(self):
        from app.services.document_processor import (
            _INGEST_ERROR_REASONS,
            INGEST_ERROR_FILE_MISSING,
            INGEST_ERROR_PARSE_FAILED,
            INGEST_ERROR_PARSER_UNAVAILABLE,
        )

        self.assertEqual(
            SHIPPED_INGEST_ERROR_CODES,
            frozenset(_INGEST_ERROR_REASONS.keys()),
            "every shipped ingest error code must have a content-free reason"
            " and no undocumented code may ship",
        )
        self.assertIn(INGEST_ERROR_PARSER_UNAVAILABLE, _INGEST_ERROR_REASONS)
        self.assertIn(INGEST_ERROR_PARSE_FAILED, _INGEST_ERROR_REASONS)
        self.assertIn(INGEST_ERROR_FILE_MISSING, _INGEST_ERROR_REASONS)


if __name__ == "__main__":
    unittest.main()
