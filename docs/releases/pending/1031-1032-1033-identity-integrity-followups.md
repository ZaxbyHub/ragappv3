# Identity-boundary and committed-path integrity follow-ups (#1031, #1032, #1033)

## #1031 — destructive-delete bookkeeping survives cancellation

`DELETE /api/users/{id}`'s post-commit steps — finalizing the Draft Room
purge discard, invalidating the active-user cache, and writing the
`user.deleted` security-audit event — previously ran only on the request's
normal return path. A client disconnect landing at the delete await while
the detached worker committed (or during the bookkeeping awaits themselves)
skipped all three: the user was gone with no audit row, a stale cache
entry kept serving the deleted principal until TTL expiry, and the purge
tombstones waited for the startup reconcile. The bookkeeping now runs as
one at-most-once drained task on every committed path: cancellation
delivered anywhere in the delete window is absorbed until the audit row,
cache invalidation, and purge finalization land, and only then does
`CancelledError` propagate (a bookkeeping failure is logged, never a 500
for a committed delete, and never a silent 200 for a cancelled one). The
settlement observation is event-truth: a failed or cancelled settle-wait is
retried until the worker actually settles, so a connection can never be
released back to the pool while a delete transaction is still open on it.

## #1032 — identity switches clear the surfaces beyond chat

An in-app identity switch (logout, or a different-principal login/register
in the same tab) cleared the chat surface but left three per-user surfaces
populated: the app-wide React Query cache (no query key carries a
principal, so the previous user's Draft Room and Canvas entries could
render before the new user's fetch replaced them), unsent canvas drafts
(persisted under a shared `canvas-draft:` localStorage namespace the next
principal could restore), and the Draft Room editor's unsent text. The
switch now clears the shared query client (in-flight fetches detach from
the cleared cache and cannot re-enter it), moves canvas drafts to a
principal-scoped, generation-gated scheme (a refused removal leaves residue
the next principal's reads cannot address; a pending debounce, unmount, or
artifact-switch flush scheduled before the switch can never re-create a
cleared key), and resets the Draft Room UI store's draft text. Same-user
re-logins clear nothing.

## #1033 — removing an org member can no longer orphan the organization

`remove_org_member` refused owner targets from a role read that ran BEFORE
its write transaction, while its membership DELETE carried no role
predicate — a `transfer_ownership` committing in between promoted the
target to owner and the DELETE then removed them, leaving the organization
with zero owners (recoverable only by a superadmin). The owner refusal is
now re-read inside the `BEGIN IMMEDIATE` transaction (a vanished row
refuses with 404), so the SQLite write lock makes the check definitive.

## Known limitations

- The non-commit restore path (rollback + `restore_purge`) still awaits
  unshielded: a second cancellation delivered exactly during the restore
  can strand the tombstoned bytes for the startup reconcile (the
  PR #1019-disclosed residual; tracked with the family follow-up).
- 31 sibling mutation routes still write their security-audit events with
  a plain await a cancellation can skip for an already-committed change;
  the transferred-pattern rollout and the adjacent `transfer_ownership`
  check windows are tracked in the same follow-up (#1036).
- `clear()` alone does not reset mounted React Query observers, so the
  boundary runs `resetQueries()` first (resetting every mounted observer to
  its initial state and cancelling in-flight fetches) before clearing the
  cache. AdminGroupsPage's nested per-scope query client predates this
  change and is retired by its existing `principalGeneration`-scoped remount
  key, not by the boundary's singleton clear.
- The canvas editor now reads its principal reactively and rehydrates under
  the new key on a same-artifact principal switch, so unsaved editor text
  cannot survive the boundary in component state either. The boundary's
  `resetQueries()` also causes every mounted live-data view to re-request its
  data once (results are then discarded by the cache clear); the requests are
  unabortable because the app's query functions do not consume abort signals
  — bounded one-request-per-mounted-query traffic with no user-visible effect.
- A canvas draft persisted before this change (legacy unscoped key) is
  removed at the first identity boundary rather than migrated — migrating it
  would hand the previous principal's text to the next one. Draft Room view
  selections (compare revision ids, stage) persist across switches as
  server-authorized identifiers with no user content.
