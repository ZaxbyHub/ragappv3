# Quality-gate truthfulness: audit-skill mirror parity, DraftClaimSource nullability gate, Draft Room 500-detail hygiene (#914, #912, #917)

## Local CI mirror parity (#914)

- The `ci-compatibility-audit` skill's Local Mirror Commands and its
  "Repository contract job" prose now enumerate all 18 Quality-contracts
  scripts (the mirror ran 7; 11 contract scripts — settings consumers, batch
  mock seq, a04, b03, both l05s, l07, all three l09s, and n05 — were silently
  missing, so a local pre-push audit under-ran CI). The Local Mirror section
  now names the `quality-contracts` job as its parity authority and the
  enforcing test.
- New `backend/tests/test_ci_compatibility_audit_mirror.py`: bidirectional
  parity in both surfaces (the fenced command list AND the prose list) against
  the ci.yml Quality-contracts job — adding a contract script to ci.yml (or
  removing one) fails the suite until the skill matches, comment-only mentions
  do not count, and stale extra entries fail the same way.

## DraftClaimSource nullability parity (#912)

- `scripts/check_n05_draftclaim_nullable.py` now gives `DraftClaimSource` the
  same bidirectional parity contract `DraftClaim` already had: NON-NULL
  (backend Optional, TS non-null), OVER-NULL (reverse drift), UNMIRRORED
  (field on one side), and structural fail-closed (exit 3) when either the
  backend class or the TS interface disappears. This closes the residual the
  maintainer's scope-narrowing comment on #912 registered; the other 18
  hand-mirrored interface pairs are enumerated as recorded re-evaluation
  triggers (not gated — outside the claim-family contract).

## Draft Room 500-detail hygiene (#917)

- `scripts/check_a04_http500_detail_hygiene.py` now audits
  `backend/app/api/routes/draft_room.py` and reads `DraftRoomHTTPError`'s
  positional `(status_code, detail, code)` signature: a literal status >= 500
  whose detail is an f-string interpolation or `str(...)` fails the gate with
  the file and line. The existing `HTTPException` coverage of
  memories/vaults/admin is intact (and now strictly stricter — the widened
  predicate also reads positional args and >= 500 statuses). A status
  computed at runtime is outside the gate's literal-status detection class;
  the two Draft Room mapper arms below are pinned by
  `backend/tests/test_a04_draftroom_map_helpers.py` instead.
- Defensive fix for two latent echo paths the localization probe found:
  `_map_store_error` and `_map_storage_error` no longer put `str(exc)` in the
  client-visible detail on their unmapped-code (status 500) fallback arms —
  the client now sees a generic detail ("draft store operation failed" /
  "draft input storage failed") while the registered exception handler's
  >= 500 branch continues to log the cause server-side (every helper result
  is raised `from exc`). No application code raises the base
  `DraftStoreError`/`DraftInputStorageError` (the only unmapped subclass,
  `_LeaseLostError`, is raised and caught on worker paths), so these arms are
  not reachable from a route today; the change hardens the fallback contract.
  Mapped 4xx arms keep their crafted `str(exc)` messages. API clients that
  matched on the previous raw 500 detail text will see the new generic
  strings.
