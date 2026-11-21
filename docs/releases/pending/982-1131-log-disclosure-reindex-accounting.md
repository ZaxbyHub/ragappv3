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
  (its result dict also feeds admin/monitor-gated HTTP responses), as do the
  curator sibling sites (`wiki_curator_client.py`'s two raise shapes and the
  `settings.py` wizard-probe error dict, which previously carried 200-char
  provider-body excerpts into admin-channel messages — fixed in the
  final-critic round rather than deferred).
- **Family B (user-chosen names/paths):** every verified default-level log
  line carrying a user-derived filename, vault name, path, email
  sender/subject, or org slug across `documents.py`, `database.py`,
  `background_tasks.py`, `document_processor.py`, `email_service.py`,
  `file_watcher.py`, `image_search.py`, and `upload_path.py` now logs a
  bounded form (`name_len=`/`path_len=`/`slug_len=`, ids kept), with full
  values at DEBUG. Exception CARRIERS are bounded at the raise site (email
  vault-name `ValueError`, `image_search` entry errors, `upload_path`
  migration errors). The reindex and upload `logger.exception` sites still
  render third-party/OSError text at ERROR (a disclosed pre-existing residual;
  see the Considered table). The #1041
  deliberate carve-out (`_log_upload_rejection` filenames verbatim) is
  untouched and its 4-logger-call structural pin holds. A new supplementary
  AST guard (`test_982_1131_guard_family_b_names.py`) walks `backend/app`
  with a name/attr set for filename/vault-name/path identifiers
  (len()-subtrees blessed); executed marker probes (frozen checks C1-C6)
  cover ONE REPRESENTATIVE per file and remain the primary coverage, with
  the expanded census (now including sender/subject/directory) as the
  supplementary rung for the other sites.
- **Family C (access log):** the four shipped non-container launch paths
  (`start-services.ps1`, both `INSTALLATION.md` commands,
  `CONTRIBUTING.md`) and the nightly schemathesis server now disable
  uvicorn's access log (`--no-access-log`), matching the container. The
  admin guide states the blind-spot caveat (no access line for CORS
  preflight / invalid-Host 400 / unhandled 500) for native runs too, and
  *(superseded in part by #1179: unhandled 500s now emit an
  `http_request` record; the caveat's remaining cases are CORS preflight
  and invalid-Host 400s)*
  documents the operator re-enable path (`--access-log`) with an explicit
  raw-query disclosure warning. The nightly change stops the SERVER access log
  (`schemathesis-uvicorn.log`) from persisting fuzz-generated query strings;
  schemathesis's own CLI failure reproductions in `schemathesis-run.log` still
  carry them (the uploaded artifact contains BOTH files).
- **Family D (limiter URI):** a malformed `REDIS_URL` (unregistered scheme,
  unparsable sentinel/cluster URI) previously crashed boot with the
  `limits` `ConfigurationError` message embedding the full credential-bearing
  URI. `build_limiter` now re-raises `RuntimeError` carrying a SCHEME-ONLY
  diagnosis (`scheme='bogus'`), `from None` (severing the
  credential-bearing `__context__` chain from boot tracebacks) — the final
  round's traceback guard test found that `redact_url` does not cover
  unknown-scheme URI shapes (`bogus://<secret>@host` rendered verbatim), so
  the raised message carries no URI text beyond the scheme token. The token
  is extracted STRUCTURALLY — echoed only when the entire prefix before the
  first `://` is a single RFC 3986 scheme token; every other shape (a
  colon-less `REDIS_URL`, whose bare split token would BE the entire secret
  even when it is scheme-charset-conforming like a bare password — the
  delta-review round 2 probe; a coloned non-URI such as `pass:word`; a
  delimiter-less sentinel blob) emits the literal `unparseable`
  placeholder. The only raw text that can reach the boot diagnosis is a
  structurally-valid scheme token; nothing beyond that token is echoed.
  Registered-scheme malformed URLs that make redis-py raise a plain
  `ValueError` are caught by the same handler and carry no URI text either.
  Boot still fails closed on bad config. Known residuals (disclosed): the boot INFO line still prints
  (under an explicitly configured root logger; the shipped launch paths configure
  logging only in lifespan, AFTER the import-time limiter build)
  `redact_url(storage_uri)`, which passes username-only, query-string-password,
  no-`@host` and unescaped-delimiter password shapes through verbatim
  (pre-existing; follow-up #1171), and non-`ConfigurationError` failures (cluster setups, bad
  ports) escape the redacted handler with equal-to-base messages.

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
  persists exactly as at base — no tombstone is written and no sweep
  change is made, so no automated path can delete good vectors. A same-
  dimension vault reindex CANNOT recover the orphan (the pre-vector guard
  and the ABSORB exclusion both stop it); recovery requires deleting the
  error row or a staged dimension/identity rebuild. Two disclosed base-state residuals (unchanged
  from base behavior, now stated): a failed delete leaves the orphan; a
  late in-flight writer's `add_chunks` after a successful unwind persists
  (its own finalize failed the same unique index; nothing deletes it).
