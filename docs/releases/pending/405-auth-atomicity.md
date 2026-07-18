# auth.py single-transaction atomicity (Issue #405, closes #393)

## What changed

### Backend — `backend/app/api/routes/auth.py`

Four transaction-atomicity defects closed (the F-2.1 … F-2.4 cluster from
the #202 deep-dive). Each fix is localized to one route or helper.

- **F-2.1 (register username uniqueness):** the case-insensitive uniqueness
  pre-check was a bare `SELECT` running *outside* `BEGIN IMMEDIATE`. Under a
  concurrent duplicate registration the loser's `INSERT` hit the `users.username`
  UNIQUE constraint and the bare `except Exception` converted the
  `sqlite3.IntegrityError` to HTTP 500 instead of 409. The pre-check SELECT
  is now performed *inside* the `BEGIN IMMEDIATE` transaction in `_register_db`,
  the user `INSERT` is wrapped in a narrow `except sqlite3.IntegrityError → 409`
  (matching the `organizations.py:930-944` idiom), and the outer handler gained
  an `except HTTPException: raise` branch so the 409 propagates unchanged.

- **F-2.2 (change-password atomicity):** the password `UPDATE` + session
  `DELETE` committed in `_change_password_db`, then the new-session `INSERT`
  committed in a separate `_create_session_db`. A crash between left the user
  with a changed password and no valid session. The new-session `INSERT` is
  now part of the same `BEGIN IMMEDIATE`-implicit transaction as the password
  change, with a single `commit()`. `_create_session_db` is removed.
  `refresh_token_raw` / `refresh_token_hash` / `expires_at` / `user_agent`
  are hoisted above the closure so they are in scope for the merged `INSERT`.

- **F-2.3 (register atomicity):** the user `INSERT` committed in
  `_register_db`, then the session `INSERT` committed in a separate
  `_register_session_db`. A crash between left a registered user with no
  login session (and the client's retry would hit 409). The session `INSERT`
  is now part of the same `BEGIN IMMEDIATE` transaction as the user `INSERT`
  (and the default-vault assignment), with a single `commit()`.
  `_register_session_db` is removed. Token/cookie inputs hoisted as in F-2.2.

- **F-2.4 (failed-login lockout race):** `_record_failed_attempt_db` received
  `failed_attempts` as a parameter snapshotted *before* any lock, so the
  `if failed_attempts + 1 >= 5` lockout decision operated on a stale value —
  concurrent failed logins could each observe a stale read and skip setting
  `locked_until`. The function now:
  1. drops the `failed_attempts` parameter,
  2. opens `BEGIN IMMEDIATE`,
  3. issues the atomic `UPDATE … = failed_attempts + 1`,
  4. **re-reads** `failed_attempts` from the DB under the write lock,
  5. sets `locked_until` based on the fresh value,
  6. **returns** the fresh count so the `login` handler can issue an
     accurate trip response (401 vs 423) and accurate audit metadata
     without trusting the stale snapshot.

### Tests

- **New** `backend/tests/test_auth_atomicity.py` — 9 regression tests
  importing the real production functions (not replicas). Seven are TRUE
  regressions (verified to fail on pre-fix code by stashing the source
  changes and re-running): the F-2.4 transaction-boundary tests
  (`test_record_failed_attempt_rereads_under_lock`,
  `test_record_failed_attempt_no_lockout_below_threshold`), the F-2.3 and
  F-2.2 atomicity tests
  (`test_register_user_and_session_atomic_on_session_failure`,
  `test_change_password_password_and_session_atomic_on_session_failure`),
  the F-2.1 concurrent test
  (`test_concurrent_register_same_username`, `ThreadPoolExecutor(max_workers=2)`
  mirroring `test_org_invites.py:782`), and the two post-review regression
  tests added during PR feedback
  (`test_change_password_session_records_ip_and_user_agent` for the audit-trail
  parity fix, `test_record_failed_attempt_handles_deleted_user` for the
  vanished-user None-guard). Two are COVERAGE tests honestly labeled as such
  in the PR body (`test_register_duplicate_returns_409_not_500`,
  `test_five_sequential_failed_logins_lock_account` — the sequential lockout
  path was previously uncovered).
- **Updated** `backend/tests/test_deps_auth_to_thread.py` — the merged
  transactions reduce the `to_thread` call count in `register` from 3 to 1
  and in `change_password` from 3 to 2; the source-inspection thresholds
  are lowered accordingly (`>= 1`, `>= 2`), and the
  `test_register_uses_lambda_pattern` assertion (which checked for a
  `lambda` keyword that the F-2.1 fix deliberately removes) is rewritten to
  assert `to_thread` presence instead. All with rationale comments.

## Why

#393 captured the highest-value cluster of live findings from the #202
auth/mgmt/vault deep-dive that had not landed. The four defects are
medium-severity under concurrent access (multi-worker ASGI sharing one
SQLite file via WAL); low-traffic deploys are less affected. Combined they
allow: wrong HTTP status on a race (500 vs 409), inconsistent
post-failure state for register and change-password, and a brute-force
lockout that can be bypassed by overlapping attempts.

## Migration steps

None. No schema change (the `users.username` UNIQUE constraint and the
`failed_attempts`/`locked_until` columns already exist), no config change,
no API contract change (status codes move toward their documented values:
409 instead of 500 for duplicate register; 423 on the lockout-trip request
under concurrency where previously a stale-snapshot 401 was possible).

## Known caveats

- The AC4 "5 concurrent failed logins → exactly one sets locked_until"
  guarantee is proven by code-reading + the SQL-sequence regression test,
  not by a 5-way ThreadPoolExecutor test. SQLite's BEGIN IMMEDIATE
  serialization is a kernel-level invariant; a concurrency test would
  mostly re-prove SQLite's locking rather than the application fix. This
  is documented in the PR body and the final-critic review.

## Post-review hardening (PR feedback round)

A swarm-pr-review of this PR found 5 VALID findings; all 5 were resolved in
the same PR:

- **SAST baseline line-shift (blocker):** the bandit baseline key is
  `(test_id, filename:line_number)`, so this PR's auth.py line shifts made
  17 pre-existing findings (B105 'bearer' + B110 try/except/pass) appear
  "new" and broke CI. Regenerated the baseline via
  `python scripts/run_bandit.py --update-baseline`. Net finding count
  **140 → 138** (the F-2.2/F-2.3 refactor and the redundant-UPDATE removal
  each eliminated a B110 block — a strict security-posture improvement, not
  a regression).
- **Change-password session INSERT audit-trail gap:** the F-2.2 inlining
  copied the column list verbatim, omitting `ip_address`/`user_agent` that
  register and login include. Fixed by adding `ip_address = _request_ip(request)`
  to the hoist and including both columns in the INSERT. Regression test
  `test_change_password_session_records_ip_and_user_agent` added.
- **`_record_failed_attempt_db` None-deref:** unguarded `fetchone()[0]` would
  TypeError → 500 if a superadmin concurrently deleted the user during a
  wrong-password login. Fixed with a `row is None` guard that rolls back and
  returns 0 (caller issues a generic 401). Regression test
  `test_record_failed_attempt_handles_deleted_user` added.
- **`change_password` HTTPException-shim symmetry:** added
  `except HTTPException: raise` (inner + outer) mirroring register's F-2.1
  pattern, so any future in-txn HTTPException is not misconverted to 500.
- **Redundant second `UPDATE must_change_password = 0`:** removed (the column
  is already set in the password UPDATE).

Four advisory findings from the PR comment were rejected with rationale:
401-vs-423 info-disclosure (standard UX trade-off, pre-existing); narrow
IntegrityError catch (by design per plan-critic C5); txn-start strategy
inconsistency (out of scope for #393); rollback-of-implicit-txn hazard
(no live call site exhibits it).
