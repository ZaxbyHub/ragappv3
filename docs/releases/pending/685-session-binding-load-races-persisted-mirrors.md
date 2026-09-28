---
issue: 685
title: Session binding, load races and persisted mirrors stay robust
---

# Session binding, load races and persisted mirrors stay robust

Workstream A PR 3 of 7 (audit remediation, frontier audit 2026-09-23).

## What changed

- **First send binds the whole session identity** (#685 / T1-13-S2-02). A
  session created by the first message from `/chat` now updates the shell's
  `activeSessionId` and replaces the URL with `/chat/<id>` in the same step
  (the chat store was already updated). Previously only `activeChatId` was
  set, so the session rail, the URL and the store disagreed until the next
  navigation, and feedback/delete guards keyed on the shell id missed.
- **Superseded session loads can never overwrite the shown session**
  (T1-13-S-02). The load effect's staleness token is now bumped on *every*
  re-run — including the early returns for "no session in the URL" and
  "session already active in the store" — so navigating 1 → 2 → back to 1
  (or to New chat) while session 2's fetch is pending discards that fetch
  when it lands. Previously only a newer *fetch* invalidated an older one.
  Navigating to `/chat` also clears the chat store, so the URL, shell id and
  store always name one session.
- **A failed session load is visible, retryable, and never retargets sends**
  (T1-13-S-03). A rejected load now shows an alert banner above the
  transcript with a Retry button (re-fetches the session). The previously
  active session is cleared, so a message typed after the failure starts a
  fresh session instead of silently streaming into the session the URL no
  longer names. A superseded *failure* is discarded just like a superseded
  success.
- **Stepping edit versions is display-only** (TQ-sibling-batch-03-06). The
  version stepper moves a display pointer; the message's stored content
  always holds the live (latest) text. Follow-up sends, retry, continue and
  suggestions serialize the live content as LLM history — stepping back to
  an older version no longer ships the old text to the model. The displayed
  version also stays stable while a follow-up streams, and editing from a
  displayed old version still starts the edit from the displayed text.
- **The feedback mirror is guarded and bounded** (T1-13-K-07 /
  T1-13-S-07). `chat_feedback_*` localStorage votes go through a shared
  guarded helper (`lib/chatFeedbackStorage`) that keeps at most 1000 keys
  (oldest removed first), and the id-migration read in the save-success path
  is wrapped so a storage exception can never mark an already-durable save
  as failed.
- **Pinned sessions sync across tabs** (T1-13-S2-11). Toggling a pin
  re-reads localStorage first, and a `storage` event from another tab
  replaces the in-memory list (including removals and clears of the key), so
  one tab's pins are no longer silently overwritten by another tab's stale
  copy.

## Behavior notes

- Navigating to the New chat surface (`/chat`) while an answer is streaming
  now aborts that stream and clears the transcript, matching the session
  rail's New chat button. Nothing is written client-side for that in-flight
  turn; the server's detached producer keeps generating after the client
  abort and later saves the answer as a normal completed turn, so it
  reappears when the session is reopened (external review F-001). This is
  disclosed deliberately: previously the URL, the shell and the store could
  keep pointing at different sessions after this navigation.
- A failed feedback-mirror migration (storage unavailable) no longer marks a
  saved exchange as failed; the vote may remain under its old key until the
  1000-key prune or a re-vote rewrites it.

## Verification

Twelve frozen acceptance checks (9 discriminating, 3 preserving) plus new
behavior tests for retry-click re-fetch, superseded-rejection discard,
cross-tab pin store sync, and edit-seed lineage; full frontend suites,
typecheck, lint and build run locally against CI parity.
