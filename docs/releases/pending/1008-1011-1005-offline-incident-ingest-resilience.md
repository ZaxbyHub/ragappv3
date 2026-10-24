# Offline-incident ingest resilience: NLTK bake, embed probe capacity, stalled-lease release, write-lock contention, email metadata bounds (#1008, #1009, #1010, #1011, #1005)

Issue: #1008, #1009, #1010, #1011, #1005 (offline-deployment incident
class: a fresh container silently parsed every document to zero elements,
deep health vouched for embedding throughput it had never exercised, a
live-but-stalled reindex worker held its lease forever with no
machine-readable signal, a routine index build terminally failed colliding
ingests, and uncapped email metadata columns carried the #1002 snippet
wedge onto the documents metadata arm).

## What changed

- **#1010 — stalled reindex lease release.** Reindex jobs now carry a
  millisecond-stamped `jobs.progress_at` watermark
  (`backend/app/services/job_lease.py`, added by guarded `ALTER TABLE` on
  pre-existing deployments). The reindex worker loop
  (`backend/app/services/background_tasks.py`,
  `_reindex_lease_worker_loop`) runs each job under a deadline-driven
  `_stall_watchdog` that cancels the run AND its heartbeat when either the
  watermark went stale while the row is still ours (watchdog-first, the
  primary release at the configured budget) or the row no longer belongs
  to this attempt (janitor-first backstop for a dead watchdog); a one-shot
  `_stall_cancel_flag` plus `asyncio.current_task().cancelling() == 0`
  keeps task-level cancellation (shutdown, teardown) from ever being
  swallowed as a stall. The cap-aware `_settle_stalled_reindex` requeues
  below the attempts cap with the stable reason
  `lease_stall_reclaimed: no per-file progress within
  jobs_stall_timeout_seconds` (registered in job_lease's stable-prefix
  vocabulary) and narrows `$.retry_file_ids` to the interrupted +
  remaining files; at/over the cap it settles terminally as
  `attempt_cap_exceeded: lease_stall_reclaimed: ...` with `completed_at`
  stamped. The lease janitor (`job_lease.reclaim_expired`) gained a stall
  arm — a parenthesized expiry group with the queue filter outside it — so
  a live-heartbeat no-progress row is also reclaimed by the backstop
  (below cap: pending with no error rewrite; at cap: the
  `lease_attempt_cap_exceeded` terminal settle). Both reindex job readers
  (`backend/app/api/routes/documents.py`, `get_reindex_job_status` and
  `list_reindex_jobs`) project a machine-readable `stalled` flag (CASE on
  `COALESCE(progress_at, started_at, created_at)` against the
  `_reindex_stall_cutoff()` helper). Cancellation is now mutation-aware in
  `DocumentParser`-sibling `process_existing_file`
  (`backend/app/services/document_processor.py`): an undisturbed cancel
  restores the prior indexed/partial status, a disturbed reindex cancel
  settles to a selectable `error` with the new
  `INGEST_ERROR_REINDEX_INTERRUPTED` code, and a disturbed upload cancel
  stays retryable for its durable job.
- **#1011 — vector write-lock contention.** `_acquire_write_lock`
  (`backend/app/services/vector_store.py`) now raises the typed
  `WriteLockTimeoutError(VectorStoreError)`, and
  `classify_ingest_error` (`backend/app/services/document_processor.py`)
  maps it to the stable code `VECTOR_STORE_LOCK_TIMEOUT` (bounded
  cause-walk, checked before the generic `VectorStoreError` branch) so
  contention stays distinguishable from a genuine store rejection.
  `_INGEST_ERROR_REASONS` and the #562 guardrail's shipped-code set
  (`backend/tests/test_issue562_guardrail_no_raw_ingest_error_persist.py`)
  carry both new codes. Both processor twins exempt contention from
  per-attempt status/phase=`error` writes (undisturbed falls to the #704
  prior-status restore; a disturbed upload row stays retryable; a
  disturbed reindex row settles to a selectable error). The lease
  settlement (`_process_ingest_job_row`) and the legacy
  `_handle_failure` ladder both use an exponent-bounded backoff
  (`retry_delay * 2**min(attempt-1, 6)`, floored at
  `write_lock_timeout_seconds`, capped at `max(60s, lock window)`), and
  contention is never terminal while the worker lives (the attempt cap is
  bypassed). In the legacy ladder's backlog-full branch a contention task
  persists its file as `status='pending', phase='queued'` (fenced UPDATE,
  no in-memory queued reservation) for the stranded-upload recovery sweep.
