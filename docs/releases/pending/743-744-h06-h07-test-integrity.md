---
title: "Security/lifecycle tests that can fail; schema fixtures that match production (#743, #744)"
issues: [743, 744]
---

## What changed

Collated Workstream H PRs 6+7 (maintainer-authorized; precedent PR #1049): the
security-, lifecycle-, and schema-sensitive test surfaces stop asserting through
proxies weaker than the invariants they guard.

### #743 — security/lifecycle behavior gets tests that can actually fail

- Org-member removal now has a DB read-back test: `org_members` deleted AND
  `group_members` cascaded for that org (untouched in other orgs) — the surface
  assertion never saw either table.
- `test_users_routes.py` gains `TestSecurityAuditTrail`: one test per users.py
  audit family (all eight: `user.created`, `user.updated`, `user.password_reset`,
  `user.role_updated`, `user.active_updated`, `user.deleted`,
  `membership.organizations_replaced`, `membership.groups_replaced`), plus a
  deactivation test asserting `user_sessions` rows are revoked while
  bystanders keep theirs (production deletes them on the same connection
  before the single commit; the test pins the observable outcome).
- The first tests that enter and exit the REAL `lifespan()` (the "impractical in
  unit CI" justification did not hold): startup wiring
  (`app.state.background_processor`), shutdown awaits `stop()`, and the logging
  setup's fresh-handler and existing-handler branches both attach
  `RequestIdFilter`/`SensitiveFieldFilter` instances to every root handler —
  runtime spies where substring/count source checks trivially survived
  commented-out registrations. Entering the lifespan twice in one process
  requires resetting its process-global singletons; the shared helper does that.
- `test_559_s3_c5_reindex_lease.py` drives the REAL `_reindex_lease_worker_loop`:
  two pending reindex jobs, the first settlement raising a transient
  `sqlite3.OperationalError`, worker survives and claims both (settle-exception
  survival, per the issue text).
- nightly `full-deps` installs `backend/requirements-otel.txt` and runs
  `tests/test_518_closure_units.py` unskipped — `TestRealTracerPipeline` (the
  F-001c held-span-lifetime guard) was skipped on every CI/nightly run because
  no workflow ever installed the OTel SDK (validated with the pinned
  opentelemetry-sdk 1.45.0: 10 passed, 0 skipped).

### #744 — schema/migration fixtures stop diverging from the real database

- `user_route_helpers.setup_test_db` now builds from the PRODUCTION schema
  (`init_db` + `run_migrations`, session-cached template copied per test —
  1.5 s once, milliseconds per test) instead of the hand-copied `TEST_SCHEMA`.
  Fixture-baseline note: the production template carries the migration-seeded
  'Default' vault (id=1), so every route-test DB now includes it —
  `include_default_vault` is retained as a compatible no-op and vaults created
  by consumers start at id=2 (all ten consumers verified green with this
  baseline):
  the route-test DB carries `security_audit_log` and `user_sessions` (the eight
  users.py audit emissions were silently no-op'ing without them) and declares no
  `vaults` foreign keys production does not have. Hermetic build: settings
  paths pinned and restored; proven by the new
  `test_h07_schema_template_isolation.py` (copy independence, pristine rebuild,
  no ambient `app.db`/vault-tree writes, no WAL sidecars).
- `test_migrate_embeddings_dim.py` builds its SQLite from the production schema
  and now seeds one `'indexed'` AND one `'partial'` row, asserting
  `_reset_file_statuses` resets both (#694's partial handling finally has a
  consumer-visible test).
- The C9 dimension-migration Part-1 seeds TWO files into the index; after the
  bare-call fail-closed refusal, both files' rows are asserted unchanged.
- `test_schema_unification.py` gains a legacy-`memories` upgrade test through
  `run_migrations` (retention columns added, seeded row survives).
- `test_vault_path_migration.py` gains the name==str(other-id) collision case:
  vault 1's files stay under `vaults/1/` (the #690 guard skips the collision).
- `TestClaimsUniqueDedupRemapsEvidence` builds `wiki_relations` (the second FK
  child) and asserts the dedup REMAPS the doomed duplicate's relation to the
  survivor instead of cascade-deleting it (#699's remap finally observed).

## Why

Frontier-audit findings S02-SK2-08, P03-SK2-07, TQ-sibling-batch-08-06,
TQ-sibling-batch-04-01, T1-21-K-02, P02-SK2-13 (#743) and T1-21-KR-09,
S06-SK-03/S06-SK2-05, T1-25-K-07, T1-05-K-07, T1-05-K2-07, T1-05-K2-08 (#744):
every cited suite was green while the invariant it nominally guarded was
unobserved. The upstream runtime defects those gaps once masked were fixed
earlier (#690/#691/#694/#699/#721/#722); the #743-AC2 admin-deactivates-
superadmin decision was made and shipped by #722.

## Guardrails

Eighteen frozen acceptance checks guard this work: the 14 DISCRIMINATING
checks (7 mutation-kill + 2 schema-parity + 5 structural drift predicates) live in
`backend/tests/test_h06_mutation_kills.py` and
`backend/tests/test_h07_fixture_fidelity.py` and run in CI's Backend job on
every push — mutation-kill checks name a production mutation
(org-member DELETE removed, lifespan stop() skipped, both addFilter call
sites commented out, the reindex worker loop early-returned, the audit
wrapper no-op'ed, a migration skipped) and require the hardened suites to
fail under it. The 4 PRESERVING baselines are issue-tracer replay wrappers
(local `.agents/issue-traces/` artifacts, not CI-wired). Anchor receipt (pre-implementation): see the issue-tracer checkpoint
comments on #743/#744.

## Impact

Test files, fixtures, and one nightly workflow step only — no production
behavior, API, migration, or persistence change. The ten `setup_test_db`
consumer files all stay green (258 tests) with zero fixture adjustments.
