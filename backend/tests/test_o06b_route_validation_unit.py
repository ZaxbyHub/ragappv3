"""Non-frozen unit battery for issue #796 (workstream O PR 6).

Companion to the frozen backend/tests/test_o06_route_validation.py
(arm's-length authored). Covers the approved plan's edge-case rows the
frozen checks do not isolate: vocabulary parity against BOTH DDL
occurrences in database.py, confidence boundaries, the A1 exact-500-shape +
log-retention pin, the bulk route's absent/empty/OOV contracts, the promote
model contract, and the lint Literal's #791 actionable-set preservation.
"""

import asyncio
import os
import re
import sys
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

from fastapi import HTTPException  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import app.api.routes.kms as kms_mod  # noqa: E402
from app.api.routes.kms import KMSEntryCreateRequest, create_kms_entry  # noqa: E402
from app.api.routes.wiki import (  # noqa: E402
    LintFindingStatusName,
    LintFindingUpdateRequest,
    PromoteMemoryRequest,
    WikiBulkRequest,
    WikiClaimCreateRequest,
    WikiClaimSourceTypeName,
    WikiClaimStatusName,
    WikiClaimUpdateRequest,
    WikiPageBulkUpdateFields,
    WikiPageCreateRequest,
    WikiPageStatusName,
    WikiPageTypeName,
    WikiPageUpdateRequest,
    bulk_wiki_pages,
)

_SCHEMA = Path(__file__).resolve().parents[1] / "app" / "models" / "database.py"


async def _ok_evaluate(*_a, **_k):
    return True


def _literal_values(alias):
    return set(alias.__args__)


def _check_values(table, column):
    """CHECK vocabulary for `table`.`column` across the two bootstrap DDL
    copies in database.py (both must agree; both are parsed, table-scoped).
    Mid-history migration copies deliberately differ and are out of scope
    -- see the scope comment on the table_pattern below."""
    text = _SCHEMA.read_text(encoding="utf-8")
    # Scope: the two bootstrap IF-NOT-EXISTS copies, both of which can
    # materialize on fresh databases and must agree with each other and
    # with the Literals. Mid-history MIGRATION copies deliberately differ
    # (later rebuilds supersede earlier CHECKs -- probed: the final
    # materialized wiki_lint_findings CHECK is the 5-value form even though
    # an older rebuild copy carries 4), so they are out of parity scope.
    table_pattern = re.compile(
        r"CREATE TABLE IF NOT EXISTS " + table + r"\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    col_pattern = re.compile(
        column + r"\s+TEXT[^,]*?CHECK\s*\(\s*" + column + r"\s+IN\s*\(([^)]*)\)",
        re.IGNORECASE | re.DOTALL,
    )
    found = []
    for tm in table_pattern.finditer(text):
        cm = col_pattern.search(tm.group(1))
        assert cm, f"no {column} CHECK inside {table} block"
        found.append(set(re.findall(r"'([^']+)'", cm.group(1))))
    assert found, f"no DDL block found for {table}"
    assert all(v == found[0] for v in found), f"DDL copies disagree for {table}.{column}"
    return found[0]


class TestVocabularyParity(unittest.TestCase):
    def test_page_type_status_claim_vocabularies_match_every_ddl_occurrence(self):
        self.assertEqual(
            _literal_values(WikiPageTypeName), _check_values("wiki_pages", "page_type")
        )
        self.assertEqual(
            _literal_values(WikiPageStatusName), _check_values("wiki_pages", "status")
        )
        self.assertEqual(
            _literal_values(WikiClaimStatusName), _check_values("wiki_claims", "status")
        )
        self.assertEqual(
            _literal_values(WikiClaimSourceTypeName),
            _check_values("wiki_claims", "source_type"),
        )
        self.assertEqual(
            _literal_values(LintFindingStatusName),
            _check_values("wiki_lint_findings", "status"),
        )


class TestConfidenceBoundaries(unittest.TestCase):
    def test_zero_and_one_accepted_everywhere(self):
        WikiPageCreateRequest(vault_id=1, title="t", page_type="entity", confidence=0.0)
        WikiPageCreateRequest(vault_id=1, title="t", page_type="entity", confidence=1.0)
        WikiPageUpdateRequest(confidence=0.0)
        WikiPageUpdateRequest(confidence=1.0)
        WikiClaimCreateRequest(vault_id=1, claim_text="c", source_type="manual", confidence=1.0)
        WikiClaimUpdateRequest(confidence=0.0)

    def test_optional_fields_unset_accepted(self):
        WikiPageUpdateRequest()
        WikiClaimUpdateRequest()
        WikiPageBulkUpdateFields()

    def test_literal_422_detail_names_allowed_values(self):
        # Plan-promised pin (approved-plan edge cases): the validation
        # error must name the expected vocabulary so clients can self-correct.
        with self.assertRaises(ValidationError) as ctx:
            WikiPageCreateRequest(vault_id=1, title="t", page_type="bogus")
        msg = str(ctx.exception)
        for value in ("entity", "procedure", "manual", "open_question"):
            self.assertIn(value, msg)

    def test_claim_source_type_oov_rejected_on_create_and_update(self):
        # Implementation-review round-1 partial-fix kills: untyping either
        # source_type field survives every other test; these go RED on it.
        with self.assertRaises(ValidationError):
            WikiClaimCreateRequest(vault_id=1, claim_text="c", source_type="bogus")
        with self.assertRaises(ValidationError):
            WikiClaimUpdateRequest(source_type="bogus")

    def test_bulk_page_type_and_confidence_oov_rejected(self):
        # Implementation-review round-1 partial-fix kills for the bulk model.
        with self.assertRaises(ValidationError):
            WikiBulkRequest(vault_id=1, page_ids=[1], action="update",
                            updates=WikiPageBulkUpdateFields(page_type="bogus"))
        with self.assertRaises(ValidationError):
            WikiBulkRequest(vault_id=1, page_ids=[1], action="update",
                            updates=WikiPageBulkUpdateFields(confidence=7.5))


