# Draft Room disposition integrity, transition-map guard, doc truth (#901, #920, #927)

## Backend

- **Re-disposition is refused (issue #901 AC1).** Dismissing or waiving a finding that
  is no longer `open` now returns 409 `finding_not_open` with `drafts.lock_version`
  unchanged (earlier preconditions keep their own codes: blockers refuse with
  `blocker_not_dismissable`, non-waivable waives with `finding_not_waivable`, blank
  notes with 422). Previously the same-target short-circuit in `_check_transition` let
  a second dismiss/waive through as a 200 "no-op" that still wrote a second audit row
  and bumped the lock version. The guard lives at the store seam
  (`DraftStore.dismiss_finding`/`waive_finding`), extending the apply path's reference
  pattern so every caller inherits it.
- **Storage faults are no longer client conflicts (issue #901 AC4).** A storage fault
  raised inside a disposition transaction (`sqlite3.OperationalError`, e.g. lock
  contention after the busy timeout) now surfaces as a server-side error instead of
  HTTP 409 `conflict`. Only the constraint-violation subclass
  (`sqlite3.IntegrityError`) keeps the 409 mapping. The frontend no longer renders
  lock contention as a reload-conflict banner.
- **Disposition eligibility uses the draft's current revision (issue #901 AC2).** The
  dismiss/waive response's `can_apply` is computed against the draft's current revision
  (defensive symmetry with the findings list; on a dispositioned finding the flag is
  closed either way because `can_apply` requires an `open` finding). Apply keeps using
  the revision it just created.
- **Findings list batches its reads (issue #901 AC3).** GET `/drafts/{id}/findings`
  now performs two store hops (authorization, then one post-authorization pass) instead
  of four, reading the `drafts` row exactly once per request — enforced by a
  store-level SQL-trace pin. Authorization order is unchanged (ownership, then vault
  read, then child reads). `DraftStore.list_findings` was removed: its only caller
  moved to the batched reader, leaving it unwired.
- **One shared disposition finish path (issue #901 AC5).** `waive_finding` routes
  through `_complete_finding_disposition` (extended with the waiver columns) instead of
  a hand-maintained copy of the four-step transaction; exactly one inline
  `DraftStore` finding-status writer remains.
- **Status-transition map guard (issue #927).** New
  `backend/tests/draft_room/test_n06b_status_transition_map.py` plus
  `test_n06d_guard_behavior_pins.py`: every status write in the three declared
  co-owner modules must resolve to a registered source — a named mirrored prior-status
  allowlist (with the `not in <CONST>` consult pinned at its live function), a located
  inline prior-status guard (matched as the complete `If.test` AST), or the documented
  settle-pair chain. The scanner resolves string assignments and `+=` chains (the
  `success_sql +=` idiom), splits SET bodies on top-level commas so a `status =` inside
  a string VALUE is not a write, and classifies non-literal right-hand sides
  (f-string, `%`-format, `.format`, `:named`, `+` concatenation, bare names) as
  `<dynamic>` — always failing closed. Because a static registry cannot see whether a
  guard actually GATES its write, behavioral negative pins put a draft in an illegal
  prior state and assert the refusal with an unchanged row for the mark-ready,
  `_move_draft`, compile-enqueue, and apply-allowlist sites.

## Documentation

- **SPEC 1.2 (delivery contract).** The draft-scoping MUST rule now carries its named
  post-authorization exception (`findings_page_after_authorization`, sole caller, hop-1
  authorization verified).
- **SPEC 4.1 module table (issue #920).** The `draft_store.py` row no longer claims
  "All SQLite CRUD"; it names the three declared co-owners and links the owner map.
- **SPEC 8.2 (issue #901/#920).** Dismiss/waive preconditions and the
  `finding_not_open` refusal documented, with the earlier preconditions' own codes.
- **SPEC 10.3 (issues #920/#927).** The status-write invariant is scoped to the owner
  map: four directly written tables, and the registered source kinds exactly as the
  guard implements them (allowlists kept by the route and pipeline co-owners, inline
  guards, settle-pair chain).
- **`draft_job_processor.py` docstring (issue #920).** Claim mediation is explicitly
  scoped to claims; the worker's direct co-owner writes (`drafts.status`,
  `draft_jobs`, `draft_inputs.parse_status`) are declared.

## Tests

- `backend/tests/draft_room/test_re_disposition_noop.py` (frozen checks C1-C5),
  `test_n06a_doc_owner_map_truth.py` (C7-C9, C14), and
  `test_n06b_status_transition_map.py` (C10-C12) ship with the frozen set;
  `test_n06c_disposition_contracts.py` adds the store-level read-discipline pin,
  refusal-code/cross-status negatives, and the SPEC structural pins;
  `test_n06d_guard_behavior_pins.py` adds the behavioral negative pins described above.
