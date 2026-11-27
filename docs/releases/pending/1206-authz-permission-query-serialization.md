# Serialized permission-query dispatch in get_effective_vault_permissions (#1206)

Issue: #1206 (follow-up from PR #1200's post-merge Backend CI verification;
the wedge itself is pre-existing code from #481 / commit 2f4a6a37, untouched
by PR #1200).

## What changed

`get_effective_vault_permissions` in `backend/app/services/authz_policy.py`
no longer dispatches its four permission queries
(`_query_vault_members`, `_query_group_access`, `_query_public_vaults`,
`_query_org_vaults`) concurrently through
`asyncio.gather(asyncio.to_thread(...))` when
`sqlite3.threadsafety == 3`. The queries now run strictly one at a time via
sequential `asyncio.to_thread` calls, each still off the event loop, all on
the caller's single `sqlite3.Connection`. The module-level
`_SQLITE_SERIALIZED` flag and the `_warn_fallback_threading()` helper are
preserved for non-SERIALIZED builds, but the flag now only gates the
once-per-process diagnostic warning — the dispatch shape is identical in both
modes. The warning (logged only on non-SERIALIZED builds) now says SQLite
does not mutex connections there, so a request connection must never be
used by two threads at once.

The no-concurrent-dispatch contract is pinned twice in the new
`backend/tests/test_authz_policy_query_isolation.py`:

- `test_permission_queries_do_not_overlap_on_shared_connection` (AC1) — a
  duck-typed connection delegate records overlapping `execute` calls (each
  held open for 250 ms so a concurrent dispatch cannot slip through a narrow
  window) while asserting the effective-permission map over a seeded
  member/group/public/org fixture is unchanged;
- `test_no_shared_connection_gather` (AC3) — an AST census
  (`test_c08_seam_census` style, with a KNOWN-BAD self-test so the matcher
  cannot fail open) asserting `get_effective_vault_permissions` contains no
  concurrent-dispatch call at all (`gather` qualified or bare,
  `create_task`, `ensure_future`, `wait`, `TaskGroup`).

`backend/tests/test_effective_vault_permissions_parallel.py`, which
previously pinned the gather construct, now pins its absence: gather must
never be called (any build), the four `to_thread` wraps must still happen,
and a raising `asyncio.gather` patch must leave the call unaffected.
`backend/tests/test_d08_sequential_branch_parity.py` (flag parity + the
forcing-test requirement of `test_d08_ops_hygiene` C18) keeps its assertions;
only its docstring and comments were updated, since both flag values now run
the same sequential dispatch and the parity check is warning-path vs
default-path. The `TestEvaluatePolicy` behavioral baseline in
`backend/tests/test_deps_auth.py` (59 passed) is unchanged and still green.

## Why

On CI runners CPython reports `sqlite3.threadsafety == 3`, so the gather
branch ran, and all four closures executed on the ONE shared
`sqlite3.Connection` the caller passed in. Under full-suite load the
`concurrent.futures` worker threads wedged inside `db.execute` for longer
than the backend watchdog's 360 s quiet window, killing the Backend job with
exit 88 and no failing test (five observed failures on master, including
twice at the PR #1200 merge commit — Actions run 45263195017, both attempts;
faulthandler dumps show three threads parked in `db.execute` at the three
query lines). The suite passes in isolation, so this is load-dependent and
inherently un-provable by a local repro; the fix's proof is the structural
pin plus CI-green streaks. The returned permission map is byte-for-byte the
same function of the DB rows (the merge logic below the queries is
untouched), so no permission decision changes.

## Migration steps

None. The public surface (`get_effective_vault_permissions`,
`get_effective_vault_permission`, `evaluate_policy`, the
`VAULT_PERMISSION_*` maps, and their `app.api.deps` re-exports) is
unchanged, as is every permission level, the superadmin short-circuit, and
the pooled-connection lifecycle in `backend/app/models/database.py`. The
only observable differences: permission resolution no longer overlaps
`execute` calls on a shared connection, and the non-SERIALIZED fallback
warning text was reworded.

## Known residual (out of scope)

`VisionEvidenceService.run` (`backend/app/services/vision_evidence.py`) fans
out per-artifact tasks with `asyncio.gather`, and each can call the
request-scoped DI `evaluate` on the non-streaming chat path, so two
`get_effective_vault_permissions` calls can still overlap on one connection
when `multimodal_query_vision_enabled` is on (default off). This PR does not
touch that path; it is tracked as follow-up issue #1225.

## Known caveats

- Sequential dispatch means the four queries' latencies add instead of
  overlapping; each is a small indexed lookup, so the wall-clock cost is
  negligible relative to the removed CI-wedge risk.
- `_SQLITE_SERIALIZED` no longer selects a dispatch branch; tests that
  monkey-patch it (the C18 forcing-test census, the parity test) force only
  the diagnostic-warning path. `deps.py` still re-exports the flag for
  import compatibility.
- The overlap recorder's 250 ms hold makes the AC1 pin take ~1 s; that is
  the bounded failure mode replacing a 360 s silent job hang (AC4).
- The wedge was never reproduced locally (load-dependent); CI-green streaks
  on the Backend job are the fix's ongoing validation, cross-referenced with
  the #1021 residual diagnosis noted on the issue.

## Tests

- `backend/tests/test_authz_policy_query_isolation.py` — RED at base commit
  8786dba0 (both the overlap recorder and the AST census fail), GREEN after
  the fix; includes the KNOWN-BAD matcher self-test.
- AC verification (as written on the issue):
  `python -m pytest tests/test_authz_policy_query_isolation.py -q -p no:randomly -p no:cacheprovider`
  (3 passed), `... ::test_no_shared_connection_gather ...` (1 passed),
  `python -m pytest tests/test_deps_auth.py -q -p no:randomly -p no:cacheprovider`
  (59 passed, the preserved behavioral baseline),
  `python -m pytest tests/test_deps_auth.py tests/test_authz_policy_query_isolation.py -q -p no:randomly -p no:cacheprovider -n auto --timeout=120`
  (62 passed under parallel load with a bounded per-test timeout). The
  `-n auto` flag needs `pytest-xdist`, which is NOT in
  `backend/requirements-dev.txt` (CI never runs `-n auto`); install it
  separately or drop `-n auto` to run the same tests serially.

## SAST baseline

The added module-level comment shifts the four pre-existing
`get_effective_vault_permissions` B608 findings (the `IN ({placeholders})`
f-string queries, whose placeholders are a fixed `?` list with every value
bound as a parameter) down seven lines. `run_bandit.py` keys findings by
line, so `backend/security/bandit-baseline.json` was regenerated with
`python scripts/run_bandit.py --update-baseline`; the only change is those
four entries' line numbers/snippets (163/170/183/193 to 170/177/190/200)
and the file's `loc` count. No finding was added or removed.
