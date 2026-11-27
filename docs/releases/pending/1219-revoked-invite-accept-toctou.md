# fix: revoked org invite can no longer mint membership (accept/revoke TOCTOU, issue #1219)

## What changed
`accept_org_invite` in `backend/app/api/routes/organizations.py` now re-reads the
invite's `revoked_at` and `accepted_at` INSIDE its `BEGIN IMMEDIATE` transaction,
immediately before the `org_members` INSERT, and refuses with the same generic
`400 Invalid or expired invite` (rolling back first) when the invite is revoked,
accepted, or has vanished. Previously the invite's state was only read on the
autocommit connection before the transaction opened, so a `revoke_org_invite`
committing in that window was invisible and the revoked invitee still received a
real membership row.

A new regression spec `backend/tests/test_org_invite_revoke_race.py` drives the
real `accept_org_invite` and `revoke_org_invite` handlers on two separate pool
connections: a RACE leg that commits the revoke between the accept's
pre-transaction read and its `BEGIN IMMEDIATE` (RED at base — the base tree minted
the membership; GREEN after the fix — no membership row, invite not marked
accepted), plus two control legs (no concurrent revoke → accept succeeds and
mints exactly one membership; revoke fully committed first → accept refuses 400
and mints nothing) so the race leg cannot pass vacuously.

## Why
Follow-up from PR #1216's closeout (collated #1209 + #1199), filed as issue #1219
(high-severity security, labels `security`/`auth`). The accept/revoke TOCTOU let a
revoked invite still mint `org_members` — an authorization-bypass class defect
structurally identical to the caller-revalidation windows fixed in #1036/#1037
and #1043: any state check performed on an autocommit read before a write
transaction must be re-validated under that transaction's write lock.

## Migration steps
None — backend-only behavior fix; no schema, API, or configuration change. The
refusal status and message for the revoked-invite case are unchanged (the
pre-transaction check already returned `400 Invalid or expired invite`).

## Known caveats
- The re-read also refuses when the invite was accepted by another connection in
  the same window; the pre-transaction path already refused that case, so no
  observable contract changed, but the in-transaction check is now the
  authoritative one.
- The revoke handler's own logic is untouched (issue scope): a revoke that runs
  after an accept has committed still succeeds in setting `revoked_at` on an
  already-accepted invite, exactly as before.
- The race spec interleaves by patching `asyncio.to_thread` at the driven
  connection's own `BEGIN IMMEDIATE` dispatch (gated on the connection identity
  because `transaction_context` also issues a raw `BEGIN IMMEDIATE`); any future
  refactor that opens the accept transaction through `transaction_context` will
  need the trigger updated, and the AC2 source check
  (`revoked_at` present between `BEGIN IMMEDIATE` and `conn.commit()` in
  `accept_org_invite`) will flag it.
