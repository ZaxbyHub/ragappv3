# fix(rag): retrieval degradation stops being invisible; agentic tools stop working from preview snippets (#715)

## What changed

Workstream C PR 10 of 14 (audit findings T1-32-K-01/S-01/K-05/K-08/S2-01/S-06/K-06,
TQ-instruction-batch-4-07/14, T1-35-K-13). Ten defects shared one theme: when
retrieval degraded or was used through the agentic tool surface, the engine kept
answering as if nothing happened, or a documented fallback/contract was unreachable.

- **Degraded retrieval is distinguishable from a real abstention.** The
  maintenance/fallback chunk now carries a fixed generic `content` notice
  ("Retrieval is degraded, so this answer is not grounded in retrieved
  documents.") — never exception text, which stays in the `reason` field and
  server logs — as a separated block, which the stream route already re-emits
  as a visible content event and the non-streaming collector now appends too
  (new fallback branch); and a degraded no-evidence turn reports
  `abstention_basis "unavailable"` instead of `"decision"`. Cited answers
  keep `"decision"` — citing memories/wiki/KMS during document degradation is
  a real decision. The notice is persisted as part of the assistant message
  (it is the visible transcript text by design).
- **The OPS-006 503 contract fires under multi-scale defaults.** A
  `SearchSemaphoreTimeoutError` from any scale in `VectorStore.search`'s
  multi-scale gather (and from any sub-query in the decomposition fan-out)
  propagates instead of being classified as a failed arm and fused away.
  Generic per-scale failures keep the graceful degrade to `[]`.
- **The atomic-visibility filter's DB failure is visible.**
  `_get_indexed_file_ids` logs at WARNING (naming `indexed_file_ids`) instead
  of DEBUG, so the silent fail-open is operator-visible.
- **Maintenance mode gates every `_execute_retrieval` surface.** The gate
  moved to the single choke point `_execute_retrieval`: eval retrieval,
  Draft-Room document-sources, and the agentic `RetrievalTool` now inherit
  it (zero searches under maintenance) instead of only `query()`'s two
  branches; an agentic-flagged request under maintenance falls through to
  the standard pipeline's fallback handling (feedback review: the agentic
  path would otherwise swallow the gate and answer ungrounded with no
  degraded signal).
- **Per-request FTS attribution.** `fts_exceptions` is now request-scoped
  (contextvar buffer opened around the search phase and consumed by
  `get_fts_exceptions`), so concurrent requests can no longer steal each
  other's counts; the interface is unchanged for fake stores.
- **Honest rerank/hybrid status.** An enabled-but-failed rerank maps to
  `rerank_status "fallback"` (both the per-query path and the consolidated
  multi-sub-query rerank), never the turned-off `"disabled"`; `hybrid_status`
  reflects the effective per-query hybrid flag (`retrieval_mode "semantic"`
  reports `"disabled"`, not a lexical arm that never ran — feedback review
  found and removed the flag-blind second derivation that had been
  overwriting this value).
- **Agentic synthesis reads full chunk text.** `RetrievalTool` attaches the
  full chunk text on a private `_chunk_text` key; `SynthesisTool` renders it
  (XML-escaped, sharing an 8000-char global budget proportionally across
  every source — so retrieve_more rounds always contribute evidence instead
  of being silently dropped — with a 300-char per-source floor that takes
  priority beyond 26 sources, matching the old 300-char-preview envelope)
  instead of the 300-char UI preview; the engine strips the
  key in place before either wire boundary (done payload and
  `evidence_candidates`).
- **Planner sub-query fallback is live.** A `retrieve_more` decision without
  a `sub_query` re-runs the current query instead of retrieving with `""`.
- **Tool description matches the signature.** `RetrievalTool.description`
  names `top_k` alongside `query`/`vault_id`.

## Known limitations

- The planner's retrieve-more/synthesize **decision** view still sees a
  120-char snippet gist (`agentic_planner.py`); the fix targets the
  synthesis evidence, which the issue's acceptance criteria pin. Widening
  the decision view would grow planner prompts for no acceptance criterion.
- `_get_scoped_partial_file_ids` (the partial-files sibling of the fixed
  helper) still swallows DB failures at DEBUG — it fails CLOSED (empty set
  keeps the stricter unscoped filter), so it is out of the fail-open defect
  class; see the trace's recurrence census.
- The stream route's capacity-timeout frame is the pre-existing
  `SEARCH_UNAVAILABLE` SSE error event; the non-stream route raises HTTP 503.
- The maintenance gate covers the engine's `_execute_retrieval` choke point
  (the surface the issue enumerated); the standalone `/api/search` endpoint
  (`backend/app/api/routes/search.py`) calls `vector_store.search` directly
  and predates this change without a maintenance check — disclosed as a
  known gap, out of this issue's scope.
- The PR regenerates `backend/security/bandit-baseline.json` (line-number
  re-anchor only: 128 findings before and after, identical signature
  multiset, zero new suppressions) because the code edits shift lines in
  baselined files; per `backend/security/README.md` this is the required
  disclosure. Merging after a master advance requires re-running
  `python scripts/run_bandit.py --update-baseline` on the merged tree —
  hand-resolving the conflict drops the other side's re-anchored keys.
