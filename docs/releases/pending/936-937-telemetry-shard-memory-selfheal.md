# Telemetry shard crash-safety + memory embedding self-heal (#936, #937)

## Telemetry shard I/O (opt-in `TELEMETRY_REGISTRY_DIR` deployments)

- A failed shard flush now surfaces as a rate-limited WARNING (first failure
  always warns; at most one per minute after) plus a new
  `ragapp_telemetry_shard_flush_failures_total` counter on `/metrics`,
  instead of silently vanishing counters at DEBUG (#936 AC1). The failing
  process's own in-memory count replaces its stale disk contribution, so a
  transient failure followed by recovery reads exactly once.
- Shard writes are atomic (temp file + `os.replace`): a concurrent scrape
  never observes a torn shard and a crash mid-write never strands one;
  crash-leftover `.tmp` siblings are reclaimed by the retention pass (#936
  AC2).
- The read side validates shard shape, types (bools are not counters),
  non-negative finite numbers, and the write-side evaluator-reason
  allowlist before merging anything into the unauthenticated `/metrics`
  exposition: a malformed or hostile shard is skipped with a rate-limited
  WARNING and the scrape stays HTTP 200 — never a 500, never an injected
  label. All label values are Prometheus-escaped (#936 AC3/AC4).
- The retention prune deletes only new-format, same-host shards whose
  owning pid is confirmed dead (Windows via query-only OpenProcess;
  never `os.kill`). Foreign-host, legacy-format, and unparsable-name
  shards are conservatively retained — a live-but-idle worker no longer
  looks like a Prometheus counter reset, and a cross-host pid is never
  probed (#936 AC5). Retained legacy shards are safe to delete by hand
  after every worker runs the new format.
- Per-class queue-wait/queue-depth history is capped at 200 samples on
  both the write and merge sides, so shard size and lock-hold time stop
  growing with process lifetime (#936 AC6).
- Every HELP/TYPE line and sample (including the new embedding-cache
  family) is generated from the `METRIC_*` constants — a rename can no
  longer desync the exposition from its samples (#936 AC7).

## Memory embedding self-heal (#937)

- New `MemoryStore.periodic_embed_heal_loop`, started by lifespan with the
  new `MEMORY_EMBED_HEAL_INTERVAL_SECONDS` setting (default 600s, bounded
  [60, 86400], forwarded by docker-compose): memory rows whose embedding
  is NULL heal on a bounded schedule once the provider recovers — no
  restart, no superadmin action. The healer selects NULL rows only (a
  staged embedding rebuild's valid old-generation rows are never
  re-embedded by every tick) and each row's embed runs inside the #939
  EMBEDDING admission + `MEMORY_EMBED_BUDGET_SECONDS` window; budget-
  rejected rows are deferred and a later tick retries them.
- All three backfill callers (lifespan startup task, heal loop, superadmin
  route) are now single-flight on one store-level lock; overlapping runs
  share one store-level embed-concurrency semaphore.
- A re-sent remember-directive that finds an existing NULL-embedding row
  re-attempts its embed (non-NULL rows untouched).
- Every post-embed degradation WARNING — create/update/remember, outcome
  and exception arms — names the actual healing mechanism in effect
  (startup backfill task, periodic embed-heal task, superadmin backfill
  route).

## Mock-contract adaptation

- `test_a04_memory_backfill_single_flight.py` / `test_a04_memory_backfill_http.py`
  store stubs gained `get_backfill_lock` (the single-flight lock moved from
  the memories route module onto `MemoryStore`).
