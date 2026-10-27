# Chat identity/page-life data lifecycle fixes (#815, #823, #1019)

## #815 — keepalive saves now carry their durable seq

The pagehide/`beforeunload` keepalive save path never read the server's
response, so rows saved through it never received their durable `seq` (or id
migration) in the chat store. If the page's store state survived — bfcache
restore, canceled unload, or a mobile background/foreground cycle — a later
Retry or Edit computed its truncate anchor below those rows' server seqs and
permanently deleted them server-side. The wrapper now opportunistically
parses the batch-save response (resolving `undefined` on any failure — the
best-effort contract is unchanged and no fallback writes are issued during
pagehide), and the hook migrates the returned ids/seqs inside the
pending-save chain revision operations already await. Two interleavings are
pinned: when the response settles while the stream is still live, the
durable seq is stamped on the live row immediately and the full id rename is
deferred to the stream's end (renaming mid-stream would orphan the stream's
appends — caught in CI by the issue552 suite); when it settles after the
stream ended, the full id/seq migration applies straight away. Real-browser
bfcache behavior remains unverified (jsdom cannot emulate it); the
interleaving is pinned at the store/hook level.

## #823 — no chat state survives an identity boundary

After logging out and a different user logging in within the same browser
tab, landing on `/chat/:id` could hit ChatShell's session-load fast path and
render the previous user's transcript from memory with no server
authorization re-fetch. Logout, a different-principal `register`, and a
different-principal login now all clear the chat surface **before** the new
credentials are published — so no request issued on the new token can
observe the previous user's state. The reset covers transcript rows and edit
snapshots (`newChat`), the composer's unsent input, every persisted
`ragapp_chat_draft_*` localStorage key (with the pending debounce timer and
the unmount flush generation-gated so they cannot re-create a cleared key),
the shell's session id/title, evidence selection, search query and pinned
sessions, and the upload store's chat attachments, upload rows, and vault
pointer (in-flight transfer completions and queue continuations of the
previous identity are suppressed; already-dispatched requests keep the token
they were sent with and cannot touch the new user's data). A failed login
never crosses the boundary; a same-principal re-login (with the user in the
response) leaves the surface untouched; a user-less login response is
unconditionally treated as a boundary.

## #1019 — delete_user can no longer strand purged Draft Room bytes

`delete_user` prepared its Draft Room purge (input bytes tombstoned into
`.trash`) and then deleted the user inside one transaction whose recovery
was reachable only through two exception handlers; every other exception
class — routinely `asyncio.CancelledError` on client disconnect — escaped
with the tombstones stranded while the rows survived. The delete now runs
on a dedicated single-worker executor whose real future the route holds:
the recovery backstop waits for the worker's actual outcome (a queued work
item cancelled before entry never ran and restores immediately; a detached
worker that commits leaves the tombstones to the startup reconcile; anything
else rolls back and restores), observes settlement through an event rather
than by re-awaiting the future (so a worker failure can never replace the
original exception), guards its own rollback, and re-raises the original
exception. Executor construction and submission sit inside the recovery
boundary, so a thread-start failure takes the same restore path. A second
cancellation delivered during that cleanup can still skip the restore and
release the pooled connection while the detached worker finishes its single
statement — strictly narrower than every pre-existing window (the base
released the connection on every cancellation), and disclosed here.

## Notes

- `clearMessages` remains intentionally unwired (`newChat` clears the
  broader surface).
- `vaults.py`'s own backstop is unchanged (its direct-on-connection shape
  keeps the #723 semantics correct there).
- The keepalive-seq residual recorded in `683-retry-edit-durable-seq.md`
  ("turns saved only through the page-unload keepalive path do not yet
  carry `seq`") is closed by this change.