- **RP-001 (staged-abort compose):** a staged dimension/identity rebuild
  that aborts after discovering duplicate-hash failures now returns the
  duplicate ledger (`duplicate_file_ids`, `failed` count,
  ten-entry-bounded `details`) instead of the empty `("failed", {}, exc)`
  shape, so the runner's duplicate-only arm settles `failed` terminally on
  the FIRST attempt — pure-duplicate and mixed aborts alike (review F-001:
  composing `failed_ids` too made the runner persist a narrowed
  `$.retry_file_ids` whose attempt-2 re-run deterministically trips the
  #702 refusal guard while the dimension is still changed, burning every
  attempt to cap; per the #820 determinism principle the compose carries
  duplicates only and never persists a retry scope). The mixed abort's
  transient failures ride along as `transient_failed_ids` — transport
  metadata the runner pops before persistence and folds into the settled
  `failed` count WITHOUT ever entering the retry scope, so EVERY terminal
  settlement recount (the below-cap duplicate-only arm AND the attempt-cap
  arm — reachable on the first attempt at the supported
  `jobs_max_attempts=1` setting) preserves the distinct transient+
  duplicate accounting (delta-review r2/r3: without the fold the runner's
  cross-attempt recounts saw an empty transient input and silently
  dropped the transient from the persisted count). A PURE-transient
  staged abort keeps the empty global
  shape so the retry re-stages the FULL scope and can complete. No
  narrowed staging is licensed (the staged table is a global replacement —
  the refusal guard stays).
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
6956429619 → v4 6957415458 → v5 6979886877 → v6 6990800554 (six CHECK_WRONG amendments per the published v6 receipt;
the first five were ruled PRESERVE by the independent implementation
reviewer, the sixth retargets C6's vacuous INFO-log capture to the real
`rate_limit` logger; semantics digest 9a84a416 constant across all six
receipts): C1/C2/C3/C4/C5 and C6's message leg DISCRIMINATING (marker probes, RED→GREEN),
C7/C10 PRESERVING (existing redaction pins + the #1112 battery), C8/C9/C11
NEW-SURFACE (fenced unwind, staged-abort compose, ABSORB exclusion — all
GREEN). New guard suite: grown AST census (fails closed on drift; two
verified false-positive pins documented) + fence/lock-order/late-writer/
delete-failure/staged-both-modes/G-1-four-variants legs. Blast radius:
test_1112 battery, test_1041, test_c15/c10 families, vault-path migration,
email service, upload path suites all green.


## Considered, not filed (second review round, PR comments 6984415094 / 6985516412)

| Item | Reason | Gate test failed | Re-evaluate when |
|---|---|---|---|
| The boot INFO `redact_url` pass-through is not emitted under the shipped launch paths (logging is configured in lifespan after the import-time limiter build) | The residual is real under an explicitly configured root logger, but narrower than the original PR text claimed; #1171 tracks it | A shipped launch path configures logging before the limiter import |
| `limits` imported directly but declared only transitively via `slowapi` | Every resolvable version in the allowed range exports `limits.errors.ConfigurationError`; the lock pins 5.8.0. No reachable failure today | necessity gate 2/3 (no reachable trigger or material impact) | slowapi drops the `limits` dependency or the lockfile is regenerated |
| Legacy transport (non-default) persists `json.dumps(result)` in `document_reindex_jobs.error` for staged aborts with duplicates | Legacy mode is off by default; duplicate ids ride along in the composed result | necessity gate 2 (no current caller depends on the previous guidance text) | a legacy-transport consumer is added |
| ~~`#1131` literal AC argv (`tests/test_1123_followup_*.py`) exits 4~~ | **RESOLVED in this feedback round (FB-023):** stable issue-named entrypoint files now re-export the C8/C9 discriminating checks; both literal argv commands exit 0 with 1 passed (gates/issue1131-ac-argv.log) | (row kept for history) | — |
| Reindex never refreshes `files.file_hash` (root cause of the false-duplicate settlement) | Pre-existing #1112 behavior outside this PR's scope; the unwind now uses the write-side hash so the residual is bounded | necessity gate 3/4 (pre-existing, not fixable without a schema/data migration) | a reindex path starts refreshing the row hash |
| Unwind acquire-side wait: the Lance write lock is held while awaiting the pooled SQLite write permit (untimed `Semaphore.acquire`), so peers can time out at 30 s | Same disclosed contention class as the held-window row; no deadlock (no permit holder awaits a Lance primitive). A bounded `wait_for` would need a new failure contract | necessity gate 2 (latency only, no data loss) | an observed 30 s peer failure under the default 2-worker deployment |
| `contextual_chunking.py:282` still logs the user filename at INFO on the opt-in contextual-chunking path | Pre-existing, outside the eight files changed here; flag defaults False and no default-level path reaches it | A non-default deployment enables it or the file enters PR scope |
| Container re-enable path for uvicorn access logs is implicit (CMD override), not documented | Native re-enable path is documented; container operators can override the CMD | A container operator reports the need |
| Boot-time upload-migration failure ERROR lines report lengths only; affected names are DEBUG-only | Redaction tradeoff documented; migration result is discarded by the startup caller | An operator needs row-level repair from default logs |
| Reindex / upload `logger.exception` sites still render OSError text (which can carry a user path) at ERROR | Pre-existing third-party exception text; the enqueued/reindex log arguments are bounded and persisted details are redacted | A default-level log capture demonstrates a leak through these sites |
| The ABSORB predicate evaluates `error_message LIKE 'DUPLICATE_FILE:%'` on an unindexed column (one SELECT per reindex job) | Cost is bounded for normal error counts; no query-plan regression measured | A mass-failure ingest produces evidence of a new scan class |
| The #1041 DEBUG helper (`_debug_upload_detail`) lives outside `_do_upload`'s pinned 4-call AST node | Guard granularity: currently DEBUG-only; a future default-level line routed through a helper would evade the count pin | A new log line is added to `_do_upload` via a helper |
| Test-quality residuals: census sanction granularity (line-keyed pins), stub-internal late-writer assertion, DEBUG-flood bodies, content-derived 40-bit digest, LIKE wildcard on a system-written column | All are disclosed limitations of guards/inherent accepted vocabulary; no reachable production failure was demonstrated | necessity gate 1/2 (no demonstrated failure) | an observed false-green or a new consumer of the bounded digest |
| Per-test early-lock acceptance waits and DNS hermeticity of the `.invalid` C6 address | Low test-quality residuals: the guards pass in CI and no nonconforming resolver was observed | necessity gate 1/2 (no demonstrated failure) | an observed flake or a resolver that answers `.invalid` |
