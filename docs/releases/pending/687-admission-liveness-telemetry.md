---
issue: 687
title: Chat admission control never leaks slots, loses wakeups or misreports saturation; optional Redis cannot starve the app
---

# Admission control liveness, accounting and telemetry match the documented contract

Workstream A PR 5 of 7 (audit remediation, frontier audit 2026-09-23).

## What changed

- **A cancelled queued admit can no longer leak a budget slot**
  (#687 / T1-28-S-01). The waiter's lane registration and both pre-park
  awaits (the foreground-preemption read and the scheduling yield) now sit
  inside the `try` whose `CancelledError` handler unregisters the waiter.
  Previously a cancellation delivered during those awaits left a live
  orphan waiter that the pump later admitted into a renewing lease nobody
  released — a slot leaked forever.
- **A release landing mid-pump is no longer a lost wakeup** (T1-28-S-02).
  The hub's pump latch gained a dirty flag: a `schedule_pump` call that
  arrives while a pump is mid-await marks the hub dirty, and the pump's
  teardown re-schedules one more pass, so the queued waiter is admitted
  without any further event. Previously the no-op schedule plus the
  pump's stale occupancy read could park a deadline-less waiter forever
  while the slot was free.
- **The shared-store budget is atomic and clock-skew-proof**
  (T1-28-S2-10, T1-28-S-12). `AdmissionStore.try_acquire` is now ONE
  atomic budget-bounded acquire; the Redis store implements it (plus
  occupancy, sweep and refresh) as Lua scripts stamped with the Redis
  server clock (`TIME`; Redis >= 6 in practice — the pinned redis-py
  speaks RESP3/`HELLO`, which Redis 5 rejects, so a Redis 5 store ends up
  `degraded`). Two replicas can no longer both
  acquire against the same budget slot, and a replica whose host clock
  runs fast can no longer sweep another replica's live holder.
- **Foreground preemption now fires in default deployments**
  (T1-28-S-04). `from_settings` maps CHAT and BACKGROUND onto one
  LLM-device budget key sized `max(chat, background)` — both contend for
  the same thinking LLM, so a blocked chat stream logically evicts one
  local background holder (its task keeps running). Documented
  consequence (docs/operations.md): under full chat occupancy the
  background queue bound tightens from 64 to
  `ADMISSION_QUEUE_MAX_SIZE − chat holders`, and background admits queue
  behind chat under sustained load.
- **Evicted leases stop renewing quietly** (T1-28-K-08). Preemption
  releases the store holder and stops the lease's renewal task, and a
  preempted lease's renewer exits silently on its next refresh miss —
  the false `lost its slot mid-flight` warning (which blamed the store
  for a deliberate eviction) can no longer fire.
- **Queue-wait telemetry records the observed depth** (P01-SK2-06). Both
  production `record_queue_wait` call sites (durable producer and
  non-durable stream) sample the chat queue depth they joined and pass
  it as `depth=`, so `ragapp_queue_depth` stops rendering a constant 0.
- **A queued non-durable stream heartbeats while it waits**
  (TQ-budget-all-05). The admission enter runs on a child task and the
  generator yields `:` heartbeat comments on every interval while
  queued, so idle proxies no longer drop connections during queueing.
  Client disconnect during queueing frees the slot cleanly.
- **An admission 503 is no longer logged as an unhandled exception**
  (T1-28-K-07). The non-stream chat route lets `HTTPException` pass to
  FastAPI ahead of its broad `except` handler.
- **Optional Redis cache calls cannot starve the app** (T1-02-S2-02).
  `redis_call` runs on a dedicated bounded thread pool (8 workers,
  `thread_name_prefix="redis-io"`) and the three sync cache clients
  (embedding L2, query-transform, query-planner) set socket timeouts
  derived from `REDIS_IO_TIMEOUT_SECONDS` — timed-out cache threads can
  no longer consume the shared default executor that unrelated
  `asyncio.to_thread` work uses, and abandoned threads free themselves
  at the wrapper's deadline.
- **The queue-bound docstring matches the enforced expression**
  (T1-28-K-12). `admission_queue_max_size` now documents the enforced
  bound (in-flight holders for the budget key plus queued waiters for
  the class); the behavior itself is unchanged and stays pinned by the
  existing queue-full test.

## Operational notes

- CHAT and BACKGROUND now share the `llm` budget key
  (`max(ADMISSION_CHAT_BUDGET, ADMISSION_BACKGROUND_BUDGET)`); instant,
  embedding, reranking and vision keys are unchanged. See
  docs/operations.md for the tightened background queue bound under chat
  saturation and the rolling-upgrade notes (Redis >= 6 in practice for
  the TIME-based scripts; mixed-version replicas admit on disjoint key
  sets until old replicas drain — `DEL admission:chat
  admission:background` afterwards if you want the orphaned hashes
  gone).
