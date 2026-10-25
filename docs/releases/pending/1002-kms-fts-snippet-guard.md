# KMS/documents FTS retrieval stops wedging on oversized bodies (#1002)

Issue: #1002 (production incident 2026-10-22: chat queries whose FTS terms
matched a KMS entry compiled from a 47.8M-char spreadsheet body wedged worker
threads forever inside SQLite FTS5's `snippet()`; the same unguarded pattern sat
on the documents ranked-search path).

## What changed

- Every content-column FTS5 auxiliary call in `kms_retrieval._retrieve_sync`
  and the documents ranked-search content arm now sits behind a lazily-evaluated
  byte-length guard:

  `CASE WHEN length(CAST(<col> AS BLOB)) <= FTS_SNIPPET_MAX_COLUMN_BYTES THEN snippet(…) END`

  (`backend/app/services/fts_query.py`, default 131072 bytes). The measure is
  BYTES rather than plain `length()` because plain `length()` stops at the first
  NUL byte — a NUL-prefixed body would report a tiny length, pass the guard, and
  send `snippet()` over the whole column (review-verified: 334–504 s on 2M-char
  NUL bodies; `.xlsx`/PDF ingestion and the manual KMS API can all produce or
  accept such content). Byte length also makes the same numeric ceiling stricter
  for multibyte text. Measured snippet() cost is super-linear in column size AND
  match density (0.4 s @ 200K chars at probe density; 9.8 s @ 1M; >90 s killed @
  43M; a single all-matching row just under the ceiling measured 1.4–3.3 s on
  dev boxes), while `MATCH` + ranking alone stays fast at every size (0.09 s @
  43M) — so **stored bodies are NOT truncated and full-body FTS search coverage
  is unchanged** (large documents remain fully searchable; a deep-term match on
  a multi-megabyte body still hits). Oversized rows now return a bounded
  head-window excerpt instead of hanging the worker. The 131072-byte ceiling is
  an extrapolation below the probe's mechanism-validation point, validated
  indirectly by the AC1/AC4 timing checks on CI; the shipped pin
  (`0 < value <= 200000`) keeps any future drift inside the measured budget
  envelope.
- The `files_search_fts` metadata snippets (file_name, file_type, status,
  source, document_date) remain exempted by the census as bounded-metadata
  columns — bounded at write/validation time. That exemption's premise was
  wrong for `email_subject`/`email_sender`, which are stored uncapped from
  mail headers; the email-metadata residual is closed by #1005: both columns
  are bounded behind the same NUL-proof
  `length(CAST(<col> AS BLOB))` length guard as the content arm (an
  oversized header skips `snippet()` entirely and the excerpt falls through
  the existing COALESCE fallbacks), and the census exemption is now an
  explicit bounded-column allowlist: an unguarded `files_search_fts`
  snippet bound to a LITERAL column index (the email columns, any future
  unbounded metadata column, or an invalid/out-of-range index) fails the
  census. The live metadata arm builds its calls through the dynamic
  `{col}` helper, which the census intentionally classifies
  bounded-metadata as a compatibility case — the runtime guard for that
  site is pinned functionally by the k7c/k7e suites and the #1005 timing
  bounds, not by the census. The
  `documents.py` code comments describe both guards precisely.
- Read paths stop materializing unbounded bodies into their results where only
  a bounded window is consumed: the KMS retrieval SELECT carries
  `substr(body, 1, 600)`; the documents-search CTE carries
  `substr(parsed_text, 1, 600)`; `KMSStore.list_entries` bounds `body` to a
  2048-char head window and reports the true length in a new `body_chars` field
  (list responses only — detail/create/update carry `body_chars: null`, and the
  single-entry detail GET still returns the full body). SQLite itself still
  reads each column once to count or slice it (linear, inherent to any length
  guard — the super-linear auxiliary-function wedge is what this removes), and
  `get_entry_by_file` is an id-only lookup (sole caller used only the id).
- Guardrail: `backend/tests/test_k7_snippet_guard_census.py` fails CI when any
  `snippet(`/`highlight(` site under `backend/app` is not bound to its OWN
  NUL-proof `length(CAST(<col> AS BLOB))` guard for the column it reads (no
  sibling-guard inheritance; case/whitespace-tolerant; `sql +=` and
  adjacent-literal constructions scanned; production paths like `ab_testing.py`
  not skipped), or is not a `files_search_fts` bounded-metadata call, with a
  sensitivity suite that plants each previously-invisible form.
  `test_k7b_snippet_threshold_and_pins.py` pins the threshold envelope and the
  bounded-excerpt clauses; `test_k7c_feedback_pins.py` pins the NUL-guard
  behavior on both paths, the HTTP list wire shape, and the disclosed
  deep-term-excerpt contract.

## Disclosed limitations

- Over-threshold bodies get a bounded head-based excerpt rather than a
  match-centered snippet in KMS evidence and documents-search results; the
  excerpt may not contain the matched term. For entries compiled from ingested
  files the full match-centered evidence still reaches chat answers through the
  chunked RAG path (the underlying file is chunked); manually-created
  over-threshold entries have no such secondary path and rely on this bounded
  excerpt alone.
- The documents ranked search evaluates the (now cheap, guarded) snippet for
  every matched row before pagination, so query cost is linear in matched-row
  count — measured 0.47/2.4/12.1 s for 25/100/500 near-threshold rows at probe
  density, and an 8-row chat-path query at adversarial (all-matching) density
  measures ~11–26 s on dev boxes, i.e. seconds to tens of seconds, not
  sub-second. There is no row cap on that path (pre-existing shape, disclosed).
  KMS chat retrieval itself is capped at 8 candidate rows, and rows above the
  ceiling are skipped outright.
- `GET /kms/entries` (and `/kms/search`) now return a truncated `body` (head
  window) alongside `body_chars`. Clients must NOT round-trip a list response's
  `body` back into `PUT /kms/entries/{id}` — a write-back would persist the
  truncated window over the stored body. Fetch the single-entry detail GET
  before updating. (In-repo consumers already do this.)
- The census is a best-effort textual scanner, not a semantic analysis: it
  cannot prove guard/association in SQL assembled across Python expressions it
  does not see (a guard in one literal and a call in another is not associated),
  it pins a site-count floor of exactly today's count (a benign refactor that
  merges calls trips it loudly), and its tokenizer assumptions are Python-3.11
  (CI-pinned; 3.12+ tokenization changes make its floor fail loudly rather than
  pass silently).
- Out of scope: `/wiki/search` and the unified-search wiki arm materialize full
  hand-authorable `markdown` per matched page (no auxiliary function — no wedge
  possible; bounded by the arm's result limit); the memory and wiki FTS arms are
  MATCH-only (no auxiliary functions — swept, nothing to guard).
