# fix(reindex): partial files heal through every bulk path, failed lease jobs keep their diagnostics, dimension refusals stop burning retries, and the watermark tracks real progress (#963, #819, #820, #1020)

## What changed

Four follow-ups in the reindex/ingest job lifecycle, landed as one collated
fix (same subsystem, same job loop):

- **`partial` files are now eligible for every bulk healing path (#963).**
  The files-table producer has treated `status='partial'` as a
  content-available state since #693 (retrievable `parsed_text`, owns its
  `(file_hash, vault_id)` slot, "'partial' must stay eligible" at the
  producer, per-file retry already selects it) — but the bulk consumers still
  hardcoded the pre-#693 status sets, so the state most in need of healing
  could never reach it. The vault/global reindex selection (all four
  scope variants), the stranded-enrichment startup sweep (both arms), the
  multimodal enrichment backfill, and the supersession currency warning now
  all include `partial` alongside `indexed` (the #793 KMS-twin precedent,
  decision recorded at each site). Two docstrings that still described the
  pre-#693 predicates were corrected, and the reindex per-file body now
  takes the same module-level per-file retry lock the chunk-retry path uses
  (#513 W15 primitive, in-process), with the status snapshot and
  stale-cancel clear moved inside the lock so restoration always uses the
  status at actual ownership acquisition.
  - Fixed in passing (found by the frozen cancel-after-promotion leg): the
    reindex cancellation-restore UPDATE never committed — pool connections
    keep Python's deferred-transaction default and `release_connection`
    rolls back open transactions (issue #548), so a cancel-during-reindex
    silently stranded the file in `processing` instead of restoring its
    prior selectable status. The restore now commits.
- **Terminally failed lease reindex jobs expose the same per-file
  diagnostics as the legacy transport (#819).** For a failure that composed
  a `processed/failed/details` result, the lease settlement now persists
  that payload into `jobs.result_json` via a new optional `result` parameter
  on `JobLease.fail` (absent from the SQL entirely when `None`, so every
  other caller — ingest, wiki, KMS, draft, janitor — is byte-identical),
  after passing the details through the same #957 persist-boundary redaction
  the legacy arm applies. Stall-cap and janitor settlements have no composed
  result and keep the error-only shape. On the read side,
  `redact_legacy_result_json` no longer requires the literal substring
  `error` in the serialized payload before parsing (a failed row whose
  details carried raw text but whose JSON text lacked that substring was
  returned raw); its `details` masking is now gated at the PAYLOAD level —
  an integer `processed`+`failed` pair identifies the reindex result shape
  and masks ALL of its detail entries (shaped `file_id=N:` and shape-less
  raw text alike, matching base behavior), while wiki/KMS free-form
  `details` payloads stay byte-identical.
- **Deterministic dimension refusals settle on the first attempt (#820).**
  An ingest refused by the #691 dimension probe
  (`EmbeddingDimensionChangedError` → `DIMENSION_CHANGED`) cannot succeed on
  a later attempt — the live index dimension does not change back — yet both
  transports ran the generic retry ladder, re-parsing and re-embedding the
  whole document on every attempt before landing on the same terminal
  refusal. Both transports now short-circuit the refusal to the
  permanent-failure path on attempt 1 (legacy: after the shutdown guard;
  lease: unprefixed failure — no `attempt_cap_exceeded` — with the same
  terminal file-row write the cap path uses). Only `DIMENSION_CHANGED` is
  short-circuited: #1011 contention keeps its never-terminal predicate and
  every other class keeps the ladder. The permanent-failure log line now
  reports the real attempt count instead of a fixed "after N attempts".
- **The reindex progress watermark advances per embedding batch (#1020).**
  The #1010 watermark refreshed only after a whole file finished, while the
  stall watchdog cancels on watermark age — so a healthy large document on a
  slow embedder was watchdog-cancelled mid-file, the settle requeued the
  same oversized file, and the job died at the attempts cap without the
  reindex ever being able to complete. `embed_batch` now accepts a
  keyword-only `progress_cb` (default call unchanged for every other
  caller); the reindex path passes a non-raising callback that stamps the
  fenced watermark after every dispatched batch. A lost fence (zero-row
  stamp, or an ordinary stamp failure — conservatively the same loss path)
  is consulted before each queued batch dispatches (batches already inside
  a provider call complete), makes `embed_batch` raise the new
  `EmbeddingProgressLost` after the gather, and the processor restores the
  file's pre-ingest status exactly like a cancellation (never stranding it
  in `processing`) before the attempt stops on the lease-fence-lost path —
  no failed-scope mutation, no later file. Wedge detection and the fenced
  stale-stop are unchanged (frozen #1010/#1018 suites stay green).

## Notes for operators

- Vault-wide and global reindexes now pick up previously-unhealable
  `partial` files (the state with failed chunks) — a one-time wave of
  re-embedding for backlogged partial files is expected and correct.
- Lease-mode reindex failures visible via
  `GET /api/documents/reindex/jobs/{id}` now carry
  `processed/failed/details` in `result_json` for result-bearing terminal
  failures, matching what the legacy table always recorded.
- The per-file lock serializing reindex and chunk-retry is in-process, the
  same scope the #513 W15 primitive has always claimed (single app process
  per deployment).

## Test plan

- Frozen issue-tracer checks C1-C11 (arm's-length authored, 4 rounds of
  plan-critic amendments, anchor receipts published on #963): 7
  discriminating RED→GREEN (selection variants incl. vault+retry, both
  sweep arms, backfill, docstrings, supersession, settlement parity with
  raw-sentinel details, read-side mask floors, both #820 transports with
  unprefixed-refusal and lease-transient pins, watchdog-completes +
  three fence-loss legs incl. concurrency running-vs-queued) and 4
  preserving GREEN→GREEN (#957 redaction suite, sentinel-free settlement
  floor, transient ladder, #1010/#1018 stall suites).
- Blast radius: embedding-double/reindex families (21 files) and
  lease/wiki/KMS/enrichment families (49 files) green; ruff clean;
  bandit baseline re-anchored, count flat at 128, multiset identical.
