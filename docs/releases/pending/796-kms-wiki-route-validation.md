# fix(api): KMS entry creation stops reporting server errors as 400s; wiki route models stop accepting unbounded or out-of-vocabulary values (#796)

## What changed

Three low-severity validation/error-handling gaps in the wiki and KMS route
layer (audit findings TQ-sweep-B03-03 + R2-L3-C08, TQ-sweep-B07-02,
TQ-differential-d-05):

- **KMS entry creation classifies server errors as 5xx.** `create_kms_entry`
  wrapped `KMSStore.create_entry` in `except Exception as e:` and returned a
  400 with the RAW exception text — the sole caught-exception passthrough in
  the file. Internal failures (DB errors, programming errors) are now a 500
  with a fixed generic message; the raw text stays in the server log via
  `logger.exception`. (`create_entry` has no deliberate input-validation
  raises — slug uniqueness is resolved internally and status is validated
  before the try — so no legitimate client 400 was ever guarded by the broad
  except.)
- **Wiki Update models carry the Create-side bounds.**
  `WikiPageUpdateRequest.title` is bounded 1-500 and
  `WikiClaimUpdateRequest.claim_text` 1-2000, matching their Create
  counterparts and every other Create/Update pair in the routes (KMS,
  organizations). Update can no longer submit values create would reject.
- **Request vocabularies mirror the store's CHECK constraints.** New
  Literals (`WikiPageTypeName` 9 values, `WikiPageStatusName` 5,
  `WikiClaimStatusName` 6, `WikiClaimSourceTypeName` 5) replace plain `str`
  on the page/claim create+update models; `confidence` is bounded
  `ge=0, le=1` on all four. Out-of-vocabulary or out-of-range values now
  fail request validation (422, listing the allowed values) instead of
  reaching SQLite — which failed as a generic 400 on create and an
  UNHANDLED 500 on update (the update route catches only `ValueError`;
  `sqlite3.IntegrityError` is not one). Same class, same file, also typed:
  `PromoteMemoryRequest` (`page_type`/`status`) and the previously untyped
  `WikiBulkRequest.updates` (new `WikiPageBulkUpdateFields` model; the bulk
  route's absent-AND-empty `updates` → 400 shapes are preserved — the typed
  model is always truthy, so the guard is an explicit no-fields-set check).
  `LintFindingUpdateRequest.status` gains the 5-value CHECK vocabulary while
  the store's narrower actionable set (resolved/dismissed/acknowledged) and
  its 400 mapping are preserved — users still cannot SET open/auto_resolved
  (issue #791 contract). `claim_type` deliberately stays `str`:
  `wiki_claims` has no CHECK on it, so a Literal would invent a vocabulary
  narrower than the store.

## Why

Frontier-audit findings: an internal KMS failure reported as a client 400
with the raw error text leaked; update-side requests bypassed create-side
length bounds; and out-of-vocabulary `status`/`page_type` or out-of-range
`confidence` (7.5 persisted and rendered as 750%) reached the store, failing
inconsistently (400 on create, 500 on update).

## Migration / compatibility

No migration; no settings keys; no frontend change needed (the UI's
PAGE_TYPES/STATUSES match the CHECKs exactly; the TS request types are loose
strings that still type-check). Clients that sent values the store always
rejected now get 422 instead of 400/500. The KMS create success path is
unchanged.

## Known limitations / residuals

- **Legacy out-of-range `confidence` rows** (`wiki_pages`, `wiki_claims`,
  `wiki_claim_sources` — no DB CHECK ever bounded these columns): the wiki
  edit dialog always echoes `confidence` in its PUT, so editing ANY field of
  a page holding e.g. 7.5 now 422s until the row is repaired. No migration
  and no silent coercion ship here (rewriting stored values on edit would be
  worse); the API prevents new out-of-range rows, and existing rows can be
  repaired with
  `UPDATE wiki_pages SET confidence = MIN(MAX(confidence,0),1) WHERE confidence < 0 OR confidence > 1;`
  (same statement for `wiki_claims` and `wiki_claim_sources`).
  Flagged for maintainer review.
- The wiki create-page route's generic fixed-message 400 (the #792 form) is
  unchanged — this issue's claims targeted the KMS entry-create outlier.
- `claim_type` remains unbounded `str` (no store CHECK to mirror).
- The `payload_json` unguarded-parse sibling in the same cluster is tracked
  separately (#950 / Workstream O PR 2).
- The structural single-enum-module generation (CHECK vocabularies + Literals
  + TS unions from one source) was not undertaken; the battery's parity tests
  parse BOTH DDL occurrences of the tables in database.py and fail on drift.
