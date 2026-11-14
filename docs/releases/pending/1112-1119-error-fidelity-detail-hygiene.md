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
    with a fenced row settlement of its own, now mirrored on `process_file`'s
    converged finalize tail (shared `_settle_duplicate_finalize_race`
    helper — the legacy scan/email/FileWatcher path settles the race loser
    too instead of stranding it in `processing`);
  - both ingestion transports (legacy ladder and lease) treat the duplicate
    class as deterministic, mirroring the #820 DIMENSION_CHANGED first-attempt
    terminal settle (no requeue, no `attempt_cap_exceeded` prefix); an
    undisturbed indexed/partial row whose CURRENT bytes hash to a sibling's
    stored hash keeps its prior status (#704 restore intent) with the
    duplicate reported through the job result;
  - reindex jobs track duplicate-hash failures in a separate non-retryable
    set (`duplicate_file_ids`): duplicate-only failures settle the job
    `failed` on the first attempt; mixed failures retry only the transient
    ids; the worker-settled terminal paths (completed-with-duplicates,
    attempt cap, duplicate-only) compose the cumulative duplicate outcomes
    into `result_json` (the operator-visible surface), and a job holding a
    permanent duplicate never completes. STALL-RECLAIM AND JANITOR
    SETTLEMENTS keep the error-only shape (duplicates remain discoverable in
    `payload_json`), and a staged dimension/identity rebuild still aborts on
    any per-file failure with full-vault retry semantics — the
    duplicate-accounting residuals on the janitor/stall-reclaim paths and
    the staged-rebuild path are disclosed in the PR body;
  - the attempt-cap terminal writers now set `phase_message` alongside
    `error_message` (the same redacted reason, 500-char cap) — the transport
    writer AND the janitor cap sweeps — so a capped row can no longer keep
    its last stage label ("Writing vector index").
- **#1119** — the HTTP-500 detail-hygiene CI gate
  (`scripts/check_a04_http500_detail_hygiene.py`) catches the
  variable-status shape issue #917 was filed about: a `DraftRoomHTTPError`
  (or `HTTPException`) call whose status is computed at runtime is flagged
  when inline, time-ordered per-scope analysis resolves it to a 5xx-capable
  value-set (literals; module-level and function-local mapping dicts and
  their `.get(key, DEFAULT)` defaults; annotated assignments; `with`/
  `async with` bodies; loop bodies via a state fixpoint that resolves
  var-to-var chains) with an interpolated detail — dominating `>= 500`
  guards prove guarded fall-through arms safe (strict `>` guards do not
  refine: 500 stays reachable), non-guard branches merge by union, a
  non-dict rebind clears a stale mapping binding, and a whole-tree literal
  pass keeps every placement the pre-widening gate covered (class bodies,
  decorators, default args, module level, sync/async function bodies). The
  promotion path-conflict 409 detail no longer echoes the server filesystem
  destination path: fixed client-safe detail (logged at warning per the
  module's 4xx doctrine), with the RAW path logged server-side from the new
  structured `DraftPromotionPathConflictError.dest_path` attribute
  (repr-escaping-free, so single backslashes survive on Windows).

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
- `backend/tests/test_1112_duplicate_settle.py` (16 tests): guard legs
  (indexed/partial owner, own-row control, bytes-changed restore), both
  transports, nonduplicate-FK control, the finalization-race backstop (plus
  its multi-worker write-permit wiring), the four reindex-job terminal
  shapes (each pinning the DISTINCT terminal-file `failed` count compose),
  a global-failure scope-preservation leg, and an end-to-end leg
  driving the REAL `_reindex_embed_all` over a seeded duplicate pair
  (asserts `duplicate_file_ids`, failed-scope membership, the stable code in
  details, and exactly one vector generation).
- `backend/tests/test_a04_variable_status_echo_impl.py` (24 legs): the gate
  analyzer's mutation shapes — mixed mappings (module and function-local),
  rebinding (after-guard, call-before-rebind, dict-to-int both directions),
  async/module-level literals, strict-gt guard, try/else and try-handler
  echoes, with/async-with and loop bindings, var chains, class-body/
  decorator/default-arg literal placements, and guarded controls.
- `backend/tests/test_issue562_guardrail_no_raw_ingest_error_persist.py`:
  `DUPLICATE_FILE` registered in the shipped-codes census.
