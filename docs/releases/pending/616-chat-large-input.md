# Large chat input: 100k inline cap, paste-to-attach, debounced bounded drafts (Issue #616)

## What changed

### The arbitrary 2000-character composer cap is gone
- `frontend/src/hooks/useSendMessage.ts` — `MAX_INPUT_LENGTH` raised from
  `2,000` to `100,000`. The old value contradicted no backend contract
  (chat `ChatMessage.content` is an unbounded `str` in
  `backend/app/api/routes/chat.py`) and blocked legitimate long-form RAG
  prompts; a ~20k message reported by a user could not be sent at all.
- The composer's send gate now surfaces the reason (`Input exceeds maximum
  length of 100,000 characters` via the store's `inputError`) instead of
  silently no-op'ing, and the character counter reflects the new cap
  (appears above 75,000, red above 100,000).
- `sendDirect` (retry/revision of an existing turn) intentionally has no
  length gate, unchanged.

### Large plain-text pastes attach as files instead of flooding the composer
- `frontend/src/components/chat/Composer.tsx` — pastes longer than
  `LARGE_PASTE_THRESHOLD` (4,000 chars) are converted to a
  `pasted-text-<timestamp>.txt` (`text/plain`) File and enqueued through
  the existing attachment pipeline (upload store → chip tray → indexing),
  with a toast explaining the conversion. Smaller pastes keep native inline
  insertion. The backend accepts `.txt` uploads (extension allowlist;
  text formats are exempt from magic-byte checks).
- 4,000 was chosen because this composer is a long-form prompt surface:
  prompts up to ~4k chars (~1k tokens) stay comfortably inline-editable,
  while paste-sized input is file material (deliberately ~2× the
  Slack/Teams convention, which is optimized for chat messages, not
  prompts).

### Draft persistence no longer amplifies every keystroke, and is bounded
- Previously every input change synchronously rewrote the ENTIRE draft to
  `localStorage` and forced a textarea reflow — measured at 37.8 ms of
  main-thread blockage per input event with a 220k draft; under
  event-wise delivery of a 20k draft (IME, clipboard managers, dictation,
  very fast typing) this saturated the main thread for minutes and crashed
  the browser tab (the reported Edge crash), and the unbounded draft was
  restored on every session reopen, crash-looping the browser
  (issue #616).
- Draft writes are now leading+trailing debounced (400 ms; the first
  change after idle still writes synchronously), the slash-menu detection
  reads only the last line (`lastIndexOf`, no full `split("\n")`), the
  auto-grow reflow is skipped once past the 200 px clamp at large sizes,
  and sending clears the draft synchronously.
- Drafts larger than `MAX_DRAFT_CHARS` (= `MAX_INPUT_LENGTH`, 100,000) are
  never persisted; the stale key is removed instead — an oversized draft
  can no longer be restored into a session loop.
- `TranscriptPane` no longer re-renders the whole transcript tree on
  every keystroke (selector-scoped store subscriptions).

## Why

User report: pasting a ~20k-character message showed an over-limit error,
crashed the entire browser (Microsoft Edge), and crash-looped on every
re-login/session reopen because the unsendable draft was restored each
time. Full trace under `.agents/issue-traces/chat-char-limit-browser-crash/`
(reproduction, root cause, frozen acceptance checks C1-C5).

## Operator / user visibility

- No migration required; the draft key format is unchanged. Oversized
  persisted drafts (from before this change) are dropped on next write
  rather than restored.
- A 100k-character query costs ~13 embedding chunks server-side (8,192
  chars per text) — a proportional retrieval-cost increase accepted with
  the higher cap.
- Known residuals, documented in the trace: the Composer itself re-renders
  per input event (inherent to the controlled-textarea pattern), and one
  bounded layout pass per event remains for inputs ≤ 4k chars.

## Known caveats

- Paste-to-attach uploads the text as a document: it becomes searchable
  once indexing completes (the composer warns while it is still
  indexing). It does not inject the text directly into the model context;
  this matches the existing attachment contract. Scoping a question to
  specific attachments via `document_ids` remains a separate,
  pre-existing mechanism (unchanged).
