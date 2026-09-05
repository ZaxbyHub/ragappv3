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
  and backfills `seq` for existing rows in one transaction — no manual step,
  no downtime. Rollback to the previous build is safe: the new columns are
  simply ignored, and existing chats remain readable.
- **New endpoints (additive):** `POST /chat/sessions/{id}/messages/batch`
  (atomic multi-message save in payload order) and
  `POST /chat/sessions/{id}/truncate` (persistently trim history after N
  messages — the server-side operation behind retry/edit). Existing
  single-message `POST …/messages` is unchanged and also accepts the new
  optional fields, so older clients keep working.
- **Behavior changes visible to users:**
  - Slow generations (>15 s silences) now keep streaming instead of being
    silently truncated by the proxy keepalive heartbeat (CHAT-002).
  - A dropped/truncated stream surfaces as an *Interrupted* state with a Retry
    affordance, keeping the partial answer and original input; the partial turn
    is saved with status `interrupted`, never as success (CHAT-004, LIVE-01).
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
