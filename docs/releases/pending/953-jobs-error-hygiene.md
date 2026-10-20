# Job-queue error hygiene: legacy-row redaction, sync json guards, terminal-event closure, stable error codes, result_json masking (#953, #954, #955, #957, #958)

Collated follow-up PR closing the five open issues filed from the
swarm-pr-review of PR #951. All changes are backend; no schema migration; no
new settings; no frontend change.

## What changed

- **#953 — historical `jobs.error` rows are masked at read time, and so are
  the historical `result_json` error strings.** A shared helper
  (`redact_legacy_error_text` in `app/services/job_lease.py`) passes values
  that are stable codes through byte-identically (operators keep the codes
  they can act on) and replaces anything else with
  `REDACTED_LEGACY_ERROR: pre-redaction failure detail withheld; see
  server logs`. The stable-code vocabulary is `COMPILE_FAILED:`,
  `PAYLOAD_MALFORMED:`, `attempt_cap_exceeded:`,
  `lease_attempt_cap_exceeded`, `lease_disabled_rollback`,
  `admission rejected:` plus the #562 ingest codes. The
  `attempt_cap_exceeded:` entry is a COMPOSITION prefix — the pre-#792
  writer wrapped raw `str(exc)` in it — so it is checked remainder-aware:
  only a nested stable code may follow it. A companion helper
  (`redact_legacy_result_json`) applies the same discipline to
  `result_json`, dropping the message tail from pre-#958
  `transport_error:` / `orchestration_error:` / `persist_error:` /
  `lint_persist_error:` strings and masking raw reindex `details`
  entries (including the historical `ssrf_blocked:` tails, which carried
  the configured curator host). Both are wired at all four wiki/KMS row
  mappers. `GET /wiki/jobs`, `GET /wiki/jobs/{id}`, `GET /kms/jobs` and
  `GET /kms/jobs/{id}` no longer surface pre-fix `str(exc)` text in either
  column.
  **At-rest posture (deliberate):** raw historical text remains stored in
  the legacy tables, in `jobs` rows the boot sync copies forward, and in
  historical `result_json` payloads — the wiki/KMS job readers mask both
  columns, and the admin reindex readers mask the `result_json`
  `details` payload, and rows reintroduced by restoring a pre-fix backup
  are masked too (a one-time scrub migration would not cover that).
  **Scoped deliberately:** the admin-only reindex `error` column is NOT
  masked — it carries operator constants ("Interrupted by process
  restart", `ReindexOperatorGuidance` texts with dynamic counts) that no
  closed vocabulary can enumerate without drift. That column's historical
  raw text stays visible to admins, which is why those routes are
  admin-gated. The withheld detail for
  a `REDACTED_LEGACY_ERROR` row is not recoverable from the database; the
  originating exception was logged in full by the worker's
  `logger.exception` line at failure time (wiki/KMS compile processor
  poll-loop failure log, or the reindex/ingest worker log).
- **#954 — the legacy-sync anti-join tolerates malformed payloads.** Both
  `json_extract(j2.payload_json, '$.legacy_job_id')` evaluations in
  `_sync_missing_compile_job_rows` are wrapped in
  `CASE WHEN json_valid(payload_json) THEN ... END` (the #792/#951 store
  pattern): one malformed `queue='wiki'`/`'kms'` row no longer aborts the
  whole boot sync with `OperationalError: malformed JSON`.
- **#955 — terminal events survive the shutdown window and a restart.**
  (a) The failure arm's terminal commit and its `job_failed` publish now
  run as one threaded unit (`_fail_job_and_publish`), so a `stop()`
  cancellation delivered at the await can no longer land between them;
  the legacy auto-retry scheduling rides a done-callback on the same
  future, so that cancellation cannot discard it either. (b) On start,
  the processor re-announces wiki jobs that reached a terminal state
  within the last 30 seconds (`WIKI_EVENT_RECONCILE_WINDOW_SECONDS`,
  bounded to `_RECONCILE_MAX_ROWS` rows per store) in both stores — the
  comparison normalizes both stored timestamp formats (SQLite
  `CURRENT_TIMESTAMP` and Python `isoformat`) so the window is
  time-of-day bounded for every writer, and the janitor's cap-settle now
  stamps `completed_at` so those rows are visible to it. The fan-out is
  DEFERRED past startup (`_RECONCILE_DELAY_SECONDS`) because no SSE
  subscriber can exist during ASGI lifespan startup — deferring lets
  reconnected clients refetch. Duplicate terminal events after a quick
  restart are benign (consumers are refetch-driven). Threaded publishes
  (the fail-and-publish unit and the boot reconcile) are marshalled onto
  the app event loop via `call_soon_threadsafe` — the event bus's
  `put_nowait` is same-loop only. (c) `POST /wiki/jobs/{id}/cancel` now
  publishes `job_cancelled` on the wiki event bus (best-effort; the
  settle stays the source of truth).
- **#957 — ingestion and legacy-reindex failure sinks persist stable
  codes.** The ingestion worker's requeue/attempt-cap settles carry
  `redact_ingest_error` codes instead of `str(exc)`. The legacy reindex
  runner redacts all three persist arms with a NEW standalone
  `_redact_reindex_error` that mirrors the lease boundary's three-way
  branch (`ReindexOperatorGuidance` → verbatim; other exceptions through
  the #562 codes; trusted operator-constant strings verbatim) —
  deliberately NOT adopted inside `_run_reindex_job_row` itself, whose
  inline isinstance + `redact_ingest_error` calls are pinned by the o02
  census. The `result["details"]` list is redacted at the persist
  boundary (the message part after `file_id=<id>: ` must carry a stable
  code, and the #562 ingest codes count as stable; keys unchanged). Raw
  detail remains in the workers' `logger.exception` lines.
- **#958 — wiki compile `result_json` error strings are class-only.** The

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
  detail; operators correlate old failures via the server log from
  failure time (named in the fragment above).
- The boot reconcile re-announces terminal wiki jobs from the last 30
  seconds on every restart — bounded duplicate events, refetch-driven
  consumers — and the fan-out is deferred by 5 seconds so reconnected SSE
  clients actually receive it.
- `AdmissionRejected.reason` strings (`admission rejected: queue_full|...`)
  pass through unredacted — a closed set, not exception text.
- The admin reindex `error` column (`GET /documents/reindex/jobs` and
  `/{job_id}`) still serves its historical text verbatim — admin-gated, and
  the operator constants that share the column cannot be distinguished
  from raw exception text by a closed vocabulary. Re-evaluate if a
  per-writer code column is introduced.
- Same-class residuals NOT fixed here (tracked elsewhere): the repo-wide
  malformed-JSON census (25+ `json_extract`/`json.loads` sites in
  `background_tasks.py`, `documents.py`, `feedback_reranker.py` and
  `job_lease.py`'s `enqueue_deduped`) is tracked by #950; the
  manual-retry attempt budget is a product decision tracked by #956
  (untouched); `vector_store.py`'s `logger.warning` lines keep raw text
  (server logs, by contract).
- In lease-off (legacy) mode a `stop()` cancellation delivered at the
  fail-and-publish await no longer discards the delayed reset: the
  scheduling rides a done-callback on the same future as the threaded unit.
  What a full shutdown still costs is the backoff sleep itself — `stop()`
  cancels the retained `_bg_tasks`, so a row requeued less than one backoff
  interval before shutdown waits for the next process's claim (the row is
  `pending`, not lost). Re-evaluate if lease-off becomes a supported
  deployment.

## Verification

- 17 frozen acceptance checks (11 DISCRIMINATING RED→GREEN, 6 PRESERVING
  GREEN→GREEN) at the PR head; anchor receipt on issue #953 (comment
  6278699762).
- o02 / #562 / lease / wiki-processor / reindex suites green; full backend
  suite green except 2 documented Windows-environmental failures
  (draft_room symlink-privilege and POSIX-only traversal tests);
  `ruff check .` clean; bandit baseline flat (128, multiset-identical
  re-anchor, regenerated against the final tree).
- Feedback-round mutation probes (one per fixed review finding): reverting
  each fix turns its new check red — remainder-aware cap prefix, mapper
  result_json mask, threaded publish arm, ingest-code vocabulary, non-str
  guard, and the widened census.
