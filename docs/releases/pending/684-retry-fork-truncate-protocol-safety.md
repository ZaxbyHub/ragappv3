# Retry, Fork and truncate stay server-anchored and single-fire: stale views refused, originals durable, guards synchronous (Issue #684)

## What changed

- **Truncate carries a tail precondition** (`backend/app/api/routes/chat.py`,
  `frontend/src/lib/api/sessions.ts`, `TranscriptPane.tsx`) —
  `TruncateSessionRequest` gains optional `expected_tail_seq` AND
  `expected_tail_id`; when present, the route refuses the request with 409
  unless the session's current `MAX(seq)` / tail-row PRIMARY KEY equal them
  (either mismatch direction: a stale second tab must never delete rows it
  never saw, and a client-ahead view after another writer truncated must not
  delete rows landed after that). The id is the authoritative half: per-
  session seq values are `MAX(seq)+1` and are REUSED after another writer
  truncates and resaves the same row count, so a seq-only precondition can
  pass on a coincidental equal tail (ABA) — the tail row's AUTOINCREMENT
  primary key is never reused. The DELETE itself becomes an atomic
  equality-guarded statement, and a zero rowcount on the `boundary < expected`
  arm (rows provably existed above the boundary at the pre-check) rolls back
  with the same 409 — a writer landing between the pre-check and the delete
  is refused, never silently applied. Retry and Edit send the client's
  observed tail (`observedTail`, the max-seq row's seq + server id); a 409
  triggers a store-level session reload (`reloadAfterStaleRefusal`, guarded
  against concurrent session switches) so the tab converges on the rows it
  had not seen. Legacy requests without these fields keep byte-identical
  semantics (no-op-at-boundary remains a success, pinned by
  `test_truncate_noop_at_boundary_with_expected_tail_succeeds`).
- **A rejected retry no longer destroys the original Q&A** (`useSendMessage.ts`,
  `chat.py`, `sessions.ts`) — Retry snapshots the rows the revision removes
  and passes them to the resend; when the replacement stream is
  admission-rejected before any content — the one terminal outcome where the
  server provably wrote nothing for the replacement (the route-level CHAT
  gate returns before the durable user-row pre-write) — the client restores
  the original turn server-side (batch re-save, ids/seqs remapped through
  `migrateId`) and locally. The SSE parser now carries the error frame's
  `code` onto the Error (it previously dropped it), and the route relabels
  engine-forwarded admission rejections to `ADMISSION_REJECTED_PREWRITTEN`
  when the user row was already pre-written, so a restore can never append
  the original after the pre-written replacement question (pinned both ways
  by `test_engine_admission_rejection_relabelled_after_prewrite`). Other
  pre-content failures keep today's behavior (a partial-content turn is
  already durable; post-pre-write rejections have a durable user row).
- **Retry/Edit/Fork are single-fire** (`TranscriptPane.tsx`) — a synchronous
  `revisionInFlightRef` set before the handler's first `await` (and Fork's
  existing ref moved before its pending-save await), each wrapped in a
  full-scope `finally` so no early return strands the guard: a double-click
  issues at most one truncate/fork server call.
- **Revisions wait for EVERY in-flight turn save** (`useChatStore.ts`,
  `useSendMessage.ts`) — `pendingTurnPersist` keeps its single-slot
  semantics; a new `pendingTurnPersists` registry tracks all unsettled saves
  and `awaitPendingPersist` drains it (defensively, so partial store mocks
  degrade to the old behavior). A Retry can no longer truncate while an
  older turn's save is still in flight.
- **Fork anchors on the durable seq** (`chat.py`, `sessions.ts`,
  `TranscriptPane.tsx`) — `ForkSessionRequest.message_index` becomes optional
  and a new `through_seq` (exactly one of the two required; `ge=1`) copies
  exactly the rows with `seq <= through_seq` (SQL-side cutoff applied to the
  main and all three positional side-fetch SELECTs so mode/kms/turn fields
  stay aligned; `fork_message_index` records the effective cutoff). The
  client sends the highest durable seq up to and including the clicked
  message; an anchor that selects zero rows is a 400, and a zero-durable
  transcript refuses locally with a warning and no server call. Legacy
  `message_index` forks keep their exact slice and out-of-bounds 400.

## Why

The frontier audit (run 20260923T174456Z) traced five findings to one class:
the client derived destructive single-shot server calls from a local view
that the server never re-validated, guards were set after an `await`, and
multi-step revisions committed their destructive step first (T1-13-S-05 HIGH,
S-08, S-04, S2-06, K-02). Issue #684 froze 11 acceptance checks at the audit
base; all 11 pass at this change (7 discriminating RED→GREEN, 4 preserving).

## Residuals (disclosed)

- Power loss between a truncate commit and the replacement's durable persist
  still loses that turn's original Q&A (bounded by the replacement's
  generation time); full closure is the one-server-operation regenerate,
  which remains future work.
- A failed restore batch-save (network death mid-retry) leaves the original
  absent — surfaced by an error toast.
- Non-admission empty failures restore nothing: post-admission failures may
  have a pre-written replacement user row, and appending the original after
  it would duplicate the question. A session switch mid-retry aborts the
  stream before the restore gate, but a switch landing inside the restore's
  own batch-save window still completes the server-side restore into the
  ORIGINAL session (correct) while the local mirror may append the old
  session's rows to the switched-in transcript; that local pollution
  self-heals on the next load of the session.
- A 409 stale-view reload resets the same session's ephemeral edit-version
  stepper state (`messageEditVersions`/`activeEditVersion`); the snapshots
  are deliberately not preserved across the reload because the inserted
  unseen rows shift transcript indices, so old slot keys would point at the
  wrong messages.
- Edit's truncate→composer window (replacement durable only when re-sent) is
  out of scope per the issue (T1-13-S-08 names Retry; the composer content
  survives locally).
