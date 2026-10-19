# Job-queue error hygiene: historical-row redaction, sync guards, terminal events, ingest/reindex codes, result_json class-only (#953, #954, #955, #957, #958)

Collated follow-up PR closing the five open issues filed from the swarm
review of PR #951. All changes are backend; no schema migration, no new
settings, no frontend change.

## What changed

- **#953 — historical `jobs.error` rows are masked at read time.** The
  wiki/KMS row mappers (lease and legacy modes, `wiki_store.py` /
  `kms_store.py`) pass values that already match the stable-code
  vocabulary (`COMPILE_FAILED:`, `PAYLOAD_MALFORMED:`,
  `attempt_cap_exceeded:`, `lease_attempt_cap_exceeded`,
  `lease_disabled_rollback`, `admission rejected:`) through byte-identically
  and replace anything else with
  `REDACTED_LEGACY_ERROR: pre-redaction failure detail withheld; see
  server logs`. `GET /wiki/jobs`, `GET /wiki/jobs/{id}`, `GET /kms/jobs`
  and `GET /kms/jobs/{id}` no longer surface pre-#951 `str(exc)` text.
  **At-rest posture (deliberate):** raw historical text remains stored in
  the legacy tables, in `jobs` rows the boot sync copies forward, and in
  historical `result_json` payloads — every reader is masked, and rows
  reintroduced by restoring a pre-fix backup are masked too (a one-time
  scrub migration would not cover that). The withheld detail for a
  `REDACTED_LEGACY_ERROR` row is not recoverable from the database; the
  originating exception was logged in full by the worker's
  `logger.exception` line at failure time (wiki/KMS compile processor
  poll-loop failure log, or the reindex/ingest worker log).
- **#954 — the legacy-sync anti-join tolerates malformed payloads.** Both
  `json_extract(j2.payload_json, '$.legacy_job_id')` evaluations in
  `_sync_missing_compile_job_rows` are wrapped in
  `CASE WHEN json_valid(payload_json) THEN ... END` (the #792/#951 store
  pattern): one malformed `queue='wiki'`/`'kms'` row no longer aborts the
  whole boot sync with `OperationalError: malformed JSON`.
- **#955 — terminal events can no longer be lost.** (a) The failure arm's
  terminal commit and its `job_failed` publish now run as one threaded
  unit (`_fail_job_and_publish`), so a `stop()` cancellation delivered at
  the await can no longer land between them. (b) On start, the processor
  re-announces wiki jobs that reached a terminal state within the last 30
  seconds (`WIKI_EVENT_RECONCILE_WINDOW_SECONDS`) in both stores — the
  comparison normalizes both stored timestamp formats (SQLite
  CURRENT_TIMESTAMP and Python isoformat) so the window is time-of-day
  bounded for every writer. Any terminal-but-unpublished row gets its
  event, and SSE consumers refetch. Duplicate terminal events after a
  quick restart are benign (consumers are refetch-driven). Threaded
  publishes (the fail-and-publish unit and the boot reconcile) are
  marshalled onto the app event loop via `call_soon_threadsafe` — the
  event bus's `put_nowait` is same-loop only. (c) `POST /wiki/jobs/{id}/cancel` now publishes
  `job_cancelled` on the wiki event bus (best-effort; the settle stays the
  source of truth).
- **#957 — ingestion and legacy-reindex error sinks persist stable codes.**
  The ingestion worker's requeue/attempt-cap settles carry
  `redact_ingest_error` codes instead of `str(exc)`. The legacy reindex
  runner redacts all three persist arms via the lease boundary's three-way
  branch (`ReindexOperatorGuidance` verbatim; other exceptions through the
  #562 codes; trusted operator-constant strings verbatim), and the
  `result["details"]` list is redacted at the persist boundary (message
  part after `file_id=<id>: ` must carry a stable code; keys unchanged).
  Raw detail remains in the workers' `logger.exception` lines.
- **#958 — wiki compile `result_json` error strings are class-only.** The
  curator transport, compiler orchestration, and claim-persist sinks emit
  `transport_error: <ClassName>` / `orchestration_error: <ClassName>` /
  `persist_error: <ClassName>` — never the exception message. A widened
  source census guardrail pins zero raw `{e}`-style interpolations on that
  surface (one named exemption: the by-design `ssrf_blocked:` arm, which
  reports the blocker's short detail).
- **#956 — a manual retry grants a full fresh attempt budget.** The
  lease-mode `retry_job` in both stores now resets `attempts` to 0
  alongside the existing status/error/run_after/lease reset. Before, a
  job that had reached `jobs_max_attempts` got exactly one claim after a
  manual retry (`JobLease.claim` pre-increments attempts and the cap
  settle fires on `attempts >= jobs_max_attempts`) before settling
  failed again — the retry button carried no budget. Chosen semantics:
  an operator-initiated retry is an explicit decision to try again, so
  the next `jobs_max_attempts` claims are real attempts (and the backoff
  ladder restarts with the cleared `run_after`). The janitor's reclaim
  accounting is a separate SQL path and is unchanged; the KMS store
  keeps parity although no KMS retry route exists yet.

## Known caveats

- Historical `REDACTED_LEGACY_ERROR` values cannot be expanded back into
  detail; operators correlating an old failure use the server log from the
  failure time.
- The boot reconcile re-announces terminal jobs from the last 30 seconds
  on every restart (bounded duplicate events, refetch-driven consumers).
- The `ssrf_blocked:` curator arm still carries the blocker's short
  message by design (it names the blocked URL class for the operator).
- `AdmissionRejected.reason` strings (`admission rejected: queue_full|...`)
  pass through unredacted — closed-set reason codes, not exception text.

## Verification

- 17 frozen acceptance checks (11 DISCRIMINATING RED→GREEN, 6 PRESERVING
  GREEN→GREEN) at the PR head; anchor receipt on issue #953 (comment
  6278699762).
- o02 / #562 / lease / wiki-processor / reindex suites green; full backend
  suite green except 2 documented Windows-environmental failures
  (draft_room symlink-privilege and POSIX-only traversal tests);
  `ruff check .` clean; bandit baseline flat (128, multiset-identical
  re-anchor).
