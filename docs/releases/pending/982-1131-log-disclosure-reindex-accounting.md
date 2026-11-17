# fix(logging,reindex): C15 log-disclosure wider family + duplicate-accounting edges (#982, #1131)

## What changed

Closes the #287/#710/#730 default-log redaction contract's remaining wider
family (#982) and the two reindex duplicate-accounting edges disclosed at PR
#1123's closeout (#1131).

### #982 — log disclosure

- **Family A (provider bodies):** `llm_client.py`'s non-stream and streaming
  `HTTPStatusError` arms — and the in-band SSE error channel — now raise
  bounded summaries (`HTTP error <status> (body_len=N, body_sha256=hex10)` /
  a status-less form for the in-band channel) instead of composing the full
  provider body into `LLMError` messages that 15+ default-level call sites
  format into logs. The full body is logged at DEBUG at each construction
  site. `model_checker.py`'s probe-error mapping gets the same treatment
  (its result dict also feeds admin/monitor-gated HTTP responses).
- **Family B (user-chosen names/paths):** every verified default-level log
  line carrying a user-derived filename, vault name, path, email
  sender/subject, or org slug across `documents.py`, `database.py`,
  `background_tasks.py`, `document_processor.py`, `email_service.py`,
  `file_watcher.py`, `image_search.py`, and `upload_path.py` now logs a
  bounded form (`name_len=`/`path_len=`/`slug_len=`, ids kept), with full
  values at DEBUG. Exception CARRIERS are bounded at the raise site (email
  vault-name `ValueError`, `image_search` entry errors, `upload_path`
  migration errors) so `exc_info` rendering is clean too. The #1041
  deliberate carve-out (`_log_upload_rejection` filenames verbatim) is
  untouched and its 4-logger-call structural pin holds. A new supplementary
  AST guard (`test_982_1131_guard_family_b_names.py`) walks `backend/app`
  with a name/attr set for filename/vault-name/path identifiers
  (len()-subtrees blessed); per-site marker probes (frozen checks C1-C6)
  remain the primary coverage.
- **Family C (access log):** the four shipped non-container launch paths
  (`start-services.ps1`, both `INSTALLATION.md` commands,
  `CONTRIBUTING.md`) and the nightly schemathesis server now disable
  uvicorn's access log (`--no-access-log`), matching the container. The
  admin guide states the blind-spot caveat (no access line for CORS
  preflight / invalid-Host 400 / unhandled 500) for native runs too, and
  documents the operator re-enable path (`--access-log`) with an explicit
  raw-query disclosure warning. The nightly change also stops the
  schemathesis log artifact from persisting fuzz-generated query strings.
- **Family D (limiter URI):** a malformed `REDIS_URL` (unregistered scheme,
  unparsable sentinel/cluster URI) previously crashed boot with the
  `limits` `ConfigurationError` message embedding the full credential-bearing
  URI. `build_limiter` now re-raises `RuntimeError` with the `redact_url`
  form, `from None` (severing the credential-bearing `__context__` chain
  from boot tracebacks). Boot still fails closed on bad config.

### #1131 — reindex duplicate accounting

- **CC-001 (race-loser unwind):** the finalization-race backstop now
  unwinds the loser's already-written vector generation when it settles the
  row. New public store seams: `write_locked()` (Lance-first lock context,
  matching `commit_dimension_rebuild`'s existing lock order) and
  `delete_generation_by_file_locked(file_id, file_hash, target)` (validates
  numeric id + hex hash; safe-order mode deletes exactly the loser's
  `{file_id}_{hash8}_%` generation; legacy mode delegates to the
  whole-file unlocked delete under the held lock — legacy ids carry no
  generation prefix and legacy semantics are delete-then-add whole-file).
  The unwind (`_unwind_duplicate_loser_generation`) holds the Lance lock
  across an explicit `BEGIN IMMEDIATE` claim transaction: a same-path row
  reset in any process cannot commit between the claim-read and the delete
  (a reset that committed earlier is visible and skips the delete; one
  committing later writes replacement vectors strictly after). Best-effort
  failure path: bounded WARNING (file_id only) + DEBUG detail; the orphan
  persists exactly as at base (operator vault reindex recovers it) — no
  tombstone is written and no sweep change is made, so no automated path
  can delete good vectors. Two disclosed base-state residuals (unchanged
  from base behavior, now stated): a failed delete leaves the orphan; a
  late in-flight writer's `add_chunks` after a successful unwind persists
  (its own finalize failed the same unique index; nothing deletes it).
- **RP-001 (staged-abort compose):** a staged dimension/identity rebuild
  that aborts after discovering duplicate-hash failures now returns the
  per-file ledger (`duplicate_file_ids`, `failed_ids`, `failed` count,
  ten-entry-bounded `details`) instead of the empty `("failed", {}, exc)`
  shape, so the runner's existing arms surface the duplicates (pure-
  duplicate aborts settle terminally via the duplicate-only arm; mixed
  aborts requeue with the narrowed non-duplicate scope, protected by the
  unchanged narrowed-staging refusal guard). Identity-only aborts with
  empty ledgers keep the exact empty shape. No narrowed staging is
  licensed (the staged table is a global replacement — the refusal guard
  stays).
- **ABSORB (re-selection loop):** all four reindex selection variants
  exclude rows that are terminally duplicate-settled
  (`status='error' AND error_message LIKE 'DUPLICATE_FILE:%'`, NULL-safe),
  so a settled loser is no longer re-selected on every later vault reindex,
  deterministically re-failed at the pre-vector guard, and blocking the
  identity/readiness lift. Plain transient-error rows remain selectable;
  the winner serves the content; a wanted re-ingest is a fresh upload.

## Open owner decision (ASK-1)

The unwind's failure path is best-effort (above). A self-healing layer —
tombstone + terminal-state sweep state machine — was designed and
plan-reviewed but requires an explicit owner decision on its ms-scale
cross-store TOCTOU residual (identical class to the shipped cancellation
sweep's accepted window; full closure needs a `vector_delete_pending`
claim-fence migration covering BOTH tombstone paths). Recommendation on
file (swarm-counsel: adopt the layer + accept the residual); it layers on
this fix without rework either way.

## Test plan

Anchor chain v1 issues/982#issuecomment-6950883207 → v2 6954250840 → v3
6956429619 → v4 6957415458 (five CHECK_WRONG amendments, all
plan-critic-sanctioned and ruled PRESERVE by the independent implementation
reviewer; semantics digest 9a84a416 constant across all four receipts): C1-C6 DISCRIMINATING (marker probes, RED→GREEN),
C7/C10 PRESERVING (existing redaction pins + the #1112 battery), C8/C9/C11
NEW-SURFACE (fenced unwind, staged-abort compose, ABSORB exclusion — all
GREEN). New guard suite: grown AST census (fails closed on drift; two
verified false-positive pins documented) + fence/lock-order/late-writer/
delete-failure/staged-both-modes/G-1-four-variants legs. Blast radius:
test_1112 battery, test_1041, test_c15/c10 families, vault-path migration,
email service, upload path suites all green.
