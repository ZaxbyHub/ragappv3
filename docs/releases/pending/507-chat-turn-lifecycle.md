# Pending release note: durable, ordered, recoverable chat turns (issues #507, #553)

## What changed

Chat conversations are now durable across sending, stopping, retrying, editing,
switching, reloading and forking. An empty or interrupted response is never
presented or saved as a successful answer, and every turn is persisted as one
ordered, all-or-nothing write.

**Issue #553 (workstream H, PR 2 of 4):** the server is now the durable writer
of record for chat turns, not just the client. `POST /chat/stream` accepts an
optional `session_id` + `turn_id` (client-generated, ≤64 chars) pair; when both
are present the server pre-writes the user row with `status = "pending"` BEFORE
the first token streams and finalizes the assistant row at stream end
(`complete` on a full stream, `interrupted` on a dropped connection with
partial content, `failed` after a mid-stream error with partial content — a
pre-content failure still persists no assistant row). A proxy drop, tab close,
browser crash, or backend kill therefore never loses the question or the
partial answer from server history, and a second device reading
`GET /chat/sessions/{id}` sees the same transcript as the sending browser. The
client's batch save (including the `pagehide` keepalive path — it uses the same
batch endpoint) becomes an idempotent reconcile keyed on `turn_id`: a batch
save for a turn the server already wrote UPDATES the existing rows instead of
inserting duplicates, so a retried or double-delivered save cannot duplicate a
turn. Clients that do not send the new fields are unaffected (no server-side
write, exactly the previous behavior); a new frontend against a pre-#553
backend is also unaffected (unknown body fields are ignored). Mid-stream
replay/resumable SSE remains out of scope (workstream H PR 4, #555).

## Operator / migration notes

- **Schema (additive, automatic):** `chat_messages` gains `seq` (per-session
  message order), `turn_id` (turn linkage), `status`
  (`pending|complete|partial|interrupted|failed` — `pending` is new in #553
  and marks a turn the server pre-wrote but never finalized, e.g. after a
  hard crash; `partial` remains reserved for server-side partial synthesis and
  no writer emits it), `citation_confidence` and `unverifiable_claims`.
  `run_migrations` adds the columns on the next startup
  and backfills `seq` for existing rows in one `BEGIN IMMEDIATE` transaction —
  no manual step, no downtime. Concurrent writers block for the duration of
  that one-time backfill (seconds on typical databases; proportionally longer
  on very large `chat_messages` tables).
- **Turn uniqueness index (#553, additive, automatic):** a partial unique
  index `idx_chat_messages_session_turn_role ON chat_messages(session_id,
  turn_id, role) WHERE turn_id IS NOT NULL` enforces one user row and one
  assistant row per turn per session — the storage-level backstop that makes
  the reconcile idempotent. A turn legitimately has TWO rows sharing one
  `turn_id` (user + assistant), so the constraint is role-aware; legacy rows
  with NULL `turn_id` are excluded by the partial index and never conflict,
  and concurrent NULL-turn-id batch saves are unaffected. Before creating the
  index (one time, on the first startup that sees it absent), the migration
  collapses any pre-existing duplicate `(session_id, turn_id, role)` groups —
  exactly the double-save defect this change closes — keeping the most
  informative survivor per group: status `complete` first, then
  `interrupted`/`failed`, then `pending`/NULL; ties broken by longest content,
  then highest id. Worst case: when same-rank duplicates carry genuinely
  different content, the shorter variant is deleted and is unrecoverable from
  the server; the recovery source is the client transcript that produced the
  turn. The scan and delete run exactly once per database (later connects only
  run the idempotent `CREATE ... IF NOT EXISTS`).
- **`add_message` (single-message endpoint) is unchanged and intentionally has
  no reconcile:** the only supported caller that sends a `turn_id` there is
  the batch endpoint's 404 fallback, which fires only against a pre-batch
  backend (which also lacks this index). With the index in place, any
  hypothetical duplicate single-message insert fails loudly (IntegrityError)
  instead of silently duplicating a turn.
- **New endpoints (additive):** `POST /chat/sessions/{id}/messages/batch`
  (atomic multi-message save in payload order) and
  `POST /chat/sessions/{id}/truncate` (persistently trim history above a
  durable `seq` boundary — the server-side operation behind retry/edit). The
  truncate boundary is `keep_seq` (highest server-issued seq to KEEP, so the
  boundary stays exact even when an older client left a local turn unsaved);
  the legacy
  positional `keep_count` is still accepted for
  compatibility, and requests supplying neither are rejected with 422.
  Existing single-message `POST …/messages` is unchanged and also accepts the
  new optional fields, so older clients keep working; a new frontend hitting a
  pre-batch backend during a rolling restart falls back to sequential
  single-message saves (and truncate failures degrade to a visible toast).
  Client-supplied `turn_id` (≤64 chars) and `unverifiable_claims` (≤50 items)
  are size-bounded; `content` and `citation_confidence` deliberately follow
  the pre-existing unbounded `sources`/`memories` pattern and are not
  tightened in this change.
- **Behavior changes visible to users:**
  - Slow generations (>15 s silences) now keep streaming instead of being
    silently truncated by the proxy keepalive heartbeat (CHAT-002).
  - A dropped/truncated stream surfaces as an *Interrupted* state with a Retry
    affordance, keeping the partial answer and original input; the partial turn
    is saved with status `interrupted`, never as success (CHAT-004, LIVE-01).
  - Pressing Stop now durably saves the user question and any partial answer
    as one interrupted batch; stopping before the first token saves only the
    question. A `pagehide` uses the same batch payload through Fetch keepalive
    as a best-effort final save.
  - Browser keepalive payloads remain subject to browser size limits, and a
    hard process termination that does not deliver `pagehide` cannot be saved.
  - A mid-stream server failure keeps the question and partial answer
    durably with status `failed` (retryable after reload) instead of losing
    the exchange; a pre-content failure still persists nothing (LIVE-01).
  - Retry/edit/fork now wait for any in-flight turn save to settle before
    revising history, and the retry/edit trim is anchored on the server's own
    `seq` values — retrying after a Stop or an empty response can no longer
    duplicate the Q&A pair after a reload.
  - Retry/edit now trims the server-side history too, so a reload or fork no
    longer resurrects the replaced tail (CHAT-006).
  - Question and answer can no longer swap order after a reload (CHAT-005).
  - Forks keep KMS citation cards and the instant/thinking mode badge
    (UI-039), and citation-confidence/unverifiable-claims assessments now
    survive reload and fork (DEEP-D-01).
  - A failed save shows a visible failed state instead of silently losing the
    exchange (UI-002); an empty model response shows a retryable error instead
    of an empty success bubble.
  - Session UX fixes: late transcript fetches can no longer overwrite a newer
    selection (UI-001); clearing session search keeps working on repeat (UI-043);
    deleting the active session releases it (UI-044); arrow/Home/End keyboard
    navigation works in the live session list (UI-045); the mobile sessions
    sheet closes when the viewport grows to desktop (UI-037); the slash-commands
    button opens the menu (UI-040); `Shift + ?` opens keyboard shortcuts (UI-048);
    a stale failed vote can no longer clear a newer saved vote (UI-050).

## Rollback

Revert to the previous deploy; no data migration must be undone. Rows written
by this build (with `seq`/`status`, including #553's `pending` user rows)
remain readable by the old build, which orders by timestamp; the #553
uniqueness index and the reconcile are ignored by the old build, and the
client's batch save degrades to being the sole writer again exactly as before.
