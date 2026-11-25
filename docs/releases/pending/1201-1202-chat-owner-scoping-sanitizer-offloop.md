# Chat session owner scoping, integrity discrimination and off-loop sanitizer (#1201, #1202)

## What changed

- **Chat sessions are owner-or-admin on every mutating surface** (#1201 AC1). Six HTTP mutation
  surfaces and the durable-stream boundary now answer `403 Cannot modify another user's session`
  for a non-owner, non-admin member of a shared vault. Legacy `user_id IS NULL` sessions stay
  shared-vault-accessible and admins/superadmins keep access. GET and fork are unchanged by the maintainer's recorded 2026-11-25 decision, which keeps #573's share
  link working.
- **Integrity failures are discriminated** (#1201 AC4). The two quality-report handlers and the two
  durable-turn helpers bind `IntegrityError` and take the idempotency recovery only for the
  `UNIQUE constraint failed` class. A non-unique failure answers the route's 404 only when a
  post-rollback lookup finds the parent gone; otherwise the original error propagates instead of
  being reported as a vanished parent or a race winner.
- **The assistant sanitizer leaves the event loop on every `chat.py` path** (#1202 AC1). Four normal
  paths dispatch through `asyncio.to_thread`. The cancellation backstop submits the finalize to a
  process-wide single worker without waiting, so neither the sanitizer nor the SQLite work runs on
  the loop during `CancelledError` unwinding. A failure to create the worker is logged and skipped —
  there is deliberately no inline fallback. At application shutdown the worker is drained before
  `db_pool.close_all()` so queued interrupted rows commit.
- **`chat_message_max_chars` is enforced on the two write routes for user-role items** (#1202 AC2)
  with the stream route's 422 shape, after auth, vault and owner checks; an over-cap batch writes
  nothing. Assistant-role items are bounded separately by `assistant_message_max_chars` (131072);
  assistant answers below that ceiling remain unbounded by the live-user cap, per the #922 contract.
- **Fence tracking is container-aware** (#1202 AC3, AC4). Literal `<answer>` markup inside a
  block-quoted or list-item fence survives, including wrapper tags before the container markers on
  the opener line; a second pass is a fixed point. Plain top-level fences behave as before.

## Why

Residual findings from PR #1193's review round, filed as #1201 and #1202: vault-write membership
granted control over another member's session; two report handlers mapped every integrity failure
to the unique race; the regex-heavy sanitizer blocked the event loop; write routes stored unbounded
user content; and the fence scanner deleted literal markup inside quoted fences.

## Migration

- **Back up the database before upgrading.** The start-up re-sanitize migration rewrites assistant
  rows whose sanitized content changes. With the new scanner it preserves literals inside quoted
  and list fences from now on; markup the old scanner already deleted cannot be restored.
- **Prefer rolling forward.** After a rollback the old scanner runs again and will delete literal
  wrapper markup inside quoted or list fences written since the upgrade.
- Clients that posted another member's session directly now get 403. A member who opens another
  member's session through a #573 share link and presses Send now receives the owner 403 instead
  of writing into that session.

## Caveats and open decisions

- Quality-report submitter attribution (#1201 AC3) uses the existing ` || ` note convention: a
  differing re-submitted note is appended with the second submitter id, and repeat submissions of the
  same note are idempotent. The one-row-per-identity retry contract is unchanged.
- GET and fork remain vault-readable by the maintainer's recorded decision (2026-11-25): denying them would retract #573's share link.
- Assistant-role content on the write routes is capped by `assistant_message_max_chars` (131072,
  derived from `thinking_max_tokens`); legitimate long answers remain well above the live-user cap.
- Canvas routes keep their pinned vault-only authorization matrix (#509); whether they should
  follow the new owner rule is an open product question.
- `llm_client.py:569` still sanitizes synchronously inside async completion methods; it is in a
  file #1202 marks out of scope and is tracked in separate issue #1209.
- Remaining fence limitation: nested list containers with lazy continuation lines, and a wrapper
  tag sitting on the closing fence line.
