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
pagehide), and the hook migrates the returned ids/seqs through the same
path the normal save uses, inside the pending-save chain revision
operations already await. Real-browser bfcache behavior remains unverified
(jsdom cannot emulate it); the interleaving is pinned at the store/hook
level.

## #823 — no chat state survives an identity boundary

After logging out and a different user logging in within the same browser
tab, landing on `/chat/:id` could hit ChatShell's session-load fast path and
render the previous user's transcript from memory with no server
authorization re-fetch. Logout — and a different-principal login, at the
instant the new credentials are published — now clears the whole chat
surface: transcript rows and edit snapshots (`newChat`), the composer's
unsent input, every persisted `ragapp_chat_draft_*` localStorage key (with
the pending debounce/unmount writers invalidated through a generation gate
so they cannot re-create cleared keys), the shell's session id/title and
evidence selection, and the upload store's chat attachments, upload rows,
and vault pointer (in-flight transfer completions and queue continuations of
the previous identity are suppressed; already-dispatched requests keep the
token they were sent with and cannot touch the new user's data). A failed
login never crosses the boundary; a same-principal re-login (with the user
in the response) leaves uploads and chat state untouched. A user-less
login response is conservatively treated as a boundary: that rare arm
clears in-progress uploads even if `fetchMe` later reveals the same
principal.

## #1019 — delete_user can no longer strand purged Draft Room bytes

`delete_user` prepared its Draft Room purge (input bytes tombstoned into
`.trash`) and then deleted the user inside one transaction whose recovery
was reachable only through two exception handlers; every other exception
class — routinely `asyncio.CancelledError` on client disconnect — escaped
with the tombstones stranded while the rows survived. The delete now runs
on a dedicated single-worker executor whose real future the route holds:
the recovery backstop waits for the worker's actual outcome (a queued
work item cancelled before entry never ran and restores immediately; a
detached worker that commits leaves the tombstones to the startup
reconcile; anything else rolls back and restores), observes settlement
through an event rather than by re-awaiting the future (so a worker
failure can never replace the original exception), guards its own
rollback, and re-raises the original exception. A second cancellation
delivered during that cleanup can still skip the restore — strictly
narrower than every pre-existing window, and disclosed here.

## Notes

- `clearMessages` remains intentionally unwired (`newChat` clears the
  broader surface).
- `vaults.py`'s own backstop is unchanged (its direct-on-connection shape
  keeps the #723 semantics correct there).
