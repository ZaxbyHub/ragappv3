# Release fragment — lifecycle drain, shutdown budget and bounded exit (#854, #799)

Issues: #854 (stop() applies its timeout per drain phase), #799 (Workstream F
PR 6 of 6 — lifecycle drain and readiness; its audit provenance absorbs #854).

## What changed

**One aggregate shutdown budget everywhere (#854).**
`BackgroundProcessor.stop(timeout)` now computes a single deadline and grants
every awaited stage only the REMAINING budget — the pre-drain task gathers,
the four drain phases (queue, enrichment, atoms, lease settle), the
worker/sweep cancellations, the three lease-release/retry-reset maintenance
calls, and the trailing vector-store optimize (kept on the MAIN loop under
`wait_for`; a timed-out LanceDB optimize continues only on LanceDB's native
async runtime, which is never joined by CPython's interpreter-exit). The
composed worst case is the deadline itself, not a per-phase multiple of it;
the ONE exception is the maintenance block, which reserves a single shared
slice (min(5 s, timeout)) when it starts so the lease hand-back still
happens after the drain phases consumed the budget — the composed worst
case is therefore the deadline PLUS that one slice.
The lifespan wraps its whole shutdown sequence (email, residency tasks, file
watcher, the four processor stops, admission, store/pool closes, the
upload-commit drain) in a 60 s aggregate budget
(`_SHUTDOWN_BUDGET_SECONDS`); only `BackgroundProcessor.stop` takes a timeout
argument — every no-argument stage runs through a bounded `_stop_stage`
helper, and the two synchronous closes run via `to_thread` under the same
budget. The turn-finalizer drain keeps its OWN enforced budget (5 s producer
retirement + 10 s drain, `TURN_PRODUCER_SHUTDOWN_TIMEOUT_SECONDS` /
`TURN_FINALIZE_DRAIN_TIMEOUT_SECONDS`).

ONE derivation, quoted VERBATIM by docker-compose.yml, both .ps1 scripts,
the lifespan module and the PR body — the composed shutdown envelope is:

    enforced: 20 s (uvicorn request drain) + 65 s (lifespan aggregate) + 15 s (turn-finalizer drain) = 100 s; allowance: 30 s (SQLite-family executor-join tail) + 5 s (teardown) = 35 s; total 135 s

(the 65 s is the 60 s lifespan aggregate plus the 5 s outer cap on the
processor stop; the 15 s is the turn-finalizer drain's 5 s producer
retirement plus its 10 s drain.)

`scripts/check_p03_shutdown_envelope.py` owns that arithmetic: it reads
every ENFORCED term from the code that enforces it (the Dockerfile CMD's
uvicorn grace, the lifespan module's shutdown budget and processor-stop
cap, and the turn-finalizer drain's own constants), requires every
force-kill limit to sit strictly above the total, and fails when a surface
stops quoting the derivation verbatim. The two ALLOWANCE terms are declared
in that script as policy: no code bounds the executor-join tail (the
abandoned-parse worker keeps running) or the interpreter teardown.

The compose grace is 150 s and the native stop wait is 140 s — both strictly
above the 135 s composed envelope, so a shutdown that stays inside the
enforced 100 s is never force-killed mid-flight.

**The residency settle ends on its bound, not on a task's cooperation.**
Awaiting a residency task DIRECTLY forwards the enclosing scope's
cancellation into it, so a task that suppresses that cancellation never
completes and the whole shutdown hangs until the outer force boundary. The
settle now waits with `asyncio.wait`, which returns the still-pending set
instead of forwarding the wait, so a task that refuses to die costs the
settle its budget and nothing more. The same shape applies to every
lifespan shutdown STAGE: `asyncio.wait_for` forwards the timeout's
cancellation into the stage and then waits for that cancellation to land,
so a stage that suppresses it would hold the sequence past the aggregate
budget; the stages are now waited for with `asyncio.wait` and a stage that
overruns is cancelled and left detached. The shutdown SEQUENCE is therefore
always bounded (the enforced 100 s); the PROCESS EXIT is bounded by the
bounded tail exit in State A (`os._exit`, so a detached task never delays
it) and by the process-level force boundary in State B (the compose grace
150 s / native stop wait 140 s). The settle's own warning names that.

**A bounded tail exit.** After every recovery-critical workflow completes
(deny-by-default `critical_shutdown_work` registry EMPTY, turn finalizers
drained, upload-commit registry empty — read at gate time; the DB pool
state is a logged cleanup outcome, not a firing condition)
and logging is flushed, the lifespan's final step exits immediately instead
of joining whatever non-critical worker threads are alive (abandoned parses
past their enforced 300 s await, a curator call past its between-calls
shutdown flag, file-watcher scans). `RAGAPP_BOUNDED_EXIT=1` is set ONLY by
the Dockerfile runtime image and the native launchers; the test suite pops
it (conftest) so embedded lifespans never take this path. If critical work
is incomplete at the tail (a live-turn finalizer timeout keeps the pool open
per #1202's durability contract; upload compensation still in flight per
PRR-020's startup-recovery contract) the gate never fires and those
deliberate recovery contracts govern — disclosed as the data-safety
preference over a hard exit.

**Thirteen registration arms across seven files.** user-delete (settlement,
rollback/restore AND both committed-finalization paths — 1 arm), document-upload
artifact cleanup and post-registration compensation (4 arms), vault
rollback/restore (3 recovery arms) plus the vault post-commit audit tail
(1 arm), draft-promotion shielded compensation, memory-transaction
commit/rollback, turn finalization, and upload commits run inside the registry
while active. The arm counts are pinned by
`backend/tests/test_p03_shutdown_ownership.py::TestRegistrationMapPin`, so a
deleted or added arm fails CI.

**AC12 conformance — resource-stall/State-A path.** Under the shipped
launchers, when drain or shutdown stages exhaust their budgets, the
bounded lifespan sequence continues to the tail gate. With
`RAGAPP_BOUNDED_EXIT=1`, an empty deny-by-default critical-work
registry, an empty upload-commit registry, and
`turn_finalizers_drained=True`, the gate invokes `os._exit(0)` regardless
of `pool_closed`; process exit reclaims open pool connections and skips
executor and interpreter joins. The enforced horizon to the gate is 100
seconds from stop delivery — 20 seconds request drain, 65 seconds
lifespan/processor envelope, and 15 seconds turn-finalizer drainage —
before Compose's 150-second stop grace and the native 140-second force
wait. `pool_closed` is logged as a resource-cleanup outcome, not a
durability precondition. Committed SQLite WAL transactions survive
process-crash recovery; uncommitted transactions are abandoned. **State B
remains an explicit exception:** incomplete turn finalization, upload
compensation, or other registered recovery-critical work prevents the
tail exit and may be force-terminated by the outer boundary under the
disclosed durability preference. No universal pre-force-exit claim is
made for State B or for direct uvicorn runs without the bounded-exit
opt-in.

**Container/native envelope (#799 AC5/#854).** Dockerfile CMD gains
`--timeout-graceful-shutdown 20` (integer seconds — uvicorn rejects unit
suffixes) and `RAGAPP_BOUNDED_EXIT=1`; docker-compose moves to
`stop_grace_period: 150s` with the derivation comment (the composed
envelope is 135 s — enforced 100 s (20 request drain + 65 lifespan
aggregate + 15 turn-finalizer drain) plus 35 s of allowance (30 SQLite-family
join tail + 5 teardown) — and the native stop wait is 140 s; both limits sit
strictly above the composed envelope, and the State-B critical-incomplete
path is named as the deliberate exception).

**Windows graceful stop (#799 AC4).** start-services.ps1 launches the
backend through `scripts/win_grouped_backend.py` (CREATE_NEW_PROCESS_GROUP
so uvicorn is a group leader; pid + approximate FILETIME start marker in
`.cache/backend.pid`). stop-services.ps1 (and start-services' own
Stop-ProcessOnPort) verify the marker (pid AND start time — a reused PID
fails the gate to the ungrouped fallback), then
`scripts/win_send_ctrl_break.py` — a sacrificial sender that detaches its
own console, attaches to the backend's, and delivers a TARGETED CTRL_BREAK
to the nonzero group — waits up to 140 s (above the 135 s natural worst
case), and only then force-stops.
CPython maps CTRL_BREAK to SIGBREAK; uvicorn handles SIGBREAK. Host-verified
end to end: graceful exit in 0.4 s with the full lifespan shutdown sequence
(including the bounded tail exit) in the backend log; the sender and the
non-group wrapper parent survived.

**Lifecycle resilience fixes (#799).** The editorial client is constructed
inside `_prime_editorial`'s try (a misconfigured pair is contained with a
warning instead of escaping the task); the shutdown loop over residency
tasks absorbs any Exception (a task that died with a natural exception no
longer skips the processor stops, admission release, and store/pool
closes); `_create_llm_clients` degrades a URLBlocked factory (live-DNS SSRF
guard) on BOTH legs instead of failing startup; the same guard covers both
settings.py late-activation sites (an SSRF-rejected endpoint skips
activation instead of 500-ing the save); a draft_pipeline compile factory
call moved inside its owning try with a None-guarded finally.

**Readiness truth (#799).** A FAILED upload migration (the "continuing
anyway" path) now records `app.state.upload_migration_ok = False` and
`/api/healthz` reports 503 with "upload migration failed at startup";
absent attribute stays inert.

**Operator guidance and warning truth.** The FTS-missing error no longer
points at the nonexistent `VectorStore._ensure_fts_index()` — it names the
real remediation (admin-triggered full reindex recreates the FTS index, or
disable hybrid search). main.py's admin-token warning is rescoped to the
one reachable corner (USERS_ENABLED=True with the published bootstrap
credential, which config deliberately permits as the first-admin
bootstrap); the unreachable JWT-default warning is deleted (config refuses
to start in every state it described); the CORS wildcard warning states the
real exposure (Starlette reflects the Origin with allow_credentials)
instead of the false browser-rejection claim.

**Executor lifecycle ownership.** The curator offload executor gains a
recreate-on-demand accessor + a cooperative between-propose-calls shutdown
flag (set at the lifespan wiki stage, reset at startup); the parse executor
gains the same accessor; both shut down (wait=False, cancel_futures=True)
at shutdown so queued work never extends interpreter exit — mirroring the
existing auth/wiki-lint/redis conventions and keeping same-process lifespan
restarts working.

## Tests

- backend/tests/test_p03_lifespan_resilience.py (C1/C2/C3/C7/C8 checks)
- backend/tests/test_p03_upload_migration_readiness.py (C6 ast + behavioral + success/inert legs)
- backend/tests/test_p03_db_transaction_pin.py (C9 removal pin)
- backend/tests/test_lifecycle_drain_budget.py (C12 composed/drains/optimize/maintenance/fast)
- backend/tests/test_p03_lifecycle_behavioral.py (C13/C14/C15/C16 behavioral)
- backend/tests/test_p03_shutdown_ownership.py (registry, tail-gate matrices, executor recreation, env pop pin)
- scripts/check_p03_graceful_shutdown.py (C5 structural; wired into the CI Quality contracts job by this PR)
- scripts/check_p03_shutdown_envelope.py (new in this PR; wired into the CI Quality contracts job — it owns the ONE envelope derivation: it reads every ENFORCED term from the code that enforces it, declares the two ALLOWANCE terms explicitly, and fails if the compose grace, either native stop wait, or any surface's quoted derivation drifts from the 135 s composed envelope)

UX note: stop-services.ps1 now waits up to 15 s for non-backend ports
(frontend/Ollama) before force-stopping, instead of force-killing
immediately — a deliberate trade for the graceful-backend default.
