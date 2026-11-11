# fix(draft): capability honesty, retry idempotency keys, cancellation-safe upload commits, and orphan-input reaping (#902, #905, #911, #913)

## Worker liveness is task liveness, not object presence (#902, #911)

- `DraftJobProcessor` exposes a read-only `is_alive` (running + poll task
  present + not done) and attaches a done-callback to the poll task that logs
  a poll-loop death after a successful start (exception class only — the loop
  already survives every ordinary exception by design).
- `BackgroundProcessor` exposes a read-only `is_alive` (`_running` AND at
  least one live ingestion worker task). This catches the shape where
  lifespan's `_safe_await` swallows a failed/timed-out `start()` and leaves
  the object mounted with `_running=False` while `enqueue` still accepts work
  nobody will consume.
- `GET /api/draft-room/capabilities` derives `compile_available`,
  `upload_available` (new field; parse and compile share one processor), and
  `promote_available` from `_worker_available()`: presence remains the
  baseline (the #789 bare-`object()` guardrail), AND-ed with `is_alive` when
  the worker exposes it. A mounted-but-not-running or dead-poll-loop worker
  reports false.
- The frontend upload CTA gates on `upload_available` (edit/delete of stored
  sources stay on the `enabled` flag alone — metadata edits enqueue nothing).

## Enqueue routes refuse work no live worker can claim (#911, #902)

- `POST .../compile`, `POST .../jobs/{id}/retry` (both the compile and the
  parse branch), `POST .../inputs`, and `POST .../promote` answer
  `503 worker_unavailable` when `_worker_available` is false. Placement:
  AFTER authentication/ownership/enabled checks and idempotency replay
  resolution (a genuine `Idempotency-Key` replay still returns its original
  job even while the worker is down) and BEFORE the first mutation — a
  refusal leaves Ready state, events, rows and bytes exactly as they were.
  This is SPEC §8.3's unavailable-before-enqueue class; behavior change for
  raw API clients (previously 202-and-never-claimed for compile/retry/upload,
  500 `promotion_failed_incomplete_cleanup` for promote with a None
  processor, or 202 for a mounted-but-stopped one).
- On the frontend, a `worker_unavailable` rejection from compile, retry,
  upload, promote, or the ActivityTray retry invalidates the Draft Room
  capabilities query immediately, so an open session reflects the loss
  without waiting out the 5-minute stale window. Other 503 codes (e.g.
  `model_unavailable`) do not trigger it.

## Retry Idempotency-Key bound to retry identity (#905)

- `retryDraftJob(draftId, jobId, data, idempotencyKey?)` forwards the header
  (same shape as compile; compile retries only — parse retries keep their
  base behavior, review F-04). DraftWorkspace and ActivityTray mint one key per
  retry IDENTITY (draft + parent job + requested stage): an accidental
  double-submit — including a second click after the first attempt settled
  but before the job list refreshed — replays the SAME key and gets the
  original job back; a genuinely new attempt (different stage, or the failed
  child as the new parent) rotates the key.

## Cancellation-atomic upload commit (#913)

- After the input row is reserved, finalize → enqueue runs as ONE detached
  commit task owning a POOL connection for its DB steps (never the request's
  `DraftStore` — the request dependency releases its connection at teardown,
  and this continuation may legitimately outlive the request).
- The request awaits it via `asyncio.wait_for(asyncio.shield(task),
  _UPLOAD_COMMIT_DRAIN_SECONDS)`; on cancellation it drains the task under
  ONE monotonic deadline (anchored at the first cancellation; repeated
  cancels are absorbed and counted, never restarting the clock) inside an
  anyio shielded scope, then re-raises — the upload is never left
  half-committed (row+bytes+job all present or all absent) and shutdown still
  unwinds: a cancellation delivered at the FINALIZE await also runs the
  staged discard + compensation before propagating (review BOT-01), and
  lifespan drains in-flight commit tasks before closing the pool (review
  PRR-020). On drain timeout of a LIVE (uncancelled) request the detached
  task remains the sole owner of the outcome and the request answers 503
  `commit_pending`; a cancelled request is drained under the same budget and
  the cancellation re-raises (no response).
- Compensation (unlink + row delete) runs shielded with a 30s bound, catches
  `CancelledError` per step separately from `Exception`, records it, runs
  both steps anyway, then re-raises — a native cancel during the unlink await
  can no longer skip the row rollback, and the cancellation is never
  swallowed.
- In-flight commit tasks are strong-referenced in `_INFLIGHT_UPLOAD_COMMITS`;
  a done-callback detaches the reference and consumes/logs the outcome
  (draft id, input id, exception class only), and lifespan drains the set
  (bounded) before closing the pool at shutdown (review PRR-020).
- The startup orphan-input sweep is wall-clock bounded
  (`_ORPHAN_SWEEP_BUDGET_SECONDS`); on overrun it logs and defers the
  remainder to the next restart (review PRR-021).

## Orphan finalized-input reaping (#913)

- `DraftInputStorage.reconcile` gains `_reconcile_orphan_inputs`: on the
  production call shape (DB-derived `valid_input_relpaths`), row-less
  finalized files of the exact `inputs/<input_id>/<name>` shape inside LIVE
  project directories are removed (counted under `orphan_files_removed`,
  WARNING-logged). Identity compares `os.path.normcase`-normalized
  forward-slash relpaths (case-folds on Windows, distinct on POSIX);
  symlinks/reparse points are skipped, never followed; the legacy pairs-only
  shape removes nothing. Runs at startup before HTTP/workers in the shipped
  single-process deployment.

## Content-title filenames keep pre-separator text (#902)

- New shared `fold_title_separators` in `upload_validation` rewrites `/` and
  `\` to `_` before `secure_filename`; `canvas.py` (canvas export names) and
  `draft_promotion.py` (promoted document stems) adopt it, and
  `_export_filename` now reuses it. Upload FILENAME callers keep raw
  `secure_filename` semantics (a client path in an upload filename is
  hostile, not content). `A/B notes` exports as `A_B_notes…`; `Q3/Q4 plan`
  promotes as `Q3_Q4_plan…`.

## Follow-ups

- Filed for the residuals named by the #907/#789 review lineage: none new;
  #911/#913's remaining census items are dispositioned in the trace's
  recurrence sweep (P1-P8), with the ownership invariant (detached work never
  captures request-scoped resources past dependency teardown) added to the
  Phase 4.2 guardrail.
