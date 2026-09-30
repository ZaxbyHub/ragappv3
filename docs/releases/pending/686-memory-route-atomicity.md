---
issue: 686
title: Memory edits and deletes keep wiki claims, embeddings and fields consistent
---

# Memory edits and deletes keep wiki claims, embeddings and fields consistent

Workstream A PR 4 of 7 (audit remediation, frontier audit 2026-09-23).

## What changed

- **Memory deletes and updates are atomic with their derived state**
  (#686 / T1-02-S-01, T1-02-K-09, T1-02-S-02). The wiki-claim stale-marking
  and the memory row write now run inside one explicit `BEGIN IMMEDIATE`
  transaction (`_atomic_memory_write` in `memories.py`). Previously the
  delete path's outermost `SAVEPOINT ... RELEASE` committed the marking
  before the DELETE ran on pool connections (default isolation level), a
  marking failure still deleted the memory, and an update committed its
  content before a claim invalidation whose failure was only logged. Now a
  DELETE failure rolls the marking back, a marking failure aborts the
  delete, and a failed invalidation keeps the previously stored content.
- **Updates are conflict-aware** (T1-02-S-05). `PUT /api/memories/{id}`
  accepts an optional `expected_updated_at` concurrency token: a stale
  token is rejected with 409 (`Memory was modified by another session`)
  both at request time and authoritatively inside the write transaction, so
  two tabs can no longer silently overwrite each other. Omitting the token
  keeps the old unconditional behavior; a memory deleted concurrently is a
  404, not a lost update. The Memory page sends the token from the row it
  loaded, so a stale tab gets a 409 toast instead of a silent overwrite.
- **Identical-content saves keep embeddings and claims** (T1-02-S-03,
  #515 AC27). The stored embedding is cleared only when the content actually
  changed (previously any request carrying `content` nulled it), and the
  re-embed runs only for real changes. Identical-content saves still keep
  sole-source wiki claims active.
- **Explicit null clears category and source** (T1-02-K-02). `category`
  and `source` join `tags`/`expires_at` in the `model_fields_set`
  explicit-null-clears contract (#515 AC19 semantics extended), and the
  Memory page sends `null` for cleared fields instead of dropping the key.
- **The memory list is bounded and legacy-tolerant** (T1-02-S2-09,
  T1-02-S-06). `GET /api/memories` gains `limit` (default 200, max 500) and
  `offset` paging — the default page still covers what the Memory page
  shows — and legacy rows whose `tags` JSON contains non-string elements no
  longer 500 the whole list (non-strings are dropped, strings kept).
- **Importance 0.0 round-trips on the routes** (T1-02-K-03). The route read
  paths replace `float(row or 0.5)` with a None-guard, matching the store
  paths fixed by #515.
- **The superadmin embedding backfill is single-flight** (T1-02-S2-07).
  Concurrent `POST /api/memories/backfill-embeddings` requests serialize on
  a per-event-loop lock instead of running overlapping full backfills.
- **5xx details carry no exception text** (TQ-sibling-batch-05-03). The
  memory, vault-delete and admin toggle/maintenance handlers return fixed
  detail strings and log server-side (the admin handlers previously had no
  log call at all). A new AST gate
  (`scripts/check_a04_http500_detail_hygiene.py`) enforces this for the
  three audited route modules and runs in the Quality-contracts CI job, the
  justfile `quality-contracts` recipe, and is pinned by
  `backend/tests/test_a04_gate_wiring.py` so the step cannot be silently
  dropped. `scripts/check_runtime_contract.py`'s `DOC_SCRIPT_RE` is widened
  to `[a-z0-9_]` so digit-bearing gate names stay inventory-enforced.

## Behavior notes

- Clients that relied on `GET /api/memories` returning an unbounded list
  now receive at most 200 rows per call by default (page with `limit`/
  `offset` for more; the maximum per call is 500).
- The concurrency token is string equality against `updated_at`
  (1-second resolution): two writes inside the same second are not
  distinguished. A monotonic version column is the complete fix and is out
  of scope for this change (no schema lane).
- The backfill single-flight lock is per-process (per event loop), matching
  the in-request overlap the audit found; multi-worker deployments were
  never serialized by this route.
- Documented asymmetries (unchanged by design): an empty-string `tags`
  value clears tags (its validator normalizes `""` to null), while an
  empty-string `category`/`source` stores the empty string; the Memory page
  always sends `null` for cleared fields. Separately, `importance: null` is
  a no-op (preserves the stored value) rather than a clear — importance has
  no meaningful cleared state — so it is deliberately a value-guard instead
  of a `model_fields_set` presence-guard.

## Verification

Thirteen frozen acceptance checks (12 discriminating RED→GREEN against the
pre-fix base, 1 preserving: #515 AC27) plus supplementary tests for the
legacy-tags content contract, pagination behavior, the 409 body and
vanished-row 404, fresh-token acceptance, and the
read-check-to-transaction race window; full backend suite, frontend
typecheck/lint/tests/build, all quality-contract gates and the SAST scan
run clean against CI parity (bandit baseline re-anchored as a pure line
shift, 131→131 signatures).
