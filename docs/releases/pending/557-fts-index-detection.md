# Truthful FTS index detection at startup (Issue #557, C06)

## What changed

### Backend
- `VectorStore` and `app.lifespan` now detect the full-text-search index by
  COLUMN and index TYPE through one shared helper
  (`app.services.vector_store.has_index(table, column, kind)`) instead of
  comparing an index-name literal. The engine auto-derives index names
  (`text_idx` for the FTS index on `text`), so the previous
  `idx.name == "fts_text"` guard could never match: every boot logged a false
  ERROR ("Hybrid search is enabled but the FTS index is missing") and, on
  every boot after the first, a false WARNING ("FTS index creation failed")
  from the dead create-if-missing branch re-attempting creation.
- All sibling ANN (IvfPq) index-existence checks in `vector_store.py`
  (churn-baseline seed, search freshness probe, conditional creation,
  post-delete maintenance) and in the standalone
  `backend/scripts/reconcile_lancedb_sqlite.py` ops script migrated to the
  same column+type detection, closing the recurrence class previously fixed
  for the ANN path in #148.

### Operator-visible outcome
- With hybrid search enabled (the default), startup no longer logs the false
  "FTS index is missing" ERROR or the false "FTS index creation failed"
  WARNING; a genuinely missing FTS index still logs the ERROR, and a real
  creation failure still logs the WARNING.
- The create-if-missing branch now actually skips once the index exists (no
  more per-boot "Index name 'text_idx' already exists" round trip).
- No retrieval behavior change: search results, hybrid ranking and FTS
  status tracking are untouched; no configuration or data migration is
  involved.

### Rollback
Revert the commit; detection returns to the (dead) name-based guards. No
schema/config changes to undo.

### Tests
- New real-LanceDB integration suite
  `backend/tests/test_fts_index_detection_integration.py` (second-boot noise,
  startup validation truthfulness, fresh-boot invariants, ANN baseline seed
  against a real IvfPq index).
- New source-contract guardrail
  `backend/tests/test_index_detection_source_contract.py` banning name-based
  index guards and the `fts_text` literal from detection code.
- Repaired 12 test files whose fakes asserted the fictional `fts_text`
  literal or name-only index shapes; fakes now mirror the real
  engine-reported index attributes.
