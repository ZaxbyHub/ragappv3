# Draft gates: production-cause analysis (F3 re-qualification, 2026-09-20/21)

## Outcome

Updated after the retry ladder completed: `gate-source-only-rewrite` is PASS —
draft 7's compile finished its full lifecycle once the server warmed (job 28:
revision with 33 citations, findings, export; requa3e_rewrite_results.transcript.json).
`gate-mixed-source-compose` remains FAIL on the final build (67527cd9), and
`journey-draft-compose-rewrite` is recorded pass-with-limitation on that basis.
The thinking endpoint itself is restored and verified (streamed, cited, durable
thinking turns — requa2_thinking.transcript.json). The mixed-source compose
pipeline cannot complete its standards stage against the RESTORED server's
serving profile. Cause chain, all measured live:

## Cause chain

1. **The restored ChatGPTN server is an always-reasoning vLLM deployment.**
   Non-streaming `/v1/chat/completions` returns `message.content` only after an
   unbounded reasoning phase; with a small `max_tokens` the entire budget is
   consumed by reasoning (`finish_reason=length`, `content=None`, measured:
   200-token budget, 200 completion tokens, empty content). Residency probes
   for the Ollama-native `/api/generate` endpoint 404 (vLLM serves only the
   OpenAI surface) — harmless warnings.
2. **Per-call latency exceeds the app's hardcoded non-streaming timeout.**
   Direct probes from inside the container: trivial prompts 5.4 s warm,
   18.2 s / 57.1 s / >90 s during warm-up; `create_thinking_client` pins
   `timeout=300.0` (backend/app/services/llm_client.py:1174, not
   settings-configurable). Draft research packets are large (evidence + JSON
   schema); on this server each call's reasoning phase exceeds 300 s, the read
   timeout fires, and the stage records a transient provider failure.
3. **The research stage's bounded-retry ladder then takes ~40-50 minutes to
   exhaust**, silently (provider exceptions are logged by type only, SPEC §20;
   httpx logs only completed requests). Observed terminal states: draft 6
   compile job 20 (vault 9) failed `provider_unavailable` at research after
   ~47 min; draft 8 compile job 24 (vault 2, compose) failed identically;
   draft 7 compile job 25 (vault 2, rewrite) walked the same ladder. The
   lease heartbeat renews throughout (the worker is alive, not crashed), and
   the single-flight DraftJobProcessor queues later compiles behind the
   wedged one (job 21/25 queueing observed).
4. **Even without the timeout, the server is too slow for the pipeline's
   budget.** The deployment's own pre-outage reference compile (draft 1, job
   10, 2026-08-22) needed 19 model calls in 56 minutes (~3 min/call on the
   pre-outage server). The restored server's large-prompt calls exceed 300 s
   each, so a ~20-call compile would run 1.5-5 h — beyond both the hardcoded
   client timeout and the job wall clock (`DRAFT_JOB_TIMEOUT_SECONDS=7200`).

## Verified NOT the cause

- The reindex dimension-probe bug (fixed in this PR) — reindex completes;
  unrelated code path.
- Search/embedding/vector paths — `/api/search` 0.1 s, embeddings 200 OK,
  hybrid search healthy in the same process while research stalls.
- The engine's retrieval adapters — per-kind failures are logged and
  degrade; none logged.
- Admission budget — in-process store, fresh per boot; first draft compile
  wedged identically on a clean boot with an empty budget.

## Unblock recipe (operator)

1. Restore the pre-outage serving profile for ChatGPTN on 172.16.50.41 (the
   2026-08-22-era server completed 19-call drafts at ~3 min/call), or serve a
   non-always-reasoning variant / disable the reasoning parser / lower
   reasoning effort.
2. Retry the mixed-source compose draft's latest failed compile job (the
   stage-cache resume restarts at its terminal stage — jobs 29/30/31 each
   resumed progressively further); when it completes, flip the
   gate-mixed-source-compose record in
   `docs/eval/2026-09-meridian-qualification-data.json` (the source-only
   rewrite already completed this way: job 28).
3. Optional product follow-up outside this qualification's scope (D2/E1):
   make the LLM client timeout settings-configurable and/or stream draft-stage
   model calls (streaming resets the read timeout per chunk, as the chat path
   already proves on this same server).

## Artifact map

- Terminal states + timings: requa3c_drafts_vault2.transcript.json
- Thinking endpoint healthy on streaming chat: requa2_thinking.transcript.json
- Raw server probes (warm-up latency, content-None shape): this file, section 1
- Outage-era failures retained: stageD3_draft_compile.transcript.json,
  draft_retry.transcript.json, stageB2_sessions.transcript.json
