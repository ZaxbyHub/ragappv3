# Format-specific ingestion and parse-thread deadlines stop silently failing (issue #703)

Workstream B PR 14 of 16 (frontier audit 20260923T174456Z, findings T1-02-K-05/-06, T1-02-S2-03, T1-25-K-05/-10, T1-25-KR-07, T1-25-S2-07/-08).

## Backend

- `.sql`/`.ddl` ingestion no longer silently drops everything that is not a
  `CREATE TABLE`: the schema parser keeps its structured table extraction and
  additionally emits every other top-level SQL statement (views, inserts,
  indexes, procedures, ...) as verbatim `other_sql` chunks labelled by
  statement type, so no SQL construct vanishes without a trace and a
  non-table-only file is never mislabeled "empty, encrypted, or unsupported"
  (T1-02-K-05). Statements are split on `;`; a semicolon inside a string
  literal may split a residual chunk boundary, but both halves are indexed —
  no content is lost. Comments never become chunks; a comments-only schema
  file still reports zero extractable content.
- Schema files decode through a real-world encoding chain: an unambiguous
  BOM wins (UTF-16/UTF-32/UTF-8), otherwise strict UTF-8, with a lossy
  cp1252 last resort — a UTF-16 (BOM) `.sql` now yields its table instead
  of zero chunks (T1-02-K-06). UTF-16 without a BOM remains out of scope.
- CSV ingestion reads through the same class of chain (BOM-aware, then
  strict UTF-8, then lossy cp1252) on every leg preserving the issue-#513
  `keep_default_na=False` literal-NA semantics — a Windows "CSV (Comma
  delimited)" export with non-ASCII cells now ingests instead of failing
  the whole file (T1-25-K-05).
- An image whose OCR could not run (for example, the tesseract binary is
  missing) or failed is now flagged degraded (`success=False` with the
  stable `ocr_failed` error code) instead of returning a clean success
  whose only searchable payload was the filename and dimensions. The file
  ends in `error` status rather than a fake `indexed`; the persisted
  user-facing message text is owned by issue #704 (T1-25-K-10).
- The atom-kind map covers the categories the pinned `unstructured`
  0.18.x actually emits for plain text and formulas: `UncategorizedText`
  maps to TEXT and `Formula` to EQUATION, so those elements stop degrading
  to `unknown` (T1-25-KR-07).
- All four parse paths — general documents, spreadsheets, schemas and
  images — now run under the same `document_parse_timeout` deadline; the
  schema and spreadsheet paths previously had no deadline at all. On
  timeout the await is abandoned with the established timeout error
  (T1-02-S2-03, T1-25-S2-07).
- A per-file, process-wide in-flight parse registry refuses a new parse of
  a file whose previous (timed-out) parse thread has not finished, so the
  1s/2s/4s retry backoff can no longer stack concurrent parse attempts of
  the same file on the shared default executor. Registry key lifetime is
  the worker thread's lifetime: a parse thread that never returns keeps
  refusing that file's retries until the process restarts — deliberately,
  because a wall-clock TTL shorter than a legitimate long parse would
  re-open the overlap this guard prevents (T1-25-S2-08).

## Tests

- `backend/tests/test_b14_parser_deadlines.py` (frozen acceptance checks,
  C1-C8) and `backend/tests/test_b14_deadlines_supplemental.py`
  (image-path deadline, registry liveness, splitter edge cases, CSV
  encoding legs) pin the new behavior; the issue-#513 NA-cell and
  quoted-identifier checks pin the preserved behavior.
- The two real-image processor tests now simulate successful OCR via
  monkeypatch: they previously passed in CI only because the OCR-failure
  swallow reported a clean success on hosts without the tesseract binary.
