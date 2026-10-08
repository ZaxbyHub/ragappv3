# Format-specific ingestion and parse-thread deadlines stop silently failing (issue #703)

Workstream B PR 14 of 16 (frontier audit 20260923T174456Z, findings T1-02-K-05/-06, T1-02-S2-03, T1-25-K-05/-10, T1-25-KR-07, T1-25-S2-07/-08).

## Backend

- `.sql`/`.ddl` ingestion no longer silently drops everything that is not a
  `CREATE TABLE`: the schema parser keeps its structured table extraction and
  additionally emits every other top-level SQL statement (views, inserts,
  indexes, procedures, ...) as whitespace-normalized `other_sql` chunks
  labelled by statement type, in document order, so no SQL construct
  vanishes without a trace and a non-table-only file is never mislabeled
  "empty, encrypted, or unsupported" (T1-02-K-05). A schema file whose
  residual is only comments now fails with an accurate "no CREATE TABLE
  statements and no other SQL statements to index" error instead of the
  generic empty-content message.
- The statement split is quote-aware, so a semicolon inside a `'...'`,
  `"..."`, or `` `...` `` span keeps its statement whole; PostgreSQL
  dollar-quoted bodies (`$$...$$`, `$tag$...$tag$`) stay one statement; and
  MySQL `#` line comments are deliberately NOT stripped: '#' is ambiguous
  across dialects (T-SQL `#temp` tables are identifiers), and silently
  stripping it corrupted valid T-SQL; `#` text surfaces as `other_sql`
  content instead — noisy but never lossy. Comments of the stripped kinds
  never become chunks — including commented-out statements — and
  table classification is anchored to statement start, so a "CREATE TABLE"
  inside a string literal of another statement neither splits it nor mints
  a phantom table chunk.
- The CREATE TABLE column-block capture is bounded, removing the
  quadratic end-of-input scan on unterminated statements (the hazard the
  audit measured at 13.3s for 4000 unterminated tables); a table whose
  column block exceeds the bound is indexed verbatim as an `other_sql`
  statement instead.
- A schema file containing more than 20,000 statements fails with an
  accurate error instead of flooding the embedding pipeline; this cap is
  `SchemaParser.MAX_STATEMENT_CHUNKS`.
- Schema files decode through a real-world encoding chain: an unambiguous
  BOM wins (UTF-16/UTF-32/UTF-8), otherwise strict UTF-8, with a lossy
  cp1252 last resort — a UTF-16 (BOM) `.sql` now yields its table instead
  of zero chunks (T1-02-K-06). A BOM whose body fails the BOM's codec
  degrades through the plain chain instead of raising. Decoded text that
  is mostly replacement characters (a non-cp1252 legacy encoding) or
  mostly NUL bytes (binary content, BOM-less UTF-16) fails with an
  accurate error instead of indexing mojibake.
- CSV ingestion reads through the same class of chain on every leg —
  preserving the issue-#513 `keep_default_na=False` literal-NA semantics —
  while streaming straight from the path in the common case (no
  whole-file byte/str copies; the buffered path is reserved for BOM'd and
  non-UTF-8 files), so a Windows "CSV (Comma delimited)" export with
  non-ASCII cells now ingests instead of failing the whole file
  (T1-25-K-05).
- An image whose OCR could not run (for example, the tesseract binary is
  missing) or failed is now flagged degraded (`success=False` with the
  stable `ocr_failed` error code) instead of returning a clean success
  whose only searchable payload was the filename and dimensions: the file
  fails ingest with the `PARSER_UNAVAILABLE` stable code — the
  classification the audit named — and its parse-quality diagnostics no
  longer present the page as OCR-sourced (T1-25-K-10). The user-facing
  message text stays content-free per the #562 redaction contract.
- The atom-kind map covers the categories the pinned `unstructured`
  0.18.x actually emits for plain text and formulas: `UncategorizedText`
  maps to TEXT and `Formula` to EQUATION, so those elements stop degrading
  to `unknown` (T1-25-KR-07). Note `equation` atoms are part of the
  vision-enrichment eligibility set, so deployments with multimodal
  enrichment enabled will enrich formula atoms on the next reindex.
- All four parse paths — general documents, spreadsheets, schemas and
  images — now run under the same `document_parse_timeout` deadline on a
  dedicated bounded parser executor (4 workers), so slow parses queue for
  a parser slot instead of occupying the shared default executor that
  also serves route/DB work (T1-02-S2-03, T1-25-S2-07). The image path
  had no deadline either before this change.
- A per-file, process-wide in-flight parse registry refuses a new parse of
  a file whose previous (timed-out) parse thread has not finished, and a
  parse whose queued work item is dropped by the deadline releases its
  slot immediately (a queued-cancel can no longer wedge the reservation).
  Registry key lifetime is the worker thread's lifetime: a parse thread
  that never returns keeps refusing that file's retries until the process
  restarts (T1-25-S2-08).
- Retries of a timed-out or in-flight-refused parse wait out at least one
  full `document_parse_timeout` window before requeueing (both the lease
  and legacy transports), instead of burning the 1s/2s backoff ladder
  against a condition that cannot clear that fast, and persist the new
  stable `PARSE_TIMEOUT` code so operators can tell a deadline hit from a
  content failure.

## Tests

- `backend/tests/test_b14_parser_deadlines.py` (frozen acceptance checks,
  C1-C8) and `backend/tests/test_b14_deadlines_supplemental.py` (36 pins:
  image-path deadline + in-flight refusal, queued-cancel release, registry
  liveness, splitter/comment/dollar-quote/`#` edge cases, encoding-gate
  legs, statement cap, bounded-scan regression, PARSER_UNAVAILABLE and
  PARSE_TIMEOUT classification, retry-delay floor, and the upgraded AST
  census guardrail) pin the new behavior; the issue-#513 NA-cell and
  quoted-identifier checks pin the preserved behavior.
- The three real-image processor tests now simulate successful OCR via
  monkeypatch: they previously passed in CI only because the OCR-failure
  swallow reported a clean success on hosts without the tesseract binary.

## Known caveats

- Encoding gates catch NUL-interleaved and replacement-heavy payloads;
  full charset detection (e.g. pure-GBK content whose bytes all fall in
  cp1252's defined range) remains out of scope and indexes as mojibake,
  and the NUL sniff samples the first 4KB (NULs appearing only after
  that window stream through as text).
- A parse thread that truly never returns keeps refusing that file's
  retries until process restart; with the retry floor, a repeatedly
  refusing file reaches its terminal state after roughly one
  timeout-window wait per remaining attempt (minutes, not seconds).
- An unterminated PostgreSQL dollar-quoted body runs to end of input
  (SQL-correct semantics): the remainder after the opening tag is kept
  as one statement's content.
- Retry-delay floors interact with the attempts cap as accepted design:
  a file whose parses keep timing out goes terminal after its attempt
  budget with one timeout-window wait between attempts.
