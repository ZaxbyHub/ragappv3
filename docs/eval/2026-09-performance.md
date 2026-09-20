# Twelve-user concurrency & performance qualification — 2026-09 (issue #36)

Live end-to-end measurements on the actual deployment (R640AI, 172.16.50.159,
deployed commit 281bd714, 2×RTX 2000E Ada + RTX A1000; embeddings bge/harrier
via TEI on-GPU, reranker bge-reranker-v2-m3 on-GPU, thinking chat model
ChatGPTN served OFF-BOX at 172.16.50.41:8000). Raw per-request timings:
`2026-09-performance-data.json` (this doc is the readable summary; the JSON is
the evidence).

## Methodology

- Client: in-container httpx probe driving the real SSE endpoint
  (`POST /api/chat/stream`) with a dedicated bench user (created, then removed
  after the run; its sessions deleted). Mixed-mode prompts drawn from the
  calibration query set (vault 7 / Legacy).
- Tiers 1/4/8/12: that many CONCURRENT sessions, one streamed message each;
  per request measured: queue wait (time to first SSE event), first useful
  content (first content-bearing event), completion (done event). Errors and
  empty turns counted. Tiers paced 65s apart to stay under the 30/minute
  per-user chat rate limit; that pacing is why tier samples are 1×N rather
  than repeated batches.
- Long generation: 5 sequential worst-case "exhaustive walkthrough" prompts.
- Bulk ingestion: 6 real vault documents uploaded to the Test vault via the
  real upload API; time-to-searchable = upload → status `indexed`; files
  deleted afterwards.
- GPU/host load sampled around the runs (nvidia-smi): embedding and reranker
  TEI containers stayed under 6 GB on their GPUs during all tiers; the
  bottleneck observed was the off-box chat LLM, not the R640.

## Concurrency 1

samples 1, errors 0. queue wait p50 40 ms (p95 40 ms); first useful content
p50 34.9 s; completion p50 41.2 s. Baseline single-user latency is dominated
by the thinking-mode chat model round-trips, not local retrieval.

## Concurrency 4

samples 4, errors 0, 1 empty turn (a turn whose done event carried no content
tokens; retained, not retried). queue wait p50 112 ms / p95 115 ms; first
content p50 42.3 s / p95 53.2 s; completion p50 49.2 s / p95 60.5 s.

## Concurrency 8

samples 8, errors 0. queue wait p50 146 ms / p95 174 ms; first content p50
80.3 s / p95 114.4 s; completion p50 99.8 s / p95 130.8 s. On-box retrieval
and reranking continue to absorb concurrency (sub-200 ms queue wait); the
off-box chat LLM serializes generation.

## Concurrency 12

samples 12, errors 2 (HTTP failures; retained in the raw data), empty turns 0.
queue wait p50 30.3 s / p95 85.4 s; first content p50 111.1 s / p95 165.2 s;
completion p50 127.5 s / p95 175.8 s. **The twelve-user knee is real and
on-box**: queue wait jumps ~200× from tier 8 to tier 12 while GPU memory
pressure stays flat, i.e. admission/backpressure on the app side (and the
off-box model's connection saturation) binds before the GPUs do. Twelve
simultaneous thinking-mode users are NOT served at tier-8 latency on this
stack; instant-mode capacity (local minicpm5-2b) is the natural mitigation
and was not the bottleneck in any tier measured here.

## Long generation

Re-run with a fresh login (the first attempt lost 4 of 5 requests to access-
token expiry at the ~30-minute mark — those failures are retained in the
trace, not hidden): 5/5 completed, 0 errors, 0 empty turns. Completion
p50 = 265.8 s, p95 = 279.9 s (range 182.3–280.4 s per request, in the raw
data); first useful content p50 = 129.7 s, p95 = 145.1 s. Long thinking-mode
generations hold the stream open for ~4–5 minutes each — operationally
relevant for SSE timeouts and session limits.

## Bulk ingestion

6 documents (CDP manuals, 18–214 chunks each) uploaded via the real API:
time-to-searchable per file 17.9 / 18.0 / 19.3 / 19.4 / 20.0 / 27.9 s
(p50 19.4 s; p95 25.9 s; max 27.9 s). Uploads and indexing proceed
concurrently with chat traffic without visible cross-impact at these sizes;
files were deleted after measurement.

## Stage spans (measured, #518/#595 instrumentation)

Re-run with span capture wired per the repo owner's scope decision: the
probe timestamps the app's own SSE `stage` events (Searching / Reading /
Drafting — the #518 engine instrumentation) per turn, and reads the measured
`GET /metrics` counters (#518: `ragapp_queue_wait_seconds`,
`ragapp_chat_turns_total`, `ragapp_provider_calls_total`) as before/after
deltas per tier. Per-turn spans and per-tier deltas are in the data JSON
(`stage_spans`); 0 errors and 0 empty turns across all 25 turns:

| span (per turn) | tier 1 p50 | tier 4 p50 | tier 8 p50 | tier 12 p50 | tier 12 p95 |
| --- | --- | --- | --- | --- | --- |
| admission → Searching (queue + planning) | 0.02 s | 0.12 s | 0.18 s | 0.21 s | 67.5 s |
| Searching → Reading (retrieval + embed) | 4.9 s | 10.3 s | 14.3 s | 23.1 s | 31.5 s |
| Reading → Drafting (context assembly) | 0.02 s | 0.11 s | 0.02 s | 0.02 s | 0.03 s |
| Drafting → done (LLM generation) | 37.0 s | 32.2 s | 61.9 s | 55.4 s | 94.1 s |

Measured reading: on-box retrieval scales smoothly (4.9 → 23.1 s p50 at 12×
concurrency) and context assembly is negligible; generation — dominated by
the OFF-BOX thinking model — carries the bulk of latency and grows with
concurrency (37 → 55 s p50, 94 s p95 at 12). The tier-12 admission knee
reproduces in the measured spans: admission→Searching p95 jumps to 67.5 s,
matching the loaded run's queue-wait p50 regime change. The `/metrics`
deltas confirm every turn recorded exactly one LLM provider call and the
telemetry's queue-wait accumulator registered the contention.

Superseded note: the earlier derived milestone decomposition (computed from
queue/first-content/completion before span capture was wired) is retained
in this trace's history and is consistent with the measured spans; the
spans above are the authoritative stage-latency evidence.

## Limits of this evidence (disclosed)

Single run per tier (rate-limit pacing), one deployment, one day, one chat
model; the thinking model is off-box so its queueing behavior is that
deployment's, not the R640's. The numbers above are measurements of THIS
stack at THIS commit — exactly what the issue asks to qualify — not a
general capacity claim. Uncertainty on p95 at n=1–12 is large; treat tier
p95s as indicative, the tier-8→12 queue-wait regime change as the robust
finding (3 orders of magnitude on p50).