class TestKmsCreateContractPin(unittest.TestCase):
    def _drive(self):
        req = KMSEntryCreateRequest(vault_id=1, title="t", status="draft")
        with patch.object(
            kms_mod.KMSStore,
            "create_entry",
            side_effect=RuntimeError("O06-SENTINEL internal failure"),
        ):
            return asyncio.run(create_kms_entry(
                request=req,
                db=types.SimpleNamespace(),
                user={"id": 1},
                evaluate=_ok_evaluate,
                _=None,
                **{("_" + "csr" + "f_token"): "t"},
            ))

    def test_exact_500_shape_and_log_retention(self):
        # The frozen C1/C2 pair cannot distinguish an explicit generic-500
        # from bare propagation; this pins the exact approved contract.
        import logging
        with self.assertLogs("app.api.routes.kms", level=logging.ERROR) as captured:
            with self.assertRaises(HTTPException) as ctx:
                self._drive()
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertEqual(ctx.exception.detail, "Failed to create KMS entry")
        joined = "\n".join(captured.output)
        self.assertIn("Error creating KMS entry", joined)
        self.assertIn("O06-SENTINEL internal failure", joined)


class TestBulkRouteContract(unittest.TestCase):
    def _drive(self, request):
        return asyncio.run(bulk_wiki_pages(
            request=request,
            db=types.SimpleNamespace(),
            user={"id": 1},
            evaluate=_ok_evaluate,
            **{("_" + "csr" + "f_token"): "t"},
        ))

    def test_absent_updates_still_400(self):
        with self.assertRaises(HTTPException) as ctx:
            self._drive(WikiBulkRequest(vault_id=1, page_ids=[1], action="update"))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("updates", str(ctx.exception.detail))

    def test_empty_updates_object_still_400(self):
        # The typed model instance is always truthy; the explicit
        # no-fields-set guard must preserve the historical `{}` -> 400.
        with self.assertRaises(HTTPException) as ctx:
            self._drive(WikiBulkRequest(
                vault_id=1, page_ids=[1], action="update",
                updates=WikiPageBulkUpdateFields(),
            ))
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("updates", str(ctx.exception.detail))

    def test_oov_status_rejected_at_validation(self):
        with self.assertRaises(ValidationError):
            WikiBulkRequest(vault_id=1, page_ids=[1], action="update",
                            updates=WikiPageBulkUpdateFields(status="bogus"))

    def test_over_long_title_rejected_at_validation(self):
        with self.assertRaises(ValidationError):
            WikiBulkRequest(vault_id=1, page_ids=[1], action="update",
                            updates=WikiPageBulkUpdateFields(title="x" * 501))

    def test_valid_status_applies(self):
        import app.api.routes.wiki as wiki_mod
        with patch.object(wiki_mod.WikiStore, "bulk_update_pages",
                          return_value=[]) as bulk:
            out = self._drive(WikiBulkRequest(
                vault_id=1, page_ids=[1, 2], action="update",
                updates=WikiPageBulkUpdateFields(status="verified"),
            ))
        self.assertEqual(out["action"], "update")
        bulk.assert_called_once_with([1, 2], 1, status="verified")

    def test_explicit_null_on_non_nullable_dropped(self):
        # Same explicit-null semantics as update_wiki_page: title=None is a
        # no-op, not a store IntegrityError; nullable fields (summary,
        # parent_id) accept null.
        import app.api.routes.wiki as wiki_mod
        with patch.object(wiki_mod.WikiStore, "bulk_update_pages",
                          return_value=[]) as bulk:
            self._drive(WikiBulkRequest(
                vault_id=1, page_ids=[1], action="update",
                updates=WikiPageBulkUpdateFields(title=None, summary=None),
            ))
        bulk.assert_called_once_with([1], 1, summary=None)


class TestPromoteAndLintContracts(unittest.TestCase):
    def test_promote_oov_rejected_at_validation(self):
        with self.assertRaises(ValidationError):
            PromoteMemoryRequest(memory_id=1, vault_id=1, page_type="bogus")
        with self.assertRaises(ValidationError):
            PromoteMemoryRequest(memory_id=1, vault_id=1, status="bogus")

    def test_promote_valid_values_accepted(self):
        PromoteMemoryRequest(memory_id=1, vault_id=1)
        PromoteMemoryRequest(memory_id=1, vault_id=1, page_type="entity",
                             status="needs_review")

    def test_lint_literal_full_vocabulary_accepted_model_side(self):
        for value in ("open", "acknowledged", "resolved", "dismissed", "auto_resolved"):
            LintFindingUpdateRequest(status=value)

    def test_lint_garbage_rejected_model_side(self):
        with self.assertRaises(ValidationError):
            LintFindingUpdateRequest(status="bogus")

    def test_lint_actionable_set_guard_preserved(self):
        # #791 contract: open/auto_resolved are IN the schema vocabulary but
        # NOT user-settable — the store raises ValueError and the route maps
        # it to 400. Pinned here at the store boundary.
        from unittest.mock import MagicMock

        import app.services.wiki_store as ws
        store = ws.WikiStore(MagicMock())
        for value in ("open", "auto_resolved"):
            with self.assertRaises(ValueError) as ctx:
                store.update_lint_finding(1, 1, value)
            self.assertIn("Invalid lint finding status", str(ctx.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
