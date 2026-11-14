# Draft Room disposition integrity, transition-map guard, doc truth (#901, #920, #927)

## Backend

- **Re-disposition is refused (issue #901 AC1).** Dismissing or waiving a finding that
  is no longer `open` now returns 409 `finding_not_open` with `drafts.lock_version`
  unchanged. Previously the same-target short-circuit in `_check_transition` let a
  second dismiss/waive through as a 200 "no-op" that still wrote a second audit row and
  bumped the draft's lock version. The guard lives at the store seam
  (`DraftStore.dismiss_finding`/`waive_finding`), extending the apply path's reference
  pattern so every caller inherits it.
- **Storage faults are no longer client conflicts (issue #901 AC4).** A retryable
  storage fault raised inside a disposition transaction (`sqlite3.OperationalError`,
  e.g. lock contention after the busy timeout) now surfaces as a server-side error
  instead of HTTP 409 `conflict`. Only the constraint-violation subclass
  (`sqlite3.IntegrityError`) keeps the 409 mapping. The frontend no longer renders lock contention
  as a reload-conflict banner.
- **Disposition eligibility uses the draft's current revision (issue #901 AC2).** The
  dismiss/waive response's `can_apply` is computed against the same
  `get_current_revision` input the findings list uses, instead of the dispositioned
  finding's own revision id. Apply keeps using the revision it just created.
- **Findings list batches its reads (issue #901 AC3).** GET `/drafts/{id}/findings`
  now performs two store hops (authorization, then one post-authorization pass) instead
  of four, reading the `drafts` row exactly once per request. Authorization order is
  unchanged (ownership, then vault read, then child reads).
- **One shared disposition finish path (issue #901 AC5).** `waive_finding` routes
  through `_complete_finding_disposition` (extended with the waiver columns) instead of
  a hand-maintained copy of the four-step transaction; exactly one inline
  finding-status writer remains.
- **Status-transition map guard (issue #927).** New
  `backend/tests/draft_room/test_n06b_status_transition_map.py`: every status write in
  the three declared co-owner modules must resolve to a DraftStore transition call, a
  named mirrored prior-status allowlist, a located inline guard, or the documented
  settle-pair chain — resolved table-specifically against the store's four transition
  maps, with same-target and composite paths justified structurally. Unregistered
  literals and unconstrained parameterized writes fail closed, naming file:line and the
  status found. This closes the gap between #790's constraint ("the census enforces the
  `_check_transition` discipline is not further eroded") and the ownership-only census.

## Documentation

- **SPEC 4.1 module table (issue #920).** The `draft_store.py` row no longer claims
  "All SQLite CRUD"; it names the three declared co-owners and links the owner map.
- **SPEC 10.3 (issues #920/#927).** The "no code may write an arbitrary status string"
  invariant is scoped to the owner map: store-routed changes go through the transition
  tables; co-owners keep mirrored prior-status allowlists pinned by the new guard.
- **`draft_job_processor.py` docstring (issue #920).** Claim mediation is explicitly
  scoped to claims; the worker's direct co-owner writes (`drafts.status`,
  `draft_jobs`, `draft_inputs.parse_status`) are declared.

## Tests

- `backend/tests/draft_room/test_re_disposition_noop.py` (frozen checks C1-C5,
  arm's-length authored) and `backend/tests/draft_room/test_n06a_doc_owner_map_truth.py`
  (C7-C9, C14) ship with this change; `test_n06b_status_transition_map.py` provides the
  transition-map guard (C10-C12).
