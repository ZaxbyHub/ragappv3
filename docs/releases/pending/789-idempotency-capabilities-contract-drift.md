# fix(draft): compile idempotency and upload rollback behave as documented, worker-liveness capability is real, and route errors/codes/exports/types stop drifting

Issue #789 (Workstream N PR 5 of 6). Audit findings T1-08-K-09, T1-08-S2-04,
T1-08-K-06, T1-08-S2-08, T1-08-K-03, T1-08-K-07, T1-08-K-05, T1-08-K-11.

## What changed

**Idempotency fingerprints hash the client's request (draft_room.py)**
- The `Idempotency-Key` request fingerprint now folds in the client's
  *requested* `start_stage` (compile: `body.start_stage`; retry: the
  route-resolved requested stage) instead of the server-normalized one. A
  fresh compile always normalizes back to `research`, so hashing the
  normalized value aliased every requested stage under one key: two fresh
  compiles reusing a key with different requested stages both returned 202;
  the second now correctly returns 409 (SPEC 8.2).
- A retry's fingerprint folds in the parent job's frozen `input_revision_id`
  instead of the live current revision. The retried job itself changes the
  current revision on success, which used to make an identical replay 409
  (`idempotency_key_conflict`) instead of returning the original job; a
  genuine replay now returns the original job at any point in its lifecycle.
- The *compile* fingerprint (checkpoint-resume identity) still hashes the
  normalized stage — the two fingerprints answer different questions and
  `TestCompileFingerprintParity` still pins the pipeline parity.
- The enqueue `IntegrityError` race handler now distinguishes which unique
  index lost the race (key index vs one-active-compile, discriminated by the
  constraint's column/index identity in the sqlite message): the key race
  answers `idempotency_key_conflict` — or returns the winner when a racing
  identical replay's fingerprint matches — instead of both races sharing
  `active_job`.

**Upload-enqueue compensation is guarded on every branch (draft_room.py)**
- The `DraftRoomHTTPError` branch of upload enqueue now routes through
  `_compensate_enqueue_phase()` like the generic branch: a failure inside the
  compensation (e.g. an `OSError` from `unlink` under a Windows sharing
  violation) can no longer skip the input-row rollback nor replace the domain
  error the client sees.

**`compile_available` reflects real worker liveness (draft_room.py)**
- `GET /api/draft-room/capabilities` derives `compile_available` from
  `app.state.draft_job_processor` being present instead of hard-coding `True`;
  startup that failed to start the worker now honestly reports compile as
  unavailable. The frontend already disabled the compile/retry CTAs on this
  flag. Disclosed residual: a raw API client bypassing capabilities can still
  202 a compile onto a dead worker (changing that would alter what the route
  accepts, which this PR forbids).

**Promotion 500s are observable and leak nothing (draft_room.py)**
- The generic promotion-failure branch logs at ERROR with the exception
  (`logger.exception`) before raising 500 `internal_error`.
- The cleanup-incomplete sub-branch answers with a fixed detail ("promotion
  failed and cleanup could not be verified") instead of echoing the raw
  wrapped exception text to the client; the raw text stays in the server log
  and the existing `draft_promotion_cleanup_failed` audit event.
- `draft_room_exception_handler` logs every 5xx `DraftRoomHTTPError` with its
  cause and target before rendering (defense in depth for future 500 paths).

**One stale-lock code everywhere (draft_room.py / draft_store.py / docs)**
- Every stale-`lock_version` raise now emits the precise code
  `stale_lock_version` (compile enqueue, apply-finding, mark-ready, the
  store's `_locked_draft` covering update/archive/restore/manual-revision)
  matching the disposition route's pre-existing code, instead of those paths
  returning the generic `conflict` for the same condition. Status stays 409;
  `docs/draft-room.md`'s operator table gains the row.

**Export filenames keep title text (draft_room.py)**
- `_export_filename` rewrites `/` and `\` to underscores *before* the
  filename sanitizer runs, so a title like `A/B test results` exports as
  `A_B_test_results-rev1-REVIEW.md` instead of silently losing everything
  before the separator. The Content-Disposition header additionally carries
  an RFC 6266 `filename*` UTF-8 parameter with the raw (percent-encoded)
  title, so non-ASCII titles survive for modern clients; the ASCII filename
  parameter still ASCII-folds them.

**DraftClaim nullability parity (frontend)**
- `DraftClaim.claim_sha256`, `span_start`, and `span_end` are now
  `string | null` / `number | null`, matching the backend's documented
  NULL-when-unverifiable emission (#517 / DRAFT-010). No frontend reader of
  these fields exists, so no null-guards were needed.
- `scripts/check_n05_draftclaim_nullable.py` (new, wired into the CI
  Quality-contracts job) fails if the backend `Optional` claim fields and the
  TS interface drift apart again.

## Why

All eight findings share one theme: routes trusting server-derived or
hard-coded state where the documented contract names the client's actual
request or observed reality — a fingerprint hashed over a constant, a
fingerprint hashed over a value the fingerprinted job itself mutates, an
unguarded compensation step, a capability flag that ignores the worker,
silent 500s, one condition under two codes, a *title* fed to a *path*
sanitizer, and a TS interface contradicting the emitting model.

## Migration

No schema migration. Request fingerprints computed by earlier versions no
longer match, so an `Idempotency-Key` enqueued before this upgrade and
replayed after it returns 409 `idempotency_key_conflict` instead of the
original job — keys are scoped to a single request attempt and the frontend
generates a fresh key per dialog, so no supported flow crosses the upgrade.
The stale-lock error *code* changes from `conflict` to `stale_lock_version`
on the compile/apply-finding/mark-ready/update/archive/restore/manual-revision
paths (status and semantics unchanged; clients matching the precise new code
gain a distinguishing signal, clients matching `conflict` should treat any
409 as "reload and retry" per the documented guidance). The promotion
cleanup-incomplete response *detail* text is now fixed; its code is
unchanged.

## Caveats

- A raw API client can still enqueue a compile while the worker is down (see
  above); the honest capability flag is the in-scope remedy, the enqueue-path
  503 would change acceptance behavior.
- `retryDraftJob` on the frontend still sends no `Idempotency-Key`
  (pre-existing; the backend replay contract is now correct and pinned
  regardless) — declared out of scope by the issue; tracked as #905.
- The ASCII `filename` parameter still ASCII-folds non-ASCII titles by
  design; the additive `filename*` parameter carries the full title.
