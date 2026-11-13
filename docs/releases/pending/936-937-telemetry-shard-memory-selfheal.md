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
  probed (#936 AC5). Retention honesty: retained shards are size-bounded
  (per-shard history caps) but NOT count-bounded — under per-incarnation
  hostnames (Docker container ids, Kubernetes pod names) every past
  incarnation's shard is kept until an operator removes it; a
  host-independent liveness signal (or a long foreign-stale window) is an
  open maintainer decision tracked in a follow-up issue. Until then,
  operators may delete stale foreign/legacy shards by hand once every
  worker runs the new format. **During a mixed-version rollout**, replicas
  still running the previous image keep applying the OLD mtime-only prune
  to ALL shards — including new-format shards of upgraded, live workers —
  so freshly written shard data may briefly disappear until the upgraded
  worker's next write-through flush recreates it (visibility-only, self-
  healing). Upgrade all replicas of a shared-registry deployment together
  where practical.
- Rollback: a single revert of this change restores the base behavior —
  the base reader ignores the new-format shard names and the optional
  `shard_flush_failures` key, and the base prune's mtime-only rule resumes
  (no schema, API, or config migration to undo). The optional
  `MEMORY_EMBED_HEAL_INTERVAL_SECONDS` env var can simply be unset.
- Per-class queue-wait/queue-depth history is capped at 200 samples per
  process on write and per shard at merge entry — the cross-shard
  aggregate keeps every shard's retained window (a merged-list trim would
  let the last-sorting shard monopolize the queue-wait average), so shard
  size and lock-hold time stop growing with process lifetime (#936 AC6).
- Every HELP/TYPE line and sample is generated from the `METRIC_*`
  constants — a rename can no longer desync the exposition from its
  samples (#936 AC7); the existing embedding-cache family gains the
  HELP/TYPE metadata lines it previously lacked (the sample itself was
  already emitted before this change).

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
  route) are now single-flight on one store-level lock **per process**
  (multi-worker deployments each run their own loop — duplicate embed work
  is idempotent and revision-guarded, so the cost is provider spend only);
  overlapping runs share one store-level embed-concurrency semaphore. The
  heal tick processes at most 200 rows per tick, bounding how long the
  single-flight lock is held (remaining rows retry on later ticks).
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
