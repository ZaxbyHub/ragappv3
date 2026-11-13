# Error-code fidelity and Draft Room detail hygiene (#1112, #1119)

## What

- **#1112** — a same-hash retry or reindex whose files-row flip collides with
  another `indexed`/`partial` row in the same vault (the
  `idx_files_hash_vault_indexed` partial unique index) stops being persisted
  as `PARSE_FAILED: document could not be parsed`:
  - new stable ingest code `DUPLICATE_FILE` ("a files row with the same
    content hash is already indexed in this vault; resolve which row owns the
    hash before retrying"), classified from `DuplicateFileError` and from the
    files hash/vault uniqueness `IntegrityError` signature only (other
    integrity failures — FK, CHECK, other tables — keep the existing
    fallback classification and retry semantics);
  - a pre-vector duplicate-ownership guard (first statement inside
    `process_existing_file`'s processing try): another row owning the hash
    (own row excluded) settles the file at the terminal duplicate state with
    ZERO vector writes, for every caller — upload retry, admin retry, and the
    reindex path; the finalization partial index stays as the race backstop
    with a fenced row settlement of its own;
  - both ingestion transports (legacy ladder and lease) treat the duplicate
    class as deterministic, mirroring the #820 DIMENSION_CHANGED first-attempt
    terminal settle (no requeue, no `attempt_cap_exceeded` prefix); an
    undisturbed indexed/partial row whose CURRENT bytes hash to a sibling's
    stored hash keeps its prior status (#704 restore intent) with the
    duplicate reported through the job result;
  - reindex jobs track duplicate-hash failures in a separate non-retryable
    set (`duplicate_file_ids`): duplicate-only failures settle the job
    `failed` on the first attempt; mixed failures retry only the transient
    ids; every terminal path composes the cumulative duplicate outcomes into
    `result_json` (the operator-visible surface), and a job holding a
    permanent duplicate never completes;
  - the attempt-cap terminal writer now sets `phase_message` alongside
    `error_message` (the same redacted stable reason, 500-char cap), so a
    capped row can no longer keep its last stage label ("Writing vector
    index").
- **#1119** — the HTTP-500 detail-hygiene CI gate
  (`scripts/check_a04_http500_detail_hygiene.py`) catches the
  variable-status shape issue #917 was filed about: a `DraftRoomHTTPError`
  (or `HTTPException`) call whose status is computed at runtime is flagged
  when inline, time-ordered per-scope analysis resolves it to a 5xx-capable
  value-set (literals; module-level and function-local mapping dicts and
  their `.get(key, DEFAULT)` defaults) with an interpolated detail —
  dominating `>= 500` guards prove guarded fall-through arms safe (strict
  `>` guards do not refine: 500 stays reachable); sync functions, async
  functions, and module-level code are covered (the literal arm is preserved
  everywhere). The promotion path-conflict 409 detail no longer echoes the
  server filesystem destination path: fixed client-safe detail, with the RAW
  path logged server-side from the new structured
  `DraftPromotionPathConflictError.dest_path` attribute (repr-escaping-free,
  so single backslashes survive on Windows).

## Why

Issue #1112 was filed from a live deployment incident (an admin retry of a
duplicate-hash file persisted `PARSE_FAILED` with a stale phase label, and
the retry burned the attempt cap after upserting vectors). Issue #1119
carried the two residuals PR #1116 could not fix in scope: the a04 gate's
literal-only predicate let the exact #917 shape pass silently, and the
promotion 409 detail disclosed the server path to authenticated users.

## Tests

- Frozen acceptance checks C1-C5 (anchor
  `issue-tracer-checkpoint-v1 … manifest=5d60fbc9… semantics=fd6ea529…`) —
  4 DISCRIMINATING RED→GREEN + 1 PRESERVING GREEN→GREEN;
  `backend/tests/test_a04_variable_status_echo.py` and
  `backend/tests/draft_room/test_promotion_conflict_detail.py` ship as the
  #1119 regression specs.
- `backend/tests/test_1112_duplicate_settle.py` (11 tests): guard legs
  (indexed/partial owner, own-row control, bytes-changed restore), both
  transports, nonduplicate-FK control, the finalization-race backstop, and
  the three reindex-job terminal shapes.
- `backend/tests/test_a04_variable_status_echo_impl.py` (8 legs): the gate
  analyzer's mutation shapes (mixed mappings, function-local mappings,
  rebinding, call-before-rebind, async/module-level literals, strict-gt
  guard, guarded control).
- `backend/tests/test_issue562_guardrail_no_raw_ingest_error_persist.py`:
  `DUPLICATE_FILE` registered in the shipped-codes census.
