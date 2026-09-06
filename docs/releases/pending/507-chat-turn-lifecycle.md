# Pending release note: durable, ordered, recoverable chat turns (issue #507)

## What changed

Chat conversations are now durable across sending, stopping, retrying, editing,
switching, reloading and forking. An empty or interrupted response is never
presented or saved as a successful answer, and every turn is persisted as one
ordered, all-or-nothing write.

## Operator / migration notes

- **Schema (additive, automatic):** `chat_messages` gains `seq` (per-session
  message order), `turn_id` (turn linkage), `status`
  (`complete|partial|interrupted|failed`), `citation_confidence` and
  `unverifiable_claims`. `run_migrations` adds the columns on the next startup
  and backfills `seq` for existing rows in one `BEGIN IMMEDIATE` transaction —
  no manual step, no downtime. Concurrent writers block for the duration of
  that one-time backfill (seconds on typical databases; proportionally longer
  on very large `chat_messages` tables). `status = "partial"` is reserved for
  workstream PR 2/3 (server-side partial synthesis); no current writer emits
  it. Rollback to the previous build is safe: the new columns are simply
  ignored, and existing chats remain readable.
- **New endpoints (additive):** `POST /chat/sessions/{id}/messages/batch`
  (atomic multi-message save in payload order) and
  `POST /chat/sessions/{id}/truncate` (persistently trim history above a
  durable `seq` boundary — the server-side operation behind retry/edit). The
  truncate boundary is `keep_seq` (highest server-issued seq to KEEP, so the
  boundary stays exact even when local rows were never persisted, e.g. after
  Stop); the legacy positional `keep_count` is still accepted for
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
by this build (with `seq`/`status`) remain readable by the old build, which
orders by timestamp.
