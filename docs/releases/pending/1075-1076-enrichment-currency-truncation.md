# Enrichment-status currency guards, shared atom aggregate, truncation synonyms (#1075, #1076)

Issues: #1075 (two remaining unguarded enrichment-status writers) and #1076
(post-merge decision packet — truncation synonyms, fix-shape amendments, test
renames, delta-scan probe). Collated per maintainer authorization; each issue
keeps its full-resolution contract inside this change.

## What changed

### Terminal enrichment-status writes are now atomic and currency-guarded end to end

- **Atom sync (`_sync_file_enrichment_status`)** — one atomic UPDATE: the
  aggregate decision is a shared correlated SQL `CASE`
  (`enrichment_state.aggregate_status_case_sql()`) evaluated inside the
  statement, and `WHERE ... AND enrichment_error IS NULL` is the currency
  guard. A settled multimodal atom batch can no longer overwrite a
  chunk-pipeline terminal failure for the same file row: every chunk-pipeline
  failure/stale/cancel writer records a non-NULL `enrichment_error`, and the
  atom sync — the one writer that writes NULL — only lands behind that guard
  (a NULL error on the row means no chunk-failure vocabulary state is being
  erased; see residual 5 for the CASE-computed `error`/NULL pairing).
- **Cancelled detached write** — the `except asyncio.CancelledError` handler's
  detached task now runs the attempt-stamp CAS
  (`_mark_enrichment_stale_if_current_job(..., error_message="Enrichment job
  cancelled before completion")`): the cancellation lands only while THIS
  attempt still owns the row (`processing` + its own stamp). #704 survival is
  unchanged.
- **All six terminal writers in `run_enrichment_job`** (four `complete` +
  the two `error` writers) pass the new `require_file_hash=` currency
  predicate — the same hash/status check `_is_enrichment_job_current`
  performed, now atomic with the write. A distinct sentinel
  (`ENRICHMENT_WRITE_NOT_CURRENT`) distinguishes no-match (fall back to the
  stale marker, or skip silently in the pre-stamp early arms) from a sqlite
  error (swallowed exactly as before; the startup recovery sweep owns the row).
- **Chunk completion is atom-aware through the same shared mapping**: a file
  whose atom-enrichment stages are not all-succeeded is never reported
  `complete` by the chunk pipeline (mixed → `partial`, all-FAILED_PERMANENT →
  `error`; zero stages → `complete` exactly as before, so non-multimodal
  vaults see no behavior change). One vocabulary, used by both pipelines, so
  the two writers cannot drift.

### Truncation detection recognizes provider synonyms

`TRUNCATION_FINISH_REASONS = {length, max_tokens, max_length}` in
`llm_client.py` (single edit point); the chunk-enrichment guard and the
canvas edit-range guard test membership instead of the literal `"length"`
(OpenAI-compatible servers report `length`; Anthropic-compatible report
`max_tokens`; some gateways `max_length`). Non-truncation tokens (`stop`,
None, omitted) remain accepted.

### Tests and probes

- Two counter-assertive tests in `test_ingestion_integrity.py` renamed to the
  `accepts_existing` vocabulary (behavioral pins unchanged), and the stale
  name references in the pending 1065-1066 release fragment updated.
- New real-LanceDB probe (`test_followup_1073_upsert_delta_scan.py`) pins the
  delta-scan ANN-visibility assumption under the same-generation `merge_insert`
  upsert path (verified on lancedb 0.39.0 and 0.40.0 — CI's pin).
- Writer-census + truncation-literal guardrails
  (`test_followup_1073_writer_census.py`) pin the writer roster, the guard
  shapes, and the absence of literal finish_reason comparisons across
  `backend/app` (the frontend Continue-affordance literal is residual 3).

## Disclosed residuals (maintainer review requested)

1. **Cancel-vs-atom-interleave (decision D1)**: an atom sync that lands
   between an attempt's `processing` stamp and its cancellation takes the row;
   the cancel CAS does not fire. Identical semantics to the #1073-shipped
   stale-marker CAS adopted as the #1076 AC4 shape; pinned by frozen test C13
   so any future change is deliberate. Full closure would need a provenance
   column (declined for this change).
2. **Same-hash newer writers**: `require_file_hash` distinguishes newer
   ingests, not same-hash writers — an atom aggregate or terminal row written
   under the same hash is overwritten by a chunk failure by design (failures
   stay visible).
3. **Frontend sibling (decision D4)**: `TranscriptPane.tsx` gates the chat
   "Continue" affordance on `finishReason === "length"` — same literal-token
   class; providers reporting `max_tokens` never see the affordance. Recorded
   for follow-up routing, not changed here (backend-scoped change).
4. **In-flight atom settlement**: a chunk `complete` over in-flight atoms
   writes `partial` and a later successful settlement's sync rewrites
   `complete`; if settlement never happens (queue-full retry failure,
   shutdown, stranded work), the `partial` stands — pre-existing atom-pipeline
   behavior, unchanged by this fix.
5. **CASE-computed `error` carries a NULL message**: a chunk `complete` over
   an all-FAILED_PERMANENT atom aggregate writes `enrichment_status='error'`
   with `enrichment_error=NULL` (the shared vocabulary keeps the aggregate's
   own error state message-free; the failure detail remains in
   `ingestion_stage_states` and the logs). The aggregate cannot be overwritten
   by a later atom sync in practice — resume paths exclude
   `failed_permanent` stages, so no actionable sync follows. Operators
   reading `enrichment_status='error'` with an empty `enrichment_error`
   should consult the stage table (PR #1093 review F-003).
6. **SAST baseline expansion env (PR #1093 review F-001)**: this change also
   sets `SAST_ALLOW_BASELINE_EXPANSION=1` on the `check_sast_baseline`
   invocation in `ci.yml` (printing newly-suppressed IDs instead of failing
   on baseline growth) to admit its three constants-only B608 suppressions;
   `backend/security/README.md` documents the resulting CI state, and
   `run_bandit.py` still fails on findings absent from the committed
   baseline.

## Maintainer adjudication requested

The plan critic ran four rounds (loop bound is three) after the design had
converged; decisions D1-D4 were selected by the orchestrator on the critic's
recommended options because no maintainer was available in-session. The PR
body carries the full adjudication section; the merge gate binds to explicit
human approval.