- **#1009 — deep-health probe embedding capacity.**
  `EmbeddingService.embed_probe`
  (`backend/app/services/embeddings.py`) sends an ingest-shaped LIST
  payload (`{"inputs": [...]}` TEI, `{"input": [...]}` openai/modern
  ollama; the legacy prompt dialect falls back to one configured-size
  text and names the fallback in the error), built from
  `embedding_probe_batch_count` x `embedding_probe_batch_chars` synthetic
  chunk-sized inputs, and validates the response per dialect
  (`_validate_probe_response`, count parity deliberately not required).
  `_int_probe_setting` reads the knobs MagicMock-safely. Defaults
  (4 x 512) cross-validate against `embedding_batch_max_chars`
  (`backend/app/config.py`).
- **#1008 — NLTK offline bake.** The backend Dockerfile stage
  (`Dockerfile`) bakes the `punkt`/`punkt_tab`/`averaged_perceptron_tagger`
  corpora into `/usr/local/nltk_data` (ENV `NLTK_DATA`), with a mandatory
  pre-flight `nltk.data.find` assertion that fails the build when the
  downloader could not reach a mirror. `DocumentParser.parse` logs a
  zero-elements diagnostic naming NLTK/tokenizer data for non-empty files
  so a missing-corpus container is tellable from an empty document. A new
  CI step (`.github/workflows/ci.yml`, "Offline first-parse runtime smoke
  (issue #1008)") runs `scripts/docker_smoke/offline_parse_probe.py`
  against the built image with `--network none` and asserts the first
  parse returns elements offline.
- **#1005 — email metadata snippet bounds.** The documents ranked-search
  metadata arm (`backend/app/api/routes/documents.py`) wraps the
  `email_subject` (FTS column 4) and `email_sender` (column 5) snippets in
  `CASE WHEN length(CAST(<col> AS BLOB)) <= FTS_SNIPPET_MAX_COLUMN_BYTES
  THEN snippet(...) END` — the same NUL-proof guard as the #1002 content
  arm; an over-ceiling header skips `snippet()` entirely and the excerpt
  falls through the existing COALESCE fallbacks. The k7 census
  (`backend/tests/test_k7_snippet_guard_census.py`) exemption became a
  tri-state: an explicit bounded-metadata column allowlist, `None` for
  invalid/out-of-range/unknown-table targets (unguarded — fail loud), and
  the dynamic `{col}` helper form as the only bounded-metadata
  compatibility case.
- **Settings.** `jobs_stall_timeout_seconds` (60..7200s, default 900; must
  exceed `jobs_lease_reclaim_timeout_seconds` AND
  `document_parse_timeout`) and the probe knobs are declared in
  `backend/app/config.py`; the stall budget is wired through the admin
  settings surface (`backend/app/api/routes/settings.py`:
  SettingsUpdate/ALLOWED_FIELDS/SettingsResponse/_build_settings_dict)
  and the startup replay list (`backend/app/lifespan.py`
  NEW_DIRECT_KEYS), mirroring the sibling job-lease knobs.
- Supporting (non-frozen) tests ship alongside the frozen acceptance
  checks: `backend/tests/test_incident_resilience_support.py` (#1010/#1011)
  and `backend/tests/test_incident_resilience_support2.py`
  (#1009/#1008/#1005).

## Disclosed limitations

- The `write_lock_timeout_seconds` 300s ceiling is RETAINED: contention is
  handled by the contention-aware never-terminal requeue (floored/capped
  backoff, attempt-cap bypass) rather than by raising the wait — a
  maintainer decision is requested on #1011 (AC14) on whether the ceiling
  should move.
- A contention-marked row whose worker DIED (process crash mid-contention)
  still cap-settles at the janitor's `lease_attempt_cap_exceeded` arm —
  the crash path has no live worker to keep requeueing; same #1011
  decision comment applies.
- The probe defaults (4 inputs x 512 chars) are a light capacity smoke,
  not the full ingest shape; operators can raise them toward the real
  batch shape (`embedding_probe_batch_count` /
  `embedding_probe_batch_chars`, cross-validated against
  `embedding_batch_max_chars`).
- Wiki/KMS/ingestion queues do not yet publish progress watermarks — the
  stall watchdog and `stalled` reader flag are reindex-only per the #1010
  issue scope.
- The email metadata guard is read-side only: `email_subject` /
  `email_sender` remain stored unbounded up to the whole-message cap; the
  length guard bounds the snippet cost, not the stored column.
- The NLTK bake requires network (or a proxy/mirror via Docker build args)
  at image build time; there is deliberately no skip fallback — the
  pre-flight `nltk.data.find` assertion fails the build instead.
